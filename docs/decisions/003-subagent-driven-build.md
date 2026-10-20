# 003 — Subagent-driven build with per-task review; hand off at ~70% context
Date: 2026-10-19 · Supersedes: none

Joseph asked for subagent-driven development wherever it improves speed, output or token use.

**How each task runs:**
- One implementer at a time, on sonnet, working from a brief in `plans/implementation.md`.
- A separate reviewer follows. Opus reviews the money-critical tasks.
- Fix rounds go back to the same implementer, then get a scoped re-review.
- The ledger in `.superpowers/sdd/implementation/progress.md` records every ruling.
- At about 70% context, the session finishes its task, commits, and hands off to a fresh session through `plans/handoff.md`.

**Why:** the per-task reviews caught real defects:
- a double-payment race
- an invoice that could be paid twice
- PIN tokens in the access log
- a lost-payment crash window

The ledger survives context loss.
