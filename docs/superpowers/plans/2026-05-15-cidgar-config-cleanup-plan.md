# Cidgar Config Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the cidgar governance config surface to a uniform CHG-247-style opt-in pattern, rename the CID value prefix from `ib_<12 hex>` to `ibc_<12 hex>` to free up the namespace for future correlators, promote shared algorithm names (`id_algorithm`, `hash_algorithm`) to top-level config, replace the boolean GAR-required flag with a ternary mode, and add config-load validation warnings.

**Architecture:** Seven sibling changes (CHG-25A through CHG-25G) landing across three repos. Phase 1 lands AGW Rust source on `ibfork/feat/cidgar`. Phase 2 lands AGW docs (spec, change-ledger, feature folder) on `ibfork/docs`. Phase 3 lands the aiplay companion config + test fixture sweep + image bump on aiplay `main`. No backwards compatibility; clean break. Operators with in-flight conversations at upgrade time see fresh CIDs minted (functionally identical to spec §9.5 truncated-history case).

**Tech Stack:** Rust 2021 edition (AGW); `serde`/`serde_yaml` for config; `sha2` for hashing; `uuid` for ID generation; pytest + Python 3.12 (aiplay); docker-compose (aiplay stack).

---

## File Structure

### AGW source files (worktree: `/home/nixusr/ws/agw-gh/.worktrees/cidgar`)

| File | Responsibility | Change kind |
|---|---|---|
| `crates/agentgateway/src/governance/config.rs` | All config types (`CidGarConfig`, `CidConfig`, `GarConfig`, `ChannelToggles`, `CidGenerator`, `McpMarkerKind`). | Restructure — promote `id_algorithm`+`hash_algorithm` to `CidGarConfig`, remove `cid.generator`, replace `gar.schema_required: bool` with `gar.mode: GarMode`, rename `text_marker`/`resource_block` to `text_marker_cid`/`resource_block_cid`, add `schema_cid` toggle |
| `crates/agentgateway/src/governance/types.rs` | `Cid` type + `Channel` enum. | Modify — `Cid::generate` emits `ibc_<12 hex>`; `Cid::parse` accepts `ibc_<12 hex>` only |
| `crates/agentgateway/src/governance/cid.rs` | CID resolution priority chain (header → scan → generate). | Modify tests — fixture strings to `ibc_<hex>` |
| `crates/agentgateway/src/governance/marker.rs` | C2 text-marker grammar (`<!-- ib:cid=... -->`). | Modify regex — accept `ibc_` value prefix (marker key `ib:cid=` literal unchanged) |
| `crates/agentgateway/src/governance/gar.rs` | `inject_governance_into_schema()` and GAR helpers. | Modify signature — `gar_required: bool` → `mode: GarMode`; branch on three-way enum |
| `crates/agentgateway/src/governance/cidgar.rs` | Pipeline impl. f1/f3/f4/f5 hooks. | Modify — call `inject_governance_into_schema(schema, cfg.gar.mode)`, gate `_ib_cid` injection at f1 + f3 PATH A behind `cfg.channels.schema_cid`, read `cfg.id_algorithm` for minting, read renamed `cfg.channels.text_marker_cid`/`resource_block_cid` |
| `crates/agentgateway/src/governance/log.rs` | `schema_hash()` SHA-256 helper + audit phase types. | Modify — `schema_hash` reads `cfg.hash_algorithm` (threaded via context or arg) |
| `crates/agentgateway/src/governance/value_ops.rs` | `_ib_cid`/`_ib_gar`/`_ib_ss` pop helpers. | No production-code change; test fixture sweep |
| `crates/agentgateway/src/governance/validate.rs` | **NEW** — config-load validation warnings. | New file, ~80 LOC + tests |
| `crates/agentgateway/src/governance/mod.rs` | Module exports. | Modify — `pub mod validate;` |
| `crates/agentgateway/src/governance/{completions_shape,messages_shape}.rs` | Per-API shape walkers. | Test fixture sweep (CID literal strings) |

### AGW docs files (worktree: `/home/nixusr/ws/agw-gh/.worktrees/docs-v2`)

| File | Change kind |
|---|---|
| `docs/features/2026-04-19-governance-cidgar/spec.md` | Modify — update §3.1, §3.2, §4.1, §4.2, §5.1, §5.3, §7.1, §12.5; add §14.5 |
| `docs/change-ledger.md` | Modify — append CHG-25A through CHG-25G rows |
| `docs/per-feature-reimplementation-inventory.md` | Modify — sweep `governance-cidgar` section with new field names |
| `docs/features/2026-05-15-cidgar-config-cleanup/README.md` | New |
| `docs/features/2026-05-15-cidgar-config-cleanup/design.md` | New — transpose from aiplay-side `docs/superpowers/specs/2026-05-15-cidgar-config-cleanup-design.md` |
| `docs/features/2026-05-15-cidgar-config-cleanup/plan.md` | New — transpose from this plan file |
| `docs/features/2026-05-15-cidgar-config-cleanup/brainstorming.md` | New — distill from `aiplay/docs/conversation-log.md` 2026-05-13→15 session |

### Aiplay files (worktree: `/home/nixusr/ws/aiplay`)

| File | Change kind |
|---|---|
| `agw/config.yaml` | Modify — rename config fields on all 10 governance routes; opt in to `schema_cid: true`, `text_marker_cid: true`, `resource_block_cid: true`, `gar.mode: required` |
| `docker-compose.yaml` | Modify — bump `image:` tag for `agentgateway` service to the new AGW build |
| `tests/test_trials.py`, `tests/test_runner.py`, `tests/test_efficacy.py`, `tests/test_adapter_combo.py` | Modify — sweep `"ib_<12 hex>"` fixture literals to `"ibc_<12 hex>"` |
| `docs/enhancements.md` | Modify — sweep stale "Status: future" entries for shipped cleanup items |

---

## Phase 1 — AGW Rust source changes (`ibfork/feat/cidgar` branch)

**Working directory for all Phase 1 tasks:** `/home/nixusr/ws/agw-gh/.worktrees/cidgar`

**Test command (used throughout):** `cargo test -p agentgateway --lib governance::`

**Ordering rationale:** simpler / lower-blast-radius changes first (CHG-25C, 25D), then API-shape changes (25E, 25F), then the foundational CID rename (25A), then the new gate (25B), then the validator (25G). Each task ends with a commit; tasks compose into one PR or a stacked sequence on `ibfork/feat/cidgar`.

---

### Task 1: CHG-25C — Promote `cid.generator` → `governance.id_algorithm`

**Files:**
- Modify: `crates/agentgateway/src/governance/config.rs`
- Modify: `crates/agentgateway/src/governance/types.rs` (Cid::generate reads the algorithm)
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (call sites)

- [ ] **Step 1.1: Write the failing test** in `config.rs`'s `tests` module (after existing tests):

```rust
#[test]
fn yaml_id_algorithm_at_top_level_round_trips() {
    let yaml = r#"
kind: cid_gar
id_algorithm: uuid4_12
cid:
  header_passthrough: true
"#;
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else {
        panic!("expected CidGar");
    };
    assert!(matches!(c.id_algorithm, IdAlgorithm::Uuid4_12));
}

#[test]
fn yaml_cid_generator_field_no_longer_accepted() {
    let yaml = r#"
kind: cid_gar
cid:
  generator: uuid4_12
  header_passthrough: true
"#;
    let result: Result<GovernancePolicy, _> = serde_yaml::from_str(yaml);
    assert!(result.is_err(), "cid.generator must be rejected by deny_unknown_fields after relocation");
}
```

- [ ] **Step 1.2: Run tests to verify failure**

Run: `cargo test -p agentgateway --lib governance::config::tests::yaml_id_algorithm_at_top_level_round_trips`
Expected: FAIL with `IdAlgorithm` undefined / field unknown.

- [ ] **Step 1.3: Implement the relocation**

In `config.rs`:

- Remove the `generator: CidGenerator` field from `CidConfig`. Resulting `CidConfig`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields, rename_all = "snake_case")]
pub struct CidConfig {
    pub header_passthrough: bool,
}

impl Default for CidConfig {
    fn default() -> Self {
        Self { header_passthrough: true }
    }
}
```

- Rename enum `CidGenerator` → `IdAlgorithm` (single variant `Uuid4_12` unchanged):

```rust
#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum IdAlgorithm {
    #[default]
    Uuid4_12,
}
```

- Add `id_algorithm: IdAlgorithm` field to `CidGarConfig`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default, deny_unknown_fields, rename_all = "snake_case")]
pub struct CidGarConfig {
    pub log_level: LogLevel,
    pub id_algorithm: IdAlgorithm,
    pub cid: CidConfig,
    pub gar: GarConfig,
    pub channels: ChannelToggles,
}
```

- [ ] **Step 1.4: Update `Cid::generate` to read the algorithm**

