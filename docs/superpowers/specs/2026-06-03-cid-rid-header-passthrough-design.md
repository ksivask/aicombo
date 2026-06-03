# Structured `X-IB-CID` / `X-IB-RID` header passthrough — design

**Date:** 2026-06-03
**Status:** Approved (brainstorming) — pending implementation plan
**Repos:** feature lands in **agw-gh / cidgar** (Rust AGW); harness coverage in **aiplay**
**Spec impact:** promotes AGW spec §14.5/§15.5 (CID header passthrough) and §15.6 (`X-IB-RID` header passthrough, previously deferred) to active behavior.

## Summary

Extend the AGW's `X-IB-CID` header support and add a new `X-IB-RID` header, both accepting a shared structured `key=value` grammar in addition to the existing bare form:

```
X-IB-CID: ibc_0lrywb7kmpxk5u82
X-IB-CID: conv_id=ibc_0lrywb7kmpxk5u82,tid=tenant_xyz,...
X-IB-RID: ibr_0lrywb7kmpxk5u82
X-IB-RID: run_id=ibr_0lrywb7kmpxk5u82,prun_id=ibr_9a8b7c6d5e4f,...
```

`conv_id` resolves the CID (as today, but now extractable from a richer header). `run_id` lets an instrumented agent assert the current run's identity, overriding f2's mint. `prun_id` supplies the parent run id. All three headers parse unknown keys into a generic bag carried on `GovContext` and emitted to audit, but only `conv_id`/`run_id`/`prun_id` change behavior in this version.

## Motivation

Today (`governance/cid.rs:21`) the entire `X-IB-CID` value is passed straight to `Cid::parse`, which strictly validates `^ibc_[a-f0-9]{12}$`. Any structured value fails the length check and is silently ignored. RID has no header path at all — f2 mints the current RID unconditionally (`cidgar.rs:281`) and resolves `parent_rid` heuristically from a message-history scan.

An instrumented-agent consumer (the §15.6 trigger) often already knows the conversation id, its own run id, and the parent run id, and wants to assert them out-of-band rather than rely on in-band markers or AGW generation. A structured header carries that intent cleanly and symmetrically across both id namespaces.

## Shared bag grammar

One parser serves both headers. Differences are only which keys are *well-known* (active) per header.

**Form selection.** If the header value contains `=`, parse as structured; otherwise treat the whole (trimmed) value as the bare primary key for that header (`conv_id` for `X-IB-CID`, `run_id` for `X-IB-RID`). Bare ids are hex-only and never contain `=`, so the split is unambiguous.

**Structured form.** Comma-separated `key=value` pairs:
- Keys: `^[a-z0-9_]+$`, lowercase.
- Split each pair on the **first** `=`. Commas are reserved separators — no embedded commas, no quoting.
- Optional whitespace (OWS) trimmed around each key and value.
- Duplicate keys: last-wins.

**Bounds** (the header is attacker-controllable at the edge):
- ≤ 8 pairs per header.
- key ≤ 32 chars; value ≤ 128 chars.
- value charset `^[A-Za-z0-9_.:-]+$`.
- Pairs violating a bound (count, length, charset, key shape) are **dropped and logged**, never fatal — a malformed pair does not void the whole header.

**Carry + audit.** Every accepted pair (well-known or not) is stored in an ordered bag on `GovContext` and emitted in the audit record. Only well-known keys additionally drive behavior.

## `X-IB-CID` behavior

Gated by `cid.header_passthrough` (see Config). When the gate is off, the entire header — including the bag — is ignored.

- `conv_id` → validated via `Cid::parse`. Feeds the existing CID priority chain: **header `conv_id` → message-history scan → generate** (`resolve_cid`, `cid.rs:20`).
- Malformed `conv_id` (present but fails `Cid::parse`): **lenient** — `conv_id` is treated as absent (fall through to scan/generate); the rest of the bag (e.g. `tid`) is still parsed, carried, and audited.
- `tid` and all other keys: **carried-only** — parsed, bounds-checked, bagged, audited. Nothing branches on them in this version. `tid` is a reserved/recognized name, not an active tenant-isolation input. (Multi-tenant CID isolation remains keyed on `backend_name` per §13.4.)

## `X-IB-RID` behavior (new)

Gated by `rid.header_passthrough` (new config; see Config). When off, the entire header — including the bag — is ignored.

- `run_id` → validated via `Rid::parse`. When present and valid, f2 **adopts it as the current RID instead of minting** — i.e. the previously-unconditional mint (`cidgar.rs:281`) becomes "mint only when no valid `run_id` is supplied." Parent-rid resolution still runs unchanged.
  - Malformed `run_id`: lenient — mint a fresh RID as today; keep the rest of the bag.
