"""FastAPI routes for the aiplay harness."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from audit_tail import AuditTail
from efficacy import compute_verdicts
from providers import get_providers
from runner import run_trial
from templates import default_turn_plan, get_default_turn_count, set_default_turn_count
from trials import AuditEntry, Trial, TrialConfig, TrialStore, TurnPlan
from validator import validate as validate_row

router = APIRouter()


# ── State (module-global, single-process harness) ──

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STORE = TrialStore(DATA_DIR / "trials")
MATRIX_PATH = DATA_DIR / "matrix.json"
AUDIT_TAIL: AuditTail | None = None
AUDIT_BUFFER_PER_TRIAL: dict[str, list[AuditEntry]] = defaultdict(list)

# Plan B T14 — cooperative abort registry. Populated when a trial starts,
# set by POST /trials/{id}/abort, checked by runner.run_trial between turns,
# cleared once the background task completes.
ABORT_EVENTS: dict[str, asyncio.Event] = {}

# I2 — cidgar's uuid4_12 generator produces markers of the form
# ``ib_<12 lowercase hex>``. _classify_diff uses this to distinguish
# governance channel markers from arbitrary content diffs. The full
# 12-hex suffix is statistically distinctive enough to avoid collisions
# with English/code tokens (e.g. library_id, fib_number).
_CID_MARKER_RE = re.compile(r'ib_[a-f0-9]{12}')


# ── Matrix persistence (distinct from trial JSON) ──

def _load_matrix() -> list[dict]:
    if not MATRIX_PATH.exists():
        # First-boot seed from harness/defaults.yaml
        seeded = _seed_matrix_from_defaults()
        if seeded:
            _save_matrix(seeded)
            return seeded
        return []
    with MATRIX_PATH.open() as f:
        return json.load(f).get("rows", [])


def _seed_matrix_from_defaults() -> list[dict]:
    """Load matrix_seed_rows from harness/defaults.yaml.

    Seeds only rows for adapters the harness can actually drive:
      * Plan A  — langchain, direct-mcp (NONE routing)
      * Plan B  — extends to autogen (T5), llamaindex (T6) for the
                  force_state_ref/verdict-e seed row.
    """
    try:
        import yaml
        defaults_path = Path(__file__).with_name("defaults.yaml")
        if not defaults_path.exists():
            return []
        with defaults_path.open() as f:
            data = yaml.safe_load(f) or {}
        rows_spec = data.get("matrix_seed_rows", [])
        allowed = {"langchain", "autogen", "llamaindex", "NONE"}
        seeded = []
        for idx, spec in enumerate(rows_spec):
            if spec.get("framework") not in allowed:
                continue
            seeded.append({
                "row_id": f"row-seed-{idx:02d}",
                **spec,
            })
        return seeded
    except Exception:
        return []


def _save_matrix(rows: list[dict]) -> None:
    MATRIX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MATRIX_PATH.open("w") as f:
        json.dump({"rows": rows}, f, indent=2)


# ── Models ──

class RowConfig(BaseModel):
    framework: str
    api: str
    stream: bool = False
    state: bool = False
    # E23 — list-form `llm` lets multi-LLM-aware adapters (initially
    # the combo adapter from E24) round-robin per turn across multiple
    # providers in one trial. Single-string form remains the default
    # and is what every existing adapter consumes today.
    llm: str | list[str]
    # E19 — list-form `mcp` lets multi-MCP-aware adapters merge tool
    # sets from several MCP servers in one trial. No adapter is opted
    # in yet (MULTI_MCP_FRAMEWORKS is empty); the schema lands now so
    # adapter wiring can opt in incrementally without a second schema
    # migration.
    mcp: str | list[str]
    routing: str = "via_agw"
    # E9 — optional per-row model override, populated by the matrix UI's
    # Model dropdown. Empty string / None means "use the adapter's
    # DEFAULT_<PROVIDER>_MODEL env fallback". Curated values are surfaced
    # by GET /providers/{id}/models; "__custom__" is a UI-only sentinel
    # that's never persisted (the frontend prompts for free text).
    # E23 — list-form pairs 1:1 with list-form `llm` (the combo adapter
    # picks model[i] for the i-th LLM); validator enforces length match.
    model: str | list[str] | None = None
    # Plan B T10 — opt-in flag that switches the row's default turn plan to
    # `with_mcp_with_compact` so verdict (d) has a compact turn to bracket.
    with_compact: bool = False
    # Plan B T11 — opt-in flag that switches the row's default turn plan to
    # `with_responses_state_force_ref` so verdict (e) has a force_state_ref
    # turn to bracket. Only meaningful for api in responses/responses+conv
    # with state=True + a supporting framework (autogen / llamaindex).
    with_force_state_ref: bool = False
    # E21 — opt-in flag that switches the row's default turn plan to
    # `with_reset` so verdict (c) exercises bracket-aware multi-segment
    # continuity (reset_context boundary + cross-segment leak detection).
    with_reset: bool = False
    # E20 verification — opt-in flag that switches the row's default turn
    # plan to `with_e20_verification` so verdict (i) tools_list_correlation
    # has 2 distinct snapshots to measure against in one trial. Requires
    # mcp=mutable (only MCP exposing /_admin endpoints) — enforced by
    # validator.
    with_e20_verification: bool = False
    # Plan B T12 — when present, takes precedence over default_turn_plan(row).
    # Shape: {"turns": [...]} matching TurnPlan. Edited via the row drawer's
    # CodeMirror JSON editor; persisted via PATCH /matrix/row/{id}; cleared
    # via DELETE /matrix/row/{id}/turn_plan_override.
    turn_plan_override: dict | None = None
    # Plan B T13 — optional baseline-pairing metadata set by
    # POST /matrix/row/{id}/clone-baseline. `baseline_of` points at the
    # source row_id; `note` is a human-readable label rendered in the UI.
    baseline_of: str | None = None
    note: str | None = None


# ── Routes ──

@router.get("/health")
def health():
    return {"status": "ok", "version": "plan-a-mvp"}


@router.get("/info")
def info():
    """Harness info + framework capability mirror.

    I-NEW-1: `frameworks` is the single source of truth consumed by the
    trial-detail page's NOTE-tab rules. Built from
    `harness/validator.py::ADAPTER_CAPABILITIES` so the JS UI never
    duplicates capability data and silently drifts when ADAPTER_CAPABILITIES
    is edited. Only `supported_apis` is exposed today — that's the only
    field ADAPTER_CAPABILITIES carries; do not add new metadata here
    without first extending the validator dict.
    """
    from validator import ADAPTER_CAPABILITIES
    return {
        "harness_version": "plan-a-mvp",
        "adapters": [
            {"framework": "langchain", "url": "http://adapter-langchain:5001"},
        ],
        "frameworks": {
            name: {"supported_apis": sorted(caps)}
            for name, caps in ADAPTER_CAPABILITIES.items()
        },
    }


@router.get("/providers")
def providers_endpoint():
    return {"providers": get_providers()}


@router.get("/providers/{provider_id}/models")
def provider_models_endpoint(provider_id: str):
    """Return curated models for a provider. Backed by harness/models.py.

    Sources: env override (``<PROVIDER>_MODELS=a,b,c``) wins; otherwise
    the curated dict in models.py. Unknown providers return an empty
    list — the UI falls back to a free-text input.
    """
    from models import get_models, to_jsonable
    return {
        "provider": provider_id,
        "models": to_jsonable(get_models(provider_id)),
    }


@router.post("/validate")
def validate_endpoint(payload: dict = Body(...)):
    row = payload.get("row_config", {})
    id_to_env = {"chatgpt": "openai", "claude": "anthropic", "gemini": "google"}
    available = {}
    for p in get_providers():
        env_key = id_to_env.get(p["id"])
        if env_key:
            available[env_key] = p["available"]
    return validate_row(row, available_keys=available)


@router.get("/settings")
def settings_get():
    """Return persisted user settings (default turn count, etc.).
    Backed by $DATA_DIR/settings.json."""
    return {"default_turn_count": get_default_turn_count()}


@router.put("/settings")
def settings_put(payload: dict = Body(...)):
    """Update settings. Currently only `default_turn_count` is supported.
    Returns the new persisted state (with clamping applied)."""
    if "default_turn_count" in payload:
        set_default_turn_count(int(payload["default_turn_count"]))
    return {"default_turn_count": get_default_turn_count()}


@router.post("/templates/preview")
def templates_preview(row: RowConfig):
    """Return the turn plan that would be used for this row config.

    Same logic as templates.default_turn_plan but exposed as an HTTP endpoint
    so the UI can preview without creating a trial. Plan B will add an edit
    path (PATCH /templates/override) to override the default per row."""
    row_dict = row.model_dump()
    plan = default_turn_plan(row_dict)
    return {"row_config": row_dict, "turn_plan": plan}


@router.get("/matrix")
def matrix_list():
    return {"rows": _load_matrix()}


@router.post("/matrix/row")
def matrix_create(row: RowConfig):
    rows = _load_matrix()
    rid = f"row-{uuid.uuid4().hex[:8]}"
    rows.append({"row_id": rid, **row.model_dump()})
    _save_matrix(rows)
    return {"row_id": rid}


@router.get("/matrix/row/{row_id}")
def matrix_get(row_id: str):
    rows = _load_matrix()
    row = next((r for r in rows if r["row_id"] == row_id), None)
    if not row:
        raise HTTPException(404, "row not found")
    return row


@router.patch("/matrix/row/{row_id}")
def matrix_update(row_id: str, updates: dict = Body(...)):
    rows = _load_matrix()
    for r in rows:
        if r["row_id"] == row_id:
            r.update(updates)
            _save_matrix(rows)
            return {"ok": True}
    raise HTTPException(404, "row not found")


@router.delete("/matrix/row/{row_id}")
def matrix_delete(row_id: str):
    rows = _load_matrix()
    rows = [r for r in rows if r["row_id"] != row_id]
    _save_matrix(rows)
    return {"ok": True}


@router.post("/matrix/row/{row_id}/clone-baseline")
def matrix_clone_baseline(row_id: str):
    """Clone a row as a 'baseline' — same config but routing=direct.

    Used for A/B comparison: the governed row goes via AGW (cidgar hooks
    fire), the baseline row hits the LLM/MCP directly (no governance).
    Verdict comparison should show pass on the governed row and na/empty
    on the baseline row — confirms the governed verdicts aren't false
    positives from non-cidgar sources.
    """
    rows = _load_matrix()
    src = next((r for r in rows if r["row_id"] == row_id), None)
    if not src:
        raise HTTPException(404, "row not found")
    if src.get("routing") == "direct":
        raise HTTPException(
            400, "row is already a direct/baseline row — nothing to clone"
        )

    new_id = f"row-{uuid.uuid4().hex[:8]}"
    clone = {
        "row_id": new_id,
        "framework": src["framework"],
        "api": src["api"],
        "stream": src.get("stream", False),
        "state": src.get("state", False),
        "llm": src["llm"],
        "mcp": src["mcp"],
        "routing": "direct",
        "with_compact": src.get("with_compact", False),
        "with_force_state_ref": src.get("with_force_state_ref", False),
        "with_reset": src.get("with_reset", False),
        # Carry the same turn_plan_override so the comparison runs the
        # same exact prompts on both rows.
        "turn_plan_override": src.get("turn_plan_override"),
        # Pairing metadata so the UI can render an A/B link.
        "baseline_of": row_id,
        "note": f"Baseline (direct, no AGW) of {row_id}",
    }
    # Strip None turn_plan_override to keep JSON tidy when not set
    if clone["turn_plan_override"] is None:
        clone.pop("turn_plan_override")
    rows.append(clone)
    _save_matrix(rows)
    return {"row_id": new_id, "baseline_of": row_id}


@router.delete("/matrix/row/{row_id}/turn_plan_override")
def matrix_clear_override(row_id: str):
    """T12 — remove a row's saved turn_plan_override so the runner falls back
    to default_turn_plan(row). Cleaner than PATCH-ing {turn_plan_override:null}."""
    rows = _load_matrix()
    for r in rows:
        if r["row_id"] == row_id:
            r.pop("turn_plan_override", None)
            _save_matrix(rows)
            return {"ok": True}
    raise HTTPException(404, "row not found")


@router.post("/templates/validate")
def templates_validate(payload: dict = Body(...)):
    """T12 — validate a turn_plan dict. Returns {ok, errors}.

    Checks the shape the runner expects (turns list of dicts with kind;
    user_msg requires content; force_state_ref requires lookback;
    mcp_admin requires op). turn_id is optional — the runner auto-
    generates one per turn at dispatch time (`turn-NNN-xxxxxxxx`), so
    user-authored plans don't need to carry it.
    """
    errors: list[str] = []
    plan = payload.get("turn_plan", {})
    turns = plan.get("turns")
    if not isinstance(turns, list) or not turns:
        errors.append("turns must be a non-empty list")
    else:
        for i, t in enumerate(turns):
            if not isinstance(t, dict):
                errors.append(f"turn {i}: must be object")
                continue
            if "kind" not in t:
                errors.append(f"turn {i}: missing 'kind'")
            elif t["kind"] not in (
                "user_msg", "compact", "force_state_ref", "mcp_admin",
                "reset_context", "refresh_tools",
            ):
                errors.append(f"turn {i}: invalid kind '{t['kind']}'")
            # user_msg requires content (NOT text — runner reads turn_spec["content"])
            if t.get("kind") == "user_msg" and not t.get("content"):
                errors.append(f"turn {i}: user_msg requires non-empty 'content'")
            if t.get("kind") == "force_state_ref" and "lookback" not in t:
                errors.append(f"turn {i}: force_state_ref requires 'lookback' (int)")
            # E22 — mcp_admin requires `op`; `mcp` and `payload` are
            # optional (mcp falls back to trial.config.mcp; payload to {}).
            if t.get("kind") == "mcp_admin" and not t.get("op"):
                errors.append(f"turn {i}: mcp_admin requires 'op' (e.g., 'set_tools', 'reset')")
            # E21 — reset_context and refresh_tools have no required fields
            # beyond `kind`; both are zero-arg dispatch markers.
    return {"ok": not errors, "errors": errors}


@router.delete("/matrix")
def matrix_clear():
    """Delete all matrix rows. Persists an empty list so the seed-on-first-boot
    logic doesn't reintroduce them on the next /matrix call."""
    prior = _load_matrix()
    _save_matrix([])
    return {"ok": True, "deleted_count": len(prior)}