In `types.rs`, change `Cid::generate()` to accept the algorithm (preserving the no-arg `generate()` for call sites that don't have config available is acceptable — see step 1.5):

```rust
impl Cid {
    /// Generate a fresh CID per spec §4.1.
    /// Algorithm selected by `IdAlgorithm`. Today `Uuid4_12` is the only variant —
    /// UUIDv4 + take first 12 hex chars. Caller threads `cfg.id_algorithm` through.
    pub fn generate_with(_algorithm: crate::governance::config::IdAlgorithm) -> Self {
        // Today only one variant exists; match is forward-compat scaffolding.
        let uuid = uuid::Uuid::new_v4();
        let hex = uuid.simple().to_string();
        let s = format!("ib_{}", &hex[..12]);  // value-shape rename happens in CHG-25A
        Self(Strng::from(s))
    }

    /// Convenience: generate using the default algorithm (Uuid4_12).
    /// Production paths should prefer `generate_with(cfg.id_algorithm)`.
    pub fn generate() -> Self {
        Self::generate_with(crate::governance::config::IdAlgorithm::default())
    }
    // ... rest unchanged
}
```

- [ ] **Step 1.5: Update production call sites in `cidgar.rs`**

Find every `Cid::generate()` call in non-test code (`grep -n 'Cid::generate' crates/agentgateway/src/governance/cidgar.rs`) and switch them to `Cid::generate_with(self.cfg.id_algorithm)`. Tests can keep `Cid::generate()` (default).

In `cidgar.rs`, the f2 hook `on_llm_request` lines around 230-240 (where CID gets generated when neither header nor scan provides one):

```rust
let cid = ctx
    .cid
    .clone()
    .or(scanned)
    .unwrap_or_else(|| Cid::generate_with(self.cfg.id_algorithm));
```

- [ ] **Step 1.6: Run tests to verify pass**

Run: `cargo test -p agentgateway --lib governance::`
Expected: all PASS, including the two new yaml round-trip tests. Pre-existing tests that referenced `cid.generator: uuid4_12` in YAML will now fail with "unknown field" — those need updating in this same task (sweep them):

```bash
grep -rn "generator: uuid4_12" crates/agentgateway/src/governance/ | grep -v "//"
```

Update each test's YAML fixture to use `id_algorithm: uuid4_12` at top level instead of `cid.generator: uuid4_12`. Re-run tests until green.

- [ ] **Step 1.7: Commit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/cidgar
git add crates/agentgateway/src/governance/{config,types,cidgar}.rs
git commit -m "feat(cidgar)!: CHG-25C promote cid.generator → governance.id_algorithm

Algorithm choice conceptually applies to any random-source hex correlator
AGW mints. Relocating to top level signals 'shared across correlators'
rather than 'owned by CID'. No backwards compatibility — old YAML configs
that set cid.generator will fail to load.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: CHG-25D — Add `governance.hash_algorithm: sha256`

**Files:**
- Modify: `crates/agentgateway/src/governance/config.rs`
- Modify: `crates/agentgateway/src/governance/log.rs` (`schema_hash` reads algorithm)
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (`compute_snapshot` reads algorithm)

- [ ] **Step 2.1: Write the failing test** in `config.rs`'s tests module:

```rust
#[test]
fn yaml_hash_algorithm_defaults_to_sha256() {
    let yaml = "kind: cid_gar";
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else {
        panic!("expected CidGar");
    };
    assert!(matches!(c.hash_algorithm, HashAlgorithm::Sha256));
}

#[test]
fn yaml_hash_algorithm_explicit_round_trips() {
    let yaml = r#"
kind: cid_gar
hash_algorithm: sha256
"#;
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else {
        panic!("expected CidGar");
    };
    assert!(matches!(c.hash_algorithm, HashAlgorithm::Sha256));
}
```

- [ ] **Step 2.2: Run tests to verify failure**

Run: `cargo test -p agentgateway --lib governance::config::tests::yaml_hash_algorithm`
Expected: FAIL — `HashAlgorithm` undefined.

- [ ] **Step 2.3: Add the enum + field**

In `config.rs`:

```rust
#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum HashAlgorithm {
    #[default]
    Sha256,
}
```

Extend `CidGarConfig`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default, deny_unknown_fields, rename_all = "snake_case")]
pub struct CidGarConfig {
    pub log_level: LogLevel,
    pub id_algorithm: IdAlgorithm,
    pub hash_algorithm: HashAlgorithm,   // NEW (CHG-25D)
    pub cid: CidConfig,
    pub gar: GarConfig,
    pub channels: ChannelToggles,
}
```

- [ ] **Step 2.4: Thread the algorithm through `schema_hash` and `compute_snapshot`**

In `log.rs`, change `schema_hash` signature to accept the algorithm:

```rust
pub fn schema_hash(tools: &[serde_json::Value], algorithm: crate::governance::config::HashAlgorithm) -> String {
    use crate::governance::config::HashAlgorithm;
    let canonical = canonicalize(tools);   // existing canonicalization helper
    let digest = match algorithm {
        HashAlgorithm::Sha256 => Sha256::digest(canonical.as_bytes()),
    };
    hex::encode(digest)
}
```

In `cidgar.rs`, update `compute_snapshot` similarly:

```rust
fn compute_snapshot(
    pre_schemas: &[serde_json::Value],
    algorithm: crate::governance::config::HashAlgorithm,
) -> (String, serde_json::Value) {
    // ... existing strip_meta / sort / canonicalize logic ...
    let canonical_str = serde_json::to_string(&canonical_body).unwrap_or_default();
    let digest = match algorithm {
        crate::governance::config::HashAlgorithm::Sha256 => Sha256::digest(canonical_str.as_bytes()),
    };
    let hex = hex::encode(digest);
    let truncated = hex[..SNAPSHOT_HASH_HEX_LEN].to_owned();
    (truncated, canonical_body)
}
```

In `cidgar.rs::on_tools_list_resp`, update the call sites:

```rust
let (snapshot_hash, snapshot_body) = if self.cfg.channels.snapshot_correlation {
    let (h, b) = compute_snapshot(&pre_schemas, self.cfg.hash_algorithm);
    (Some(h), Some(b))
} else {
    (None, None)
};
// ... and the schema_hash audit field:
LogEntry::new(
    Phase::ToolsList {
        tools: tool_names,
        schema_hash: schema_hash(&pre_schemas, self.cfg.hash_algorithm),
        snapshot_hash,
        snapshot_body,
    },
    // ...
)
```

- [ ] **Step 2.5: Run tests to verify pass**

Run: `cargo test -p agentgateway --lib governance::`
Expected: all PASS, including the two new hash_algorithm tests. If any tests called `schema_hash(&tools)` without the algorithm arg, update them to pass `HashAlgorithm::Sha256` (default).

- [ ] **Step 2.6: Commit**

```bash
git add crates/agentgateway/src/governance/{config,log,cidgar}.rs
git commit -m "feat(cidgar)!: CHG-25D add governance.hash_algorithm: sha256 config

Promotes SHA-256 from hard-coded constants (log.rs::schema_hash and
cidgar.rs::compute_snapshot) to operator-visible config. Single variant
today; forward-compat for BLAKE3 or hash-rotation discussions.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: CHG-25E — Replace `gar.schema_required: bool` with `gar.mode: GarMode` ternary

**Files:**
- Modify: `crates/agentgateway/src/governance/config.rs`
- Modify: `crates/agentgateway/src/governance/gar.rs` (signature + behavior of `inject_governance_into_schema`)
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (call site)

- [ ] **Step 3.1: Write failing tests** in `gar.rs`'s tests module (add after existing tests):

```rust
#[test]
fn inject_with_mode_required_adds_to_required_array() {
    use serde_json::Map;
    let mut schema: Map<String, Value> = serde_json::from_value(json!({
        "type": "object",
        "properties": {},
        "required": []
    })).unwrap();
    inject_governance_into_schema(&mut schema, crate::governance::config::GarMode::Required);
    let props = schema.get("properties").unwrap().as_object().unwrap();
    assert!(props.contains_key("_ib_gar"));
    let req = schema.get("required").unwrap().as_array().unwrap();
    assert!(req.iter().any(|v| v.as_str() == Some("_ib_gar")));
}

#[test]
fn inject_with_mode_optional_injects_property_but_not_required() {
    use serde_json::Map;
    let mut schema: Map<String, Value> = serde_json::from_value(json!({
        "type": "object",
        "properties": {},
        "required": []
    })).unwrap();
    inject_governance_into_schema(&mut schema, crate::governance::config::GarMode::Optional);
    let props = schema.get("properties").unwrap().as_object().unwrap();
    assert!(props.contains_key("_ib_gar"));
    let req = schema.get("required").unwrap().as_array().unwrap();
    assert!(!req.iter().any(|v| v.as_str() == Some("_ib_gar")));
}

#[test]
fn inject_with_mode_none_skips_gar_injection_entirely() {
    use serde_json::Map;
    let mut schema: Map<String, Value> = serde_json::from_value(json!({
        "type": "object",
        "properties": {},
        "required": []
    })).unwrap();
    inject_governance_into_schema(&mut schema, crate::governance::config::GarMode::None);
    let props = schema.get("properties").unwrap().as_object().unwrap();
    assert!(!props.contains_key("_ib_gar"),
        "_ib_gar must NOT appear in properties when mode=none");
    let req = schema.get("required").unwrap().as_array().unwrap();
    assert!(!req.iter().any(|v| v.as_str() == Some("_ib_gar")));
}
```

Also in `config.rs` tests:

