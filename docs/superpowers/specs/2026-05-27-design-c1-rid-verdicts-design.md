---
status: proposed
drafted: 2026-05-27
design: C1 (first sub-project of Design C)
depends_on:
  - Design B (run-identity RID infrastructure) — AGW emits the RID audit fields
  - CHG-26F (f2→f3 / f4→f5 RID handoff fix) — makes rid/parent_rid actually populate
---

# Design C1 — RID efficacy verdicts (l, m)

## Summary

Design B made AGW emit run-identity audit fields (`rid`, `parent_rid`,
`parent_run_rid`, `is_turn_boundary`, `parent_rid_sources`,
`parent_rid_anomaly`, `provider_response_id`). Nothing in aiplay scores them
yet. Design C is the aiplay consumer side, split into two independent
sub-projects:

- **C1 (this spec)** — two efficacy verdicts, `l` (run-lineage integrity) and
  `m` (turn-boundary correctness), in the Python harness.
- **C2 (separate cycle)** — frontend lineage visualization in the dashboard.

C1 is the higher-value, immediately-testable half: pure verdict functions over
the trial's audit stream, validated against synthetic fixtures, no AGW change
and no frontend dependency.

## Scope

In scope:
- `verdict_l_run_lineage_integrity(trial) -> Verdict`
- `verdict_m_turn_boundary_correctness(trial) -> Verdict`
- Registration of both in `compute_verdicts`.
- Small shared accessors for reading RID fields off `AuditEntry.body`.
- Synthetic-fixture tests in `tests/test_efficacy.py`.

Out of scope (recorded as TODOs below):
- verdict_n (cross-API RID continuity).
- `strict_lineage` per-row config flag + its UI toggle.
- C2 frontend lineage visualization.

## Background — how the RID data reaches a verdict

Verdicts are pure functions `verdict_x(trial: Trial) -> Verdict` (where
`Verdict` is `{verdict: "pass"|"fail"|"na"|"error", reason: str}`), registered
in `compute_verdicts` (`harness/efficacy.py`). Existing letters in use: a, b,
c, d, e, f, h, i, k. Free letters: g, j, **l, m, n**.

The RID fields are reachable today with no ingestion change. `audit_tail.py`
parses each governance log line (both JSON and structured-text shapes) and
populates a `body` dict; `AuditEntry.body` (the E26 field) carries it, and both
trial-audit ingestion paths in `api.py` pass `body=entry.get("body")`. So a
verdict reads `entry.body["rid"]`, `["parent_rid"]`, `["is_turn_boundary"]`,
`["parent_rid_anomaly"]` directly.

Legacy pre-Design-B trials have `body=None` and no RID data → both verdicts
return `na` naturally; no raw-fallback is needed (unlike verdict_i, which
back-walks `raw` for the pre-E26 `correlation_lost` field).

### `parent_rid_anomaly` semantics (consumed by verdict_l)

From the f2 resolver (AGW `cidgar.rs`): f2 scans the request body for `_ib_rid`
carriers across channels C1 (tool_use.input), C2 (text marker), C3 (resource
block), producing `(carrier, rid)` candidates. It picks the winning
`parent_rid` by priority chain (`prev_resp_id → c1 → c3 → c2`,
most-recent-by-position within a class), then:

```
parent_rid_anomaly = any observed candidate's rid != the chosen winning rid
```

i.e. **true iff two or more carriers carried conflicting `_ib_rid` values.**
It is a disagreement detector, not a wrong-parent detector: the chosen
`parent_rid` still follows the priority chain, so lineage can be fully intact
with anomaly true. Per AGW spec §9.17 it is a "free signal, no behavior
change." verdict_l therefore **surfaces** anomaly in its reason but never fails
on it alone.

## Architecture

Both verdicts are standalone functions in `harness/efficacy.py` (Approach A —
no shared chain-extraction layer; that abstraction would only serve the
deferred verdict_n and is YAGNI now). They share four small accessors that
mirror the existing `_cid` closures and handle the dict/attr duality of
`AuditEntry`:

