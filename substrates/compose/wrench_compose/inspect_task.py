"""WRENCH compose substrate as an Inspect task.

``compose_sentinel(kinds=..., seeds=..., turns=...)`` builds one Sample per
(fault kind, seed); the solver is Inspect's message/generate loop around
``wrench_compose.episode.ComposeEpisode`` (which owns the compose stack,
the sandbox shell, the fixed post-fire window and the post-hoc metrics),
mirroring ``fle.eval.inspect.wrench.wrench_solver`` on the Factorio side.
The scorers are pure readers of the ``ComposeData`` store and re-run
``wrench_core.metrics.episode_metrics`` with the compose ``ScoringConfig``,
so the Inspect numbers and ``ComposeEpisode.finalize()`` cannot drift.

Concurrency: one episode per compose slot. ``WRENCH_COMPOSE_SLOTS`` (default
1) sizes ``wrench_compose.slots.slot_pool``; a sample past that count waits
for a free slot, the same way the Factorio solver waits for a server.

    inspect eval wrench_compose/inspect_task.py@compose_sentinel \\
        -T kinds=entity_destruction -T seeds=3 -T turns=30 --model openrouter/...

``compose-scripted/operator`` (``wrench_compose.policy``) is a scripted
Inspect model provider that repairs from the observation alone; it proves
the model path can recover, not just the fixture path.
"""

import asyncio
import logging
import re
import traceback
from collections.abc import Callable

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    ModelOutput,
    get_model,
)
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import StoreModel, store_as
from pydantic import Field
from wrench_core.metrics import episode_metrics

from wrench_compose import policy as _policy  # noqa: F401  (registers compose-scripted/*)
from wrench_compose.episode import (
    DEFAULT_TURN_PERIOD_S,
    DEFAULT_TURNS,
    NO_OUTPUT_FEEDBACK,
    ComposeEpisode,
    EpisodeError,
    parse_command,
)
from wrench_compose.kinds import parse_kinds, parse_seeds
from wrench_compose.report import ITEM
from wrench_compose.scoring import COMPOSE_CONFIG
from wrench_compose.slots import slot_pool

logger = logging.getLogger(__name__)

# One command per reply, but OpenRouter counts a reasoning model's thinking
# against max_tokens (and gemini-2.5-pro cannot switch thinking off), so
# 1024 came back as an empty completion turn after turn.
MAX_TOKENS = 4096
# Thinking budget for the providers that take one (Anthropic, Gemini);
# OpenAI-style models get reasoning_effort="low" instead.
REASONING_TOKENS = 1024
_OPENAI_REASONING_MODEL = re.compile(r"^(gpt-[5-9]|o[1-9])")
# Consecutive empty completions before the episode is abandoned as an error
# row instead of burning the rest of its turn budget.
NO_OUTPUT_TURNS = 4
# The Factorio solver keeps the system message plus the most recent 24
# messages. Compose observations are ~15x smaller, so the same window
# costs far less; it is kept for the one-contract story.
DEFAULT_CONTEXT_MESSAGES = 24


class ComposeData(StoreModel):
    """Store model for one compose episode (samples + ledger), populated by
    the solver after every turn and read by the scorers. Same fields as the
    Factorio ``WrenchData`` plus the compose identifiers."""

    samples: list[dict] = Field(default_factory=list)
    ledger_events: list[dict] = Field(default_factory=list)
    quota_item: str = Field(default=ITEM)
    quota: float = Field(default=0.0)
    kind: str = Field(default="")
    seed: int = Field(default=0)
    slot: int = Field(default=-1)
    end_tick: int = Field(default=0)
    steps_completed: int = Field(default=0)
    quota_met: bool = Field(default=False)
    error: str = Field(default="")
    shaped_rewards: list[dict] = Field(default_factory=list)


class NoOutputError(EpisodeError):
    """The model produced ``NO_OUTPUT_TURNS`` empty completions in a row."""


def generate_config(model_name: str) -> GenerateConfig:
    """Per-model generate config. OpenRouter maps ``reasoning_effort`` to
    ``reasoning.effort`` and ``reasoning_tokens`` to ``reasoning.max_tokens``
    (``inspect_ai.model._providers.openrouter``); the direct providers take
    them natively. Models without a reasoning knob (mockllm, the scripted
    operator, older chat models) get only the token/retry bounds."""
    name = model_name.lower()
    kwargs: dict = dict(max_tokens=MAX_TOKENS, max_retries=5, timeout=180)
    if _OPENAI_REASONING_MODEL.match(name.rsplit("/", 1)[-1]):
        kwargs["reasoning_effort"] = "low"
    elif any(vendor in name for vendor in ("claude", "anthropic", "gemini", "google")):
        kwargs["reasoning_tokens"] = REASONING_TOKENS
    return GenerateConfig(**kwargs)


def _sync_store(data: ComposeData, episode: ComposeEpisode) -> None:
    data.samples = list(episode.samples)
    data.ledger_events = list(episode.ledger_events)
    data.shaped_rewards = list(episode.shaped_rewards)
    data.steps_completed = episode.steps_completed
    data.quota_met = episode.quota_met
    data.end_tick = episode.end_tick


