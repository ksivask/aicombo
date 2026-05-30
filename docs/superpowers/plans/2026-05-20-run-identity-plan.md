# Run Identity (RID) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a second governance correlator — `_ib_rid` (LLM-run identifier) — that sits below CID in the conversation hierarchy, enabling per-run audit lineage and turn-boundary detection for the audit / forensic-replay use case.

**Architecture:** AGW mints an `ibr_<12 hex>` RID at f2 for every LLM call. Three new payload-toggle channels (`schema_rid`, `text_marker_rid`, `resource_block_rid`) extend the existing CID carriers (C1 in tool_use.input, C2 in terminal text, C3 in MCP tool/call response) to carry RID alongside CID. The marker grammar evolves from single-capture `<!-- ib:cid=X -->` to combined `<!-- ib:cid=X,rid=Y -->`. At f2, `parent_rid` resolves via a priority chain (previous_response_id → C1 → C3 → C2 → null), and `is_turn_boundary` derives from body shape. Audit phases gain `rid` / `parent_rid` / `is_turn_boundary` / `parent_run_rid` / `provider_response_id` / `parent_rid_sources` / `parent_rid_anomaly` fields.

**Tech Stack:** Rust 2021 (AGW); `serde`/`serde_yaml`/`serde_json`; `regex` for marker parsing; `uuid` for RID minting; pytest (aiplay companion); docker-compose stack.