- `_rid(entry) -> str | None` — `entry.body["rid"]`
- `_parent_rid(entry) -> str | None` — `entry.body["parent_rid"]`
- `_is_turn_boundary(entry) -> bool | None` — `entry.body["is_turn_boundary"]`
- `_rid_anomaly(entry) -> bool` — `entry.body["parent_rid_anomaly"]` (absent → False)

All read from `entry.body`, returning `None`/`False` when `body` is absent or
the key is missing (the skip-serializing-if AGW behavior omits default-valued
fields).

No new files. No changes to `audit_tail.py`, `trials.py`, `api.py`, or AGW.

## verdict_l — run-lineage integrity

Validates that the `parent_rid` chain reconstructs across the trial's LLM runs.

**Run model.** The `llm_request` audit entries, in capture order, are the runs
`[r0, r1, …, rk]`. The runs form a linear chain: each run's `parent_rid` should
point backward to an earlier run — within a turn (tool-loop hops: run K's body
replays run K-1's injected `_ib_rid`) and across turns (turn N's first run
replays turn N-1's history, whose most-recent surviving `_ib_rid` is turn N-1's
last run). The cross-turn link "turn N first run's `parent_rid` == turn N-1 last
run's `rid`" is the boundary case of this chain.

**Default mode (lenient — truncation-tolerant).** This is the default because
agents that truncate history legitimately break the *tight* chain while keeping
a valid *backward* link.

- `r0` (genesis run) — null `parent_rid` is expected; not a failure.
- Every non-genesis run `r_k` — `pass` if `parent_rid` points backward to a real
  earlier run in the trial (`parent_rid ∈ {rid(r0)…rid(r_{k-1})}`).
