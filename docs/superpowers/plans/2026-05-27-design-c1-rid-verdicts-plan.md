# Design C1 — RID Efficacy Verdicts (l, m) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two efficacy verdicts to the aiplay harness that score Design B's RID audit data — `verdict_l` (run-lineage integrity) and `verdict_m` (turn-boundary correctness).

**Architecture:** Two pure verdict functions in `harness/efficacy.py`, registered in `compute_verdicts`, reading RID fields from `AuditEntry.body`. verdict_l walks the global ordered `llm_request` run list and checks the `parent_rid` chain (lenient/truncation-tolerant default + `strict` param). verdict_m maps `llm_request` entries to user-turn time-windows and checks `is_turn_boundary` placement. No new files, no ingestion change, no AGW change.

**Tech Stack:** Python 3, pytest. Existing harness modules: `harness/efficacy.py`, `harness/trials.py` (`Trial`, `AuditEntry`, `Turn`, `Verdict`, `TrialConfig`, `TurnPlan`), tests in `tests/test_efficacy.py`.

**Spec:** `docs/superpowers/specs/2026-05-27-design-c1-rid-verdicts-design.md`

**Spec corrections discovered during planning (this plan supersedes the spec on these mechanics):**
1. verdict_m uses **time-window** turn-mapping (`turn.started_at`/`finished_at` vs `entry.captured_at`), NOT `_audit_for_turn` (which keys on `turn_id`, unpopulated in practice). This mirrors the existing `_cids_for_turn_window`.
2. verdict_l needs **no** turn mapping — it operates on the global `llm_request` run order (sorted by `captured_at`).

---

## File map

- **Modify** `harness/efficacy.py` — add 4 accessors + 2 ordering helpers + 2 verdict functions + 3 registration edits.
- **Modify** `tests/test_efficacy.py` — add verdict_l and verdict_m test suites.
- **Verify** `frontend/trial.js` (Task 5) — confirm verdict letters render generically; one-line fix only if hard-coded.

All new code follows the existing `efficacy.py` conventions: module-level helper functions prefixed `_`, dict/attr duality (entries may be `AuditEntry` objects or plain dicts in tests), `Verdict(verdict, reason)` returns.

---

## Task 1: Shared RID accessors + ordered-runs helper

**Files:**
- Modify: `harness/efficacy.py` (add helpers near the existing `_audit_kind`, around line 869)
- Test: `tests/test_efficacy.py`

- [ ] **Step 1.1: Write the failing test**

Add to `tests/test_efficacy.py`:

```python
# ── Design C1 — RID accessor helpers ──

def test_rid_accessors_read_from_body():
    from efficacy import _rid, _parent_rid, _is_turn_boundary, _rid_anomaly
    e = AuditEntry(
        trial_id="t", turn_id=None, phase="llm_request",
        cid="ibc_aaa", backend="ollama", raw={}, captured_at="2026-01-01T00:00:00Z",
        body={"rid": "ibr_111", "parent_rid": "ibr_000",
              "is_turn_boundary": True, "parent_rid_anomaly": True},
    )
    assert _rid(e) == "ibr_111"
    assert _parent_rid(e) == "ibr_000"
    assert _is_turn_boundary(e) is True
    assert _rid_anomaly(e) is True


def test_rid_accessors_default_when_body_absent():
    from efficacy import _rid, _parent_rid, _is_turn_boundary, _rid_anomaly
    e = AuditEntry(
        trial_id="t", turn_id=None, phase="llm_request",
        cid="ibc_aaa", backend="ollama", raw={}, captured_at="", body=None,
    )
    assert _rid(e) is None
    assert _parent_rid(e) is None
    assert _is_turn_boundary(e) is None
    assert _rid_anomaly(e) is False


def test_llm_runs_ordered_filters_and_sorts():
    from efficacy import _llm_runs_ordered
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="terminal",
                   cid="ibc_a", backend="ollama", raw={}, captured_at="2026-01-01T00:00:03Z",
                   body={}),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ibc_a", backend="ollama", raw={}, captured_at="2026-01-01T00:00:02Z",
                   body={"rid": "ibr_2"}),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ibc_a", backend="ollama", raw={}, captured_at="2026-01-01T00:00:01Z",
                   body={"rid": "ibr_1"}),
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ibc_a", backend="ollama", raw={}, captured_at="2026-01-01T00:00:04Z",
                   body={}),  # llm_request without rid → excluded
    ]
    trial = _trial_with(turns, audit)
    runs = _llm_runs_ordered(trial)
    from efficacy import _rid
    assert [_rid(r) for r in runs] == ["ibr_1", "ibr_2"]
```

