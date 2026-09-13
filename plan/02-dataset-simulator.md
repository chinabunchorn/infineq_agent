# Phase 2 — Dataset and Deterministic Simulator Plan

> **For Hermes:** Use TDD and the frozen Design v1.0 equations. Generate observed data and hidden oracles separately. Do not add agents or Foundry calls in this phase.

**Goal:** Build a deterministic discrete-event simulator and generate the frozen 24-episode Infineq evaluation corpus.

**Architecture:** A seed-controlled simulator emits per-request events and aggregated service/infrastructure/change evidence. A separate oracle writer records the injected mechanism and expected behavior. The agent-readable tree never contains fault names or oracle fields.

**Tech stack:** Python standard library priority queue, Pydantic schemas, JSONL, SHA-256 manifests, pytest property/invariant tests.

---

## Frozen nominal scenario

- 180 seconds total
- baseline 0–60 seconds at 1.2 requests/second
- degradation 60–120 seconds at 2.6 requests/second
- recovery 120–180 seconds
- eight active sequence slots per simulated replica
- 256 input and 64 output tokens with seed-controlled ±5% jitter
- `prefill_s = 0.10 + input_tokens × 0.00078`
- `decode_s = max(output_tokens - 1, 0) × ITL`
- base ITL 50 ms
- one replica before approval; two after approved `SIMULATED_SCALE_OUT`
- synthetic p95 TTFT SLO 2,000 ms over 30 seconds

## Task 1: Define corpus and scenario manifests

**Files:**

- Create: `src/infineq/simulator/manifests.py`
- Create: `data/infineq/v1/corpus_manifest.json`
- Create: `data/infineq/v1/knowledge/source_manifest.json`
- Test: `tests/unit/simulator/test_manifests.py`

**Steps:**

1. Write tests requiring schema version, simulator version, split, opaque episode ID, seed, phases, workload, service parameters, and artifact checksums.
2. Define separate `ObservedManifest` and `HiddenOracle` models.
3. Prohibit causal fields from the observed manifest.
4. Pin timestamp format, units, ordering, and float serialization.
5. Validate that an episode cannot be both development and held out.

## Task 2: Implement the queue engine

**Files:**

- Create: `src/infineq/simulator/engine.py`
- Test: `tests/unit/simulator/test_engine.py`
- Test: `tests/unit/simulator/test_invariants.py`

**TDD cases:**

1. Empty workload produces no events.
2. Below-capacity arrivals produce zero sustained queue wait.
3. Above-capacity arrivals grow a queue.
4. Adding a replica after approval increases slots only at the approved timestamp.
5. FCFS ordering is stable.
6. TTFT equals queue wait plus prefill.
7. ITL remains independent of queue wait in the canonical scenario.
8. Identical seed/config produces byte-identical normalized output.
9. Different seeds change jitter but not the scenario family or safety policy.
10. No request starts before submission or completes before first token.

## Task 3: Implement evidence emitters

**Files:**

- Create: `src/infineq/simulator/emitters.py`
- Create: `src/infineq/evidence/ids.py`
- Test: `tests/unit/simulator/test_emitters.py`
- Test: `tests/unit/evidence/test_ids.py`

**Outputs:**

- `request_events.jsonl`
- `service_metrics.jsonl`
- `infrastructure_events.jsonl`
- `change_events.jsonl`
- `evidence_index.jsonl`

**Rules:**

- Sort deterministically.
- Include value, unit, source, aggregation, window, freshness, and `data_origin=synthetic`.
- Generate IDs as `ev:<episode>:<source>:<window>:<signal>:<aggregation>:<hash8>`.
- Hash normalized content, not filesystem paths.
- Represent absent changes as an explicit empty query result, not a fabricated event.
- Never include a fault label in agent-visible output.

## Task 4: Implement the eight scenario templates

**Files:**

- Create: `src/infineq/simulator/scenarios.py`
- Test: `tests/unit/simulator/test_scenarios.py`

**Templates:**

1. healthy steady state;
2. benign burst below sustained capacity;
3. queue saturation;
4. backend/decode slowdown;
5. backend errors/timeouts;
6. prompt-length shift;
7. replica restart/readiness loss;
8. missing or contradictory telemetry.

For each template, define three variants. Variant A is development-visible; B and C change seed, onset, intensity, and noise and are held out.

**Scenario-specific assertions:**

