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

**Status: development exit gate passed on v11; no action was executed.** The authorized
`verifier-v2` Foundry version **2** was created and its
prompt, tool schema, model and response schema were read back. Investigator version **14**
remained pinned. One authorized eight-episode live development run was saved as
`run-phase5-development-live-v9` (24 mode records). It did **not** pass the exit gate:
all three modes passed **3/8**, two-agent justification is **false**, and the canonical
queue case `ep-61d8aa` did not reach a verified action card. Zero actions were executed.
The saved v9 artifacts were validated and must not be rewritten or treated as proof that
subsequent local fixes work live.

One separately authorized live evaluation, `run-phase5-development-live-v10`, stopped
after writing only the first **three** episode directories. Its first successful
revision exposed a validator bug: the one-agent mode correctly records only the initial
Investigator response IDs, but the validator compared it against the initial **plus**
two-agent correction responses. The CLI stopped with `LiveArtifactError` before writing
an aggregate manifest or evaluation. This is **partial, unscored evidence**, not a 3/8
result and not a completed eight-episode rerun. The three saved episodes individually
validate under their historical `1.0` artifact schema after correcting the validator;
v10 must remain untouched. No action was executed. In the saved canonical episode
`ep-61d8aa`, the Investigator proposed a 3600-second action, the Verifier requested a
typed correction, the Investigator completed one revision, and the second Verifier
request was denied by the one-revision limit. It ended blocked without an action card.
The saved Verifier trace shows its second invocation fetched only one evidence object;
the final result requests retrieval for cited claims. The `1.0` artifact did not retain
the revision result, initial Verifier decision, or correction packet, so **we cannot
establish** whether the revision fixed the TTL or exactly why the second check still
lacked evidence. Future `1.1` episode artifacts persist all three typed revision-stage
objects with validation and retain `1.0` historical compatibility. The validator
links one-agent responses to the initial invocation and separately checks the
correction responses. These fixes were made after the v10 failure, not measured live.

The root cause of the second-pass evidence failure was at the Verifier's read boundary:
it accepted a model-requested subset of one ID even though the corrected investigation
cited more IDs. The missing lookup was then wrongly sent to the Investigator as a
correction. The read-only executor now expands a `get_evidence` request to include every
cited ID in the **same single bounded batch**, on either pass. It never fabricates a
returned record: the tool still resolves each ID, and nonexistent IDs fail closed.
If the Verifier skips evidence lookup entirely, that is a Verifier block, not an
Investigator revision. Evidence IDs truncated out of a tool output no longer count as
seen by the model. These are local boundary changes; the Foundry `verifier-v2` prompt
and version were not changed or redeployed.

A new complete run, `run-phase5-development-live-v11`, used existing exact pins
(Investigator version **14**, Verifier version **2**) and saved **eight episodes and 24
mode records**. Both the CLI and a separate local artifact validation completed
successfully. Static and one-agent baselines passed **3/8** each; two-agent passed
**4/8**. Unsupported claims remained **0** in all three modes. Policy errors were **1**
for Investigator alone and **0** for the two-agent mode. The canonical queue case
`ep-61d8aa` ended `complete` with a verified, eligible simulator action card; its
revised plan had a **300-second TTL**, required human approval, and both Verifier
evidence lookups returned all **eight cited IDs**. The final Verifier result was
`verified` with five claim checks and no issues or correction requests. No action was
executed in any episode (`action_execution_count=0`). The two-agent justification
gate is **true** under the corrected canonical criterion. Two other two-agent episodes
still ended `invalid_schema`; this development result is not a claim of perfect model
reliability, production safety, or held-out performance. Historic v8/v9 and partial
v10 results remain unchanged. This continuation used **one** new cloud evaluation,
with **zero** deployments.

The preserved `run-phase5-development-live-v8` has 24 records (eight episodes × three
modes), with 3/8 full passes per mode. Its archived `two_agent_justification_passed=true`
was too permissive: the queue action case `ep-61d8aa` did not produce a verified card.
In-memory rescore with the corrected gate leaves pass counts at 3/8 but sets justification
**false**. Historical run artifacts must not be rewritten. One Investigator result cites a
runbook chunk ID rather than an `ev:` evidence ID; a second has an empty final output.
Both remain fail-closed, and the repair instruction now distinguishes those ID types.

