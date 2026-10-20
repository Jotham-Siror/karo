"""Writes the proof of a run: the audit log, the money rows, and a note on what was real and what was pretend.

Usage:
    uv run python scripts/export_proof.py                 # the database named by KARO_DB (see .env)
    uv run python scripts/export_proof.py --db PATH --out DIR

Writes DIR/run-<stamp>/ (default DIR is proof/) holding audit.json, payouts.csv, collections.csv and README.md.
The chain head is printed. Copy it somewhere outside the database: the chain can show a changed row, but it can't
show that the newest rows were cut off, so the head written down earlier is what catches that.
Exit code: 0 if the audit chain is intact, 1 if it is broken, 2 if the export could not be made (no database,
or the run folder is already there: nothing is overwritten).
The database is only read. Audit data holds no PIN, token or key, and the collection links are not exported.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from karo import audit, clock, db
from karo.config import load_settings
from karo.money import Money

ROOT = Path(__file__).resolve().parent.parent
STANDIN = "standin"

PAYOUT_COLUMNS = (
    "id",
    "family_id",
    "kind",
    "status",
    "provider",
    "reference",
    "provider_id",
    "amount",
    "currency",
    "debit",
    "debit_currency",
    "home_value",
    "home_currency",
    "simulated",
    "proposal_id",
    "created_at",
    "updated_at",
)
COLLECTION_COLUMNS = (
    "id",
    "family_id",
    "share_id",
    "provider",
    "tx_ref",
    "provider_id",
    "amount",
    "currency",
    "credit",
    "credit_currency",
    "status",
    "simulated",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class Exported:
    folder: Path
    chain_head: str
    intact: bool
    broken_at_seq: int | None


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _amount(minor: int, currency: str) -> str:
    return str(Money(minor, currency).decimal)


def _payout_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, f.home_currency FROM payouts p JOIN families f ON f.id = p.family_id ORDER BY p.rowid"
    )
    return [
        {
            "id": r["id"],
            "family_id": r["family_id"],
            "kind": r["kind"],
            "status": r["status"],
            "provider": r["provider"],
            "reference": r["reference"],
            "provider_id": r["provider_id"] or "",
            "amount": _amount(r["amount_minor"], r["currency"]),
            "currency": r["currency"],
            "debit": _amount(r["debit_minor"], r["debit_currency"]),
            "debit_currency": r["debit_currency"],
            "home_value": _amount(r["home_value_minor"], r["home_currency"]),
            "home_currency": r["home_currency"],
            "simulated": _flag(bool(r["simulated"])),
            "proposal_id": r["proposal_id"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def _collection_rows(conn: sqlite3.Connection) -> list[dict]:
    # The link column is left out: it is a payment page address, not evidence of anything.
    return [
        {
            "id": r["id"],
            "family_id": r["family_id"],
            "share_id": r["share_id"],
            "provider": r["provider"],
            "tx_ref": r["tx_ref"],
            "provider_id": r["provider_id"] or "",
            "amount": _amount(r["amount_minor"], r["currency"]),
            "currency": r["currency"],
            "credit": _amount(r["credit_minor"], r["credit_currency"]),
            "credit_currency": r["credit_currency"],
            "status": r["status"],
            # Collections have no simulated column: only the stand-in's checkout is pretend.
            "simulated": _flag(r["provider"] == STANDIN),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in conn.execute("SELECT * FROM collections ORDER BY rowid")
    ]


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _chain_head(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    return audit.GENESIS if row is None else row["hash"]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _tally(provider_and_simulated: list[tuple[str, bool]]) -> str:
    counts = Counter(provider_and_simulated)
    parts = [
        f"{n} through {provider} ({'simulated, no real money' if simulated else 'real test-mode calls, no real money'})"
        for (provider, simulated), n in sorted(counts.items())
    ]
    return ", ".join(parts) if parts else "none"


def _opening_balances(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT family_id, currency, SUM(amount_minor) AS minor FROM ledger WHERE ref = 'opening_balance' "
        "GROUP BY family_id, currency ORDER BY family_id"
    )
    return [f"{Money(r['minor'], r['currency'])} for {r['family_id']}" for r in rows]


def _readme(
    stamp: str,
    events: int,
    head: str,
    intact: bool,
    broken_at: int | None,
    payouts: list[dict],
    rows: list[dict],
    clock_offset: int,
    openings: list[str],
) -> str:
    chain = "intact" if intact else f"BROKEN at event {broken_at}"
    paid = _tally([(p["provider"], p["simulated"] == "true") for p in payouts])
    collected = _tally([(c["provider"], c["simulated"] == "true") for c in rows])
    moved = ""
    if clock_offset:
        moved = (
            f"\nThe demo clock was moved forward by {clock_offset} seconds during this run, "
            "so later times in the log are demo times, not real dates.\n"
        )
    opening = "".join(
        f"- The opening balance, {text}: money the demo family was given to start from (ledger ref `opening_balance`, "
        "audit event `balance.opening`). It is pretend, not a deposit anyone made.\n"
        for text in openings
    )
    return f"""# Karo proof of a run ({stamp})