@router.post("/trials/{row_id}/run")
async def trial_run(row_id: str):
    rows = _load_matrix()
    row = next((r for r in rows if r["row_id"] == row_id), None)
    if not row:
        raise HTTPException(404, "row not found")

    # Reject unrunnable rows server-side via the full validator, not just
    # the UI. Covers: LLM=NONE+MCP=NONE, api↔llm mismatch, adapter-not-
    # implemented (Plan A only has langchain/chat), etc.
    id_to_env = {"chatgpt": "openai", "claude": "anthropic", "gemini": "google"}
    available_keys = {}
    for p in get_providers():
        env_key = id_to_env.get(p["id"])
        if env_key:
            available_keys[env_key] = p["available"]
    v = validate_row(row, available_keys=available_keys)
    if not v.get("runnable", True):
        reasons = " | ".join(v.get("warnings", [])) or "row is not runnable"
        raise HTTPException(400, f"row not runnable: {reasons}")

    trial_id = str(uuid.uuid4())
    # When llm=NONE, the direct-mcp adapter drives the trial regardless of
    # the framework the user picked in the matrix row (that dropdown is
    # irrelevant for llm=NONE — the runner picks the adapter for us).
    framework = row["framework"]
    if row.get("llm", "NONE") == "NONE":
        framework = "direct-mcp"
    cfg = TrialConfig(
        framework=framework, api=row["api"],
        stream=row.get("stream", False), state=row.get("state", False),
        llm=row["llm"], mcp=row["mcp"], routing=row.get("routing", "via_agw"),
        # E9 — empty string is treated as "no override" by the adapter
        # (which then falls back to DEFAULT_<PROVIDER>_MODEL). None and ""
        # are equivalent at the wire level.
        model=row.get("model") or None,
    )
    # T12 — prefer per-row override when the user has saved one via the drawer.
    plan_dict = row.get("turn_plan_override") or default_turn_plan(row)
    plan = TurnPlan(turns=plan_dict["turns"])
    trial = Trial(trial_id=trial_id, config=cfg, turn_plan=plan, status="running")
    STORE.save(trial)

    # Record trial start time for time-window audit correlation.
    # Header-based demux still registered as a fallback (fires only if
    # cidgar ever starts logging X-Harness-Trial-ID; currently does not).
    import time
    trial_started_mono = time.time()
    if AUDIT_TAIL is not None:
        def cb(entry: dict):
            AUDIT_BUFFER_PER_TRIAL[trial_id].append(AuditEntry(
                trial_id=trial_id, turn_id=entry.get("turn_id"),
                phase=entry.get("phase"), cid=entry.get("cid"),
                backend=entry.get("backend"), raw=entry.get("raw", {}),
                captured_at=entry.get("timestamp", ""),
                body=entry.get("body"),  # E26
            ))
        AUDIT_TAIL.subscribe(trial_id, cb)

    # Adapter client (simplified HTTP wrapper)
    from adapters_registry import AdapterClient
    adapter = AdapterClient(framework=cfg.framework)

    # T14 — register cooperative-abort event BEFORE spawning so a racing
    # POST /abort can find it even if it arrives before create_task yields.
    ABORT_EVENTS[trial_id] = asyncio.Event()

    # Run in background (pass row_id so we can update the matrix on completion)
    asyncio.create_task(_run_trial_bg(trial_id, adapter, row_id=row_id, started_mono=trial_started_mono))

    return {"trial_id": trial_id, "status": "running"}