In v9 the Investigator proposed 3600 seconds in `ep-4b6fa0` and 900 seconds in both
`ep-61d8aa` and `ep-c43f91`, while the frozen policy requires 300 seconds. The Verifier
requested a TTL correction, but the typed correction builder rejected that allow-listed
request, so all three two-agent episodes ended `invalid_schema` before a correction
attempt. After fixing that mapping, replay of the **saved** model outputs exposed a
second obstacle: all three revision packets said only "Repair the policy violation"
and had no permitted extra lookup. The local typed packet now supplies the exact,
allow-listed 300-second duration without mutating the Investigator's pinned v14 prompt
or silently rewriting its proposed plan. Tests reject model-supplied alternate durations;
the revised plan still requires independent verification. This is an offline input-contract
fix, **not** evidence that Investigator v14 will obey it live. The archived v9 distribution
is three `abstained`, two `blocked`, and three `invalid_schema` for the two-agent mode,
with three policy errors under its original evaluation.
`ep-4b6fa0` also has one deployed but **zero ready** replicas. The read-only
`prepare_action_plan` tool rejects a scale-out plan there, but the deterministic
presentation gate previously accepted a matching 1→2 plan despite that unready source.
It now rejects a plan unless both deployed and ready counts equal the policy source
replica count. That case must stay ineligible even if its TTL is corrected; the
canonical `ep-61d8aa` has one ready replica. A local counterfactual using the **saved**
three v9 Investigator outputs and changing only their TTL to 300 seconds confirms that
the action-policy check denies `ep-4b6fa0` for readiness but allows `ep-61d8aa` and
`ep-c43f91`. This checks only the deterministic policy, not whether Investigator v14
will make the revision or Verifier v2 will approve it. The stricter local gate does not
rescore or edit the archived v9 evidence.
The v9 Investigator/Verifier also demonstrated that model-written claim notes can say
"300-second TTL" while the structured action timestamps disagree; deterministic policy
correctly rejects the action.

The live scorer now counts failed claim checks symmetrically even when the Verifier
blocks a card; the per-episode justification and saved aggregate validator enforce the
canonical action criterion. Current validation rejects stale green evaluation flags
instead of treating a matched pair of old flags as acceptance evidence. The archived v8
files remain unchanged and historical. The local `created_at` field may be an observation
timestamp when Foundry omits its own timestamp; do not call it a provider-verified
creation-time pin. Version, prompt, tool, model and response-schema pins **were** read back.

- [x] Verifier has only two read tools in the local boundary.
- [x] One correction maximum is enforced in code.
- [x] Every tested failure path ends without execution.
- [x] Policy gate is deterministic and independently tested.
- [x] Static, one-agent, and two-agent development records were saved (v8 and v9, historical).
- [x] A **new** full eight-episode run after the local correction fix improves a
  grounding/safety weakness **without reducing full-pass count**, and an action-eligible
  episode (including canonical queue `ep-61d8aa`) reaches the verified action-card state.
  A decline in unsupported claims achieved only by blocking the action is not sufficient.
- [x] `unset PYTHONPATH && uv run pytest tests/unit/agents tests/unit/workflow tests/unit/evaluation tests/integration/test_workflow_paths.py -q` passes locally.
- [x] Full local suite and static checks pass before v11: 560 passed, one opt-in Azure
  test skipped; total coverage 90.05%; Ruff, format, mypy, lock, and diff checks pass.
- [x] Phase 5 development gate reviewed against fresh live evidence and saved artifacts.
  A local phase commit has **not** been made; no push/PR or infrastructure action occurred.

### Next boundary

Phase 5's **development** gate passed on the complete v11 run. Do not edit the saved
v8–v11 evidence to improve its scores, infer held-out performance from eight development
episodes, execute a proposed action without approval, or treat this as production readiness.
Phase 6 is a separate approval/recovery UI task; it was not started in this continuation.
