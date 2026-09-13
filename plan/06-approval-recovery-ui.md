# Phase 6 — Approval, Simulated Recovery, and Operator UI Plan

> **For Hermes:** The only write is to simulator state, and it must be bound to an exact approved plan hash. Keep the UI evidence-first and single-screen. No Kubernetes or Azure mutation code may exist.

**Goal:** Complete the user-visible loop: verified incident brief, human decision, simulator-only scale-out, deterministic recovery verification, and one-screen operator console.

**Architecture:** A deterministic renderer produces an approval card. The decision service signs the exact plan hash and expiry. A simulator executor accepts only matching approved plans. FastAPI exposes typed local endpoints; Streamlit renders the operator experience.

**Tech stack:** Pydantic, FastAPI, Streamlit, Plotly or Altair, pytest.

---

## Task 1: Implement canonical action-plan hashing

**Files:**

- Create: `src/infineq/actions/plans.py`
- Test: `tests/unit/actions/test_plans.py`

Canonicalize and hash:

- action type;
- target;
- source and destination replica counts;
- incident ID;
- simulator version;
- TTL/expiry;
- expected effect;
- risks;
- rollback/recovery conditions.

Reject unknown fields, reordered semantic differences, expired plans, mutable target aliases, and any action except `SIMULATED_SCALE_OUT` from one to two replicas.

## Task 2: Implement the human-decision record

**Files:**

- Create: `src/infineq/actions/approval.py`
- Test: `tests/unit/actions/test_approval.py`
- Test: `tests/safety/test_approval_binding.py`

A decision records:

- approve, reject, or request-more-evidence;
- exact plan hash;
- incident ID;
- actor label suitable for a local demo;
- timestamp and expiry;
- no secret or credential.

Test that approval for one plan/incident cannot authorize another, edited plans invalidate approval, expiry blocks execution, and replaying a consumed approval is denied.

## Task 3: Implement the simulator-only executor

**Files:**

- Create: `src/infineq/actions/simulator_executor.py`
- Test: `tests/unit/actions/test_simulator_executor.py`

**Rules:**

- receives no Azure or Kubernetes client;
- verifies policy, plan hash, approval, target, current state, and expiry;
- changes simulated replicas from one to two once;
- writes an append-only action event;
- returns a typed result;
- supports TTL rollback inside simulator state only;
- rejects all real target schemes and arbitrary parameters.

Add a source-code safety test that fails if action modules import Kubernetes, Azure Resource Manager, shell/process, or SSH libraries.

## Task 4: Implement deterministic recovery verification

**Files:**

- Create: `src/infineq/actions/recovery.py`
- Test: `tests/unit/actions/test_recovery.py`
- Test: `tests/integration/test_action_recovery.py`

Recovery succeeds only when the final 30-second window has:

- p95 TTFT ≤2,000 ms;
- queue depth ≤2 for three consecutive five-second checks;
- error rate <1%;
- ITL within ±10% of baseline median;
- two simulated replicas ready.

Test full recovery, partial recovery, metric missing, action rejected, wrong plan, and queue not drained. Never infer recovery from action completion alone.

## Task 5: Create the deterministic incident brief renderer

**Files:**

- Create: `src/infineq/ui/presenters.py`
- Test: `tests/unit/ui/test_presenters.py`

Render only structured fields:

- synthetic provenance;
- observed symptoms;
- evidence timeline;
- ranked hypotheses;
- support, contradiction, and missing evidence;
- Verifier result;
- exact action card;
- approval state;
- recovery criteria and outcome.

No hidden reasoning or raw model response appears in the UI.

## Task 6: Build typed local API endpoints

**Files:**

- Create: `src/infineq/api/app.py`
- Create: `src/infineq/api/routes.py`
- Test: `tests/integration/test_api.py`

**Endpoints:**

- `GET /health`
- `GET /episodes/{id}`
- `POST /episodes/{id}/investigate`
- `GET /incidents/{id}`
- `POST /incidents/{id}/decision`
- `POST /incidents/{id}/apply-simulation`
- `GET /incidents/{id}/recovery`

All state-changing endpoints require the current plan hash. CORS is local-only. No endpoint accepts a filesystem path, arbitrary query, or executor command.

## Task 7: Build the single-screen Streamlit console

**Files:**

- Create: `src/infineq/ui/app.py`
- Create: `src/infineq/ui/theme.py`
- Test: `tests/unit/ui/test_view_models.py`
- Create: `tests/manual/ui-checklist.md`

**Information hierarchy:**

1. permanent `SYNTHETIC REPLAY` banner;
2. incident state, detector time, SLO;
3. before/now metrics strip;
4. evidence timeline;
5. hypothesis comparison table;
6. Verifier panel;
7. exact decision card with plan hash and TTL;
8. Approve simulation, Reject, Request more evidence;
9. expected-versus-observed recovery panel.

Disable approval until verification and deterministic policy both pass. Use accessible colors and labels; do not rely on red/green alone.

## Task 8: Rehearse all operator paths

**Manual paths:**

- canonical queue → approve → recovery verified;
- canonical queue → reject → no action;
- missing evidence → no action card;
- failed recovery → `recovery_not_verified`;
- expired/tampered approval → blocked;
- page refresh → incident state remains coherent.

Capture temporary screenshots for review, but do not call them submission evidence until Phase 8.

## Phase 6 exit gate

- [ ] No action executes without exact, unexpired approval.
- [ ] Source-code import guard proves no real infrastructure clients exist in executor code.
- [ ] Recovery is measurement-based and can fail visibly.
- [ ] All five manual paths work.
- [ ] UI prominently labels synthetic replay.
- [ ] UI fits one screen at the target recording resolution.
- [ ] `unset PYTHONPATH && uv run pytest tests/unit/actions tests/unit/ui tests/integration/test_action_recovery.py tests/integration/test_api.py tests/safety/test_approval_binding.py -q` passes.
- [ ] Local phase commit created; nothing pushed.
