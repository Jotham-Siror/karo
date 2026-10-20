# Karo

Karo is an AI money assistant that a family talks to on WhatsApp, or on a web page. It sends money across borders: to people abroad, to a university for a student's fees, and to the student every month. For fees it collects each relative's share in the way they pay at home, compares ways to send, and pays the university. Money moves only after the main payer confirms with her PIN on a separate page.

The AI suggests and plain code decides. Every money rule lives in `src/karo/rules.py`, outside the AI.

Entry for the 2026 Shenzhen FinTechathon, International Track, Topic C (cross-border and student finance).
Team Fedha: Maela Mouto, Jotham Siror and May Jelimo, Strathmore University, Nairobi.

- **Repository:** https://github.com/Jotham-Siror/karo
- **Live demo:** https://karo.linxke.com. It runs from a team laptop through a Cloudflare tunnel, so it may be offline. Running it locally takes about five minutes (see below).
- **Demo video, PDFs and promo video:** https://github.com/Jotham-Siror/karo/releases/tag/submission-v2
- **Technical documentation:** [docs/technical-documentation.md](docs/technical-documentation.md)
- **Security self-assessment:** [docs/security-self-assessment.md](docs/security-self-assessment.md)

## After submission

The `submission-v2` tag is the state that was submitted. A review of the whole branch the same day found no critical issue. Commits made after the tag, all on 20 October between 17:03 and 17:41 Nairobi time, before the 23:59 Beijing deadline, carry the fixes for what it did find, listed in section 5.3 of the [security self-assessment](docs/security-self-assessment.md):

- A pending payment link is checked with the provider every 10 minutes, so a lost notice still credits the share.
- A confirmed request that never reached a payout is run again after 2 minutes.
- A share is reminded at most once a day, including when the AI asks.
- The monthly allowance's check and proposal happen in one transaction.
- An invoice is filed under the trusted payee's stored name, and the chat links only Karo's own pages.
- A bare number in chat is refused as a PIN only while a confirm link is live, and "STOP!" freezes.
- Forged provider notices are collapsed to one audit row a minute.
- A Flutterwave payout with a lost first answer is looked up by reference, and two Payaza (off) mappings are fixed.

## How a money move works

1. **Propose.** Claude reads the chat or the invoice and calls a *propose* tool. It has no tool that pays, approves, unfreezes, changes limits or edits the trusted payee list.
2. **Check.** Plain code runs all 15 rules and logs each result.
3. **Confirm.** The main payer gets a one-time link and enters her PIN on a separate page. The PIN approves a fingerprint of that exact proposal. The PIN never goes through the chat, and the AI never sees it or the link.
4. **Execute.** Code runs every rule again, checks that the fingerprint still matches, then calls the payment provider. A retried payment can't pay twice.
5. **Verify.** A payment notice must be signed, and Karo asks the provider directly before it credits anything.

## What is real and what is simulated

| Part | Status |
| --- | --- |
| Chat on the web page | Real |
| Chat on WhatsApp (Twilio sandbox) | Real: Twilio's sandbox number; every incoming message's signature is checked. The demo video's main part is a recording of a real phone on WhatsApp |
| AI agent | Real: Claude, `claude-opus-5-5` |
| Relatives paying their share, in the app | Simulated on the stand-in provider. Its checkout page says "simulated, no real money" |
| Flutterwave test mode | Built into the app as an option (`KARO_PROVIDER=flutterwave`: Flutterwave collects the shares, the stand-in pays out). Proven against Flutterwave by `scripts/smoke_flutterwave.py`: hosted payment links for M-Pesa (KES) and MTN Uganda (UGX), and an M-Pesa test charge confirmed by Flutterwave. Test mode moves no real money |
| Payaza adapter | Ported from a working integration in another project (alianza), where Payaza's NGN card checkout ran in test mode. Switched off in Karo (`KARO_PROVIDER=payaza`). Not yet run against Payaza from Karo. KES, UGX and XAF collections and payouts need Payaza to enable them for the account. Two wrong mappings found in review are fixed; one gap is left (the M-Pesa prompt is sent before the collection row is stored), so it stays off. Added after the `submission-v2` tag |
| Paying the UK university, sending money abroad, the allowance | Simulated on the stand-in. Every receipt says so. Flutterwave refuses transfers for our test account |
| Mid-market exchange rate | Real, from open.er-api.com |
| Provider exchange quote | Simulated (stand-in rates), labelled on screen |
| Other sending services in the comparison | Labelled examples |
| The family's opening balance (KES 200,000) | Simulated, so the allowance can run after the fee is paid |
| Universities, families, bank details, phone numbers | Made up for the demo |

