# 002 — Stack: Python and the official Anthropic SDK, no agent framework
Date: 2026-10-19 · Supersedes: none

**Choice:**
- Karo is one Python service: FastAPI and SQLite, plus a Deep Chat web page.
- The agent uses the official Anthropic SDK with our own short loop, on model claude-opus-5-5.
- Money goes through Flutterwave v3 test mode behind a provider interface, with an offline stand-in provider.

**Why:** the agent never pauses for approval. It proposes, and a PIN page runs plain code, so the approval features of agent frameworks aren't needed.

**Rejected:**
- **ADK-Rust**, after it was compiled and its code reviewed. It lacks WhatsApp, Flutterwave and the PIN page, and 3.0 brings breaking changes. Three ideas were kept from it: tying the approval to a digest of the proposal, a hash-chained log stored in the database, and guards on the model's settings.
- **TypeScript with Vercel Chat SDK.** Its saving landed mostly on the optional Discord door.