- [ ] **Step 1.2: Run to verify it fails**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py::test_rid_accessors_read_from_body ../tests/test_efficacy.py::test_rid_accessors_default_when_body_absent ../tests/test_efficacy.py::test_llm_runs_ordered_filters_and_sorts -v`
Expected: FAIL — `ImportError: cannot import name '_rid' from 'efficacy'`.

- [ ] **Step 1.3: Implement the helpers**

In `harness/efficacy.py`, add directly after `_audit_kind` (around line 950):

```python
def _entry_body(entry) -> dict:
    """Design C1 — the governance audit body dict for an entry, or {}.

    `AuditEntry.body` (E26) holds the parsed cidgar log body, which carries
    the Design B RID fields (rid, parent_rid, is_turn_boundary,
    parent_rid_anomaly, ...). Synthetic test entries / dicts expose the same
    under a "body" key. Pre-E26 / pre-Design-B entries have body=None.
    """
    if isinstance(entry, dict):
        b = entry.get("body")
    else:
        b = getattr(entry, "body", None)
    return b if isinstance(b, dict) else {}


def _rid(entry) -> str | None:
    return _entry_body(entry).get("rid")


def _parent_rid(entry) -> str | None:
    return _entry_body(entry).get("parent_rid")


def _is_turn_boundary(entry):
    """Tri-state: True / False / None (flag absent → cannot assess)."""
    return _entry_body(entry).get("is_turn_boundary")


def _rid_anomaly(entry) -> bool:
    return bool(_entry_body(entry).get("parent_rid_anomaly", False))


def _llm_runs_ordered(trial) -> list:
    """Design C1 — the LLM runs of a trial, in capture order.

    An LLM run = one `llm_request` audit entry that carries a `rid`. Their
    `parent_rid` values form the lineage chain consumed by verdict_l. Sorted
    by `captured_at` (ISO-8601, lexicographically orderable as Harness writes
    it); ties keep insertion order via Python's stable sort.
    """
    runs = [e for e in trial.audit_entries
            if _audit_kind(e) == "llm_request" and _rid(e)]
    runs.sort(key=lambda e: (getattr(e, "captured_at", "") or "")
              if not isinstance(e, dict) else (e.get("captured_at") or ""))
    return runs
```

- [ ] **Step 1.4: Run to verify it passes**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py::test_rid_accessors_read_from_body ../tests/test_efficacy.py::test_rid_accessors_default_when_body_absent ../tests/test_efficacy.py::test_llm_runs_ordered_filters_and_sorts -v`
Expected: 3 passed.

- [ ] **Step 1.5: Commit**

```bash
git add harness/efficacy.py tests/test_efficacy.py
git commit -m "feat(efficacy): C1 RID accessors + ordered-runs helper

Shared building blocks for verdict_l/_m: _rid/_parent_rid/
_is_turn_boundary/_rid_anomaly read Design B fields from AuditEntry.body;
_llm_runs_ordered returns rid-bearing llm_request entries in capture order.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: verdict_l — run-lineage integrity

**Files:**
- Modify: `harness/efficacy.py` (add `verdict_l_run_lineage_integrity` after `_llm_runs_ordered`)
- Test: `tests/test_efficacy.py`

- [ ] **Step 2.1: Write the failing tests**

Add to `tests/test_efficacy.py`. Helper to build a run entry, then the cases:

```python
# ── Design C1 — verdict_l run-lineage integrity ──

def _run(rid, parent_rid, ts, *, anomaly=False):
    """A single llm_request run audit entry for verdict_l tests."""
    body = {"rid": rid}
    if parent_rid is not None:
        body["parent_rid"] = parent_rid
    if anomaly:
        body["parent_rid_anomaly"] = True
    return AuditEntry(
        trial_id="t", turn_id=None, phase="llm_request",
        cid="ibc_a", backend="ollama", raw={}, captured_at=ts, body=body,
    )