async def _run_trial_bg(trial_id: str, adapter, row_id: str | None = None, started_mono: float | None = None):
    def audit_provider():
        """Combine (a) header-demuxed entries (future) + (b) time-window entries.
        Plan A relies on (b) since cidgar governance log omits request headers.
        """
        demuxed = list(AUDIT_BUFFER_PER_TRIAL.get(trial_id, []))
        if AUDIT_TAIL is not None and started_mono is not None:
            window = AUDIT_TAIL.entries_since(started_mono)
            # Dedupe against demuxed (header-matched) entries by phase+cid+timestamp
            seen = {(e.phase, e.cid, e.captured_at) for e in demuxed}
            for e in window:
                key = (e.get("phase"), e.get("cid"), e.get("timestamp"))
                if key in seen:
                    continue
                demuxed.append(AuditEntry(
                    trial_id=trial_id, turn_id=e.get("turn_id"),
                    phase=e.get("phase"), cid=e.get("cid"),
                    backend=e.get("backend"), raw=e.get("raw", {}),
                    captured_at=e.get("timestamp", ""),
                    body=e.get("body"),  # E26
                ))
        return demuxed

    try:
        await run_trial(
            trial_id=trial_id,
            store=STORE,
            adapter_client=adapter,
            audit_entries_provider=audit_provider,
            # T14 — may be None if the event was already cleaned up by a sibling
            # path, but in practice it's registered in trial_run before this bg
            # task is scheduled.
            abort_event=ABORT_EVENTS.get(trial_id),
        )
    finally:
        # F1 — cleanup is idempotent and MUST run even if run_trial raises so
        # ABORT_EVENTS, AUDIT_BUFFER_PER_TRIAL, and the audit-tail subscription
        # don't leak across trials.
        if AUDIT_TAIL is not None:
            AUDIT_TAIL.unsubscribe(trial_id)
        ABORT_EVENTS.pop(trial_id, None)
        AUDIT_BUFFER_PER_TRIAL.pop(trial_id, None)

    # Reconcile matrix row with final trial state so the UI polling path
    # can recover the row pill even if the user closed the trial tab. Outside
    # finally so it reads the persisted final state (run_trial sets
    # trial.status in its own error/abort branches; the reconciler just
    # reflects it).
    if row_id:
        try:
            final = STORE.load(trial_id)
            rows = _load_matrix()
            for r in rows:
                if r["row_id"] == row_id:
                    r["status"] = final.status
                    r["verdicts"] = final.verdicts or {}
                    r["last_trial_id"] = trial_id
                    _save_matrix(rows)
                    break
        except Exception as e:
            # M4 partial — log rather than silently swallow so reconcile
            # failures (stale matrix) are at least visible in logs.
            import logging
            logging.getLogger(__name__).warning(
                "matrix reconcile failed for trial %s row %s: %s",
                trial_id, row_id, e,
            )


