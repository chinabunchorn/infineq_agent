# Infineq Implementation Roadmap

> **For Hermes:** Implement one phase at a time. Load `test-driven-development` before code work and `requesting-code-review` before each phase gate. Do not begin a later phase until the preceding exit gate is verified.

**Goal:** Build the frozen Infineq v1 prototype: deterministic incident detection, evidence-linked investigation, independent verification, human-gated simulated remediation, measured recovery, and Microsoft Foundry tracing/evaluation.

**Architecture:** Python 3.13 monorepo with a deterministic simulator and workflow around two Microsoft Foundry agents. Foundry is the model/agent, identity, trace, and evaluation control plane; deterministic Python owns incident generation, thresholds, policy, approval, action execution, and oracle scoring.

**Tech stack:** Python 3.13, `uv`, Pydantic v2, pytest, Microsoft Foundry SDK (`azure-ai-projects>=2.4.0`), `azure-identity`, OpenTelemetry/Application Insights, FastAPI for the local application boundary, and Streamlit for the one-screen demo UI.

---

## Resumption status

The agentathon has ended; do not treat the historic submission schedule as a current gate.
Phases 0–4 have recorded completion evidence. Phase 5's **development exit gate passed**
on the complete v11 run (eight episodes, 24 records, two-agent 4/8 versus 3/8 per
baseline, canonical queue action card verified, zero actions executed). Saved v8/v9
failed the corrected gate; v10 stopped after three episodes without an aggregate score.
See [the Phase 5 evidence and limitations](05-verifier-workflow.md). Phase 6 has not
started; it remains a separate approval/recovery UI phase. Phase 8
now means reproducible **post-event hardening and documentation**, not a new submission.

---

## Authority and scope

The frozen design is:

`/Users/nuttananapirakcheewan/chinabunchorn/20 Events/Hackathons/Microsoft agent-a-thon idea.md`

Design Freeze v1.0 is authoritative. If a plan conflicts with the freeze, stop and repair the plan rather than silently changing the design.

Frozen boundaries:

- one canonical queue-saturation demonstration;
- 24 deterministic episodes: eight development and 16 held out;
- two agents: Reliability Investigator and Evidence & Safety Verifier;
- one executable action: human-approved `SIMULATED_SCALE_OUT`;
- no live Kubernetes, EKS, GPU, or production writes;
- no arbitrary shell, URL fetcher, PromQL, or prompt-content access;
- no production reliability or MTTR claims.

## Repository state observed before planning

- Repository: `/Users/nuttananapirakcheewan/infineq_agent`
- Git: initialized, no commits yet, branch `main`
- Remote: `https://github.com/chinabunchorn/infineq_agent.git`
- Remote branch currently absent
- Local commits are permitted; **do not push or create a PR without the user's explicit approval**
- No application files existed when these plans were written
- Apple system `python3` is 3.9.6, but `/opt/homebrew/bin/python3.13` is available
- `uv` is installed
- Azure CLI 2.90.0 is installed and authenticated
- `Microsoft.CognitiveServices` is registered
- The signed-in user has Owner and Foundry User access at sufficient scope
- `azd` is not installed; it is not required for the prompt-agent-first path

## Foundry resource decision

Use a **Basic Agent Service setup**. Infineq stores its synthetic episode data locally and does not require bring-your-own Cosmos DB, AI Search, Storage, or private networking for the hackathon. Standard setup would add resources, permissions, cost, and failure modes without serving the frozen MVP.[2]

The implementation sequence follows the current Foundry resource and SDK quickstarts,[1][3] uses strict application-executed function tools,[7] connects tracing and evaluation early,[5][6] applies Foundry RBAC guidance,[8] and treats hosted-agent deployment as an optional late-stage step rather than a prerequisite.[4]

Recommended resource manifest:

| Item | Planned value |
|---|---|
| Resource group | `rg-infineq-agentathon` |
| Foundry resource/account | `infineq-agentathon-chin` if the name remains available |
| Foundry project | `infineq-agentathon` |
| Region | `japaneast` |
| Model deployment | `infineq-gpt-5-4-mini` |
| Model | `gpt-5.4-mini`, version `2026-03-17` |
| SKU | `GlobalStandard` |
| Initial deployment capacity | `10` |
| Application Insights | `appi-infineq-agentathon` or portal-generated equivalent |
| Authentication | Microsoft Entra ID through `DefaultAzureCredential`; no API key in code |

The subscription's enforced location policy excludes Southeast Asia and allows Japan East. The existing `rg-infineq-agentathon` resource group is empty; although its metadata location is Southeast Asia, Azure permits resources in a resource group to use a different region.[9] Use that dedicated group and set the Foundry resource itself to Japan East. The live Azure catalog and quota checks showed `gpt-5.4-mini` version `2026-03-17` with `GlobalStandard` and unused quota in Japan East, and Agent Service documents Japan East as supported.[10] Recheck immediately before creation because availability can change.

