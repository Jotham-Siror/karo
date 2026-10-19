# Karo

Karo is an AI money agent a family talks to in chat. It pays a student's fees abroad, collects each relative's share in whatever way they pay at home, picks the cheapest way to send, and sends the student an allowance. Money only moves after the main payer confirms with her PIN.

The AI suggests; ordinary code decides. Every money rule lives in `src/karo/rules.py`, outside the AI.

Entry for the 2026 Shenzhen FinTechathon, International Track, Topic C.

## Setup

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then fill in test-mode keys
```

## Tests

```bash
uv run pytest
```

## Flutterwave smoke test

Checks what works in Flutterwave test mode. Results are saved to `proof/smoke/`.

```bash
uv run python scripts/smoke_flutterwave.py
```
