---
status: proposed
drafted: 2026-05-15
shipped_at: null
phase_dep: cross-phase
companion_docs:
  - features/2026-04-19-governance-cidgar/spec.md
  - change-ledger.md
  - per-feature-reimplementation-inventory.md
---

# Cidgar config cleanup + uniform opt-in + value-shape rename

**Date:** 2026-05-15
**Scope:** AGW (`crates/agentgateway/src/governance/`) + aiplay (`agw/config.yaml`).
**Goal:** Bring the cidgar governance config surface to a uniform CHG-247-style opt-in pattern, rename CID value prefix to free up the namespace for future correlators, promote shared algorithm names to top-level config, and replace the bool GAR-required flag with a ternary mode.
**Status:** Ready for implementation plan. Prerequisite for a separate run-identity (RID) design that is parked at `2026-05-15-run-identity-design-PARKED.md`.
**Back-compat:** None. Clean break — every change in this spec is a breaking change. Operators carrying live conversation history across the upgrade will see those conversations get fresh CIDs (functionally identical to spec §9.5 truncated-history behavior).

## Canonical doc location

This file lives in aiplay as the brainstorm artifact. The canonical home for the design + plan + companion brainstorm trail is on the **`ibfork/docs`** branch under:

```
features/2026-05-15-cidgar-config-cleanup/
├── README.md           # status frontmatter, scope summary, file index
├── design.md           # this content, transposed
├── plan.md             # implementation worklist (from writing-plans skill)
├── brainstorming.md    # decisions log from the 2026-05-13 → 2026-05-15 session
├── conversation-log.md # exchange-by-exchange record
└── memory-log.md       # parked items, deferred follow-ups
```

The feature folder is created at implementation-plan time; this aiplay-side file remains as the brainstorm output that drove it. Per `docs/CONTRIBUTING.md` on the `ibfork/docs` branch:

> "Approved feature delivery work → `features/<area>/<date>-<slug>/`"

The slug `cidgar-config-cleanup` follows the existing pattern (`governance-cidgar`, `mcp-session-version-and-echo`, etc.).

## Why now

Five existing surface concerns that compound when a second correlator (RID) lands:

1. CID values use `ib_<12 hex>` — the `ib_` prefix is the governance family marker, not a per-correlator discriminator. Adding RID with the same `ib_` prefix would make values indistinguishable; adding `ibr_` for RID leaves CID asymmetric forever. Renaming CID to `ibc_` now establishes the `ib<type>_` family pattern that future correlators (`ibr_`, `ibs_` if SS ever gets a prefix, `ibt_`/`ibm_` if turn/MCP-session ids land) can extend cleanly.
2. `_ib_cid` injection at f1 (inputSchema) and f3 PATH A (tool_use.input override) is unconditional. Every other governance feature added since the initial cidgar shipped (snapshot_correlation via CHG-245/246, text_marker/resource_block flipped to opt-in via CHG-247) follows uniform opt-in. The unconditional behavior is the last remaining inconsistency.
3. `gar.schema_required: bool` conflates two orthogonal decisions: whether to inject the field at all, and whether to mark it required. A ternary cleanly separates them and aligns the GAR feature with the uniform opt-in default.
4. `cid.generator: uuid4_12` is namespaced under the `cid` config block, but the algorithm conceptually applies to any random-source hex-shape correlator AGW mints (CID today, RID after the parked design lands). The location actively misleads.
5. SHA-256 is hard-coded at `log.rs:201` (`schema_hash`) and `cidgar.rs:73` (`compute_snapshot`). No operator-visible algorithm declaration. Promoting to a config knob mirrors `id_algorithm` symmetry and future-proofs the eventual BLAKE3 / hash-rotation discussion.

Channel toggle names also carry an asymmetry that's invisible today and will be glaring once RID toggles arrive: `text_marker` and `resource_block` implicitly mean "for CID" but have no explicit `_cid` suffix; the planned RID siblings would be `text_marker_rid` / `resource_block_rid`. Renaming now (during a breaking-change window already needed for the rest) avoids a second rename window when RID lands.