```rust
#[test]
fn yaml_gar_mode_defaults_to_none() {
    let yaml = "kind: cid_gar";
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else {
        panic!("expected CidGar");
    };
    assert!(matches!(c.gar.mode, GarMode::None));
}

#[test]
fn yaml_gar_mode_explicit_round_trips() {
    let yaml = r#"
kind: cid_gar
gar:
  mode: required
"#;
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else {
        panic!("expected CidGar");
    };
    assert!(matches!(c.gar.mode, GarMode::Required));
}

#[test]
fn yaml_gar_schema_required_field_no_longer_accepted() {
    let yaml = r#"
kind: cid_gar
gar:
  schema_required: true
"#;
    let result: Result<GovernancePolicy, _> = serde_yaml::from_str(yaml);
    assert!(result.is_err(), "gar.schema_required must be rejected by deny_unknown_fields after rename");
}
```

- [ ] **Step 3.2: Run tests to verify failure**

Run: `cargo test -p agentgateway --lib governance::gar::tests::inject_with_mode`
Run: `cargo test -p agentgateway --lib governance::config::tests::yaml_gar_mode`
Expected: FAIL — `GarMode` undefined; `inject_governance_into_schema` second arg type mismatch.

- [ ] **Step 3.3: Add the enum + update GarConfig**

In `config.rs`:

```rust
#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum GarMode {
    /// `_ib_gar` is injected into `properties` AND appended to `required`.
    /// LLM provider strongly enforces; agent must supply audit reasoning.
    Required,
    /// `_ib_gar` is injected into `properties` only. LLM may skip.
    Optional,
    /// `_ib_gar` is NOT injected at all. Schema does not advertise the field.
    /// Audit captures `null` GAR for all tool calls.
    #[default]
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields, rename_all = "snake_case")]
pub struct GarConfig {
    pub mode: GarMode,
}

impl Default for GarConfig {
    fn default() -> Self {
        Self { mode: GarMode::None }
    }
}
```

- [ ] **Step 3.4: Update `inject_governance_into_schema` signature + behavior**

In `gar.rs`:

```rust
pub fn inject_governance_into_schema(
    schema: &mut serde_json::Map<String, serde_json::Value>,
    mode: crate::governance::config::GarMode,
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

    // _ib_cid is unchanged in this task — CHG-25B will gate this on schema_cid.
    props.insert(
        "_ib_cid".into(),
        json!({
            "type": "string",
            "description": "Auto-populated by gateway. Do not fill."
        }),
    );

    // CHG-25E ternary: inject _ib_gar only when mode != None.
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
}
```

- [ ] **Step 3.5: Update the call site in `cidgar.rs::on_tools_list_resp`**

Find the line that calls `inject_governance_into_schema(schema, self.cfg.gar.schema_required);` (around line 173) and replace:

```rust
crate::governance::gar::inject_governance_into_schema(schema, self.cfg.gar.mode);
```

- [ ] **Step 3.6: Update existing tests that pass `true`/`false` to `inject_governance_into_schema`**

In `gar.rs`'s existing tests, replace every `inject_governance_into_schema(&mut schema, true)` with `inject_governance_into_schema(&mut schema, GarMode::Required)` and every `inject_governance_into_schema(&mut schema, false)` with `inject_governance_into_schema(&mut schema, GarMode::Optional)`. Also update the existing test `inject_with_gar_required_false_still_injects_properties_but_not_required` — its semantics now match `mode: Optional`, name it accordingly.

In `cidgar.rs`'s existing test `schema_required_false_injects_properties_but_not_required` (around line 1190), update to use `cfg.gar.mode = GarMode::Optional`.

In `config.rs` tests, the existing `yaml_full_cid_gar_round_trips` test references `gar.schema_required: true` — update to `gar.mode: required` and assert `c.gar.mode == GarMode::Required`. Same for the `defaults_applied_when_omitted` test which asserts `c.gar.schema_required` — change to `assert!(matches!(c.gar.mode, GarMode::None))`.

- [ ] **Step 3.7: Run tests to verify pass**

Run: `cargo test -p agentgateway --lib governance::`
Expected: all PASS. Iterate on any remaining `schema_required` references until clean.

- [ ] **Step 3.8: Commit**

```bash
git add crates/agentgateway/src/governance/{config,gar,cidgar}.rs
git commit -m "feat(cidgar)!: CHG-25E gar.schema_required → gar.mode ternary (default none)

Replaces gar.schema_required: bool with gar.mode: required | optional | none.
The bool conflated whether-to-inject with whether-to-mark-required; the
ternary cleanly separates them.

  required: inject _ib_gar into properties + add to required (was schema_required: true)
  optional: inject _ib_gar into properties only         (was schema_required: false)
  none:     skip injection entirely                     (was not expressible)

Default flipped to 'none' for CHG-247 uniformity. Operators wanting audit
reasoning must opt in to 'required' or 'optional'.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: CHG-25F — Rename channel toggles: `text_marker` → `text_marker_cid`, `resource_block` → `resource_block_cid`

**Files:**
- Modify: `crates/agentgateway/src/governance/config.rs`
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (consumer sites)

- [ ] **Step 4.1: Write the failing test** in `config.rs` tests:

```rust
#[test]
fn yaml_renamed_channel_toggles_round_trip() {
    let yaml = r#"
kind: cid_gar
channels:
  text_marker_cid: true
  resource_block_cid: true
"#;
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else {
        panic!("expected CidGar");
    };
    assert!(c.channels.text_marker_cid);
    assert!(c.channels.resource_block_cid);
}

#[test]
fn yaml_old_channel_toggle_names_rejected() {
    let yaml = r#"
kind: cid_gar
channels:
  text_marker: true
"#;
    let result: Result<GovernancePolicy, _> = serde_yaml::from_str(yaml);
    assert!(result.is_err(), "channels.text_marker must be rejected after rename");
}
```

- [ ] **Step 4.2: Run tests to verify failure**

Run: `cargo test -p agentgateway --lib governance::config::tests::yaml_renamed_channel_toggles`
Expected: FAIL — `text_marker_cid` field doesn't exist on `ChannelToggles`.

- [ ] **Step 4.3: Rename fields in `ChannelToggles`**

In `config.rs`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields, rename_all = "snake_case")]
pub struct ChannelToggles {
    pub text_marker_cid: bool,        // RENAMED from text_marker (CHG-25F)
    pub resource_block_cid: bool,     // RENAMED from resource_block (CHG-25F)
    pub mcp_marker_kind: McpMarkerKind,
    pub snapshot_correlation: bool,
}

impl Default for ChannelToggles {
    fn default() -> Self {
        Self {
            text_marker_cid: false,
            resource_block_cid: false,
            mcp_marker_kind: McpMarkerKind::Resource,
            snapshot_correlation: false,
        }
    }
}
```

- [ ] **Step 4.4: Update consumer sites in `cidgar.rs`**

Find every `self.cfg.channels.text_marker` and `self.cfg.channels.resource_block` in non-test code:

```bash
grep -n "cfg.channels.text_marker\|cfg.channels.resource_block" crates/agentgateway/src/governance/cidgar.rs
```

Update each to use the renamed field. Examples (around lines 279, 318, 379):

```rust
// Before:  } else if self.cfg.channels.text_marker {
// After:
} else if self.cfg.channels.text_marker_cid {

// Before:  && self.cfg.channels.resource_block
// After:
&& self.cfg.channels.resource_block_cid
```

- [ ] **Step 4.5: Update test code in `cidgar.rs` that constructs configs**

Sweep all `cfg.channels.text_marker = ` and `cfg.channels.resource_block = ` references in tests (lines 517, 571, 675, 763, 790, 818, 850, 1032, 1313) to the renamed fields.

In `config.rs` tests, update existing assertions: `c.channels.text_marker` → `c.channels.text_marker_cid`; `c.channels.resource_block` → `c.channels.resource_block_cid`.

- [ ] **Step 4.6: Run tests to verify pass**

Run: `cargo test -p agentgateway --lib governance::`
Expected: all PASS.

- [ ] **Step 4.7: Commit**

```bash
git add crates/agentgateway/src/governance/{config,cidgar}.rs
git commit -m "refactor(cidgar)!: CHG-25F rename channel toggles for symmetry

  channels.text_marker     → channels.text_marker_cid
  channels.resource_block  → channels.resource_block_cid

Establishes symmetric naming ahead of the parked RID design's _rid
sibling additions. No semantic change — same gating behavior, renamed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: CHG-25A — CID value shape rename: `ib_<12 hex>` → `ibc_<12 hex>`

**Files:**
- Modify: `crates/agentgateway/src/governance/types.rs` (`Cid::generate_with`, `Cid::parse`, tests)
- Modify: `crates/agentgateway/src/governance/marker.rs` (regex grammar)
- Modify: `crates/agentgateway/src/governance/cid.rs` (tests)
- Sweep: all other files for `"ib_<12 hex>"` test fixtures

- [ ] **Step 5.1: Write the failing test** in `types.rs` tests module:

```rust
#[test]
fn cid_generate_emits_ibc_prefix() {
    let cid = Cid::generate();
    let s = cid.as_str();
    assert!(s.starts_with("ibc_"), "expected ibc_ prefix, got {s}");
    assert_eq!(s.len(), 16, "expected total length 16 (ibc_ + 12 hex), got {s}");
    assert!(s[4..].chars().all(|c| c.is_ascii_hexdigit()), "non-hex chars in {s}");
}

#[test]
fn cid_parse_rejects_legacy_ib_prefix() {
    assert!(Cid::parse("ib_7f3a2b91c4d8").is_none(),
        "legacy ib_ shape must be rejected — no back-compat per Design A");
}

