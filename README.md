# Infineq

Infineq is an evidence-first LLM-serving reliability copilot. It is being built as a narrow, synthetic-replay prototype: deterministic code detects and measures degradation, Microsoft Foundry agents investigate and verify evidence, and any state-changing simulation requires explicit human approval.

## Current status

- Design: frozen as v1.0
- Implementation: Phase 1 foundation
- Data: synthetic only; no production telemetry or customer prompts
- Infrastructure actions: simulator-only in the planned MVP

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
