"""The ``verifiers`` package (``environments/wrench_compose_env``) through
verifiers' own rollout loop with a scripted client and a fake episode (no
Docker): turn parsing, the no-command / empty-reply paths, completion when
the episode is done (the last command still runs), context trimming, slot
release, and the rubric over a canned ``finalize()`` record. Mirrors the
fork's ``tests/wrench/test_verifiers_env.py``."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

vf = pytest.importorskip("verifiers")

from verifiers.types import Response, ResponseMessage, Usage  # noqa: E402

from wrench_compose.episode import NO_COMMAND_FEEDBACK, NO_OUTPUT_FEEDBACK  # noqa: E402

ENV_FILE = Path(__file__).resolve().parents[1] / "environments" / "wrench_compose_env" / "wrench_compose_env.py"


def _load_env_module():
    if "wrench_compose_env" in sys.modules:
        return sys.modules["wrench_compose_env"]
    spec = importlib.util.spec_from_file_location("wrench_compose_env", ENV_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["wrench_compose_env"] = module
    spec.loader.exec_module(module)
    return module


wce = _load_env_module()


class ScriptedClient(vf.Client):
    """Returns the next scripted reply on every call."""

    def __init__(self, replies):
        super().__init__(object())
        self.replies = list(replies)
        self.prompts = []

    def setup_client(self, config):
        return object()

    async def to_native_tool(self, tool):
        return tool

    async def to_native_prompt(self, messages):
        return messages, {}

    async def get_native_response(self, prompt, model, sampling_args, tools=None, **kwargs):
        return None

    async def raise_from_native_response(self, response):
        return None

    async def from_native_response(self, response):
        return response

    async def close(self):
        return None

    async def get_response(self, prompt, model, sampling_args, tools=None, **kwargs):
        self.prompts.append(list(prompt))
        text = self.replies.pop(0) if self.replies else "```sh\nwrenchctl metrics\n```"
        return Response(
            id=f"scripted-{len(self.prompts)}",
            created=0,
            model=model,
            usage=Usage(prompt_tokens=0, reasoning_tokens=0, completion_tokens=0, total_tokens=0),
            message=ResponseMessage(content=text, finish_reason="stop", is_truncated=False),
        )


CANNED_RESULT = {
    "kind": "entity_destruction",
    "seed": 3,
    "turns": 3,
    "steps_taken": 3,
    "steps_completed": 2,
    "quota_met": True,
    "quota_item": "jobs_done",
    "quota": 400.0,
    "end_tick": 180000,
    "error": "",
    "samples": [],
    "ledger_events": [],
    "shaped_rewards": [{"tick": 70000, "fire_tick": 60000, "phi": 0.4, "delta": 0.1}],
    "fires": [{"kind": "entity_destruction", "tick": 60000, "seed": 3}],
    "num_fires": 1,
    "metrics": {
        "throughput_retained": 0.73,
        "throughput_retained_raw": 0.73,
        "throughput_retained_floor_adj": 0.55,
        "tr_scoreable": True,
        "tr_pooled_numerator": 146.0,
        "tr_pooled_denominator": 200.0,
        "recovery_rate": 1.0,
        "recovery_scoreable_fires": 1,
        "recovery_recovered": 1,
        "time_to_recovery_ticks": 1234.0,
        "detection_recall": 1.0,
        "detection_precision_strict": 0.5,
        "detection_precision": 1.0,
        "detection_latency_ticks": 300.0,
        "num_fires": 1,
        "num_reports": 2,
    },
    "scores": {},
}


class FakeEpisode:
    """Same surface as ComposeEpisode, no Docker. Records every call."""

    instances = []

    def __init__(self, kind, seed=1, *, slot=None, turns=None, turn_period_s=6.0, fail_start=False):
        self.kind = kind
        self.seed = seed
        self.slot = slot
        self.turns = int(turns or 3)
        self.fail_start = fail_start
        self.started = False
        self.turn = 0
        self.steps_completed = 0
        self.quota_met = False
        self.shaped_rewards = []
        self.feedback = "The pipeline is up."
        self.calls = []
        self.cleaned_up = False
        self.finalized = False
        FakeEpisode.instances.append(self)

    def start(self):
        if self.fail_start:
            raise ConnectionError("no docker")
        self.started = True
        return self

    @property
    def is_done(self):
        return self.turn >= self.turns

    def observe(self):
        return f"{self.feedback}\n\n---\n\nobservation for turn {self.turn + 1}/{self.turns}"

    def step(self, command):
        self.calls.append(("step", command))
        if command:
            self.steps_completed += 1
            if self.turn >= 1:
                self.shaped_rewards.append({"tick": 1000 * self.turn, "fire_tick": 1000, "phi": 0.1, "delta": 0.05})
            feedback = f"## Turn {self.turn + 1} result\n\n```\nran: {command}\n```"
        else:
            feedback = NO_COMMAND_FEEDBACK
        self.feedback = feedback
        self.turn += 1
        return feedback

    def skip_step(self, feedback):
        self.calls.append(("skip", feedback))
        self.feedback = feedback
        self.turn += 1
        return feedback

    def finalize(self):
        self.finalized = True
        result = dict(CANNED_RESULT)
        result["steps_completed"] = self.steps_completed
        result["quota_met"] = self.steps_completed >= 2
        return result

    def cleanup(self):
        self.cleaned_up = True


class FakePool:
    def __init__(self, size=1):
        self.available = list(range(size))
        self.released = []

    async def acquire(self):
        return self.available.pop(0)

    async def release(self, slot):
        self.released.append(slot)
        self.available.append(slot)


def make_env(pool, turns=3, **kwargs):
    async def pool_factory():
        return pool

    return wce.WrenchComposeEnv(
        dataset=wce.build_dataset(kinds="entity_destruction", seeds=3, turns=turns),
        rubric=wce.build_rubric(),
        episode_factory=FakeEpisode,
        pool_factory=pool_factory,
        **kwargs,
    )


def run_rollout(env, client, sampling_args=None):
    async def _run():
        row = env.get_dataset()[0]
        state = await env.rollout(row, client, "scripted", sampling_args or {})
        await env.rubric.score_rollout(state)
        return state

    return asyncio.run(_run())


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeEpisode.instances.clear()
    yield
    FakeEpisode.instances.clear()


def test_dataset_rows_are_kinds_times_seeds():
    ds = wce.build_dataset(kinds="entity_destruction,belt_cut", seeds="1,3", turns=4)
    assert len(ds) == 4
    row = ds[0]
    assert [m["role"] for m in row["prompt"]] == ["system"]
    assert "exactly ONE shell command line" in row["prompt"][0]["content"]
    assert "You have 4 turns" in row["prompt"][0]["content"]
    assert row["info"]["kind"] == "entity_destruction" and row["info"]["turns"] == 4
    assert {r["info"]["seed"] for r in ds} == {1, 3}
    with pytest.raises(ValueError):
        wce.build_dataset(kinds="not_a_kind")
    assert len(wce.build_dataset()) == 4


def test_load_environment_defaults():
    env = wce.load_environment(kinds="belt_cut", turns=2)
    assert isinstance(env, vf.MultiTurnEnv)
    assert env.max_turns == -1  # completion comes from final_env_response
    assert env.sampling_args["max_tokens"] == wce.DEFAULT_MAX_TOKENS
    assert isinstance(env.rubric, vf.RubricGroup)
    wrench_rubric = env.rubric.rubrics[0]
    names = wrench_rubric._get_reward_func_names()
    assert names[0] == "throughput_retained" and wrench_rubric.weights[0] == 1.0
    assert set(wrench_rubric.weights[1:]) == {0.0}
    assert "detection_precision_strict" in names and "quota_met" in names and "command_rate" in names


def test_turn_parsing_and_completion():
    pool = FakePool()
    env = make_env(pool, turns=3)
    client = ScriptedClient(
        [
            "Let me look around.\n```sh\nwrenchctl ps\n```",
            "I am not sure yet.\nLet me think about it.",  # no command
            "```sh\nwrenchctl scale worker 2\n```",  # last turn: must still execute
        ]
    )
    state = run_rollout(env, client)

    episode = FakeEpisode.instances[0]
    assert episode.calls == [("step", "wrenchctl ps"), ("step", None), ("step", "wrenchctl scale worker 2")]
    assert episode.slot == 0
    assert episode.is_done and episode.finalized and episode.cleaned_up
    assert state["stop_condition"] == "has_final_env_response"
    assert state["is_completed"] and state["error"] is None
    assert len(state["trajectory"]) == 3
    assert pool.released == [0]
    assert state.get("wrench_episode") is None
    assert state["wrench"]["steps_completed"] == 2

    first_prompt = client.prompts[0]
    assert [m.role for m in first_prompt] == ["system", "user"]
    assert "observation for turn 1/3" in first_prompt[1].content
    assert NO_COMMAND_FEEDBACK in client.prompts[2][-1].content
    extras = [s["extras"]["wrench"] for s in state["trajectory"]]
    assert [e["had_command"] for e in extras] == [True, False, True]
    assert [e["command_ran"] for e in extras] == [True, False, True]
    assert extras[2]["shaped_reward"]["delta"] == 0.05
    assert all(s["reward"] is None for s in state["trajectory"])
    roles = [m.role for m in state["completion"]]
    assert roles == ["assistant", "user"] * 3
    assert "Episode complete" in state["completion"][-1].content

    assert state["reward"] == pytest.approx(0.73)
    m = state["metrics"]
    assert m["throughput_retained"] == pytest.approx(0.73)
    assert m["throughput_retained_floor_adj"] == pytest.approx(0.55)
    assert m["tr_scoreable"] == 1.0 and m["tr_pooled_denominator"] == 200.0
    assert m["recovery_rate"] == 1.0 and m["time_to_recovery_ticks"] == 1234.0
    assert m["detection_recall"] == 1.0 and m["detection_precision_strict"] == 0.5
    assert m["num_fires"] == 1.0 and m["quota_met"] == 1.0 and m["steps_completed"] == 2.0
    assert m["command_rate"] == pytest.approx(2 / 3)
    assert m["num_turns"] == 3


def test_empty_reply_is_a_skipped_turn():
    pool = FakePool()
    env = make_env(pool, turns=2)
    client = ScriptedClient(["", "```sh\nwrenchctl ps\n```"])
    state = run_rollout(env, client)
    episode = FakeEpisode.instances[0]
    assert episode.calls[0] == ("skip", NO_OUTPUT_FEEDBACK)
    assert episode.calls[1] == ("step", "wrenchctl ps")
    assert NO_OUTPUT_FEEDBACK in client.prompts[1][-1].content
    assert state["stop_condition"] == "has_final_env_response"


def test_start_failure_releases_the_slot():
    pool = FakePool()

    def failing_factory(kind, seed=1, **kw):
        return FakeEpisode(kind, seed, fail_start=True, **kw)

    async def pool_factory():
        return pool

    env = wce.WrenchComposeEnv(
        dataset=wce.build_dataset(kinds="belt_cut", seeds=1, turns=1),
        rubric=wce.build_rubric(),
        episode_factory=failing_factory,
        pool_factory=pool_factory,
    )
    state = run_rollout(env, ScriptedClient([]))
    assert state["error"] is not None and "could not start" in str(state["error"])
    assert pool.released == [0] and FakeEpisode.instances[0].cleaned_up


def test_shaped_turn_rewards_opt_in():
    env = make_env(FakePool(), turns=3, shaped_turn_rewards=True)
    state = run_rollout(env, ScriptedClient(["```sh\nwrenchctl ps\n```"] * 3))
    assert [s["reward"] for s in state["trajectory"]] == [0.0, 0.05, 0.05]


def test_context_window_trims_like_the_inspect_harness():
    env = make_env(FakePool(), turns=5, max_context_messages=2)
    client = ScriptedClient(["```sh\nwrenchctl ps # %d\n```" % i for i in range(5)])
    state = run_rollout(env, client)
    lengths = [len(p) for p in client.prompts]
    assert lengths == [2, 4, 4, 4, 4]
    for prompt in client.prompts[1:]:
        assert prompt[0].role == "system"
        assert [m.role for m in prompt[1:]] == ["user", "assistant", "user"]
    assert "observation for turn 2/5" in client.prompts[2][1].content
    assert len(state["completion"]) == 10
