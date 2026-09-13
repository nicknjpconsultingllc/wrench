"""``ComposeEpisode``'s turn protocol without Docker: a fake stack and a
fake sandbox stand in for the probe and ``docker exec``. Mirrors the
fork's ``tests/wrench/test_episode.py`` for ``WrenchEpisode``."""

import math

import pytest

from wrench_compose.episode import (
    INITIAL_FEEDBACK,
    NO_COMMAND_FEEDBACK,
    ComposeEpisode,
    EpisodeError,
    cap_bytes,
    format_feedback,
    parse_command,
)
from wrench_compose.report import ITEM

from fakes import METRICS_TEXT, PS_TEXT, FakeSandbox, FakeStack


@pytest.fixture(autouse=True)
def _reset():
    FakeStack.instances.clear()
    yield
    FakeStack.instances.clear()


def make_episode(tmp_path, turns=3, **kw):
    kw.setdefault("turn_period_s", 0)
    kw.setdefault("stack_factory", FakeStack)
    return ComposeEpisode(
        "entity_destruction",
        3,
        turns=turns,
        out_root=tmp_path,
        name="ep",
        sandbox_factory=FakeSandbox,
        **kw,
    )


class TestParseCommand:
    def test_fenced_block_first_line(self):
        assert parse_command("Let me look.\n```sh\nwrenchctl ps\n```") == "wrenchctl ps"
        assert parse_command("```bash\n# check\n$ wrenchctl metrics\nwrenchctl ps\n```") == "wrenchctl metrics"
        assert parse_command("```\nwrenchctl scale worker 2\n```\n```\nwrenchctl ps\n```") == "wrenchctl scale worker 2"

    def test_bare_wrenchctl_line(self):
        assert parse_command("I will run:\n$ wrenchctl logs worker --tail 5\nthen decide") == (
            "wrenchctl logs worker --tail 5"
        )

    def test_single_line_taken_whole(self):
        assert parse_command("ls /tmp") == "ls /tmp"

    @pytest.mark.parametrize("text", [None, "", "   ", "I need to think.\nNothing to do yet.", "```sh\n```"])
    def test_no_command(self, text):
        assert parse_command(text) is None


class TestFormatting:
    def test_cap_bytes(self):
        assert cap_bytes("abc", 10) == "abc"
        capped = cap_bytes("x" * 100, 10)
        assert capped.startswith("x" * 10) and "truncated 90 bytes" in capped

    def test_feedback_shape(self):
        text = format_feedback(2, "wrenchctl ps", 0, "out\n", "", 12)
        assert text.startswith("## Turn 3 result")
        assert "$ wrenchctl ps" in text and "exit code 0 (12 ms)" in text
        assert "stdout:\n```\nout\n```" in text and "stderr" not in text
        text = format_feedback(0, "false", 1, "", "boom", 1)
        assert "stdout:\n```\n(empty)\n```" in text and "stderr:\n```\nboom\n```" in text


