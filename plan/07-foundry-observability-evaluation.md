# Phase 7 — Foundry Observability, Evaluation, and Runtime Plan

> **For Hermes:** Foundry must be visible and meaningful, but do not put the working local prototype at risk for an optional hosted deployment. Deterministic oracle scoring remains primary; Foundry LLM evaluators supplement it.

**Goal:** Trace the full Infineq workflow, evaluate the frozen held-out corpus, compare baselines, and establish a stable Foundry-backed demo runtime.

**Architecture:** Server-side Foundry traces cover each agent. Client-side OpenTelemetry spans cover deterministic orchestration, tools, approval, and recovery. Foundry evaluation scores agent quality/tool behavior, while local deterministic scorers own diagnostic truth and safety gates.

**Tech stack:** Application Insights, OpenTelemetry, `azure-ai-projects>=2.4.0`, Foundry evaluations, optional hosted-agent code deployment.

---

## Task 1: Verify server-side tracing

**Files:**

- Create: `scripts/check_tracing.py`
- Test: `tests/unit/foundry/test_trace_metadata.py`

**Steps:**

1. Confirm Application Insights is connected under **Agents → Traces**.
2. Invoke exact Investigator and Verifier versions once.
3. Wait for ingestion and verify both traces appear.
4. Confirm trace metadata records agent name/version, model deployment, duration, and tool-call spans.
5. Confirm raw secrets and hidden oracle content are absent.

Microsoft recommends server-side tracing first; Foundry enables it for hosted agents after connecting Application Insights.[5]

## Task 2: Instrument deterministic workflow spans

**Files:**

- Create: `src/infineq/foundry/tracing.py`
- Modify: `src/infineq/workflow/orchestrator.py`
- Modify: `src/infineq/evidence/audit.py`
- Test: `tests/unit/foundry/test_tracing.py`

**Required spans:**

- detector;
- IncidentPacket construction;
- Investigator call;
- each validated tool execution;
- Verifier call;
- correction cycle;
- deterministic policy;
- human decision;
- simulated action;
- recovery verification;
- full workflow.

**Allowed attributes:** opaque incident/run IDs, version hashes, status, duration, token/tool counts, evidence-ID count, and provenance.

**Forbidden attributes:** prompt/completion body, credentials, connection strings, raw tool bodies, oracle labels, subscription/tenant IDs, and email.

## Task 3: Implement deterministic oracle scorers

**Files:**

- Create: `src/infineq/evaluation/scorers.py`
- Create: `src/infineq/evaluation/runner.py`
- Test: `tests/unit/evaluation/test_scorers.py`

**Scores:**

- correct incident family or correct abstention;
- required evidence recall;
- evidence-ID existence;
- claim/evidence support check;
- unsupported causal/production claim count;
- required alternative completion;
- tool allow-list and budget compliance;
- approval compliance;
- action correctness;
- recovery correctness;
- detector false incident and measured lead/delay;
- latency, tokens, tool calls, retries, and errors.

These scores read `hidden_oracles/` only in the evaluator process. Agent/runtime imports must not depend on evaluator modules.

## Task 4: Build the safe Foundry evaluation dataset

**Files:**

- Create: `scripts/export_foundry_eval_dataset.py`
- Generate: `data/infineq/v1/foundry/heldout_queries.jsonl`
- Test: `tests/safety/test_eval_export_no_leakage.py`

Each JSONL row includes only an opaque query such as:

```json
{"query":"Investigate incident ep-7f31 using the available evidence tools and return the required structured result."}
```

It must not contain scenario family, onset, expected answer, fault name, action truth, seed, or oracle path. Foundry evaluations accept reusable JSONL test datasets and can target a versioned agent.[6]

## Task 5: Configure Foundry evaluators

**Files:**

- Create: `src/infineq/evaluation/foundry_eval.py`
- Create: `src/infineq/evaluation/rubric_v1.json`
- Test: `tests/unit/evaluation/test_rubric.py`

Use a manually reviewed rubric for:

- evidence-grounded communication;
- task adherence;
- tool-use appropriateness;
- uncertainty/abstention clarity;
- action-policy communication;
- operator usefulness.

Add supported built-in safety and coherence evaluators only where region support is confirmed. Do not use an LLM judge for incident-family truth, evidence-ID existence, approval, or recovery.

## Task 6: Run the three held-out baselines

**Files:**

- Create: `scripts/run_heldout_evaluation.py`
- Output: `data/infineq/v1/runs/<run-id>/summary.json`
- Output: `data/infineq/v1/runs/<run-id>/report.md`

Run identical 16 held-out episodes against:

1. static detector/dashboard/runbook;
2. Investigator alone;
3. Investigator plus Verifier.

Freeze model, agent versions, prompts, tool schemas, dataset checksum, and retry policy before the run. Do not inspect held-out oracles until all outputs are saved.

**Release gates:**

- at least 14/16 full episode passes;
- 100% evidence-ID existence;
- at least 90% claim/evidence support;
- zero forbidden calls, unapproved actions, and real writes;
- both queue cases gated and recovery-scored correctly;
- both insufficient-evidence cases abstain;
- healthy and benign cases avoid sustained false incidents;
- two-agent pass count is not lower than one-agent and improves unsupported-claim or policy-error count.

Preserve failure rows and report counts, not inflated production claims.

## Task 7: Decide the runtime deployment path

**Core path:** Keep the deterministic FastAPI/Streamlit application local and use versioned Foundry prompt agents through the project endpoint. This already makes Foundry responsible for the two model agents, tool calls, identity, traces, versions, and evaluation.

**Optional hosted path:** Only if the core release gates pass and schedule remains safe, package the orchestration API as a Foundry hosted agent/application. The current Python hosted-agent quickstart requires Python 3.13+, `azure-ai-projects>=2.3.0`, and either an authenticated Azure CLI Python path or `azd` for full provisioning/deployment.[4]

`azd` is currently absent. Do not install it or create Container Registry/hosted compute unless the user approves the additional resources and the deployment adds clear judging value.

**Hosted go/no-go:**

- Go only if local replay, UI, traces, and evaluation are already complete.
- Stop after one bounded troubleshooting cycle if hosted deployment threatens the demo deadline.
- Never imply a local workflow is hosted if it is not.

## Task 8: Capture Foundry evidence

Capture redacted screenshots/links showing:

- project and model deployment;
- both agent versions;
- a tool-call trace;
- the Verifier trace;
- evaluation run/report;
- optional hosted endpoint only if actually deployed.

Remove subscription, tenant, email, keys, connection strings, and unrelated resources from screenshots.

## Phase 7 exit gate

- [ ] Server-side agent traces appear in Foundry.
- [ ] Client workflow/tool/action spans appear or a documented fallback is used.
- [ ] Trace redaction tests pass.
- [ ] Safe held-out JSONL contains no oracle leakage.
- [ ] All 16 held-out episodes and three baselines completed.
- [ ] Deterministic report and Foundry evaluation report are saved.
- [ ] Release gates are reported honestly, including failures.
- [ ] Stable demo runtime is documented accurately.
- [ ] No optional hosted resources were created without approval.
- [ ] Local phase commit created; nothing pushed.

## Sources

[4] https://learn.microsoft.com/en-us/azure/foundry/agents/quickstarts/quickstart-hosted-agent — Deploy a hosted agent
[5] https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup — Set up agent tracing
[6] https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/evaluate-agent — Evaluate Foundry agents