## Locked decisions

| Decision | Value |
|---|---|
| CID value shape | `ibc_<12 hex>` (was `ib_<12 hex>`) |
| Field name `_ib_cid` | **unchanged** — too invasive to rename, and no symmetric benefit |
| Marker namespace literal `ib:` | **unchanged** — `<!-- ib:cid=ibc_xxx -->` continues to use `ib:` as the marker grammar prefix; only the VALUE prefix changes |
| `schema_cid` toggle | new in `channels` block, default `false`, gates today's unconditional C1 CID injection (both f1 inputSchema and f3 PATH A overwrite) |
| `gar.schema_required: bool` | renamed to `gar.mode: required \| optional \| none`, default `none` |
| `cid.generator` | relocated and renamed to `governance.id_algorithm`, default `uuid4_12` |
| `hash_algorithm` | new field at `governance.hash_algorithm`, default `sha256`, applies to both `schema_hash` and `compute_snapshot` |
| `channels.text_marker` | renamed to `channels.text_marker_cid` |
| `channels.resource_block` | renamed to `channels.resource_block_cid` |
| `cid.header_passthrough` | unchanged — stays as `cid.header_passthrough: bool`, default `true` |
| `mcp_marker_kind` | unchanged — `resource \| text \| both`, default `resource` |
| `snapshot_correlation` | unchanged — default `false` per CHG-246 |
| Back-compat for any of the above | **none** |

## Target config shape (after this design lands)

```yaml
governance:
  id_algorithm:          uuid4_12     # NEW location (was cid.generator). Algorithm for
                                      # minting random-source hex-shape correlators.
                                      # Applies to CID today; RID after parked design.
                                      # Only variant: uuid4_12.
  hash_algorithm:        sha256       # NEW. Algorithm for schema_hash + compute_snapshot
                                      # (_ib_ss). Only variant: sha256.

cid:
  header_passthrough:    true         # UNCHANGED. Spec §15.5. CID-specific; stays here.

gar:
  mode:                  none         # RENAMED + RETYPED. Ternary: required | optional | none.
                                      # Default `none` for CHG-247 uniformity.

channels:
  mcp_marker_kind:       resource     # UNCHANGED.
  schema_cid:            false        # NEW. Gates C1 _ib_cid injection (f1 inputSchema +
                                      # f3 PATH A overwrite). CHG-247-style default.
  text_marker_cid:       false        # RENAMED (was text_marker). C2 carries cid.
  resource_block_cid:    false        # RENAMED (was resource_block). C3 carries cid.
  snapshot_correlation:  false        # UNCHANGED (CHG-246 default).
```

## Detailed changes

### Change 1 — CID value shape: `ib_<12 hex>` → `ibc_<12 hex>`

**Files:**
- `governance/cidgar.rs` — `generate_cid()` (or equivalent minting site): emit `ibc_<hex>` prefix.
- `governance/value_ops.rs` — `pop_cid_from_value`: parser must accept `ibc_<hex>` shape. Reject `ib_<hex>` shape (no back-compat).
- `governance/log.rs` — marker emission in `make_text_marker` (if shape-specific) updated to use `ibc_` prefix.
- All test fixtures across `governance/` — sweep `"ib_<12 hex>"` literals to `"ibc_<12 hex>"`. Mechanical.

**Marker grammar:** the `ib:cid=` literal in `<!-- ib:cid={value} -->` stays unchanged. Only the VALUE format changes. Example:
- Before: `<!-- ib:cid=ib_abc123def456 -->`
- After: `<!-- ib:cid=ibc_abc123def456 -->`

**Why `ibc_`:** preserves the `ib` family marker (greppable `^ib` matches all governance ids), introduces a single-char type discriminator (`c` for conversation), establishes the pattern for future correlators (`ibr_` for run, `ibs_` if SS ever gets a value prefix, `ibt_`/`ibm_` for turn/MCP-session if materialized). Costs 1 char per CID occurrence vs the original `ib_<hex>`.