## Run it locally

You need Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
```

Fill in `.env`. The comments in `.env.example` explain each value. The minimum to run the demo is:

- `ANTHROPIC_API_KEY`
- `KARO_PIN_PEPPER`, `KARO_SESSION_SECRET` and `KARO_STANDIN_SECRET`. Make each one with `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`
- `KARO_DEMO_PASSWORD`, the password for the demo login
- `DEMO_PIN`, the main payer's PIN (4 to 6 digits)

Leave `KARO_PROVIDER` empty to use the stand-in provider, which needs no internet for the money part. Then start the app:

```bash
uv run uvicorn karo.web.app:app --port 8000
```

Open http://localhost:8000. Choose who you are (Amina is the main payer) and log in with `KARO_DEMO_PASSWORD`. The first start creates `data/karo.db` and seeds the demo families.

- `/log` shows the audit log and whether its hash chain is intact.
- `/demo` moves the demo clock forward, jumps to the 1st of next month to show the allowance, and resets the demo.

Run **one** uvicorn worker. The scheduler and some locks live inside the process.

### Public deployment (WhatsApp and provider webhooks)

1. Put the app behind HTTPS. We use a Cloudflare named tunnel to port 8000. Set `PUBLIC_BASE_URL` to the public address. Links sent to phones use it, and so does the Twilio signature check.
2. **WhatsApp:** set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_WHATSAPP_FROM`. In the Twilio sandbox settings, set "When a message comes in" to `<PUBLIC_BASE_URL>/webhooks/twilio` (POST). Join the sandbox from each demo phone, and put the numbers in `DEMO_WHATSAPP_AMINA`, `DEMO_WHATSAPP_BARAKA` and `DEMO_WHATSAPP_NAKATO`.
3. **Flutterwave (test mode only, optional):** set `KARO_PROVIDER=flutterwave`, `FLW_SECRET_KEY` (must start `FLWSECK_TEST-`; live keys are refused) and `FLW_WEBHOOK_HASH`. Point the dashboard webhook to `<PUBLIC_BASE_URL>/webhooks/flutterwave`. Payouts stay on the stand-in unless `KARO_FLW_PAYOUTS=flutterwave` and your account can make transfers.

## Tests

```bash
uv run pytest
```

Result at submission: **1,215 passed** (the code at commit `1352c5a`, tag `submission-v2`). With the switched-off Payaza adapter and its 65 tests added after that tag (commit `77cbb9b`): **1,280 passed**. With the fixes listed under "After submission" (branch `build`, commit `987f84d`): **1,317 passed**. The tests run offline: a fake Claude replaces the model, the stand-in replaces the provider, and HTTP calls are mocked.

| File | What it checks |
| --- | --- |
| `test_rules.py` | Each of the 15 rules blocks what it should |
| `test_approvals.py` | PIN hashing, lockout, one-time links, confirm races |
| `test_proposals.py`, `test_executor.py` | Proposals and fingerprints; pays once; reconcile; a change after the PIN is refused; freeze |
| `test_agent.py` | Tools offered by role, the prompt, loop limits, refusals, no link or PIN in anything the AI sees |
| `test_doors.py`, `test_web.py` | Messages handled before the AI (freeze word, typed PIN); web routes, PIN page, log, demo controls, webhooks |
| `test_whatsapp.py` | Twilio signature, repeated messages, media, sending |
| `test_send.py` | Sending money abroad: saved recipients, every rule, the PIN, the receipt |
| `test_csp.py` | The Content-Security-Policy on every page |
| `test_flutterwave.py` | The Flutterwave adapter against recorded test-mode responses |
| `test_collections.py`, `test_notices.py` | Payment links and reminders; signed notices, re-checks with the provider, no double credit |
| `test_comparison.py`, `test_scheduler.py` | Market rate and route comparison; allowance, reminders, reconcile |
| `test_store.py`, `test_seed.py`, `test_db_audit.py`, `test_ledger_clock.py`, `test_money.py` | Family-scoped data, demo seed, hash-chained log, ledger, money in minor units |
| `test_countries.py`, `test_config.py`, `test_flutterwave_keys.py`, `test_standin.py`, `test_export_proof.py` | Countries as settings, required secrets, live keys refused, stand-in provider, proof export |
| `test_done_means.py` | The project's "done means" list, end to end |