@router.get("/trials/{trial_id}")
def trial_get(trial_id: str):
    try:
        trial = STORE.load(trial_id)
        from trials import _to_jsonable
        return _to_jsonable(trial)
    except FileNotFoundError:
        raise HTTPException(404, "trial not found")


# ── E4 — pair diff endpoints + helpers ──

def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _percentile(xs: list[float], pct: int) -> float:
    """Linear-interpolation percentile; matches numpy's default for small lists."""
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * pct / 100
    f = int(k)
    if f + 1 >= len(s):
        return s[f]
    return s[f] + (s[f + 1] - s[f]) * (k - f)


def _compute_latency_deltas_ms(g_turns: list, b_turns: list) -> list[float]:
    """Per-turn (governed - baseline) latency in ms. Pairs by turn_idx.

    Skips turns missing started_at/finished_at on either side, and skips
    turns whose timestamps are malformed.
    """
    from datetime import datetime
    deltas: list[float] = []
    for g_turn in g_turns:
        idx = g_turn.get("turn_idx")
        if idx is None:
            continue
        b_turn = next((t for t in b_turns if t.get("turn_idx") == idx), None)
        if not b_turn:
            continue
        try:
            g_dur = (datetime.fromisoformat(g_turn["finished_at"]) -
                     datetime.fromisoformat(g_turn["started_at"])).total_seconds() * 1000
            b_dur = (datetime.fromisoformat(b_turn["finished_at"]) -
                     datetime.fromisoformat(b_turn["started_at"])).total_seconds() * 1000
            deltas.append(g_dur - b_dur)
        except (KeyError, TypeError, ValueError):
            continue
    return deltas