### Change 2 — Add `channels.schema_cid` toggle (default `false`)

**Files:**
- `governance/config.rs::ChannelToggles` — add `pub schema_cid: bool` field with `#[serde(default)]`. Default impl: `false`.
- `governance/cidgar.rs::on_tools_list_resp` (f1) — gate `_ib_cid` injection into `inputSchema.properties` behind `self.cfg.channels.schema_cid`.
- `governance/cidgar.rs::on_llm_response` PATH A (f3) — gate `tool_use.input._ib_cid` overwrite behind `self.cfg.channels.schema_cid`.

Effect when `schema_cid: false`:
- `_ib_cid` field does NOT appear in inputSchema's `properties`
- `_ib_cid` field does NOT appear in tool_use.input on f3 PATH A
- `pop_cid_from_value` at f4 finds no `_ib_cid` to pop — `tool_call` audit's `cid` field comes from `ctx.cid` (resolved via other channels at f2), not from arg extraction
- CID still resolvable via C2 (text_marker_cid) and C3 (resource_block_cid) carriers if those are on

**Why default `false`:** uniform CHG-247 opt-in.

### Change 3 — Promote `cid.generator` to `governance.id_algorithm`

**Files:**
- `governance/config.rs::CidGarConfig` — add `pub id_algorithm: IdAlgorithm` field (or similar name). Move the existing `CidGenerator` enum out of `CidConfig`, rename to `IdAlgorithm`, keep single variant `Uuid4_12`.
- `governance/config.rs::CidConfig` — remove the `generator` field; the block now only contains `header_passthrough`.
- All call sites: read `self.cfg.id_algorithm` instead of `self.cfg.cid.generator`.

**Why:** algorithm conceptually applies to any random-source hex correlator AGW mints. Today only CID; once the parked RID design lands, RID will use the same field. Promoting to top-level removes the misleading `cid.` namespace.

### Change 4 — Add `governance.hash_algorithm: sha256`

**Files:**
- `governance/config.rs::CidGarConfig` — add `pub hash_algorithm: HashAlgorithm` field. New enum `HashAlgorithm` with single variant `Sha256`.
- `governance/log.rs::schema_hash` — read `self.cfg.hash_algorithm` (passed via context or thread-through) to select algorithm. Today's hard-coded `Sha256::digest` becomes a match on the enum.
- `governance/cidgar.rs::compute_snapshot` — same pattern.

**Why:** documents the algorithm choice operator-visibly, establishes the symmetry with `id_algorithm`, future-proofs for BLAKE3 / hash rotation discussions (the comment "BLAKE3 is an option for the future if hash-cost on the f1 path becomes a bottleneck" in `cidgar.rs:62` becomes a concrete extension point — add `Blake3` variant when needed).

### Change 5 — Rename `gar.schema_required: bool` → `gar.mode: GarMode` (ternary)