**Branch decision:** Implementation lands on `ibfork/feat/cidgar` (continuing Design A's branch) rather than a sibling. Rationale: Design B builds directly on Design A's symbols (`ChannelToggles`, `inject_governance_into_schema(schema, mode, schema_cid)`, `governance/validate.rs`, etc.); a sibling would either duplicate or merge later. Atomic shipping is simpler. If branch-shape preferences differ at PR-prep time, the plan can be rebased onto a fresh `ibfork/feat/cidgar-rid` without scope change.

---

## File Structure

### AGW source files (`/home/nixusr/ws/agw-gh/.worktrees/cidgar/crates/agentgateway/src/governance/`)

| File | Current responsibility | What this design adds |
|---|---|---|
| `types.rs` | `Cid` type + `Channel` enum + `GovContext` | New `Rid` type (parallel to `Cid`); extend `Channel` enum if needed; extend `GovContext` with `rid`/`parent_rid`/`is_turn_boundary`/`parent_rid_sources`/`parent_rid_anomaly`/`provider_response_id` |
| `marker.rs` | C2 text-marker grammar (single-capture `<!-- ib:cid=X -->`) + `make_text_marker` + `strip_text_marker` | Evolve `MARKER_RE` to key=value pair walker. New helpers `make_combined_marker(payload)` + `strip_combined_marker(text) -> MarkerPayload`. Old single-correlator helpers stay as thin wrappers for backwards-compat with existing call sites. |
| `value_ops.rs` | `pop_cid_from_value`, `pop_gar_from_value`, `pop_ib_ss_from_value`, `inject_cid_into_value` | New `pop_ib_rid_from_value`, `pop_ib_rid_from_map`, `inject_rid_into_value` parallel to existing CID helpers. |
| `config.rs` | `CidGarConfig`, `ChannelToggles` (schema_cid, text_marker_cid, resource_block_cid, snapshot_correlation, mcp_marker_kind), `GarMode`, `IdAlgorithm`, `HashAlgorithm` | Add 3 toggles to `ChannelToggles`: `schema_rid`, `text_marker_rid`, `resource_block_rid` (all default `false`). |
| `gar.rs` | `inject_governance_into_schema(schema, mode, schema_cid)` | Extend signature to `(schema, mode, schema_cid, schema_rid)` for parallel RID injection. |
| `messages_shape.rs` | Anthropic Messages API shape: `clean_and_scan_request`, `inject_cid_into_tool_use_response`, `append_text_marker_response`, etc. | Parallel `_rid` helpers: `inject_rid_into_tool_use_response`, `extract_rid_from_messages` (parent_rid resolution scan), `append_combined_marker_response`. |
| `completions_shape.rs` | OpenAI chat completions shape: parallel helpers to messages_shape | Parallel `_rid` helpers as in messages_shape. |
| `cidgar.rs` | `CidGarPipeline` with f1/f2/f3/f4/f5 hooks | f1 add `_ib_rid` schema injection (gated by `schema_rid`); f2 add RID minting + `parent_rid` resolution + `is_turn_boundary` derivation; f3 add RID-aware injection (PATH A `_ib_rid` overwrite gated by `schema_rid`; PATH B combined marker); f4 extract `_ib_rid` for `parent_run_rid` audit field; f5 add RID payload to C3 emission. |
| `log.rs` | `Phase` enum (ToolsList, LlmRequest, ToolPlanned, Terminal, ToolCall, ToolResponse) + `LogEntry` + `schema_hash` | Extend `LlmRequest` with `rid` / `parent_rid` / `is_turn_boundary` / `parent_rid_sources` / `parent_rid_anomaly`. Add new `LlmResponse` phase variant (currently the Terminal/ToolPlanned audits cover f3; we add a dedicated LlmResponse for `rid` + `provider_response_id`). Extend `ToolCall` and `ToolResponse` with `parent_run_rid`. |
| `validate.rs` | 3 warnings: all-CID-channels-off / gar-none-with-channels-on / schema-cid-isolated | Add 3 RID-mirror warnings + 1 cross-correlator warning (RID enabled without any CID propagation). |

### AGW docs files (`/home/nixusr/ws/agw-gh/.worktrees/docs-v2/docs/`)

| File | Change |
|---|---|
| `features/2026-04-19-governance-cidgar/spec.md` | FILL §3.3 (reserved by Design A for `_ib_rid`); add §4.6 (RID lifecycle); extend §5.1/§5.3/§5.4/§5.5 with RID-specific behavior; add §9.x new edge cases (conv-mode parent_rid gap, multi-source disagreement, parent_rid_anomaly); extend §10 audit-phase schema; add §14.x new uniform-opt-in entry for RID toggles; add §15 future-consideration for `X-IB-RID` header passthrough. |
| `change-ledger.md` | Append CHG-26A..E rows. |
| `per-feature-reimplementation-inventory.md` | Extend `governance-cidgar` section with RID infrastructure recipe. |
| `features/2026-05-20-run-identity/README.md` | New feature folder README with status frontmatter. |
| `features/2026-05-20-run-identity/design.md` | Transposed from `2026-05-20-run-identity-design.md` (drop preamble that calls it a brainstorm artifact). |
| `features/2026-05-20-run-identity/plan.md` | Transposed from this plan. |
| `features/2026-05-20-run-identity/brainstorming.md` | Distilled decision log: combined-carrier vs parallel, ibr_<12hex> choice, conv-mode gap acceptance, no-back-compat (just like Design A). |

### Aiplay companion files (`/home/nixusr/ws/aiplay/`)

| File | Change |
|---|---|
| `agw/config.yaml` | Add `schema_rid: true`, `text_marker_rid: true`, `resource_block_rid: true` to every governance route's `channels:` block (10 routes). |
| `docker-compose.yaml` | Bump AGW image tag when the new image with CHG-26A..E is published. |
| `docs/enhancements.md` | Sweep any "Status: future" entries now subsumed by Design B (likely none beyond what was already swept in Design A's Task 15, but verify). |

---

## Phase 1 — AGW Rust source changes (`ibfork/feat/cidgar` branch)

**Working directory:** `/home/nixusr/ws/agw-gh/.worktrees/cidgar`

**Test command:** `cargo test -p agentgateway --lib governance::`

**Ordering rationale:** foundational types first (Rid type + value_ops helpers), then carrier mechanics (marker grammar + shape-helper additions), then pipeline hooks (f1/f2/f3/f4/f5), then audit shape, then validate.rs. Each task ends with a commit; tasks compose into one PR on `ibfork/feat/cidgar`.

---

### Task 1: CHG-26A — `Rid` type + `value_ops` helpers

**Files:**
- Modify: `crates/agentgateway/src/governance/types.rs`
- Modify: `crates/agentgateway/src/governance/value_ops.rs`

- [ ] **Step 1.1: Write failing test for `Rid::generate` shape**

Add to `types.rs::tests`:

```rust
#[test]
fn rid_generate_format_is_ibr_prefix_plus_12_hex() {
    let rid = Rid::generate();
    let s = rid.as_str();
    assert!(s.starts_with("ibr_"), "expected ibr_ prefix, got {s}");
    assert_eq!(s.len(), 16, "expected total length 16 (ibr_ + 12 hex), got {s}");
    assert!(s[4..].chars().all(|c| c.is_ascii_hexdigit()), "non-hex chars in {s}");
}

#[test]
fn rid_parse_accepts_valid() {
    assert!(Rid::parse("ibr_7f3a2b91c4d8").is_some());
}

#[test]
fn rid_parse_rejects_cid_prefix() {
    // ibc_ (CID family) must not parse as RID — disjoint namespaces.
    assert!(Rid::parse("ibc_7f3a2b91c4d8").is_none());
}

#[test]
fn rid_parse_rejects_wrong_length() {
    assert!(Rid::parse("ibr_7f3a").is_none());
    assert!(Rid::parse("ibr_7f3a2b91c4d800").is_none());
}

#[test]
fn rid_parse_rejects_non_hex() {
    assert!(Rid::parse("ibr_7f3a2b91c4dZ").is_none());
}

#[test]
fn rid_parse_rejects_uppercase_hex() {
    assert!(Rid::parse("ibr_7F3A2B91C4D8").is_none());
}

#[test]
fn rid_generate_produces_distinct_values() {
    let a = Rid::generate();
    let b = Rid::generate();
    assert_ne!(a.as_str(), b.as_str());
}
```

- [ ] **Step 1.2: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::types::tests::rid_
```

Expected: FAIL — `Rid` undefined.

- [ ] **Step 1.3: Add `Rid` type to `types.rs`**

Insert after the `Cid` impl block (~line 56):

```rust
/// Run ID — `ibr_` prefix + 12 lowercase hex chars (e.g. `ibr_7f3a2b91c4d8`).
/// Part of the `ib<type>_` naming family (`r` = run); minted once per LLM call
/// (CHG-26A). Disjoint from `Cid` namespace by prefix. Spec §3.3.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default)]
pub struct Rid(Strng);

impl Rid {
    /// Generate a fresh RID. Algorithm selected by `IdAlgorithm` (shared with
    /// Cid::generate_with per CHG-25C).
    pub fn generate_with(_algorithm: crate::governance::config::IdAlgorithm) -> Self {
        let uuid = uuid::Uuid::new_v4();
        let hex = uuid.simple().to_string();
        let s = format!("ibr_{}", &hex[..12]);
        Self(Strng::from(s))
    }

    /// Convenience: generate using the default algorithm (`Uuid4_12`).
    /// Production paths should prefer `generate_with(cfg.id_algorithm)`.
    pub fn generate() -> Self {
        Self::generate_with(crate::governance::config::IdAlgorithm::default())
    }

    /// Parse an RID string, validating `^ibr_[a-f0-9]{12}$`. Returns `None` on
    /// any structural failure — never panics. Only lowercase hex accepted for
    /// round-trip safety with `generate()`. Disjoint from `Cid::parse` by prefix.
    pub fn parse(s: &str) -> Option<Self> {
        if s.len() != 16 || !s.starts_with("ibr_") {
            return None;
        }
        if !s[4..]
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        {
            return None;
        }
        Some(Self(Strng::from(s)))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for Rid {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}
```

- [ ] **Step 1.4: Run, see pass**

```bash
cargo test -p agentgateway --lib governance::types::tests::rid_
```

Expected: all 7 new tests pass.

- [ ] **Step 1.5: Write failing tests for `value_ops` RID helpers**

Add to `value_ops.rs::tests` module:

```rust
#[test]
fn pop_ib_rid_from_value_removes_and_returns() {
    let mut v = json!({"_ib_rid": "ibr_7f3a2b91c4d8", "loc": "NYC"});
    let rid = pop_ib_rid_from_value(&mut v);
    assert_eq!(
        rid.map(|r| r.as_str().to_owned()),
        Some("ibr_7f3a2b91c4d8".into())
    );
    assert!(v.get("_ib_rid").is_none());
    assert!(v.get("loc").is_some());
}

#[test]
fn pop_ib_rid_from_value_returns_none_when_absent() {
    let mut v = json!({"loc": "NYC"});
    assert!(pop_ib_rid_from_value(&mut v).is_none());
}

#[test]
fn pop_ib_rid_from_value_drops_malformed_silently() {
    let mut v = json!({"_ib_rid": "not-a-rid", "loc": "NYC"});
    assert!(pop_ib_rid_from_value(&mut v).is_none());
    assert!(v.get("_ib_rid").is_none(),
        "malformed RID should still be stripped (§11.5 non-destructive)");
}

#[test]
fn pop_ib_rid_rejects_cid_shape() {
    // ibc_<hex> in _ib_rid field must NOT parse as RID — wrong namespace.
    let mut v = json!({"_ib_rid": "ibc_7f3a2b91c4d8"});
    assert!(pop_ib_rid_from_value(&mut v).is_none());
    assert!(v.get("_ib_rid").is_none(), "still stripped");
}

#[test]
fn inject_rid_into_value_overwrites_existing() {
    let mut v = json!({"_ib_rid": "ibr_01d000000000", "loc": "NYC"});
    let rid = Rid::parse("ibr_aec000000000").unwrap();
    inject_rid_into_value(&mut v, &rid);
    assert_eq!(
        v.get("_ib_rid").and_then(|v| v.as_str()),
        Some("ibr_aec000000000")
    );
}

#[test]
fn inject_rid_into_value_adds_new_when_absent() {
    let mut v = json!({"loc": "NYC"});
    let rid = Rid::parse("ibr_abc012345678").unwrap();
    inject_rid_into_value(&mut v, &rid);
    assert_eq!(
        v.get("_ib_rid").and_then(|v| v.as_str()),
        Some("ibr_abc012345678")
    );
}

#[test]
fn pop_ib_rid_from_map_removes_and_returns() {
    let mut m = serde_json::Map::new();
    m.insert("_ib_rid".into(), Value::String("ibr_7f3a2b91c4d8".into()));
    m.insert("loc".into(), Value::String("NYC".into()));
    let rid = pop_ib_rid_from_map(&mut m);
    assert_eq!(
        rid.map(|r| r.as_str().to_owned()),
        Some("ibr_7f3a2b91c4d8".into())
    );
    assert!(m.get("_ib_rid").is_none());
}
```

Also add `use crate::governance::types::Rid;` at the top of the file.

- [ ] **Step 1.6: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::value_ops::tests::pop_ib_rid
cargo test -p agentgateway --lib governance::value_ops::tests::inject_rid
```

Expected: FAIL — `pop_ib_rid_from_value`/`pop_ib_rid_from_map`/`inject_rid_into_value` undefined.

- [ ] **Step 1.7: Add RID helpers to `value_ops.rs`**

Add after `inject_cid_into_value`:

```rust
/// CHG-26A — pop `_ib_rid` from a JSON object, parsing into Rid.
/// Malformed values are still stripped but not returned (§11.5 non-destructive).
/// Parallel to `pop_cid_from_value`.
pub fn pop_ib_rid_from_value(v: &mut Value) -> Option<Rid> {
    let obj = v.as_object_mut()?;
    let raw = obj.remove("_ib_rid")?;
    raw.as_str().and_then(Rid::parse)
}

/// CHG-26A — map-native variant of `pop_ib_rid_from_value`. Parallel to
/// `pop_cid_from_map`. Operates directly on a `serde_json::Map` to avoid a
/// `Value::Object(map.clone())` round-trip at call sites where the args
/// already live as a `Map` (notably MCP tool-call arguments).
pub fn pop_ib_rid_from_map(map: &mut serde_json::Map<String, Value>) -> Option<Rid> {
    let raw = map.remove("_ib_rid")?;
    raw.as_str().and_then(Rid::parse)
}

/// CHG-26A — write `_ib_rid` into a JSON object, overwriting any existing value.
/// No-op on non-object values (§11.5 non-destructive). Parallel to `inject_cid_into_value`.
pub fn inject_rid_into_value(v: &mut Value, rid: &Rid) {
    if let Some(obj) = v.as_object_mut() {
        obj.insert("_ib_rid".into(), Value::String(rid.as_str().to_owned()));
    }
}
```

- [ ] **Step 1.8: Run, see pass**

```bash
cargo test -p agentgateway --lib governance::value_ops
cargo test -p agentgateway --lib governance::types
```

Expected: all tests pass. governance:: subset baseline grows by ~13 tests.

- [ ] **Step 1.9: Commit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/cidgar
git add crates/agentgateway/src/governance/{types,value_ops}.rs
git commit -m "feat(cidgar): CHG-26A add Rid type + value_ops RID helpers

Introduces the second governance correlator type, parallel to Cid:
  - Rid struct with ibr_<12 hex> value shape (spec §3.3)
  - Rid::generate_with(IdAlgorithm) + Rid::generate convenience
  - Rid::parse with strict validation (rejects ibc_ prefix, uppercase hex,
    wrong length)
  - pop_ib_rid_from_value / pop_ib_rid_from_map / inject_rid_into_value
    helpers parallel to existing CID helpers in value_ops.rs

13 new tests; zero behavior change to any existing pipeline path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: CHG-26B — Add 3 new RID payload toggles to `ChannelToggles`

**Files:**
- Modify: `crates/agentgateway/src/governance/config.rs`

- [ ] **Step 2.1: Write failing tests in `config.rs::tests`**

```rust
#[test]
fn yaml_rid_toggles_default_to_false() {
    let yaml = "kind: cid_gar";
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else { panic!("expected CidGar"); };
    assert!(!c.channels.schema_rid);
    assert!(!c.channels.text_marker_rid);
    assert!(!c.channels.resource_block_rid);
}

#[test]
fn yaml_rid_toggles_explicit_round_trip() {
    let yaml = r#"
kind: cid_gar
channels:
  schema_rid: true
  text_marker_rid: true
  resource_block_rid: true
"#;
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else { panic!("expected CidGar"); };
    assert!(c.channels.schema_rid);
    assert!(c.channels.text_marker_rid);
    assert!(c.channels.resource_block_rid);
}
```

- [ ] **Step 2.2: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::config::tests::yaml_rid_toggles
```

Expected: FAIL — `schema_rid` / `text_marker_rid` / `resource_block_rid` fields don't exist.

- [ ] **Step 2.3: Add 3 fields to `ChannelToggles`**

In `config.rs`, after `snapshot_correlation` field (line 155), add:

```rust
    /// CHG-26B Channel 1 (RID) — inject `_ib_rid` into every tool's
    /// `inputSchema.properties` at f1 AND overwrite `tool_use.input._ib_rid`
    /// at f3 PATH A. Symmetric with `schema_cid`. Default false (CHG-247
    /// uniform opt-in). When off, RID is neither advertised in the schema
    /// nor written into tool_use.input — but the f4 strip still runs
    /// unconditionally (defense-in-depth at the MCP boundary).
    pub schema_rid: bool,
    /// CHG-26B Channel 2 (RID) — include `rid=ibr_xxx` in the combined
    /// `<!-- ib:cid=ibc_xxx,rid=ibr_yyy -->` text marker appended to terminal
    /// LLM responses at f3 PATH B. Symmetric with `text_marker_cid`. Default
    /// false (CHG-247). When BOTH text_marker_cid AND text_marker_rid are off,
    /// no marker is emitted.
    pub text_marker_rid: bool,
    /// CHG-26B Channel 3 (RID) — include `rid` in the combined payload of MCP
    /// `tools/call` response markers (resource block or text content block,
    /// per `mcp_marker_kind`). Symmetric with `resource_block_cid`. Default
    /// false (CHG-247). Master gate `resource_block_cid` still controls
    /// whether ANY C3 emission happens; this toggle controls whether the
    /// emission includes RID payload.
    pub resource_block_rid: bool,
```

In `impl Default for ChannelToggles`, add three lines before the closing `}`:

```rust
            // CHG-26B: RID toggles default FALSE — Channel 1/2/3 RID payloads
            // are opt-in per CHG-247 pattern.
            schema_rid: false,
            text_marker_rid: false,
            resource_block_rid: false,
```

- [ ] **Step 2.4: Run, see pass**

```bash
cargo test -p agentgateway --lib governance::config
```

Expected: all tests pass; 2 new toggle tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add crates/agentgateway/src/governance/config.rs
git commit -m "feat(cidgar): CHG-26B add 3 RID channel toggles (default false)

Adds schema_rid / text_marker_rid / resource_block_rid to ChannelToggles,
parallel to the CID toggles introduced in CHG-25B/CHG-25F. All three
default to false per CHG-247 uniform opt-in pattern.

Six payload toggles total now (3 carriers x 2 correlators).
mcp_marker_kind continues to control C3 wire format for whatever payload
it carries.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: CHG-26C — Combined-carrier marker grammar evolution

**Files:**
- Modify: `crates/agentgateway/src/governance/marker.rs`

- [ ] **Step 3.1: Write failing tests in `marker.rs::tests`**

```rust
#[test]
fn parse_marker_payload_extracts_single_cid() {
    let s = "<!-- ib:cid=ibc_7f3a2b91c4d8 -->";
    let payload = parse_marker_payload(s);
    assert!(payload.is_some());
    let p = payload.unwrap();
    assert_eq!(
        p.cid.map(|c| c.as_str().to_owned()),
        Some("ibc_7f3a2b91c4d8".into())
    );
    assert!(p.rid.is_none());
}

#[test]
fn parse_marker_payload_extracts_single_rid() {
    let s = "<!-- ib:rid=ibr_7f3a2b91c4d8 -->";
    let payload = parse_marker_payload(s);
    assert!(payload.is_some());
    let p = payload.unwrap();
    assert!(p.cid.is_none());
    assert_eq!(
        p.rid.map(|r| r.as_str().to_owned()),
        Some("ibr_7f3a2b91c4d8".into())
    );
}

#[test]
fn parse_marker_payload_extracts_combined_cid_rid() {
    let s = "<!-- ib:cid=ibc_7f3a2b91c4d8,rid=ibr_aaaaaaaaaaaa -->";
    let payload = parse_marker_payload(s);
    assert!(payload.is_some());
    let p = payload.unwrap();
    assert_eq!(p.cid.unwrap().as_str(), "ibc_7f3a2b91c4d8");
    assert_eq!(p.rid.unwrap().as_str(), "ibr_aaaaaaaaaaaa");
}

#[test]
fn parse_marker_payload_accepts_reverse_order() {
    let s = "<!-- ib:rid=ibr_aaaaaaaaaaaa,cid=ibc_7f3a2b91c4d8 -->";
    let payload = parse_marker_payload(s).expect("parse");
    assert_eq!(payload.cid.unwrap().as_str(), "ibc_7f3a2b91c4d8");
    assert_eq!(payload.rid.unwrap().as_str(), "ibr_aaaaaaaaaaaa");
}

#[test]
fn parse_marker_payload_skips_unknown_keys() {
    // Forward-compat: unknown keys are dropped, known keys are still extracted.
    let s = "<!-- ib:cid=ibc_7f3a2b91c4d8,future=value123,rid=ibr_aaaaaaaaaaaa -->";
    let payload = parse_marker_payload(s).expect("parse");
    assert_eq!(payload.cid.unwrap().as_str(), "ibc_7f3a2b91c4d8");
    assert_eq!(payload.rid.unwrap().as_str(), "ibr_aaaaaaaaaaaa");
}

#[test]
fn make_combined_marker_cid_only() {
    let cid = Cid::parse("ibc_7f3a2b91c4d8").unwrap();
    let payload = MarkerPayload { cid: Some(cid), rid: None };
    assert_eq!(
        make_combined_marker(&payload),
        "\n<!-- ib:cid=ibc_7f3a2b91c4d8 -->"
    );
}

#[test]
fn make_combined_marker_rid_only() {
    let rid = Rid::parse("ibr_aaaaaaaaaaaa").unwrap();
    let payload = MarkerPayload { cid: None, rid: Some(rid) };
    assert_eq!(
        make_combined_marker(&payload),
        "\n<!-- ib:rid=ibr_aaaaaaaaaaaa -->"
    );
}

#[test]
fn make_combined_marker_cid_and_rid() {
    let cid = Cid::parse("ibc_7f3a2b91c4d8").unwrap();
    let rid = Rid::parse("ibr_aaaaaaaaaaaa").unwrap();
    let payload = MarkerPayload { cid: Some(cid), rid: Some(rid) };
    assert_eq!(
        make_combined_marker(&payload),
        "\n<!-- ib:cid=ibc_7f3a2b91c4d8,rid=ibr_aaaaaaaaaaaa -->"
    );
}

#[test]
fn make_combined_marker_empty_payload_returns_empty() {
    let payload = MarkerPayload { cid: None, rid: None };
    assert_eq!(make_combined_marker(&payload), "");
}

#[test]
fn strip_combined_marker_removes_syntactic_marker_even_when_payload_invalid() {
    let mut s = String::from("Hello\n<!-- ib:cid=not-a-cid -->");
    let result = strip_combined_marker(&mut s);
    assert!(result.cid.is_none(), "malformed CID should be None");
    assert_eq!(s, "Hello", "syntactic marker still removed");
}

#[test]
fn existing_make_text_marker_still_works_as_cid_only_shim() {
    let cid = Cid::parse("ibc_7f3a2b91c4d8").unwrap();
    assert_eq!(make_text_marker(&cid), "\n<!-- ib:cid=ibc_7f3a2b91c4d8 -->");
}
```

- [ ] **Step 3.2: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::marker::tests::parse_marker_payload
cargo test -p agentgateway --lib governance::marker::tests::make_combined_marker
cargo test -p agentgateway --lib governance::marker::tests::strip_combined_marker
```

Expected: FAIL — `parse_marker_payload`, `make_combined_marker`, `strip_combined_marker`, `MarkerPayload` all undefined.

- [ ] **Step 3.3: Implement the combined-carrier grammar**

Replace the current `marker.rs` body (everything between the file header and the tests module) with:

```rust
//! Text-marker primitives shared between shape walkers.
//! Spec §4.2 Channel 2, §6.3. Evolved by CHG-26C to combined-carrier grammar
//! (key=value pairs supporting cid and rid; forward-compat for future correlators).

use std::sync::OnceLock;

use regex::Regex;

use crate::governance::types::{Cid, Rid};

/// Combined marker payload — what's carried in a single `<!-- ib:... -->` text marker.
/// Either field may be Some independently (single-correlator emission) or both
/// (combined emission). Empty payload renders to empty string (no marker emitted).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct MarkerPayload {
    pub cid: Option<Cid>,
    pub rid: Option<Rid>,
}

impl MarkerPayload {
    pub fn is_empty(&self) -> bool {
        self.cid.is_none() && self.rid.is_none()
    }
}

/// CHG-26C — marker grammar. Wide-alphabet capture at lex; per-key parse below.
/// Pattern matches `<!-- ib:<kvpairs> -->` where <kvpairs> is one or more
/// key=value pairs joined by `,`. We use a single regex to detect + remove the
/// marker; key=value splitting happens in Rust (simpler than nested regex).
fn marker_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\n?<!-- ib:([a-zA-Z0-9_=,-]+) -->").unwrap())
}

/// Parse a key=value pairs string into a MarkerPayload. Unknown keys are
/// logged at debug and skipped (forward-compat for future correlator additions).
fn parse_kvpairs(kvpairs: &str) -> MarkerPayload {
    let mut payload = MarkerPayload::default();
    for pair in kvpairs.split(',') {
        let mut iter = pair.splitn(2, '=');
        let key = iter.next().unwrap_or("").trim();
        let value = iter.next().unwrap_or("").trim();
        match key {
            "cid" => {
                payload.cid = Cid::parse(value);
            },
            "rid" => {
                payload.rid = Rid::parse(value);
            },
            other if !other.is_empty() => {
                tracing::debug!(
                    target: "agentgateway::governance",
                    "marker: skipping unknown key {other:?} (forward-compat)"
                );
            },
            _ => {},
        }
    }
    payload
}

/// Parse a marker from any string containing one. Returns None if no syntactic
/// marker present. Returns Some(MarkerPayload) with whatever known keys parsed
/// successfully; unknown keys are dropped.
pub fn parse_marker_payload(text: &str) -> Option<MarkerPayload> {
    marker_re()
        .captures(text)
        .and_then(|c| c.get(1))
        .map(|m| parse_kvpairs(m.as_str()))
}

/// Build a marker string from a payload. Empty payload returns empty string
/// (no marker — callers can short-circuit). Single-field emission still uses
/// key=value form: `<!-- ib:cid=ibc_xxx -->` or `<!-- ib:rid=ibr_yyy -->`.
/// Combined: `<!-- ib:cid=ibc_xxx,rid=ibr_yyy -->`. Key order: cid before rid
/// when both present (deterministic for testing).
pub fn make_combined_marker(payload: &MarkerPayload) -> String {
    if payload.is_empty() {
        return String::new();
    }
    let mut parts = Vec::new();
    if let Some(c) = &payload.cid {
        parts.push(format!("cid={}", c.as_str()));
    }
    if let Some(r) = &payload.rid {
        parts.push(format!("rid={}", r.as_str()));
    }
    format!("\n<!-- ib:{} -->", parts.join(","))
}

/// CHG-26C replacement for `strip_text_marker`. Removes a syntactic marker
/// from `text` in place and returns the parsed payload (empty MarkerPayload
/// if no known keys matched). §6.7 non-destructive — marker bytes are
/// stripped regardless of payload validity.
pub fn strip_combined_marker(text: &mut String) -> MarkerPayload {
    let payload = parse_marker_payload(text).unwrap_or_default();
    *text = marker_re().replace_all(text, "").into_owned();
    payload
}

// --- Backwards-compat shims ---
//
// Old callers using `make_text_marker(cid)` / `strip_text_marker(text) -> Option<Cid>`
// still work; they're thin wrappers around the new combined-carrier helpers.
// New code should prefer `make_combined_marker` / `strip_combined_marker`.

/// CID-only marker (compat shim). Equivalent to
/// `make_combined_marker(&MarkerPayload { cid: Some(cid), rid: None })`.
pub fn make_text_marker(cid: &Cid) -> String {
    let payload = MarkerPayload { cid: Some(cid.clone()), rid: None };
    make_combined_marker(&payload)
}

/// CID-only strip (compat shim). Equivalent to
/// `strip_combined_marker(text).cid`.
pub fn strip_text_marker(text: &mut String) -> Option<Cid> {
    strip_combined_marker(text).cid
}
```

- [ ] **Step 3.4: Run, see pass**

```bash
cargo test -p agentgateway --lib governance::marker
```

Expected: all tests pass (existing single-correlator tests still pass via shims; new combined-carrier tests also pass).

- [ ] **Step 3.5: Commit**

```bash
git add crates/agentgateway/src/governance/marker.rs
git commit -m "feat(cidgar): CHG-26C combined-carrier marker grammar

Evolves the C2 text marker from single-capture (cid only) to a key=value
pair walker supporting both cid and rid (and forward-compat for future
correlator keys via debug-log-and-skip).

  Old: <!-- ib:cid=ibc_xxx -->
  New: <!-- ib:cid=ibc_xxx,rid=ibr_yyy -->        (combined)
       <!-- ib:cid=ibc_xxx -->                    (cid only)
       <!-- ib:rid=ibr_yyy -->                    (rid only)

MarkerPayload struct + parse_marker_payload / make_combined_marker /
strip_combined_marker public API. Old make_text_marker / strip_text_marker
preserved as thin compat shims so existing call sites in messages_shape
and completions_shape don't break.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: CHG-26A — Extend `GovContext` with RID-related fields

**Files:**
- Modify: `crates/agentgateway/src/governance/types.rs`

- [ ] **Step 4.1: Write failing test**

Add to `types.rs::tests`:

```rust
#[test]
fn govcontext_default_rid_fields_are_none() {
    let ctx = GovContext::default();
    assert!(ctx.rid.is_none());
    assert!(ctx.parent_rid.is_none());
    assert!(!ctx.is_turn_boundary);
    assert!(ctx.parent_rid_sources.is_empty());
    assert!(!ctx.parent_rid_anomaly);
    assert!(ctx.provider_response_id.is_none());
}
```

- [ ] **Step 4.2: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::types::tests::govcontext_default_rid
```

Expected: FAIL — `rid`/`parent_rid`/`is_turn_boundary`/etc. fields don't exist.

- [ ] **Step 4.3: Extend `GovContext`**

In `types.rs`, replace the `GovContext` struct definition:

```rust
/// Per-request governance context. Carries identity metadata for §14.5 header-passthrough
/// readiness AND CHG-26A run-identity fields. Designed so future hyperstate work can adopt
/// the same struct (renamed to `PipelineContext`) without reshaping it.
#[derive(Debug, Default, Clone)]
pub struct GovContext {
    pub request_id: Strng,
    pub backend_name: Strng,
    pub route: Strng,
    /// Populated at f2; consumed at f3 within the same HTTP transaction.
    pub cid: Option<Cid>,
    // §14.5 readiness — populated when present, never required.
    pub user_sub: Option<Strng>,
    pub agent_nhi_sub: Option<Strng>,
    pub trace_id: Option<Strng>,
    pub span_id: Option<Strng>,
    pub baggage: Vec<(Strng, Strng)>,
    // CHG-26A — RID fields. All populated at f2 (or left default).
    /// Current LLM call's RID. Minted at f2.
    pub rid: Option<Rid>,
    /// Resolved at f2 via priority chain (previous_response_id → C1 → C3 → C2 → null).
    pub parent_rid: Option<Rid>,
    /// Derived at f2 from body shape.
    pub is_turn_boundary: bool,
    /// Which carriers contributed to parent_rid resolution (e.g. ["c1", "c2"]).
    /// Empty when parent_rid is None.
    pub parent_rid_sources: Vec<Strng>,
    /// True iff multiple carriers carried disagreeing parent_rid values.
    pub parent_rid_anomaly: bool,
    /// Provider's native response id (e.g. `chatcmpl-xxx`, `resp_xxx`, `msg_xxx`).
    /// Captured at f3 from response body; NOT injected into any carrier.
    pub provider_response_id: Option<Strng>,
}
```

- [ ] **Step 4.4: Run, see pass**

```bash
cargo test -p agentgateway --lib governance::types
```

Expected: all tests pass.

- [ ] **Step 4.5: Commit**

```bash
git add crates/agentgateway/src/governance/types.rs
git commit -m "feat(cidgar): CHG-26A extend GovContext with RID fields

Adds rid / parent_rid / is_turn_boundary / parent_rid_sources /
parent_rid_anomaly / provider_response_id fields to GovContext. All
default to None / empty / false. Populated by f2 and consumed by f3
within the same HTTP transaction (no cross-request state).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: CHG-26D — Extend `Phase` enum with RID audit fields

**Files:**
- Modify: `crates/agentgateway/src/governance/log.rs`

- [ ] **Step 5.1: Write failing test**

Add to `log.rs::tests`:

```rust
#[test]
fn llm_request_phase_serializes_rid_fields() {
    let cid = Cid::generate();
    let rid = crate::governance::types::Rid::parse("ibr_aaaaaaaaaaaa").unwrap();
    let parent_rid = crate::governance::types::Rid::parse("ibr_bbbbbbbbbbbb").unwrap();
    let entry = LogEntry::new(
        Phase::LlmRequest {
            uctx: Some("hello".into()),
            sctx: None,
            rid: Some(rid.clone()),
            parent_rid: Some(parent_rid.clone()),
            is_turn_boundary: true,
            parent_rid_sources: vec!["c1".into(), "c2".into()],
            parent_rid_anomaly: false,
        },
        Some(&cid),
        "ollama",
        None,
    );
    let v = entry.serialize_for_level(LogLevel::Debug);
    let s = v.to_string();
    assert!(s.contains("\"rid\":\"ibr_aaaaaaaaaaaa\""));
    assert!(s.contains("\"parent_rid\":\"ibr_bbbbbbbbbbbb\""));
    assert!(s.contains("\"is_turn_boundary\":true"));
    assert!(s.contains("\"parent_rid_sources\":[\"c1\",\"c2\"]"));
}

#[test]
fn llm_response_phase_serializes_provider_response_id() {
    let cid = Cid::generate();
    let rid = crate::governance::types::Rid::parse("ibr_aaaaaaaaaaaa").unwrap();
    let entry = LogEntry::new(
        Phase::LlmResponse {
            rid: Some(rid),
            provider_response_id: Some("chatcmpl-xyz".into()),
        },
        Some(&cid),
        "ollama",
        None,
    );
    let v = entry.serialize_for_level(LogLevel::Debug);
    let s = v.to_string();
    assert!(s.contains("\"rid\":\"ibr_aaaaaaaaaaaa\""));
    assert!(s.contains("\"provider_response_id\":\"chatcmpl-xyz\""));
}

#[test]
fn tool_call_phase_serializes_parent_run_rid() {
    let cid = Cid::generate();
    let rid = crate::governance::types::Rid::parse("ibr_aaaaaaaaaaaa").unwrap();
    let entry = LogEntry::new(
        Phase::ToolCall {
            tool: "get_weather".into(),
            args: json!({"city": "Paris"}),
            gar: GarValueLog::Missing(()),
            snapshot_hash: None,
            original_tool_name: None,
            correlation_lost: None,
            parent_run_rid: Some(rid),
        },
        Some(&cid),
        "ollama",
        None,
    );
    let v = entry.serialize_for_level(LogLevel::Debug);
    let s = v.to_string();
    assert!(s.contains("\"parent_run_rid\":\"ibr_aaaaaaaaaaaa\""));
}

#[test]
fn audit_redaction_preserves_rid_fields() {
    // RID/parent_rid are operator telemetry, NOT user content. Audit redaction
    // must NOT mangle them. Mirrors snapshot_correlation field preservation.
    let cid = Cid::generate();
    let rid = crate::governance::types::Rid::parse("ibr_aaaaaaaaaaaa").unwrap();
    let entry = LogEntry::new(
        Phase::LlmRequest {
            uctx: Some("super secret user query".into()),
            sctx: None,
            rid: Some(rid),
            parent_rid: None,
            is_turn_boundary: false,
            parent_rid_sources: vec![],
            parent_rid_anomaly: false,
        },
        Some(&cid),
        "ollama",
        None,
    );
    let v = entry.serialize_for_level(LogLevel::Audit);
    let s = v.to_string();
    assert!(!s.contains("super secret user query"), "uctx must be redacted");
    assert!(s.contains("\"rid\":\"ibr_aaaaaaaaaaaa\""), "rid must survive redaction");
}
```

- [ ] **Step 5.2: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::log::tests::llm_request_phase_serializes_rid_fields
cargo test -p agentgateway --lib governance::log::tests::llm_response_phase_serializes_provider_response_id
cargo test -p agentgateway --lib governance::log::tests::tool_call_phase_serializes_parent_run_rid
```

Expected: FAIL — new fields and new `LlmResponse` variant undefined.

- [ ] **Step 5.3: Extend `Phase` enum**

In `log.rs`, replace the `Phase` enum:

```rust
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
pub enum Phase {
    ToolsList {
        tools: Vec<String>,
        schema_hash: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        snapshot_hash: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        snapshot_body: Option<serde_json::Value>,
    },
    LlmRequest {
        uctx: Option<String>,
        sctx: Option<String>,
        // CHG-26D — RID fields. Skipped on serialize when None/empty/false for
        // backwards-compat with audit consumers that pre-date Design B.
        #[serde(skip_serializing_if = "Option::is_none")]
        rid: Option<crate::governance::types::Rid>,
        #[serde(skip_serializing_if = "Option::is_none")]
        parent_rid: Option<crate::governance::types::Rid>,
        #[serde(skip_serializing_if = "is_false")]
        is_turn_boundary: bool,
        #[serde(skip_serializing_if = "Vec::is_empty")]
        parent_rid_sources: Vec<agent_core::prelude::Strng>,
        #[serde(skip_serializing_if = "is_false")]
        parent_rid_anomaly: bool,
    },
    // CHG-26D NEW — LlmResponse phase carries the response's RID and the
    // provider's native id. Emitted at f3 after on_llm_response runs.
    LlmResponse {
        #[serde(skip_serializing_if = "Option::is_none")]
        rid: Option<crate::governance::types::Rid>,
        #[serde(skip_serializing_if = "Option::is_none")]
        provider_response_id: Option<agent_core::prelude::Strng>,
    },
    ToolPlanned {
        tool: String,
        gar: GarValueLog,
    },
    Terminal,
    ToolCall {
        tool: String,
        args: serde_json::Value,
        gar: GarValueLog,
        #[serde(skip_serializing_if = "Option::is_none")]
        snapshot_hash: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        original_tool_name: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        correlation_lost: Option<bool>,
        // CHG-26D — RID of the LLM run that issued this tool call. From f4
        // extraction of `_ib_rid` in args.
        #[serde(skip_serializing_if = "Option::is_none")]
        parent_run_rid: Option<crate::governance::types::Rid>,
    },
    ToolResponse {
        tool: String,
        is_error: bool,
        // CHG-26D — same value as ToolCall's parent_run_rid (same transaction).
        #[serde(skip_serializing_if = "Option::is_none")]
        parent_run_rid: Option<crate::governance::types::Rid>,
    },
}

fn is_false(b: &bool) -> bool {
    !b
}
```

Add serde derive to `Rid`: in `types.rs`, change the `Rid` derive line to:

```rust
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, serde::Serialize, serde::Deserialize)]
pub struct Rid(Strng);
```

(Cid already has `Serialize`/`Deserialize` via Strng — but we use a custom display path. Use the same Strng-wrapper approach as Cid.)

Wait — need to check: does `Cid` actually derive Serialize/Deserialize? Looking at types.rs line 9:

```rust
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default)]
pub struct Cid(Strng);
```

No — Cid does NOT derive serde. But Phase::ToolsList serializes `tools: Vec<String>` and the Phase has `#[derive(Serialize)]`. The `cid` field in `LogEntry` is `Option<&'a str>`, not `Option<&Cid>` directly. So serialization uses `as_str()` via a manual mapping. Same approach needed for Rid.

Revise the Phase enum to use `Option<crate::governance::types::Rid>` but serialize via a custom helper. Actually the simplest path: add `serde::Serialize` derive to Rid too, since Strng (agent_core::prelude::Strng) is itself serializable.

Add to `Rid`:

```rust
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, serde::Serialize)]
pub struct Rid(Strng);
```

(Strng's Serialize impl uses the inner string representation.)

- [ ] **Step 5.4: Run, see pass**

```bash
cargo test -p agentgateway --lib governance::log
cargo test -p agentgateway --lib governance::types
```

Expected: all tests pass.

- [ ] **Step 5.5: Update existing tests in log.rs that construct Phase::LlmRequest**

Find existing tests that construct `Phase::LlmRequest { uctx, sctx }`:

```bash
grep -n "Phase::LlmRequest" crates/agentgateway/src/governance/log.rs
```

For each, extend to include the new fields (all None/false/empty):

```rust
Phase::LlmRequest {
    uctx: Some("...".into()),
    sctx: None,
    rid: None,
    parent_rid: None,
    is_turn_boundary: false,
    parent_rid_sources: vec![],
    parent_rid_anomaly: false,
}
```

Same for `Phase::ToolCall`: existing construction now needs `parent_run_rid: None`. Apply the sweep.

The `audit_redacts_uctx_to_length_stub` test at log.rs:234, `debug_includes_full_uctx` at log.rs:252, and `emit_structured_fields_does_not_panic_for_all_phases` at log.rs:329 all need this sweep. Add a new `Phase::LlmResponse { rid: None, provider_response_id: None }` entry to the `phases` vec in the latter.

The `with_audit_redaction` method's match in `log.rs:130` also needs updating — add the new fields to the `Phase::LlmRequest` arm:

```rust
Phase::LlmRequest {
    uctx,
    sctx,
    rid,
    parent_rid,
    is_turn_boundary,
    parent_rid_sources,
    parent_rid_anomaly,
} => Phase::LlmRequest {
    uctx: uctx.map(|s| format!("<{} chars>", s.chars().count())),
    sctx: sctx.map(|s| format!("<{} chars>", s.chars().count())),
    // CHG-26D — RID fields are operator telemetry, not user content. Preserve
    // through audit-level redaction (mirrors snapshot_correlation pattern).
    rid,
    parent_rid,
    is_turn_boundary,
    parent_rid_sources,
    parent_rid_anomaly,
},
```

And the `Phase::ToolCall` arm — add `parent_run_rid` pass-through:

```rust
Phase::ToolCall {
    tool,
    args,
    gar,
    snapshot_hash,
    original_tool_name,
    correlation_lost,
    parent_run_rid,
} => Phase::ToolCall {
    tool,
    args: serde_json::Value::String(format!(
        "<{} bytes>",
        serde_json::to_string(&args).unwrap_or_default().len()
    )),
    gar,
    snapshot_hash,
    original_tool_name,
    correlation_lost,
    // CHG-26D — operator telemetry, preserve through redaction.
    parent_run_rid,
},
```

The `phase_name` match in `emit` (line 176-183) needs a new arm:

```rust
Phase::LlmResponse { .. } => "llm_response",
```

- [ ] **Step 5.6: Run full governance test suite, see all pass**

```bash
cargo test -p agentgateway --lib governance::
```

Expected: all tests pass; baseline grows by 4 new tests + existing tests adapted.

- [ ] **Step 5.7: Commit**

```bash
git add crates/agentgateway/src/governance/{log,types}.rs
git commit -m "feat(cidgar): CHG-26D extend Phase enum with RID audit fields

Adds new audit fields per spec §10 + §3.3:
  - Phase::LlmRequest: rid, parent_rid, is_turn_boundary,
                       parent_rid_sources, parent_rid_anomaly
  - Phase::LlmResponse (NEW variant): rid, provider_response_id
  - Phase::ToolCall: parent_run_rid
  - Phase::ToolResponse: parent_run_rid

All new fields are Option/Vec/bool with skip_serializing_if so audit
consumers from before Design B see no shape change when the toggles
are off (which is the default).

Audit-level redaction preserves RID fields (operator telemetry, not
user content) — same pattern as snapshot_correlation fields.

Adds Serialize derive to Rid type so it serializes uniformly with Cid.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: f1 — schema injection for `_ib_rid` (gated by `schema_rid`)

**Files:**
- Modify: `crates/agentgateway/src/governance/gar.rs` (`inject_governance_into_schema` signature extension)
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (call site)

- [ ] **Step 6.1: Write failing tests in `gar.rs::tests`**

```rust
#[test]
fn inject_with_schema_rid_true_includes_ib_rid_property() {
    use serde_json::Map;
    let mut schema: Map<String, Value> = serde_json::from_value(json!({
        "type": "object",
        "properties": {},
        "required": []
    })).unwrap();
    inject_governance_into_schema(
        &mut schema,
        crate::governance::config::GarMode::None,
        false,    // schema_cid: false
        true,     // schema_rid: true (NEW)
    );
    let props = schema.get("properties").unwrap().as_object().unwrap();
    assert!(props.contains_key("_ib_rid"));
    assert!(!props.contains_key("_ib_cid"),
        "_ib_cid should be absent when schema_cid=false");
}

#[test]
fn inject_with_schema_rid_false_omits_ib_rid_property() {
    use serde_json::Map;
    let mut schema: Map<String, Value> = serde_json::from_value(json!({
        "type": "object",
        "properties": {},
        "required": []
    })).unwrap();
    inject_governance_into_schema(
        &mut schema,
        crate::governance::config::GarMode::None,
        true,     // schema_cid: true
        false,    // schema_rid: false
    );
    let props = schema.get("properties").unwrap().as_object().unwrap();
    assert!(props.contains_key("_ib_cid"));
    assert!(!props.contains_key("_ib_rid"));
}

#[test]
fn inject_with_both_schema_toggles_includes_both() {
    use serde_json::Map;
    let mut schema: Map<String, Value> = serde_json::from_value(json!({
        "type": "object",
        "properties": {},
        "required": []
    })).unwrap();
    inject_governance_into_schema(
        &mut schema,
        crate::governance::config::GarMode::None,
        true,    // schema_cid
        true,    // schema_rid
    );
    let props = schema.get("properties").unwrap().as_object().unwrap();
    assert!(props.contains_key("_ib_cid"));
    assert!(props.contains_key("_ib_rid"));
}

#[test]
fn inject_ib_rid_property_is_optional_not_required() {
    // Spec §3.3 / Design B per-API f1: _ib_rid is in properties but NOT
    // appended to required. AGW overwrites at f3 PATH A; LLM doesn't need
    // to populate it.
    use serde_json::Map;
    let mut schema: Map<String, Value> = serde_json::from_value(json!({
        "type": "object",
        "properties": {},
        "required": []
    })).unwrap();
    inject_governance_into_schema(
        &mut schema,
        crate::governance::config::GarMode::Required,  // GAR required
        true,    // schema_cid
        true,    // schema_rid
    );
    let req = schema.get("required").unwrap().as_array().unwrap();
    assert!(req.iter().any(|v| v.as_str() == Some("_ib_gar")),
        "_ib_gar should be required when mode=Required");
    assert!(!req.iter().any(|v| v.as_str() == Some("_ib_rid")),
        "_ib_rid should NEVER be in required (AGW writes it at f3 PATH A)");
    assert!(!req.iter().any(|v| v.as_str() == Some("_ib_cid")),
        "_ib_cid should NEVER be in required (existing §3.1 behavior)");
}
```

- [ ] **Step 6.2: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::gar::tests::inject_with_schema_rid
cargo test -p agentgateway --lib governance::gar::tests::inject_with_both_schema_toggles
cargo test -p agentgateway --lib governance::gar::tests::inject_ib_rid_property_is_optional_not_required
```

Expected: FAIL — `inject_governance_into_schema` takes only 3 args.

- [ ] **Step 6.3: Extend `inject_governance_into_schema` signature**

In `gar.rs`, change the function signature and body:

```rust
pub fn inject_governance_into_schema(
    schema: &mut serde_json::Map<String, serde_json::Value>,
    mode: crate::governance::config::GarMode,
    schema_cid: bool,
    schema_rid: bool,            // NEW (CHG-26A / CHG-26B)
) {
    use crate::governance::config::GarMode;
    use serde_json::{Value, json};

    if schema.get("type").and_then(Value::as_str) != Some("object") {
        return;
    }
    let Some(props) = schema
        .entry("properties")
        .or_insert_with(|| Value::Object(serde_json::Map::new()))
        .as_object_mut()
    else {
        return;
    };

    if schema_cid {
        props.insert(
            "_ib_cid".into(),
            json!({
                "type": "string",
                "description": "Auto-populated by gateway. Do not fill."
            }),
        );
    }

    // CHG-26A / CHG-26B: gate _ib_rid injection on schema_rid. Same shape as
    // _ib_cid — optional string field, auto-populated by AGW at f3 PATH A.
    // LLM does NOT need to fill it.
    if schema_rid {
        props.insert(
            "_ib_rid".into(),
            json!({
                "type": "string",
                "description": "Auto-populated by gateway. Do not fill."
            }),
        );
    }

    if mode != GarMode::None {
        props.insert(
            "_ib_gar".into(),
            json!({
                "type": "string",
                "description": GAR_SCHEMA_DESC
            }),
        );
    }

    let Some(req) = schema
        .entry("required")
        .or_insert_with(|| Value::Array(Vec::new()))
        .as_array_mut()
    else {
        return;
    };

    if mode == GarMode::Required && !req.iter().any(|v| v.as_str() == Some("_ib_gar")) {
        req.push(Value::String("_ib_gar".into()));
    }
    // Note: _ib_cid and _ib_rid are NEVER added to required — AGW writes both
    // at f3 PATH A; the LLM doesn't need to populate them.
}
```

- [ ] **Step 6.4: Update call site in `cidgar.rs::on_tools_list_resp`**

```bash
grep -n "inject_governance_into_schema" crates/agentgateway/src/governance/cidgar.rs
```

Find the call (likely around line 186) and add the 4th arg:

```rust
crate::governance::gar::inject_governance_into_schema(
    schema,
    self.cfg.gar.mode,
    self.cfg.channels.schema_cid,
    self.cfg.channels.schema_rid,   // NEW
);
```

- [ ] **Step 6.5: Sweep existing tests that call `inject_governance_into_schema` with 3 args**

```bash
grep -rn "inject_governance_into_schema(" crates/agentgateway/src/governance/
```

For every existing call site (tests + production), append `, false` (most tests don't want RID injection):

Pattern: `inject_governance_into_schema(schema, mode, schema_cid)` → `inject_governance_into_schema(schema, mode, schema_cid, false)`.

Apply to all ~12-15 existing call sites.

- [ ] **Step 6.6: Run full governance suite**

```bash
cargo test -p agentgateway --lib governance::
```

Expected: all tests pass; +4 new schema_rid tests.

- [ ] **Step 6.7: Commit**

```bash
git add crates/agentgateway/src/governance/{gar,cidgar}.rs
git commit -m "feat(cidgar): CHG-26A f1 inject _ib_rid into inputSchema (gated by schema_rid)

inject_governance_into_schema signature: (schema, mode, schema_cid, schema_rid).
When schema_rid=true, _ib_rid appears in properties as an optional string
field. NEVER added to required (AGW overwrites at f3 PATH A; LLM doesn't
populate). Mirrors _ib_cid injection.

All existing call sites updated to pass schema_rid=false (preserves
behavior pre-Design-B).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: f2 — RID minting + parent_rid resolution priority chain + is_turn_boundary derivation

**Files:**
- Modify: `crates/agentgateway/src/governance/messages_shape.rs`
- Modify: `crates/agentgateway/src/governance/completions_shape.rs`
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (f2 hook integration)

This is the most architecturally substantial task. Subdivided into shape helpers first, then pipeline integration.

#### 7a — Shape helpers: parent_rid scanning + is_turn_boundary

- [ ] **Step 7a.1: Write failing test for `messages_shape::scan_for_rid_carriers`**

Add to `messages_shape.rs::tests`:

```rust
#[test]
fn scan_for_rid_carriers_finds_c2_marker_in_assistant_content() {
    use crate::governance::marker::MarkerPayload;
    let req: Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "hi back\n<!-- ib:rid=ibr_aaaaaaaaaaaa -->"}
            ]},
            {"role": "user", "content": "continue"}
        ]
    })).unwrap();
    let scan = scan_for_rid_carriers(&req);
    assert_eq!(scan.candidates.len(), 1);
    let (carrier, rid) = &scan.candidates[0];
    assert_eq!(carrier.as_ref(), "c2");
    assert_eq!(rid.as_str(), "ibr_aaaaaaaaaaaa");
}

