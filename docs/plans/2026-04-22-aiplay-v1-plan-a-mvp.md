# aiplay v1 — Plan A (MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working cidgar test harness with one LLM framework (langchain) and four MCP servers, where a user can click "Run" on a matrix row and see cidgar efficacy verdicts (presence + channel structure) computed from AGW audit logs.

**Architecture:** FastAPI harness-api coordinates between an AG-Grid browser UI (top), framework adapters over HTTP (middle), and a cidgar-patched AGW proxying LLM and MCP traffic (bottom). All stitched together by docker-compose. Trial state persists as JSON per trial. AGW audit logs consumed via Docker SDK stderr tail.

**Tech Stack:** Python 3.12, FastAPI, pytest, httpx, docker (Python SDK), PyYAML, langchain + langchain-openai (adapter), fastmcp (MCP servers reused from /my/ws/demo), AG-Grid Community (CDN), vanilla JS, docker-compose.

**Design doc:** `/my/ws/aiplay/docs/design.md` (sections 1-10).

**Plan B preview (out of scope here):** 5 additional adapters, verdicts c/d/e, turn-plan editor, clone-for-baseline, abort, compaction, force_state_ref, inject_ambient_cid, Anthropic + OpenAI routes.

---

## File structure (Plan A)

```
/my/ws/aiplay/
├── .gitignore                              [T1]
├── .env.example                            [T1]
├── Makefile                                [T4]
├── docker-compose.yaml                     [T5]
├── README.md                               # already exists
├── agw/
│   └── config.yaml                         [T3]
├── mcp/                                    [T2 — imported from /my/ws/demo]
│   ├── weather/{Dockerfile,main.py,requirements.txt}
│   ├── news/{Dockerfile,main.py,requirements.txt}
│   ├── library/{Dockerfile,main.py,requirements.txt}
│   └── fetch/{Dockerfile,main.py,requirements.txt}
├── harness/
│   ├── Dockerfile                          [T11]
│   ├── requirements.txt                    [T11]
│   ├── main.py                             [T11]
│   ├── api.py                              [T11]
│   ├── trials.py                           [T6]
│   ├── validator.py                        [T7]
│   ├── templates.py                        [T7]
│   ├── providers.py                        [T8]
│   ├── efficacy.py                         [T8]
│   ├── audit_tail.py                       [T9]
│   ├── runner.py                           [T10]
│   └── defaults.yaml                       [T7]
├── adapters/
│   └── langchain/
│       ├── Dockerfile                      [T12]
│       ├── requirements.txt                [T12]
│       ├── main.py                         [T12]
│       └── framework_bridge.py             [T12]
├── frontend/
│   ├── index.html                          [T13]
│   ├── config.js                           [T13]
│   ├── style.css                           [T13]
│   ├── app.js                              [T14]
│   └── drawer.js                           [T15]
├── data/
│   └── trials/                             # runtime; gitignored
├── tests/
│   ├── conftest.py                         [T6]
│   ├── test_trials.py                      [T6]
│   ├── test_validator.py                   [T7]
│   ├── test_templates.py                   [T7]
│   ├── test_providers.py                   [T8]
│   ├── test_efficacy.py                    [T8]
│   ├── test_audit_tail.py                  [T9]
│   ├── test_runner.py                      [T10]
│   ├── test_api.py                         [T11]
│   └── test_adapter_langchain.py           [T12]
└── docs/
    └── plans/
        └── 2026-04-22-aiplay-v1-plan-a-mvp.md   # this file
```

---

## Task 1: Project scaffold + git init

**Files:**
- Create: `.gitignore`, `.env.example`, empty `data/trials/`, empty `tests/`, empty `agw/`, empty `frontend/`, empty `harness/`, empty `adapters/langchain/`, empty `mcp/`

- [ ] **Step 1.1: Create directory structure**

```bash
cd /my/ws/aiplay
mkdir -p agw harness adapters/langchain frontend mcp data/trials tests
touch tests/__init__.py harness/__init__.py adapters/langchain/__init__.py
```

- [ ] **Step 1.2: Write .gitignore**

Create `/my/ws/aiplay/.gitignore`:

```
# Secrets
.env

# Runtime data
data/trials/*.json
data/settings.json
data/matrix.json

# Python
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
.venv/

# Editor
.DS_Store
.vscode/
.idea/

# Docker
*.log
```

- [ ] **Step 1.3: Write .env.example**

Create `/my/ws/aiplay/.env.example`:

```bash
# Copy to .env and fill in. Do NOT commit .env.

# OpenAI — for chatgpt provider (Plan B)
OPENAI_API_KEY=

# Anthropic — for claude.ai provider (Plan B)
ANTHROPIC_API_KEY=

# Google — for gemini provider (Plan B)
GOOGLE_API_KEY=
```

Plan A only exercises Ollama (no keys needed), but the template ships full so Plan B doesn't need `.env.example` updates.

- [ ] **Step 1.4: Initialize git**

```bash
cd /my/ws/aiplay
git init
git add .gitignore .env.example
git commit -m "chore(aiplay): initial scaffold + gitignore + env template"
```

Expected: 1 commit; `git status` shows the other pre-existing files (README.md, docs/, data/trials/) as untracked.

- [ ] **Step 1.5: Commit existing docs (they were already written before git init)**

```bash
cd /my/ws/aiplay
git add README.md docs/
git commit -m "docs(aiplay): design doc + brainstorming + README

Full design at docs/design.md (10 sections).
Decision log at docs/brainstorming.md.
User-facing docs at README.md."
```

- [ ] **Step 1.6: Commit directory placeholders**

```bash
touch data/trials/.gitkeep tests/__init__.py harness/__init__.py adapters/langchain/__init__.py
git add data/trials/.gitkeep tests/__init__.py harness/__init__.py adapters/langchain/__init__.py
git commit -m "chore(aiplay): add directory structure placeholders"
```

---

## Task 2: Import MCP services from demo

**Files:**
- Create: `mcp/weather/{Dockerfile,main.py,requirements.txt}` (copy from `/my/ws/demo/mcp/weather/`)
- Create: `mcp/news/{Dockerfile,main.py,requirements.txt}` (copy from demo)
- Create: `mcp/library/{Dockerfile,main.py,requirements.txt}` (copy from demo)
- Create: `mcp/fetch/{Dockerfile,main.py,requirements.txt}` (copy from demo)

No code changes — these are direct imports. Each is a fastmcp server exposing `streamable-http` transport on port 8000 inside the container.

- [ ] **Step 2.1: Copy weather MCP**

```bash
cd /my/ws/aiplay
cp -r /my/ws/demo/mcp/weather mcp/weather
ls mcp/weather/
# Expected: Dockerfile  main.py  requirements.txt
```

- [ ] **Step 2.2: Copy news MCP**

```bash
cp -r /my/ws/demo/mcp/news mcp/news
```

- [ ] **Step 2.3: Copy library MCP**

```bash
cp -r /my/ws/demo/mcp/library mcp/library
```

- [ ] **Step 2.4: Copy fetch MCP**

```bash
cp -r /my/ws/demo/mcp/fetch mcp/fetch
```

- [ ] **Step 2.5: Verify all 4 MCPs build clean (one sanity build)**

```bash
cd /my/ws/aiplay/mcp/weather
docker build -t aiplay-mcp-weather:test .
# Expected: build completes; no errors
docker rmi aiplay-mcp-weather:test
```

- [ ] **Step 2.6: Commit**

```bash
cd /my/ws/aiplay
git add mcp/
git commit -m "feat(aiplay): import MCP services from /my/ws/demo

Verbatim import of 4 fastmcp servers (weather, news, library, fetch)
from the demo project. No code changes. Each exposes streamable-http
transport on port 8000 internally. Auth-related MCPs (auth/) excluded
per design decision."
```

---

## Task 3: AGW config (`agw/config.yaml`)

**Files:**
- Create: `agw/config.yaml`

Defines AGW routes with cidgar governance policies. Plan A routes: `/llm/ollama/*` (governance on) + `/mcp/{weather,news,library,fetch}` (governance on). Anthropic/OpenAI routes deferred to Plan B.

- [ ] **Step 3.1: Write agw/config.yaml**

Create `/my/ws/aiplay/agw/config.yaml`:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/agentgateway/agentgateway/main/schema/config.json
#
# aiplay — AGW gateway config for cidgar Harness C MVP
#
# All routes governed (cidgar policy present). Plan A exposes:
#   - /llm/ollama/*        → host.docker.internal:11434 (Ollama on host)
#   - /mcp/{weather,news,library,fetch} → fastmcp containers
#
# Plan B adds /llm/chatgpt/, /llm/claude/, /llm/gemini/ routes.
#
# The cidgar governance policy is identical on every route — it's what makes
# this a cidgar test harness. Tweaking per-route is a Plan B exercise.

binds:
  - port: 8080
    listeners:
      - protocol: HTTP
        routes:

          # ── LLM route: Ollama (primary provider in Plan A) ──
          - name: llm-ollama
            matches:
              - path:
                  pathPrefix: /llm/ollama/
            policies:
              urlRewrite:
                path:
                  prefix: /
              governance:
                kind: cid_gar
                log_level: debug
                cid:
                  generator: uuid4_12
                  header_passthrough: true
                gar:
                  schema_required: true
                channels:
                  text_marker: true
                  resource_block: true
            backends:
              - ai:
                  name: ollama
                  provider:
                    openAI: {}
                  hostOverride: "host.docker.internal:11434"

          # ── MCP route: weather ──
          - name: mcp-weather
            matches:
              - path:
                  pathPrefix: /mcp/weather
            policies:
              urlRewrite:
                path:
                  prefix: /mcp
              governance:
                kind: cid_gar
                log_level: debug
                cid:
                  generator: uuid4_12
                gar:
                  schema_required: true
                channels:
                  text_marker: true
                  resource_block: true
            backends:
              - mcp:
                  prefixMode: always
                  targets:
                    - name: weather
                      mcp:
                        host: http://mcp-weather:8000/mcp

          # ── MCP route: news ──
          - name: mcp-news
            matches:
              - path:
                  pathPrefix: /mcp/news
            policies:
              urlRewrite:
                path:
                  prefix: /mcp
              governance:
                kind: cid_gar
                log_level: debug
            backends:
              - mcp:
                  prefixMode: always
                  targets:
                    - name: news
                      mcp:
                        host: http://mcp-news:8000/mcp

          # ── MCP route: library ──
          - name: mcp-library
            matches:
              - path:
                  pathPrefix: /mcp/library
            policies:
              urlRewrite:
                path:
                  prefix: /mcp
              governance:
                kind: cid_gar
                log_level: debug
            backends:
              - mcp:
                  prefixMode: always
                  targets:
                    - name: library
                      mcp:
                        host: http://mcp-library:8000/mcp

          # ── MCP route: fetch ──
          - name: mcp-fetch
            matches:
              - path:
                  pathPrefix: /mcp/fetch
            policies:
              urlRewrite:
                path:
                  prefix: /mcp
              governance:
                kind: cid_gar
                log_level: debug
            backends:
              - mcp:
                  prefixMode: always
                  targets:
                    - name: fetch
                      mcp:
                        host: http://mcp-fetch:8000/mcp
```

- [ ] **Step 3.2: Commit**

```bash
cd /my/ws/aiplay
git add agw/config.yaml
git commit -m "feat(aiplay): agw config with cidgar governance on all routes

Plan A routes: /llm/ollama/ (primary LLM provider) + /mcp/{weather,news,
library,fetch}. All carry cidgar kind:cid_gar policy with debug log_level
so harness audit tail captures full payloads. prefixMode:always on MCP
routes matches demo convention. Plan B will add /llm/chatgpt, /llm/claude,
/llm/gemini routes + ai.routes map for Anthropic Messages-shape dispatch."
```

---

## Task 4: Makefile

**Files:**
- Create: `Makefile`

- [ ] **Step 4.1: Write Makefile**

Create `/my/ws/aiplay/Makefile`:

```makefile
# aiplay — convenience targets

.PHONY: up down logs reset check-agw up-safe rotate-keys test help

help:
	@echo "aiplay targets:"
	@echo "  up           — bring up the stack"
	@echo "  down         — tear down (keeps data/ volume)"
	@echo "  logs         — follow agentgateway + harness-api logs"
	@echo "  reset        — clear data/trials/ (irreversible)"
	@echo "  check-agw    — verify agentgateway:cidgar image exists locally"
	@echo "  up-safe      — check-agw THEN up (fails fast if image missing)"
	@echo "  rotate-keys  — restart adapters to pick up .env changes"
	@echo "  test         — run pytest suite (harness + adapters)"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f agentgateway harness-api