Made by `scripts/export_proof.py` from the database after the run.

## This run

- Audit log: {_plural(events, "event")}. The hash chain is **{chain}**.
- Chain head: `{head}`
- Payouts: {_plural(len(payouts), "payout")}: {paid}.
- Collections: {_plural(len(rows), "collection")}: {collected}.
{moved}
Copy the chain head somewhere outside the database now. The chain shows a changed row, but cannot show that the newest
rows were removed. A head written down earlier, that no longer matches, is what shows that.

To check the chain yourself: each event's `hash` is the SHA-256 of the previous event's hash (64 zeros for the first)
followed by the compact, key-sorted JSON of its `at`, `family_id`, `actor`, `event` and `data`.

## What is real

- Claude reading the messages and invoices and proposing actions, when the run used the live API key.
- The rules in `src/karo/rules.py`, which decide what is allowed. Claude only proposes.
- The PIN page: money moves only after the main payer's PIN, on a one-time page tied to the exact proposal.
- The audit log and its hash chain.
- The market exchange rate shown in route comparisons, when the rate service could be reached.
- Flutterwave test-mode traffic, when the providers above say so. It is real traffic to Flutterwave's test
  environment, with real references, but it is not real money.

## What is simulated

- Money handled by the stand-in provider: its payouts, its collections and its balances are pretend, and are marked
  `simulated` in the CSV files.
- The stand-in checkout page, where a relative presses "Pay" instead of paying through a bank or mobile money.
- The automatic conversion of money collected in another currency (for example Ugandan shillings) into the family's
  home currency. It is recorded in the ledger and the audit log as simulated.
{opening}- Universities, families and bank details are made-up test data.

## Files

- `audit.json`: every audit event in order, the chain head, and the verify result.
- `payouts.csv`: each payout with its provider references and a `simulated` flag.
- `collections.csv`: each share collection with its provider references and a `simulated` flag.
"""


def export(conn: sqlite3.Connection, out_root: Path) -> Exported:
    stamp = clock.now(conn).strftime("%Y%m%dT%H%M%SZ")
    folder = out_root / f"run-{stamp}"
    count = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    # events() is newest first and capped, so ask for all of them and turn the list round.
    events = list(reversed(audit.events(conn, None, limit=max(count, 1))))
    head = _chain_head(conn)
    intact, broken_at = audit.verify(conn)
    payouts = _payout_rows(conn)
    collected = _collection_rows(conn)

    folder.mkdir(parents=True)
    document = {
        "chain_head": head,
        "verify": {"intact": intact, "broken_at_seq": broken_at},
        "event_count": len(events),
        "events": events,
    }
    (folder / "audit.json").write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(folder / "payouts.csv", PAYOUT_COLUMNS, payouts)
    _write_csv(folder / "collections.csv", COLLECTION_COLUMNS, collected)
    openings = _opening_balances(conn)
    readme = _readme(stamp, len(events), head, intact, broken_at, payouts, collected, clock.offset(conn), openings)
    (folder / "README.md").write_text(readme, encoding="utf-8")
    return Exported(folder, head, intact, broken_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the proof of a Karo run.")
    parser.add_argument("--db", help="the database to export (default: KARO_DB from the settings)")
    parser.add_argument("--out", help="the folder to put run-<stamp>/ in (default: proof/)")
    args = parser.parse_args(argv)

    try:
        db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    if not db_path.is_file():
        # db.connect would create an empty database here, and the export would then "prove" a run with no events.
        print(f"There is no database at {db_path}.", file=sys.stderr)
        return 2

    conn = db.connect(db_path)
    try:
        conn.execute("PRAGMA query_only = ON")
        result = export(conn, Path(args.out) if args.out else ROOT / "proof")
    except FileExistsError as error:
        # Two runs in one second share a stamp. The first stays as it is; this one is told, and is not "chain broken".
        print(f"{error.filename} already exists, so nothing was written. Wait a second and try again.", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(f"Wrote {result.folder}")
    print(f"Chain head: {result.chain_head}")
    if not result.intact:
        print(f"AUDIT CHAIN BROKEN at event {result.broken_at_seq}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