- Healthy/benign: no sustained detector condition.
- Queue: queue wait and TTFT rise while ITL, token distribution, revision, and readiness stay stable.
- Backend slowdown: ITL rises without an initiating workload or rollout change.
- Error: aggregate error and normalized backend event corroborate each other.
- Workload shift: input-token distribution and prefill rise.
- Restart: readiness loss matches an infrastructure event.
- Missing/contradictory: required evidence is absent/stale/inconsistent and must force abstention.

## Task 5: Write hidden oracles through a separate code path

**Files:**

- Create: `src/infineq/simulator/oracle_writer.py`
- Test: `tests/safety/test_oracle_isolation.py`

**Oracle fields:** injected family, onset/end, seed, mechanism, expected detector behavior, expected leading family or abstention, required evidence, forbidden conclusions, allowed action, prohibited actions, and recovery truth.

**Isolation tests:**

- Agent-readable path traversal cannot reach `hidden_oracles/`.
- Observed files contain none of the known fault labels or oracle keys.
- Search across `observed/` for every oracle-only string returns no match.
- Runtime configuration has no oracle-root setting.

## Task 6: Generate the canonical queue episode first

**Files:**

- Create: `scripts/generate_corpus.py`
- Generate: `data/infineq/v1/observed/<opaque-id>/...`
- Generate separately: `data/infineq/v1/hidden_oracles/<opaque-id>.oracle.json`
- Test: `tests/integration/test_canonical_episode.py`

**Required verification:**

- one-replica nominal capacity is approximately 2.32 requests/second;
- two-replica capacity is approximately 4.64;
- detector inputs visibly separate rising queue/TTFT from stable ITL;
- if approval is applied at 120 seconds, final 30-second p95 TTFT meets 2,000 ms and queue drains;
- if approval is rejected, no capacity change occurs and recovery is not falsely reported.

## Task 7: Generate and freeze all 24 episodes

**Steps:**

1. Generate eight development episodes and inspect plots/tables manually.
2. Lock detector-independent generator parameters.
3. Generate 16 held-out episodes without opening their oracle contents during prompt work.
4. Write corpus-level SHA-256 hashes.
5. Regenerate to a temporary directory and compare hashes.
6. Fail if any observed artifact differs for the same manifest and seed.

**Command:**

```bash
unset PYTHONPATH
uv run python scripts/generate_corpus.py --version v1 --verify-reproducible
```

## Task 8: Document data limitations

**Files:**

- Create: `data/infineq/v1/README.md`

State prominently:

- data is simulated and replayed;
- timing is not a vLLM/GPU/EKS measurement;
- no customer prompt content exists;
- hidden oracle is for evaluation only;
- public BurstGPT is excluded from v1 and may be a later workload-shape input;
- synthetic and future live data must never be pooled.

## Phase 2 exit gate

- [x] 24 episodes exist with 8/16 split.
- [x] Same seed/config regenerates identical checksums.
- [x] Oracle isolation tests pass.
- [x] Canonical queue and rejection paths satisfy invariants.
- [x] All eight scenario templates have independent evidence patterns.
- [x] Dataset README clearly labels simulation limits.
- [x] `unset PYTHONPATH && uv run pytest tests/unit/simulator tests/unit/evidence tests/integration/test_canonical_episode.py tests/safety/test_oracle_isolation.py -q` passes.
- [x] Local phase commit created; nothing pushed.

## Verified gate evidence

- Corpus command: `unset PYTHONPATH && uv run python scripts/generate_corpus.py --version v1 --verify-reproducible` returned `episode_count=24`, `development_count=8`, `held_out_count=16`, and `reproducible=true`.
- The persisted observed-tree SHA-256 is `f5790609ca5953bc7f0792d65d10c73faf9abf6cbc7cdda2e90594567311763f`; an independent audit matched it and found 24 observed directories, 24 hidden-oracle files, and no forbidden oracle/fault-label strings under `observed/`.
- Exact Phase 2 gate: 46 passed.
- Full quality gates: Ruff check passed; Ruff format check passed (64 files); mypy passed (25 source files); full pytest passed (119 tests) with 93.46% coverage; `uv lock --check` exited successfully.
- Canonical verification measured one-replica capacity `2.319055680526889` requests/s and two-replica capacity `4.638111361053778`; approved scale-out produced `criteria_met=true`, p95 TTFT `308.455 ms`, queue checks `(0, 0, 0)`, and 2 ready replicas; rejected scale-out produced `criteria_met=false` and 1 ready replica.
- Independent reviewer subagent returned `passed=true` with empty blocking security and logic findings. The final local commit is the only delivery; nothing was pushed.