- A **skip-to-ancestor** (e.g. `parent_rid == rid(r_{k-2})` because truncation
  dropped the immediate predecessor's marker but an older one survived) is a
  valid backward link → **pass**, counted in the reason
  (e.g. `"lineage valid; 2 skips (truncation)"`).
- A **null `parent_rid` on a non-genesis run** is **tolerated** (counted as a
  gap in the reason), not failed: from the audit stream alone we cannot
  distinguish legitimate truncation/no-replay (§9.18/§9.16) from an
  agent-side propagation drop, so the lenient default does not assume a bug.
  (Strict mode does fail on it — see below.)
- **`fail`** only on an unambiguous breach detectable from the audit stream:
  - **orphan** — `parent_rid` references a `rid` that never appears anywhere in
    the trial (foreign/stale marker, or AGW mint error);
  - **forward-reference** — `parent_rid == rid(r_j)` with `j ≥ k`.
- **`na`** — no `llm_request` entry carries a `rid` (chat-only / no-governance /
  pre-Design-B trial), OR there is only a genesis run (nothing to chain), OR
  every non-genesis run has a null `parent_rid` (no lineage recoverable at all —
  lineage cannot be assessed).

Note: distinguishing "null because the body had no marker (legitimate)" from
"null because AGW had a marker but failed to extract (bug)" requires inspecting
the LLM **request** bodies (`framework_events` / exchanges), which is a
different data source than the governance audit stream. That extraction-bug
discrimination is intentionally left to verdict_n's rich taxonomy (which reuses
verdict_k's request-body machinery), keeping verdict_l audit-stream-only and
lean.

**Strict mode (`strict=True`, default `False`).** Assumes full-history replay,
so the tight chain must hold: for every non-genesis `r_k`,
`parent_rid == rid(r_{k-1})`. Any **skip** or **null `parent_rid`** on a
non-genesis run is a `fail` (under full replay, the immediate predecessor's
marker must be present and extracted). Intended for full-history-replay
frameworks (langchain, crewai, …) where a skip/gap indicates a real propagation
gap. Exercised via the function parameter in tests; per-row config wiring is
deferred (see TODOs).

**Anomaly.** Independently of pass/fail, if any run has
`parent_rid_anomaly == true`, append a note to the reason
(e.g. `"anomaly on r3 (carriers disagreed)"`). Never changes the verdict.

**Reason content.** Names the offending run on failure; on pass, notes skip
count and any anomaly. Mirrors verdict_c's lean, descriptive style.

## verdict_m — turn-boundary correctness

Validates AGW's `is_turn_boundary` boolean — a per-request flag derived from
body shape ("latest message is a new user message, not a tool continuation").
Orthogonal to verdict_l: m validates the boundary *flag*, l validates the parent
*chain*. No redundancy.

**Oracle (positional).** The harness knows the true turn structure
(`_user_msg_turns`, `_audit_for_turn`). For each user-message turn window:

- the **first** `llm_request` in the window must have `is_turn_boundary: true`;
- every subsequent `llm_request` in that turn (tool-loop continuations) must
  have `is_turn_boundary: false`.

- `pass` — every turn's first run is a boundary and all continuations are not.
- `fail` — a turn's first run is not flagged, a mid-turn hop is flagged, or the
  boundary count ≠ the user-turn count; the reason names the offending
  run/turn.
- `na` — `is_turn_boundary` absent on all entries (no-governance / streaming /
  Responses / pre-Design-B trial).

A count-only check was rejected: it passes a misplaced boundary (right total,
wrong position), which defeats the verdict.

## Registration

Add to `compute_verdicts`:

```python
"l": verdict_l_run_lineage_integrity(trial),
"m": verdict_m_turn_boundary_correctness(trial),
```

The dashboard iterates the verdict map, so new letters should surface
automatically. The implementation plan will verify this; if the frontend
hard-codes letters, a one-line map addition is the fix (this is a verdict-render
tweak, not C2's lineage visualization).

## Testing

Synthetic trial fixtures in `tests/test_efficacy.py`, mirroring the existing
verdict tests (construct `Trial` with crafted `AuditEntry` lists carrying
`body` dicts).

verdict_l:
- clean 2-turn chain, every parent immediate → `pass` (lenient and strict)
- skip/truncation (parent = grandparent) → `pass` + "skips" note (lenient);
  `fail` under `strict=True`
- null `parent_rid` on a non-genesis run, chain otherwise intact → `pass` +
  "gap" note (lenient); `fail` under `strict=True`
- orphan (parent_rid references an unseen rid) → `fail` (both modes)
- forward-reference → `fail` (both modes)
- genesis-only single run → `na` (nothing to chain)
- no rid data → `na`
- every non-genesis run null parent → `na` (no lineage recoverable)
- intact chain with `parent_rid_anomaly: true` on one run → `pass` + anomaly
  note

verdict_m:
- correct boundaries (first run of each turn flagged) → `pass`
- mid-turn hop flagged as boundary → `fail`
- turn-start run not flagged → `fail`
- boundary count ≠ turn count → `fail`
- `is_turn_boundary` absent → `na`

## Deferred (TODOs)

- **verdict_n — cross-API RID continuity.** The RID analog of verdict_k, with a
  rich A/B/C failure taxonomy, reusing verdict_k's route-walking machinery keyed
  on `rid`/`parent_rid`. Blocked on (a) trials that span an LLM/API switch
  within one run chain, and (b) Responses-API RID, which is itself unimplemented
  (the `previous_response_id` extraction TODO from CHG-26F's review — AGW
  reserves the priority-chain slot but does not yet populate it).
- **`strict_lineage` per-row config flag (option a).** Two halves: a `RowConfig`
  field + wiring it into `verdict_l(strict=…)` (verdict-side, a small C1-style
  follow-up), and a per-row toggle UI control (frontend-side). Scope with C2,
  since that is when the matrix/drawer UI work lands; decide the split then.

## Out of scope

- **C2 — frontend lineage visualization.** Rendering the `rid → parent_rid`
  chain (and `parent_rid_anomaly`) in the trial view. Its own design cycle after
  C1; consumes the same audit data, no harness dependency.

## Cross-references

- AGW canonical spec: `features/2026-04-19-governance-cidgar/spec.md` on
  `ibfork/docs` (RID sections §3.3, §4.6, §9.16-18, §10.2).
- Design B spec (producer side):
  `docs/superpowers/specs/2026-05-20-run-identity-design.md`.
- CHG-26F (RID handoff fix) — AGW `ibfork/feat/cidgar`, commit `c733818`.
- Existing verdict precedents: `verdict_c_continuity` (lean continuity — l's
  analog), `verdict_k_cross_api_continuity` (rich taxonomy — n's analog),
  `verdict_i_tools_list_correlation` (reads `body`).
