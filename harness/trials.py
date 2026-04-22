"""Trial + Turn dataclasses + JSON persistence (design doc §2.6)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TrialConfig:
    framework: str
    api: str
    stream: bool
    state: bool
    llm: str
    mcp: str
    routing: str
    model: str | None = None


@dataclass
class TurnPlan:
    turns: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Turn:
    turn_id: str
    turn_idx: int
    kind: str  # "user_msg" | "compact" | "force_state_ref" | "inject_ambient_cid"
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    framework_events: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class AuditEntry:
    trial_id: str
    turn_id: str | None
    phase: str
    cid: str | None
    backend: str | None
    raw: dict[str, Any]
    captured_at: str = ""


@dataclass
class Verdict:
    verdict: str  # "pass" | "fail" | "na" | "error"
    reason: str


@dataclass
class Trial:
    trial_id: str
    config: TrialConfig
    turn_plan: TurnPlan
    status: str = "idle"  # "idle" | "running" | "pass" | "fail" | "error" | "aborted" | "paused"
    paired_trial_id: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    turns: list[Turn] = field(default_factory=list)
    audit_entries: list[AuditEntry] = field(default_factory=list)
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    error_reason: str | None = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


def _to_jsonable(obj: Any) -> Any:
    """Convert dataclass / nested structure to JSON-serializable form."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


class TrialStore:
    def __init__(self, base_dir: Path | str):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, trial_id: str) -> Path:
        return self.base / f"{trial_id}.json"

    def save(self, trial: Trial) -> None:
        p = self._path(trial.trial_id)
        tmp = p.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(_to_jsonable(trial), f, indent=2)
        tmp.replace(p)

    def load(self, trial_id: str) -> Trial:
        p = self._path(trial_id)
        if not p.exists():
            raise FileNotFoundError(f"Trial {trial_id} not found at {p}")
        with p.open() as f:
            data = json.load(f)
        cfg = TrialConfig(**data["config"])
        plan = TurnPlan(turns=data.get("turn_plan", {}).get("turns", []))
        turns = [Turn(**t) for t in data.get("turns", [])]
        audits = [AuditEntry(**a) for a in data.get("audit_entries", [])]
        verdicts = {k: Verdict(**v) for k, v in data.get("verdicts", {}).items()}
        trial = Trial(
            trial_id=data["trial_id"],
            config=cfg,
            turn_plan=plan,
            status=data.get("status", "idle"),
            paired_trial_id=data.get("paired_trial_id"),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            turns=turns,
            audit_entries=audits,
            verdicts=verdicts,
            error_reason=data.get("error_reason"),
        )
        return trial

    def append_turn(self, trial_id: str, turn: Turn) -> None:
        trial = self.load(trial_id)
        trial.turns.append(turn)
        self.save(trial)

    def append_audit(self, trial_id: str, entry: AuditEntry) -> None:
        trial = self.load(trial_id)
        trial.audit_entries.append(entry)
        self.save(trial)

    def list_all(self) -> list[Trial]:
        out = []
        for p in self.base.glob("*.json"):
            try:
                out.append(self.load(p.stem))
            except Exception:
                continue
        out.sort(key=lambda t: t.created_at, reverse=True)
        return out
