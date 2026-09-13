# Phase 1 — Project Foundation and Contracts Plan

> **For Hermes:** Use `test-driven-development`. This phase creates local project scaffolding and typed contracts only. It must not implement the simulator, agents, or Azure deployment.

**Goal:** Establish a reproducible Python 3.13 project with secure configuration, frozen schemas, and a verified Foundry connectivity smoke test.

**Architecture:** A `src/` package keeps deterministic domain logic independent from Foundry adapters. Pydantic models define every boundary before behavior is implemented.

**Tech stack:** Python 3.13, `uv`, Pydantic v2, pytest, Ruff, mypy, `azure-ai-projects>=2.4.0`, `azure-identity`, `python-dotenv` for local-only settings.

---

## Task 1: Initialize the package and ignore rules

**Files:**

- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `README.md`
- Create: `src/infineq/__init__.py`
- Create: `tests/__init__.py`

**Steps:**

1. Write a failing repository-layout test in `tests/unit/test_project_layout.py`.
2. Run `unset PYTHONPATH && /opt/homebrew/bin/python3.13 -m pytest tests/unit/test_project_layout.py -v`; expect collection/setup failure before dependencies exist.
3. Create `pyproject.toml` with Python `>=3.13`, runtime dependencies, and dev extras.
4. Add Ruff, mypy, pytest, and coverage configuration.
5. Ignore `.env`, `.venv`, caches, local trace exports, `data/**/runs/`, and any downloaded public trace.
6. Keep committed fixtures, corpus manifests, observed episodes, and hidden oracle fixtures explicitly allow-listed later.
7. Create the environment:

```bash
unset PYTHONPATH
uv venv --python /opt/homebrew/bin/python3.13
uv sync --all-groups
```

8. Rerun the layout test; expect pass.

## Task 2: Implement environment configuration

**Files:**

- Create: `src/infineq/config.py`
- Test: `tests/unit/test_config.py`

**Required settings:**

- `AZURE_AI_PROJECT_ENDPOINT`
- `AZURE_AI_MODEL_DEPLOYMENT_NAME`
- `INFINEQ_DATA_ROOT`
- `INFINEQ_RUNS_ROOT`
- `INFINEQ_LOG_LEVEL`
- `INFINEQ_ENV=local|test|foundry`

**Steps:**

1. Write tests proving missing Azure settings fail only when a Foundry client is requested—not when deterministic tests run.
2. Test endpoint scheme/host validation and rejection of embedded credentials.
3. Implement `Settings` without logging values.
4. Put placeholders, never real values, in `.env.example`.
5. Verify `.env` is ignored using `git check-ignore .env`.

## Task 3: Freeze Pydantic domain schemas

**Files:**

- Create: `src/infineq/schemas/common.py`
- Create: `src/infineq/schemas/evidence.py`
- Create: `src/infineq/schemas/incident.py`
- Create: `src/infineq/schemas/investigation.py`
- Create: `src/infineq/schemas/verification.py`
- Create: `src/infineq/schemas/action.py`
- Create: `src/infineq/schemas/recovery.py`
- Test: `tests/unit/schemas/`

**Contracts:**

- `EvidenceRef`
- `SignalObservation`
- `DataQuality`
- `IncidentPacketV1`
- `Hypothesis`
- `InvestigationResultV1`
- `VerificationResultV1`
- `ActionPlanV1`
- `HumanDecisionV1`
- `RecoveryResultV1`

**Steps for each schema:**

1. Write failing valid/invalid fixture tests.
2. Require `schema_version`, opaque IDs, origin, UTC timestamp, unit, window, and evidence references where applicable.
3. Use enums for incident family, disposition, provenance, verification status, action type, and recovery state.
4. Reject unknown fields at trust boundaries.
5. Enforce `requires_human_approval=true` for every state-changing plan.
6. Allow only `SIMULATED_SCALE_OUT` in v1.
7. Require evidence-backed claims for diagnosed actions, verified verification results, and verified recovery results.
8. Generate JSON Schema snapshots under `tests/fixtures/schema_snapshots/` and fail on accidental drift.

## Task 4: Define error taxonomy and redaction

**Files:**

- Create: `src/infineq/errors.py`
- Create: `src/infineq/security/redaction.py`
- Test: `tests/unit/test_errors.py`
- Test: `tests/safety/test_redaction.py`

