"""Flutterwave test-mode smoke test: what works for Karo's demo, with proof saved to proof/smoke/.

Usage:
    uv run python scripts/smoke_flutterwave.py              # run every probe
    uv run python scripts/smoke_flutterwave.py --verify REF # check a hosted-link payment after paying it

Test keys only (live keys are refused). Keys are read from .env and never printed or saved.
Payout references end in _PMCK, which Flutterwave's test mode treats as "succeed"; without it,
test payouts stay pending. Each result notes that, so the proof stays honest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from karo.providers.flutterwave import BASE_URL, require_test_key

ROOT = Path(__file__).resolve().parent.parent
PROOF_DIR = ROOT / "proof" / "smoke"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
EMAIL = "karo.smoke@example.com"

# Made-up test destinations. Test mode moves no real money.
KE_PHONE = "254700000001"
UG_PHONE = "256770000001"
UK_UNIVERSITY = {
    "account_number": "41234567",
    "sort_code": "204060",
    "bank_name": "Westbridge Test Bank",
    "name": "University of Westbridge",
}


def ref(tag: str) -> str:
    return f"karo-smoke-{STAMP}-{tag}"


class Smoke:
    def __init__(self, secret: str, public_base: str) -> None:
        self.secret = secret
        self.redirect = f"{public_base.rstrip('/')}/flutterwave/return" if public_base else "https://example.com/return"
        self.http = httpx.Client(
            base_url=BASE_URL, headers={"Authorization": f"Bearer {secret}"}, timeout=30.0
        )
        self.results: list[dict] = []

    def _request(self, method: str, path: str, **kw) -> tuple[int | None, dict, str | None]:
        """A failed probe is a result to record, not a reason to stop the other probes."""
        try:
            r = self.http.request(method, path, **kw)
            return r.status_code, (r.json() if r.content else {}), None
        except (httpx.HTTPError, ValueError) as exc:
            return None, {}, type(exc).__name__

    def _record(self, step: str, decides: str, ok: bool, http: int | None, body: dict, error: str | None) -> None:
        self.results.append({"step": step, "ok": ok, "decides": decides, "http": http,
                             "message": body.get("message") or error, "data": body.get("data")})

    def call(self, step: str, decides: str, method: str, path: str, **kw) -> dict | None:
        http, body, error = self._request(method, path, **kw)
        ok = http is not None and http < 300 and body.get("status") == "success"
        self._record(step, decides, ok, http, body, error)
        return body if ok else None

    def poll_verify(self, step: str, tx_ref: str, tries: int = 6, wait: float = 5.0) -> None:
        """Test-mode mobile money is authorised automatically after a few seconds."""
        for _ in range(tries):
            time.sleep(wait)
            http, body, error = self._request("GET", "/transactions/verify_by_reference", params={"tx_ref": tx_ref})
            status = (body.get("data") or {}).get("status")
            if status in {"successful", "failed"}:
                break
        self._record(step, "collection confirmed by re-check", status == "successful", http, body, error)

    def run(self) -> None:
        self.call("1 keys and balances", "keys work; test balances per currency", "GET", "/balances")

        for src in ("KES", "UGX"):
            self.call(f"2 rate {src}->GBP", "real exchange rates", "GET", "/transfers/rates",
                      params={"amount": 6000, "source_currency": src, "destination_currency": "GBP"})

        for tag, currency, amount, option in (("ke-link", "KES", 100, "mpesa"),
                                              ("ug-link", "UGX", 1000, "mobilemoneyuganda")):
            self.call(f"3 hosted link {currency} {option}", "relative pays by local method via checkout",
                      "POST", "/payments", json={
                          "tx_ref": ref(tag), "amount": amount, "currency": currency,
                          "redirect_url": self.redirect, "payment_options": option,
                          "customer": {"email": EMAIL, "name": "Karo Smoke Test"},
                          "customizations": {"title": "Karo smoke test"},
                      })

        if self.call("4 M-Pesa direct charge", "Kenya collection without a browser", "POST", "/charges",
                     params={"type": "mpesa"}, json={
                         "tx_ref": ref("ke-charge"), "amount": 100, "currency": "KES",
                         "phone_number": KE_PHONE, "email": EMAIL, "fullname": "Karo Smoke Test"}):
            self.poll_verify("4b M-Pesa verify", ref("ke-charge"))

        if self.call("5 MTN Uganda direct charge", "Uganda collection without a browser", "POST", "/charges",
                     params={"type": "mobile_money_uganda"}, json={
                         "tx_ref": ref("ug-charge"), "amount": 1000, "currency": "UGX", "network": "MTN",
                         "phone_number": UG_PHONE, "email": EMAIL, "fullname": "Karo Smoke Test"}):
            self.poll_verify("5b MTN Uganda verify", ref("ug-charge"))

        kes_payout = {
            "account_bank": "MPS", "account_number": KE_PHONE, "amount": 100, "currency": "KES",
            "debit_currency": "KES", "beneficiary_name": "Karo Smoke Test",
            "narration": "Karo smoke test", "reference": ref("ke-payout") + "_PMCK",
        }
        sent = self.call("6 KES payout to M-Pesa", "at least one real test-mode payout", "POST", "/transfers",
                         json=kes_payout)
        if sent:
            self.call("6b KES payout status", "payout reference and status", "GET", f"/transfers/{sent['data']['id']}")
            self.call("7 duplicate reference", "provider also refuses double pay (expect FAIL here)",
                      "POST", "/transfers", json=kes_payout)

        gbp_sent = self.call("8 GBP payout to UK university", "university payout real or simulated", "POST",
                             "/transfers", json={
                                 "account_number": UK_UNIVERSITY["account_number"], "amount": 60,
                                 "currency": "GBP", "debit_currency": "KES",
                                 "beneficiary_name": UK_UNIVERSITY["name"], "narration": "Karo smoke test fees",
                                 "reference": ref("gb-payout") + "_PMCK",
                                 "meta": [{"routing_number": UK_UNIVERSITY["sort_code"],
                                           "bank_name": UK_UNIVERSITY["bank_name"], "account_type": "corporate",
                                           "beneficiary_merchant_name": UK_UNIVERSITY["name"]}],
                             })
        if gbp_sent:
            self.call("8b GBP payout status", "payout reference and status", "GET",
                      f"/transfers/{gbp_sent['data']['id']}")

    def verify(self, tx_ref: str) -> None:
        self.call("verify hosted-link payment", "collection confirmed by re-check", "GET",
                  "/transactions/verify_by_reference", params={"tx_ref": tx_ref})

    def save(self) -> Path:
        PROOF_DIR.mkdir(parents=True, exist_ok=True)
        path = PROOF_DIR / f"smoke-{STAMP}.json"
        record = {
            "run_at": STAMP, "provider": "Flutterwave v3", "mode": "test",
            "note": "Payout references ending _PMCK are told to succeed by Flutterwave test mode.",
            "results": self.results,
        }
        text = json.dumps(record, indent=2, default=str).replace(self.secret, "[REDACTED]")
        path.write_text(text, encoding="utf-8")
        return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify", metavar="TX_REF", help="verify one hosted-link payment by its tx_ref")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    try:
        secret = require_test_key(os.environ.get("FLW_SECRET_KEY", ""))
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    smoke = Smoke(secret, os.environ.get("PUBLIC_BASE_URL", ""))
    if args.verify:
        smoke.verify(args.verify)
    else:
        smoke.run()
    path = smoke.save()

    for r in smoke.results:
        mark = "PASS" if r["ok"] else "FAIL"
        detail = r.get("message") or ""
        link = r["data"].get("link") if isinstance(r.get("data"), dict) else None
        print(f"{mark}  {r['step']:<34} {detail}" + (f"\n      open to pay: {link}" if link else ""))
    print(f"\nSaved: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