def _diff_summary(governed: dict, baseline: dict) -> dict:
    """Top-level summary of governed vs baseline for the E4 pair diff UI.

    Surfaces:
      * audit_entry_count — governed vs baseline (expect >0 vs 0)
      * turn_count — match is expected; mismatch = misconfig
      * latency_overhead_ms — median + p95 of (governed - baseline) per turn
      * verdicts — side-by-side map of every verdict on both sides
      * classification — expected vs unexpected human-readable diffs
    """
    g_audit = governed.get("audit_entries", []) or []
    b_audit = baseline.get("audit_entries", []) or []
    g_turns = governed.get("turns", []) or []
    b_turns = baseline.get("turns", []) or []

    expected: list[str] = []
    unexpected: list[str] = []

    # Audit presence — governed should have entries, baseline should have zero.
    if len(b_audit) > 0:
        unexpected.append(
            f"baseline has {len(b_audit)} audit entries — direct route is "
            f"leaking through AGW"
        )
    if len(g_audit) == 0:
        unexpected.append(
            "governed has zero audit entries — cidgar may not be wired up"
        )
    if len(g_audit) > 0 and len(b_audit) == 0:
        expected.append(
            f"governed audit count: {len(g_audit)} entries; baseline: 0 (correct)"
        )

    # Turn count match
    if len(g_turns) != len(b_turns):
        unexpected.append(
            f"turn count differs: governed={len(g_turns)}, baseline={len(b_turns)}"
        )

    # Per-turn latency delta (best-effort — turns may lack timestamps if errored)
    deltas_ms = _compute_latency_deltas_ms(g_turns, b_turns)

    return {
        "audit_entry_count": {"governed": len(g_audit), "baseline": len(b_audit)},
        "turn_count": {"governed": len(g_turns), "baseline": len(b_turns)},
        "latency_overhead_ms": {
            "median": _median(deltas_ms) if deltas_ms else None,
            "p95": _percentile(deltas_ms, 95) if deltas_ms else None,
            "n_turns": len(deltas_ms),
        },
        "verdicts": {
            "governed": governed.get("verdicts", {}) or {},
            "baseline": baseline.get("verdicts", {}) or {},
        },
        "classification": {
            "expected_diffs": expected,
            "unexpected_diffs": unexpected,
        },
    }