class TestTurnProtocol:
    def test_requires_start(self, tmp_path):
        ep = make_episode(tmp_path)
        with pytest.raises(RuntimeError):
            ep.observe()
        with pytest.raises(RuntimeError):
            ep.step("wrenchctl ps")

    def test_observation_is_feedback_plus_agent_view(self, tmp_path):
        ep = make_episode(tmp_path, turns=5).start()
        stack = FakeStack.instances[0]
        assert stack.calls[:2] == ["up", ("arm", "entity_destruction", 3)]
        obs = ep.observe()
        assert obs.startswith(INITIAL_FEEDBACK)
        assert "## Turn 1/5" in obs
        assert f"`wrenchctl ps`:\n```\n{PS_TEXT}\n```" in obs
        assert f"`wrenchctl metrics`:\n```\n{METRICS_TEXT}\n```" in obs
        assert obs.rstrip().endswith("for your next action.")

    def test_step_runs_the_command_and_feeds_back(self, tmp_path):
        ep = make_episode(tmp_path).start()
        feedback = ep.step("wrenchctl scale worker 2")
        assert ep.sandbox.commands == ["wrenchctl scale worker 2"]
        assert "## Turn 1 result" in feedback and "ran: wrenchctl scale worker 2" in feedback
        assert ep.turn == 1 and ep.steps_completed == 1
        assert ep.observe().startswith(feedback)
        assert ep.actions[0]["command"] == "wrenchctl scale worker 2" and ep.actions[0]["exit_code"] == 0

    def test_no_command_turn_is_consumed(self, tmp_path):
        ep = make_episode(tmp_path).start()
        assert ep.step(None) == NO_COMMAND_FEEDBACK
        assert ep.turn == 1 and ep.steps_completed == 0 and ep.sandbox.commands == []
        assert NO_COMMAND_FEEDBACK in ep.observe()

    def test_skip_and_fail_consume_turns(self, tmp_path):
        ep = make_episode(tmp_path).start()
        ep.skip_step("model failed")
        ep.fail_step("driver died")
        assert ep.turn == 2 and ep.steps_completed == 0
        assert ep.feedback == "driver died"
        assert ep.actions[0]["skipped"] == "model failed" and "failed" not in ep.actions[0]
        assert ep.actions[1]["failed"] == "driver died"

    def test_is_done_on_budget_and_step_refuses(self, tmp_path):
        ep = make_episode(tmp_path, turns=2).start()
        ep.step("wrenchctl ps")
        assert not ep.is_done
        ep.step("wrenchctl ps")
        assert ep.is_done
        with pytest.raises(RuntimeError):
            ep.step("wrenchctl ps")
        with pytest.raises(RuntimeError):
            ep.skip_step("x")

    def test_is_done_when_the_window_closes(self, tmp_path):
        ep = make_episode(tmp_path, turns=100).start()
        stack = FakeStack.instances[0]
        stack.tick_step = 50000  # each drain advances 50 s
        ep.step("wrenchctl ps")  # tick 50 s: nothing yet
        assert ep.fired is None
        ep.step("wrenchctl ps")  # tick 100 s: fired at 60 s, window open
        assert ep.fired is not None and not ep.is_done
        ep.step("wrenchctl ps")  # 150 s
        ep.step("wrenchctl ps")  # 200 s > 60 + 120 + 1.5
        assert ep.window_closed and ep.is_done
        ep2 = make_episode(tmp_path, turns=100, stop_after_window=False).start()
        FakeStack.instances[-1].tick_step = 50000
        for _ in range(4):
            ep2.step("wrenchctl ps")
        assert ep2.window_closed and not ep2.is_done

    def test_drain_tracks_fire_quota_and_shaped_reward(self, tmp_path):
        ep = make_episode(tmp_path, turns=10).start()
        stack = FakeStack.instances[0]
        stack.tick_step = 30000
        ep.step("wrenchctl ps")  # tick 30 s: armed only
        assert ep.quota_met and ep.fired is None and ep.shaped_rewards == []
        ep.step("wrenchctl ps")  # tick 60 s: fire
        ep.step("wrenchctl ps")  # tick 90 s
        assert ep.fired["tick"] == 60000 and ep.fires[0]["kind"] == "entity_destruction"
        assert ep.shaped_rewards and ep.shaped_rewards[-1]["fire_tick"] == 60000
        assert all(e["tick"] >= 60000 for e in ep.shaped_rewards)


