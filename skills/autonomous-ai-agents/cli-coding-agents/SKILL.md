---
name: cli-coding-agents
description: "Delegate coding work to Codex, Claude Code, or OpenCode."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Codex, Claude-Code, OpenCode, Delegation, Worktrees]
---

# CLI Coding Agents

Use this umbrella when delegating implementation, refactoring, review, or exploration to an external coding-agent CLI. The specific backend may be Codex, Claude Code, OpenCode, or another compatible tool; the orchestration pattern is the same.

## Backend selection

- Use the CLI the user explicitly requests.
- If no preference is given, choose the installed/configured tool best suited to the repo and task.
- Verify binary and auth before launching: `which -a <tool>`, `<tool> --version`, and backend-specific auth/status commands.

## Operating pattern

1. Prepare a clean worktree/branch and write a concise task brief with acceptance criteria.
2. Run interactive/TUI tools with `pty=true`; run long bounded jobs in background with `notify_on_complete=true`.
3. Poll logs, but do not assume success from agent claims. Inspect diffs and run tests yourself.
4. Keep user-facing summaries grounded in real git diff/test output.
5. For parallel work, isolate each agent in a separate worktree or checkout.

## Backend notes

- **Codex:** good for OpenAI-backed coding sessions and PR work. Keep prompts scoped and verify generated changes with local commands.
- **Claude Code:** strong at larger refactors and repo reasoning. Still require independent diff/test verification.
- **OpenCode:** provider-agnostic and open-source; check which binary Hermes resolves because shell environments can differ.

## Absorbed package notes

This umbrella absorbed `codex`, `claude-code`, and `opencode` as provider-specific subsections.