## Phase order

| Phase | Plan | Deliverable | Hard gate |
|---:|---|---|---|
| 0 | [Foundry bootstrap](00-foundry-bootstrap.md) | Resource, project, model, endpoint, tracing connection | Model smoke test and trace connection verified |
| 1 | [Project foundation](01-project-foundation.md) | Python package, config, schemas, tests | Clean install and schema tests pass |
| 2 | [Dataset and simulator](02-dataset-simulator.md) | Deterministic 24-episode corpus with isolated oracle | Regeneration is byte-stable and nominal scenario recovers |
| 3 | [Detector and evidence tools](03-detector-evidence-tools.md) | IncidentPacket and seven allow-listed read tools | Detector/data-quality/tool-security tests pass |
| 4 | [Investigator agent](04-investigator-agent.md) | Foundry Investigator with structured evidence output | Eight development episodes run without forbidden access |
| 5 | [Verifier and workflow](05-verifier-workflow.md) | Independent Verifier and finite state machine | Correction, abstention, timeout, and policy paths pass |
| 6 | [Approval, recovery, UI](06-approval-recovery-ui.md) | Human-gated simulator action and one-screen UI | No action without matching approval hash; recovery is measured |
| 7 | [Foundry observability and evaluation](07-foundry-observability-evaluation.md) | Traces, Foundry eval, deterministic held-out report | Release gates measured; no oracle leakage |
| 8 | [Hardening and submission](08-hardening-submission.md) | Reproducible demo, screenshots, video, submission package | Final rehearsal and claim audit pass |

## Global development rules

1. Use TDD for deterministic components: write a failing test, observe failure, implement minimally, rerun.
2. Run project commands with `unset PYTHONPATH` to avoid Hermes environment contamination.
3. Pin dependencies and record model/agent/schema versions in every run manifest.
4. Never place endpoints, subscription IDs, tenant IDs, credentials, or connection strings in committed files.
5. The project endpoint is not a secret, but keep environment-specific values in ignored `.env` and document names in `.env.example`.
6. The agent runtime may mount only `data/infineq/v1/observed` and `knowledge`; only the evaluator may read `hidden_oracles`.
7. No infrastructure write tool is exposed to either agent.
8. Make a local commit only after the phase tests pass. Never push until the user says so.
9. Preserve failed results; do not edit evaluation outputs to improve the story.
10. If a frozen design condition proves impossible, stop and propose Design v1.1.

## Planned project layout

```text
infineq_agent/
  plan/
  src/infineq/
    config.py
    schemas/
    simulator/
    detection/
    evidence/
    agents/
    workflow/
    actions/
    evaluation/
    foundry/
    api/
    ui/
  data/infineq/v1/
    corpus_manifest.json
    knowledge/
    observed/
    hidden_oracles/
    runs/
  tests/
    unit/
    integration/
    safety/
  scripts/
  pyproject.toml
  uv.lock
  .env.example
  .gitignore
  README.md
```

## Definition of done

Infineq v1 is complete only when:

- at least 14 of 16 held-out episodes pass the full frozen episode rule;
- both held-out queue cases produce only the gated simulator action and verify recovery correctly;
- both missing/contradictory cases abstain;
- healthy and benign cases avoid sustained false incidents;
- all cited evidence IDs exist and at least 90% of evidence-to-claim links support their claims;
- there are zero forbidden tool calls, zero unapproved actions, and zero real infrastructure writes;
- the two-agent system does not reduce pass count versus the one-agent baseline and improves unsupported-claim or policy-error count;
- Foundry traces and an evaluation report are captured;
- the three-minute video and all submission claims match measured evidence.

## Sources

[1] https://learn.microsoft.com/en-us/azure/foundry/tutorials/quickstart-create-foundry-resources — Set up Microsoft Foundry resources
[2] https://learn.microsoft.com/en-us/azure/foundry/agents/environment-setup — Foundry Agent Service environment setup
[3] https://learn.microsoft.com/en-us/azure/foundry/quickstarts/get-started-code — Microsoft Foundry SDK quickstart
[4] https://learn.microsoft.com/en-us/azure/foundry/agents/quickstarts/quickstart-hosted-agent — Deploy a hosted agent
[5] https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup — Set up agent tracing
[6] https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/evaluate-agent — Evaluate Foundry agents
[7] https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/function-calling — Function calling with Foundry agents
[8] https://learn.microsoft.com/en-us/azure/foundry/concepts/rbac-foundry — Foundry role-based access control
[9] https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/overview — Azure Resource Manager overview and resource-group region behavior
[10] https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions — Foundry Agent Service limits, quotas, and supported regions