#[test]
fn scan_for_rid_carriers_finds_c1_in_tool_use_input() {
    let req: Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "what's the weather"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "get_weather",
                 "input": {"city": "Paris", "_ib_rid": "ibr_aaaaaaaaaaaa"}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "content": "sunny"}
            ]}
        ]
    })).unwrap();
    let scan = scan_for_rid_carriers(&req);
    let c1_rids: Vec<_> = scan.candidates.iter()
        .filter(|(c, _)| c.as_ref() == "c1")
        .collect();
    assert!(!c1_rids.is_empty());
    assert_eq!(c1_rids[0].1.as_str(), "ibr_aaaaaaaaaaaa");
}

#[test]
fn scan_for_rid_carriers_returns_empty_when_no_rid_present() {
    let req: Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hello"}]
    })).unwrap();
    let scan = scan_for_rid_carriers(&req);
    assert!(scan.candidates.is_empty());
}

#[test]
fn is_turn_boundary_messages_user_after_assistant_is_boundary() {
    let req: Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "follow-up"}
        ]
    })).unwrap();
    assert!(is_turn_boundary(&req),
        "latest user index after latest assistant → boundary");
}

#[test]
fn is_turn_boundary_messages_tool_result_continuation_is_not_boundary() {
    let req: Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "what's the weather"},
            {"role": "assistant", "content": [{"type": "tool_use", "name": "x"}]},
            {"role": "user", "content": [{"type": "tool_result", "content": "sunny"}]}
        ]
    })).unwrap();
    // The "user" message at the end is a tool_result, not a new user message.
    // In Anthropic messages shape, tool_result is sent as a user-role message
    // with content blocks of type tool_result. We treat this as continuation,
    // not a new turn boundary.
    assert!(!is_turn_boundary(&req),
        "tool_result user message is continuation, not new turn");
}
```

- [ ] **Step 7a.2: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::messages_shape::tests::scan_for_rid_carriers
cargo test -p agentgateway --lib governance::messages_shape::tests::is_turn_boundary
```

