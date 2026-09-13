"""The Inspect solver end to end through ``inspect_ai.eval`` with the fake
stack and sandbox (no Docker): the no-output guard, error propagation and
retry, the no-fire terminal event, and slot release under cancellation."""

import pytest
from inspect_ai import eval as inspect_eval
from inspect_ai.model import ModelOutput, get_model

from wrench_compose.episode import ComposeEpisode
from wrench_compose.inspect_task import NO_OUTPUT_TURNS, create_compose_task

from fakes import FakeSandbox, FakeStack

COMMAND = "```sh\nwrenchctl ps\n```"


class EpisodeRecorder:
    """An ``episode_factory`` over the real ``ComposeEpisode`` with the fake
    stack/sandbox; keeps every episode it built."""

    def __init__(self, stack_factory=FakeStack):
        self.stack_factory = stack_factory
        self.episodes = []

    def __call__(self, kind, seed, **kw):
        episode = ComposeEpisode(kind, seed, stack_factory=self.stack_factory, sandbox_factory=FakeSandbox, **kw)
        self.episodes.append(episode)
        return episode


def run_task(tmp_path, outputs, factory, *, turns=5, kind="entity_destruction", **eval_kwargs):
    model = get_model(
        "mockllm/model",
        custom_outputs=[ModelOutput.from_content("mockllm/model", text) for text in outputs],
        memoize=False,
    )
    task = create_compose_task(
        kind, "3", turns=turns, turn_period_s=0, out_root=str(tmp_path / "episodes"), episode_factory=factory
    )
    (log,) = inspect_eval(
        task, model=model, display="none", log_dir=str(tmp_path / "logs"), fail_on_error=False, **eval_kwargs
    )
    assert log.status == "success", log.error
    return log.samples[0]


@pytest.fixture(autouse=True)
def _reset():
    FakeStack.instances.clear()
    yield
    FakeStack.instances.clear()


def test_happy_path_is_scored_without_error(tmp_path):
    factory = EpisodeRecorder()
    sample = run_task(tmp_path, [COMMAND] * 3, factory, turns=3)
    assert sample.error is None and sample.store["ComposeData:error"] == ""
    assert sample.scores["throughput_retained"].value == pytest.approx(0.5, abs=0.02)
    (episode,) = factory.episodes
    assert episode.steps_completed == 3 and FakeStack.instances[0].down_called == 1


def test_consecutive_empty_completions_abandon_the_episode(tmp_path):
    factory = EpisodeRecorder()
    sample = run_task(tmp_path, [""] * NO_OUTPUT_TURNS + [COMMAND] * 6, factory, turns=10)
    (episode,) = factory.episodes
    assert episode.turn == NO_OUTPUT_TURNS and episode.steps_completed == 0
    assert sample.store["ComposeData:error"] == f"model produced no output for {NO_OUTPUT_TURNS} consecutive turns"
    assert FakeStack.instances[0].down_called == 1  # cleaned up, not left running


def test_an_empty_completion_streak_resets_on_output(tmp_path):
    factory = EpisodeRecorder()
    outputs = [""] * (NO_OUTPUT_TURNS - 1) + [COMMAND] + [""] * (NO_OUTPUT_TURNS - 1) + [COMMAND]
    sample = run_task(tmp_path, outputs, factory, turns=len(outputs))
    (episode,) = factory.episodes
    assert sample.store["ComposeData:error"] == "" and episode.turn == len(outputs)
    assert episode.steps_completed == 2


class FlakyStack(FakeStack):
    """``up()`` fails on the first stack built in the process, then works."""

    failures = 1

    def up(self):
        if FlakyStack.failures > 0:
            FlakyStack.failures -= 1
            raise RuntimeError("compose up failed: network 10.232.0.0/24 in use")
        super().up()


def test_store_error_becomes_sample_error_and_is_retried(tmp_path):
    FlakyStack.failures = 1
    factory = EpisodeRecorder(stack_factory=FlakyStack)
    sample = run_task(tmp_path, [COMMAND] * 6, factory, turns=3, retry_on_error=1)
    assert len(factory.episodes) == 2  # first attempt raised, retry_on_error ran a second
    assert sample.error is None and sample.store["ComposeData:error"] == ""
    assert sample.scores["throughput_retained"].value == pytest.approx(0.5, abs=0.02)


def test_store_error_without_retry_is_sample_error(tmp_path):
    FlakyStack.failures = 1
    factory = EpisodeRecorder(stack_factory=FlakyStack)
    sample = run_task(tmp_path, [COMMAND] * 3, factory, turns=3)
    assert len(factory.episodes) == 1
    assert sample.error is not None and "compose up failed" in sample.error.message
    assert "compose up failed" in sample.store["ComposeData:error"]


class NotApplicableStack(FakeStack):
    def _fire(self):
        if self._tick < self.fire_tick:
            return None
        return {"tick": self.fire_tick, "event": "not_applicable", "kind": self.kind, "seed": self.seed, "detail": {}}


def test_no_fire_is_an_error_row_not_a_zero_fire_success(tmp_path):
    factory = EpisodeRecorder(stack_factory=NotApplicableStack)
    sample = run_task(tmp_path, [COMMAND] * 2, factory, turns=2)
    assert sample.error is not None and "not_applicable" in sample.error.message
    assert "not_applicable" in sample.store["ComposeData:error"]
    assert FakeStack.instances[0].down_called == 1


class SlowUpStack(FakeStack):
    """``up()`` takes long enough for the test to cancel the sample mid-start."""

    def up(self):
        import time

        time.sleep(0.3)
        super().up()


def test_cancelled_sample_still_tears_down_and_releases_the_slot(monkeypatch):
    import anyio
    from inspect_ai.model import ModelName
    from inspect_ai.solver import TaskState

    from wrench_compose import slots
    from wrench_compose.inspect_task import compose_solver

    monkeypatch.setenv(slots.SLOTS_ENV, "1")
    slots._POOL = None
    factory = EpisodeRecorder(stack_factory=SlowUpStack)
    solve = compose_solver(turns=2, turn_period_s=0, episode_factory=factory)

    async def main():
        pool = await slots.slot_pool()
        state = TaskState(
            model=ModelName("mockllm/model"),
            sample_id="belt_cut_seed1",
            epoch=0,
            input="",
            messages=[],
            metadata={"kind": "belt_cut", "seed": 1},
        )
        async with anyio.create_task_group() as tg:
            tg.start_soon(solve, state, None)
            await anyio.sleep(0.05)
            tg.cancel_scope.cancel()
        return pool

    pool = anyio.run(main)
    assert pool.available == 1, "cancelled sample leaked its slot"
    (episode,) = factory.episodes
    assert FakeStack.instances[0].down_called == 1
