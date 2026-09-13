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