Expected: FAIL — `scan_for_rid_carriers` and `is_turn_boundary` undefined.

- [ ] **Step 7a.3: Implement `messages_shape::scan_for_rid_carriers` and `is_turn_boundary`**

Add to `messages_shape.rs`:

```rust
use agent_core::prelude::Strng;
use crate::governance::types::Rid;
use crate::governance::marker::parse_marker_payload;

/// Result of scanning a Messages-shape request for RID carriers.
#[derive(Debug, Default)]
pub struct RidScanResult {
    /// All (carrier_name, rid) pairs observed, in body order (top-to-bottom).
    /// carrier_name ∈ {"c1", "c2", "c3"} corresponding to the persistence channel.
    pub candidates: Vec<(Strng, Rid)>,
}

/// CHG-26A — scan a Messages-shape LLM request body for `_ib_rid` carriers.
/// Looks in:
///   - assistant content `tool_use.input._ib_rid`              → "c1"
///   - assistant text blocks with `<!-- ib:cid=...,rid=... -->` → "c2"
///   - user content `tool_result` content blocks (resource block or text)
///     carrying `_ib_rid`                                       → "c3"
///
/// Returns all observations in order so the caller can detect disagreement
/// (multi-source anomaly) and apply priority resolution.
pub fn scan_for_rid_carriers(req: &Request) -> RidScanResult {
    let mut result = RidScanResult::default();

    for msg in req.messages.iter() {
        match msg.role.as_str() {
            "assistant" => {
                // Walk content blocks for tool_use._ib_rid (c1) and text markers (c2)
                if let Some(content) = msg.content.as_ref() {
                    if let serde_json::Value::Array(blocks) = content {
                        for block in blocks {
                            let block_type = block.get("type").and_then(|v| v.as_str());
                            if block_type == Some("tool_use") {
                                if let Some(input) = block.get("input") {
                                    if let Some(rid_str) =
                                        input.get("_ib_rid").and_then(|v| v.as_str())
                                    {
                                        if let Some(rid) = Rid::parse(rid_str) {
                                            result.candidates.push(("c1".into(), rid));
                                        }
                                    }
                                }
                            } else if block_type == Some("text") {
                                if let Some(text) = block.get("text").and_then(|v| v.as_str()) {
                                    if let Some(payload) = parse_marker_payload(text) {
                                        if let Some(rid) = payload.rid {
                                            result.candidates.push(("c2".into(), rid));
                                        }
                                    }
                                }
                            }
                        }
                    } else if let serde_json::Value::String(text) = content {
                        // Some adapters use string content for assistant — scan as text marker.
                        if let Some(payload) = parse_marker_payload(text) {
                            if let Some(rid) = payload.rid {
                                result.candidates.push(("c2".into(), rid));
                            }
                        }
                    }
                }
            },
            "user" => {
                // Walk content blocks for tool_result — extract from resource block
                // or text block embedded in tool_result.content.
                if let Some(serde_json::Value::Array(blocks)) = msg.content.as_ref() {
                    for block in blocks {
                        if block.get("type").and_then(|v| v.as_str()) == Some("tool_result") {
                            // tool_result.content may be a string or array of content blocks
                            if let Some(tr_content) = block.get("content") {
                                if let Some(text) = tr_content.as_str() {
                                    // Text content — scan for combined marker
                                    if let Some(payload) = parse_marker_payload(text) {
                                        if let Some(rid) = payload.rid {
                                            result.candidates.push(("c3".into(), rid));
                                        }
                                    }
                                } else if let Some(arr) = tr_content.as_array() {
                                    for inner in arr {
                                        // Resource block carrying gateway-meta JSON, or text block
                                        if inner.get("type").and_then(|v| v.as_str()) == Some("resource") {
                                            if let Some(text) = inner
                                                .get("resource")
                                                .and_then(|r| r.get("text"))
                                                .and_then(|v| v.as_str())
                                            {
                                                if let Ok(payload) =
                                                    serde_json::from_str::<serde_json::Value>(text)
                                                {
                                                    if let Some(rid_str) =
                                                        payload.get("rid").and_then(|v| v.as_str())
                                                    {
                                                        if let Some(rid) = Rid::parse(rid_str) {
                                                            result.candidates.push(("c3".into(), rid));
                                                        }
                                                    }
                                                }
                                            }
                                        } else if inner.get("type").and_then(|v| v.as_str())
                                            == Some("text")
                                        {
                                            if let Some(text) =
                                                inner.get("text").and_then(|v| v.as_str())
                                            {
                                                if let Some(payload) = parse_marker_payload(text) {
                                                    if let Some(rid) = payload.rid {
                                                        result.candidates.push(("c3".into(), rid));
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            _ => {}, // system, other — skip
        }
    }

    result
}

/// CHG-26A — derive `is_turn_boundary` for a Messages-shape request.
/// A turn boundary is a call whose latest message is a new user message
/// (not a tool_result continuation), placed after the most-recent assistant
/// message. Spec §3.3 / Design B per-API f2.
pub fn is_turn_boundary(req: &Request) -> bool {
    let mut latest_user_idx: Option<usize> = None;
    let mut latest_assistant_idx: Option<usize> = None;
    let mut latest_user_is_tool_result = false;

    for (i, msg) in req.messages.iter().enumerate() {
        match msg.role.as_str() {
            "user" => {
                latest_user_idx = Some(i);
                // Check whether this "user" message is a tool_result wrapper
                // (Anthropic Messages shape: tool_result is sent as a user-role
                // message with content blocks of type tool_result).
                latest_user_is_tool_result = matches!(
                    msg.content.as_ref(),
                    Some(serde_json::Value::Array(blocks))
                        if blocks.iter().any(|b|
                            b.get("type").and_then(|v| v.as_str()) == Some("tool_result")
                        )
                );
            },
            "assistant" => {
                latest_assistant_idx = Some(i);
            },
            _ => {},
        }
    }

    match (latest_user_idx, latest_assistant_idx) {
        (Some(u), Some(a)) => u > a && !latest_user_is_tool_result,
        (Some(_u), None) => !latest_user_is_tool_result, // first turn, no prior assistant
        _ => false,
    }
}
```

Mirror in `completions_shape.rs` with the OpenAI chat-completions wire shape (`role: "user"|"assistant"|"tool"`, `content: string`, `tool_calls`).

- [ ] **Step 7a.4: Add the equivalent `completions_shape::scan_for_rid_carriers` and `is_turn_boundary`**

In `completions_shape.rs`, add (adjacent to the existing CID scanning helpers):

