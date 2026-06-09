---
name: apple-app-automation
description: "Class-level macOS Apple app automation: Notes, Reminders, Messages, Find My, and GUI fallbacks."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [Apple, macOS, Notes, Reminders, Messages, FindMy, Automation]
---

# Apple App Automation

Use this umbrella for user-facing Apple ecosystem tasks on macOS: Apple Notes, Reminders, Messages/iMessage/SMS, Find My device/item lookup, and GUI fallback workflows.

## Safety and consent

- Sending messages, creating reminders, or modifying personal notes are user-visible side effects: confirm target/content when ambiguous.
- Never type passwords, 2FA codes, or secrets into Apple apps.
- Stop and ask if macOS permission prompts, payment prompts, or account sign-in dialogs appear.

## Notes.app via `memo`

Use when the user asks to create, search, organize, or export Apple Notes that sync through iCloud. Install with `brew tap antoniorodr/memo && brew install antoniorodr/memo/memo`. Verify by listing notes/folders before destructive edits.

## Reminders.app via `remindctl`

Use when the user wants tasks in Apple Reminders/iPhone/iPad. Install with `brew install steipete/tap/remindctl`; run `remindctl status` and `remindctl authorize` if needed. Distinguish human Reminders from agent cron alerts.

## Messages.app via `imsg`

Use for iMessage/SMS reads and sends on macOS. Install with `brew install steipete/tap/imsg`. For any send, resolve the recipient and message body before sending; avoid bulk/mass messaging unless explicitly authorized.

## Find My via GUI automation

Find My has no stable CLI. Open Find My, capture screenshots, and use vision/AX evidence to report device or AirTag status. Prefer non-invasive reads; do not change sharing/device settings.

## GUI fallback

For Apple apps without a CLI path or when a CLI fails, use `computer_use` in background mode: capture first, click by element index, verify after each state change, and avoid permission dialogs unless the user explicitly asked you to handle them.

## Absorbed package notes

This umbrella absorbed `apple-notes`, `apple-reminders`, `imessage`, and `findmy`. `macos-computer-use` remains standalone because it is a cross-application desktop-control primitive, not only an Apple app workflow.