def _verdict_l(audit, strict=False):
    from efficacy import verdict_l_run_lineage_integrity
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    trial = _trial_with(turns, audit)
    return verdict_l_run_lineage_integrity(trial, strict=strict)


def test_verdict_l_pass_clean_chain():
    audit = [
        _run("ibr_0", None, "2026-01-01T00:00:01Z"),
        _run("ibr_1", "ibr_0", "2026-01-01T00:00:02Z"),
        _run("ibr_2", "ibr_1", "2026-01-01T00:00:03Z"),
    ]
    assert _verdict_l(audit).verdict == "pass"
    assert _verdict_l(audit, strict=True).verdict == "pass"


def test_verdict_l_skip_to_ancestor_pass_lenient_fail_strict():
    # ibr_2's parent is the grandparent ibr_0 (truncation dropped ibr_1's marker)
    audit = [
        _run("ibr_0", None, "2026-01-01T00:00:01Z"),
        _run("ibr_1", "ibr_0", "2026-01-01T00:00:02Z"),
        _run("ibr_2", "ibr_0", "2026-01-01T00:00:03Z"),
    ]
    lenient = _verdict_l(audit)
    assert lenient.verdict == "pass"
    assert "skip" in lenient.reason
    assert _verdict_l(audit, strict=True).verdict == "fail"


def test_verdict_l_null_parent_gap_pass_lenient_fail_strict():
    audit = [
        _run("ibr_0", None, "2026-01-01T00:00:01Z"),
        _run("ibr_1", None, "2026-01-01T00:00:02Z"),   # non-genesis null parent
        _run("ibr_2", "ibr_1", "2026-01-01T00:00:03Z"),
    ]
    lenient = _verdict_l(audit)
    assert lenient.verdict == "pass"
    assert "gap" in lenient.reason
    assert _verdict_l(audit, strict=True).verdict == "fail"


def test_verdict_l_orphan_fail_both_modes():
    audit = [
        _run("ibr_0", None, "2026-01-01T00:00:01Z"),
        _run("ibr_1", "ibr_999", "2026-01-01T00:00:02Z"),  # parent never seen
    ]
    assert _verdict_l(audit).verdict == "fail"
    assert _verdict_l(audit, strict=True).verdict == "fail"


def test_verdict_l_forward_reference_fail():
    audit = [
        _run("ibr_0", None, "2026-01-01T00:00:01Z"),
        _run("ibr_1", "ibr_2", "2026-01-01T00:00:02Z"),  # points to a later run
        _run("ibr_2", "ibr_1", "2026-01-01T00:00:03Z"),
    ]
    assert _verdict_l(audit).verdict == "fail"


def test_verdict_l_genesis_only_na():
    audit = [_run("ibr_0", None, "2026-01-01T00:00:01Z")]
    assert _verdict_l(audit).verdict == "na"


def test_verdict_l_no_rid_data_na():
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ibc_a", backend="ollama", raw={}, captured_at="2026-01-01T00:00:01Z",
                   body={}),
    ]
    assert _verdict_l(audit).verdict == "na"


def test_verdict_l_all_null_parents_na():
    audit = [
        _run("ibr_0", None, "2026-01-01T00:00:01Z"),
        _run("ibr_1", None, "2026-01-01T00:00:02Z"),
        _run("ibr_2", None, "2026-01-01T00:00:03Z"),
    ]
    assert _verdict_l(audit).verdict == "na"


def test_verdict_l_anomaly_surfaced_not_failed():
    audit = [
        _run("ibr_0", None, "2026-01-01T00:00:01Z"),
        _run("ibr_1", "ibr_0", "2026-01-01T00:00:02Z", anomaly=True),
    ]
    v = _verdict_l(audit)
    assert v.verdict == "pass"
    assert "anomaly" in v.reason
```

- [ ] **Step 2.2: Run to verify it fails**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py -k verdict_l -v`
Expected: FAIL — `ImportError: cannot import name 'verdict_l_run_lineage_integrity'`.

- [ ] **Step 2.3: Implement verdict_l**

In `harness/efficacy.py`, add after `_llm_runs_ordered`:

```python
def verdict_l_run_lineage_integrity(trial, strict: bool = False) -> Verdict:
    """(l) run-lineage integrity — Design B parent_rid chain reconstructs.

    The rid-bearing llm_request entries, in capture order, are the runs
    [r0..rk]; each run's parent_rid should point backward to an earlier run.

    Lenient default (truncation-tolerant):
      pass — every non-genesis run links backward to a real earlier run.
             Skip-to-ancestor (parent = grandparent, from truncation) and
             null-parent gaps are tolerated and counted in the reason.
      fail — orphan (parent_rid never seen in the trial) or forward-reference
             (parent_rid is a later/self run).
      na   — no rid data, single run, or every non-genesis run null-parent.

    strict=True (full-history-replay assumption): additionally require
    immediate-predecessor linkage; any skip OR null-parent gap fails.

    parent_rid_anomaly is surfaced in the reason but never changes the
    verdict (AGW spec §9.17 — free signal). See design C1 spec.
    """
    runs = _llm_runs_ordered(trial)
    if not runs:
        return Verdict("na", "no llm_request audits carry a rid")
    if len(runs) < 2:
        return Verdict("na", "single run — no lineage to assess")

    rids = [_rid(r) for r in runs]
    anomaly_rids = [rids[i] for i, r in enumerate(runs) if _rid_anomaly(r)]
    seen = {rids[0]}  # r0 genesis — null parent expected
    skips = 0
    gaps = 0

    for k in range(1, len(runs)):
        prid = _parent_rid(runs[k])
        if prid is None:
            if strict:
                return Verdict(
                    "fail",
                    f"run {k} (rid {rids[k]}) has null parent_rid "
                    f"(strict mode expects full replay)",
                )
            gaps += 1
            seen.add(rids[k])
            continue
        if prid not in seen:
            if prid in rids[k:]:
                return Verdict(
                    "fail",
                    f"run {k} (rid {rids[k]}) parent_rid {prid} is a "
                    f"forward reference",
                )
            return Verdict(
                "fail",
                f"run {k} (rid {rids[k]}) parent_rid {prid} references a "
                f"run never seen in the trial (orphan)",
            )
        if prid != rids[k - 1]:
            if strict:
                return Verdict(
                    "fail",
                    f"run {k} (rid {rids[k]}) parent_rid {prid} != immediate "
                    f"predecessor {rids[k - 1]} (strict mode)",
                )
            skips += 1
        seen.add(rids[k])

    if gaps == len(runs) - 1:
        return Verdict(
            "na",
            "no lineage recoverable — every non-genesis run has a null "
            "parent_rid (truncating agent or RID propagation off)",
        )

    notes = []
    if skips:
        notes.append(f"{skips} skip(s) — truncation")
    if gaps:
        notes.append(f"{gaps} null-parent gap(s)")
    if anomaly_rids:
        notes.append(f"anomaly on {len(anomaly_rids)} run(s): {anomaly_rids}")
    reason = "lineage valid"
    if notes:
        reason += "; " + ", ".join(notes)
    return Verdict("pass", reason)
```

- [ ] **Step 2.4: Run to verify it passes**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py -k verdict_l -v`
Expected: 9 passed.

- [ ] **Step 2.5: Commit**

```bash
git add harness/efficacy.py tests/test_efficacy.py
git commit -m "feat(efficacy): C1 verdict_l run-lineage integrity

Lenient/truncation-tolerant default (pass on valid backward link, tolerate
ancestor-skips and null-parent gaps, fail only on orphan/forward-ref) +
strict param (immediate-predecessor required). parent_rid_anomaly surfaced,
never fails. na on no-rid/single-run/all-null.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: verdict_m — turn-boundary correctness

**Files:**
- Modify: `harness/efficacy.py` (add `_llm_requests_in_window` + `verdict_m_turn_boundary_correctness`)
- Test: `tests/test_efficacy.py`

- [ ] **Step 3.1: Write the failing tests**

Add to `tests/test_efficacy.py`:

