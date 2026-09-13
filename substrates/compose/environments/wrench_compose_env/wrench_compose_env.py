"""WRENCH compose substrate as a Prime Intellect ``verifiers`` environment.

WRENCH measures disruption recovery. Here the disrupted system is a
docker-compose job pipeline (gateway -> redis -> workers -> postgres); the
agent is its on-call operator, acting through ``wrenchctl`` from a sandbox
container, one shell command per turn. Once the pipeline demonstrably meets
its quota a seeded fault strikes it (nothing is announced), and the
benchmark scores -- against ground truth the agent never sees -- how much
throughput survived (Throughput Retained), whether it recovered, and
whether the agent reported the faulty service.

This module is a thin ``MultiTurnEnv`` around
``wrench_compose.episode.ComposeEpisode``, the same episode driver the
Inspect task uses, so the two harnesses produce identical observations and
identical metrics; it mirrors ``environments/wrench_factorio`` in the
Factorio substrate turn for turn.

Turn protocol (one turn == one command):

    system: the operator briefing + wrenchctl --help (from the dataset row)
    user:   initial observation (ps + metrics, appended in ``setup_state``)
    assistant: ```sh\nwrenchctl ...\n```
    user:   command result + next ps + metrics
    ...
    user (final): the last command's result; episode over

The final model turn's command is executed too: verifiers checks stop
conditions *before* it would call ``env_response``, so ``max_turns`` is
deliberately left unset and completion is signalled with
``state["final_env_response"]`` once the episode is done. ``finalize()``
(which waits for the post-fire window to close) runs at that point.

Docker with the ``wrench-svc:local`` / ``wrench-agent:local`` images is
required (``make build``). Rollouts are bound to compose slots through
``wrench_compose.slots``; concurrency beyond ``WRENCH_COMPOSE_SLOTS`` waits.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import verifiers as vf
from datasets import Dataset
from verifiers.types import Messages, State
from verifiers.utils.message_utils import concat_messages, maybe_normalize_messages

from wrench_compose.episode import (
    DEFAULT_TURN_PERIOD_S,
    DEFAULT_TURNS,
    NO_OUTPUT_FEEDBACK,
    ComposeEpisode,
    parse_command,
)
from wrench_compose.kinds import parse_kinds, parse_seeds
from wrench_compose.prompt import system_prompt
from wrench_compose.slots import slot_pool

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 1024
# The Inspect harness keeps the system message plus the most recent 24 messages.
DEFAULT_MAX_CONTEXT_MESSAGES = 24


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


class WrenchComposeEnv(vf.MultiTurnEnv):
    """One compose episode per rollout.

    Rollout-local objects live in ``state``:

    - ``state["wrench_episode"]``: the live ``ComposeEpisode`` (dropped at cleanup);
    - ``state["wrench_slot"]``: the compose slot, released at cleanup;
    - ``state["wrench"]``: ``ComposeEpisode.finalize()`` -- samples, ledger
      events, fires, quota_met, end_tick, shaped rewards, every metric --
      populated when the episode ends (or at cleanup on an abnormal exit).
      Pass ``-C wrench`` to ``vf-eval`` to save it with the results.
    - ``state["trajectory"][i]["extras"]["wrench"]``: per-turn record.
    """

    def __init__(
        self,
        *,
        max_context_messages: int = DEFAULT_MAX_CONTEXT_MESSAGES,
        shaped_turn_rewards: bool = False,
        episode_factory: Callable[..., ComposeEpisode] = ComposeEpisode,
        pool_factory: Callable[[], Any] = slot_pool,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.max_context_messages = int(max_context_messages)
        self.shaped_turn_rewards = bool(shaped_turn_rewards)
        self._episode_factory = episode_factory
        self._pool_factory = pool_factory

    # -- rollout lifecycle -------------------------------------------------

    async def setup_state(self, state: State, **kwargs) -> State:
        info = state.get("info") or {}
        kind = info["kind"]
        seed = int(info.get("seed", 1))

        state["wrench"] = None
        state["wrench_episode"] = None
        state["wrench_env_responses"] = []

        pool = await self._pool_factory()
        slot = await pool.acquire()
        state["wrench_slot"] = slot
        try:
            episode = self._episode_factory(
                kind,
                seed,
                slot=slot,
                turns=int(info.get("turns") or DEFAULT_TURNS),
                turn_period_s=float(info.get("turn_period_s", DEFAULT_TURN_PERIOD_S)),
            )
            state["wrench_episode"] = episode
            await asyncio.to_thread(episode.start)
            initial_observation = await asyncio.to_thread(episode.observe)
        except Exception as exc:
            # The cleanup handler releases the slot and tears the stack down.
            raise vf.InfraError(f"WRENCH compose {kind} seed={seed}: could not start on slot {slot}: {exc}") from exc
        logger.info(f"WRENCH compose {kind} seed={seed}: slot {slot}, {episode.turns} turns")
        state["prompt"] = concat_messages([state["prompt"], [vf.UserMessage(content=initial_observation)]])
        return state

    async def env_response(self, messages: Messages, state: State, **kwargs) -> Messages:
        episode: ComposeEpisode = state["wrench_episode"]
        turn_index = episode.turn
        reply = self.parser._content_to_text(self.parser._message_field(messages[-1], "content"))
        command = parse_command(reply)
        completed_before = episode.steps_completed
        try:
            if not reply.strip():
                feedback = await asyncio.to_thread(episode.skip_step, NO_OUTPUT_FEEDBACK)
            else:
                feedback = await asyncio.to_thread(episode.step, command)
            if episode.is_done:
                state["wrench"] = await asyncio.to_thread(episode.finalize)
                response = [
                    vf.UserMessage(
                        content=f"{feedback}\n\n---\n\nEpisode complete: {episode.turn} of {episode.turns} turns used."
                    )
                ]
                state["final_env_response"] = response
            else:
                observation = await asyncio.to_thread(episode.observe)
                response = [vf.UserMessage(content=observation)]
        except Exception as exc:
            raise vf.InfraError(f"WRENCH compose {episode.kind} turn {turn_index + 1} failed: {exc}") from exc

        shaped = episode.shaped_rewards[-1] if episode.shaped_rewards else None
        step = state["trajectory"][-1]
        step["extras"]["wrench"] = {
            "turn": turn_index,
            "had_command": command is not None,
            "command_ran": episode.steps_completed > completed_before,
            "quota_met": episode.quota_met,
            "shaped_reward": shaped,
        }
        if self.shaped_turn_rewards:
            step["reward"] = float(shaped["delta"]) if shaped else 0.0
        state["wrench_env_responses"].append(response)
        return response

    async def get_prompt_messages(self, state: State) -> Messages:
        """``MultiTurnEnv``'s prompt with the Inspect harness's context
        window: system message + the most recent ``max_context_messages``."""
        if len(state["trajectory"]) == 0:
            return state["prompt"]
        prev = state["trajectory"][-1]
        messages = concat_messages([prev["prompt"], prev["completion"]])
        env_response = await self.env_response(messages, state)
        env_response = maybe_normalize_messages(env_response, field_name="env_response")
        return self._trim(concat_messages([messages, env_response]))

    def _trim(self, messages: Messages) -> Messages:
        limit = self.max_context_messages
        if limit <= 0 or len(messages) <= limit + 2:
            return messages
        first_role = self.parser._message_field(messages[0], "role")
        if first_role != "system":
            return messages
        return [messages[0]] + list(messages[-(limit + 1) :])

    async def render_completion(self, state: State) -> None:
        """The full, untrimmed conversation after the prompt."""
        completion: Messages = []
        responses = state.get("wrench_env_responses") or []
        for i, step in enumerate(state["trajectory"]):
            completion.extend(step["completion"])
            if i < len(responses):
                completion.extend(responses[i])
        state["completion"] = completion

    @vf.cleanup
    async def close_episode(self, state: State) -> None:
        """Finalize on abnormal exit, tear the stack down, release the slot."""
        episode: ComposeEpisode | None = state.get("wrench_episode")
        if episode is not None:
            if state.get("wrench") is None and episode.started:
                try:
                    state["wrench"] = await asyncio.to_thread(episode.finalize)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"WRENCH compose finalize failed at cleanup: {exc}")
            await asyncio.to_thread(episode.cleanup)
            state["wrench_episode"] = None
        slot = state.pop("wrench_slot", None)
        if slot is not None:
            try:
                pool = await self._pool_factory()
                await pool.release(slot)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"error releasing slot {slot}: {exc}")


# --------------------------------------------------------------------------
# Rubric
# --------------------------------------------------------------------------


def _scalar(state: State, key: str, default: float = 0.0) -> float:
    result = state.get("wrench") or {}
    value = (result.get("metrics") or {}).get(key)
    if value is None:
        return default
    return float(value)


def throughput_retained(state: State) -> float:
    """Reward. Pooled, winsorized Throughput Retained over every fired
    fault (sum actual / sum expected post-fire jobs against the frozen
    pre-fire baseline, clamped to [-0.5, 1.5]). 0.0 when nothing fired or
    no fire had a valid baseline; check ``tr_scoreable`` before pooling."""
    return _scalar(state, "throughput_retained")


def tr_scoreable(state: State) -> float:
    """1.0 when at least one fire had a valid frozen baseline."""
    return _scalar(state, "tr_scoreable")


def throughput_retained_raw(state: State) -> float:
    """Pooled TR before the winsorize clamp."""
    return _scalar(state, "throughput_retained_raw")


def throughput_retained_floor_adj(state: State) -> float:
    """TR with the passive-redundancy floor removed (entity_destruction
    fires only); 0.0 when undefined."""
    return _scalar(state, "throughput_retained_floor_adj")


def tr_pooled_numerator(state: State) -> float:
    return _scalar(state, "tr_pooled_numerator")


def tr_pooled_denominator(state: State) -> float:
    return _scalar(state, "tr_pooled_denominator")


def recovery_rate(state: State) -> float:
    """Fraction of scoreable fires whose trailing rate returned to >= 0.9x
    baseline for two consecutive samples before the window closed."""
    return _scalar(state, "recovery_rate")


def time_to_recovery_ticks(state: State) -> float:
    """Mean ms from fire to sustained recovery, right-censored at the window."""
    return _scalar(state, "time_to_recovery_ticks")


def detection_recall(state: State) -> float:
    """Fraction of fires matched by a ``report_fault`` on the right service
    (or one dependency hop away); gated to 0 when loose precision < 0.5;
    1.0 with no fires (vacuous)."""
    return _scalar(state, "detection_recall", default=1.0)


def detection_precision_strict(state: State) -> float:
    """Reports naming exactly the faulted service / all reports."""
    return _scalar(state, "detection_precision_strict", default=1.0)


def detection_precision(state: State) -> float:
    return _scalar(state, "detection_precision", default=1.0)


def detection_latency_ticks(state: State) -> float:
    return _scalar(state, "detection_latency_ticks")


def num_fires(state: State) -> float:
    return _scalar(state, "num_fires")


def quota_met(state: State) -> float:
    """1.0 when the pipeline demonstrated the quota (the fault armed)."""
    result = state.get("wrench") or {}
    return 1.0 if result.get("quota_met") else 0.0


def steps_completed(state: State) -> float:
    """Turns whose command actually ran in the sandbox."""
    result = state.get("wrench") or {}
    return float(result.get("steps_completed") or 0)


def command_rate(state: State) -> float:
    """Fraction of model turns that yielded a runnable command line."""
    steps = state.get("trajectory") or []
    if not steps:
        return 0.0
    hits = sum(1 for s in steps if (s.get("extras") or {}).get("wrench", {}).get("had_command"))
    return hits / len(steps)


def build_rubric() -> vf.Rubric:
    rubric = vf.Rubric()
    rubric.add_reward_func(throughput_retained, weight=1.0)
    for metric in (
        tr_scoreable,
        throughput_retained_raw,
        throughput_retained_floor_adj,
        tr_pooled_numerator,
        tr_pooled_denominator,
        recovery_rate,
        time_to_recovery_ticks,
        detection_recall,
        detection_precision_strict,
        detection_precision,
        detection_latency_ticks,
        num_fires,
        quota_met,
        steps_completed,
        command_rate,
    ):
        rubric.add_metric(metric)
    return rubric


# --------------------------------------------------------------------------
# Dataset + loader
# --------------------------------------------------------------------------


def build_dataset(
    kinds: Any = None,
    seeds: Any = None,
    turns: int | None = None,
    turn_period_s: float = DEFAULT_TURN_PERIOD_S,
    workers: int = 2,
    quota: int = 400,
) -> Dataset:
    """One row per (kind, seed). The system prompt is built offline; the
    first observation is read from the live stack in ``setup_state``."""
    rows = []
    turn_budget = int(turns or DEFAULT_TURNS)
    prompt = system_prompt(workers=workers, turns=turn_budget, quota=quota)
    for kind in parse_kinds(kinds):
        for seed in parse_seeds(seeds):
            rows.append(
                {
                    "prompt": [{"role": "system", "content": prompt}],
                    "answer": "",
                    "info": {
                        "kind": kind,
                        "seed": seed,
                        "turns": turn_budget,
                        "turn_period_s": float(turn_period_s),
                        "quota_item": "jobs_done",
                        "quota": float(quota),
                    },
                }
            )
    return Dataset.from_list(rows)


def load_environment(
    kinds: Any = None,
    seeds: Any = None,
    turns: int | None = None,
    turn_period_s: float = DEFAULT_TURN_PERIOD_S,
    max_context_messages: int = DEFAULT_MAX_CONTEXT_MESSAGES,
    shaped_turn_rewards: bool = False,
    **kwargs,
) -> vf.Environment:
    """WRENCH compose disruption-recovery environment.

    Args:
        kinds: fault kind(s) -- a list or comma-separated string. Default:
            all four (``entity_destruction``, ``belt_cut``,
            ``resource_exhaustion``, ``adaptive_strike``).
        seeds: seed(s) per kind (rows = kinds x seeds); default ``1``.
        turns: turn budget per episode (default 30).
        turn_period_s: minimum seconds between two executed commands.
        max_context_messages: messages kept after the system prompt in each
            model call (0 = never trim).
        shaped_turn_rewards: write each turn's potential-based shaping
            delta into ``trajectory[i]["reward"]``.
        **kwargs: forwarded to ``MultiTurnEnv`` (e.g. ``timeout_seconds``).
    """
    dataset = build_dataset(kinds=kinds, seeds=seeds, turns=turns, turn_period_s=turn_period_s)
    sampling_args = {"max_tokens": DEFAULT_MAX_TOKENS}
    sampling_args.update(kwargs.pop("sampling_args", None) or {})
    return WrenchComposeEnv(
        dataset=dataset,
        eval_dataset=dataset,
        rubric=build_rubric(),
        sampling_args=sampling_args,
        max_context_messages=max_context_messages,
        shaped_turn_rewards=shaped_turn_rewards,
        **kwargs,
    )
