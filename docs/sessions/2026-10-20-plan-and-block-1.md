# Session 2026-10-20 — plan-and-block-1
Lead: Claude Opus 5.5 · Team: none (one subagent per task; side sessions karo-block-2 and the submission package run on)
## What changed
- Wrote the plan of record (`Karo — Our Build Plan.md`, approved) and the task plan (`plans/implementation.md`, Tasks 1–14)
- Built and reviewed Tasks 1–9 on `build`, commits 2144e17..8aa5ecf (671 tests); saved the rules PDF and Flutterwave smoke results in `plans/` and `proof/smoke/`
## What's left
- Joseph confirms registration and how to submit. Both deadlines are today, 18:59 Nairobi
- Joseph adds 105.164.123.58 to Flutterwave's IP allowlist; re-run the smoke test; then Task 11 in karo-block-2 (WhatsApp, Task 12, is already under way there)
## Decisions taken
- 001: the plan of record and its scope (web page and WhatsApp; Discord only if time allows; PIN covers payments out and standing orders)
- 002: stack is Python and the official Anthropic SDK with no agent framework; ADK-Rust supplies ideas only
- 003: build by subagent-driven development, reviewed per task; hand off at about 70% context
- 004: WhatsApp through the Twilio sandbox with live credentials; public address karo.linxke.com through a Cloudflare tunnel
## Open questions
- Is the submission a website upload or an email? And why does Flutterwave's /transfers/rates return 400?