- `prun_id` → validated via `Rid::parse`. Supplies the parent run id as a **winning source that is still observed**:
  - It wins over scanned candidates and becomes `ctx.parent_rid`.
  - It is recorded in `parent_rid_sources` tagged `source=header`.
  - If the message-history scan yields a different candidate, `parent_rid_anomaly` still fires (the client's assertion wins, but a lineage mismatch is never hidden).
  - Malformed `prun_id`: lenient — fall back to the normal scan resolver; keep the rest of the bag.
- Both keys optional and independent: `run_id` alone adopts current RID with scan-resolved parent; `prun_id` alone mints a fresh current RID with header-asserted parent.
- Edge — `run_id == prun_id` (a run cannot be its own parent): accept both but flag via `parent_rid_anomaly` (self-parent). No hard reject.
- Other/unknown keys: carried-only (bagged + audited).

## Config

```rust
// governance/config.rs
pub struct CidConfig { pub header_passthrough: bool, /* ... */ }   // default flips: true -> false
pub struct RidConfig { pub header_passthrough: bool }               // new; default false
// CidGarConfig gains: pub rid: RidConfig
```

Both `cid.header_passthrough` and `rid.header_passthrough` default **`false`**. Each independently gates its whole header (id extraction **and** bag parse/carry/audit).

### Backward-compatibility note (migration)

This **flips the current `cid.header_passthrough` default**, which is `true` today (`config.rs:39`). Consequences:
- Deployments relying on the default to receive `X-IB-CID` passthrough will lose it until they set `cid.header_passthrough: true` explicitly.
- Configs that already set `header_passthrough: true` are unaffected.
- The existing default-asserting test (`config.rs:316`, `assert!(c.cid.header_passthrough)`) and the YAML examples that set `true` (`config.rs:270/344/382`) must be reviewed: the unit asserting the *default* must be updated to expect `false`; examples that explicitly set `true` stay valid.

## Data model

`GovContext` (governance/types.rs) gains:
- `header_bag: Vec<(String, String)>` (or equivalent ordered map) — the parsed pairs, populated at the hook site before f2.
- existing `cid`, `rid`, `parent_rid`, `parent_rid_sources`, `parent_rid_anomaly` reused; `parent_rid_sources` gains the `header` source variant.

`Rid::parse` already exists (`types.rs:88`, `^ibr_[a-f0-9]{12}$`) and is reused unchanged.

## Hook-site wiring (`llm/mod.rs`)

Mirror the existing `X-IB-CID` pre-population (`llm/mod.rs:771-781`):
- Parse `X-IB-CID` via the shared bag parser when `cid.header_passthrough`; set `gov_ctx.cid` from `conv_id`, store the bag.
- Parse `X-IB-RID` via the shared bag parser when `rid.header_passthrough`; set `gov_ctx.rid` from `run_id` and seed the header `prun_id` for f2's parent resolution; merge the bag.
- f2 (`cidgar.rs:280-343`): mint current RID only if `gov_ctx.rid` is unset; fold a header `prun_id` into parent_rid resolution as the winning, recorded source.

## Audit schema

The audit record (the f2/log path around `cidgar.rs:361-372`, `governance/log.rs`) gains:
- `header_bag` (the carried key-values, redaction policy = same as other request-derived fields).
- `rid_source` / `cid_source` indicators (`header` | `scan` | `generated`/`minted`) so a client-asserted id is distinguishable from an AGW-derived one in the audit trail.

## Edge cases

| # | Case | Behavior |
|---|------|----------|
| H1 | Bare `X-IB-CID: ibc_…` / `X-IB-RID: ibr_…` | Unchanged / new bare path → primary key. |
| H2 | Structured with malformed `conv_id` | Ignore conv_id, fall through; keep bag. |
| H3 | Structured with malformed `run_id` | Mint fresh RID; keep bag. |
| H4 | Malformed `prun_id` | Scan-resolve parent; keep bag. |
| H5 | `prun_id` disagrees with scan | Header wins, recorded as `source=header`, `parent_rid_anomaly` fires. |
| H6 | `run_id == prun_id` | Accept; `parent_rid_anomaly` (self-parent). |
| H7 | Over-limit / bad-charset pair | Drop that pair, log; rest of header stands. |
| H8 | Duplicate key | Last-wins. |
| H9 | Gate off (`header_passthrough=false`) | Whole header ignored (no id, no bag). |
| H10 | No `=`, value not a valid id (e.g. `X-IB-CID: garbage`) | Bare-parse fails → treated as absent; fall through. |

## Implementation targets

**agw-gh / cidgar (Rust):**
- `governance/cid.rs` — shared bag parser; `resolve_cid` unchanged signature (still header→scan→generate, header now sourced from parsed `conv_id`).
- `governance/types.rs` — `GovContext.header_bag`; `parent_rid_sources` header variant.
- `governance/config.rs` — `CidConfig` default → false; new `RidConfig`; wire into `CidGarConfig`.
- `governance/cidgar.rs` (f2) — conditional mint; header `prun_id` as winning+observed parent source.
- `governance/log.rs` — audit fields (`header_bag`, `*_source`).
- `llm/mod.rs` — hook-site parse + pre-populate for both headers.
- AGW spec `features/2026-04-19-governance-cidgar/spec.md` — §14.5/§15.5 active, §15.6 active, grammar + edge cases, config defaults.

**aiplay (harness):**
- Adapter/efficacy coverage exercising both headers end-to-end (bare + structured, gate on/off, malformed, anomaly).

## Out of scope (future)

- `tid` driving tenant-scoped CID isolation (kept carried-only here).
- Active behavior for any non-`conv_id`/`run_id`/`prun_id` keys.
- Quoting / embedded-comma values in the grammar.