#[test]
fn cid_parse_accepts_ibc_prefix() {
    assert!(Cid::parse("ibc_7f3a2b91c4d8").is_some());
}
```

- [ ] **Step 5.2: Run tests to verify failure**

Run: `cargo test -p agentgateway --lib governance::types::tests::cid_generate_emits_ibc_prefix`
Expected: FAIL — generate still emits `ib_`.

- [ ] **Step 5.3: Update `Cid::generate_with`**

In `types.rs`:

```rust
impl Cid {
    pub fn generate_with(_algorithm: crate::governance::config::IdAlgorithm) -> Self {
        let uuid = uuid::Uuid::new_v4();
        let hex = uuid.simple().to_string();
        let s = format!("ibc_{}", &hex[..12]);   // CHG-25A: ib_ → ibc_
        Self(Strng::from(s))
    }
    // generate() helper unchanged (delegates to generate_with).
}
```

- [ ] **Step 5.4: Update `Cid::parse` validation**

In `types.rs`:

```rust
pub fn parse(s: &str) -> Option<Self> {
    if s.len() != 16 || !s.starts_with("ibc_") {     // CHG-25A: was len 15, prefix "ib_"
        return None;
    }
    if !s[4..]                                        // CHG-25A: hex starts at index 4
        .chars()
        .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return None;
    }
    Some(Self(Strng::from(s)))
}
```

- [ ] **Step 5.5: Update marker regex in `marker.rs`**

The marker grammar key (`ib:cid=`) stays — only the VALUE format changes. The existing regex `r"\n?<!-- ib:cid=([a-zA-Z0-9_-]+) -->"` already accepts any `[a-zA-Z0-9_-]+` value, which covers both `ib_xxx` and `ibc_xxx`. The grammar itself doesn't strictly need updating — but `strip_text_marker` calls `Cid::parse` on the captured value, which now requires `ibc_` shape. Verify with a quick test:

```rust
#[test]
fn strip_text_marker_rejects_legacy_ib_value_in_marker() {
    let mut s = String::from("Hello\n<!-- ib:cid=ib_7f3a2b91c4d8 -->");
    let cid = strip_text_marker(&mut s);
    assert!(cid.is_none(),
        "legacy ib_ value inside marker must fail Cid::parse, returning None");
    assert_eq!(s, "Hello",
        "syntactic marker still removed regardless of value validity (§6.7)");
}

#[test]
fn strip_text_marker_accepts_ibc_value() {
    let mut s = String::from("Hello\n<!-- ib:cid=ibc_7f3a2b91c4d8 -->");
    let cid = strip_text_marker(&mut s);
    assert_eq!(cid.map(|c| c.as_str().to_owned()), Some("ibc_7f3a2b91c4d8".into()));
    assert_eq!(s, "Hello");
}
```

Update existing `make_text_marker_produces_correct_format` test to assert `ibc_` shape.

- [ ] **Step 5.6: Sweep CID literal fixtures across all files**

Run:

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/cidgar
grep -rn "ib_[a-f0-9]\{12\}" crates/agentgateway/src/governance/ | grep -v ".bak"
```

This produces ~80 hits across `types.rs`, `cid.rs`, `marker.rs`, `cidgar.rs`, `completions_shape.rs`, `messages_shape.rs`, `value_ops.rs`. For EACH match:
- Replace `ib_<12 hex>` with `ibc_<12 hex>` (preserve the hex value; just prepend `c`).
- Verify the change is in a string literal (not a comment that mentions the legacy shape historically).

Tool-assisted: a perl one-liner can do the bulk replace, then audit the diff:

```bash
perl -i -pe 's/("|=)ib_([a-f0-9]{12})/$1ibc_$2/g' crates/agentgateway/src/governance/*.rs
```

Then audit the diff:

```bash
git diff crates/agentgateway/src/governance/ | head -200
```

Look for false positives — comments mentioning the legacy shape historically should NOT be rewritten. Revert those by hand if any.

The existing test `cid_parse_rejects_wrong_length` expected `ib_7f3a2b91c4d800` (16 chars including underscore) — this needs to expect rejection of values that are 16 chars but DON'T start with `ibc_`. Adapt:

```rust
#[test]
fn cid_parse_rejects_wrong_length() {
    assert!(Cid::parse("ibc_7f3a").is_none());             // too short
    assert!(Cid::parse("ibc_7f3a2b91c4d800").is_none());   // too long (was 16-char rejection)
}
```

- [ ] **Step 5.7: Update `cid.rs` test `resolve_cid_generates_when_neither_header_nor_scan`**

Existing assertions check `starts_with("ib_")` and `len() == 15`. Update:

```rust
#[test]
fn resolve_cid_generates_when_neither_header_nor_scan() {
    let parts = empty_parts();
    let resolved = resolve_cid(&parts, None);
    assert!(resolved.as_str().starts_with("ibc_"));
    assert_eq!(resolved.as_str().len(), 16);
}
```

- [ ] **Step 5.8: Run tests to verify pass**

Run: `cargo test -p agentgateway --lib governance::`
Expected: all PASS. Iterate on any remaining `ib_<hex>` fixture strings until clean.

- [ ] **Step 5.9: Commit**

```bash
git add crates/agentgateway/src/governance/
git commit -m "feat(cidgar)!: CHG-25A rename CID value shape ib_<12hex> → ibc_<12hex>

Preserves the ib_ family marker (grep ^ib matches all governance ids),
introduces a single-char type discriminator (c = conversation), establishes
the i<type>_ pattern that future correlators (ibr_ for run, etc.) will
extend.

No backwards compatibility: Cid::parse rejects legacy ib_<hex> shape;
extractor finds no CID in conversation history containing pre-rename
markers and treats it as a §9.5 truncated-history case (mints fresh CID).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: CHG-25B — Add `channels.schema_cid` toggle, gate C1 CID injection (default false)

**Files:**
- Modify: `crates/agentgateway/src/governance/config.rs`
- Modify: `crates/agentgateway/src/governance/gar.rs` (`inject_governance_into_schema` accepts schema_cid)
- Modify: `crates/agentgateway/src/governance/cidgar.rs` (f3 PATH A overwrite gated)

- [ ] **Step 6.1: Write failing tests** in `gar.rs` tests:

```rust
#[test]
fn inject_with_schema_cid_false_omits_ib_cid_property() {
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
    );
    let props = schema.get("properties").unwrap().as_object().unwrap();
    assert!(!props.contains_key("_ib_cid"),
        "_ib_cid must NOT appear in properties when schema_cid=false");
}

#[test]
fn inject_with_schema_cid_true_includes_ib_cid_property() {
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
    );
    let props = schema.get("properties").unwrap().as_object().unwrap();
    assert!(props.contains_key("_ib_cid"));
}
```

In `config.rs`:

```rust
#[test]
fn yaml_schema_cid_defaults_to_false() {
    let yaml = "kind: cid_gar";
    let parsed: GovernancePolicy = serde_yaml::from_str(yaml).expect("parse");
    let GovernancePolicy::CidGar(c) = parsed else {
        panic!("expected CidGar");
    };
    assert!(!c.channels.schema_cid);
}
```

- [ ] **Step 6.2: Run tests to verify failure**

Run: `cargo test -p agentgateway --lib governance::gar::tests::inject_with_schema_cid`
Expected: FAIL — `inject_governance_into_schema` takes only 2 args; `schema_cid` field doesn't exist.

- [ ] **Step 6.3: Add `schema_cid` field to `ChannelToggles`**

In `config.rs`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields, rename_all = "snake_case")]
pub struct ChannelToggles {
    pub schema_cid: bool,                  // NEW (CHG-25B), default false
    pub text_marker_cid: bool,
    pub resource_block_cid: bool,
    pub mcp_marker_kind: McpMarkerKind,
    pub snapshot_correlation: bool,
}

impl Default for ChannelToggles {
    fn default() -> Self {
        Self {
            schema_cid: false,             // CHG-25B uniform opt-in default
            text_marker_cid: false,
            resource_block_cid: false,
            mcp_marker_kind: McpMarkerKind::Resource,
            snapshot_correlation: false,
        }
    }
}
```

- [ ] **Step 6.4: Update `inject_governance_into_schema` to accept and honor `schema_cid`**

In `gar.rs`:

```rust
pub fn inject_governance_into_schema(
    schema: &mut serde_json::Map<String, serde_json::Value>,
    mode: crate::governance::config::GarMode,
    schema_cid: bool,                                                       // NEW arg
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

    // CHG-25B: gate _ib_cid injection on schema_cid.
    if schema_cid {
        props.insert(
            "_ib_cid".into(),
            json!({
                "type": "string",
                "description": "Auto-populated by gateway. Do not fill."
            }),
        );
    }

    // CHG-25E: inject _ib_gar only when mode != None.
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
}
```

- [ ] **Step 6.5: Update the call site in `cidgar.rs::on_tools_list_resp`**

```rust
crate::governance::gar::inject_governance_into_schema(
    schema,
    self.cfg.gar.mode,
    self.cfg.channels.schema_cid,    // NEW
);
```

- [ ] **Step 6.6: Gate `_ib_cid` overwrite at f3 PATH A**

Find `on_llm_response`'s PATH A in `cidgar.rs`. Today it always sets `tool_use.input._ib_cid = cid`. Gate it:

```rust
// CHG-25B: only overwrite _ib_cid into tool_use.input when schema_cid is on.
// When off, the field was never injected into the schema in the first place,
// so writing it now would surprise the agent. Keeps the carrier symmetric:
// f1 inject ↔ f3 overwrite both gated together.
if self.cfg.channels.schema_cid {
    crate::governance::value_ops::insert_cid_into_value(input, &cid);
}
```

