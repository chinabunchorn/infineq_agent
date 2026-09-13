# Phase 0 — Microsoft Foundry Bootstrap Plan

> **For Hermes:** This phase provisions only the minimum Foundry foundation. Show the exact create/deploy operations before performing cost-bearing Azure writes. Do not create Infineq agents or deploy application code in this phase.

**Goal:** Create and verify one Basic Microsoft Foundry project, one model deployment, and one Application Insights connection for Infineq.

**Architecture:** Use the current Foundry project model—not the classic portal. Authenticate through Microsoft Entra ID. Keep the hackathon environment deliberately small and defer hosted-agent compute until the local workflow is proven.

**Tech stack:** Foundry portal, Azure CLI 2.90+, `gpt-5.4-mini`, GlobalStandard, Application Insights.

---

## Why Basic setup

Basic setup is sufficient for Infineq because the prototype uses local synthetic/replay files and custom function tools. Standard setup adds customer-managed Storage, Cosmos DB, and AI Search for agent files, threads, and vector stores; those are not required by the frozen MVP.[2]

## Preflight already verified read-only

- [x] Azure CLI installed: 2.90.0, above the 2.80.0 minimum documented for current project commands.[1]
- [x] Azure CLI authenticated to AzureCloud.
- [x] `Microsoft.CognitiveServices` provider registered.
- [x] User has Owner and Foundry User assignments at sufficient scope.
- [x] `rg-infineq-agentathon` exists and is empty; its metadata location is Southeast Asia.
- [x] Subscription policy `Allowed resource deployment regions` excludes Southeast Asia and allows `japaneast`, `indiasouthcentral`, `indonesiacentral`, `eastasia`, and `malaysiawest`.
- [x] Japan East advertises `gpt-5.4-mini` `2026-03-17` with GlobalStandard and is documented as a supported Agent Service region.[10]
- [x] The account's regional usage output shows unused GlobalStandard quota for that model.

Recheck the catalog and quota immediately before deployment; read-only discovery is not a guarantee that provisioning will succeed.

## Resource manifest

| Field | Value |
|---|---|
| Resource group | `rg-infineq-agentathon` |
| Foundry resource/account | `infineq-agentathon-chin` if the name remains available |
| Project | `infineq-agentathon` |
| Region | `Japan East` / `japaneast` |
| Setup | Basic |
| Model | `gpt-5.4-mini` |
| Model version | `2026-03-17` |
| Deployment name | `infineq-gpt-5-4-mini` |
| SKU | `GlobalStandard` |
| Capacity | `10` initially |
| Tracing | Connected Application Insights |
| Authentication | Entra ID; no key-based application auth |

The resource account name must be globally unique. The failed regional deployment should not by itself consume `infineq-agentathon-chin`, but the portal must confirm availability. Do not put subscription or tenant IDs in this plan.

Azure resource groups can contain resources deployed in regions different from the resource group's metadata region.[9] Therefore the empty `rg-infineq-agentathon` group can contain a Japan East Foundry resource. This is cleaner than reusing a practice-lab group that still contains an AI Services account and project.

## Task 1: Review the cost-bearing creation plan

**Objective:** Confirm the subscription, names, region, SKU, and model before any Azure write.

**Steps:**

1. In Azure Portal, verify the active subscription is the intended hackathon subscription.
2. Verify no policy requires tags, approved regions, private networking, or a naming convention.
3. Confirm `Japan East`, Basic setup, GlobalStandard, and capacity 10.
4. Confirm a user-chosen budget/alert threshold for the resource group.
5. Stop if the portal shows unavailable quota or a policy warning; do not switch regions silently.

**Gate:** A reviewed create manifest with no unresolved policy or quota warning.

## Task 2: Create the Foundry resource and project in the portal

**Objective:** Create the current-generation Foundry project with Basic defaults.

**Portal steps:**