reset:
	rm -f data/trials/*.json
	@echo "Cleared data/trials/"

check-agw:
	@docker image inspect agentgateway:cidgar > /dev/null 2>&1 \
		&& echo "✅ agentgateway:cidgar found" \
		|| (echo "❌ agentgateway:cidgar missing — build from agw-gh worktree first" && exit 1)

up-safe: check-agw up
	@echo "Stack up. UI at http://localhost:8000"

rotate-keys:
	@ADAPTERS=$$(docker compose ps --services 2>/dev/null | grep '^adapter-' || true); \
	if [ -z "$$ADAPTERS" ]; then \
		echo "No adapter services running"; \
	else \
		echo "Restarting adapters: $$ADAPTERS"; \
		docker compose restart $$ADAPTERS; \
	fi

test:
	cd harness && python -m pytest ../tests/ -xvs
```

- [ ] **Step 4.2: Test `make help` works**

```bash
cd /my/ws/aiplay
make help
# Expected: Multi-line output showing all targets
```

- [ ] **Step 4.3: Test `make check-agw` works (pass case)**

Assumes the user has already built `agentgateway:cidgar` per project convention.

```bash
make check-agw
# Expected: "✅ agentgateway:cidgar found"
# If "❌ agentgateway:cidgar missing", user needs to build it first:
#   cd /mnt/share/ws/agw-gh/.worktrees/cidgar && docker build -t agentgateway:cidgar .
```

- [ ] **Step 4.4: Commit**

```bash
cd /my/ws/aiplay
git add Makefile
git commit -m "feat(aiplay): add Makefile with up/down/logs/reset/check-agw targets

check-agw gate prevents silent stale-image runs; up-safe combines
check-agw + up. rotate-keys restarts adapter services only (harness
reads env on demand). test target forwards to pytest."
```

---

## Task 5: docker-compose.yaml

**Files:**
- Create: `docker-compose.yaml`

- [ ] **Step 5.1: Write docker-compose.yaml**

Create `/my/ws/aiplay/docker-compose.yaml`:

```yaml
networks:
  aiplay-network:
    driver: bridge
    name: aiplay_net

services:

  # ── Harness API (+ frontend) ──
  harness-api:
    build: ./harness
    image: aiplay-harness:local
    ports:
      - "8000:8000"
    environment:
      - AIPLAY_DEFAULT_ROUTING=via_agw
      - MAX_CONCURRENT_TRIALS=1
      - TURN_CAP=10
      - AGW_CONTAINER_NAME=agentgateway
      - AGW_ADMIN_URL=http://agentgateway:15000
      - DATA_DIR=/data
    volumes:
      - ./data:/data
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./frontend:/app/frontend:ro
      - ./harness/defaults.yaml:/app/defaults.yaml:ro
    depends_on:
      - agentgateway
    networks: [aiplay-network]
    restart: unless-stopped

  # ── AGW (cidgar branch, externally-built) ──
  agentgateway:
    image: agentgateway:cidgar
    command: ["-f", "/config/gateway-config.yaml"]
    ports:
      - "8080:8080"
      - "15000:15000"
    environment:
      - RUST_LOG=info,agentgateway::governance=debug
      - RUST_LOG_FORMAT=json
      - ADMIN_ADDR=0.0.0.0:15000
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./agw/config.yaml:/config/gateway-config.yaml:ro
    networks: [aiplay-network]
    restart: unless-stopped

  # ── Adapter: langchain (Plan A's only framework adapter) ──
  adapter-langchain:
    build: ./adapters/langchain
    image: aiplay-adapter-langchain:local
    environment:
      - ADAPTER_PORT=5001
      - AGW_LLM_BASE_URL_OLLAMA=http://agentgateway:8080/llm/ollama/v1
      - AGW_MCP_WEATHER=http://agentgateway:8080/mcp/weather
      - AGW_MCP_NEWS=http://agentgateway:8080/mcp/news
      - AGW_MCP_LIBRARY=http://agentgateway:8080/mcp/library
      - AGW_MCP_FETCH=http://agentgateway:8080/mcp/fetch
      - DIRECT_LLM_BASE_URL_OLLAMA=http://host.docker.internal:11434/v1
      - DIRECT_MCP_WEATHER=http://mcp-weather:8000/mcp
      - DIRECT_MCP_NEWS=http://mcp-news:8000/mcp
      - DIRECT_MCP_LIBRARY=http://mcp-library:8000/mcp
      - DIRECT_MCP_FETCH=http://mcp-fetch:8000/mcp
      - DEFAULT_OLLAMA_MODEL=qwen2.5:7b-instruct
    env_file: [.env]
    extra_hosts:
      - "host.docker.internal:host-gateway"
    depends_on:
      - agentgateway
    networks: [aiplay-network]
    restart: unless-stopped

  # ── MCP servers (all 4 imported from demo; all on aiplay-network) ──
  mcp-weather:
    build: ./mcp/weather
    image: aiplay-mcp-weather:local
    networks: [aiplay-network]
    restart: unless-stopped

  mcp-news:
    build: ./mcp/news
    image: aiplay-mcp-news:local
    networks: [aiplay-network]
    restart: unless-stopped

  mcp-library:
    build: ./mcp/library
    image: aiplay-mcp-library:local
    networks: [aiplay-network]
    restart: unless-stopped

  mcp-fetch:
    build: ./mcp/fetch
    image: aiplay-mcp-fetch:local
    networks: [aiplay-network]
    restart: unless-stopped
```

- [ ] **Step 5.2: Validate YAML syntax**

```bash
cd /my/ws/aiplay
docker compose config > /dev/null
# Expected: no output = valid
# If error: YAML syntax error or service reference issue
```

- [ ] **Step 5.3: Commit**

```bash
git add docker-compose.yaml
git commit -m "feat(aiplay): docker-compose topology — 7 services on aiplay-network

Services: harness-api (UI + API on :8000), agentgateway (cidgar image on
:8080+:15000, externally built), adapter-langchain (internal :5001),
4 MCP servers (internal :8000 each, reused from demo). AGW receives
RUST_LOG_FORMAT=json for L1 audit capture. MCP services on same network
so adapter can reach them directly under routing=direct (post Plan B)."
```

---

## Task 6: Harness — trials.py + persistence

**Files:**
- Create: `harness/trials.py`
- Create: `tests/test_trials.py`
- Create: `tests/conftest.py`

- [ ] **Step 6.1: Write the failing test (`tests/conftest.py` + `tests/test_trials.py`)**

Create `/my/ws/aiplay/tests/conftest.py`:

```python
"""Shared pytest fixtures for aiplay harness tests."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make harness/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))


@pytest.fixture
def tmp_data_dir(monkeypatch):
    """Isolate trial JSON writes in a temp directory per test."""
    with tempfile.TemporaryDirectory() as d:
        data_dir = Path(d) / "trials"
        data_dir.mkdir()
        monkeypatch.setenv("DATA_DIR", str(Path(d)))
        yield Path(d)
```

Create `/my/ws/aiplay/tests/test_trials.py`:

```python
"""Tests for harness/trials.py — Trial/Turn dataclasses + JSON persistence."""
from pathlib import Path

import pytest

from trials import Trial, Turn, AuditEntry, TrialStore, TrialConfig, TurnPlan


def test_trial_round_trip(tmp_data_dir: Path):
    """Create a Trial, save, load — fields preserved."""
    cfg = TrialConfig(
        framework="langchain", api="chat",
        stream=False, state=False,
        llm="ollama", mcp="weather",
        routing="via_agw",
    )
    plan = TurnPlan(turns=[{"kind": "user_msg", "content": "hi"}])
    trial = Trial(
        trial_id="test-trial-001",
        config=cfg,
        turn_plan=plan,
        status="running",
    )
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    loaded = store.load("test-trial-001")
    assert loaded.trial_id == "test-trial-001"
    assert loaded.config.framework == "langchain"
    assert loaded.status == "running"
    assert loaded.turn_plan.turns[0]["content"] == "hi"


def test_trial_append_turn(tmp_data_dir: Path):
    """Appending a turn mutates + persists without clobber."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    trial = Trial(trial_id="t2", config=cfg, turn_plan=TurnPlan(turns=[]))
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    turn = Turn(turn_id="turn-001", turn_idx=0, kind="user_msg",
                request={"body": {"x": 1}}, response={"body": {"y": 2}})
    store.append_turn("t2", turn)

    loaded = store.load("t2")
    assert len(loaded.turns) == 1
    assert loaded.turns[0].turn_id == "turn-001"
    assert loaded.turns[0].request["body"]["x"] == 1


def test_trial_append_audit(tmp_data_dir: Path):
    """Audit entries accumulate per trial."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    trial = Trial(trial_id="t3", config=cfg, turn_plan=TurnPlan(turns=[]))
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    entry = AuditEntry(
        trial_id="t3", turn_id="turn-001",
        phase="llm_request", cid="ib_abc123def456",
        backend="ollama", raw={"body": {}},
    )
    store.append_audit("t3", entry)

    loaded = store.load("t3")
    assert len(loaded.audit_entries) == 1
    assert loaded.audit_entries[0].cid == "ib_abc123def456"


def test_trial_list(tmp_data_dir: Path):
    """List all trials; sorts by created_at desc."""
    store = TrialStore(tmp_data_dir / "trials")
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    store.save(Trial(trial_id="older", config=cfg, turn_plan=TurnPlan(turns=[])))
    store.save(Trial(trial_id="newer", config=cfg, turn_plan=TurnPlan(turns=[])))

    all_trials = store.list_all()
    assert len(all_trials) == 2
    ids = {t.trial_id for t in all_trials}
    assert ids == {"older", "newer"}


def test_trial_load_missing(tmp_data_dir: Path):
    """Loading a missing trial raises FileNotFoundError."""
    store = TrialStore(tmp_data_dir / "trials")
    with pytest.raises(FileNotFoundError):
        store.load("nonexistent")
```

- [ ] **Step 6.2: Run test to confirm it fails**

```bash
cd /my/ws/aiplay
python -m pytest tests/test_trials.py -xvs
# Expected: ImportError: No module named 'trials' OR ModuleNotFoundError
```

- [ ] **Step 6.3: Write `harness/trials.py`**

Create `/my/ws/aiplay/harness/trials.py`:

```python
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
```

- [ ] **Step 6.4: Run tests to confirm they pass**

```bash
cd /my/ws/aiplay
python -m pytest tests/test_trials.py -xvs
# Expected: 5 passed
```

- [ ] **Step 6.5: Commit**

```bash
git add harness/trials.py tests/test_trials.py tests/conftest.py
git commit -m "feat(aiplay): trial + turn dataclasses + JSON persistence

TrialStore handles atomic write via tmp+rename. Trial carries config +
turn_plan + status + turns[] + audit_entries[] + verdicts. Schema
matches design §2.6 JSON sketch. 5 tests pass."
```

---

## Task 7: Harness — validator + templates + defaults.yaml

**Files:**
- Create: `harness/validator.py`
- Create: `harness/templates.py`
- Create: `harness/defaults.yaml`
- Create: `tests/test_validator.py`
- Create: `tests/test_templates.py`

- [ ] **Step 7.1: Write failing validator test**

Create `/my/ws/aiplay/tests/test_validator.py`:

```python
"""Tests for harness/validator.py — validate row configs."""
from validator import validate


def test_chat_completion_forces_state_false():
    """API=chat → state column must be False and disabled."""
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
    })
    assert "state" in result["disabled_cells"]
    assert result["forced_values"]["state"] is False
    assert result["runnable"] is True


def test_responses_conv_forces_state_true():
    """API=responses+conv → state forced to True."""
    result = validate({
        "framework": "langchain", "api": "responses+conv",
        "stream": False, "state": True,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    })
    assert "state" in result["disabled_cells"]
    assert result["forced_values"]["state"] is True


def test_messages_api_forces_state_false():
    """API=messages → state forced to False."""
    result = validate({
        "framework": "langchain", "api": "messages",
        "stream": False, "state": False,
        "llm": "claude", "mcp": "NONE", "routing": "via_agw",
    })
    assert "state" in result["disabled_cells"]
    assert result["forced_values"]["state"] is False


def test_none_llm_and_none_mcp_is_not_runnable():
    """No LLM + no MCP → row is not runnable."""
    result = validate({
        "framework": "NONE", "api": "NONE",
        "stream": False, "state": False,
        "llm": "NONE", "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is False


def test_none_llm_disables_api_stream_state():
    """LLM=NONE → api/stream/state all disabled."""
    result = validate({
        "framework": "NONE", "api": "NONE",
        "stream": False, "state": False,
        "llm": "NONE", "mcp": "weather", "routing": "via_agw",
    })
    for cell in ("api", "stream", "state", "provider"):
        assert cell in result["disabled_cells"]


def test_invalid_combo_api_responses_stream_off_state_on_is_valid():
    """Responses with state but no stream is valid."""
    result = validate({
        "framework": "autogen", "api": "responses",
        "stream": False, "state": True,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    })
    assert result["runnable"] is True
    # State is allowed on responses, not forced
    assert "state" not in result["disabled_cells"]


def test_missing_provider_key_disables_option():
    """If env shows chatgpt key missing, chatgpt option is in disabled_dropdown_options."""
    available_keys = {"openai": False, "anthropic": True, "google": True}
    result = validate({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "chatgpt", "mcp": "NONE", "routing": "via_agw",
    }, available_keys=available_keys)
    llm_disabled = {o["id"] for o in result.get("disabled_dropdown_options", {}).get("llm", [])}
    assert "chatgpt" in llm_disabled
```

Run: `python -m pytest tests/test_validator.py -xvs` → **Expected: ModuleNotFoundError**.

- [ ] **Step 7.2: Write `harness/validator.py`**

Create `/my/ws/aiplay/harness/validator.py`:

```python
"""Validate a matrix row config; return disabled cells + forced values + runnability."""
from __future__ import annotations

from typing import Any

API_VALID_STATE = {
    "chat": {"valid": [False], "forced": False, "disabled": True},
    "responses": {"valid": [True, False], "forced": None, "disabled": False},
    "responses+conv": {"valid": [True], "forced": True, "disabled": True},
    "messages": {"valid": [False], "forced": False, "disabled": True},
}

API_TO_PROVIDERS = {
    "chat": ["ollama", "claude", "chatgpt", "gemini"],
    "responses": ["chatgpt"],
    "responses+conv": ["chatgpt"],
    "messages": ["claude"],
}

PROVIDER_TO_KEY = {
    "ollama": None,          # no key required
    "chatgpt": "openai",
    "claude": "anthropic",
    "gemini": "google",
}


def validate(row: dict[str, Any], available_keys: dict[str, bool] | None = None) -> dict[str, Any]:
    """Validate a row config. Returns disabled_cells, forced_values, runnable, disabled_dropdown_options."""
    available_keys = available_keys or {}
    disabled: list[str] = []
    forced: dict[str, Any] = {}
    warnings: list[str] = []
    disabled_dropdown_options: dict[str, list[dict[str, str]]] = {"llm": []}

    llm = row.get("llm", "NONE")
    mcp = row.get("mcp", "NONE")
    api = row.get("api", "chat")
    state = row.get("state", False)

    # Rule 1: LLM=NONE disables api/provider/stream/state (direct-MCP only mode)
    if llm == "NONE":
        for cell in ("api", "stream", "state", "provider"):
            disabled.append(cell)

    # Rule 2: LLM=NONE AND MCP=NONE → not runnable
    if llm == "NONE" and mcp == "NONE":
        return {
            "disabled_cells": disabled,
            "forced_values": forced,
            "runnable": False,
            "warnings": ["LLM=NONE AND MCP=NONE is not a valid combination"],
            "disabled_dropdown_options": disabled_dropdown_options,
        }

    # Rule 3: Per-API state constraints
    if llm != "NONE":
        rules = API_VALID_STATE.get(api, {})
        if rules.get("disabled"):
            disabled.append("state")
            forced["state"] = rules.get("forced")

    # Rule 4: provider availability (keys detected)
    for provider, env_key in PROVIDER_TO_KEY.items():
        if env_key is None:
            continue
        if not available_keys.get(env_key, True):
            disabled_dropdown_options["llm"].append({
                "id": provider,
                "reason": f"{env_key.upper()}_API_KEY not set in .env",
            })

    return {
        "disabled_cells": disabled,
        "forced_values": forced,
        "runnable": True,
        "warnings": warnings,
        "disabled_dropdown_options": disabled_dropdown_options,
    }
```

- [ ] **Step 7.3: Run validator tests**

```bash
cd /my/ws/aiplay
python -m pytest tests/test_validator.py -xvs
# Expected: 7 passed
```

- [ ] **Step 7.4: Write failing templates test**

Create `/my/ws/aiplay/tests/test_templates.py`:

```python
"""Tests for harness/templates.py — default turn plans by config."""
from templates import default_turn_plan


def test_chat_no_mcp_returns_three_text_turns():
    """No MCP → 3 text turns with default joke content."""
    plan = default_turn_plan({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
    })
    assert len(plan["turns"]) == 3
    for t in plan["turns"]:
        assert t["kind"] == "user_msg"
        assert isinstance(t["content"], str) and len(t["content"]) > 0


def test_chat_with_weather_mcp_includes_weather_queries():
    """MCP=weather → turns reference weather."""
    plan = default_turn_plan({
        "framework": "langchain", "api": "chat",
        "stream": False, "state": False,
        "llm": "ollama", "mcp": "weather", "routing": "via_agw",
    })
    assert len(plan["turns"]) >= 3
    contents = " ".join(t["content"] for t in plan["turns"])
    assert "weather" in contents.lower() or "paris" in contents.lower()


def test_none_llm_with_mcp_produces_direct_mcp_plan():
    """LLM=NONE + MCP=weather → direct tool invocation plan."""
    plan = default_turn_plan({
        "framework": "NONE", "api": "NONE",
        "stream": False, "state": False,
        "llm": "NONE", "mcp": "weather", "routing": "via_agw",
    })
    # For Plan A: single tools/list + tools/call
    assert len(plan["turns"]) == 2
    assert plan["turns"][0]["kind"] == "direct_mcp_tools_list"
    assert plan["turns"][1]["kind"] == "direct_mcp_tools_call"
```

- [ ] **Step 7.5: Write `harness/defaults.yaml`**

Create `/my/ws/aiplay/harness/defaults.yaml`:

```yaml
# Matrix seed rows (Plan A — only langchain is actively running)

matrix_seed_rows:
  - {framework: langchain, api: chat, stream: false, state: false, llm: ollama, mcp: NONE, routing: via_agw}
  - {framework: langchain, api: chat, stream: false, state: false, llm: ollama, mcp: weather, routing: via_agw}
  - {framework: NONE, api: NONE, stream: false, state: false, llm: NONE, mcp: weather, routing: via_agw}

# Default turn-plan templates — keyed by (has_mcp, api_category)
# Plan A implements: no_mcp_chat, with_mcp_chat, direct_mcp
# Plan B implements: with_mcp_responses, responses_stateful, with_compact, etc.

turn_plan_templates:
  no_mcp_chat:
    turns:
      - {kind: user_msg, content: "Hello, tell me a short one-line fact about testing."}
      - {kind: user_msg, content: "Can you elaborate on that?"}
      - {kind: user_msg, content: "Summarize what you just told me."}

  with_mcp_weather:
    turns:
      - {kind: user_msg, content: "Hello, what tools do you have available?"}
      - {kind: user_msg, content: "What's the weather in Paris?"}
      - {kind: user_msg, content: "And in London?"}

  with_mcp_news:
    turns:
      - {kind: user_msg, content: "Hello, what tools do you have available?"}
      - {kind: user_msg, content: "Give me news about AI."}
      - {kind: user_msg, content: "Any news about space exploration?"}

  with_mcp_library:
    turns:
      - {kind: user_msg, content: "Hello, what tools do you have available?"}
      - {kind: user_msg, content: "Find books with 'Clean Code' in the title."}
      - {kind: user_msg, content: "Show me more results."}

  with_mcp_fetch:
    turns:
      - {kind: user_msg, content: "Hello, what tools do you have available?"}
      - {kind: user_msg, content: "Fetch https://example.com and summarize it."}
      - {kind: user_msg, content: "Fetch https://httpbin.org/uuid and tell me what it returns."}

  direct_mcp:
    turns:
      - {kind: direct_mcp_tools_list}
      - {kind: direct_mcp_tools_call}
```

- [ ] **Step 7.6: Write `harness/templates.py`**

Create `/my/ws/aiplay/harness/templates.py`:

```python
"""Default turn plans by row config."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")


def _load_defaults() -> dict[str, Any]:
    with _DEFAULTS_PATH.open() as f:
        return yaml.safe_load(f)


def default_turn_plan(row: dict[str, Any]) -> dict[str, Any]:
    """Pick a turn plan template based on the row config."""
    data = _load_defaults()
    templates = data["turn_plan_templates"]

    llm = row.get("llm", "NONE")
    mcp = row.get("mcp", "NONE")

    if llm == "NONE":
        return templates["direct_mcp"]

    if mcp == "NONE":
        return templates["no_mcp_chat"]

    # Active MCP — pick per-MCP template
    key = f"with_mcp_{mcp}"
    if key in templates:
        return templates[key]

    # Fallback — generic mcp query template (shouldn't reach in Plan A)
    return templates.get("with_mcp_weather", templates["no_mcp_chat"])
```

- [ ] **Step 7.7: Run templates tests**

```bash
cd /my/ws/aiplay
python -m pytest tests/test_templates.py -xvs
# Expected: 3 passed
```

- [ ] **Step 7.8: Commit**

```bash
git add harness/validator.py harness/templates.py harness/defaults.yaml \
        tests/test_validator.py tests/test_templates.py
git commit -m "feat(aiplay): validator + turn-plan templates

validator.py enforces: chat→state=F forced, responses+conv→state=T forced,
messages→state=F forced, LLM=NONE disables api/stream/state/provider,
LLM+MCP both NONE→not runnable, provider dropdown options filtered by
env key availability. templates.py picks default turn plan by (llm, mcp)
combo, backed by defaults.yaml. 10 tests pass."
```

---

## Task 8: Harness — providers + efficacy verdicts (a + b)

**Files:**
- Create: `harness/providers.py`
- Create: `harness/efficacy.py`
- Create: `tests/test_providers.py`
- Create: `tests/test_efficacy.py`

- [ ] **Step 8.1: Write failing providers test**

Create `/my/ws/aiplay/tests/test_providers.py`:

```python
"""Tests for harness/providers.py — env-based key detection."""
import os

import pytest

from providers import get_providers


def test_ollama_always_available(monkeypatch):
    """Ollama doesn't need a key → always available."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    providers = get_providers()
    ollama = next(p for p in providers if p["id"] == "ollama")
    assert ollama["available"] is True


def test_chatgpt_available_when_openai_key_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    providers = get_providers()
    chatgpt = next(p for p in providers if p["id"] == "chatgpt")
    assert chatgpt["available"] is True


def test_chatgpt_unavailable_when_key_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    providers = get_providers()
    chatgpt = next(p for p in providers if p["id"] == "chatgpt")
    assert chatgpt["available"] is False
    assert "OPENAI_API_KEY" in chatgpt["unavailable_reason"]


def test_all_4_providers_returned(monkeypatch):
    providers = get_providers()
    ids = {p["id"] for p in providers}
    assert {"NONE", "ollama", "claude", "chatgpt", "gemini"} <= ids
```

- [ ] **Step 8.2: Write `harness/providers.py`**

Create `/my/ws/aiplay/harness/providers.py`:

```python
"""Return current LLM provider availability based on env key detection."""
from __future__ import annotations

import os


def get_providers() -> list[dict]:
    """Return list of {id, name, available, unavailable_reason}."""

    def key_set(env: str) -> bool:
        return bool(os.environ.get(env, "").strip())

    return [
        {
            "id": "NONE",
            "name": "NONE (direct MCP only)",
            "available": True,
            "unavailable_reason": None,
        },
        {
            "id": "ollama",
            "name": "Ollama (local)",
            "available": True,
            "unavailable_reason": None,
        },
        {
            "id": "claude",
            "name": "claude.ai (Anthropic)",
            "available": key_set("ANTHROPIC_API_KEY"),
            "unavailable_reason":
                None if key_set("ANTHROPIC_API_KEY")
                else "ANTHROPIC_API_KEY not set in .env",
        },
        {
            "id": "chatgpt",
            "name": "chatgpt (OpenAI)",
            "available": key_set("OPENAI_API_KEY"),
            "unavailable_reason":
                None if key_set("OPENAI_API_KEY")
                else "OPENAI_API_KEY not set in .env",
        },
        {
            "id": "gemini",
            "name": "gemini (Google)",
            "available": key_set("GOOGLE_API_KEY"),
            "unavailable_reason":
                None if key_set("GOOGLE_API_KEY")
                else "GOOGLE_API_KEY not set in .env",
        },
    ]
```

- [ ] **Step 8.3: Run providers tests**

```bash
python -m pytest tests/test_providers.py -xvs
# Expected: 4 passed
```

- [ ] **Step 8.4: Write failing efficacy test**

Create `/my/ws/aiplay/tests/test_efficacy.py`:

```python
"""Tests for harness/efficacy.py — verdict computation (Plan A: verdicts a + b)."""
from trials import Trial, TrialConfig, TurnPlan, Turn, AuditEntry, Verdict
from efficacy import compute_verdicts


def _trial_with(turns, audit_entries, routing="via_agw", api="chat"):
    cfg = TrialConfig(
        framework="langchain", api=api,
        stream=False, state=False,
        llm="ollama", mcp="NONE", routing=routing,
    )
    return Trial(
        trial_id="t", config=cfg, turn_plan=TurnPlan(turns=[]),
        turns=turns, audit_entries=audit_entries,
    )


def test_verdict_a_pass_when_cid_present_each_turn():
    turns = [
        Turn(turn_id="t0", turn_idx=0, kind="user_msg"),
        Turn(turn_id="t1", turn_idx=1, kind="user_msg"),
    ]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={}),
        AuditEntry(trial_id="t", turn_id="t1", phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["a"].verdict == "pass"


def test_verdict_a_fail_when_turn_has_no_cid_entry():
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    audit = []  # no audit entries captured
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["a"].verdict in ("fail", "error")


def test_verdict_b_pass_when_c2_marker_in_text_response():
    """Verdict b — text response carries marker matching audit cid."""
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={
            "body": {
                "choices": [
                    {"message": {"content": "Here's info.<!-- ib:cid=ib_abc123def456 -->"}}
                ]
            }
        },
    )]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="terminal",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "pass"


def test_verdict_b_fail_when_text_response_missing_marker():
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={"body": {"choices": [{"message": {"content": "plain text response"}}]}},
    )]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="terminal",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "fail"
    assert "C2" in v["b"].reason or "marker" in v["b"].reason.lower()


def test_verdict_b_pass_when_c1_in_tool_calls_args():
    turns = [Turn(
        turn_id="t0", turn_idx=0, kind="user_msg",
        response={
            "body": {
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"Paris","_ib_cid":"ib_abc123def456"}',
                            }
                        }]
                    }
                }]
            }
        },
    )]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="tool_planned",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    assert v["b"].verdict == "pass"


def test_direct_mode_skips_all_verdicts():
    """routing=direct → all 5 verdicts are na."""
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    trial = _trial_with(turns, [], routing="direct")
    v = compute_verdicts(trial)
    for lvl in ("a", "b", "c", "d", "e"):
        assert v[lvl].verdict == "na"


def test_plan_b_verdicts_cde_return_na():
    """Plan A reports c/d/e as na with reason 'deferred to Plan B'."""
    turns = [Turn(turn_id="t0", turn_idx=0, kind="user_msg")]
    audit = [
        AuditEntry(trial_id="t", turn_id="t0", phase="llm_request",
                   cid="ib_abc", backend="ollama", raw={}),
    ]
    trial = _trial_with(turns, audit)
    v = compute_verdicts(trial)
    for lvl in ("c", "d", "e"):
        assert v[lvl].verdict == "na"
        assert "plan b" in v[lvl].reason.lower() or "deferred" in v[lvl].reason.lower()
```

- [ ] **Step 8.5: Write `harness/efficacy.py`**

Create `/my/ws/aiplay/harness/efficacy.py`:

```python
"""Compute cidgar efficacy verdicts (Plan A: a + b; c/d/e stub to Plan B)."""
from __future__ import annotations

import json
import re
from typing import Any

from trials import Trial, Verdict

MARKER_RE = re.compile(r"<!--\s*ib:cid=(ib_[a-f0-9]{12})\s*-->")


def _user_msg_turns(trial: Trial):
    return [t for t in trial.turns if t.kind == "user_msg"]


def _audit_for_turn(trial: Trial, turn_id: str) -> list:
    return [e for e in trial.audit_entries if e.turn_id == turn_id and e.cid]


def verdict_a_presence(trial: Trial) -> Verdict:
    """(a) presence — each user_msg turn has ≥1 audit entry with a cid."""
    user_turns = _user_msg_turns(trial)
    if not user_turns:
        return Verdict("na", "no user_msg turns in trial")
    cids = set()
    for t in user_turns:
        matching = _audit_for_turn(trial, t.turn_id)
        if not matching:
            if not trial.audit_entries:
                return Verdict("error",
                    "no AGW audit entries captured — check governance policy on route "
                    "and RUST_LOG_FORMAT=json")
            return Verdict("fail", f"Turn {t.turn_idx} has no audit entry with a CID")
        cids.update(e.cid for e in matching)
    return Verdict("pass", f"CID present in all {len(user_turns)} turns; unique CIDs: {sorted(cids)}")


def _find_cid_in_text(text: str) -> str | None:
    if not isinstance(text, str):
        return None
    m = MARKER_RE.search(text)
    return m.group(1) if m else None


def _find_cid_in_tool_calls_openai(body: dict[str, Any]) -> list[str]:
    """Extract _ib_cid from openai-shape tool_calls[].function.arguments."""
    out = []
    choices = body.get("choices", []) or []
    for ch in choices:
        msg = ch.get("message", {}) or {}
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            args_str = fn.get("arguments", "")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                cid = args.get("_ib_cid")
                if cid:
                    out.append(cid)
            except (ValueError, AttributeError):
                continue
    return out


def _find_c2_marker_openai(body: dict[str, Any]) -> str | None:
    """Pull C2 marker from choices[].message.content (text-only response)."""
    choices = body.get("choices", []) or []
    for ch in choices:
        content = ch.get("message", {}).get("content", "")
        cid = _find_cid_in_text(content)
        if cid:
            return cid
    return None


def verdict_b_channel_structure(trial: Trial) -> Verdict:
    """(b) channel structure — expected channels carry audit-reported CID."""
    user_turns = _user_msg_turns(trial)
    if not user_turns:
        return Verdict("na", "no user_msg turns in trial")

    issues = []

    for t in user_turns:
        audit = _audit_for_turn(trial, t.turn_id)
        if not audit:
            return Verdict("error", f"turn {t.turn_idx} has no audit entry — verdict_a should have caught this")
        expected_cid = audit[0].cid
        body = (t.response or {}).get("body", {}) or {}

        # Detect whether this turn carries tool_calls (→ C1 expected) or text (→ C2 expected)
        choices = body.get("choices", []) or []
        has_tool_calls = any(
            (ch.get("message", {}) or {}).get("tool_calls")
            for ch in choices
        )
        has_text = any(
            (ch.get("message", {}) or {}).get("content")
            for ch in choices
        )

        if has_tool_calls:
            c1_cids = _find_cid_in_tool_calls_openai(body)
            if expected_cid not in c1_cids:
                issues.append(f"Turn {t.turn_idx}: C1 missing — expected cid={expected_cid} "
                              f"in tool_calls[].function.arguments._ib_cid; found={c1_cids}")
        elif has_text:
            c2_cid = _find_c2_marker_openai(body)
            if c2_cid != expected_cid:
                issues.append(f"Turn {t.turn_idx}: C2 text marker missing or mismatched — "
                              f"expected cid={expected_cid}; found={c2_cid}")

    if issues:
        return Verdict("fail", " | ".join(issues))
    return Verdict("pass", f"all channels carry expected CID across {len(user_turns)} turns")


def compute_verdicts(trial: Trial) -> dict[str, Verdict]:
    """Return {a, b, c, d, e} verdicts. Plan A computes a+b; c/d/e na."""
    if trial.config.routing == "direct":
        na = Verdict("na", "baseline — cidgar not in path")
        return {"a": na, "b": na, "c": na, "d": na, "e": na}
    if trial.status == "aborted":
        na = Verdict("na", "trial aborted before completion")
        return {"a": na, "b": na, "c": na, "d": na, "e": na}
    return {
        "a": verdict_a_presence(trial),
        "b": verdict_b_channel_structure(trial),
        "c": Verdict("na", "deferred to Plan B"),
        "d": Verdict("na", "deferred to Plan B"),
        "e": Verdict("na", "deferred to Plan B"),
    }
```

- [ ] **Step 8.6: Run efficacy tests**

```bash
python -m pytest tests/test_efficacy.py -xvs
# Expected: 7 passed
```

- [ ] **Step 8.7: Commit**

```bash
git add harness/providers.py harness/efficacy.py tests/test_providers.py tests/test_efficacy.py
git commit -m "feat(aiplay): providers detection + efficacy verdicts a + b

providers.py reads env to detect key availability; ollama always
available. efficacy.py computes verdict a (CID presence in audit log
per turn) and verdict b (channel structure — C1 in tool_calls args or
C2 text marker matches audit-reported CID). c/d/e stub to 'deferred to
Plan B'. Handles direct routing (all na) + aborted trials (all na).
11 tests pass."
```

---

## Task 9: Harness — audit_tail (Docker SDK log consumer)

**Files:**
- Create: `harness/audit_tail.py`
- Create: `tests/test_audit_tail.py`

- [ ] **Step 9.1: Write failing audit_tail test**

Create `/my/ws/aiplay/tests/test_audit_tail.py`:

```python
"""Tests for harness/audit_tail.py — parse AGW json logs + demux by trial-id."""
import json

from audit_tail import parse_log_line, line_matches_trial


def test_parse_json_log_line():
    """AGW emits json; we parse out governance fields."""
    line = json.dumps({
        "timestamp": "2026-04-22T14:23:12Z",
        "level": "INFO",
        "target": "agentgateway::governance",
        "fields": {
            "phase": "llm_request",
            "cid": "ib_abc123def456",
            "backend": "ollama",
            "trace_id": "abc",
            "body": '{"headers": {"X-Harness-Trial-ID": "trial-1", "X-Harness-Turn-ID": "turn-1"}}'
        }
    })
    entry = parse_log_line(line)
    assert entry is not None
    assert entry["phase"] == "llm_request"
    assert entry["cid"] == "ib_abc123def456"
    assert entry["trial_id"] == "trial-1"
    assert entry["turn_id"] == "turn-1"


def test_parse_non_governance_line_returns_none():
    """Lines from other log targets are skipped."""
    line = json.dumps({
        "target": "agentgateway::proxy",
        "fields": {"msg": "proxied request"},
    })
    assert parse_log_line(line) is None


def test_parse_malformed_line_returns_none():
    assert parse_log_line("not json at all") is None
    assert parse_log_line("") is None
    assert parse_log_line("{}") is None


def test_line_matches_trial():
    entry = {"trial_id": "trial-42"}
    assert line_matches_trial(entry, "trial-42") is True
    assert line_matches_trial(entry, "other-trial") is False
    assert line_matches_trial({"trial_id": None}, "trial-42") is False
```

- [ ] **Step 9.2: Run tests (fail)**

```bash
python -m pytest tests/test_audit_tail.py -xvs
# Expected: ModuleNotFoundError
```

- [ ] **Step 9.3: Write `harness/audit_tail.py`**

Create `/my/ws/aiplay/harness/audit_tail.py`:

```python
"""Consume AGW JSON logs via Docker SDK; demux by X-Harness-Trial-ID."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Callable

log = logging.getLogger("aiplay.audit_tail")


def parse_log_line(line: str) -> dict[str, Any] | None:
    """Parse a single AGW JSON log line; return None if not a governance entry."""
    if not line or not line.strip():
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    target = obj.get("target", "")
    if target != "agentgateway::governance":
        return None

    fields = obj.get("fields", {}) or {}

    # Parse body field (JSON string with headers + payload)
    body_str = fields.get("body", "")
    trial_id = None
    turn_id = None
    if isinstance(body_str, str) and body_str:
        try:
            body_obj = json.loads(body_str)
            headers = body_obj.get("headers", {}) or {}
            trial_id = headers.get("X-Harness-Trial-ID") or headers.get("x-harness-trial-id")
            turn_id = headers.get("X-Harness-Turn-ID") or headers.get("x-harness-turn-id")
        except json.JSONDecodeError:
            pass
    elif isinstance(body_str, dict):
        # Defensive: AGW may emit body as a dict directly
        headers = body_str.get("headers", {}) or {}
        trial_id = headers.get("X-Harness-Trial-ID")
        turn_id = headers.get("X-Harness-Turn-ID")

    return {
        "timestamp": obj.get("timestamp"),
        "target": target,
        "phase": fields.get("phase"),
        "cid": fields.get("cid"),
        "backend": fields.get("backend"),
        "trace_id": fields.get("trace_id"),
        "trial_id": trial_id,
        "turn_id": turn_id,
        "raw": obj,
    }


def line_matches_trial(entry: dict[str, Any], trial_id: str) -> bool:
    """Whether an entry's trial_id matches the given trial_id."""
    return entry is not None and entry.get("trial_id") == trial_id


class AuditTail:
    """Background task that tails Docker logs for the AGW container."""

    def __init__(self, container_name: str = "agentgateway"):
        self.container_name = container_name
        self.subscribers: dict[str, list[Callable[[dict], None]]] = {}
        self._task: asyncio.Task | None = None

    def subscribe(self, trial_id: str, callback: Callable[[dict], None]) -> None:
        self.subscribers.setdefault(trial_id, []).append(callback)

    def unsubscribe(self, trial_id: str) -> None:
        self.subscribers.pop(trial_id, None)

    async def run(self) -> None:
        """Run the tail loop. Uses `docker logs -f <container>` via subprocess."""
        cmd = ["docker", "logs", "-f", "--tail", "0", self.container_name]
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,  # AGW logs to stderr; merge
                )
                async for line in self._stream_lines(proc.stdout):
                    entry = parse_log_line(line)
                    if entry is None:
                        continue
                    tid = entry.get("trial_id")
                    if tid and tid in self.subscribers:
                        for cb in self.subscribers[tid]:
                            try:
                                cb(entry)
                            except Exception:
                                log.exception("audit_tail subscriber callback failed")
                await proc.wait()
            except Exception:
                log.exception("audit_tail loop error; restarting in 2s")
                await asyncio.sleep(2)

    async def _stream_lines(self, reader) -> AsyncIterator[str]:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            yield raw.decode("utf-8", errors="replace").strip()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())
```

- [ ] **Step 9.4: Run tests pass**

```bash
python -m pytest tests/test_audit_tail.py -xvs
# Expected: 4 passed
```

- [ ] **Step 9.5: Commit**

```bash
git add harness/audit_tail.py tests/test_audit_tail.py
git commit -m "feat(aiplay): AGW audit log tail via docker logs subprocess

parse_log_line extracts governance fields from AGW's RUST_LOG_FORMAT=json
output. AuditTail uses 'docker logs -f' subprocess (not Docker Python
SDK — simpler, no volume requirement). Demuxes by X-Harness-Trial-ID
from the parsed body.headers field. Auto-restarts on loop failure.
4 tests pass (parsing logic only; subprocess integration is tested in
the end-to-end task)."
```

---

## Task 10: Harness — runner (turn plan executor)

**Files:**
- Create: `harness/runner.py`
- Create: `tests/test_runner.py`

- [ ] **Step 10.1: Write failing runner test**

Create `/my/ws/aiplay/tests/test_runner.py`:

```python
"""Tests for harness/runner.py — turn plan executor using mock adapter."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from trials import Trial, TrialConfig, TurnPlan, AuditEntry, TrialStore
from runner import run_trial


@pytest.mark.asyncio
async def test_runner_executes_single_user_msg_turn(tmp_data_dir):
    """One user_msg turn: runner calls adapter.turn, saves result."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    plan = TurnPlan(turns=[{"kind": "user_msg", "content": "hello"}])
    trial = Trial(trial_id="trial-x", config=cfg, turn_plan=plan, status="running")
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    # Mock adapter client
    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock(return_value={
        "turn_id": "turn-0",
        "assistant_msg": "hi!",
        "tool_calls": [],
        "request_captured": {"body": {}},
        "response_captured": {
            "status": 200,
            "body": {"choices": [{"message": {"content": "hi!<!-- ib:cid=ib_abc123def456 -->"}}]},
        },
    })
    adapter.delete_trial = AsyncMock(return_value={"ok": True})

    # Mock audit tail — simulate one audit entry
    audit_entries = [
        AuditEntry(trial_id="trial-x", turn_id="turn-0", phase="terminal",
                   cid="ib_abc123def456", backend="ollama", raw={}),
    ]

    await run_trial(
        trial_id="trial-x",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: audit_entries,
    )

    loaded = store.load("trial-x")
    assert loaded.status == "pass"
    assert len(loaded.turns) == 1
    assert loaded.verdicts["a"]["verdict"] == "pass"
    assert loaded.verdicts["b"]["verdict"] == "pass"
    adapter.create_trial.assert_called_once()
    adapter.drive_turn.assert_called_once()
    adapter.delete_trial.assert_called_once()


@pytest.mark.asyncio
async def test_runner_handles_adapter_error(tmp_data_dir):
    """Adapter raises → trial marked error."""
    cfg = TrialConfig(
        framework="langchain", api="chat", stream=False, state=False,
        llm="ollama", mcp="NONE", routing="via_agw",
    )
    plan = TurnPlan(turns=[{"kind": "user_msg", "content": "hello"}])
    trial = Trial(trial_id="t-err", config=cfg, turn_plan=plan, status="running")
    store = TrialStore(tmp_data_dir / "trials")
    store.save(trial)

    adapter = MagicMock()
    adapter.create_trial = AsyncMock(return_value={"ok": True})
    adapter.drive_turn = AsyncMock(side_effect=RuntimeError("adapter crashed"))
    adapter.delete_trial = AsyncMock(return_value={"ok": True})

    await run_trial(
        trial_id="t-err",
        store=store,
        adapter_client=adapter,
        audit_entries_provider=lambda: [],
    )

    loaded = store.load("t-err")
    assert loaded.status == "error"
    assert "adapter crashed" in (loaded.error_reason or "")
```

- [ ] **Step 10.2: Add pytest-asyncio to requirements**

Will be listed in the upcoming Task 11 requirements.txt. For now test locally:

```bash
pip install pytest-asyncio
```

- [ ] **Step 10.3: Write `harness/runner.py`**

Create `/my/ws/aiplay/harness/runner.py`:

```python
"""Drive a trial's turn plan through the adapter; capture audit entries."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Callable

from trials import Trial, TrialStore, Turn, AuditEntry
from efficacy import compute_verdicts


async def run_trial(
    trial_id: str,
    store: TrialStore,
    adapter_client,
    audit_entries_provider: Callable[[], list[AuditEntry]],
) -> None:
    """Execute a trial's turn plan end to end.

    adapter_client must expose: create_trial, drive_turn, delete_trial (async).
    audit_entries_provider returns the list of audit entries captured
    so far (used at verdict time; for production wiring, it's audit_tail's
    subscriber-side buffer).
    """
    trial = store.load(trial_id)
    trial.started_at = datetime.now(timezone.utc).isoformat()
    trial.status = "running"
    store.save(trial)

    try:
        await adapter_client.create_trial(trial_id=trial_id, config=trial.config)

        for idx, turn_spec in enumerate(trial.turn_plan.turns):
            kind = turn_spec.get("kind", "user_msg")
            turn_id = f"turn-{idx:03d}-{uuid.uuid4().hex[:8]}"
            turn = Turn(
                turn_id=turn_id, turn_idx=idx, kind=kind,
                started_at=datetime.now(timezone.utc).isoformat(),
            )

            if kind == "user_msg":
                resp = await adapter_client.drive_turn(
                    trial_id=trial_id,
                    turn_id=turn_id,
                    user_msg=turn_spec.get("content", ""),
                )
                turn.request = resp.get("request_captured", {})
                turn.response = resp.get("response_captured", {})
                turn.framework_events = resp.get("framework_events", [])
            else:
                # Plan A supports only user_msg; others are documented in design as Plan B.
                turn.error = {"reason": f"turn kind {kind!r} not implemented in Plan A"}

            turn.finished_at = datetime.now(timezone.utc).isoformat()
            store.append_turn(trial_id, turn)

        # Grace period for audit log stragglers
        await asyncio.sleep(0.3)

        # Pull audit entries collected by audit_tail (via provider)
        audits = audit_entries_provider()
        for a in audits:
            store.append_audit(trial_id, a)

        trial = store.load(trial_id)
        verdicts_out = compute_verdicts(trial)

        # Persist verdicts (convert Verdict dataclass → plain dict)
        trial.verdicts = {k: {"verdict": v.verdict, "reason": v.reason}
                          for k, v in verdicts_out.items()}

        any_fail = any(v.verdict == "fail" for v in verdicts_out.values())
        any_error = any(v.verdict == "error" for v in verdicts_out.values())
        trial.status = (
            "error" if any_error
            else "fail" if any_fail
            else "pass"
        )
        trial.finished_at = datetime.now(timezone.utc).isoformat()
        store.save(trial)

    except Exception as e:
        trial = store.load(trial_id)
        trial.status = "error"
        trial.error_reason = str(e)
        trial.finished_at = datetime.now(timezone.utc).isoformat()
        store.save(trial)
    finally:
        try:
            await adapter_client.delete_trial(trial_id=trial_id)
        except Exception:
            pass
```

- [ ] **Step 10.4: Run runner tests**

```bash
# Ensure pytest-asyncio installed
pip install pytest-asyncio
python -m pytest tests/test_runner.py -xvs
# Expected: 2 passed
```

You may need to add `asyncio_mode = "auto"` to a `pytest.ini`. Create `/my/ws/aiplay/pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
```

Re-run:

```bash
python -m pytest tests/test_runner.py -xvs
# Expected: 2 passed
```

- [ ] **Step 10.5: Commit**

```bash
git add harness/runner.py tests/test_runner.py pytest.ini
git commit -m "feat(aiplay): trial runner executes turn plan + computes verdicts

run_trial drives adapter through turn_plan.turns, captures req/resp per
turn, grace-waits for audit stragglers, calls compute_verdicts, persists
status=pass|fail|error. adapter_client is duck-typed (create_trial,
drive_turn, delete_trial async methods) so tests can mock it. audit
entries flow in via injectable provider for testability. Plan A only
implements kind=user_msg; other turn kinds recorded with error.
2 tests pass."
```

---

## Task 11: Harness — API + main + Dockerfile + requirements

**Files:**
- Create: `harness/api.py`
- Create: `harness/main.py`
- Create: `harness/Dockerfile`
- Create: `harness/requirements.txt`
- Create: `tests/test_api.py`

- [ ] **Step 11.1: Write failing API test**

Create `/my/ws/aiplay/tests/test_api.py`:

```python
"""Tests for harness/api.py — FastAPI endpoints via TestClient."""
from fastapi.testclient import TestClient

from main import app


def test_health_returns_ok():
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_providers_returns_all_five():
    with TestClient(app) as client:
        r = client.get("/providers")
        assert r.status_code == 200
        ids = {p["id"] for p in r.json()["providers"]}
        assert {"NONE", "ollama", "claude", "chatgpt", "gemini"} == ids


def test_validate_chat_api_disables_state():
    with TestClient(app) as client:
        r = client.post("/validate", json={"row_config": {
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        }})
        assert r.status_code == 200
        data = r.json()
        assert "state" in data["disabled_cells"]


def test_matrix_row_crud(tmp_data_dir):
    """Create, update, delete matrix rows."""
    with TestClient(app) as client:
        # Empty matrix at start
        r = client.get("/matrix")
        assert r.status_code == 200
        initial_count = len(r.json()["rows"])

        # Create
        r = client.post("/matrix/row", json={
            "framework": "langchain", "api": "chat",
            "stream": False, "state": False,
            "llm": "ollama", "mcp": "NONE", "routing": "via_agw",
        })
        assert r.status_code == 200
        row_id = r.json()["row_id"]
        assert row_id

        # Read
        r = client.get("/matrix")
        assert len(r.json()["rows"]) == initial_count + 1

        # Update
        r = client.patch(f"/matrix/row/{row_id}", json={"stream": True})
        assert r.status_code == 200

        # Delete
        r = client.delete(f"/matrix/row/{row_id}")
        assert r.status_code == 200


def test_info_reports_adapter_discovery():
    with TestClient(app) as client:
        r = client.get("/info")
        assert r.status_code == 200
        # Plan A has adapter-langchain as the only discovered adapter
        data = r.json()
        assert "adapters" in data
```

- [ ] **Step 11.2: Write `harness/requirements.txt`**

Create `/my/ws/aiplay/harness/requirements.txt`:

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
httpx>=0.27.0
pydantic>=2.6.0
pyyaml>=6.0
sse-starlette>=2.0.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

- [ ] **Step 11.3: Write `harness/api.py`**

Create `/my/ws/aiplay/harness/api.py`:

```python
"""FastAPI routes for the aiplay harness."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from audit_tail import AuditTail
from efficacy import compute_verdicts
from providers import get_providers
from runner import run_trial
from templates import default_turn_plan
from trials import AuditEntry, Trial, TrialConfig, TrialStore, TurnPlan
from validator import validate as validate_row

router = APIRouter()


# ── State (module-global, single-process harness) ──

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STORE = TrialStore(DATA_DIR / "trials")
MATRIX_PATH = DATA_DIR / "matrix.json"
AUDIT_TAIL: AuditTail | None = None
AUDIT_BUFFER_PER_TRIAL: dict[str, list[AuditEntry]] = defaultdict(list)
SSE_QUEUES: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))


# ── Matrix persistence (distinct from trial JSON) ──

def _load_matrix() -> list[dict]:
    if not MATRIX_PATH.exists():
        return []
    with MATRIX_PATH.open() as f:
        return json.load(f).get("rows", [])


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
    llm: str
    mcp: str
    routing: str = "via_agw"


# ── Routes ──

@router.get("/health")
def health():
    return {"status": "ok", "version": "plan-a-mvp"}


@router.get("/info")
def info():
    return {
        "harness_version": "plan-a-mvp",
        "adapters": [
            {"framework": "langchain", "url": "http://adapter-langchain:5001"},
        ],
    }


@router.get("/providers")
def providers_endpoint():
    return {"providers": get_providers()}


@router.post("/validate")
def validate_endpoint(payload: dict = Body(...)):
    row = payload.get("row_config", {})
    available = {p["id"].replace("chatgpt", "openai").replace("claude", "anthropic").replace("gemini", "google"):
                 p["available"] for p in get_providers()}
    return validate_row(row, available_keys=available)


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


@router.post("/trials/{row_id}/run")
async def trial_run(row_id: str):
    rows = _load_matrix()
    row = next((r for r in rows if r["row_id"] == row_id), None)
    if not row:
        raise HTTPException(404, "row not found")

    trial_id = str(uuid.uuid4())
    cfg = TrialConfig(
        framework=row["framework"], api=row["api"],
        stream=row.get("stream", False), state=row.get("state", False),
        llm=row["llm"], mcp=row["mcp"], routing=row.get("routing", "via_agw"),
    )
    plan_dict = default_turn_plan(row)
    plan = TurnPlan(turns=plan_dict["turns"])
    trial = Trial(trial_id=trial_id, config=cfg, turn_plan=plan, status="running")
    STORE.save(trial)

    # Subscribe audit_tail → capture into buffer for this trial
    if AUDIT_TAIL is not None:
        def cb(entry: dict):
            AUDIT_BUFFER_PER_TRIAL[trial_id].append(AuditEntry(
                trial_id=trial_id, turn_id=entry.get("turn_id"),
                phase=entry.get("phase"), cid=entry.get("cid"),
                backend=entry.get("backend"), raw=entry.get("raw", {}),
                captured_at=entry.get("timestamp", ""),
            ))
        AUDIT_TAIL.subscribe(trial_id, cb)

    # Adapter client (simplified HTTP wrapper)
    from adapters_registry import AdapterClient
    adapter = AdapterClient(framework=cfg.framework)

    # Run in background
    asyncio.create_task(_run_trial_bg(trial_id, adapter))

    return {"trial_id": trial_id, "status": "running"}


async def _run_trial_bg(trial_id: str, adapter):
    def audit_provider():
        return list(AUDIT_BUFFER_PER_TRIAL.get(trial_id, []))

    await run_trial(
        trial_id=trial_id,
        store=STORE,
        adapter_client=adapter,
        audit_entries_provider=audit_provider,
    )

    if AUDIT_TAIL is not None:
        AUDIT_TAIL.unsubscribe(trial_id)


@router.get("/trials/{trial_id}")
def trial_get(trial_id: str):
    try:
        trial = STORE.load(trial_id)
        from trials import _to_jsonable
        return _to_jsonable(trial)
    except FileNotFoundError:
        raise HTTPException(404, "trial not found")


@router.get("/trials/{trial_id}/stream")
async def trial_stream(trial_id: str):
    async def event_gen():
        while True:
            await asyncio.sleep(1.0)
            try:
                trial = STORE.load(trial_id)
                yield f"data: {json.dumps({'event': 'status', 'status': trial.status})}\n\n"
                if trial.status in ("pass", "fail", "error", "aborted"):
                    yield f"data: {json.dumps({'event': 'trial_done', 'status': trial.status})}\n\n"
                    break
            except FileNotFoundError:
                yield f"data: {json.dumps({'event': 'error', 'message': 'trial missing'})}\n\n"
                break
    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/audit/stream")
async def audit_stream():
    """Raw AGW audit stream — all trials."""
    async def gen():
        while True:
            await asyncio.sleep(2.0)
            yield f": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
```

- [ ] **Step 11.4: Write `harness/adapters_registry.py`**

Create `/my/ws/aiplay/harness/adapters_registry.py`:

```python
"""Map framework → adapter URL; HTTP client for adapter endpoints."""
from __future__ import annotations

import os

import httpx

ADAPTER_URLS = {
    "langchain": os.environ.get("ADAPTER_LANGCHAIN_URL", "http://adapter-langchain:5001"),
}


class AdapterClient:
    def __init__(self, framework: str):
        self.base = ADAPTER_URLS.get(framework)
        if not self.base:
            raise ValueError(f"no adapter registered for framework={framework}")
        self.client = httpx.AsyncClient(base_url=self.base, timeout=120.0)

    async def create_trial(self, trial_id: str, config) -> dict:
        r = await self.client.post("/trials", json={
            "trial_id": trial_id,
            "config": {
                "api": config.api,
                "stream": config.stream,
                "state": config.state,
                "llm": config.llm,
                "mcp": config.mcp,
                "routing": config.routing,
                "model": config.model,
            },
        })
        r.raise_for_status()
        return r.json()

    async def drive_turn(self, trial_id: str, turn_id: str, user_msg: str) -> dict:
        r = await self.client.post(
            f"/trials/{trial_id}/turn",
            json={"turn_id": turn_id, "user_msg": user_msg},
        )
        r.raise_for_status()
        return r.json()

    async def delete_trial(self, trial_id: str) -> dict:
        r = await self.client.delete(f"/trials/{trial_id}")
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 11.5: Write `harness/main.py`**

Create `/my/ws/aiplay/harness/main.py`:

```python
"""FastAPI app entrypoint for aiplay harness."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import api
from audit_tail import AuditTail


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start audit tail
    container_name = os.environ.get("AGW_CONTAINER_NAME", "agentgateway")
    tail = AuditTail(container_name=container_name)
    tail.start()
    api.AUDIT_TAIL = tail

    yield

    # Shutdown — nothing to explicitly close; subprocess exits with process
    pass


app = FastAPI(title="aiplay — cidgar Harness C", lifespan=lifespan)
app.include_router(api.router)

# Serve frontend static files from /app/frontend/
frontend_dir = Path("/app/frontend")
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
```

- [ ] **Step 11.6: Write `harness/Dockerfile`**

Create `/my/ws/aiplay/harness/Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# docker CLI (for `docker logs -f` subprocess in audit_tail)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    curl -fsSL https://get.docker.com | sh && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

CMD ["python", "main.py"]
```

- [ ] **Step 11.7: Add reset_api_state fixture to conftest**

API module `harness/api.py` captures `STORE`, `DATA_DIR`, `MATRIX_PATH` at module import time. Tests must reset these to point at the test tmpdir. Extend `tests/conftest.py`:

```python
# tests/conftest.py — add after the existing tmp_data_dir fixture
import pytest


@pytest.fixture(autouse=False)
def reset_api_state(tmp_data_dir, monkeypatch):
    """Rewire harness/api.py module-level state to point at tmp_data_dir.
    Tests that exercise API endpoints should use this fixture explicitly."""
    import api
    from trials import TrialStore
    monkeypatch.setattr(api, "DATA_DIR", tmp_data_dir)
    monkeypatch.setattr(api, "STORE", TrialStore(tmp_data_dir / "trials"))
    monkeypatch.setattr(api, "MATRIX_PATH", tmp_data_dir / "matrix.json")
    yield
```

Update `tests/test_api.py` tests that use matrix or trials to declare the fixture:

```python
def test_matrix_row_crud(tmp_data_dir, reset_api_state):  # ← add reset_api_state
    ...
```

- [ ] **Step 11.8: Install requirements locally + run API tests**

```bash
cd /my/ws/aiplay
pip install -r harness/requirements.txt
python -m pytest tests/test_api.py -xvs
# Expected: 5 passed
```

- [ ] **Step 11.9: Commit**

```bash
git add harness/ tests/test_api.py tests/conftest.py
git commit -m "feat(aiplay): harness FastAPI + routes + Dockerfile

api.py exposes GET /health, /info, /providers, /matrix, /trials/{id},
POST /validate, /matrix/row, /trials/{row_id}/run, PATCH/DELETE matrix
rows, GET /trials/{id}/stream (SSE), /audit/stream (SSE). main.py wires
lifespan to start AuditTail on startup. adapters_registry maps framework
→ adapter URL and wraps HTTP calls. Dockerfile installs docker CLI for
subprocess-based audit log tailing. 5 API tests pass."
```

---

## Task 12: Adapter — langchain

**Files:**
- Create: `adapters/langchain/Dockerfile`
- Create: `adapters/langchain/requirements.txt`
- Create: `adapters/langchain/main.py`
- Create: `adapters/langchain/framework_bridge.py`
- Create: `tests/test_adapter_langchain.py`

- [ ] **Step 12.1: Write failing adapter test**

Create `/my/ws/aiplay/tests/test_adapter_langchain.py`:

```python
"""Tests for adapters/langchain — framework bridge logic (offline)."""
import sys
from pathlib import Path

# Make the adapter module importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters" / "langchain"))


def test_pick_llm_base_url_via_agw_ollama():
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="via_agw", llm="ollama")
    assert "agentgateway" in url and "ollama" in url


def test_pick_llm_base_url_direct_ollama():
    from framework_bridge import pick_llm_base_url
    url = pick_llm_base_url(routing="direct", llm="ollama")
    assert "host.docker.internal" in url or "11434" in url


def test_pick_mcp_base_url_via_agw_weather():
    from framework_bridge import pick_mcp_base_url
    url = pick_mcp_base_url(routing="via_agw", mcp="weather")
    assert "agentgateway" in url and "weather" in url


def test_pick_mcp_base_url_direct_weather():
    from framework_bridge import pick_mcp_base_url
    url = pick_mcp_base_url(routing="direct", mcp="weather")
    assert "mcp-weather" in url
```

- [ ] **Step 12.2: Write `adapters/langchain/requirements.txt`**

Create `/my/ws/aiplay/adapters/langchain/requirements.txt`:

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
httpx>=0.27.0
langchain>=0.2.0
langchain-openai>=0.1.0
pytest>=8.0.0
```

- [ ] **Step 12.3: Write `adapters/langchain/framework_bridge.py`**

Create `/my/ws/aiplay/adapters/langchain/framework_bridge.py`:

```python
"""Langchain-specific adapter logic."""
from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI


def pick_llm_base_url(routing: str, llm: str) -> str:
    env_map_via_agw = {
        "ollama": "AGW_LLM_BASE_URL_OLLAMA",
    }
    env_map_direct = {
        "ollama": "DIRECT_LLM_BASE_URL_OLLAMA",
    }
    env_map = env_map_via_agw if routing == "via_agw" else env_map_direct
    var = env_map.get(llm)
    if not var:
        raise ValueError(f"no LLM base URL mapping for llm={llm} routing={routing}")
    url = os.environ.get(var)
    if not url:
        raise ValueError(f"env var {var} not set")
    return url


def pick_mcp_base_url(routing: str, mcp: str) -> str:
    if mcp == "NONE":
        return ""
    prefix = "AGW_MCP_" if routing == "via_agw" else "DIRECT_MCP_"
    var = f"{prefix}{mcp.upper()}"
    url = os.environ.get(var)
    if not url:
        raise ValueError(f"env var {var} not set")
    return url


class Trial:
    """Holds per-trial framework state for langchain."""
    def __init__(self, trial_id: str, config: dict):
        self.trial_id = trial_id
        self.config = config
        self.messages: list[dict] = []  # role/content pairs

        base_url = pick_llm_base_url(routing=config["routing"], llm=config["llm"])
        model = config.get("model") or os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b-instruct")
        self.llm = ChatOpenAI(
            base_url=base_url,
            api_key="ollama",  # placeholder; Ollama doesn't validate
            model=model,
            default_headers={},  # populated per-turn in drive_turn
            temperature=0.3,
        )

    async def turn(self, turn_id: str, user_msg: str) -> dict:
        """One turn. Propagates X-Harness-* headers."""
        headers = {
            "X-Harness-Trial-ID": self.trial_id,
            "X-Harness-Turn-ID": turn_id,
        }
        self.llm.default_headers = headers

        self.messages.append({"role": "user", "content": user_msg})

        # For Plan A with MCP=NONE: plain chat
        # For Plan A with MCP=weather: pass tools — but langchain doesn't natively
        #   do MCP; we'd need langchain-mcp adapter. Plan A's MCP seeded row is
        #   primarily for the direct_mcp case (LLM=NONE + MCP=weather); the
        #   langchain+weather row can be a pure chat test where the LLM doesn't
        #   actually invoke tools.
        resp = await self.llm.ainvoke(self.messages)

        assistant_content = resp.content if hasattr(resp, "content") else str(resp)
        self.messages.append({"role": "assistant", "content": assistant_content})

        # Capture request/response at HTTP level. Langchain's OpenAI client
        # doesn't expose this cleanly; we reconstruct from what we know.
        request_captured = {
            "method": "POST",
            "url": f"{self.llm.openai_api_base}/chat/completions",
            "headers": headers,
            "body": {
                "model": self.llm.model_name,
                "messages": self.messages[:-1],  # everything before the assistant reply
            },
        }
        response_captured = {
            "status": 200,
            "headers": {},
            "body": {
                "choices": [{"message": {"content": assistant_content}}],
            },
        }

        return {
            "turn_id": turn_id,
            "assistant_msg": assistant_content,
            "tool_calls": [],
            "request_captured": request_captured,
            "response_captured": response_captured,
            "framework_events": [],
        }
```

- [ ] **Step 12.4: Write `adapters/langchain/main.py`**

Create `/my/ws/aiplay/adapters/langchain/main.py`:

```python
"""FastAPI adapter service wrapping langchain."""
from __future__ import annotations

import logging
import os

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel

import langchain
from framework_bridge import Trial

log = logging.getLogger("aiplay.adapter.langchain")

app = FastAPI(title="aiplay-adapter-langchain")

TRIALS: dict[str, Trial] = {}


class Config(BaseModel):
    api: str
    stream: bool = False
    state: bool = False
    llm: str
    mcp: str = "NONE"
    routing: str = "via_agw"
    model: str | None = None


class CreateTrialReq(BaseModel):
    trial_id: str
    config: Config


class TurnReq(BaseModel):
    turn_id: str
    user_msg: str


@app.get("/info")
def info():
    return {
        "framework": "langchain",
        "version": getattr(langchain, "__version__", "unknown"),
        "supports": {
            "apis": ["chat"],
            "streaming": False,  # Plan A
            "state_modes": ["stateless"],
            "compact_strategies": [],  # Plan B
        },
        "default_ollama_model": os.environ.get("DEFAULT_OLLAMA_MODEL", "qwen2.5:7b-instruct"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/trials")
def create_trial(req: CreateTrialReq):
    if req.config.api != "chat":
        raise HTTPException(400, f"unsupported_combination: api={req.config.api}")
    TRIALS[req.trial_id] = Trial(req.trial_id, req.config.model_dump())
    return {"ok": True, "trial_id": req.trial_id}


@app.post("/trials/{trial_id}/turn")
async def drive_turn(trial_id: str, req: TurnReq):
    trial = TRIALS.get(trial_id)
    if trial is None:
        raise HTTPException(404, "trial not found")
    return await trial.turn(req.turn_id, req.user_msg)


@app.delete("/trials/{trial_id}")
def delete_trial(trial_id: str):
    if trial_id in TRIALS:
        del TRIALS[trial_id]
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ADAPTER_PORT", "5001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
```

- [ ] **Step 12.5: Write `adapters/langchain/Dockerfile`**

Create `/my/ws/aiplay/adapters/langchain/Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

EXPOSE 5001

CMD ["python", "main.py"]
```

- [ ] **Step 12.6: Run adapter tests**

```bash
cd /my/ws/aiplay
pip install langchain langchain-openai  # local dev
python -m pytest tests/test_adapter_langchain.py -xvs
# Expected: 4 passed
```

- [ ] **Step 12.7: Commit**

```bash
git add adapters/langchain/ tests/test_adapter_langchain.py
git commit -m "feat(aiplay): langchain adapter — chat completions via ChatOpenAI

framework_bridge.Trial wraps langchain's ChatOpenAI, switches base_url
based on routing (via_agw vs direct), injects X-Harness-* headers via
default_headers. main.py exposes POST /trials + /trials/{id}/turn +
DELETE + GET /info /health per adapter contract in design §4. Plan A
supports api=chat only. 4 tests pass for URL-selection logic."
```

---

## Task 13: Frontend — static skeleton (index.html + config.js + style.css)

**Files:**
- Create: `frontend/index.html`
- Create: `frontend/config.js`
- Create: `frontend/style.css`

- [ ] **Step 13.1: Write `frontend/index.html`**

Create `/my/ws/aiplay/frontend/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>aiplay — cidgar Harness C</title>
  <link rel="stylesheet" href="/style.css">
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/ag-grid-community@32/styles/ag-grid.css">
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/ag-grid-community@32/styles/ag-theme-quartz.css">
</head>
<body>
  <header class="toolbar">
    <h1>aiplay — cidgar Harness C</h1>
    <div class="toolbar-actions">
      <button id="btn-add-row">+ Add Row</button>
      <button id="btn-run-all">▶ Run All</button>
      <button id="btn-settings">⚙ Settings</button>
    </div>
  </header>

  <main>
    <div id="matrix-grid" class="ag-theme-quartz" style="height: 60vh;"></div>
    <div id="drawer" class="drawer hidden">
      <div class="drawer-header">
        <span id="drawer-title">Trial details</span>
        <button id="drawer-close">✕</button>
      </div>
      <div class="drawer-tabs">
        <button class="tab-btn active" data-tab="turns">Turns</button>
        <button class="tab-btn" data-tab="verdicts">Verdicts</button>
        <button class="tab-btn" data-tab="raw">Raw JSON</button>
      </div>
      <div class="drawer-body">
        <div id="tab-turns" class="tab-content active"></div>
        <div id="tab-verdicts" class="tab-content"></div>
        <div id="tab-raw" class="tab-content"><pre id="raw-json"></pre></div>
      </div>
    </div>
  </main>

  <script src="https://cdn.jsdelivr.net/npm/ag-grid-community@32/dist/ag-grid-community.min.js"></script>
  <script type="module" src="/config.js"></script>
  <script type="module" src="/app.js"></script>
  <script type="module" src="/drawer.js"></script>
</body>
</html>
```

- [ ] **Step 13.2: Write `frontend/config.js`**

Create `/my/ws/aiplay/frontend/config.js`:

```javascript
export const API_BASE = "";
export const SSE_RETRY_MS = 3000;
export const VALIDATE_DEBOUNCE_MS = 150;
export const PROVIDERS_REFRESH_MS = 30000;
export const MAX_ROWS = 50;
```

- [ ] **Step 13.3: Write `frontend/style.css`**

Create `/my/ws/aiplay/frontend/style.css`:

```css
body { font-family: system-ui, sans-serif; margin: 0; background: #f7f7f7; }

.toolbar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 20px; background: #333; color: white;
}
.toolbar h1 { margin: 0; font-size: 18px; }
.toolbar-actions button {
  margin-left: 6px; padding: 6px 12px; background: #555;
  color: white; border: none; border-radius: 3px; cursor: pointer;
}
.toolbar-actions button:hover { background: #666; }

main { padding: 10px; }

#matrix-grid { background: white; border: 1px solid #ddd; }

.status-pill { padding: 2px 8px; border-radius: 12px; font-size: 11px; }
.status-pill.pass { background: #d4edda; color: #155724; }
.status-pill.fail { background: #f8d7da; color: #721c24; }
.status-pill.running { background: #fff3cd; color: #856404; }
.status-pill.idle { background: #e2e3e5; color: #495057; }
.status-pill.error { background: #f8d7da; color: #721c24; }

.verdict-pill {
  display: inline-block; width: 20px; text-align: center;
  margin-right: 2px; font-weight: bold;
}
.verdict-pill.pass { color: #28a745; }
.verdict-pill.fail { color: #dc3545; }
.verdict-pill.na { color: #6c757d; }

.drawer {
  background: white; border: 1px solid #ddd; border-top: none;
  height: 35vh; overflow-y: auto;
}
.drawer.hidden { display: none; }
.drawer-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 16px; background: #f0f0f0; border-bottom: 1px solid #ddd;
}
.drawer-tabs { display: flex; border-bottom: 1px solid #ddd; }
.tab-btn {
  padding: 8px 16px; background: none; border: none; cursor: pointer;
  border-bottom: 2px solid transparent;
}
.tab-btn.active { border-bottom-color: #007bff; font-weight: bold; }
.drawer-body { padding: 16px; }
.tab-content { display: none; }
.tab-content.active { display: block; }

.turn-card {
  border: 1px solid #ddd; margin-bottom: 10px; padding: 10px;
  border-radius: 4px;
}
.turn-card h4 { margin: 0 0 6px 0; font-size: 14px; }
.turn-card pre {
  background: #f5f5f5; padding: 8px; border-radius: 3px;
  font-size: 11px; overflow-x: auto; max-height: 200px;
}

.verdict-card {
  border-left: 3px solid #ccc; padding: 6px 12px; margin-bottom: 6px;
}
.verdict-card.pass { border-left-color: #28a745; background: #f4f9f5; }
.verdict-card.fail { border-left-color: #dc3545; background: #fbf4f4; }
.verdict-card.na { border-left-color: #6c757d; background: #f5f5f5; }
```

- [ ] **Step 13.4: Commit**

```bash
git add frontend/index.html frontend/config.js frontend/style.css
git commit -m "feat(aiplay): frontend static skeleton — index.html + config + styles

Single-page app scaffold. AG-Grid loaded from CDN (no build step).
Toolbar (top) + grid (middle) + collapsible drawer (bottom) with
Turns/Verdicts/Raw tabs. config.js holds API_BASE + debounce constants.
style.css defines pills (status + verdict) + drawer tabs + turn cards."
```

---

## Task 14: Frontend — app.js (grid + SSE)

**Files:**
- Create: `frontend/app.js`

- [ ] **Step 14.1: Write `frontend/app.js`**

Create `/my/ws/aiplay/frontend/app.js`:

```javascript
import { API_BASE, VALIDATE_DEBOUNCE_MS, PROVIDERS_REFRESH_MS } from "/config.js";
import { openDrawer, refreshDrawer } from "/drawer.js";

let gridApi;
let providers = [];

async function fetchProviders() {
  const r = await fetch(`${API_BASE}/providers`);
  const j = await r.json();
  providers = j.providers;
}

async function fetchMatrix() {
  const r = await fetch(`${API_BASE}/matrix`);
  return (await r.json()).rows || [];
}

async function validateRow(rowConfig) {
  const r = await fetch(`${API_BASE}/validate`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({row_config: rowConfig}),
  });
  return r.json();
}

function providerOptions() {
  return providers.map(p => p.id);
}

function buildColumnDefs() {
  return [
    {headerName: "#", valueGetter: "node.rowIndex + 1", width: 60, pinned: "left"},
    {
      headerName: "Framework", field: "framework", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["langchain", "langgraph", "crewai", "pydantic-ai", "autogen", "llamaindex", "NONE"]},
      pinned: "left", width: 120,
    },
    {
      headerName: "API", field: "api", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["chat", "responses", "responses+conv", "messages", "NONE"]},
      width: 140,
    },
    {headerName: "Stream", field: "stream", editable: true, cellDataType: "boolean", width: 80},
    {headerName: "State", field: "state", editable: true, cellDataType: "boolean", width: 80},
    {
      headerName: "LLM", field: "llm", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: providerOptions()},
      cellStyle: params => {
        const provider = providers.find(p => p.id === params.value);
        return provider && !provider.available ? {color: "#999", textDecoration: "line-through"} : null;
      },
      tooltipValueGetter: params => {
        const provider = providers.find(p => p.id === params.value);
        return provider && !provider.available ? provider.unavailable_reason : null;
      },
      width: 110,
    },
    {
      headerName: "MCP", field: "mcp", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["NONE", "weather", "news", "library", "fetch"]},
      width: 110,
    },
    {
      headerName: "Routing", field: "routing", editable: true,
      cellEditor: "agSelectCellEditor",
      cellEditorParams: {values: ["via_agw", "direct"]},
      width: 100,
    },
    {
      headerName: "Status", field: "status", pinned: "right", width: 100,
      cellRenderer: params => {
        const v = params.value || "idle";
        return `<span class="status-pill ${v}">${v}</span>`;
      },
    },
    {
      headerName: "Verdicts", field: "verdicts", pinned: "right", width: 140,
      cellRenderer: params => {
        const v = params.value || {};
        return ["a", "b", "c", "d", "e"].map(lvl => {
          const cls = (v[lvl]?.verdict) || "na";
          const glyph = cls === "pass" ? "✓" : cls === "fail" ? "✗" : "—";
          return `<span class="verdict-pill ${cls}" title="${v[lvl]?.reason || ""}">${glyph}</span>`;
        }).join("");
      },
    },
    {
      headerName: "Actions", pinned: "right", width: 140,
      cellRenderer: params => `
        <button class="btn-run" data-row-id="${params.data.row_id}">▶</button>
        <button class="btn-delete" data-row-id="${params.data.row_id}">✕</button>
      `,
    },
  ];
}

async function initGrid() {
  await fetchProviders();
  const rows = await fetchMatrix();

  const gridOptions = {
    columnDefs: buildColumnDefs(),
    rowData: rows,
    getRowId: params => params.data.row_id,
    onCellValueChanged: onCellValueChanged,
    onRowClicked: onRowClicked,
    onCellClicked: onCellClicked,
  };
  const div = document.getElementById("matrix-grid");
  gridApi = agGrid.createGrid(div, gridOptions);
}

let debounceTimer = null;
async function onCellValueChanged(event) {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(async () => {
    const row = event.data;
    const validity = await validateRow(row);
    // Apply forced values
    if (validity.forced_values) {
      for (const [k, v] of Object.entries(validity.forced_values)) {
        row[k] = v;
      }
      event.api.getRowNode(row.row_id).setData(row);
    }
    // Persist
    await fetch(`${API_BASE}/matrix/row/${row.row_id}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(row),
    });
  }, VALIDATE_DEBOUNCE_MS);
}

async function onRowClicked(event) {
  // If click was on a button, don't open drawer
  if (event.event?.target?.tagName === "BUTTON") return;
  const trialId = event.data.last_trial_id;
  if (trialId) openDrawer(trialId);
}

async function onCellClicked(event) {
  const target = event.event?.target;
  if (!target) return;
  if (target.classList.contains("btn-run")) {
    const rowId = target.dataset.rowId;
    await runRow(rowId);
  } else if (target.classList.contains("btn-delete")) {
    const rowId = target.dataset.rowId;
    await deleteRow(rowId);
  }
}

async function runRow(rowId) {
  const r = await fetch(`${API_BASE}/trials/${rowId}/run`, {method: "POST"});
  const j = await r.json();
  const trialId = j.trial_id;

  // Update row: status=running, last_trial_id=...
  const rowNode = gridApi.getRowNode(rowId);
  rowNode.setDataValue("status", "running");
  rowNode.setDataValue("last_trial_id", trialId);

  // Subscribe to SSE
  const es = new EventSource(`${API_BASE}/trials/${trialId}/stream`);
  es.onmessage = async (e) => {
    const data = JSON.parse(e.data);
    if (data.event === "trial_done") {
      es.close();
      // Reload trial to pull verdicts
      const tr = await fetch(`${API_BASE}/trials/${trialId}`);
      const trial = await tr.json();
      rowNode.setDataValue("status", trial.status);
      rowNode.setDataValue("verdicts", trial.verdicts || {});
      refreshDrawer(trialId);
    } else if (data.event === "status") {
      rowNode.setDataValue("status", data.status);
    }
  };
}

async function deleteRow(rowId) {
  if (!confirm("Delete this row?")) return;
  await fetch(`${API_BASE}/matrix/row/${rowId}`, {method: "DELETE"});
  gridApi.applyTransaction({remove: [{row_id: rowId}]});
}

document.getElementById("btn-add-row").addEventListener("click", async () => {
  const newRow = {
    framework: "langchain", api: "chat",
    stream: false, state: false,
    llm: "ollama", mcp: "NONE", routing: "via_agw",
  };
  const r = await fetch(`${API_BASE}/matrix/row`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(newRow),
  });
  const j = await r.json();
  gridApi.applyTransaction({add: [{row_id: j.row_id, ...newRow}]});
});

document.getElementById("btn-run-all").addEventListener("click", async () => {
  const rows = await fetchMatrix();
  for (const row of rows) {
    await runRow(row.row_id);
  }
});

initGrid();
setInterval(fetchProviders, PROVIDERS_REFRESH_MS);
```

- [ ] **Step 14.2: Commit**

```bash
git add frontend/app.js
git commit -m "feat(aiplay): frontend AG-Grid + SSE wiring

15-column grid per design §5.1; cell-edit debounced validate; action
buttons (Run / Delete) via cellRenderer; SSE subscription per trial to
update status + verdict pills live. Add/Run All/Delete actions on
toolbar. Providers refreshed every 30s so missing-key changes in .env
are picked up without full reload."
```

---

## Task 15: Frontend — drawer.js

**Files:**
- Create: `frontend/drawer.js`

- [ ] **Step 15.1: Write `frontend/drawer.js`**

Create `/my/ws/aiplay/frontend/drawer.js`:

```javascript
import { API_BASE } from "/config.js";

const drawerEl = document.getElementById("drawer");
const drawerTitle = document.getElementById("drawer-title");
const closeBtn = document.getElementById("drawer-close");
const tabBtns = document.querySelectorAll(".tab-btn");
const tabContents = {
  turns: document.getElementById("tab-turns"),
  verdicts: document.getElementById("tab-verdicts"),
  raw: document.getElementById("tab-raw"),
};

let currentTrialId = null;

closeBtn.addEventListener("click", () => {
  drawerEl.classList.add("hidden");
  currentTrialId = null;
});

tabBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    tabBtns.forEach(b => b.classList.remove("active"));
    Object.values(tabContents).forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    tabContents[btn.dataset.tab].classList.add("active");
  });
});

export async function openDrawer(trialId) {
  currentTrialId = trialId;
  drawerEl.classList.remove("hidden");
  drawerTitle.textContent = `Trial ${trialId.slice(0, 8)}…`;
  await refreshDrawer(trialId);
}

export async function refreshDrawer(trialId) {
  if (currentTrialId !== trialId) return;
  const r = await fetch(`${API_BASE}/trials/${trialId}`);
  if (!r.ok) {
    tabContents.turns.innerHTML = `<p>Error loading trial: ${r.status}</p>`;
    return;
  }
  const trial = await r.json();

  // Turns tab
  tabContents.turns.innerHTML = (trial.turns || []).map((t, i) => `
    <div class="turn-card">
      <h4>Turn ${i}: ${t.kind}</h4>
      <details><summary>Request</summary><pre>${JSON.stringify(t.request, null, 2)}</pre></details>
      <details><summary>Response</summary><pre>${JSON.stringify(t.response, null, 2)}</pre></details>
      <details><summary>Audit entries (${(trial.audit_entries || []).filter(a => a.turn_id === t.turn_id).length})</summary>
        <pre>${(trial.audit_entries || []).filter(a => a.turn_id === t.turn_id).map(a => JSON.stringify(a, null, 2)).join("\n\n")}</pre>
      </details>
    </div>
  `).join("") || "<p>No turns yet.</p>";

  // Verdicts tab
  const verdicts = trial.verdicts || {};
  const labels = {a: "Presence", b: "Channel structure", c: "Continuity", d: "Resilience", e: "State-mode gap"};
  tabContents.verdicts.innerHTML = ["a","b","c","d","e"].map(lvl => {
    const v = verdicts[lvl] || {verdict: "na", reason: "not computed"};
    return `
      <div class="verdict-card ${v.verdict}">
        <strong>(${lvl}) ${labels[lvl]}</strong> — <em>${v.verdict}</em><br>
        <small>${v.reason}</small>
      </div>
    `;
  }).join("");

  // Raw JSON tab
  document.getElementById("raw-json").textContent = JSON.stringify(trial, null, 2);
}
```

- [ ] **Step 15.2: Commit**

```bash
git add frontend/drawer.js
git commit -m "feat(aiplay): frontend drawer — 3 tabs (Turns/Verdicts/Raw JSON)

Turns tab renders per-turn cards with collapsible Request/Response/Audit
sections. Verdicts tab renders 5 cards (a-e) colored by pass/fail/na.
Raw JSON tab dumps the entire trial record. openDrawer + refreshDrawer
exported for app.js to call on row click + SSE trial_done. Turn Plan
tab deferred to Plan B (editable JSON with schema validation)."
```

---

## Task 16: End-to-end integration verification

**Files:**
- Modify: `docs/plans/2026-04-22-aiplay-v1-plan-a-mvp.md` (add a "results" section at the end once verification passes)

This task is manual end-to-end verification — no new code, just a smoke test of the full stack.

- [ ] **Step 16.1: Verify Ollama model available**

```bash
ollama list | grep qwen2.5
# Expected: qwen2.5:7b-instruct (or similar tag)
# If missing: ollama pull qwen2.5:7b-instruct
```

- [ ] **Step 16.2: Verify AGW image is built**

```bash
make -C /my/ws/aiplay check-agw
# Expected: ✅ agentgateway:cidgar found
```

- [ ] **Step 16.3: Validate compose config**

```bash
cd /my/ws/aiplay
docker compose config > /dev/null
# Expected: no output = valid
```

- [ ] **Step 16.4: Build + start the stack**

```bash
cd /my/ws/aiplay
docker compose build     # builds harness, adapter, 4 MCPs
docker compose up -d     # start all 7 services
docker compose ps        # all should be "Up"
```

- [ ] **Step 16.5: Verify harness-api is healthy**

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok","version":"plan-a-mvp"}
```

- [ ] **Step 16.6: Verify providers endpoint**

```bash
curl http://localhost:8000/providers | python -m json.tool
# Expected: JSON with 5 providers (NONE/ollama/claude/chatgpt/gemini);
# ollama.available=true, others depend on .env
```

- [ ] **Step 16.7: Open UI in browser**

```
http://localhost:8000
```

Expected: empty AG-Grid (no seeded rows — matrix is initially empty; user creates rows via "+ Add Row").

- [ ] **Step 16.8: Create a test row**

Click "+ Add Row" — a default row (langchain + chat + ollama + NONE + via_agw) is created.

Expected in grid: one row, Status=idle, all verdict pills = `—`.

- [ ] **Step 16.9: Click Run on the row**

Click the `▶` button in the Actions column.

Expected:
- Status pill → running (yellow)
- After ~5-30 seconds: Status → pass / fail; verdicts (a) and (b) show ✓ or ✗; (c) (d) (e) show `—`
- Click the row → drawer opens with 3 tabs populated

- [ ] **Step 16.10: Inspect audit entries in drawer**

Drawer → Turns tab → expand "Audit entries (N)" on each turn.

Expected: non-empty list of audit entries, each with `phase`, `cid`, `backend`, `turn_id` matching the turn.

If "Audit entries (0)":
- Check `docker compose logs agentgateway | grep governance` — should have entries
- Check `docker compose logs harness-api | grep audit` — should show AuditTail subscribing
- Most common cause: `RUST_LOG_FORMAT=json` not being honored; verify with `docker compose logs agentgateway | head -5` — should be JSON-shaped lines

- [ ] **Step 16.11: Document result in plan file**

Add at the end of `docs/plans/2026-04-22-aiplay-v1-plan-a-mvp.md`:

```markdown
## Plan A results (manual verification)

- [ ] Stack builds cleanly (`docker compose build`)
- [ ] All 7 services up (`docker compose ps` — all "Up")
- [ ] UI loads at http://localhost:8000
- [ ] First trial runs end-to-end
- [ ] Verdicts (a) presence + (b) channel structure return pass for a happy-path trial
- [ ] Audit entries visible in drawer

Signed off: <date>
```

- [ ] **Step 16.12: Commit**

```bash
cd /my/ws/aiplay
git add docs/plans/2026-04-22-aiplay-v1-plan-a-mvp.md
git commit -m "docs(aiplay): plan A results section (manual verification checklist)

End-to-end verification gates for Plan A sign-off. Build + service
liveness + UI access + first-trial happy path + verdicts a+b returning
pass + audit entries captured."
```

---

## Sanity checks summary

After completing all 16 tasks, the following should all pass:

```bash
cd /my/ws/aiplay

# All unit tests pass
python -m pytest tests/ -xvs
# Expected: 30+ tests pass (exact count: 33 as of this plan)

# Compose config valid
docker compose config > /dev/null
# Expected: no output

# AGW image present
make check-agw
# Expected: ✅ agentgateway:cidgar found

# Build clean
docker compose build
# Expected: all 7 services build (may take 5-10 minutes on first build)

# Stack up
make up-safe
# Expected: UI at http://localhost:8000

# First trial runs → pass verdicts a + b
# (manual browser step — covered by Task 16.9)
```

## What's out (explicit — see Plan B)

- Adapters: langgraph, crewai, pydantic-ai, autogen, llamaindex (5 remaining)
- Verdicts: (c) multi-turn continuity, (d) compaction resilience, (e) server-state-mode gap
- Turn-plan editor (CodeMirror) in drawer
- Clone-for-baseline row action
- Abort running trial action
- `compact` turn kind (framework-specific history mutation)
- `force_state_ref` turn kind (responses+state)
- `inject_ambient_cid` turn kind (pre-seed CID)
- Anthropic routes in agw/config.yaml (claude provider)
- OpenAI routes in agw/config.yaml (chatgpt/Responses API)
- Gemini route in agw/config.yaml
- MCP tool-calling via langchain-mcp-adapter (Plan A langchain+weather row is chat-only; LLM doesn't actually call MCP tools)

All of the above are documented in `docs/design.md` §10.1 (v1.1 candidates) and will form Plan B.
