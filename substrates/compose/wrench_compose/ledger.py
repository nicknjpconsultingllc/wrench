"""Ledger entry shape, identical to the fork's fle/disruptions/ledger.py so the
JSONL written here re-scores under the fork's scorers (and wrench_core later)."""

import json
from pathlib import Path

from pydantic import BaseModel, Field


class LedgerEntry(BaseModel, frozen=True, extra="forbid"):
    tick: int
    event: str
    kind: str | None = None
    seed: int | None = None
    affected: list[dict] = Field(default_factory=list)
    detail: dict = Field(default_factory=dict)


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(LedgerEntry.model_validate(e).model_dump_json(exclude_none=True) + "\n")


def read_jsonl(path: Path) -> list[LedgerEntry]:
    if not path.exists():
        return []
    with path.open() as f:
        return [LedgerEntry.model_validate(json.loads(line)) for line in f if line.strip()]


def check_sample(sample: dict) -> None:
    """Assert the scoring contract's sample shape: {"tick": int, "counts": {item: cumulative}}."""
    assert isinstance(sample.get("tick"), int), sample
    assert isinstance(sample.get("counts"), dict), sample
    for k, v in sample["counts"].items():
        assert isinstance(k, str) and isinstance(v, (int, float)), sample
