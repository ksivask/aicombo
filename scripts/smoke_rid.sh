#!/usr/bin/env bash
# RID end-to-end smoke — runs a real RID-enabled trial through the live AGW and
# asserts (1) the Design B / CHG-26F RID audit shape and (2) verdict (b)'s
# C2-marker detection against AGW's REAL marker output.
#
# This is the true guard against AGW<->aiplay marker-format drift: the pytest
# unit tests use hand-written marker strings and CANNOT catch a wire-format
# change in AGW. This script feeds aiplay's detection logic AGW's actual bytes.
#
# Prereqs: stack up (`make up`), ollama reachable on the host, and the
# harness-api image rebuilt so efficacy.py is current (verdict (b) detection +
# verdict_l/m). Run BEFORE trusting a fresh AGW/harness build.
#
# Env:
#   AIPLAY_API   API base URL          (default http://localhost:8000)
#   SMOKE_ROW    matrix row to run     (default row-seed-01 = langchain/chat/
#                                        weather/via_agw/ollama → has MCP tool
#                                        phases so parent_run_rid is exercised)
#   SMOKE_POLL_S total seconds to wait (default 120)
#
# Exit 0 = all assertions pass; non-zero = first failure (message on stderr).
set -uo pipefail

API="${AIPLAY_API:-http://localhost:8000}"
ROW="${SMOKE_ROW:-row-seed-01}"
POLL_S="${SMOKE_POLL_S:-120}"

echo "RID smoke — API=$API row=$ROW"

trial_id=$(curl -fsS -X POST "$API/trials/$ROW/run" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('trial_id',''))") \
  || { echo "❌ failed to launch trial on row '$ROW' (is the stack up?)" >&2; exit 2; }
[ -n "$trial_id" ] || { echo "❌ no trial_id returned" >&2; exit 2; }
echo "launched trial $trial_id; polling up to ${POLL_S}s…"

status="running"
waited=0
while [ "$status" = "running" ] && [ "$waited" -lt "$POLL_S" ]; do
  sleep 4; waited=$((waited + 4))
  status=$(curl -fsS "$API/trials/$trial_id" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
done
echo "final status: $status (after ${waited}s)"
[ "$status" = "running" ] && { echo "❌ trial did not finish in ${POLL_S}s" >&2; exit 3; }
[ "$status" = "error" ]   && { echo "❌ trial errored at run level" >&2; exit 3; }

# Pull the finished trial to a temp FILE (not a pipe into python — the heredoc
# below already owns python's stdin; piping curl there too collides and python
# sees empty input). Pass the path as argv[2].
tmpjson="$(mktemp)"; trap 'rm -f "$tmpjson"' EXIT
curl -fsS "$API/trials/$trial_id" -o "$tmpjson" \
  || { echo "❌ failed to fetch finished trial $trial_id" >&2; exit 3; }
python3 - "$trial_id" "$tmpjson" <<'PY'
import json, re, sys

trial_id = sys.argv[1]
with open(sys.argv[2]) as _f:
    d = json.load(_f)
audits = d.get("audit_entries", []) or []
verdicts = d.get("verdicts", {}) or {}
fails = []

def body(e):
    return e.get("body") or {}

RID_RE = re.compile(r"^ibr_[a-f0-9]{12}$")

llm_req  = [e for e in audits if e.get("phase") == "llm_request"]
llm_resp = [e for e in audits if e.get("phase") == "llm_response"]
tool_ph  = [e for e in audits if e.get("phase") in ("tool_call", "tool_response")]

# 1. rid present + well-formed on every llm_request / llm_response (Design B + CHG-26F)
if not llm_req:
    fails.append("no llm_request audit entries")
for e in llm_req + llm_resp:
    rid = body(e).get("rid")
    if not (rid and RID_RE.match(rid)):
        fails.append(f"{e.get('phase')} has bad/absent rid: {rid!r}")
        break

# 2. provider_response_id captured on llm_response (CHG-26D emission)
if llm_resp and not any(body(e).get("provider_response_id") for e in llm_resp):
    fails.append("no llm_response carries provider_response_id")

# 3. parent_rid chain populated past genesis (CHG-26F handoff; CHG-26G accuracy)
non_genesis_with_parent = [e for e in llm_req if body(e).get("parent_rid")]
if len(llm_req) >= 2 and not non_genesis_with_parent:
    fails.append("parent_rid never populated on any non-genesis run (f2->f3 handoff broken)")

# 4. parent_run_rid on tool phases — MCP-call <-> requesting-LLM-run association
if tool_ph:
    bad = [e.get("phase") for e in tool_ph if not RID_RE.match(body(e).get("parent_run_rid") or "")]
    if bad:
        fails.append(f"tool phase(s) missing well-formed parent_run_rid: {bad}")
else:
    print("  note: trial had no tool phases (parent_run_rid not exercised) — use an MCP row")

# 5. verdict (b) — C2 combined-marker detection against AGW's real output.
#    This is the drift guard: requires the harness running the current
#    efficacy.py (MARKER_RE tolerant of `<!-- ib:cid=X,rid=Y -->`).
vb = verdicts.get("b", {})
if not vb:
    fails.append("verdict (b) not computed (harness may predate l/m registration / fix — rebuild harness-api)")
elif vb.get("verdict") not in ("pass", "na"):
    fails.append(f"verdict (b) = {vb.get('verdict')}: {vb.get('reason','')} "
                 "(combined-marker detection failed — MARKER_RE drift?)")

if fails:
    print(f"\n❌ RID smoke FAILED for {trial_id}:")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)

rids = [body(e).get("rid") for e in llm_req]
print(f"\n✅ RID smoke PASSED for {trial_id}")
print(f"   runs: {len(llm_req)}  rids: {rids}")
print(f"   verdict(b): {vb.get('verdict')} — {vb.get('reason','')[:70]}")
PY