```rust
use agent_core::prelude::Strng;
use crate::governance::types::Rid;
use crate::governance::marker::parse_marker_payload;

/// CHG-26A — scan a Completions-shape LLM request body for _ib_rid carriers.
/// Looks in:
///   - assistant `content` text for C2 markers                  → "c2"
///   - assistant `tool_calls[].function.arguments` (JSON string) → "c1"
///   - role="tool" messages `content` text for C3 markers        → "c3"
pub fn scan_for_rid_carriers(req: &Request) -> super::messages_shape::RidScanResult {
    let mut result = super::messages_shape::RidScanResult::default();
    for msg in req.messages.iter() {
        match msg.role.as_str() {
            "assistant" => {
                // C2: text marker in content (content is a string in OpenAI chat-completions)
                if let Some(content) = msg.content.as_ref().and_then(|v| v.as_str()) {
                    if let Some(payload) = parse_marker_payload(content) {
                        if let Some(rid) = payload.rid {
                            result.candidates.push(("c2".into(), rid));
                        }
                    }
                }
                // C1: tool_calls[].function.arguments is a JSON-encoded string
                if let Some(tcs) = msg.rest.get("tool_calls").and_then(|v| v.as_array()) {
                    for tc in tcs {
                        if let Some(args_str) = tc
                            .get("function")
                            .and_then(|f| f.get("arguments"))
                            .and_then(|v| v.as_str())
                        {
                            if let Ok(args) = serde_json::from_str::<serde_json::Value>(args_str) {
                                if let Some(rid_str) =
                                    args.get("_ib_rid").and_then(|v| v.as_str())
                                {
                                    if let Some(rid) = Rid::parse(rid_str) {
                                        result.candidates.push(("c1".into(), rid));
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "tool" => {
                // C3: role="tool" message content carries MCP tool/call response — may
                // contain a combined-carrier text marker or resource block (transposed by
                // the adapter to text in chat-completions shape).
                if let Some(content) = msg.content.as_ref().and_then(|v| v.as_str()) {
                    if let Some(payload) = parse_marker_payload(content) {
                        if let Some(rid) = payload.rid {
                            result.candidates.push(("c3".into(), rid));
                        }
                    }
                    // Also try parsing as JSON for resource-block-style payload
                    if let Ok(json_payload) = serde_json::from_str::<serde_json::Value>(content) {
                        if let Some(rid_str) = json_payload.get("rid").and_then(|v| v.as_str()) {
                            if let Some(rid) = Rid::parse(rid_str) {
                                result.candidates.push(("c3".into(), rid));
                            }
                        }
                    }
                }
            },
            _ => {}, // user, system — no RID carriers in chat-completions shape
        }
    }
    result
}

/// CHG-26A — derive is_turn_boundary for a Completions-shape request.
/// Stateless APIs: latest user-role index > latest assistant-role index.
/// (Tool messages have role="tool" in completions, not "user", so there's
/// no tool_result-as-user-message ambiguity here.)
pub fn is_turn_boundary(req: &Request) -> bool {
    let mut latest_user_idx: Option<usize> = None;
    let mut latest_assistant_idx: Option<usize> = None;
    for (i, msg) in req.messages.iter().enumerate() {
        match msg.role.as_str() {
            "user" => latest_user_idx = Some(i),
            "assistant" => latest_assistant_idx = Some(i),
            _ => {},
        }
    }
    match (latest_user_idx, latest_assistant_idx) {
        (Some(u), Some(a)) => u > a,
        (Some(_), None) => true,
        _ => false,
    }
}
```

Write 5 parallel tests in `completions_shape.rs::tests`:

```rust
#[test]
fn scan_for_rid_carriers_completions_finds_c2_in_assistant_content() {
    let req: Request = serde_json::from_value(json!({
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello\n<!-- ib:rid=ibr_aaaaaaaaaaaa -->"},
            {"role": "user", "content": "continue"}
        ]
    })).unwrap();
    let scan = scan_for_rid_carriers(&req);
    let c2: Vec<_> = scan.candidates.iter().filter(|(c, _)| c.as_str() == "c2").collect();
    assert_eq!(c2.len(), 1);
    assert_eq!(c2[0].1.as_str(), "ibr_aaaaaaaaaaaa");
}

#[test]
fn scan_for_rid_carriers_completions_finds_c1_in_tool_call_arguments() {
    let req: Request = serde_json::from_value(json!({
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "what's the weather"},
            {"role": "assistant", "content": null, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "get_weather",
                    "arguments": "{\"city\":\"Paris\",\"_ib_rid\":\"ibr_aaaaaaaaaaaa\"}"
                }}
            ]}
        ]
    })).unwrap();
    let scan = scan_for_rid_carriers(&req);
    let c1: Vec<_> = scan.candidates.iter().filter(|(c, _)| c.as_str() == "c1").collect();
    assert_eq!(c1.len(), 1);
    assert_eq!(c1[0].1.as_str(), "ibr_aaaaaaaaaaaa");
}

#[test]
fn scan_for_rid_carriers_completions_finds_c3_in_tool_message() {
    let req: Request = serde_json::from_value(json!({
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": null, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1",
             "content": "sunny\n<!-- ib:rid=ibr_aaaaaaaaaaaa -->"}
        ]
    })).unwrap();
    let scan = scan_for_rid_carriers(&req);
    let c3: Vec<_> = scan.candidates.iter().filter(|(c, _)| c.as_str() == "c3").collect();
    assert_eq!(c3.len(), 1);
    assert_eq!(c3[0].1.as_str(), "ibr_aaaaaaaaaaaa");
}

#[test]
fn is_turn_boundary_completions_user_after_assistant_is_boundary() {
    let req: Request = serde_json::from_value(json!({
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second turn"}
        ]
    })).unwrap();
    assert!(is_turn_boundary(&req));
}

#[test]
fn is_turn_boundary_completions_tool_message_after_assistant_is_not_boundary() {
    // Continuation: assistant called a tool, tool response, no new user message yet.
    let req: Request = serde_json::from_value(json!({
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "weather"},
            {"role": "assistant", "content": null, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}
        ]
    })).unwrap();
    assert!(!is_turn_boundary(&req),
        "tool message is continuation, not new turn (no user-after-assistant)");
}
```

- [ ] **Step 7a.5: Run tests, see pass**

```bash
cargo test -p agentgateway --lib governance::messages_shape::tests::scan_for_rid_carriers
cargo test -p agentgateway --lib governance::messages_shape::tests::is_turn_boundary
cargo test -p agentgateway --lib governance::completions_shape::tests::scan_for_rid_carriers
cargo test -p agentgateway --lib governance::completions_shape::tests::is_turn_boundary
```

Expected: all pass.

- [ ] **Step 7a.6: Commit (shape helpers only)**

```bash
git add crates/agentgateway/src/governance/{messages_shape,completions_shape}.rs
git commit -m "feat(cidgar): CHG-26A shape helpers — scan_for_rid_carriers + is_turn_boundary

Adds Messages- and Completions-shape helpers to support f2 RID resolution:

  - scan_for_rid_carriers(req) -> RidScanResult: walks the body looking
    for _ib_rid in C1 (tool_use.input / tool_calls.function.arguments),
    C2 (text markers parsed via marker.rs combined-grammar), and C3
    (tool_result content resource blocks or text blocks). Returns all
    observations in body order so the f2 priority chain can detect
    multi-source disagreement.

  - is_turn_boundary(req) -> bool: implements the wire-shape-based
    boundary detection per spec §3.3 / Design B per-API f2 rules.
    Stateless APIs: latest user index > latest assistant index AND not
    a tool_result continuation.

No pipeline integration yet — Task 7b wires these into the f2 hook.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

#### 7b — f2 hook integration: parent_rid resolution + RID minting

- [ ] **Step 7b.1: Write failing tests in `cidgar.rs::tests`**

```rust
#[test]
fn f2_mints_rid_for_every_request() {
    // Even when no parent_rid is recoverable, the current call gets a fresh RID.
    let mut cfg = CidGarConfig::default();
    cfg.channels.text_marker_cid = true; // enable to allow CID flow
    let pipeline = CidGarPipeline::new(cfg);
    let mut req: messages_shape::Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}]
    })).unwrap();
    let mut ctx = GovContext::default();
    pipeline.on_llm_request(LlmRequest::Messages(&mut req), &mut ctx);
    assert!(ctx.rid.is_some(), "rid must be minted at f2");
    let rid = ctx.rid.as_ref().unwrap();
    assert!(rid.as_str().starts_with("ibr_"));
}

#[test]
fn f2_resolves_parent_rid_from_c2_marker() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.text_marker_cid = true;
    let pipeline = CidGarPipeline::new(cfg);
    let mut req: messages_shape::Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "hello\n<!-- ib:rid=ibr_aaaaaaaaaaaa -->"}
            ]},
            {"role": "user", "content": "continue"}
        ]
    })).unwrap();
    let mut ctx = GovContext::default();
    pipeline.on_llm_request(LlmRequest::Messages(&mut req), &mut ctx);
    let parent_rid = ctx.parent_rid.as_ref()
        .expect("parent_rid should resolve via C2 marker");
    assert_eq!(parent_rid.as_str(), "ibr_aaaaaaaaaaaa");
    assert!(ctx.parent_rid_sources.iter().any(|s| s.as_str() == "c2"));
}

#[test]
fn f2_sets_is_turn_boundary_true_on_new_user_message() {
    let cfg = CidGarConfig::default();
    let pipeline = CidGarPipeline::new(cfg);
    let mut req: messages_shape::Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second turn"}
        ]
    })).unwrap();
    let mut ctx = GovContext::default();
    pipeline.on_llm_request(LlmRequest::Messages(&mut req), &mut ctx);
    assert!(ctx.is_turn_boundary, "new user message after assistant → boundary");
}

#[test]
fn f2_detects_multi_source_disagreement() {
    // C1 says ibr_aaa, C2 says ibr_bbb — anomaly flag must fire.
    let mut cfg = CidGarConfig::default();
    cfg.channels.text_marker_cid = true;
    let pipeline = CidGarPipeline::new(cfg);
    let mut req: messages_shape::Request = serde_json::from_value(json!({
        "model": "claude-3-5-haiku",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "what's weather"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "x",
                 "input": {"_ib_rid": "ibr_aaaaaaaaaaaa"}},
                {"type": "text", "text": "<!-- ib:rid=ibr_bbbbbbbbbbbb -->"}
            ]},
            {"role": "user", "content": "continue"}
        ]
    })).unwrap();
    let mut ctx = GovContext::default();
    pipeline.on_llm_request(LlmRequest::Messages(&mut req), &mut ctx);
    assert!(ctx.parent_rid_anomaly, "disagreement between c1 and c2 must set anomaly");
    assert!(ctx.parent_rid_sources.len() >= 2);
}
```

- [ ] **Step 7b.2: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::cidgar::tests::f2_mints_rid_for_every_request
cargo test -p agentgateway --lib governance::cidgar::tests::f2_resolves_parent_rid_from_c2_marker
cargo test -p agentgateway --lib governance::cidgar::tests::f2_sets_is_turn_boundary_true
cargo test -p agentgateway --lib governance::cidgar::tests::f2_detects_multi_source_disagreement
```

Expected: FAIL — the f2 hook doesn't populate `rid`/`parent_rid`/`is_turn_boundary`/etc. on ctx.

- [ ] **Step 7b.3: Wire into `cidgar.rs::on_llm_request` (f2 hook)**

In `cidgar.rs`, find `on_llm_request` and extend after the CID resolution block:

```rust
// CHG-26A: RID minting + parent_rid resolution + is_turn_boundary derivation.
// Always runs (even when all RID channels are off) so the audit phase fields
// stay populated for downstream observability — the toggle gates EMISSION,
// not OBSERVATION.

// 1. Mint current RID.
ctx.rid = Some(Rid::generate_with(self.cfg.id_algorithm));

// 2. Scan body for RID carriers (priority chain candidates).
let scan = match &req {
    LlmRequest::Messages(r) => messages_shape::scan_for_rid_carriers(r),
    LlmRequest::Completions(r) => completions_shape::scan_for_rid_carriers(r),
};

// 3. Resolve parent_rid via priority: previous_response_id → c1 → c3 → c2.
// previous_response_id is set on the LlmRequest's inner type for Responses
// API; for Messages and Completions shapes today, it's None.
// TODO when Responses API support lands: extract previous_response_id and
// prepend to scan.candidates with carrier="prev_resp_id".

let priority = ["prev_resp_id", "c1", "c3", "c2"];
let mut chosen: Option<(Strng, Rid)> = None;
for p in priority {
    if let Some((c, r)) = scan.candidates.iter().rev().find(|(c, _)| c.as_str() == p) {
        chosen = Some((c.clone(), r.clone()));
        break;
    }
}

// 4. Populate parent_rid + sources + anomaly.
if let Some((_winning_carrier, winning_rid)) = chosen {
    ctx.parent_rid = Some(winning_rid.clone());
    let mut sources: Vec<Strng> = scan.candidates.iter()
        .filter(|(_, r)| r == &winning_rid)
        .map(|(c, _)| c.clone())
        .collect();
    sources.dedup();
    ctx.parent_rid_sources = sources;
    // Anomaly: any candidate disagrees with the chosen value.
    ctx.parent_rid_anomaly = scan.candidates.iter()
        .any(|(_, r)| r != &winning_rid);
}

// 5. Derive is_turn_boundary from body shape.
ctx.is_turn_boundary = match &req {
    LlmRequest::Messages(r) => messages_shape::is_turn_boundary(r),
    LlmRequest::Completions(r) => completions_shape::is_turn_boundary(r),
};

// 6. Emit LlmRequest audit phase with RID fields (extending the existing
//    LogEntry::new call to include the new fields).
```

Update the existing `LogEntry::new(Phase::LlmRequest { uctx, sctx }, ...)` call at the end of `on_llm_request` to include the new fields:

```rust
LogEntry::new(
    Phase::LlmRequest {
        uctx,
        sctx,
        rid: ctx.rid.clone(),
        parent_rid: ctx.parent_rid.clone(),
        is_turn_boundary: ctx.is_turn_boundary,
        parent_rid_sources: ctx.parent_rid_sources.clone(),
        parent_rid_anomaly: ctx.parent_rid_anomaly,
    },
    Some(&cid),
    &ctx.backend_name,
    ctx.trace_id.as_deref(),
)
.emit(self.cfg.log_level);
```

- [ ] **Step 7b.4: Add necessary imports at top of cidgar.rs**

```rust
use crate::governance::types::Rid;
use agent_core::prelude::Strng;
```

- [ ] **Step 7b.5: Run, see all 4 tests pass**

```bash
cargo test -p agentgateway --lib governance::cidgar::tests::f2_
```

Expected: 4 new tests pass.

- [ ] **Step 7b.6: Run full governance suite**

```bash
cargo test -p agentgateway --lib governance::
```

Expected: all tests pass. Some existing cidgar.rs tests may need to be updated if they construct LlmRequest phases — sweep them.

- [ ] **Step 7b.7: Commit**

