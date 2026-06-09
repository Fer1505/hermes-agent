---
name: github-workflows
description: "Class-level GitHub work: auth, repos, issues, PRs, reviews, CI, and repo inspection."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Git, Pull-Requests, Issues, Code-Review, CI, Repository, gh-cli]
---

# GitHub Workflows

Use this umbrella whenever a task involves GitHub or a Git repository connected to GitHub: authentication, repo setup, issues, PR lifecycle, code review, releases, CI checks, or repository inspection. Prefer `gh` when authenticated; use `git` plus GitHub REST API as the fallback.

## 0. Discovery and authentication

1. Confirm repository context: `git remote -v`, `git status`, and current branch.
2. Check tools and auth:
   ```bash
   git --version
   gh --version 2>/dev/null || echo "gh not installed"
   gh auth status 2>/dev/null || echo "gh not authenticated"
   ```
3. If `gh` is authenticated, use it for API work. If not, look for a provided token in environment/config and use the GitHub REST API with an Authorization header; never echo the token.
4. Never print tokens. Redact any credential material in summaries or logs.

## Repository setup and management

Use for cloning, creating/forking repos, managing remotes, secrets, releases, and branch protection.

- Extract owner/repo from remote before API calls.
- Prefer `gh repo view`, `gh repo clone`, `gh release`, and `gh secret` where available.
- Verify remote mutations by reading back state (`gh repo view`, `git remote -v`, release list, etc.).

## Issues and project triage

Use for creating, searching, labeling, assigning, and closing issues.

- Search before creating duplicates.
- Include reproduction steps, expected/actual behavior, environment, and links to evidence.
- For feature requests, capture user value, non-goals, acceptance criteria, and rollout notes.
- After creation or mutation, return the issue URL/number.

## Pull request lifecycle

Use for branch creation, commits, PR body drafting, CI monitoring, review response, and merge.

1. Start from clean status or explain existing dirty state.
2. Use conventional commits where the repo already uses them.
3. Draft a PR body with summary, tests, risks, and screenshots/logs if relevant.
4. Monitor checks (`gh pr checks --watch` when appropriate) and inspect failing logs before retrying.
5. Do not merge unless the user explicitly asks or project policy permits it.

## Code review and pre-commit verification

Two modes belong here:

- **Pre-commit verification of your own changes:** inspect `git diff`, run tests/lints/security checks, and use an independent reviewer when the change is non-trivial.
- **Reviewing someone else's PR:** fetch PR diff, inspect risky files, leave actionable comments, and distinguish blockers from suggestions.

Always ground findings in file paths/lines and run whatever verification is reasonably available.

## Repository inspection and metrics

Use `pygount` or language-native tools when the user asks about repo size/composition. Always exclude dependencies/build output (`.git`, `node_modules`, `venv`, `.venv`, `dist`, `build`, `.next`, caches) so metrics reflect source code rather than vendored artifacts.

## Absorbed package notes

This umbrella absorbed the previous narrow GitHub skills: `github-auth`, `github-repo-management`, `github-issues`, `github-pr-workflow`, `github-code-review`, `codebase-inspection`, and `requesting-code-review`. Their complete packages were archived unchanged for recovery; their class-level workflows now live here.