```python
# ── Design C1 — verdict_m turn-boundary correctness ──

def _boundary_run(rid, is_boundary, ts):
    return AuditEntry(
        trial_id="t", turn_id=None, phase="llm_request",
        cid="ibc_a", backend="ollama", raw={}, captured_at=ts,
        body={"rid": rid, "is_turn_boundary": is_boundary},
    )


def _verdict_m(turns, audit):
    from efficacy import verdict_m_turn_boundary_correctness
    trial = _trial_with(turns, audit)
    return verdict_m_turn_boundary_correctness(trial)


def test_verdict_m_pass_correct_boundaries():
    # Two turns; turn 0 = single run (boundary), turn 1 = boundary + tool hop.
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:09Z"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg",
             started_at="2026-01-01T00:00:10Z", finished_at="2026-01-01T00:00:19Z"),
    ]
    audit = [
        _boundary_run("ibr_0", True, "2026-01-01T00:00:01Z"),
        _boundary_run("ibr_1", True, "2026-01-01T00:00:11Z"),
        _boundary_run("ibr_2", False, "2026-01-01T00:00:12Z"),  # tool hop
    ]
    assert _verdict_m(turns, audit).verdict == "pass"


def test_verdict_m_fail_turn_start_not_flagged():
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:09Z"),
    ]
    audit = [_boundary_run("ibr_0", False, "2026-01-01T00:00:01Z")]
    assert _verdict_m(turns, audit).verdict == "fail"


def test_verdict_m_fail_midturn_hop_flagged():
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:09Z"),
    ]
    audit = [
        _boundary_run("ibr_0", True, "2026-01-01T00:00:01Z"),
        _boundary_run("ibr_1", True, "2026-01-01T00:00:02Z"),  # continuation wrongly flagged
    ]
    assert _verdict_m(turns, audit).verdict == "fail"


def test_verdict_m_na_when_flag_absent():
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg",
                  started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:09Z")]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ibc_a", backend="ollama", raw={}, captured_at="2026-01-01T00:00:01Z",
                   body={"rid": "ibr_0"}),  # no is_turn_boundary key
    ]
    assert _verdict_m(turns, audit).verdict == "na"


def test_verdict_m_na_when_no_turn_windows():
    # Flag present but turns lack started_at/finished_at and no turn_id demux.
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    audit = [_boundary_run("ibr_0", True, "2026-01-01T00:00:01Z")]
    assert _verdict_m(turns, audit).verdict == "na"


def test_verdict_m_na_no_user_turns():
    turns = []
    audit = [_boundary_run("ibr_0", True, "2026-01-01T00:00:01Z")]
    assert _verdict_m(turns, audit).verdict == "na"
```

- [ ] **Step 3.2: Run to verify it fails**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py -k verdict_m -v`
Expected: FAIL — `ImportError: cannot import name 'verdict_m_turn_boundary_correctness'`.

- [ ] **Step 3.3: Implement the window helper and verdict_m**

In `harness/efficacy.py`, add after `verdict_l_run_lineage_integrity`:

```python
def _llm_requests_in_window(trial, turn) -> list:
    """Design C1 — rid/flag-bearing llm_request entries for a turn, ordered.

    Mirrors _cids_for_turn_window's correlation strategy: prefer header-demux
    (entries tagged with this turn_id) when available; else fall back to the
    turn's [started_at, finished_at] timestamp envelope. Returns [] when
    neither channel resolves (caller treats trial as unassessable → na).
    """
    direct = [e for e in trial.audit_entries
              if e.turn_id == turn.turn_id and _audit_kind(e) == "llm_request"]
    if direct:
        runs = direct
    else:
        if not turn.started_at or not turn.finished_at:
            return []
        runs = [e for e in trial.audit_entries
                if _audit_kind(e) == "llm_request"
                and e.captured_at
                and turn.started_at <= e.captured_at <= turn.finished_at]
    runs.sort(key=lambda e: e.captured_at or "")
    return runs