`tests/test_done_means.py` holds one test for each item we promised:

- Every rule blocks what it should.
- A message not really from Twilio is rejected.
- A payment notice not really from the provider (the stand-in or Flutterwave) is rejected.
- The same message sent twice is handled once.
- A retried payment pays once, including after a dropped connection.
- An invoice with hidden instructions can't get money moved.
- One family can't see another's records.
- Uganda works from settings alone, with no new code.
- The whole story runs on the stand-in provider with no internet.
- The AI cannot send money to a recipient the PIN has not confirmed.
- A large send to a recipient added today waits 24 hours.

### End-to-end check with the real Claude

With the app running, this drives the 10 demo scenes through the web page in short casual messages, with the invoices sent as PDFs, and prints PASS or FAIL per scene. It uses the real Claude API, so it costs API credit. It also resets the demo, so never aim it at a demo you want to keep. On the submitted code it passed all ten, and it passed all ten again on the fixed code at `987f84d`.

```bash
uv run python scripts/e2e_web_check.py http://localhost:8000
```

### Execution evidence

```bash
uv run python scripts/export_proof.py
```

This writes the audit log, payouts and collections, and a note on what was real and what was simulated, to `proof/run-<time>/`. It prints the audit chain head. The exit code is 0 only if the chain is intact.

## Project layout

```
src/karo/
  rules.py          the 15 money rules (pure functions)
  agent/            Claude loop, tools and prompt
  doors/            what every message passes before the AI; WhatsApp
  web/              FastAPI app: chat, PIN page, log, demo controls
  proposals.py      propose, then send the PIN link to the main payer only
  approvals.py      PIN hashing, lockout, one-time links
  executor.py       the only place money leaves
  collections.py    relatives' payment links and reminders
  notices.py        signed provider notices, re-checked with the provider
  providers/        provider interface, stand-in, Flutterwave (test mode), hybrid
  audit.py          hash-chained, append-only log
config/             countries (Kenya, Uganda, Cameroon, UK), demo institutions, stand-in rates, route examples
scripts/            smoke test, end-to-end check, proof export
tests/
```

## Third-party components

| Component | Version | Licence |
| --- | --- | --- |
| anthropic (Python SDK) | 1.15.0 | MIT |
| FastAPI | 0.146.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| twilio | 9.12.0 | MIT |
| APScheduler | 3.11.3 | MIT |
| argon2-cffi | 25.1.0 | MIT |
| Babel | 2.18.0 | BSD-3-Clause |
| phonenumbers | 9.0.41 | Apache-2.0 |
| itsdangerous | 2.2.0 | BSD-3-Clause |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| tzdata | 2026.5 | Apache-2.0 |
| Deep Chat (web chat box, from jsDelivr) | 2.5.1 | MIT |
| pytest, respx, time-machine (tests only) | 9.1.1, 0.23.1, 3.5.1 | MIT, BSD-3-Clause, MIT |

The promo video uses music by Sascha Ende (ende.app), "Happy Beats & Business Moves" Vol. 9 and Vol. 12, licensed CC BY 4.0 (ende.app/standard-license).

Exchange rates by [Exchange Rate API](https://www.exchangerate-api.com) (open.er-api.com). Flutterwave and Twilio are used through their public APIs in test and sandbox modes.

## Not for real money

Karo runs in test and sandbox modes only. It has no KYC or AML checks and no licence to move money. A real launch would need a licensed partner (see the "what's next" section of the technical documentation).