class TestFinalize:
    def test_runs_to_window_end_and_scores(self, tmp_path):
        ep = make_episode(tmp_path, turns=2).start()
        stack = FakeStack.instances[0]
        ep.step("wrenchctl ps")
        ep.step("wrenchctl ps")
        assert ep.fired is None  # 2 s in; the fire is at 60 s
        result = ep.finalize()
        assert "wait_resolved" in stack.calls and ("wait_tick", 60000 + 120000 + 1500) in stack.calls
        assert result["end_tick"] == 180000  # clipped to fire + window
        assert result["num_fires"] == 1 and result["fires"][0]["tick"] == 60000
        assert result["quota_met"] and result["steps_completed"] == 2 and result["steps_taken"] == 2
        # 8 jobs/s before, 4 after: TR = 0.5 against the frozen 1-minute baseline.
        tr = result["scores"]["throughput_retained"]
        assert tr["value"] == pytest.approx(0.5, abs=0.02)
        assert tr["metadata"]["scoreable"] and tr["metadata"]["num_fires"] == 1
        assert tr["metadata"]["pooled_denominator"] == pytest.approx(8 * 120, rel=0.02)
        assert result["metrics"]["throughput_retained"] == tr["value"]
        assert result["scores"]["recovery"]["value"] == 0.0
        assert result["scores"]["detection"]["value"] == 0.0  # no report
        assert result["scores"]["time_to_recovery"]["fires"][0]["parts"]["recovered"] is False
        assert result["samples"][-1]["counts"][ITEM] > 0 and result["ledger_events"][1]["event"] == "fired"
        assert ep.finalize() is result  # idempotent
        assert (ep.run_dir / "episode.json").exists()

    def test_detection_through_a_report(self, tmp_path):
        ep = make_episode(tmp_path, turns=1).start()
        stack = FakeStack.instances[0]
        stack.tick_step = 70000
        ep.step("wrenchctl report_fault worker 'gone'")
        stack.report(70500, "worker")
        result = ep.finalize()
        det = result["scores"]["detection"]
        assert det["value"] == 1.0 and det["metadata"]["precision_strict"] == 1.0
        assert det["metadata"]["latencies"] == [10500]

    def test_no_fire_is_an_episode_error(self, tmp_path):
        ep = make_episode(tmp_path, turns=1).start()
        stack = FakeStack.instances[0]
        stack.wait_resolved = lambda timeout_s: (_ for _ in ()).throw(EpisodeError("no fire"))
        with pytest.raises(EpisodeError):
            ep.finalize()

    def test_finalize_without_start_is_unscoreable(self, tmp_path):
        result = make_episode(tmp_path).finalize()
        assert result["num_fires"] == 0 and math.isnan(result["metrics"]["throughput_retained"] or math.nan)
        assert result["scores"]["throughput_retained"]["metadata"]["scoreable"] is False

    def test_cleanup_is_idempotent_and_tears_down(self, tmp_path):
        ep = make_episode(tmp_path).start()
        ep.cleanup()
        ep.cleanup()
        assert FakeStack.instances[0].down_called == 1
        assert (ep.run_dir / "episode.json").exists()

    def test_system_prompt_carries_the_budget(self, tmp_path):
        text = make_episode(tmp_path, turns=7).system_prompt()
        assert "You have 7 turns" in text and "quota of 400" in text and "2 replicas" in text


class TestPacing:
    """Every turn holds ``turn_period_s``, the no-command and skipped turns
    included, so a model that answers with nothing cannot burn its budget
    before the fault fires."""

    @pytest.fixture
    def clock(self, monkeypatch):
        from types import SimpleNamespace

        from wrench_compose import episode as episode_module

        state = SimpleNamespace(now=1000.0, slept=[])

        def sleep(s):
            state.slept.append(round(s, 3))
            state.now += s

        monkeypatch.setattr(
            episode_module, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep, time=lambda: 0.0)
        )
        return state

    def test_no_command_turn_is_paced(self, tmp_path, clock):
        ep = make_episode(tmp_path, turns=5, turn_period_s=6.0).start()
        ep.step("wrenchctl ps")
        ep.step(None)
        assert clock.slept == [6.0]
        ep.step("wrenchctl ps")
        assert clock.slept == [6.0, 6.0]

    def test_skipped_turn_is_paced(self, tmp_path, clock):
        ep = make_episode(tmp_path, turns=5, turn_period_s=6.0).start()
        ep.step("wrenchctl ps")
        ep.skip_step("no output")
        ep.skip_step("no output")
        assert clock.slept == [6.0, 6.0]


class NotApplicableStack(FakeStack):
    """The spec resolves to ``not_applicable`` instead of firing."""

    def _fire(self):
        if self._tick < self.fire_tick:
            return None
        return {
            "tick": self.fire_tick,
            "event": "not_applicable",
            "kind": self.kind,
            "seed": self.seed,
            "affected": [],
            "detail": {"error": "no holder carried any flow in the window"},
        }


class TestNoFire:
    def test_not_applicable_is_an_episode_error_naming_the_event(self, tmp_path):
        ep = make_episode(tmp_path, turns=1, stack_factory=NotApplicableStack).start()
        ep.step("wrenchctl ps")
        with pytest.raises(EpisodeError, match="not_applicable") as info:
            ep.finalize()
        assert "no holder carried any flow" in str(info.value)
        assert ep.fires == []

    def test_failed_terminal_event_in_the_ledger(self, tmp_path):
        ep = make_episode(tmp_path, turns=1).start()
        stack = FakeStack.instances[0]
        failed = {
            "tick": 61000,
            "event": "failed",
            "kind": "belt_cut",
            "seed": 3,
            "detail": {"error": "toxiproxy refused"},
        }
        stack.wait_resolved = lambda timeout_s: failed
        stack.ledger = lambda: [{"tick": 1000, "event": "armed", "kind": "belt_cut", "seed": 3}, failed]
        with pytest.raises(EpisodeError, match="'failed'"):
            ep.finalize()