def _path_get(obj: Any, dotted: str) -> Any:
    """Walk a dotted path through dicts/lists.

    Example: 'turns.0.response.body'. Empty segments (leading dot, double
    dot) are skipped. Returns None on any missing key or out-of-range index.
    """
    cur = obj
    for part in dotted.split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _classify_diff(path: str, g_val: Any, b_val: Any) -> str:
    """Tag a diff as noise / expected_governance_marker / unexpected_diff.

    Heuristics (per E4 brainstorm §2):
      - Identical values → 'noise' (nothing to report)
      - Governed contains cidgar's uuid4_12 marker signature (``ib_`` + 12
        lowercase hex chars) not present in baseline →
        'expected_governance_marker'
      - Everything else → 'unexpected_diff' (worth user attention)

    I2 fix: the prior substring check ``"ib_" in g_str`` matched any text
    containing that literal fragment (e.g. ``fib_number``), producing
    false positives on LLM non-determinism. The regex below requires the
    full 12-hex suffix, which is statistically distinctive enough not to
    collide with arbitrary English/code tokens.
    """
    if g_val == b_val:
        return "noise"
    g_str = (
        json.dumps(g_val) if isinstance(g_val, (dict, list))
        else ("" if g_val is None else str(g_val))
    )
    b_str = (
        json.dumps(b_val) if isinstance(b_val, (dict, list))
        else ("" if b_val is None else str(b_val))
    )
    if _CID_MARKER_RE.search(g_str) and not _CID_MARKER_RE.search(b_str):
        return "expected_governance_marker"
    return "unexpected_diff"