def verdict_m_turn_boundary_correctness(trial) -> Verdict:
    """(m) turn-boundary correctness — AGW's is_turn_boundary lands right.

    For each user_msg turn, the first llm_request in the turn's window must
    have is_turn_boundary=true and every continuation (tool-loop hop) must be
    false. Validates AGW's body-shape heuristic; orthogonal to verdict_l's
    parent chain.

      pass — every turn's first run is a boundary, all continuations are not.
      fail — a turn-start run isn't flagged, a continuation is flagged, or
             the boundary count != user-turn count.
      na   — no user turns, is_turn_boundary absent on all llm_requests, or
             turn windows are unresolvable (no timestamps + no demux).

    See design C1 spec. Positional (not count-only): a misplaced boundary
    with the right total still fails.
    """
    user_turns = _user_msg_turns(trial)
    if not user_turns:
        return Verdict("na", "no user_msg turns")

    flag_present = any(
        _is_turn_boundary(e) is not None
        for e in trial.audit_entries if _audit_kind(e) == "llm_request"
    )
    if not flag_present:
        return Verdict(
            "na",
            "no is_turn_boundary flag on any llm_request "
            "(no-governance / streaming / pre-Design-B)",
        )

    boundary_count = 0
    resolved_turns = 0
    for turn in user_turns:
        runs = _llm_requests_in_window(trial, turn)
        if not runs:
            continue
        resolved_turns += 1
        first, rest = runs[0], runs[1:]
        if _is_turn_boundary(first) is not True:
            return Verdict(
                "fail",
                f"turn {turn.turn_idx}: first run (rid {_rid(first)}) is not "
                f"is_turn_boundary=true",
            )
        for r in rest:
            if _is_turn_boundary(r) is True:
                return Verdict(
                    "fail",
                    f"turn {turn.turn_idx}: a continuation run (rid {_rid(r)}) "
                    f"is is_turn_boundary=true (should be false)",
                )
        boundary_count += 1

    if resolved_turns == 0:
        return Verdict(
            "na",
            "turn windows unresolvable (turns lack timestamps and no "
            "turn_id demux)",
        )
    if boundary_count != len(user_turns):
        return Verdict(
            "fail",
            f"turn-boundary count {boundary_count} != user-turn count "
            f"{len(user_turns)} (a turn window had no llm_request runs)",
        )
    return Verdict("pass", f"{boundary_count} turn boundaries correctly placed")
```

- [ ] **Step 3.4: Run to verify it passes**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py -k verdict_m -v`
Expected: 6 passed.

- [ ] **Step 3.5: Commit**

```bash
git add harness/efficacy.py tests/test_efficacy.py
git commit -m "feat(efficacy): C1 verdict_m turn-boundary correctness

Positional check: per user-turn time-window, the first llm_request must be
is_turn_boundary=true and continuations false. Time-window turn mapping
(mirrors _cids_for_turn_window), not turn_id demux. na when flag absent or
windows unresolvable.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Register verdict_l and verdict_m in compute_verdicts

**Files:**
- Modify: `harness/efficacy.py` (`compute_verdicts` — the two `na` guard dicts + the main return)
- Test: `tests/test_efficacy.py`

- [ ] **Step 4.1: Write the failing test**

Add to `tests/test_efficacy.py`:

```python
# ── Design C1 — registration in compute_verdicts ──

def test_compute_verdicts_includes_l_and_m():
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg",
             started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:09Z"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id=None, phase="llm_request",
                   cid="ibc_a", backend="ollama", raw={}, captured_at="2026-01-01T00:00:01Z",
                   body={"rid": "ibr_0", "is_turn_boundary": True}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert "l" in v and "m" in v
    # single run → l is na; single correctly-flagged turn → m passes
    assert v["l"].verdict == "na"
    assert v["m"].verdict == "pass"


def test_compute_verdicts_l_m_na_for_direct_routing():
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    trial = _trial_with(turns, [], routing="direct")
    v = compute_verdicts(trial)
    assert v["l"].verdict == "na"
    assert v["m"].verdict == "na"
```

- [ ] **Step 4.2: Run to verify it fails**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py -k "compute_verdicts_includes_l_and_m or compute_verdicts_l_m_na_for_direct" -v`
Expected: FAIL — `KeyError: 'l'` (l/m not yet registered).

- [ ] **Step 4.3: Register in all three sites**

First locate every place the verdict map is produced:

```bash
grep -n '"a": ' harness/efficacy.py
```

There are three: the `routing == "direct"` na-dict, the `status == "aborted"` na-dict, and the main return (all in `compute_verdicts`, ~lines 1183, 1190, 1202).

In **both** na-guard dicts, extend the key list. Each currently reads:

```python
        return {
            "a": na, "b": na, "c": na, "d": na, "e": na,
            "f": na, "h": na, "i": na, "k": na,
        }
```

Change each to:

```python
        return {
            "a": na, "b": na, "c": na, "d": na, "e": na,
            "f": na, "h": na, "i": na, "k": na, "l": na, "m": na,
        }
```

In the main return, add the two lines after `"k": ...`:

```python
        "i": verdict_i_tools_list_correlation(trial),
        "k": verdict_k_cross_api_continuity(trial),
        "l": verdict_l_run_lineage_integrity(trial),
        "m": verdict_m_turn_boundary_correctness(trial),
    }
```