**Files:**
- `governance/config.rs::GarConfig` — remove `pub schema_required: bool`. Add `pub mode: GarMode`. New enum `GarMode { Required, Optional, None }` with `#[serde(rename_all = "snake_case")]`. Default impl: `None`.
- `governance/gar.rs::inject_governance_into_schema` — branch on `self.cfg.gar.mode`:
  - `Required`: inject `_ib_gar` into `properties` AND append to `required` array (today's `schema_required: true` behavior).
  - `Optional`: inject `_ib_gar` into `properties`; do NOT touch `required` (today's `schema_required: false` behavior).
  - `None`: skip injection entirely. Schema does not contain `_ib_gar`.
- `governance/cidgar.rs::on_tool_call_req` (f4) — `_ib_gar` extraction logic unchanged; when `mode: none`, the field is absent in args and `pop_gar_from_value` returns None as it does today on a missing field. Audit's `gar` field is null.

**Why `none` default:** strictest CHG-247 opt-in. Operators not actively consuming GAR pay zero token cost. Operators wanting audit reasoning explicitly set `required` (full enforcement) or `optional` (best-effort).

### Change 6 — Rename channel toggles for symmetry

**Files:**
- `governance/config.rs::ChannelToggles`:
  - `text_marker: bool` → `text_marker_cid: bool`
  - `resource_block: bool` → `resource_block_cid: bool`
- `governance/cidgar.rs` — every `self.cfg.channels.text_marker` → `text_marker_cid`; `resource_block` → `resource_block_cid`.
- All test fixtures sweeping the same two field names.

**Why:** symmetric naming with the (parked) `text_marker_rid` / `resource_block_rid` siblings. No semantic change — same gating behavior, just renamed.

### Change 7 — Aiplay companion config update

**File:** `agw/config.yaml` in aiplay repo.

For every governance route, set:

```yaml
governance:
  id_algorithm:          uuid4_12     # explicit even though default — documents intent
  hash_algorithm:        sha256       # same
cid:
  header_passthrough:    true         # unchanged
gar:
  mode:                  required     # aiplay wants full audit reasoning
channels:
  mcp_marker_kind:       both         # maximum survival; matches existing aiplay pattern
  schema_cid:            true
  text_marker_cid:       true
  resource_block_cid:    true
  snapshot_correlation:  true
```

The aiplay-side coordination comment at the top of `routes:` (the one explaining the channels block) updates to reflect the renamed fields. Approximate scope: 10 governance routes × the channels block; mechanical sweep.

## Validation warnings (new — small AGW addition)

`governance::config::validate` (or `validate.rs`) emits structured `tracing::warn!` calls at config-load when these patterns appear. Warnings only — do not block startup.

| Pattern | Warning |
|---|---|
| `schema_cid: false` AND `text_marker_cid: false` AND `resource_block_cid: false` AND `snapshot_correlation: false` | "All governance carriers disabled. AGW will mint a fresh CID on every LLM request (spec §9.5 truncation case). Effectively, audit will not correlate across turns. Confirm intentional." |
| `gar.mode: none` AND any channel toggle is `true` | "`gar.mode: none` disables LLM audit reasoning. Tool-call audits will have null GAR. Set `gar.mode: optional` (best-effort) or `required` (strict) for audit value." |
| `schema_cid: true` AND `text_marker_cid: false` AND `resource_block_cid: false` | "CID injected into MCP tool args but no inter-LLM-request propagation channel enabled. CID will survive across tool-call turns but be lost across pure-text turns. Enable `text_marker_cid` and/or `resource_block_cid`." |

Throttling: emit once per route on first config-load; subsequent reloads of the same config don't re-warn. Same pattern as CHG-243's `RAW_FALLBACK_FIRST_SEEN`.

**Implementation:** new file `governance/validate.rs` (or extend `config.rs`); ~50 LOC + 5 tests; called from the same site that constructs `CidGarConfig` from the deserialized YAML.

## Migration / sequencing

No back-compat means a single atomic landing per repo:

1. **AGW side (single PR or commit chain):**
   - Rust source updates (changes 1-6)
   - Test fixtures swept end-to-end
   - Spec language updated (§3.1 CID format, §4.1 birth, §5 flows, §13 test scenarios, §14 add new CHG entries)
   - Change-ledger entries added (CHG-25A through CHG-25F or whatever the actual numbers are)

2. **Aiplay side (companion PR, lands AFTER AGW image tag bumps):**
   - `agw/config.yaml` updated (change 7)
   - Any aiplay test fixtures referencing `ib_<hex>` literals updated to `ibc_<hex>`
   - Aiplay's verdict code (`harness/efficacy.py`) that uses regex like `ib_[0-9a-f]{12}` updated to `ibc_[0-9a-f]{12}`
   - Frontend (`frontend/trial.js`, etc.) — same regex sweep if any
   - Compose: bump `image:` tag to the new AGW build

3. **In-flight conversations:** any conversation whose history contains `ib_<hex>` markers at upgrade time will not have a recoverable CID after upgrade. AGW will mint a fresh CID at f2 (per §9.5 truncation case). Acceptable trade for the clean break; operators with critical in-flight state should drain before upgrading.

## Testing

### AGW Rust tests (per change)

| Change | New / updated tests |
|---|---|
| 1 — CID rename | Existing `cidgar::tests` fixtures all swept; add `test_generate_cid_emits_ibc_prefix` pinning the new shape; add `test_pop_cid_rejects_legacy_ib_prefix_no_back_compat`. |
| 2 — `schema_cid` toggle | `test_schema_cid_default_off_skips_injection`, `test_schema_cid_explicit_on_injects`, `test_schema_cid_off_still_extracts_cid_via_c2`, `test_schema_cid_off_still_extracts_cid_via_c3`. |
| 3 — `id_algorithm` promotion | `test_id_algorithm_defaults_to_uuid4_12`, `test_id_algorithm_explicit_round_trips_through_serde`. |
| 4 — `hash_algorithm` addition | `test_hash_algorithm_defaults_to_sha256`, `test_schema_hash_uses_configured_algorithm`, `test_compute_snapshot_uses_configured_algorithm`. |
| 5 — `gar.mode` ternary | `test_gar_mode_required_adds_to_required_array`, `test_gar_mode_optional_omits_from_required`, `test_gar_mode_none_skips_injection_entirely`, `test_gar_mode_default_is_none`. |
| 6 — channel toggle renames | Existing tests using `text_marker` / `resource_block` updated to `text_marker_cid` / `resource_block_cid`. |
| Validation warnings | `test_validate_warns_when_all_carriers_disabled`, `test_validate_warns_when_gar_none_but_channels_on`, `test_validate_warns_when_schema_cid_isolated`. |

### Aiplay tests

- `harness/efficacy.py` — any regex-based CID match updated; existing efficacy tests pass against new shape fixtures.
- `tests/test_audit_tail.py` — body extraction tests use `ibc_<hex>` fixtures.
- `tests/test_runner.py` — any header / matcher referencing `ib_<hex>` updated.
- Frontend smoke (`make up` + click through trial detail) — visual confirmation of CID rendering in the new shape across all tabs (Turns, Verdicts, CID flow, Services, Raw JSON).

### Test count delta estimate

AGW: ~12-15 new tests + ~30-40 fixture updates. Aiplay: ~5-10 fixture updates, no new tests.

## Out of scope (deferred to parked Design B)

- RID infrastructure: value shape (`ibr_<12 hex>`), `_ib_rid` field, new audit phase fields (`rid`, `parent_rid`, `is_turn_boundary`, `parent_run_rid`, `provider_response_id`).
- New channel toggles: `schema_rid`, `text_marker_rid`, `resource_block_rid`.
- Combined-carrier marker grammar: `<!-- ib:cid=...,rid=... -->`.
- Per-API f2 parent_rid resolution priority chain.
- Aiplay verdicts for run-lineage correctness.

All deferred to the parked design at `2026-05-15-run-identity-design-PARKED.md`. Resume after this design lands.

## Cross-references

### AGW spec sections (`features/2026-04-19-governance-cidgar/spec.md` on `ibfork/docs`)

Sections that will update:

| § | What changes |
|---|---|
| §3.1 `_ib_cid` — Conversation ID | Value shape becomes `ibc_<12 hex>` (was `ib_<12 hex>`); description updated. |
| §3.2 `_ib_gar` — Governance Audit Reasoning | Description references ternary `gar.mode: required \| optional \| none`; default `none`. The "GAR is required" framing softened — required only when operator opts in via `gar.mode: required`. |
| §3.3 `_ib_rid` | **Reserved** for the parked RID design; not added in this design. |
| §4.1 Birth | CID minting uses `governance.id_algorithm` (was `cid.generator`). |
| §4.2 Persistence Channels | Channel 2 references `text_marker_cid` toggle (renamed from `text_marker`); Channel 3 references `resource_block_cid` (renamed from `resource_block`); Channel 1 introduces `schema_cid` gating. |
| §5.1 f1 — `tools/list` Response Interception | `_ib_cid` injection conditional on `schema_cid: true`; `_ib_gar` injection conditional on `gar.mode != none`. |
| §5.3 f3/f7 — LLM Response Interception (PATH A) | `tool_use.input._ib_cid` overwrite conditional on `schema_cid: true`. |
| §7.1 Tool Schema | LLM sees governance fields conditional on per-route toggles; no longer "always present". |
| §12.5 Hash Function for Schema Hash | References `governance.hash_algorithm` config; algorithm choice operator-visible. |
| §14.5 — new section | "Cidgar config cleanup + uniform opt-in (CHG-25A through CHG-25F)" — describes this design's shipped state once landed. Mirrors §14.4's CHG-247 style. |

### Change-ledger entries (`change-ledger.md` at `ibfork/docs` root)

One CHG-NNN row per change in this design, with `[A]/[M]/[E]/[D]` type codes per the ledger's convention:

| CHG | Type | What |
|---|---|---|
| 25A | [M] | CID value shape rename `ib_<12hex>` → `ibc_<12hex>`; extractor accepts new shape only. |
| 25B | [E] | Add `channels.schema_cid: bool` toggle (default `false`); gate today's unconditional C1 CID injection (f1 + f3 PATH A). |
| 25C | [M] | Relocate + rename `cid.generator` → `governance.id_algorithm`. |
| 25D | [E] | Add `governance.hash_algorithm: sha256` field; promote SHA-256 hard-coded calls in `log.rs::schema_hash` and `cidgar.rs::compute_snapshot`. |
| 25E | [M] | Replace `gar.schema_required: bool` with `gar.mode: GarMode` ternary; default `none`. |
| 25F | [M] | Rename `channels.text_marker` → `text_marker_cid` and `channels.resource_block` → `resource_block_cid`. |
| 25G | [A] | New file `governance/validate.rs`; emit `tracing::warn!` on config-load for the three asymmetric-toggle patterns. |

Bundled commits OK in implementation plan; the ledger entries stay one-per-CHG for traceability.

### Per-feature reimplementation inventory (`per-feature-reimplementation-inventory.md` at `ibfork/docs` root)

Add a section under `governance-cidgar` describing the post-cleanup config surface (recipe-grade spec for fresh reimplementation if the branch is ever reset). Sweeping update — not a new entry, since cidgar is already inventoried; just keep the inventory in sync with the new field names and defaults.

### Aiplay-side artifacts

- `docs/enhancements.md` (aiplay) — this design supersedes the "Status: future" labels on any cidgar-side cleanup items parked there; sweep the listed items as `shipped` once the implementation lands.
- `agw/config.yaml` (aiplay) — companion change 7 in this design's scope.
- `harness/efficacy.py`, `tests/`, `frontend/` (aiplay) — sweep any `ib_[0-9a-f]{12}` regex / fixture literal to `ibc_[0-9a-f]{12}`.

### Parked sibling design

`docs/superpowers/specs/2026-05-15-run-identity-design-PARKED.md` (aiplay brainstorm artifact). Once this design lands, the parked design's canonical home becomes `features/<resumption-date>-run-identity/design.md` on `ibfork/docs`.

### Branch / worktree mapping for implementation

| Repo / worktree path | Branch | What lands here |
|---|---|---|
| `/home/nixusr/ws/agw-gh/.worktrees/cidgar` | `ibfork/feat/cidgar` (or a new sibling `ibfork/feat/cidgar-config-cleanup` if cleaner) | Rust source: `governance/{config,cidgar,value_ops,log,gar,validate}.rs` and matching test fixtures |
| `/home/nixusr/ws/agw-gh/.worktrees/docs-v2` | `ibfork/docs` | New `features/2026-05-15-cidgar-config-cleanup/{README,design,plan,brainstorming}.md`; updates to `features/2026-04-19-governance-cidgar/spec.md` (the cidgar spec); new rows in `change-ledger.md`; sweep `per-feature-reimplementation-inventory.md` |
| `/home/nixusr/ws/aiplay` | `main` | Companion config + sweeps (Change 7); bump AGW image tag in `docker-compose.yaml` once the AGW build is published |