@router.get("/pairs/{row_id}")
def pairs_get(row_id: str):
    """Return both trials in a governed/baseline pair, plus a diff summary.

    The pair is identified by `row_id`; the sibling is resolved via the
    matrix's `baseline_of` metadata (T13). Both rows must have a saved
    `last_trial_id` — the endpoint 409s if either side hasn't been run yet.
    """
    rows = _load_matrix()
    src = next((r for r in rows if r["row_id"] == row_id), None)
    if not src:
        raise HTTPException(404, "row not found")

    # Determine which side of the pair `row_id` is.
    if src.get("routing", "via_agw") == "via_agw":
        governed_row = src
        baseline_row = next(
            (r for r in rows if r.get("baseline_of") == row_id), None
        )
    elif src.get("baseline_of"):
        baseline_row = src
        governed_row = next(
            (r for r in rows if r["row_id"] == src["baseline_of"]), None
        )
    else:
        raise HTTPException(
            400,
            "row is direct-routed but has no baseline_of pointer; "
            "not part of a pair",
        )

    if not governed_row or not baseline_row:
        raise HTTPException(
            404, "pair incomplete: missing baseline or governed sibling"
        )

    governed_trial_id = governed_row.get("last_trial_id")
    baseline_trial_id = baseline_row.get("last_trial_id")
    if not governed_trial_id or not baseline_trial_id:
        raise HTTPException(
            409, "pair has no run history yet — run both rows first"
        )

    from trials import _to_jsonable
    try:
        governed = _to_jsonable(STORE.load(governed_trial_id))
        baseline = _to_jsonable(STORE.load(baseline_trial_id))
    except FileNotFoundError as e:
        raise HTTPException(404, f"trial JSON missing: {e}")

    summary = _diff_summary(governed, baseline)
    return {
        "governed_row_id": governed_row["row_id"],
        "baseline_row_id": baseline_row["row_id"],
        "governed": governed,
        "baseline": baseline,
        "diff_summary": summary,
    }