```bash
git add crates/agentgateway/src/governance/cidgar.rs
git commit -m "feat(cidgar): CHG-26A f2 RID minting + parent_rid resolution

Wires the shape-helper scanners into on_llm_request:
  - Mint current RID via id_algorithm.
  - Scan body for _ib_rid carriers (c1/c2/c3).
  - Resolve parent_rid via priority chain (prev_resp_id > c1 > c3 > c2).
  - Populate parent_rid_sources (which carriers agreed) +
    parent_rid_anomaly (any carrier disagreed).
  - Derive is_turn_boundary from body shape.
  - Extend LlmRequest audit phase emission with new fields.

Minting runs UNCONDITIONALLY (independent of channel toggles); the
toggles gate emission/extraction, not observation. parent_rid resolution
also always runs — if no carriers found, parent_rid stays None.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: f3 PATH A — `_ib_rid` overwrite in tool_use.input (gated by `schema_rid`)

**Files:**
- Modify: `crates/agentgateway/src/governance/messages_shape.rs` (add `inject_rid_into_tool_use_response`)
- Modify: `crates/agentgateway/src/governance/completions_shape.rs` (add `inject_rid_into_tool_calls_response`)
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (gate the overwrite at f3 PATH A)

- [ ] **Step 8.1: Write failing test in `messages_shape.rs::tests`**

```rust
#[test]
fn inject_rid_into_tool_use_response_overwrites_each_tool_use() {
    use crate::governance::types::Rid;
    let mut resp: Response = serde_json::from_value(json!({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-haiku",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "name": "get_weather", "id": "tu_1",
             "input": {"city": "Paris"}}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5}
    })).unwrap();
    let rid = Rid::parse("ibr_aaaaaaaaaaaa").unwrap();
    let injected = inject_rid_into_tool_use_response(&mut resp, &rid);
    assert!(injected, "tool_use block present → returns true");
    // Verify the rid was written
    let v = serde_json::to_value(&resp).unwrap();
    let input = &v["content"][0]["input"];
    assert_eq!(input["_ib_rid"], "ibr_aaaaaaaaaaaa");
    assert_eq!(input["city"], "Paris");
}
```

- [ ] **Step 8.2: Run, see fail**

- [ ] **Step 8.3: Implement `inject_rid_into_tool_use_response`**

Add to `messages_shape.rs`:

```rust
/// CHG-26A f3 PATH A — overwrite `_ib_rid` in each tool_use block's input.
/// Returns true if any tool_use was found and modified, false otherwise.
/// Parallel to inject_cid_into_tool_use_response.
pub fn inject_rid_into_tool_use_response(resp: &mut Response, rid: &Rid) -> bool {
    let mut any = false;
    for content in resp.content.iter_mut() {
        if content.rest.get("type").and_then(|v| v.as_str()) == Some("tool_use") {
            if let Some(input) = content.rest.get_mut("input") {
                if let Some(obj) = input.as_object_mut() {
                    obj.insert("_ib_rid".into(), serde_json::Value::String(rid.as_str().to_owned()));
                    any = true;
                }
            }
        }
    }
    any
}
```

- [ ] **Step 8.4: Mirror in `completions_shape.rs`**

```rust
pub fn inject_rid_into_tool_calls_response(resp: &mut Response, rid: &Rid) -> bool {
    // ... mirror of inject_cid_into_tool_calls_response ...
}
```

Write a parallel test for completions shape.

- [ ] **Step 8.5: Run shape tests, see pass**

```bash
cargo test -p agentgateway --lib governance::messages_shape::tests::inject_rid_into_tool_use_response
cargo test -p agentgateway --lib governance::completions_shape::tests::inject_rid_into_tool_calls_response
```

- [ ] **Step 8.6: Gate the call in `cidgar.rs::on_llm_response` PATH A**

In `cidgar.rs` PATH A (around line 270-330), AFTER the `inject_cid_into_tool_use_response` (or detect-only) block, add an analogous RID injection gated by `schema_rid`:

```rust
// CHG-26A — also inject _ib_rid if schema_rid is on. Independent of schema_cid
// gating (caller may opt in to RID propagation through tool args without
// opting in to CID propagation, or vice versa). Both override prior values.
if self.cfg.channels.schema_rid {
    if let Some(rid) = ctx.rid.as_ref() {
        messages_shape::inject_rid_into_tool_use_response(r, rid);
    }
}
```

Mirror in Completions branch.

- [ ] **Step 8.7: Write end-to-end test**

In `cidgar.rs::tests`:

```rust
#[test]
fn f3_path_a_overwrites_ib_rid_when_schema_rid_true() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.schema_rid = true;
    let pipeline = CidGarPipeline::new(cfg);
    let mut ctx = GovContext::default();
    ctx.rid = Some(Rid::parse("ibr_aaaaaaaaaaaa").unwrap());
    ctx.cid = Some(Cid::parse("ibc_aaaaaaaaaaaa").unwrap());

    let mut resp: messages_shape::Response = serde_json::from_value(json!({
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-3-5-haiku", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "x", "id": "tu_1",
                     "input": {"city": "Paris"}}],
        "usage": {"input_tokens": 1, "output_tokens": 1}
    })).unwrap();
    pipeline.on_llm_response(
        LlmResponse::Messages(&mut resp),
        ctx.cid.as_ref().unwrap(),
        &ctx,
    );
    let v = serde_json::to_value(&resp).unwrap();
    assert_eq!(v["content"][0]["input"]["_ib_rid"], "ibr_aaaaaaaaaaaa");
}

#[test]
fn f3_path_a_omits_ib_rid_when_schema_rid_false() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.schema_rid = false;  // default
    let pipeline = CidGarPipeline::new(cfg);
    let mut ctx = GovContext::default();
    ctx.rid = Some(Rid::parse("ibr_aaaaaaaaaaaa").unwrap());
    ctx.cid = Some(Cid::parse("ibc_aaaaaaaaaaaa").unwrap());

    let mut resp: messages_shape::Response = serde_json::from_value(json!({
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-3-5-haiku", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "x", "id": "tu_1",
                     "input": {"city": "Paris"}}],
        "usage": {"input_tokens": 1, "output_tokens": 1}
    })).unwrap();
    pipeline.on_llm_response(
        LlmResponse::Messages(&mut resp),
        ctx.cid.as_ref().unwrap(),
        &ctx,
    );
    let v = serde_json::to_value(&resp).unwrap();
    assert!(v["content"][0]["input"].get("_ib_rid").is_none(),
        "_ib_rid must NOT appear when schema_rid=false");
}
```

- [ ] **Step 8.8: Run tests, see pass**

```bash
cargo test -p agentgateway --lib governance::cidgar::tests::f3_path_a
```

- [ ] **Step 8.9: Commit**

```bash
git add crates/agentgateway/src/governance/{messages_shape,completions_shape,cidgar}.rs
git commit -m "feat(cidgar): CHG-26A f3 PATH A inject _ib_rid into tool_use.input

Adds inject_rid_into_tool_use_response (messages) and
inject_rid_into_tool_calls_response (completions) helpers. Wired into
on_llm_response PATH A, gated by channels.schema_rid (independent of
schema_cid — operator can opt in to either or both).

End-to-end tests verify both gate states.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: f3 PATH B — combined marker emission

**Files:**
- Modify: `crates/agentgateway/src/governance/messages_shape.rs` (replace `append_text_marker_response` with `append_combined_marker_response`)
- Modify: `crates/agentgateway/src/governance/completions_shape.rs` (same)
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (PATH B branch)

- [ ] **Step 9.1: Write failing test in `messages_shape.rs::tests`**

```rust
#[test]
fn append_combined_marker_response_emits_both_cid_and_rid() {
    use crate::governance::marker::MarkerPayload;
    use crate::governance::types::{Cid, Rid};
    let mut resp: Response = serde_json::from_value(json!({
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-3-5-haiku", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Hello there."}],
        "usage": {"input_tokens": 1, "output_tokens": 1}
    })).unwrap();
    let payload = MarkerPayload {
        cid: Some(Cid::parse("ibc_aaaaaaaaaaaa").unwrap()),
        rid: Some(Rid::parse("ibr_bbbbbbbbbbbb").unwrap()),
    };
    append_combined_marker_response(&mut resp, &payload);
    let v = serde_json::to_value(&resp).unwrap();
    let appended = v["content"].as_array().unwrap().last().unwrap();
    let text = appended["text"].as_str().unwrap();
    assert!(text.contains("<!-- ib:cid=ibc_aaaaaaaaaaaa,rid=ibr_bbbbbbbbbbbb -->"));
}

#[test]
fn append_combined_marker_response_empty_payload_is_noop() {
    use crate::governance::marker::MarkerPayload;
    let mut resp: Response = serde_json::from_value(json!({
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-3-5-haiku", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Hello."}],
        "usage": {"input_tokens": 1, "output_tokens": 1}
    })).unwrap();
    let before = serde_json::to_value(&resp).unwrap();
    append_combined_marker_response(&mut resp, &MarkerPayload::default());
    let after = serde_json::to_value(&resp).unwrap();
    assert_eq!(before, after, "empty payload must be no-op");
}
```

- [ ] **Step 9.2: Run, see fail**

- [ ] **Step 9.3: Implement `append_combined_marker_response`**

In `messages_shape.rs`:

```rust
/// CHG-26C f3 PATH B — append a combined marker text block to a terminal
/// LLM response. No-op if payload is empty (caller short-circuit safe).
pub fn append_combined_marker_response(
    resp: &mut Response,
    payload: &crate::governance::marker::MarkerPayload,
) {
    use crate::governance::marker::make_combined_marker;
    let marker = make_combined_marker(payload);
    if marker.is_empty() {
        return;
    }
    // Strip the leading newline since we'll insert as a discrete content block
    let marker_text = marker.strip_prefix('\n').unwrap_or(&marker).to_owned();

    // Append a new text content block at the end of content[]
    let new_block = serde_json::json!({"type": "text", "text": marker_text});
    let content_val = serde_json::to_value(&new_block).unwrap();
    // Need to extend resp.content (which is Vec<ContentBlock>)
    // Use the existing ContentBlock representation; defer to existing append helper pattern
    // by parsing the json back as a ContentBlock.
    if let Ok(block) = serde_json::from_value::<crate::governance::messages_shape::ContentBlock>(content_val) {
        resp.content.push(block);
    }
}
```

(The exact shape of ContentBlock is in messages_shape.rs; adapt to the existing struct definition. The existing `append_text_marker_response` provides the template.)

Mirror in `completions_shape.rs`:

```rust
pub fn append_combined_marker_response(
    resp: &mut Response,
    payload: &crate::governance::marker::MarkerPayload,
) {
    use crate::governance::marker::make_combined_marker;
    let marker = make_combined_marker(payload);
    if marker.is_empty() {
        return;
    }
    // Completions appends to choices[0].message.content (a single string)
    // ... mirror of existing append_text_marker_response logic with the new marker text ...
}
```

- [ ] **Step 9.4: Gate the call in `cidgar.rs::on_llm_response` PATH B**

Currently PATH B looks like:

```rust
} else if self.cfg.channels.text_marker_cid {
    messages_shape::append_text_marker_response(r, cid);
}
```

Replace with:

```rust
} else {
    // PATH B — terminal text response. Build combined marker payload from
    // currently-enabled C2 toggles.
    let payload = crate::governance::marker::MarkerPayload {
        cid: if self.cfg.channels.text_marker_cid { Some(cid.clone()) } else { None },
        rid: if self.cfg.channels.text_marker_rid { ctx.rid.clone() } else { None },
    };
    if !payload.is_empty() {
        messages_shape::append_combined_marker_response(r, &payload);
    }
}
```

Mirror in Completions branch.

- [ ] **Step 9.5: Write end-to-end tests in cidgar.rs**

```rust
#[test]
fn f3_path_b_emits_combined_marker_when_both_toggles_on() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.text_marker_cid = true;
    cfg.channels.text_marker_rid = true;
    let pipeline = CidGarPipeline::new(cfg);
    let mut ctx = GovContext::default();
    ctx.rid = Some(Rid::parse("ibr_bbbbbbbbbbbb").unwrap());
    let cid = Cid::parse("ibc_aaaaaaaaaaaa").unwrap();

    let mut resp: messages_shape::Response = serde_json::from_value(json!({
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-3-5-haiku", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Hello."}],
        "usage": {"input_tokens": 1, "output_tokens": 1}
    })).unwrap();
    pipeline.on_llm_response(LlmResponse::Messages(&mut resp), &cid, &ctx);
    let v = serde_json::to_value(&resp).unwrap();
    let last_text = v["content"].as_array().unwrap()
        .last().unwrap()["text"].as_str().unwrap();
    assert!(last_text.contains("ib:cid=ibc_aaaaaaaaaaaa"));
    assert!(last_text.contains("rid=ibr_bbbbbbbbbbbb"));
}

#[test]
fn f3_path_b_emits_no_marker_when_both_toggles_off() {
    let cfg = CidGarConfig::default(); // both off by default
    let pipeline = CidGarPipeline::new(cfg);
    let mut ctx = GovContext::default();
    let cid = Cid::parse("ibc_aaaaaaaaaaaa").unwrap();

    let mut resp: messages_shape::Response = serde_json::from_value(json!({
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-3-5-haiku", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Hello."}],
        "usage": {"input_tokens": 1, "output_tokens": 1}
    })).unwrap();
    pipeline.on_llm_response(LlmResponse::Messages(&mut resp), &cid, &ctx);
    let v = serde_json::to_value(&resp).unwrap();
    // No additional content blocks appended
    assert_eq!(v["content"].as_array().unwrap().len(), 1);
}
```

- [ ] **Step 9.6: Run tests**

```bash
cargo test -p agentgateway --lib governance::messages_shape::tests::append_combined_marker_response
cargo test -p agentgateway --lib governance::completions_shape::tests::append_combined_marker_response
cargo test -p agentgateway --lib governance::cidgar::tests::f3_path_b
```

Expected: all pass.

- [ ] **Step 9.7: Commit**

```bash
git add crates/agentgateway/src/governance/{messages_shape,completions_shape,cidgar}.rs
git commit -m "feat(cidgar): CHG-26C f3 PATH B combined-carrier marker emission

Replaces single-correlator text-marker appends with combined-payload
append. Builds a MarkerPayload from currently-enabled C2 toggles
(text_marker_cid, text_marker_rid), then emits one marker carrying
whatever payload is non-empty. Empty payload → no marker emission.

End-to-end tests verify: both-on → combined marker; both-off → no
marker; mixed → single-key marker.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: f4 — extract `_ib_rid` for `parent_run_rid` audit field

**Files:**
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (extend f4 hook)

- [ ] **Step 10.1: Write failing test**

```rust
#[test]
fn f4_extracts_ib_rid_into_parent_run_rid() {
    let cfg = CidGarConfig::default();
    let pipeline = CidGarPipeline::new(cfg);
    let mut ctx = GovContext::default();
    let mut args = json!({
        "city": "Paris",
        "_ib_cid": "ibc_aaaaaaaaaaaa",
        "_ib_rid": "ibr_bbbbbbbbbbbb"
    });
    pipeline.on_tool_call_req(&mut args, &mut ctx);
    // After f4, both _ib_cid and _ib_rid should be stripped from args
    assert!(args.get("_ib_cid").is_none(), "_ib_cid stripped");
    assert!(args.get("_ib_rid").is_none(), "_ib_rid stripped");
    assert!(args.get("city").is_some(), "non-governance field preserved");
}
```

- [ ] **Step 10.2: Run, see fail (or partially pass — _ib_rid extraction may not be wired yet)**

- [ ] **Step 10.3: Add `_ib_rid` extraction in `on_tool_call_req`**

In `cidgar.rs::on_tool_call_req`, after the existing `pop_cid_from_value` / `pop_gar_from_value` / `pop_ib_ss_from_value` block, add:

```rust
// CHG-26A f4 — extract _ib_rid for the tool_call audit's parent_run_rid
// field. Like _ib_cid's strip, this is UNCONDITIONAL — defense in depth:
// operators who flip schema_rid: false should not leak _ib_rid downstream
// if it survives in conversation history.
let parent_run_rid = crate::governance::value_ops::pop_ib_rid_from_value(args);
```

Thread `parent_run_rid` into the `Phase::ToolCall` LogEntry emission later in the same function:

```rust
LogEntry::new(
    Phase::ToolCall {
        tool: /* existing */,
        args: /* existing */,
        gar: /* existing */,
        snapshot_hash,
        original_tool_name,
        correlation_lost,
        parent_run_rid,    // NEW
    },
    /* ... */
)
```

- [ ] **Step 10.4: Run, see pass**

- [ ] **Step 10.5: Commit**

```bash
git add crates/agentgateway/src/governance/cidgar.rs
git commit -m "feat(cidgar): CHG-26A f4 extract _ib_rid for parent_run_rid audit field

