"""The LLM path end to end (compose_live, ~3.5 min per episode, Docker):

(a) ``mockllm/model`` through the Inspect task: five turns of a reply that
    is not a valid command, then run-to-window-end. The store must carry no
    error and the scorers must return the no-op numbers for a worker kill
    (seed 3: TR ~0.62, not recovered, recall 0).
(b) ``compose-scripted/operator`` through the same task: a policy that
    repairs from the observation alone must score TR > 0.9 with the fire
    recovered and reported. This is the proof that the model path (reply
    -> sandbox shell -> probe) can recover, not just the fixture agents.
"""

import math
import shutil

import pytest
from inspect_ai import eval as inspect_eval

from wrench_compose.inspect_task import create_compose_task

pytestmark = pytest.mark.compose_live


@pytest.fixture(scope="module", autouse=True)
def _docker():
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")


def run_task(model: str, turns: int, tmp_path):
    task = create_compose_task("entity_destruction", 3, turns=turns, out_root=str(tmp_path / "episodes"), name="live")
    [log] = inspect_eval(task, model=model, log_dir=str(tmp_path / "logs"), display="none")
    assert log.status == "success", log.error
    [sample] = log.samples
    store = sample.store or {}
    assert not sample.error, sample.error
    assert store.get("ComposeData:error", "") == "", store.get("ComposeData:error")
    scores = {name: s for name, s in sample.scores.items()}
    return sample, store, scores


def test_mockllm_five_turns_scores_like_noop(tmp_path):
    sample, store, scores = run_task("mockllm/model", 5, tmp_path)
    # "Default output from mockllm/model" is a one-line reply, so it runs as a
    # command and fails (exit 127); the episode is scored like a no-op.
    assert store["ComposeData:steps_completed"] == 5
    assert all("exit code 127" in m.text for m in sample.messages[3::2])
    assert store["ComposeData:quota_met"] is True
    tr = scores["throughput_retained"]
    assert tr.metadata["num_fires"] == 1 and tr.metadata["scoreable"]
    assert 0.5 <= tr.value <= 0.75, tr.value  # one of two workers gone, nothing rebuilt
    assert scores["recovery"].value == 0.0
    assert scores["detection"].value == 0.0 and scores["detection"].metadata["num_reports"] == 0
    assert len(sample.messages) == 1 + 2 * 5


def test_scripted_operator_recovers(tmp_path):
    sample, store, scores = run_task("compose-scripted/operator", 30, tmp_path)
    # sample.messages is the trimmed context window; the ledger's agent_action
    # events (chaos records every mutating call) are the full action record.
    actions = [e["detail"] for e in store["ComposeData:ledger_events"] if e["event"] == "agent_action"]
    assert any(a["action"] == "scale" and a["service"] == "worker" and a["created"] for a in actions), actions
    tr = scores["throughput_retained"]
    assert tr.value > 0.9, (tr.value, actions)
    assert scores["recovery"].value == 1.0
    det = scores["detection"]
    assert det.value == 1.0 and det.metadata["precision_strict"] == 1.0
    assert not math.isnan(det.metadata["mean_latency_ticks"])
    assert 0 < store["ComposeData:steps_completed"] <= 30
