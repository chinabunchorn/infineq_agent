# Infineq

Infineq is an evidence-first LLM-serving reliability copilot. It is being built as a narrow, synthetic-replay prototype: deterministic code detects and measures degradation, Microsoft Foundry agents investigate and verify evidence, and any state-changing simulation requires explicit human approval.

## Current status

- Design: frozen as v1.0; agentathon is complete, but the repository remains a prototype.
- Implementation: Phases 0–4 are recorded as complete. Phase 5's **development live exit
  gate passed** on v11; Phase 6 has not started. See
  [`plan/05-verifier-workflow.md`](plan/05-verifier-workflow.md).
- `verifier-v2` was deployed as Foundry agent version 2 and read back with its exact
  prompt/tool/model/schema definition. The authorized eight-episode development run v9
  still scored 3/8 in every mode and **failed** the two-agent justification gate; the
  canonical queue case did not reach an action card. The TTL correction packet now
  supplies the exact policy duration and the scale-out card requires a ready source.
- An authorized v10 Foundry evaluation stopped after three episodes because its artifact
  validator mishandled response IDs after a correction. No eight-episode v10 score exists;
  the partial files are preserved. The validator is fixed locally, and future artifacts
  retain the revision-stage outputs needed for diagnosis.
- The complete v11 run used the existing Investigator 14 / Verifier 2 pins. It saved
  eight episodes and 24 records; two-agent passed **4/8** versus **3/8** in both
  baselines, and the canonical queue case produced a verified, eligible action card.
  The Verifier's bounded evidence read now includes every cited ID even when the model
  requests a subset. Artifact validation passed, and **zero actions were executed**.
  Two other two-agent cases still ended `invalid_schema`; this is a development gate,
  not proof of production or held-out reliability.
- Historical run v8 also scored 3/8 per mode; its archived green justification flag is
  stale under the corrected gate. Historical evaluation files are preserved unchanged.
- Data: synthetic replay only; no production telemetry or customer prompts.
- Infrastructure actions: no real infrastructure writes; simulator-only action remains for
  the planned approval and recovery phase.

The authoritative roadmap is [`plan/README.md`](plan/README.md).

## Development prerequisites

- Python 3.13
- [`uv`](https://docs.astral.sh/uv/)
- Azure CLI authenticated with `az login` for optional Foundry smoke tests
- A Microsoft Foundry project and model deployment

## Set up

```bash
uv venv --python /opt/homebrew/bin/python3.13
uv sync --all-groups
cp .env.example .env
```

Fill `.env` with the Foundry project endpoint and deployment name. Never place API keys or connection strings in the repository.

## Verify

```bash
unset PYTHONPATH
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

## Safety boundary

Infineq v1 will not receive unrestricted shell, Kubernetes, Azure Resource Manager, or cluster-admin credentials. The only planned executable remediation changes deterministic simulator state from one replica to two after approval; it does not scale a real service.