Locate the exact line(s) by `grep -n insert_cid_into_value crates/agentgateway/src/governance/cidgar.rs` and `grep -n "_ib_cid".into()` for any direct insertion. Apply the gate at each site.

- [ ] **Step 6.7: Update existing tests that call `inject_governance_into_schema` with 2 args**

Every call now needs the 3rd arg (`schema_cid: true` typically, to preserve test intent that the field is present):

```bash
grep -rn "inject_governance_into_schema" crates/agentgateway/src/governance/
```

For each call, append `, true` (most tests want `_ib_cid` injected).

In `cidgar.rs`, the call site at `on_tools_list_resp` already passes the runtime config (Step 6.5 done). Existing tests construct configs; ensure they set `cfg.channels.schema_cid = true` where they expect `_ib_cid` present in tool args.

- [ ] **Step 6.8: Add a positive test asserting end-to-end gating**

In `cidgar.rs` tests:

```rust
#[test]
fn schema_cid_false_omits_ib_cid_from_tool_use_input_overwrite() {
    let mut cfg = CidGarConfig::default();
    cfg.gar.mode = GarMode::None;
    cfg.channels.schema_cid = false;   // gate off
    // Build a pipeline with this config, run f3 PATH A against a response
    // with one tool_use block. Assert tool_use.input does NOT contain _ib_cid.
    //
    // [Full test scaffolding follows the existing pattern in cidgar.rs's tests
    //  module — copy a similar test like `cidgar_chat_then_tools_reuses_marker_cid_messages`
    //  and adapt to assert absence of _ib_cid post-f3.]
}
```

(The exact scaffolding mirrors the existing f3 PATH A tests — copy one and adapt.)

- [ ] **Step 6.9: Run tests to verify pass**

Run: `cargo test -p agentgateway --lib governance::`
Expected: all PASS, including new schema_cid gating tests.

- [ ] **Step 6.10: Commit**

```bash
git add crates/agentgateway/src/governance/{config,gar,cidgar}.rs
git commit -m "feat(cidgar)!: CHG-25B add channels.schema_cid toggle (default false)

Gates today's unconditional C1 _ib_cid injection at f1 (inputSchema.properties)
and f3 PATH A (tool_use.input overwrite). Default false matches CHG-247
uniform opt-in pattern across all channels.

Breaking change: operators relying on _ib_cid in MCP tool args must
explicitly set channels.schema_cid: true.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: CHG-25G — Add `validate.rs` with config-load warnings

**Files:**
- Create: `crates/agentgateway/src/governance/validate.rs`
- Modify: `crates/agentgateway/src/governance/mod.rs` (export the new module)
- Modify: `crates/agentgateway/src/governance/config.rs` (call validate at config build)

- [ ] **Step 7.1: Write the failing test** in a new test module — create `validate.rs` with the test first:

```rust
//! Config-load validation — emits structured tracing::warn! calls when
//! channel toggle combinations leave coverage gaps that are likely
//! misconfigurations.

use crate::governance::config::{CidGarConfig, GarMode};