on_tool_call_req now pops _ib_rid from tool-call args (unconditionally,
mirroring the f4 _ib_cid strip's defense-in-depth pattern). The extracted
value flows into Phase::ToolCall's parent_run_rid field, identifying which
LLM run issued each tool call.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: f5 — add RID payload to C3 emission

**Files:**
- Modify: `crates/agentgateway/src/governance/cidgar.rs::on_tool_call_resp` (f5)

Approach: extend the existing C3 emission (resource block / text block) to carry combined payload `{cid, rid}` based on the two toggles.

- [ ] **Step 11.1: Add `originating_run_rid` to the f4 → f5 handoff in GovContext**

The originating LLM run's RID was extracted at f4 (`pop_ib_rid_from_value` in Task 10). To make it available at f5, store it in `GovContext` as `originating_run_rid`. In `types.rs`, add to `GovContext`:

```rust
/// CHG-26C — RID of the LLM run that issued the current tool call.
/// Set at f4 from the popped `_ib_rid` value; consumed at f5 to emit
/// C3 RID payload. Distinct from `parent_rid` (which is the parent of
/// the current LLM call); both can coexist on the same context.
pub originating_run_rid: Option<Rid>,
```

In `cidgar.rs::on_tool_call_req` (Task 10), populate it from the extraction:

```rust
let parent_run_rid = crate::governance::value_ops::pop_ib_rid_from_value(args);
ctx.originating_run_rid = parent_run_rid.clone();
```

- [ ] **Step 11.2: Write failing test in `cidgar.rs::tests`**

```rust
#[test]
fn f5_emits_combined_rid_payload_when_both_c3_toggles_on() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.resource_block_cid = true;
    cfg.channels.resource_block_rid = true;
    let pipeline = CidGarPipeline::new(cfg);

    let mut ctx = GovContext::default();
    let cid = Cid::parse("ibc_aaaaaaaaaaaa").unwrap();
    ctx.cid = Some(cid.clone());
    ctx.originating_run_rid = Some(Rid::parse("ibr_bbbbbbbbbbbb").unwrap());

    // Build a mock MCP tool/call response with a single text content block.
    let mut tool_resp_content: Vec<rmcp::model::Content> = vec![];
    pipeline.on_tool_call_resp(&mut tool_resp_content, &cid, false, &ctx);

    // After f5 emission, the content vec should have an additional block
    // carrying combined cid+rid payload. With mcp_marker_kind=Resource (default),
    // it's a resource block whose text field is JSON {"cid":..., "rid":...}.
    let appended_text = tool_resp_content.iter().find_map(|c| match c {
        rmcp::model::Content::Resource(r) => {
            r.resource.as_text().map(|t| t.text.clone())
        },
        _ => None,
    });
    let text = appended_text.expect("resource block appended");
    let v: serde_json::Value = serde_json::from_str(&text).expect("payload is JSON");
    assert_eq!(v["cid"], "ibc_aaaaaaaaaaaa");
    assert_eq!(v["rid"], "ibr_bbbbbbbbbbbb");
}

#[test]
fn f5_emits_cid_only_when_resource_block_rid_false() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.resource_block_cid = true;
    cfg.channels.resource_block_rid = false;
    let pipeline = CidGarPipeline::new(cfg);

    let mut ctx = GovContext::default();
    let cid = Cid::parse("ibc_aaaaaaaaaaaa").unwrap();
    ctx.cid = Some(cid.clone());
    ctx.originating_run_rid = Some(Rid::parse("ibr_bbbbbbbbbbbb").unwrap());

    let mut tool_resp_content: Vec<rmcp::model::Content> = vec![];
    pipeline.on_tool_call_resp(&mut tool_resp_content, &cid, false, &ctx);

    let appended_text = tool_resp_content.iter().find_map(|c| match c {
        rmcp::model::Content::Resource(r) => r.resource.as_text().map(|t| t.text.clone()),
        _ => None,
    });
    let text = appended_text.expect("resource block appended");
    let v: serde_json::Value = serde_json::from_str(&text).expect("payload is JSON");
    assert_eq!(v["cid"], "ibc_aaaaaaaaaaaa");
    assert!(v.get("rid").is_none(), "rid omitted when resource_block_rid=false");
}

#[test]
fn f5_emits_no_block_when_both_c3_toggles_off() {
    let cfg = CidGarConfig::default(); // both default false
    let pipeline = CidGarPipeline::new(cfg);

    let mut ctx = GovContext::default();
    let cid = Cid::parse("ibc_aaaaaaaaaaaa").unwrap();
    ctx.cid = Some(cid.clone());
    ctx.originating_run_rid = Some(Rid::parse("ibr_bbbbbbbbbbbb").unwrap());

    let mut tool_resp_content: Vec<rmcp::model::Content> = vec![];
    let before_len = tool_resp_content.len();
    pipeline.on_tool_call_resp(&mut tool_resp_content, &cid, false, &ctx);
    assert_eq!(tool_resp_content.len(), before_len, "no block appended when both toggles off");
}
```

- [ ] **Step 11.3: Run, see fail**

```bash
cargo test -p agentgateway --lib governance::cidgar::tests::f5_emits
```

Expected: FAIL — current `on_tool_call_resp` only handles CID-only payload + can't read `ctx.originating_run_rid`.

- [ ] **Step 11.4: Implement combined C3 payload in `on_tool_call_resp`**

In `cidgar.rs`, locate `on_tool_call_resp` (around lines 440-510 post-Design-A). The existing implementation emits a resource block with `text` = JSON `{"cid": "..."}` when `resource_block_cid` is true. Replace the payload-building logic with combined-carrier:

```rust
fn on_tool_call_resp(
    &self,
    content: &mut Vec<rmcp::model::Content>,
    cid: &Cid,
    is_error: bool,
    ctx: &GovContext,
) {
    // CHG-26C — build combined payload from currently-enabled C3 toggles.
    let cid_part = if self.cfg.channels.resource_block_cid {
        Some(cid.as_str().to_owned())
    } else {
        None
    };
    let rid_part = if self.cfg.channels.resource_block_rid {
        ctx.originating_run_rid.as_ref().map(|r| r.as_str().to_owned())
    } else {
        None
    };

    if cid_part.is_none() && rid_part.is_none() {
        // Empty payload — short-circuit, no emission.
        // Still emit the tool_response audit (existing behavior; preserve).
        // ... existing LogEntry::new(Phase::ToolResponse { ... }, ...) emit ...
        return;
    }

    // Build the JSON payload object with only the present keys.
    let mut payload_obj = serde_json::Map::new();
    if let Some(c) = &cid_part {
        payload_obj.insert("cid".into(), serde_json::Value::String(c.clone()));
    }
    if let Some(r) = &rid_part {
        payload_obj.insert("rid".into(), serde_json::Value::String(r.clone()));
    }
    let payload_json = serde_json::Value::Object(payload_obj).to_string();

    // Emit per mcp_marker_kind.
    match self.cfg.channels.mcp_marker_kind {
        McpMarkerKind::Resource | McpMarkerKind::Both => {
            // Append resource block with the combined payload in text.
            // URI scheme remains conv/{cid} when cid is present, else use rid/{rid}.
            let uri = match (&cid_part, &rid_part) {
                (Some(c), _) => format!("gateway-meta://conv/{}", c),
                (None, Some(r)) => format!("gateway-meta://run/{}", r),
                _ => unreachable!("guarded above"),
            };
            content.push(rmcp::model::Content::Resource(
                rmcp::model::EmbeddedResource {
                    resource: rmcp::model::ResourceContents::TextResourceContents(
                        rmcp::model::TextResourceContents {
                            uri,
                            mime_type: Some("application/json".into()),
                            text: payload_json.clone(),
                        },
                    ),
                    annotations: Some(rmcp::model::Annotations {
                        audience: Some(vec![rmcp::model::Role::Assistant]),
                        priority: Some(0.2),
                    }),
                },
            ));
        },
        McpMarkerKind::Text => {},
    }
    if matches!(
        self.cfg.channels.mcp_marker_kind,
        McpMarkerKind::Text | McpMarkerKind::Both
    ) {
        // Append text block with combined marker.
        let payload = crate::governance::marker::MarkerPayload {
            cid: cid_part.and_then(|c| Cid::parse(&c)),
            rid: rid_part.and_then(|r| Rid::parse(&r)),
        };
        let marker = crate::governance::marker::make_combined_marker(&payload);
        if !marker.is_empty() {
            let marker_text = marker.strip_prefix('\n').unwrap_or(&marker).to_owned();
            content.push(rmcp::model::Content::Text(rmcp::model::TextContent {
                text: marker_text,
                annotations: None,
            }));
        }
    }

    // Existing LogEntry::new(Phase::ToolResponse { ..., parent_run_rid: ctx.originating_run_rid.clone() }, ...)
    // emit — preserve, extending with parent_run_rid field.
}
```

(Exact `rmcp::model::*` type paths follow whatever the current cidgar.rs imports; this code block shows the structure. Adapt to actual types.)

- [ ] **Step 11.5: Run, see pass**

```bash
cargo test -p agentgateway --lib governance::cidgar::tests::f5_emits
```

Expected: 3 tests pass.

- [ ] **Step 11.6: Run full governance suite**

```bash
cargo test -p agentgateway --lib governance::
```

Expected: all pass. Any existing tests that called `on_tool_call_resp` without `ctx.originating_run_rid` set should still pass (None case handled).

- [ ] **Step 11.7: Commit**

```bash
git add crates/agentgateway/src/governance/{types,cidgar}.rs
git commit -m "feat(cidgar): CHG-26C f5 combined-payload C3 emission

on_tool_call_resp builds the C3 resource block / text content payload
from currently-enabled toggles (resource_block_cid, resource_block_rid).
Empty payload → no emission. Format mirrors C2's combined marker.

Adds GovContext::originating_run_rid for f4→f5 handoff of the RID of
the LLM run that issued the current tool call. Distinct from parent_rid
(which is the parent of the LLM call itself).

Three new tests pin both-on / cid-only / both-off behavior.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: CHG-26E — Validation warnings for asymmetric RID toggle states

**Files:**
- Modify: `crates/agentgateway/src/governance/validate.rs`

- [ ] **Step 12.1: Write failing tests in `validate.rs::tests`**

```rust
#[traced_test]
#[test]
fn warns_when_schema_rid_isolated() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.schema_rid = true;
    // text_marker_rid, resource_block_rid both stay false
    validate(&cfg);
    assert!(logs_contain(
        "schema_rid=true but text_marker_rid=false AND resource_block_rid=false"
    ));
}

#[traced_test]
#[test]
fn warns_when_rid_channels_on_but_schema_rid_off() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.text_marker_rid = true;
    cfg.channels.schema_rid = false;
    validate(&cfg);
    assert!(logs_contain(
        "RID propagated across LLM hops but not injected into MCP tool args"
    ));
}

#[traced_test]
#[test]
fn warns_when_rid_enabled_without_any_cid_channel() {
    let mut cfg = CidGarConfig::default();
    cfg.channels.schema_rid = true;
    cfg.channels.text_marker_rid = true;
    // ALL cid channels stay off
    validate(&cfg);
    assert!(logs_contain(
        "RID enabled without any CID propagation channel"
    ));
}
```

- [ ] **Step 12.2: Run, see fail**

- [ ] **Step 12.3: Add 3 RID warning patterns to `validate.rs`**

In `validate.rs::validate`, append after the existing 3 patterns:

```rust
// CHG-26E — RID-specific asymmetric-toggle warnings.

if cfg.channels.schema_rid
    && !cfg.channels.text_marker_rid
    && !cfg.channels.resource_block_rid
{
    tracing::warn!(
        "governance config: schema_rid=true but text_marker_rid=false AND \
         resource_block_rid=false. RID will be injected into MCP tool args \
         but cannot propagate across pure-text LLM turns. Enable \
         text_marker_rid and/or resource_block_rid for full coverage."
    );
}

if (cfg.channels.text_marker_rid || cfg.channels.resource_block_rid)
    && !cfg.channels.schema_rid
{
    tracing::warn!(
        "governance config: RID propagated across LLM hops (C2/C3) but not \
         injected into MCP tool args (schema_rid=false). tool_call audits \
         will lack parent_run_rid association. Enable schema_rid for full \
         coverage."
    );
}

if (cfg.channels.schema_rid
    || cfg.channels.text_marker_rid
    || cfg.channels.resource_block_rid)
    && !cfg.channels.schema_cid
    && !cfg.channels.text_marker_cid
    && !cfg.channels.resource_block_cid
{
    tracing::warn!(
        "governance config: RID enabled without any CID propagation channel. \
         RIDs lose their global uniqueness anchor (always interpreted within \
         a (cid, rid) pair). Enable at least one of: schema_cid, \
         text_marker_cid, resource_block_cid."
    );
}
```

- [ ] **Step 12.4: Run tests, see pass**

```bash
cargo test -p agentgateway --lib governance::validate
```

Expected: 3 new + 4 existing pass.

- [ ] **Step 12.5: Commit**

```bash
git add crates/agentgateway/src/governance/validate.rs
git commit -m "feat(cidgar): CHG-26E validate.rs RID-toggle asymmetry warnings

Three new tracing::warn! patterns at config-build:
  - schema_rid isolated (true, but C2/C3 RID toggles off)
  - RID C2/C3 enabled but schema_rid off (tool_call audits lose parent_run_rid)
  - RID enabled without any CID channel (RID lacks (cid, rid) anchor)

Extends the CHG-25G validate framework. Same per-route throttling pattern.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: AGW Phase 1 final verification

- [ ] **Step 13.1: Run full crate test suite**

```bash
cargo test -p agentgateway
```

Expected: all tests pass. Delta from Design A baseline (793 lib) is approximately +40-60 new tests.

- [ ] **Step 13.2: cargo fmt + clippy**

```bash
cargo fmt --check -p agentgateway
cargo clippy -p agentgateway --lib --tests --no-deps -- -D warnings
```

Both must be clean.

- [ ] **Step 13.3: Commit chain audit**

```bash
git log --oneline -15
```

Expected: 12 commits on top of Design A's tip `258a9430` (one per task: 1, 2, 3, 4, 5, 6, 7a, 7b, 8, 9, 10, 11, 12).

---

## Phase 2 — AGW docs branch updates (`ibfork/docs`)

**Working directory:** `/home/nixusr/ws/agw-gh/.worktrees/docs-v2`

---

### Task 14: Update cidgar spec — FILL §3.3, add §4.6, extend §5/§9/§10/§14/§15

**Files:** `docs/features/2026-04-19-governance-cidgar/spec.md`

- [ ] **Step 14.1: FILL §3.3 placeholder**

Replace the existing §3.3 placeholder with full content per spec §3.3 from the Design B spec (semantics, value shape `ibr_<12 hex>`, lifecycle minted-at-f2, parent_rid resolution, channels, audit fields, etc.).

Reference: `/home/nixusr/ws/aiplay/docs/superpowers/specs/2026-05-20-run-identity-design.md` §3 + the Hierarchy + lineage model section.

Mirror §3.1 (CID) structure for consistency.

- [ ] **Step 14.2: Add §4.6 RID lifecycle**

Parallel to §4 (CID Lifecycle):
- §4.6.1 Birth: f2 minting via `governance.id_algorithm`
- §4.6.2 Persistence Channels: same 3 carriers as CID, gated independently
- §4.6.3 Extraction and Resolution: priority chain (prev_resp_id → C1 → C3 → C2)
- §4.6.4 Death: end of single HTTP transaction (no persistence)
- §4.6.5 Cardinality: 1 RID per LLM call

- [ ] **Step 14.3: Extend §5.1 / §5.3 / §5.4 / §5.5 with RID behavior**

§5.1: schema injection of `_ib_rid` conditional on `schema_rid: true`.
§5.3 PATH A: tool_use.input overwrite conditional on `schema_rid: true`.
§5.3 PATH B: terminal marker payload builder logic (cid + rid based on toggles).
§5.4: f4 `_ib_rid` strip always runs (defense-in-depth).
§5.5: f5 C3 payload builder logic.

- [ ] **Step 14.4: Add §9.x new edge cases**

- §9.16 Responses + Conversations API parent_rid unreachable for new-turn Run 0 (conv-mode gap)
- §9.17 Multi-source disagreement on parent_rid (anomaly flag)
- §9.18 Truncated agent history — no `_ib_rid` reachable → parent_rid: None

- [ ] **Step 14.5: Extend §10 audit schema**

Document the new Phase fields:
- `Phase::LlmRequest`: rid, parent_rid, is_turn_boundary, parent_rid_sources, parent_rid_anomaly
- `Phase::LlmResponse` (new variant): rid, provider_response_id
- `Phase::ToolCall`: parent_run_rid
- `Phase::ToolResponse`: parent_run_rid

- [ ] **Step 14.6: Add §14.6 — describing this design's shipped state**

Parallel to §14.5 (Design A summary):