- [ ] **Step 4.4: Run to verify it passes**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py -k "compute_verdicts_includes_l_and_m or compute_verdicts_l_m_na_for_direct" -v`
Expected: 2 passed.

- [ ] **Step 4.5: Run the full efficacy suite (no regressions)**

Run: `cd harness && python -m pytest ../tests/test_efficacy.py -v 2>&1 | tail -20`
Expected: all pass (pre-existing + the new C1 tests).

- [ ] **Step 4.6: Commit**

```bash
git add harness/efficacy.py tests/test_efficacy.py
git commit -m "feat(efficacy): register C1 verdicts l + m in compute_verdicts

Added to the main verdict map and both na-guard dicts (direct routing,
aborted trial).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Verify dashboard renders l/m + final verification

**Files:**
- Verify (and only if needed, modify): `frontend/trial.js`

- [ ] **Step 5.1: Check how the dashboard renders verdict letters**

Run: `grep -n "verdict" frontend/trial.js | head -30`

Determine whether verdicts are rendered by iterating the verdict map (e.g. `Object.entries(trial.verdicts)`) or by a hard-coded letter list.

- [ ] **Step 5.2: Decide**

- If it **iterates** the map generically → no change. New letters l/m surface automatically. Note this in the commit message of Step 5.4 and skip the edit.
- If it **hard-codes** a letter list (e.g. `["a","b","c",...]`) or per-letter labels → add `"l"` and `"m"` (with short labels: l = "Run lineage", m = "Turn boundary"). This is a one-line/two-line data addition, NOT the C2 lineage visualization.

- [ ] **Step 5.3: (Only if hard-coded) Add the labels**

Add `l` and `m` to the letter list / label map in `frontend/trial.js`, mirroring the existing entries' shape. Labels: `l: "Run lineage"`, `m: "Turn boundary"`.

- [ ] **Step 5.4: Final full-suite verification**

Run the broader harness test set to confirm nothing regressed:

```bash
cd harness && python -m pytest ../tests/test_efficacy.py ../tests/test_api.py -q 2>&1 | tail -15
```

Expected: all pass.

- [ ] **Step 5.5: Commit (if Step 5.3 made changes; else skip)**

```bash
git add frontend/trial.js
git commit -m "feat(frontend): surface verdict l/m labels in trial view

Verdict letters l (run lineage) + m (turn boundary) render in the existing
verdict list. Not the C2 lineage visualization — just the per-letter label
so the new verdicts appear alongside a-k.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Deferred (TODOs — not in C1, recorded for later)

- **verdict_n — cross-API RID continuity** (RID analog of verdict_k, rich A/B/C taxonomy, reuses verdict_k's route-walking + request-body machinery). Blocked on cross-API RID trials + Responses-API RID (`previous_response_id`, AGW TODO).
- **`strict_lineage` RowConfig flag + UI toggle** — wire `verdict_l(strict=...)` to a per-row config field (verdict-side) and a toggle control (frontend-side). Scope with C2.
- **C2 — frontend lineage visualization** — render the `rid → parent_rid` chain + anomalies in the trial view. Separate design cycle.

---

## Self-review notes

- **Spec coverage:** verdict_l (lenient + strict + anomaly-surface) → Task 2. verdict_m (positional) → Task 3. Registration → Task 4. Dashboard surfacing → Task 5. Accessors → Task 1. Deferred TODOs carried forward. All spec sections covered.
- **Spec mechanic corrections** (documented at top): verdict_m time-window mapping (not `_audit_for_turn`); verdict_l global run order (no turn dependency). These are improvements found during planning; the spec's intent is unchanged.
- **Type/name consistency:** `_rid`/`_parent_rid`/`_is_turn_boundary`/`_rid_anomaly`/`_entry_body`/`_llm_runs_ordered`/`_llm_requests_in_window`/`verdict_l_run_lineage_integrity`/`verdict_m_turn_boundary_correctness` used consistently across tasks. `AuditEntry` kwargs (`trial_id`, `turn_id`, `phase`, `cid`, `backend`, `raw`, `captured_at`, `body`) and `Turn` kwargs (`turn_id`, `turn_idx`, `kind`, `started_at`, `finished_at`) match `harness/trials.py`. `Verdict(verdict, reason)` matches.