/// Walk the config and emit warnings for asymmetric / partial-coverage
/// toggle states. Never errors — warnings only. Caller is the YAML
/// deserializer wrapper at config-build time.
pub fn validate(cfg: &CidGarConfig) {
    if !cfg.channels.schema_cid
        && !cfg.channels.text_marker_cid
        && !cfg.channels.resource_block_cid
        && !cfg.channels.snapshot_correlation
    {
        tracing::warn!(
            "governance config: all CID propagation channels disabled \
             (schema_cid, text_marker_cid, resource_block_cid all false). \
             AGW will mint a fresh CID on every LLM request (spec §9.5 \
             truncation case). Governance audit will not correlate across \
             turns. Confirm intentional."
        );
    }

    if cfg.gar.mode == GarMode::None
        && (cfg.channels.schema_cid
            || cfg.channels.text_marker_cid
            || cfg.channels.resource_block_cid
            || cfg.channels.snapshot_correlation)
    {
        tracing::warn!(
            "governance config: gar.mode=none disables LLM audit reasoning, \
             but other channels are enabled. Tool-call audits will carry null \
             GAR. Set gar.mode: optional (best-effort) or required (strict) \
             for audit reasoning value."
        );
    }

    if cfg.channels.schema_cid
        && !cfg.channels.text_marker_cid
        && !cfg.channels.resource_block_cid
    {
        tracing::warn!(
            "governance config: schema_cid=true but text_marker_cid=false \
             AND resource_block_cid=false. CID will be injected into MCP tool \
             args but cannot propagate across pure-text LLM turns. Enable \
             text_marker_cid and/or resource_block_cid for full coverage."
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tracing_test::traced_test;

    #[traced_test]
    #[test]
    fn warns_when_all_channels_disabled() {
        let cfg = CidGarConfig::default();   // all channels off by default
        validate(&cfg);
        assert!(logs_contain("all CID propagation channels disabled"));
    }

    #[traced_test]
    #[test]
    fn warns_when_gar_none_but_channels_on() {
        let mut cfg = CidGarConfig::default();
        cfg.channels.text_marker_cid = true;
        // gar.mode defaults to None
        validate(&cfg);
        assert!(logs_contain("gar.mode=none disables LLM audit reasoning"));
    }

    #[traced_test]
    #[test]
    fn warns_when_schema_cid_isolated() {
        let mut cfg = CidGarConfig::default();
        cfg.channels.schema_cid = true;
        // text_marker_cid, resource_block_cid both stay false
        validate(&cfg);
        assert!(logs_contain("schema_cid=true but text_marker_cid=false"));
    }

    #[traced_test]
    #[test]
    fn no_warnings_for_fully_enabled_config() {
        let mut cfg = CidGarConfig::default();
        cfg.channels.schema_cid = true;
        cfg.channels.text_marker_cid = true;
        cfg.channels.resource_block_cid = true;
        cfg.channels.snapshot_correlation = true;
        cfg.gar.mode = GarMode::Required;
        validate(&cfg);
        assert!(!logs_contain("all CID propagation channels disabled"));
        assert!(!logs_contain("gar.mode=none"));
        assert!(!logs_contain("schema_cid=true but"));
    }
}
```

- [ ] **Step 7.2: Add module export**

In `mod.rs`:

```rust
pub mod validate;
```

- [ ] **Step 7.3: Add `tracing-test` to dev-dependencies**

In `crates/agentgateway/Cargo.toml`, under `[dev-dependencies]`:

```toml
tracing-test = "0.2"
```

- [ ] **Step 7.4: Run tests to verify pass**

Run: `cargo test -p agentgateway --lib governance::validate`
Expected: 4 PASS.

- [ ] **Step 7.5: Call `validate` from config-build site**

In `config.rs`, in `GovernancePolicy::build`:

```rust
impl GovernancePolicy {
    pub fn build(self) -> Arc<dyn GovernancePipeline> {
        match self {
            Self::CidGar(c) => {
                crate::governance::validate::validate(&c);
                Arc::new(crate::governance::cidgar::CidGarPipeline::new(c))
            }
            Self::Noop => Arc::new(NoopGovernance),
        }
    }
}
```

(Per-route throttling — emit once per route instance — is a refinement: use a one-shot `AtomicBool` keyed by route name if log volume becomes problematic. Out of scope for v1; the simple invocation is fine.)

- [ ] **Step 7.6: Run full governance suite to confirm no regressions**

Run: `cargo test -p agentgateway --lib governance::`
Expected: all PASS.

- [ ] **Step 7.7: Commit**

```bash
git add crates/agentgateway/src/governance/{validate,mod,config}.rs crates/agentgateway/Cargo.toml
git commit -m "feat(cidgar): CHG-25G add validate.rs with config-load warnings

Three asymmetric-toggle patterns emit tracing::warn! at config-build:
  - all CID propagation channels disabled → fresh CID per request
  - gar.mode=none but other channels enabled → null GAR in audits
  - schema_cid isolated → CID survives tool turns but not text turns

Warnings only — never block startup. Operator-visible signal that the
config might not be doing what the operator thinks it's doing.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: AGW Phase 1 final verification

- [ ] **Step 8.1: Run the full crate test suite**

Run: `cargo test -p agentgateway`
Expected: all PASS. Note baseline test count vs pre-Phase-1 baseline (delta should be ~+15 from the new test cases added across CHG-25A through 25G).

- [ ] **Step 8.2: Run `cargo fmt --check` and `cargo clippy`**

```bash
cargo fmt --check -p agentgateway
cargo clippy -p agentgateway --lib --no-deps -- -D warnings 2>&1 | tail -30
```

Expected: no fmt drift; no clippy warnings on touched files.

- [ ] **Step 8.3: Inspect the commit chain**

```bash
git log --oneline -10
```

Expected: 7 commits on top of pre-Phase-1 HEAD, one per CHG-25A through 25G, in the order: 25C, 25D, 25E, 25F, 25A, 25B, 25G.

---

## Phase 2 — AGW docs branch updates (`ibfork/docs` branch)

**Working directory for all Phase 2 tasks:** `/home/nixusr/ws/agw-gh/.worktrees/docs-v2`

---

### Task 9: Update cidgar spec at `features/2026-04-19-governance-cidgar/spec.md`

**Files:** `docs/features/2026-04-19-governance-cidgar/spec.md`

- [ ] **Step 9.1: Update §3.1 `_ib_cid` — Conversation ID**

Replace the value-format paragraph (around line 50-60 in the current file):

```markdown
| Format | `ibc_` prefix + 12 lowercase hex chars (e.g. `ibc_7f3a2b91c4d8`). Total length 16 chars. Generated via `governance.id_algorithm` (currently `uuid4_12`: UUIDv4 → first 12 hex chars). |
```

Also update any example values within §3.1 from `ib_xxx` to `ibc_xxx`.

- [ ] **Step 9.2: Update §3.2 `_ib_gar` — Governance Audit Reasoning**

Replace the "required" framing with ternary semantics:

```markdown
### 3.2 `_ib_gar` — Governance Audit Reasoning

| Property | Value |
|---|---|
| Full name | `_ib_gar` |
| Schema injection mode | Controlled by `gar.mode` (ternary: `required` \| `optional` \| `none`). Default `none`. |
| Wire shape | JSON-as-string with 5 keys: `goal`, `need`, `impact`, `dspm`, `alt`. See §12.4 for description. |
| Lifecycle | Injected at f1 (when `mode != none`); read at f4; never returned to LLM. |
| When `mode: none` | The field is NOT advertised in the inputSchema. Audit captures `null` GAR. Tool calls proceed normally. |
| When `mode: optional` | Field appears in `properties` but not in `required`. LLM may skip. |
| When `mode: required` | Field appears in `properties` AND `required`. LLM provider strongly enforces. |
```

- [ ] **Step 9.3: Update §3.3 — reserve for `_ib_rid`**

Add a new §3.3 entry as a placeholder:

```markdown
### 3.3 `_ib_rid` — Run ID

**Reserved.** Run-identity infrastructure is parked in a sibling design (see `features/2026-05-15-run-identity-design/` once that design lands). This section is allocated for the eventual `_ib_rid` correlator specification.
```

- [ ] **Step 9.4: Update §4.1 Birth**

Insert reference to `governance.id_algorithm`:

> "CID is minted at f2 when neither header passthrough nor message-history scan yields a value. Minting uses `governance.id_algorithm` (currently `uuid4_12`: UUIDv4 → first 12 hex chars, prefixed `ibc_`)."

- [ ] **Step 9.5: Update §4.2 Persistence Channels**

For each channel description:
- Channel 1: note that injection is gated by `channels.schema_cid` (default `false`); when disabled, no `_ib_cid` appears in inputSchema or tool_use.input.
- Channel 2: rename references from `channels.text_marker` to `channels.text_marker_cid`.
- Channel 3: rename references from `channels.resource_block` to `channels.resource_block_cid`.

- [ ] **Step 9.6: Update §5.1, §5.3, §5.4, §5.5 flow specs**

- §5.1 step 1 (inject into inputSchema): note `_ib_cid` injection is conditional on `channels.schema_cid: true`; note `_ib_gar` injection is conditional on `gar.mode != none`.
- §5.3 PATH A step 1.b (overwrite `tool_use.input._ib_cid`): note conditional on `channels.schema_cid: true`.
- §5.4 step 1-2 (extract `_ib_cid` / `_ib_gar`): no semantic change; field may be absent if the corresponding toggle is off, audit captures null.

- [ ] **Step 9.7: Update §7.1 Tool Schema**

Replace the "LLM sees `_ib_cid` (optional) and `_ib_gar` (required)" text with the new conditional reality:

```markdown
The LLM sees `_ib_cid` if and only if `channels.schema_cid: true`. The LLM sees `_ib_gar` if and only if `gar.mode != none`; with `mode: required`, the field is in the schema's `required` array; with `mode: optional`, it's optional. Both correlators default to "not injected" per CHG-247 uniform opt-in.
```

- [ ] **Step 9.8: Update §12.5 Hash Function for Schema Hash**

Add reference to the new config knob:

```markdown
SHA-256 is the current hash. The algorithm is selected by `governance.hash_algorithm` (currently `sha256` — the only variant). Future BLAKE3 / hash-rotation discussions live behind this enum.
```

- [ ] **Step 9.9: Add §14.5 — describing this design's shipped state**

After §14.4 (CHG-247 entry), add:

```markdown
### 14.5 Cidgar config cleanup + uniform opt-in (CHG-25A through CHG-25G) — implemented

Seven sibling changes landed under the cidgar config cleanup design (2026-05-15):

- **CHG-25A**: CID value shape renamed from `ib_<12 hex>` to `ibc_<12 hex>`. No backwards compatibility — `Cid::parse` rejects the legacy shape.
- **CHG-25B**: New `channels.schema_cid` toggle (default `false`). Gates today's unconditional C1 `_ib_cid` injection at f1 (inputSchema) and f3 PATH A (tool_use.input).
- **CHG-25C**: Promoted `cid.generator` → `governance.id_algorithm`. Same single variant (`uuid4_12`); location signals "shared across correlators".
- **CHG-25D**: New `governance.hash_algorithm: sha256` field. Promotes the hard-coded SHA-256 calls in `log.rs::schema_hash` and `cidgar.rs::compute_snapshot` to operator-visible config.
- **CHG-25E**: Replaced `gar.schema_required: bool` with `gar.mode: required | optional | none` ternary. Default `none` for uniform opt-in.
- **CHG-25F**: Renamed `channels.text_marker` → `text_marker_cid`; `channels.resource_block` → `resource_block_cid`. Symmetric naming ahead of the parked RID design.
- **CHG-25G**: New `governance/validate.rs` emitting `tracing::warn!` at config-build for three asymmetric-toggle coverage gaps.

No back-compat; operators with in-flight conversations at upgrade time see fresh CIDs minted (§9.5 truncated-history case).
```

- [ ] **Step 9.10: Commit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/docs-v2
git add docs/features/2026-04-19-governance-cidgar/spec.md
git commit -m "docs(cidgar): CHG-25A..G update spec for config cleanup + value rename

Sections updated:
  §3.1 — CID value format ibc_<12hex>
  §3.2 — GAR ternary mode
  §3.3 — reserved for _ib_rid
  §4.1 — birth via governance.id_algorithm
  §4.2 — channel toggle renames + schema_cid gating
  §5.1, §5.3, §5.4, §5.5 — per-hook conditional injection
  §7.1 — what LLM sees, conditional on toggles
  §12.5 — schema hash algorithm via governance.hash_algorithm
  §14.5 — new: design summary

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Add CHG-25A through 25G rows to change-ledger

**Files:** `docs/change-ledger.md`

- [ ] **Step 10.1: Append CHG rows**

Identify the table the ledger uses (look at the last 5-10 rows to match style). Append 7 rows in CHG-NN order with the type codes from the design spec:

```markdown
| CHG-25A | [M] | `governance/types.rs` — `Cid::generate_with` emits `ibc_<12 hex>` prefix; `Cid::parse` accepts `ibc_<12 hex>` only. `governance/marker.rs` regex captures wider value alphabet but `Cid::parse` enforces shape — legacy `ib_<hex>` strings inside markers fail extraction. No back-compat. | — | feat/cidgar | Implemented |
| CHG-25B | [E] | `governance/config.rs::ChannelToggles` — add `schema_cid: bool`, default `false`. `governance/gar.rs::inject_governance_into_schema` signature extended; gates `_ib_cid` injection at f1. `governance/cidgar.rs::on_llm_response` PATH A gates `tool_use.input._ib_cid` overwrite. | — | feat/cidgar | Implemented |
| CHG-25C | [M] | `governance/config.rs` — relocate `cid.generator: CidGenerator` to top-level `id_algorithm: IdAlgorithm`. `governance/types.rs::Cid::generate_with` takes algorithm arg. No back-compat: legacy `cid.generator` field rejected by `deny_unknown_fields`. | — | feat/cidgar | Implemented |
| CHG-25D | [E] | `governance/config.rs` — add `hash_algorithm: HashAlgorithm` (single variant `Sha256`). `governance/log.rs::schema_hash` and `governance/cidgar.rs::compute_snapshot` take algorithm arg. | — | feat/cidgar | Implemented |
| CHG-25E | [M] | `governance/config.rs::GarConfig` — replace `schema_required: bool` with `mode: GarMode` (ternary: `Required \| Optional \| None`). Default `None`. `governance/gar.rs::inject_governance_into_schema` branches on mode. `governance/cidgar.rs` call site updated. No back-compat: `gar.schema_required` rejected. | — | feat/cidgar | Implemented |
| CHG-25F | [M] | `governance/config.rs::ChannelToggles` — rename `text_marker` → `text_marker_cid`, `resource_block` → `resource_block_cid`. `governance/cidgar.rs` all consumers updated. No back-compat: old field names rejected. | — | feat/cidgar | Implemented |
| CHG-25G | [A] | `governance/validate.rs` — new module. Emits `tracing::warn!` at config-build for three asymmetric-toggle patterns: all-CID-off, gar-none-with-channels-on, schema-cid-isolated. Called from `GovernancePolicy::build`. `crates/agentgateway/Cargo.toml` — add `tracing-test` to dev-dependencies. | — | feat/cidgar | Implemented |
```

- [ ] **Step 10.2: Commit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/docs-v2
git add docs/change-ledger.md
git commit -m "docs(change-ledger): append CHG-25A..G — cidgar config cleanup design

Seven sibling changes landed under feat/cidgar on 2026-05-15.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Sweep `per-feature-reimplementation-inventory.md` for the governance-cidgar section

**Files:** `docs/per-feature-reimplementation-inventory.md`

- [ ] **Step 11.1: Locate the governance-cidgar section**

```bash
grep -nE "^##|governance.cidgar" docs/per-feature-reimplementation-inventory.md | head -20
```

- [ ] **Step 11.2: Update field-name references**

Within the governance-cidgar section, sweep references from old to new field names:

- `cid.generator` → `governance.id_algorithm`
- `gar.schema_required` → `gar.mode`
- `channels.text_marker` → `channels.text_marker_cid`
- `channels.resource_block` → `channels.resource_block_cid`
- Any sample YAML showing the channels block — update to the new field names with `schema_cid: <bool>` added.
- CID value shape examples: `ib_<12 hex>` → `ibc_<12 hex>`.

- [ ] **Step 11.3: Add inventory pointer to the new validate.rs file**

Where the section lists files touched by cidgar, add `crates/agentgateway/src/governance/validate.rs` (new file under CHG-25G).

- [ ] **Step 11.4: Commit**

```bash
git add docs/per-feature-reimplementation-inventory.md
git commit -m "docs(inventory): sweep governance-cidgar for post-cleanup field names

Reflects CHG-25A..G renames so a future reimplementation against this
inventory uses the current field names and value shapes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Create the cidgar-config-cleanup feature folder

**Files:** create `docs/features/2026-05-15-cidgar-config-cleanup/{README,design,plan,brainstorming}.md`

- [ ] **Step 12.1: Create the folder + README.md**

```bash
mkdir -p docs/features/2026-05-15-cidgar-config-cleanup/
```

Write `docs/features/2026-05-15-cidgar-config-cleanup/README.md`:

```markdown
---
status: in-progress
drafted: 2026-05-15
shipped_at: null
phase_dep: cross-phase
companion_docs:
  - features/2026-04-19-governance-cidgar/spec.md
  - change-ledger.md
  - per-feature-reimplementation-inventory.md
---

# Cidgar config cleanup + uniform opt-in + value-shape rename

Seven sibling changes (CHG-25A through CHG-25G) bringing the cidgar governance config surface to a uniform CHG-247-style opt-in pattern, renaming CID value prefix `ib_` → `ibc_`, promoting algorithm names to top-level config, and replacing the boolean GAR-required flag with a ternary.

## Pinned foundation refs

| File | Pinned at SHA |
|---|---|
| `features/2026-04-19-governance-cidgar/spec.md` | TBD (current HEAD when this lands) |

## Scope

Seven CHG entries, no backwards compatibility, single atomic landing per repo.

- CHG-25A: CID value rename `ib_<12hex>` → `ibc_<12hex>`
- CHG-25B: `channels.schema_cid` gate (default false)
- CHG-25C: `cid.generator` → `governance.id_algorithm`
- CHG-25D: `governance.hash_algorithm` (new)
- CHG-25E: `gar.schema_required: bool` → `gar.mode: GarMode` ternary (default `none`)
- CHG-25F: rename `channels.text_marker` → `text_marker_cid`, `resource_block` → `resource_block_cid`
- CHG-25G: new `validate.rs` with config-load warnings

Explicitly **not** in scope: Run identity (RID) infrastructure — parked at `features/2026-05-15-run-identity-design/` once that design lands.

See `design.md` for full scope, rationale, and locked decisions.

## Files in this folder

| File | Role |
|---|---|
| `README.md` | This file |
| `design.md` | Design + rationale + locked decisions |
| `plan.md` | Implementation worklist (per-task TDD steps) |
| `brainstorming.md` | Decision log from the 2026-05-13 → 15 session |
```

- [ ] **Step 12.2: Transpose design.md from the aiplay-side spec**

```bash
cp /home/nixusr/ws/aiplay/docs/superpowers/specs/2026-05-15-cidgar-config-cleanup-design.md \
   /home/nixusr/ws/agw-gh/.worktrees/docs-v2/docs/features/2026-05-15-cidgar-config-cleanup/design.md
```

Then edit the new file to drop the "Canonical doc location" section (since now it IS the canonical doc location — the section is now meta-noise).

- [ ] **Step 12.3: Transpose plan.md from the aiplay-side plan**

```bash
cp /home/nixusr/ws/aiplay/docs/superpowers/plans/2026-05-15-cidgar-config-cleanup-plan.md \
   /home/nixusr/ws/agw-gh/.worktrees/docs-v2/docs/features/2026-05-15-cidgar-config-cleanup/plan.md
```

- [ ] **Step 12.4: Distill brainstorming.md from aiplay session logs**

Read `/home/nixusr/ws/aiplay/docs/conversation-log.md` for the 2026-05-13 → 2026-05-15 exchanges. Extract the decision points:

- Why combined-carrier (not parallel channels)
- Naming evolution `ib_rid_` → `r_` → `ibr_`, CID `ib_` → `ic_` → `ibc_`
- Why `ibc_` won (IB brand preservation, +1 char acceptable)
- Why `gar.mode` ternary
- Why `governance.id_algorithm` promoted (not `cid.generator`)
- Why no backwards-compat (clean break preferred)
- Why split Design A from parked Design B

Write the distilled version to `docs/features/2026-05-15-cidgar-config-cleanup/brainstorming.md`. Keep to 1-2 KB; this is the "why we decided this" trail, not a transcript.

- [ ] **Step 12.5: Commit**

```bash
git add docs/features/2026-05-15-cidgar-config-cleanup/
git commit -m "docs(features): create 2026-05-15-cidgar-config-cleanup/ feature folder

README + design + plan + brainstorming for the seven-CHG cleanup.
Transposed from aiplay/docs/superpowers/{specs,plans}/ which served as
the brainstorm output during the 2026-05-13→15 session.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — Aiplay companion (`aiplay/main` branch)

**Working directory for all Phase 3 tasks:** `/home/nixusr/ws/aiplay`

**Sequencing:** Phase 3 lands AFTER the AGW build is published with the Phase 1 changes (i.e., a tagged image like `ghcr.io/agentgateway/agentgateway:v1.0.1-ib.mcp.cidgar.cleanup`). The aiplay PR is atomic — config update + test sweep + image bump all in one commit chain.

---

### Task 13: Update aiplay `agw/config.yaml`

**Files:** `agw/config.yaml`

- [ ] **Step 13.1: Inspect current channels blocks**

```bash
cd /home/nixusr/ws/aiplay
grep -nB1 "channels:" agw/config.yaml | head -40
```

There are 10 governance routes. Each has a `cid:`, `gar:`, and `channels:` block.

- [ ] **Step 13.2: Update field names on each route**

For every governance route's policy YAML, apply these transformations:

```yaml
# Before:
governance:
  kind: cid_gar
  cid:
    generator: uuid4_12
    header_passthrough: true
  gar:
    schema_required: true
  channels:
    text_marker: true
    resource_block: true
    mcp_marker_kind: both
    snapshot_correlation: true

# After:
governance:
  kind: cid_gar
  id_algorithm: uuid4_12              # promoted from cid.generator (CHG-25C)
  hash_algorithm: sha256              # new (CHG-25D)
  cid:
    header_passthrough: true
  gar:
    mode: required                    # was schema_required: true (CHG-25E)
  channels:
    schema_cid: true                  # new gate, explicit opt-in (CHG-25B)
    text_marker_cid: true             # was text_marker (CHG-25F)
    resource_block_cid: true          # was resource_block (CHG-25F)
    mcp_marker_kind: both
    snapshot_correlation: true
```

Apply uniformly across all 10 routes. The coordination comment block at the top of `routes:` (explaining channel fields) extends with new field names and the schema_cid addition.

- [ ] **Step 13.3: Validate YAML loads cleanly**

Run a quick Python smoke:

```bash
python3 -c "import yaml; yaml.safe_load(open('agw/config.yaml').read()); print('OK')"
```

Expected: `OK`. No syntax errors.

- [ ] **Step 13.4: Commit (do NOT push yet — wait for image bump)**

```bash
git add agw/config.yaml
git commit -m "chore(aiplay): config opt-in to cidgar cleanup field names (CHG-25A..G)

Companion to AGW Design A. Updates 10 governance routes:
  cid.generator → governance.id_algorithm
  gar.schema_required: true → gar.mode: required
  channels.text_marker → text_marker_cid
  channels.resource_block → resource_block_cid
  + adds schema_cid: true (explicit opt-in to C1 CID injection)
  + adds hash_algorithm: sha256 (forward-compat algorithm declaration)

DO NOT MERGE until the AGW image with these field names is published
and the docker-compose.yaml image tag bump (next commit) is staged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 14: Sweep aiplay test fixtures for CID value shape

**Files:** `tests/test_trials.py`, `tests/test_runner.py`, `tests/test_efficacy.py`, `tests/test_adapter_combo.py`

- [ ] **Step 14.1: Identify all `ib_<hex>` literals**

```bash
grep -rn 'ib_[a-f0-9]\{3,\}' tests/ | grep -v __pycache__
```

- [ ] **Step 14.2: Sweep replacements**

For each match in test fixtures (NOT comments mentioning legacy shape historically), replace `ib_<value>` → `ibc_<value>`. Verify each replacement is in a string literal that represents a CID value.

```bash
# Be careful: NOT every "ib_" is a CID. Variables like ib_observed_cid_header
# are fine to keep. Manual review required.
```

Specific known sites:
- `tests/test_trials.py`: `cid="ib_abc123def456"` → `cid="ibc_abc123def456"`
- `tests/test_runner.py`: same pattern
- `tests/test_efficacy.py`: multiple — `"ib_abc"`, `"ib_abc123def456"`, embedded in `arguments` JSON strings
- `tests/test_adapter_combo.py`: `_observed_cid_header = "ib_aaaaaaaaaaaa"`, `X-IB-CID` header value

- [ ] **Step 14.3: Run targeted pytest**

```bash
docker run --rm \
  -v /home/nixusr/ws/aiplay:/aiplay \
  -w /aiplay \
  -e AIPLAY_DISABLE_AUDIT_TAIL=1 \
  aiplay-harness:local \
  python -m pytest tests/test_efficacy.py tests/test_trials.py tests/test_runner.py -v --tb=short
```

Expected: all pass with new shape literals.

For combo tests, use the combo image:

```bash
docker run --rm \
  -v /home/nixusr/ws/aiplay:/aiplay \
  -w /aiplay \
  -e AIPLAY_DISABLE_AUDIT_TAIL=1 \
  aiplay-adapter-combo:local \
  sh -c "pip install --quiet pytest-asyncio>=0.23.0 && python -m pytest tests/test_adapter_combo.py -v"
```

Expected: all pass.

- [ ] **Step 14.4: Commit**

```bash
git add tests/
git commit -m "test(aiplay): sweep CID literals ib_<hex> → ibc_<hex> for CHG-25A

Mechanical fixture-string rename — no test logic changes. Pinned-shape
assertions now expect the new 16-char ibc_<12hex> format.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 15: Sweep aiplay docs for stale "Status: future" cleanup-related entries

**Files:** `docs/enhancements.md`

- [ ] **Step 15.1: Identify relevant enhancements**

```bash
grep -nE "^## E|Status: future" docs/enhancements.md | head -50
```

Look for cidgar-side cleanup items now superseded by Design A. Likely candidates (search the file for "cleanup", "config", or similar):

- (Anything previously parked about renaming/cleaning channel fields)
- (Any "Status: future" entry that's now shipped by CHG-25A..G)

- [ ] **Step 15.2: Flip status lines for shipped items**

For each shipped item, change `**Status: future. ...**` to:

```markdown
**Status: shipped (CHG-25X).** Landed in the Design A cleanup on 2026-05-15. See `docs/superpowers/specs/2026-05-15-cidgar-config-cleanup-design.md` for context.
```

- [ ] **Step 15.3: Commit**

```bash
git add docs/enhancements.md
git commit -m "docs(aiplay): mark cidgar cleanup items shipped (CHG-25A..G)

Updates enhancements.md to reflect that items previously parked as
'Status: future' are now shipped under Design A.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 16: Bump AGW image tag in `docker-compose.yaml`

**Files:** `docker-compose.yaml`

- [ ] **Step 16.1: Locate the agentgateway image reference**

```bash
grep -nE "image:.*agentgateway" docker-compose.yaml
```

- [ ] **Step 16.2: Bump the tag**

Identify the new tag that the AGW build produces from Phase 1 commits. Conventionally `ghcr.io/agentgateway/agentgateway:v1.0.1-ib.mcp.cidgar.cleanup` (the actual tag is set by whoever publishes the AGW build; this plan assumes that tag is final by the time Phase 3 runs).

Replace the existing image reference:

```yaml
# Before:
agentgateway:
  image: ghcr.io/agentgateway/agentgateway:v1.0.1-ib.mcp.cidgar

# After:
agentgateway:
  image: ghcr.io/agentgateway/agentgateway:v1.0.1-ib.mcp.cidgar.cleanup
```

- [ ] **Step 16.3: Pull the new image to verify it exists**

```bash
docker compose pull agentgateway
```

Expected: pull succeeds. If it fails (image not yet published), wait for Phase 1 AGW build to publish before proceeding.

- [ ] **Step 16.4: Smoke test — bring up the stack and verify config loads**

```bash
make down
make up
sleep 10
docker compose logs agentgateway 2>&1 | grep -iE "error|warn" | head -20
```

Expected:
- No errors from YAML parsing (the renamed fields must be accepted)
- Three structured warnings from `validate.rs` MAY appear if any route is partially configured — but with the explicit opt-ins from Task 13, no warnings should fire on aiplay's default config

Open the UI:

```bash
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok"}`.

Run one trial through the matrix UI (or via curl):

```bash
curl -X POST http://localhost:8000/trials/$(curl -s http://localhost:8000/matrix | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['row_id'])")/run
```

Wait 30s, then fetch the trial JSON and verify the CID has `ibc_` prefix:

```bash
sleep 30
curl -s http://localhost:8000/trials | python3 -c "
import json, sys
data = json.load(sys.stdin)
for t in data[:1]:
    cid = t.get('audit_entries', [{}])[0].get('cid', '')
    print(f'CID shape: {cid!r}')
    assert cid.startswith('ibc_'), f'expected ibc_ prefix, got {cid!r}'
    print('PASS: CID has ibc_ prefix')
"
```

Expected: `PASS: CID has ibc_ prefix`.

- [ ] **Step 16.5: Commit**

```bash
git add docker-compose.yaml
git commit -m "chore(aiplay): bump AGW image to v1.0.1-ib.mcp.cidgar.cleanup

Picks up CHG-25A..G — cidgar config cleanup. New AGW build:
  - CID values are ibc_<12 hex>
  - gar.mode ternary
  - Renamed channel toggles (text_marker_cid, resource_block_cid)
  - schema_cid gate (explicit opt-in in agw/config.yaml)
  - governance.id_algorithm + hash_algorithm

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4 — End-to-end verification

### Task 17: Cross-repo smoke + commit-chain audit

- [ ] **Step 17.1: AGW source commit chain audit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/cidgar
git log --oneline -10
```

Expected: 7 commits for CHG-25A..G in order 25C, 25D, 25E, 25F, 25A, 25B, 25G.

- [ ] **Step 17.2: AGW docs commit chain audit**

```bash
cd /home/nixusr/ws/agw-gh/.worktrees/docs-v2
git log --oneline -10
```

Expected: 4 commits — spec update, change-ledger append, inventory sweep, feature folder creation.

- [ ] **Step 17.3: Aiplay commit chain audit**

```bash
cd /home/nixusr/ws/aiplay
git log --oneline -10
```

Expected: 4 commits — config opt-in, test fixture sweep, enhancements sweep, image bump.

- [ ] **Step 17.4: Aiplay full pytest run (touched suites)**

```bash
docker run --rm \
  -v /home/nixusr/ws/aiplay:/aiplay \
  -w /aiplay \
  -e AIPLAY_DISABLE_AUDIT_TAIL=1 \
  aiplay-harness:local \
  python -m pytest tests/test_efficacy.py tests/test_trials.py tests/test_runner.py tests/test_api.py -q
```

Expected: all PASS, baseline test count unchanged or slightly up from Phase 3 fixture sweep.

- [ ] **Step 17.5: Aiplay live trial — verify ibc_ propagation end-to-end**

With the stack up from Task 16.4, run a trial and inspect:

```bash
TRIAL_ID=$(curl -s -X POST "http://localhost:8000/trials/$(curl -s http://localhost:8000/matrix | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['row_id'])")/run" | python3 -c "import json,sys; print(json.load(sys.stdin)['trial_id'])")
sleep 30
curl -s "http://localhost:8000/trials/$TRIAL_ID" | python3 -c "
import json, sys
t = json.load(sys.stdin)
cids = {e['cid'] for e in t.get('audit_entries', []) if e.get('cid')}
print(f'Distinct CIDs: {cids}')
assert all(c.startswith('ibc_') for c in cids), f'Some CIDs not ibc_-shaped: {cids}'
print('PASS — all CIDs are ibc_-shaped')
"
```

Expected: `PASS — all CIDs are ibc_-shaped`.

- [ ] **Step 17.6: Final report**

Report back to the user:
- AGW Phase 1: ✓ (7 commits, N+15 tests passing on cidgar branch)
- AGW Phase 2: ✓ (4 commits on docs branch — spec, ledger, inventory, feature folder)
- Aiplay Phase 3: ✓ (4 commits on main — config, tests, enhancements, image bump)
- End-to-end smoke: ✓ (live trial produces ibc_<hex> CIDs)

---

## Out of scope

- **Run identity (RID) infrastructure** — parked. See `docs/superpowers/specs/2026-05-15-run-identity-design-PARKED.md` for the architecture; resume after this design lands.
- **`X-IB-RID` header passthrough** — deferred until an instrumented-agent consumer surfaces.
- **Per-conversation RID chain reconstruction in conv mode** — would require statefulness; anti-pattern.
- **OTel span integration** — separate piece sitting atop the eventual RID design.

## Cross-references

- Design spec: `docs/superpowers/specs/2026-05-15-cidgar-config-cleanup-design.md`
- Parked sibling design (RID): `docs/superpowers/specs/2026-05-15-run-identity-design-PARKED.md`
- Existing cidgar spec: `features/2026-04-19-governance-cidgar/spec.md` on `ibfork/docs` branch
- Recent CHG precedent (CHG-244/245/246/247): same architectural pattern of channel toggle additions + uniform opt-in flips
- Brainstorm conversation: aiplay `docs/conversation-log.md` entries from 2026-05-13 through 2026-05-15
