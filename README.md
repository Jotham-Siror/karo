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

## Run the demo

Fill in `.env` (see `.env.example`), then:

```bash
uv run uvicorn karo.web.app:app --port 8000
```

Open http://localhost:8000 and log in with `KARO_DEMO_PASSWORD`. The first start creates `data/karo.db` and seeds the demo families with `DEMO_PIN`. `/log` shows the audit log, and `/demo` has the demo clock and a reset button.

## Tests

```bash
uv run pytest
```

## Flutterwave smoke test

Checks what works in Flutterwave test mode. Results are saved to `proof/smoke/`.

```bash
uv run python scripts/smoke_flutterwave.py
```
