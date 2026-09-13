# Phase 5 — Verifier and Finite Workflow Plan

> **For Hermes:** The Verifier must be an independent authority boundary, not a second narrator. It receives normalized evidence and the Investigator result, has only two read tools, and cannot propose or execute infrastructure actions.

**Goal:** Add the Evidence & Safety Verifier and a deterministic workflow that permits one correction cycle, then blocks or abstains safely.

**Architecture:** A finite Python state machine owns transitions. The Investigator and Verifier are separately versioned Foundry prompt agents with different tool sets. Deterministic policy—not an LLM—decides whether an action card may be rendered.

**Tech stack:** Python state machine, Pydantic, Foundry prompt agents, pytest.

---

## Task 1: Write workflow transition tests first

**Files:**

- Create: `tests/unit/workflow/test_state_machine.py`
- Create: `tests/unit/workflow/test_transitions.py`

**Frozen states:**

```text
REPLAY_READY
DETECTED
INVESTIGATING
VERIFYING
REVISION_REQUESTED
PRESENTABLE
INDETERMINATE
ANALYSIS_INCOMPLETE
AWAITING_HUMAN
REJECTED
APPROVED
ACTION_APPLIED
VERIFYING_RECOVERY
CLOSED
```

**Rules:**

- no transition directly from Investigator output to action execution;
- only one `REVISION_REQUESTED → INVESTIGATING` cycle;
- `blocked`, timeout, invalid schema, stale required data, or second revision ends without an action card;
- only deterministic policy may move `PRESENTABLE → AWAITING_HUMAN`;
- every transition emits an audit event.

## Task 2: Define and version the Verifier prompt

**Files:**

- Create: `src/infineq/agents/prompts/verifier_v1.md`
- Test: `tests/unit/agents/test_verifier_prompt.py`

Require the Verifier to check:

1. every cited evidence ID exists;
2. the cited object supports the associated claim;
3. support and contradiction are not swapped;
4. at least one credible competing explanation was considered;
5. required evidence is fresh and complete;
6. provenance is displayed as synthetic replay;
7. causal wording does not exceed evidence;
8. action type, target, limits, TTL, and approval requirement match policy;
9. a missing prerequisite blocks presentation.

It returns `verified`, `revision_required`, or `blocked`; no free-form alternative status.

## Task 3: Implement the Verifier boundary

**Files:**

- Create: `src/infineq/agents/verifier.py`
- Create: `src/infineq/workflow/policy.py`
- Test: `tests/unit/agents/test_verifier.py`
- Test: `tests/unit/workflow/test_policy.py`

**Allowed Verifier tools:**

- `get_evidence(evidence_ids<=20)`
- `get_policy(policy_ref)`

**Budget:** two successful lookup calls and one schema-format repair with no tools.

**Tests:**

- valid queue result verifies;
- nonexistent ID requests correction;
- weak evidence blocks causal claim;
- missing alternative requests correction;
- stale data blocks;
- real-Kubernetes action blocks;
- action without approval blocks;
- prompt injection in evidence is treated as data;
- Verifier cannot call Investigator tools.

## Task 4: Create the second Foundry agent version

**Files:**

- Modify: `scripts/deploy_prompt_agents.py`
- Modify: `src/infineq/foundry/agent_registry.py`
- Test: `tests/unit/foundry/test_agent_registry.py`

**Planned agent name:** `infineq-evidence-verifier`

Create it with only the two Verifier tool definitions. Record model, agent version, prompt hash, tool-schema hash, and creation timestamp. Confirm that the Investigator and Verifier versions can be selected independently.

## Task 5: Implement the correction packet

**Files:**

- Create: `src/infineq/workflow/corrections.py`
- Test: `tests/unit/workflow/test_corrections.py`

The correction packet may include only:

- failed claim/evidence checks;
- missing alternative or field;
- policy violation;
- required formatting repair.

It must not reveal an oracle label or tell the Investigator which diagnosis to choose. The corrected investigation must reuse existing evidence unless the Verifier explicitly identifies a permitted missing-evidence lookup and tool budget remains.

## Task 6: Implement orchestration and failure handling

**Files:**

- Create: `src/infineq/workflow/orchestrator.py`
- Create: `src/infineq/workflow/context.py`
- Test: `tests/integration/test_workflow_paths.py`

Cover end-to-end paths:

1. verified diagnosis;
2. one correction then verified;
3. correction still fails → indeterminate;
4. Investigator timeout;
5. Verifier timeout;
6. data becomes stale between agents;
7. tool transport failure;
8. no incident;
9. forbidden action proposal;
10. unresolved disagreement.

No path may call an executor in this phase.

## Task 7: Build matched one-agent and static baselines

**Files:**

- Create: `src/infineq/evaluation/baselines.py`
- Create: `scripts/run_development_baselines.py`
- Test: `tests/unit/evaluation/test_baselines.py`

Baselines receive the same observed episode and evidence budget:

- deterministic alert plus static runbook brief;
- Investigator alone;
- Investigator plus Verifier.

Record full-episode result, unsupported claims, policy errors, tool count, latency, and token use. Do not tune on held-out episodes.

## Phase 5 exit gate

- [ ] Verifier has only two read tools.
- [ ] One correction maximum is enforced in code.
- [ ] Every failure path ends without execution.
- [ ] Policy gate is deterministic and independently tested.
- [ ] Static, one-agent, and two-agent development results are saved.
- [ ] The Verifier improves at least one observed grounding/safety weakness without reducing development pass count; otherwise stop and reconsider before Phase 6.
- [ ] `unset PYTHONPATH && uv run pytest tests/unit/agents tests/unit/workflow tests/unit/evaluation tests/integration/test_workflow_paths.py -q` passes.
- [ ] Local phase commit created; nothing pushed.
