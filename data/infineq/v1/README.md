# Infineq v1 synthetic replay corpus

This directory contains a deterministic, synthetic incident-replay and evaluation corpus.
It is not a training dataset and it is not a measurement of vLLM, a GPU, EKS, Kubernetes,
or any production service.

- Every episode is generated and replayed from fixed simulator parameters and a seed.
- Request artifacts contain no customer prompt content or completion text.
- `observed/` is the only tree mounted by the agent-readable runtime.
- `hidden_oracles/` is evaluator-only answer-key data and is never exposed to agents or tools.
- Timing is a queue-model simulation, not a vLLM/GPU/EKS benchmark.
- Public BurstGPT is excluded from v1; it may be considered later only as a workload-shape input.
- Synthetic/replay data must never be pooled with future live data.
- The frozen corpus contains 8 development episodes and 16 held-out episodes.

Artifact hashes are content hashes. Filesystem paths are not part of evidence identity hashes.