@router.get("/pairs/{row_id}/diff")
def pairs_diff(row_id: str, path: str = ""):
    """Scoped diff for a specific dotted JSON path within the pair.

    Returns the governed-vs-baseline value at `path` on each side plus a
    classification (expected_governance_marker / unexpected_diff / noise).
    An empty `path` returns the full trials (same as /pairs/{row_id} but
    without the diff_summary envelope).
    """
    pair = pairs_get(row_id)
    governed_val = _path_get(pair["governed"], path) if path else pair["governed"]
    baseline_val = _path_get(pair["baseline"], path) if path else pair["baseline"]
    classification = _classify_diff(path, governed_val, baseline_val)
    return {
        "path": path,
        "governed": governed_val,
        "baseline": baseline_val,
        "classification": classification,
    }


@router.post("/trials/{trial_id}/recompute_verdicts")
def trial_recompute_verdicts(trial_id: str):
    """Re-run compute_verdicts on the persisted trial and save back.

    Use case: verdict (h) depends on matrix.json's baseline_of metadata
    which isn't persisted until AFTER run_trial returns, so the first
    run of a governed trial records h=na. Running the baseline later
    doesn't retroactively update the governed trial's frozen verdicts.
    This endpoint is the explicit 'refresh my verdicts' action.

    Returns the new verdicts dict. Idempotent.

    Gated against running trials (I3 fix) — ABORT_EVENTS membership is
    the canonical "is this trial running" registry. Racing recompute
    against _run_trial_bg's writes could read partial JSON, compute
    verdicts on incomplete data, and silently overwrite the runner's
    final persisted state.
    """
    if trial_id in ABORT_EVENTS:
        raise HTTPException(
            409,
            f"trial {trial_id} is still running — recompute after it completes",
        )
    try:
        trial = STORE.load(trial_id)
    except FileNotFoundError:
        raise HTTPException(404, "trial not found")

    verdicts_out = compute_verdicts(trial)
    # Persist as plain dicts (mirrors runner.run_trial's conversion) so
    # the on-disk shape matches what the UI and other callers subscript.
    trial.verdicts = {
        k: {"verdict": v.verdict, "reason": v.reason}
        for k, v in verdicts_out.items()
    }
    STORE.save(trial)
    return {"trial_id": trial_id, "verdicts": trial.verdicts}


@router.post("/trials/{trial_id}/abort")
async def trial_abort(trial_id: str):
    """Request cooperative abort of a running trial.

    The currently-executing turn finishes naturally (so no HTTP-mid-call
    corruption / framework state poisoning); subsequent turns are skipped.
    The trial transitions to status=aborted and verdicts compute on whatever
    turns completed.

    Returns:
      - 404 when the trial does not exist (no event registered AND no trial
        JSON on disk).
      - {ok: False, reason, status} when the trial already finished (no
        event in the registry, but the trial JSON exists with a terminal
        status). Idempotent — a second abort on a completed trial is a
        no-op rather than an error.
      - {ok: True, abort_requested: True, trial_id} on success.
    """
    ev = ABORT_EVENTS.get(trial_id)
    if ev is None:
        try:
            trial = STORE.load(trial_id)
        except FileNotFoundError:
            raise HTTPException(404, "trial not found")
        return {
            "ok": False,
            "reason": f"trial is not running (status={trial.status})",
            "status": trial.status,
            "trial_id": trial_id,
        }
    ev.set()
    return {"ok": True, "abort_requested": True, "trial_id": trial_id}