@solver
def compose_solver(
    turns: int | None = None,
    turn_period_s: float | None = None,
    context_messages: int = DEFAULT_CONTEXT_MESSAGES,
    out_root: str | None = None,
    episode_factory: Callable[..., ComposeEpisode] = ComposeEpisode,
):
    """Drive one ComposeEpisode, syncing the store after every turn.

    Mirrors the Factorio solver: the outer try/except records
    ``ComposeData.error``, the slot is released in ``finally``, and every
    turn-level failure consumes a turn instead of aborting the episode.
    Unlike the Factorio solver it then re-raises, so an infrastructure
    failure is ``sample.error`` (retried by ``retry_on_error``), never a
    0-fire "success".
    ``NO_OUTPUT_TURNS`` consecutive empty completions abandon the episode
    (``NoOutputError``) rather than burning the remaining turns.
    ``episode_factory`` exists for the unit tests (no Docker).
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        meta = state.metadata or {}
        kind = meta.get("kind", "entity_destruction")
        seed = int(meta.get("seed", 1))
        data = store_as(ComposeData)
        data.kind = kind
        data.seed = seed

        slot: int | None = None
        episode: ComposeEpisode | None = None
        try:
            pool = await slot_pool()
            slot = await pool.acquire()
            data.slot = slot
            episode = episode_factory(
                kind,
                seed,
                slot=slot,
                turns=int(turns or meta.get("turns") or DEFAULT_TURNS),
                turn_period_s=float(
                    turn_period_s if turn_period_s is not None else meta.get("turn_period_s", DEFAULT_TURN_PERIOD_S)
                ),
                out_root=out_root,
            )
            logger.info(f"WRENCH compose {kind} seed={seed}: slot {slot}, {episode.turns} turns")
            await asyncio.to_thread(episode.start)
            data.quota = episode.quota
            data.quota_item = episode.quota_item
            state.messages = [ChatMessageSystem(content=episode.system_prompt())]
            model = get_model()
            config = generate_config(model.name)

            turn = 0
            empty_turns = 0
            while not episode.is_done:
                turn += 1
                if len(state.messages) > context_messages + 1 and state.messages[0].role == "system":
                    state.messages = [state.messages[0]] + state.messages[-context_messages:]
                try:
                    state.messages.append(ChatMessageUser(content=await asyncio.to_thread(episode.observe)))
                    try:
                        state.output = await model.generate(input=state.messages, config=config)
                    except Exception as gen_err:  # noqa: BLE001
                        logger.warning(f"WRENCH compose turn {turn} generate() error: {gen_err}")
                        state.messages.append(ChatMessageAssistant(content="[generation failed this turn]"))
                        await asyncio.to_thread(episode.skip_step, f"Turn {turn} generation error: {gen_err}")
                        _sync_store(data, episode)
                        continue
                    completion = state.output.completion if state.output.choices else ""
                    if not completion.strip():
                        empty_turns += 1
                        state.messages.append(ChatMessageAssistant(content="[no output produced this turn]"))
                        await asyncio.to_thread(episode.skip_step, NO_OUTPUT_FEEDBACK)
                        _sync_store(data, episode)
                        if empty_turns >= NO_OUTPUT_TURNS:
                            raise NoOutputError(f"model produced no output for {NO_OUTPUT_TURNS} consecutive turns")
                        continue
                    empty_turns = 0
                    state.messages.append(state.output.message)
                    await asyncio.to_thread(episode.step, parse_command(completion))
                    _sync_store(data, episode)
                except EpisodeError:
                    raise
                except Exception as step_err:  # noqa: BLE001
                    logger.error(f"WRENCH compose turn {turn} error: {step_err}")
                    episode.fail_step(f"Turn {turn} error: {step_err}")

            await asyncio.to_thread(episode.finalize)
            _sync_store(data, episode)
            state.output = ModelOutput(completion=episode.summary(), model=meta.get("model", "unknown"))
            state.completed = True
        except Exception as e:  # noqa: BLE001
            if isinstance(e, EpisodeError):
                error_msg = str(e)
            else:
                error_msg = f"WRENCH compose solver error: {e}\n{traceback.format_exc()}"
            logger.error(error_msg)
            data.error = error_msg
            if episode is not None:
                episode.error = error_msg
                _sync_store(data, episode)
            state.output = ModelOutput(completion=f"Error in WRENCH compose episode: {e}", model="unknown")
        finally:
            if episode is not None:
                await asyncio.to_thread(episode.cleanup)
            if slot is not None:
                try:
                    await (await slot_pool()).release(slot)
                except Exception as release_err:  # noqa: BLE001
                    logger.error(f"error releasing slot {slot}: {release_err}")
        if data.error:
            # Re-raise after the cleanup so Inspect records ``sample.error``
            # and ``retry_on_error`` gets another attempt; ``fail_on_error=False``
            # keeps the rest of the eval running.
            raise EpisodeError(data.error)
        return state

    return solve


# --------------------------------------------------------------------------
# Scorers: pure readers of the store, the fork's value/metadata shapes
# --------------------------------------------------------------------------


def metrics_from(data: ComposeData) -> dict:
    return episode_metrics(
        data.samples, data.ledger_events, data.quota_item or ITEM, data.end_tick, config=COMPOSE_CONFIG
    )


def throughput_retained_score(data: ComposeData) -> Score:
    block = metrics_from(data)["throughput_retained"]
    pooled = block["value"]
    num_fires = block["metadata"]["num_fires"]
    return Score(
        value=pooled if pooled is not None else float("nan"),
        answer=f"{pooled:.3f}" if pooled is not None else "unscoreable",
        explanation=(
            f"TR pooled over {num_fires} fire(s): {pooled:.3f}"
            if pooled is not None
            else f"Not scoreable: {num_fires} fire(s), no valid baseline"
        ),
        metadata=block["metadata"],
    )


def recovery_score(data: ComposeData) -> Score:
    block = metrics_from(data)["recovery"]
    rate = block["value"]
    meta = block["metadata"]
    scoreable = meta["scoreable"]
    return Score(
        value=rate if rate is not None else float("nan"),
        answer=f"{meta['recovered']}/{meta['scoreable_fires']}" if scoreable else "unscoreable",
        explanation=(
            f"Recovered {meta['recovered']} of {meta['scoreable_fires']} scoreable fire(s)"
            if scoreable
            else f"Not scoreable: {meta['num_fires']} fire(s), no valid baseline"
        ),
        metadata=meta,
    )


def detection_score(data: ComposeData) -> Score:
    meta = metrics_from(data)["detection"]["metadata"]
    return Score(
        value=meta["recall"],
        answer=f"recall={meta['recall']:.2f}",
        explanation=(
            f"Detection over {meta['num_fires']} fire(s), {meta['num_reports']} report(s): "
            f"recall={meta['recall']:.2f}, precision_strict={meta['precision_strict']:.2f} "
            f"(loose {meta['precision']:.2f})"
        ),
        metadata=meta,
    )


@scorer(metrics=[mean()])
def throughput_retained() -> Scorer:
    """Pooled, winsorized Throughput Retained over the episode's fires.
    NaN when no fire was scoreable; metadata carries the raw pooled
    numerator/denominator (and the redundancy-floor-adjusted pair) for
    cross-seed pooling."""

    async def score(state: TaskState, target: Target) -> Score:
        return throughput_retained_score(store_as(ComposeData))

    return score


@scorer(metrics=[mean()])
def recovery() -> Scorer:
    """Fraction of fires whose trailing rate returned to 0.9x the frozen
    baseline for two consecutive samples before the window closed."""

    async def score(state: TaskState, target: Target) -> Score:
        return recovery_score(store_as(ComposeData))

    return score


@scorer(metrics=[mean()])
def detection() -> Scorer:
    """Detection recall (value) plus precision/latency detail (metadata)."""

    async def score(state: TaskState, target: Target) -> Score:
        return detection_score(store_as(ComposeData))

    return score


# --------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------


def create_compose_task(
    kinds=None,
    seeds=None,
    turns: int = DEFAULT_TURNS,
    turn_period_s: float = DEFAULT_TURN_PERIOD_S,
    context_messages: int = DEFAULT_CONTEXT_MESSAGES,
    out_root: str | None = None,
    name: str | None = None,
    episode_factory: Callable[..., ComposeEpisode] = ComposeEpisode,
) -> Task:
    """One Sample per (kind, seed). ``kinds`` and ``seeds`` accept lists or
    comma-separated strings (``-T kinds=belt_cut,adaptive_strike -T seeds=1,3``)."""
    kind_list = parse_kinds(kinds)
    seed_list = parse_seeds(seeds)
    samples = [
        Sample(
            input="Begin: operate the pipeline and keep committed jobs per minute at or above the quota.",
            target="success",
            metadata={
                "kind": kind,
                "seed": seed,
                "turns": int(turns),
                "turn_period_s": float(turn_period_s),
                "substrate": "compose",
            },
            id=f"{kind}_seed{seed}",
        )
        for kind in kind_list
        for seed in seed_list
    ]
    return Task(
        dataset=samples,
        solver=compose_solver(
            turns=int(turns),
            turn_period_s=float(turn_period_s),
            context_messages=int(context_messages),
            out_root=out_root,
            episode_factory=episode_factory,
        ),
        scorer=[throughput_retained(), recovery(), detection()],
        name=name or (kind_list[0] if len(kind_list) == 1 else "compose_sentinel"),
    )


@task
def compose_sentinel(
    kinds: str | None = None,
    seeds: str | None = None,
    turns: int = DEFAULT_TURNS,
    turn_period_s: float = DEFAULT_TURN_PERIOD_S,
    context_messages: int = DEFAULT_CONTEXT_MESSAGES,
    out_root: str | None = None,
) -> Task:
    """The compose sentinel: kinds x seeds, default all four kinds at seed 1."""
    return create_compose_task(
        kinds=kinds,
        seeds=seeds,
        turns=turns,
        turn_period_s=turn_period_s,
        context_messages=context_messages,
        out_root=out_root,
        name="compose_sentinel",
    )
