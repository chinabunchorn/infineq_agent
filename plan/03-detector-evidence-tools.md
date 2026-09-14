# Phase 3 — Detector and Evidence Tools Plan

> **For Hermes:** Keep anomaly detection numerical and deterministic. Agents must never read raw files or construct unrestricted queries. Use TDD for every rule and tool boundary.

**Goal:** Convert replay telemetry into a typed IncidentPacket and expose only the frozen, allow-listed evidence tools.

**Architecture:** The detector reads normalized episode events through an evidence store, validates quality, evaluates fixed rolling windows, and emits observations. Tool functions accept strict schemas, enforce episode/window/limit allow lists, and return immutable evidence references.

**Tech stack:** Python, Pydantic, pytest, read-only local evidence store.

---

## Task 1: Build the read-only evidence store

**Files:**

- Create: `src/infineq/evidence/store.py`
- Create: `src/infineq/evidence/paths.py`
- Test: `tests/unit/evidence/test_store.py`
- Test: `tests/safety/test_path_policy.py`

**Rules:**

- Store root is fixed at `data/infineq/v1/observed`.
- Episode IDs must match an opaque allow-listed pattern.
- Resolve and verify paths remain under the observed root.
- No glob, arbitrary filename, URI, SQL, PromQL, or shell parameter is accepted.
- Data is opened read-only.
- Missing files become typed `data_missing`; they are never silently treated as empty or zero.

## Task 2: Implement data-quality validation

**Files:**

- Create: `src/infineq/detection/quality.py`
- Test: `tests/unit/detection/test_quality.py`

**Checks:**

- schema version;
- source/episode identity;
- timestamp ordering;
- unit consistency;
- freshness ≤10 seconds in replay time;
- sample count ≥20 for detector decision;
- required labels;
- counter-reset marker;
- contradictory replica/revision state;
- evidence-index integrity.

Quality failures populate `DataQuality`; they cannot be converted into a diagnosis.

## Task 3: Implement rolling aggregations

**Files:**

- Create: `src/infineq/detection/windows.py`
- Test: `tests/unit/detection/test_windows.py`

**Required functions:**

- trailing-window selection;
- p50/p95 with the frozen interpolation rule;
- request/error rate;
- queue depth persistence;
- baseline ratio/change;
- median ITL and token distribution;
- detector/SLO crossing timestamp.

Test inclusive/exclusive boundaries, sparse windows, duplicate timestamps, and deterministic ordering.

## Task 4: Implement detector policy v1

**Files:**

- Create: `src/infineq/detection/detector.py`
- Create: `src/infineq/detection/policy_v1.py`
- Test: `tests/unit/detection/test_detector.py`
- Test: `tests/integration/test_detector_corpus.py`

**Frozen evaluation:** every five seconds over the trailing 15 seconds.

Emit an incident only when:

1. quality gates pass;
2. at least 20 requests are available;
3. p95 TTFT ≥1,000 ms for two consecutive evaluations or error rate ≥5%; and
4. a second signal family corroborates the symptom.

**Output:** `IncidentPacketV1` containing measurements, evidence IDs, baseline/observation windows, SLO, detector rule/version, deployment snapshot, and quality state—never a fault label.

**Tests:** healthy, benign, canonical queue, backend, workload shift, restart, missing, and counter-reset cases.

## Task 5: Implement the allow-listed tools

**Files:**

- Create: `src/infineq/evidence/tools.py`
- Create: `src/infineq/evidence/tool_schemas.py`
- Test: `tests/unit/evidence/test_tools.py`
- Test: `tests/safety/test_tool_allowlist.py`

**Investigator tools:**

- `get_incident_packet(incident_id)`
- `get_signal_window(incident_id, signal_enum, window_enum)`
- `get_request_samples(incident_id, window_enum, limit<=20)`
- `get_deployment_snapshot(incident_id, window_enum)`
- `search_runbook(query_enum, top_k<=3)`
- `get_evidence(evidence_ids<=20)`
- `prepare_action_plan(incident_id, action_type)`—dry run only

**Verifier tool:**

- `get_policy(policy_ref)` plus `get_evidence`

**Security tests:**

