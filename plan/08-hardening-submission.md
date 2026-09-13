# Phase 8 — Hardening and Agent-a-thon Submission Plan

> **For Hermes:** Preserve measured failures and maintain the frozen claims policy. Do not push, publish, submit, or delete cloud resources without the user's explicit approval.

**Goal:** Produce a reproducible, safe, judge-ready Infineq demonstration and complete submission package.

**Architecture:** Freeze a release candidate from the tested local application plus versioned Foundry assets. Rehearse a fixed synthetic replay, capture evidence, audit every claim, and prepare a video under the contest limits.

**Tech stack:** Existing Infineq stack, Git, Foundry portal, screen recording/editing tools.

---

## Task 1: Run the release-candidate quality suite

**Files:**

- Create: `scripts/release_check.py`
- Create: `docs/release-checklist.md`

**Commands:**

```bash
unset PYTHONPATH
uv sync --frozen --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python scripts/generate_corpus.py --version v1 --verify-reproducible
uv run python scripts/release_check.py
```

Release check validates dependency lock, corpus checksums, schema/prompt/tool versions, held-out result completeness, trace links, approval safety, and prohibited-claim scan.

## Task 2: Perform security and privacy review

**Files:**

- Create: `docs/security-review.md`
- Test: `tests/safety/test_repository_secrets.py`

Review:

- git history and working tree for keys, tokens, connection strings, `.env`, subscription/tenant IDs, emails, and raw prompts;
- oracle isolation;
- path traversal and tool allow-list;
- action import guard;
- approval hash/expiry/replay;
- trace redaction;
- screenshots and logs;
- dependency vulnerabilities where tooling is available.

Do not upload proprietary logs, customer data, or confidential runbooks.

## Task 3: Prepare product documentation

**Files:**

- Finalize: `README.md`
- Create: `docs/architecture.md`
- Create: `docs/dataset-card.md`
- Create: `docs/evaluation-report.md`
- Create: `docs/governance.md`
- Create: `docs/demo-runbook.md`
- Create: `docs/limitations.md`

README must contain:

- one-sentence product definition;
- architecture diagram;
- local setup and Foundry prerequisites;
- exact demo command;
- synthetic provenance warning;
- evaluation summary with denominators;
- security boundary;
- known limitations;
- Microsoft Foundry role;
- cleanup instructions that require confirmation.

## Task 4: Freeze the demonstration episode and fallback

**Primary demo:** canonical queue saturation → verified diagnosis → human-approved simulated scale-out → measured recovery.

**Required fallback:** replay the previously saved successful agent/tool outputs through the same UI if a live Foundry invocation is unavailable. Label the fallback `RECORDED FOUNDRY RUN REPLAY`; do not present it as live.

**Files:**

- Create: `data/demo/demo_manifest.json`
- Create: `docs/demo-runbook.md`

Pin episode ID, corpus checksum, model deployment, agent versions, prompt/tool hashes, expected UI checkpoints, and recording resolution.

## Task 5: Rehearse the three-minute story

**Target storyboard:**

- 0:00–0:20 — endpoint is healthy but first-token delay is worsening;
- 0:20–0:40 — two agents plus deterministic safety architecture;
- 0:40–1:40 — evidence queries and competing explanations;
- 1:40–2:15 — Verifier and approval-bound simulator action;
- 2:15–2:45 — measured recovery, trace, and evaluation evidence;
- 2:45–3:00 — narrow value and honest limitation.

Run at least three timed rehearsals. Remove any step that requires waiting for ingestion or model deployment during recording.

## Task 6: Capture submission assets

**Files:**

- Create: `submission/screenshots/`
- Create: `submission/example-interactions.md`
- Create: `submission/refinement-summary.md`
- Create: `submission/lessons-learned.md`
- Create: `submission/written-description.md`

Required screenshots:

1. one-screen incident brief;
2. competing hypotheses/evidence;
3. approval card;
4. recovery verification;
5. Foundry trace;
6. Foundry evaluation report.

Redact account identifiers and unrelated Azure resources.

## Task 7: Record and validate the video

**Constraints:** maximum three minutes and maximum 150 MB.

**Verification:**

- actual duration under 180 seconds;
- actual file size under 150 MB;
- readable at normal playback speed;
- audio intelligible;
- synthetic/replay label visible;
- no credentials or personal identifiers;
- no unsupported claims;
- opening problem and final value understandable without prior context.

Preserve the source recording and final export separately.

## Task 8: Run the final claim audit

Allowed only if demonstrated:

- working Foundry-based two-agent workflow;
- deterministic detector;
- evidence-linked hypotheses;
- explicit abstention;
- human-gated simulator action;
- measured recovery on the frozen synthetic corpus;
- reported held-out counts and traces.

Prohibited without future evidence:

- production incident prevention;
- MTTR, cost, or outage reduction figures;
- EKS/GPU/live-traffic validation;
- real autoscaling behavior;
- universal pre-SLO prediction;
- autonomous root cause or self-healing.

Every quantitative sentence must point to a saved report row or be removed.

## Task 9: Commit, review, then wait for remote approval

1. Run the full release check.
2. Request code review and resolve findings.
3. Create the final local commit.
4. Show `git status`, branch, commit summary, test results, evaluation counts, and artifact locations.
5. **Stop. Do not push or open a PR until the user explicitly authorizes the remote write.**
6. Do not submit to Founderz or delete Azure resources without separate explicit approval.

## Phase 8 exit gate

- [ ] Clean, reproducible release check passes.
- [ ] Security/privacy review has no unresolved high-risk finding.
- [ ] Dataset card and evaluation report disclose synthetic scope.
- [ ] Demo works live and from clearly labelled recorded-run fallback.
- [ ] Video is under three minutes and 150 MB.
- [ ] Written description, screenshots, example interactions, refinement summary, and lessons learned are complete.
- [ ] Claims audit passes.
- [ ] Final local commit exists.
- [ ] Push, submission, and cleanup remain pending explicit approval.
