---
name: ml-evaluation-operations
description: "Evaluate ML models and track benchmark experiments."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [MLOps, Evaluation, Experiment-Tracking, WandB, lm-eval, Benchmarks]
---

# ML Evaluation Operations

Use this umbrella for measuring model quality or experiment progress: Weights & Biases tracking, hyperparameter sweeps, artifact/model registry, and standardized LLM benchmarks with lm-evaluation-harness.

## Experiment tracking with W&B

Use when training or experiments need metrics, comparisons, dashboards, sweeps, artifacts, or team collaboration. Log configs, seeds, code version, data/model artifacts, and final metrics. Verify runs appear in the intended project/entity before reporting success.

## LLM benchmarking with lm-evaluation-harness

Use for standardized tasks such as MMLU, GSM8K, HellaSwag, HumanEval, TruthfulQA, and custom benchmark definitions. Record model args, task list, batch size, hardware, git SHAs, and exact command. For API models, capture rate limits/retries and provider settings.

## Evaluation hygiene

- Preserve reproducibility: dataset versions, seeds, prompts, model revision, and environment.
- Separate smoke tests from final benchmark runs.
- Store result JSON/CSV paths and summarize statistical uncertainty when relevant.
- Do not compare runs unless task sets and prompt formats match.

## Absorbed package notes

This umbrella absorbed `weights-and-biases` and `evaluating-llms-harness`.
