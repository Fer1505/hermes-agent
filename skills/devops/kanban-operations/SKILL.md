---
name: kanban-operations
description: "Coordinate Kanban tasks and agent worker handoffs."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Kanban, Orchestration, Workers, Task-Management, DevOps]
---

# Kanban Operations

Use this umbrella for Kanban-backed agent work: creating task lanes, assigning work, running worker loops, tracking blockers, and reporting completed evidence.

## Orchestrator responsibilities

- Normalize user goals into tasks with clear owner, status, priority, and completion criteria.
- Keep WIP low; prefer finishing or unblocking existing work before opening more.
- Record evidence links/logs for state changes instead of vague progress text.

## Worker responsibilities

- Pull one task at a time, restate acceptance criteria, execute, verify, and update status.
- Mark blockers with the exact missing dependency or decision needed.
- Do not mark complete until artifacts/tests/logs prove completion.

## Reporting pattern

Summaries should include: changed tasks, completed evidence, blockers, next owner, and risks. Avoid duplicating the entire board unless requested.

## Absorbed package notes

This umbrella absorbed `kanban-orchestrator` and `kanban-worker`.