```
### 14.6 Run identity (CHG-26A through CHG-26E) — implemented

Five sibling changes landed under the run-identity design (2026-05-20):

- **CHG-26A**: `_ib_rid` correlator type + minting + f1/f3/f4 hooks.
- **CHG-26B**: Three RID payload toggles (schema_rid, text_marker_rid,
  resource_block_rid; default false).
- **CHG-26C**: Combined-carrier marker grammar (key=value pair walker).
- **CHG-26D**: Audit phase extensions (rid, parent_rid, is_turn_boundary,
  parent_run_rid, provider_response_id, parent_rid_sources, parent_rid_anomaly).
- **CHG-26E**: validate.rs warnings for asymmetric RID toggle states.
```

- [ ] **Step 14.7: Add §15.6 future considerations — `X-IB-RID` header passthrough**

Parallel to §15.5 (X-IB-CID header passthrough). Marked as future.

- [ ] **Step 14.8: Commit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/docs-v2
git add docs/features/2026-04-19-governance-cidgar/spec.md
git commit -m "docs(cidgar): CHG-26A..E update spec for run-identity infrastructure

Sections updated:
  §3.3 — FILL reserved placeholder with _ib_rid semantics
  §4.6 — new: RID lifecycle (birth, persistence, extraction, death)
  §5.1, §5.3 PATH A+B, §5.4, §5.5 — per-hook RID behavior
  §9.16/9.17/9.18 — new edge cases (conv-mode gap, multi-source
                    disagreement, truncated history)
  §10 — audit phase additions
  §14.6 — new: design summary
  §15.6 — new: future X-IB-RID header passthrough

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 15: Add CHG-26A..E rows to change-ledger

**Files:** `docs/change-ledger.md`

- [ ] **Step 15.1: Append 5 rows under a new section header**

Following the Design A precedent (commit `83856e00`), add a new themed section `## Run identity (CHG-26A through CHG-26E) — 2026-05-20`:

| CHG | Type | Description | feat branch | Status |
|---|---|---|---|---|
| 26A | [E] | `governance/types.rs` — Rid struct + parse/generate. `value_ops.rs` — pop_ib_rid_from_value/map + inject_rid_into_value. GovContext extension. f2 RID minting + parent_rid resolution + is_turn_boundary derivation. f3 PATH A `_ib_rid` overwrite. f4 `_ib_rid` extraction → parent_run_rid. | feat/cidgar | Implemented |
| 26B | [E] | `governance/config.rs::ChannelToggles` — add schema_rid + text_marker_rid + resource_block_rid (all default false). | feat/cidgar | Implemented |
| 26C | [M] | `governance/marker.rs` — evolve MARKER_RE + add MarkerPayload + make_combined_marker + strip_combined_marker. f3 PATH B combined emission. f5 C3 combined payload. | feat/cidgar | Implemented |
| 26D | [M] | `governance/log.rs` — extend Phase enum (LlmRequest +5 fields; LlmResponse new variant; ToolCall +parent_run_rid; ToolResponse +parent_run_rid). | feat/cidgar | Implemented |
| 26E | [E] | `governance/validate.rs` — 3 new RID-toggle warnings. | feat/cidgar | Implemented |

- [ ] **Step 15.2: Commit**

```bash
git add docs/change-ledger.md
git commit -m "docs(change-ledger): append CHG-26A..E — run-identity design

Five sibling changes landed under feat/cidgar (commits to follow on
the same branch atop 258a9430).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 16: Sweep per-feature-reimplementation-inventory.md

**Files:** `docs/per-feature-reimplementation-inventory.md`

- [ ] **Step 16.1: Extend governance-cidgar section with RID recipe**

Add a sub-section under the existing governance-cidgar entry titled "Run identity (CHG-26A..E)" listing:
- Files touched (parallel to CID's listed files)
- Recipe-grade spec for the 5 CHGs (struct fields, signatures, gate locations, audit fields)
- Pinned at `ibfork/feat/cidgar` tip (will be the post-Phase-1 SHA)

- [ ] **Step 16.2: Commit**

```bash
git add docs/per-feature-reimplementation-inventory.md
git commit -m "docs(inventory): extend governance-cidgar with RID infrastructure recipe

Recipe-grade spec for CHG-26A..E so a future reimplementation against
this inventory can rebuild RID infrastructure without re-reading the
design doc.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 17: Create features/2026-05-20-run-identity/ folder

**Files:** new feature folder + 4 files

- [ ] **Step 17.1: Create folder**

```bash
mkdir -p docs/features/2026-05-20-run-identity/
```

- [ ] **Step 17.2: Write README.md with status frontmatter**

```yaml
---
status: shipped
drafted: 2026-05-15
shipped_at: 2026-05-20
phase_dep: cross-phase
companion_docs:
  - features/2026-04-19-governance-cidgar/spec.md
  - features/2026-05-15-cidgar-config-cleanup/design.md
  - change-ledger.md
  - per-feature-reimplementation-inventory.md
---
```

Plus one-paragraph summary + scope bullet list + file index table.

- [ ] **Step 17.3: Transpose design.md from aiplay-side spec**

```bash
cp /home/nixusr/ws/aiplay/docs/superpowers/specs/2026-05-20-run-identity-design.md \
   docs/features/2026-05-20-run-identity/design.md
```

Then edit to:
- Drop the "Canonical doc location" section (this IS the canonical location now)
- Update status frontmatter to `status: shipped`, `shipped_at: 2026-05-20`
- Drop the "Why this was parked (historical)" section or keep abbreviated

- [ ] **Step 17.4: Transpose plan.md from aiplay-side plan**

```bash
cp /home/nixusr/ws/aiplay/docs/superpowers/plans/2026-05-20-run-identity-plan.md \
   docs/features/2026-05-20-run-identity/plan.md
```

Update header banner to indicate shipped status.

- [ ] **Step 17.5: Distill brainstorming.md**

~50-80 lines distilling key decisions from the Design B brainstorm + refresh sessions:
- Use case: audit/forensic replay
- Combined-carrier vs parallel-carrier
- ibr_<12hex> naming
- gar.mode-style ternary not used for rid (binary toggle each carrier)
- Conv-mode gap acknowledged as unaddressable by channel mechanisms
- Empirical Design A C2 evidence reinforced the priority chain ordering
- Refresh-time light-edits only (no architectural revisions)

- [ ] **Step 17.6: Commit**

```bash
git add docs/features/2026-05-20-run-identity/
git commit -m "docs(features): create 2026-05-20-run-identity/ feature folder

README + design + plan + brainstorming for the five-CHG run-identity
infrastructure (CHG-26A..E). Transposed from aiplay/docs/superpowers/
{specs,plans}/ which served as the brainstorm + plan output during the
2026-05-13..2026-05-20 sessions.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — Aiplay companion (`main` branch)

**Working directory:** `/home/nixusr/ws/aiplay`

---

### Task 18: Update aiplay `agw/config.yaml` — opt in to 3 new RID toggles

**Files:** `agw/config.yaml`

- [ ] **Step 18.1: Add 3 toggles to every governance route's channels block**

For each of the 10 governance routes, add to the `channels:` block:

```yaml
    schema_rid: true            # CHG-26B (default false)
    text_marker_rid: true       # CHG-26B (default false)
    resource_block_rid: true    # CHG-26B (default false)
```

Keep alphabetical-ish ordering or follow existing convention (schema_cid → schema_rid → text_marker_cid → text_marker_rid → resource_block_cid → resource_block_rid → snapshot_correlation).

- [ ] **Step 18.2: YAML lint**

```bash
python3 -c "import yaml; yaml.safe_load(open('agw/config.yaml').read()); print('YAML OK')"
```

- [ ] **Step 18.3: Verify count**

```bash
grep -c "schema_rid:" agw/config.yaml
grep -c "text_marker_rid:" agw/config.yaml
grep -c "resource_block_rid:" agw/config.yaml
```

All three should be 10 (or 11 if a comment block in the coordination header also mentions them).

- [ ] **Step 18.4: Commit (do NOT push yet — wait for image bump)**

```bash
cd /home/nixusr/ws/aiplay
git add agw/config.yaml
git commit -m "chore(aiplay): opt in to RID toggles on all governance routes (CHG-26B)

Adds schema_rid, text_marker_rid, resource_block_rid (all true) to the
channels block on every cid_gar route. Companion to AGW Design B.

DO NOT MERGE until the AGW image with these toggles understood
(post-CHG-26 build) is pulled. Image bump is the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 19: Bump AGW image (when published) + smoke verify

**Files:** `docker-compose.yaml`

- [ ] **Step 19.1: Locate image tag**

```bash
grep -n "agentgateway:" docker-compose.yaml | head -3
```

- [ ] **Step 19.2: Bump tag**

When the post-CHG-26 AGW image is published, bump the tag. Naming pattern follows Design A: e.g., `v1.0.1-ib.mcp.cidgar.rid` (the user owns the actual tagging).

```yaml
agentgateway:
  image: ghcr.io/agentgateway/agentgateway:<new-tag>
```

- [ ] **Step 19.3: Pull + smoke**

```bash
docker compose pull agentgateway
make down && make up
sleep 15
curl -s http://localhost:8000/health
```

- [ ] **Step 19.4: Verify zero validate warnings + RID end-to-end**

```bash
docker compose logs agentgateway 2>&1 | grep -iE "WARN|ERROR" | head -10
```

Expected: zero warnings (aiplay opts in to all RID toggles).

Run a smoke trial:

```bash
ROW_ID=$(curl -s http://localhost:8000/matrix | python3 -c "
import json, sys
rows = json.load(sys.stdin)
for r in rows:
    if r.get('mcp') == 'weather' and r.get('routing') == 'via_agw' and r.get('llm') == 'ollama':
        print(r['row_id']); break
")
TRIAL_ID=$(curl -s -X POST "http://localhost:8000/trials/${ROW_ID}/run" | python3 -c "import json,sys; print(json.load(sys.stdin)['trial_id'])")
sleep 60

curl -s "http://localhost:8000/trials/${TRIAL_ID}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
audits = data.get('audit_entries', [])

# Check RIDs in audit
rids = {a.get('rid') for a in audits if a.get('rid')}
print(f'Distinct RIDs: {rids}')
assert all(r.startswith('ibr_') for r in rids if r), f'bad shape: {rids}'

# Check parent_rid chain (every audit except first should have parent_rid)
chain = [(a.get('rid'), a.get('parent_rid')) for a in audits if a.get('rid')]
print(f'Lineage chain: {chain}')

# Check is_turn_boundary derivation
boundaries = sum(1 for a in audits if a.get('is_turn_boundary'))
print(f'Turn boundaries: {boundaries}')

# Check parent_run_rid on tool_call audits
tool_calls = [a for a in audits if a.get('phase') == 'tool_call']
for tc in tool_calls:
    prr = tc.get('parent_run_rid')
    print(f'tool_call.parent_run_rid: {prr}')
    assert prr and prr.startswith('ibr_')

# Check provider_response_id on llm_response audits
llm_resps = [a for a in audits if a.get('phase') == 'llm_response']
for lr in llm_resps:
    print(f'llm_response.provider_response_id: {lr.get(\"provider_response_id\")}')

print('✅ ALL RID fields populate correctly end-to-end')
"
```

Expected: All RIDs `ibr_<12 hex>` shape; parent_rid chain non-empty (after Run 0); `parent_run_rid` set on every tool_call; `provider_response_id` set on llm_response phases.

- [ ] **Step 19.5: Commit**

```bash
git add docker-compose.yaml
git commit -m "chore(aiplay): bump AGW image to <new-tag> (CHG-26A..E)

Picks up Design B run-identity infrastructure:
  - _ib_rid correlator (ibr_<12 hex>)
  - Combined-carrier marker grammar (<!-- ib:cid=...,rid=... -->)
  - 3 RID payload toggles (schema_rid, text_marker_rid, resource_block_rid)
  - Audit phase extensions (rid, parent_rid, is_turn_boundary,
    parent_run_rid, provider_response_id)
  - validate.rs warnings for asymmetric RID states

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4 — Cross-repo verification + final review

### Task 20: Cross-repo audit + comprehensive final review

- [ ] **Step 20.1: AGW source commit chain audit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/cidgar
git log --oneline -15
```

Expected: 12+ commits atop `258a9430` (Phase 1 of Design A's tip).

- [ ] **Step 20.2: AGW docs commit chain audit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/docs-v2
git log --oneline -10
```

Expected: 4 commits atop `fce33690` (Phase 2 of Design A's tip).

- [ ] **Step 20.3: Aiplay commit chain audit**

```bash
cd /home/nixusr/ws/aiplay
git log --oneline -5
```

Expected: 2 commits atop `96cc1e9` (config opt-in + image bump).

- [ ] **Step 20.4: Run aiplay full pytest**

```bash
docker run --rm \
  -v /home/nixusr/ws/aiplay:/aiplay \
  -w /aiplay \
  -e AIPLAY_DISABLE_AUDIT_TAIL=1 \
  aiplay-harness:local \
  python -m pytest tests/test_efficacy.py tests/test_trials.py tests/test_runner.py tests/test_api.py tests/test_audit_tail.py -q 2>&1 | tail -10
```

Expected: all pass (no regression from Design A baseline).

- [ ] **Step 20.5: Dispatch comprehensive final reviewer**

Dispatch a `superpowers:code-reviewer` agent covering:
- All 5 CHGs against the design spec
- Cross-repo consistency (aiplay config opt-ins match AGW toggle field names)
- Test design quality (positive + negative + multi-source-disagreement coverage)
- Audit shape consumer compat (skip_serializing_if works for pre-Design-B consumers)
- Conv-mode gap acknowledged and unaddressed (acceptable per spec)
- Acknowledged limitations documented in source AND spec (truncated history, multi-source disagreement, concurrent trials)

Pattern matches Design A's Task 17 final review.

- [ ] **Step 20.6: Report final state**

- Phase 1: ✓ Rust source (12 commits)
- Phase 2: ✓ AGW docs (4 commits)
- Phase 3: ✓ Aiplay companion (2 commits)
- Phase 4: ✓ Cross-repo verified, end-to-end smoke green

---

## Out of scope (deferred)

- **OTel span integration** — Design B emits enough audit shape to feed OTel; integration glue is separate.
- **Aiplay frontend lineage visualization** — separate enhancement after audit fields land.
- **New verdicts l/m/n** (run-lineage integrity, turn-boundary correctness, cross-API run continuity) — separate aiplay enhancement; design depends on this audit shape but is best treated as Design C.
- **`X-IB-RID` header passthrough** — deferred until instrumented-agent consumer surfaces.
- **Per-conversation RID chain reconstruction in conv mode** — anti-pattern (requires state).
- **`Cid::generate` removal** — convenience function stays for backwards-compat with tests/noop paths.

## Cross-references

- **Design spec:** `docs/superpowers/specs/2026-05-20-run-identity-design.md`
- **Prerequisite design (shipped):** `docs/superpowers/specs/2026-05-15-cidgar-config-cleanup-design.md` (canonical at `features/2026-05-15-cidgar-config-cleanup/design.md` on `ibfork/docs`)
- **AGW cidgar spec:** `features/2026-04-19-governance-cidgar/spec.md` on `ibfork/docs` (tip `8b06e2e2` post-Design-A)
- **Brainstorm conversation:** aiplay `docs/conversation-log.md` entries from 2026-05-13 → 2026-05-20
- **Provisional CHG numbers:** CHG-26A through CHG-26E (finalized at landing time)

---

## Post-execution follow-ups (added 2026-05-30)

Two CHG follow-ups landed after this plan executed (A..E), discovered via end-to-end smoke against the deployed `feat/cidgar` build. They are NOT in the task list above; both were one-commit fixes.

- **CHG-26F** — RID hook-boundary handoff (`feat/cidgar` `c7338181`). `LLMRequest.governance_rid` field (mirrors `governance_cid`) + f5 closure capture of `originating_run_rid`. Fixes `rid` lost across the fresh-`GovContext`-per-hook boundaries.
- **CHG-26G** — recency-based `parent_rid` resolution + same-position anomaly (`feat/cidgar` `8ebf73f1`). `RidScanResult.candidates` gained a message-position element; resolution picks the globally most-recent-by-position with carrier-trust as a same-position tiebreak; anomaly fires only on same-position conflicts.

AGW docs synced for both in `ibfork/docs` commit `3af1b118` (change-ledger + inventory recipe + spec §3.3/§4.6.3).