1. Open [Microsoft Foundry](https://ai.azure.com).
2. Select the project chooser in the upper-left, then **Create new project**.
3. Enter project name `infineq-agentathon`.
4. Open **Advanced options**.
5. Select the existing empty resource group `rg-infineq-agentathon`.
6. Enter `infineq-agentathon-chin` as the Foundry resource/account name; change it only if the portal reports that the name is unavailable.
7. Select **Japan East** for the Foundry resource.
8. Keep Basic/default networking and managed storage choices.
9. Review the summary and select **Create project**.
10. Wait until the project Overview page loads successfully.

Microsoft's current quickstart uses this project-first flow and states that a project organizes models, agents, and related resources.[1]

**Do not:** create Cosmos DB, AI Search, custom Storage, a VNet, a hosted agent, or extra projects.

**Verification:**

- Resource group contains one Foundry account/resource and its project.
- Project Overview is reachable.
- **Manage → Project details → Connected resources** has no broken required connection.

## Task 3: Deploy the pinned model

**Objective:** Create the one model deployment used by both agents.

**Portal steps:**

1. In the project, select **Discover → Models**.
2. Search for `gpt-5.4-mini`.
3. Select version `2026-03-17`.
4. Select **Deploy**.
5. Set deployment name `infineq-gpt-5-4-mini`.
6. Select `GlobalStandard` and capacity `10`.
7. Confirm the deployment and wait for provisioning state `Succeeded`.

**Fallback rule:** If this exact model/version/SKU fails, capture the portal error and rerun read-only model/quota discovery. Do not choose a different model until its structured-output, tool-calling, region, quota, and cost fit are recorded in a Design v1.0 environment decision.

**Verification commands after creation:**

```bash
az cognitiveservices account deployment show \
  --name '<foundry-account-name>' \
  --resource-group rg-infineq-agentathon \
  --deployment-name infineq-gpt-5-4-mini \
  --query properties.provisioningState -o tsv
```

Expected: `Succeeded`.

## Task 4: Capture the project endpoint safely

**Objective:** Obtain the non-secret endpoint and deployment name without using API keys.

**Portal steps:**

1. Open the project Overview page.
2. Copy the project endpoint in the form:
   `https://<resource>.services.ai.azure.com/api/projects/infineq-agentathon`
3. Record only these later in ignored `.env`:
   - `AZURE_AI_PROJECT_ENDPOINT`
   - `AZURE_AI_MODEL_DEPLOYMENT_NAME=infineq-gpt-5-4-mini`
4. Do not copy an API key into the repository.

The current SDK quickstart uses `DefaultAzureCredential` with `azure-identity`; authenticate locally using `az login`.[3]

## Task 5: Connect Application Insights before agent development

**Objective:** Make trace evidence available from the beginning rather than retrofitting it at submission time.

**Portal steps:**

1. Open the Foundry project.
2. Select **Agents**.
3. Select **Traces** at the top.
4. Select **Connect**.
5. Create or connect Application Insights named `appi-infineq-agentathon` where the portal permits naming.
6. Verify the connection appears in the project.

Microsoft recommends server-side tracing as the starting point; after Application Insights is connected, Foundry-hosted agent traces require no agent-code instrumentation.[5]

**RBAC check:** Ensure the signed-in user can query the connected telemetry. Add only the documented read role if required; do not grant broad roles as a troubleshooting shortcut.[8]

## Task 6: Verify model access in the playground

**Objective:** Prove the project and model work before local SDK setup.

**Steps:**

1. Open **Build → Models** and select `infineq-gpt-5-4-mini`.
2. Send a deterministic smoke prompt: `Return exactly: INFOUNDRY_READY`.
3. Record whether the response succeeded, deployment name, and timestamp; do not treat exact-text deviation as infrastructure failure if a valid response arrived.
4. Open **Agents → Traces** after a traced agent exists in Phase 4; model-playground traffic alone is not the final trace gate.

## Task 7: Set cost and cleanup controls

**Objective:** Prevent a forgotten hackathon resource from running indefinitely.

**Steps:**

1. Create a resource-group budget with a user-approved amount and email alerts.
2. Record the resource inventory and owner.
3. Add a post-submission cleanup date to the project checklist, not as an automatic deletion.
4. Never run `az group delete` or `azd down` without explicit confirmation of the exact resource list.

## Phase 0 exit gate

All must be true:

- [ ] Foundry project `infineq-agentathon` exists in Basic setup.
- [ ] Model deployment `infineq-gpt-5-4-mini` is `Succeeded`.
- [ ] Project endpoint is captured locally without an API key.
- [ ] A model playground request succeeds.
- [ ] Application Insights is connected.
- [ ] No Standard-setup resources or hosted-agent compute were created accidentally.
- [ ] Cost alert and cleanup ownership are recorded.

Only then begin Phase 1.

## Sources

[1] https://learn.microsoft.com/en-us/azure/foundry/tutorials/quickstart-create-foundry-resources — Set up Microsoft Foundry resources
[2] https://learn.microsoft.com/en-us/azure/foundry/agents/environment-setup — Foundry Agent Service environment setup
[3] https://learn.microsoft.com/en-us/azure/foundry/quickstarts/get-started-code — Microsoft Foundry SDK quickstart
[5] https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup — Set up agent tracing
[8] https://learn.microsoft.com/en-us/azure/foundry/concepts/rbac-foundry — Foundry role-based access control
[9] https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/overview — Azure Resource Manager overview and resource-group region behavior
[10] https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions — Foundry Agent Service limits, quotas, and supported regions