- reject unknown signal/window/action enums;
- reject limit >20;
- reject traversal, URL, shell metacharacters, and arbitrary query text;
- reject evidence IDs from a different episode;
- ensure responses contain no oracle fields or filesystem paths;
- ensure `prepare_action_plan` cannot execute anything.

## Task 6: Create curated runbook evidence

**Files:**

- Create: `data/infineq/v1/knowledge/runbook_chunks.jsonl`
- Create: `src/infineq/evidence/runbooks.py`
- Test: `tests/unit/evidence/test_runbooks.py`

Each chunk must contain:

- immutable runbook evidence ID;
- symptom family;
- prerequisites;
- diagnostic checks;
- bounded action class;
- approval requirement;
- rollback and verification criteria;
- source URL, retrieval date, and version;
- no executable shell text supplied to the model.

Use enum/filter retrieval for v1, not embeddings or open-ended RAG.

## Task 7: Add tool audit records

**Files:**

- Create: `src/infineq/evidence/audit.py`
- Test: `tests/unit/evidence/test_audit.py`

Record tool name, validated arguments hash, agent role, start/end time, status, returned evidence IDs, and redacted error. Never store credentials, raw prompts, or hidden oracle fields.

## Phase 3 exit gate

- [x] Detector emits no root-cause label.
- [x] Detector behavior matches every development oracle and records SLO lead time rather than assuming it.
- [x] All tools reject unrestricted paths/queries and return typed evidence.
- [x] Oracle root cannot be reached through any tool.
- [x] Runbook evidence preserves source/version.
- [x] Tool audit logs are redacted.
- [x] `unset PYTHONPATH && uv run pytest tests/unit/detection tests/unit/evidence tests/integration/test_detector_corpus.py tests/safety/test_path_policy.py tests/safety/test_tool_allowlist.py -q` passes.
- [x] Local phase commit created after the gate; remote push occurred only after explicit user approval.

## Verified Phase 3 gate evidence

- The targeted audit rerun `uv run pytest tests/unit/evidence/test_audit.py` passed with 11 tests.
- The exact Phase 3 gate passed with 104 tests. The exact Phase 2 compatibility gate passed with 77 tests.
- Full pytest passed with 221 tests and 91.39% total coverage (the project threshold is 90%). Ruff check passed; Ruff format check passed with 86 files already formatted; mypy passed with 36 source files; `uv lock --check` passed; and `git diff --check` passed.
- Corpus regeneration `unset PYTHONPATH && uv run python scripts/generate_corpus.py --version v1 --verify-reproducible` returned 24 episodes, 8 development, 16 held-out, `reproducible=true`, and observed-tree SHA-256 `7cb9d1195bcd508874ff08722b63a552eee7ebe6619500e86934fff5caf847fd`. The checked-in five-record runbook catalog remains present after regeneration.
- The eight development oracle behaviors matched exactly: `ep-a91e7c` no_sustained_incident; `ep-f02b4d` no_sustained_incident; `ep-61d8aa` warning at detector 75s with primary TTFT, SLO crossing 80s, lead 5s; `ep-c43f91` warning at 80s with primary TTFT, SLO crossing 105s, lead 25s; `ep-0e7ab3` warning at 70s with primary error rate; `ep-d8c214` warning at 70s with primary TTFT; `ep-4b6fa0` warning at 70s with primary TTFT and SLO crossing 65s, lead -5s; and `ep-e35192` abstain. Every triggered primary was only TTFT or error rate, and required oracle evidence was present without exposing oracle fields.
- Tool evidence includes allow-list/path safety tests, cross-episode evidence rejection, dry-run-only action planning, five immutable runbook chunks with source URLs/version/date, and redacted audit records. No remote write or push was performed during these checks.
- The Sol-high orchestrator independently inspected the staged detector, evidence-store, tool, runbook, audit, simulator-compatibility, and test changes. Its added-line/AST security scan found zero hardcoded-secret, shell-execution, `eval`/`exec`, pickle, SQL-interpolation, or unsafe-call findings; its direct detector replay matched all eight development oracles and confirmed that only TTFT or error rate can be a primary trigger. The final exit-gate verdict was pass.
