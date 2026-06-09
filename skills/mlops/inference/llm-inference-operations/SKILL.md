---
name: llm-inference-operations
description: "Run and serve open-weight LLMs locally or in production with llama.cpp/GGUF and vLLM."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [LLM, Inference, llama.cpp, GGUF, vLLM, Serving, Quantization]
---

# LLM Inference Operations

Use this umbrella when the user wants to run, serve, benchmark, or troubleshoot open-weight LLM inference.

## Choose the serving path

- **llama.cpp / GGUF:** best for local CPU, Apple Silicon, edge deployment, and simple local servers. Start from Hugging Face `?local-app=llama.cpp` snippets and verify actual `.gguf` files via the repo tree API.
- **vLLM:** best for high-throughput GPU serving, OpenAI-compatible APIs, continuous batching, tensor parallelism, and production-ish deployments.

## Common workflow

1. Identify model, hardware, memory/VRAM budget, latency/throughput target, and API shape.
2. Choose quantization and backend based on those constraints.
3. Launch with explicit model path/repo and port.
4. Verify readiness with a health endpoint or a tiny completion request.
5. Troubleshoot by checking logs, CUDA/Metal/ROCm availability, quant compatibility, context length, and batch settings.

## Notes by backend

- llama.cpp commands often come from model-specific Hugging Face snippets; prefer those over generic quant tables when visible.
- vLLM requires careful version compatibility across Python, CUDA, torch, transformers, and model architecture.
- Keep servers tracked with background process management; do not use shell `&`/`nohup` wrappers when Hermes can track the process.

## Absorbed package notes

This umbrella absorbed `llama-cpp` and `serving-llms-vllm`. `obliteratus` remains standalone because model weight surgery/refusal ablation is a different class than serving/inference operations.