**Required normalized errors:**

- `configuration_error`
- `schema_error`
- `data_missing`
- `data_stale`
- `tool_transport_error`
- `tool_policy_denied`
- `agent_timeout`
- `verification_blocked`
- `approval_mismatch`
- `recovery_not_verified`

**Redaction tests:** API keys, bearer tokens, client secrets, OAuth access/refresh tokens, connection strings, subscription/tenant identifiers, email addresses, and prompt/completion bodies must not appear in normal logs.

## Task 5: Add the Foundry client boundary and smoke check

**Files:**

- Create: `src/infineq/foundry/client.py`
- Create: `scripts/check_foundry.py`
- Test: `tests/unit/foundry/test_client.py`

**Steps:**

1. Define a small client protocol so unit tests use a fake.
2. Use `AIProjectClient(endpoint=..., credential=DefaultAzureCredential())` only in the concrete adapter.
3. Add a read-only smoke script that requests a short deterministic model response and prints only success, latency, deployment name, and request/trace identifier.
4. Return a nonzero exit status when the model response is empty or does not exactly match the smoke sentinel.
5. Never print a credential or full environment dump.
6. Run unit tests with no Azure access.
7. Run the live smoke only after Phase 0 is complete:

```bash
unset PYTHONPATH
uv run python scripts/check_foundry.py
```

Expected: exit 0 and an exact successful model response.

## Task 6: Add quality gates

**Files:**

- Modify: `pyproject.toml`
- Create: `scripts/verify.sh` only if a cross-platform Python task runner is not used

**Verification commands:**

```bash
unset PYTHONPATH
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=infineq --cov-report=term-missing -q
uv lock --check
```

The project enforces at least 90% line coverage. All commands must pass from a clean checkout with only `.env` added locally.

## Task 7: Make the local commits

Only after all tests, quality gates, and independent review pass:

```bash
git add -A
git commit -m "chore: establish Infineq project contracts"
git commit -m "fix: tighten phase one evidence boundaries"
```

The implementation was committed locally as:

- `0dd4340` — `chore: establish Infineq project contracts`
- `4649d3b` — `fix: tighten phase one evidence boundaries`

Do not push. The remote currently has no usable branch, and remote writes require explicit user approval.

## Phase 1 exit gate — reached 2026-09-13

- [x] Python 3.13 environment recreates with `uv sync --all-groups`.
- [x] `.env` and run artifacts are ignored; verified with `git check-ignore`.
- [x] All frozen schema valid/invalid tests pass.
- [x] Schema snapshots are committed and protected by drift tests.
- [x] Unit tests require no Azure access.
- [x] Live Foundry model smoke succeeds without key authentication, using `DefaultAzureCredential`, against deployment `infineq-gpt-5-4-mini`.
- [x] Ruff, mypy, coverage, pytest, and `uv lock --check` pass.
- [x] Independent final review passed with no security concerns or logic errors.
- [x] Local commits exist; nothing was pushed.

### Recorded verification evidence

- Full suite: `68 passed`.
- Coverage: `91.24%`, with the configured 90% minimum reached.
- Live smoke: exact sentinel match, `success=true`, deployment `infineq-gpt-5-4-mini`.
- Package wheel build/install smoke passed in a fresh Python 3.13 environment.
- `pip-audit`: no known vulnerabilities found.
- Git working tree: clean.

## Scope boundary

Phase 1 is complete. Do not add simulator arithmetic, dataset generation, detector thresholds, Foundry agents, workflow orchestration, UI, or infrastructure integrations to this phase. Those belong to Phases 2–8.

## Next phase

Proceed to [Phase 2 — Dataset and simulator](02-dataset-simulator.md) only after explicitly opening a new phase gate. Keep the first remediation simulator-only and preserve the observed/hidden-oracle separation.

---

## Phase 1 retrospective fixes

The independent review identified and the implementation corrected:

1. OAuth credential names and values were added to the redaction boundary.
2. A nonmatching smoke response now produces `success=false` and a nonzero script exit.
3. Verified recovery requires measured values and cited evidence.
4. Verified verification results require at least one supported, cited claim.
5. Complete hypotheses and proposed actions require supporting evidence citations.
6. A 90% coverage threshold is now enforced by project configuration.

These fixes are part of commit `4649d3b` and were reverified by the final independent review.
