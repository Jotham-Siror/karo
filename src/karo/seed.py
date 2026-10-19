"""The demo data: the Otieno family, who pay a student's fees in Britain, and a second family so the
privacy checks have someone to keep apart. Ids are fixed so the demo reads the same every time."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import NamedTuple

from karo import approvals, audit, countries, db
from karo.clock import iso

FAMILY_ID = "fam_otieno"
OTHER_FAMILY_ID = "fam_mwangi"

POLICY = {
    "payment_cap": {"minor": 260000000, "currency": "KES"},
    "allowance_monthly_cap": {"minor": 17300000, "currency": "KES"},
    "daily_cap": {"minor": 346000000, "currency": "KES"},
    "new_payee_threshold": {"minor": 3460000, "currency": "KES"},
    "new_payee_wait_seconds": 86400,
    "quote_ttl_seconds": 900,
    "pin_max_attempts": 3,
    "pin_lockout_seconds": 900,
}


class _Person(NamedTuple):
    id: str
    family_id: str
    role: str
    name: str
    country: str
    phone: str | None = None
    email: str | None = None


_PEOPLE = (
    _Person("m_amina", FAMILY_ID, "main_payer", "Amina", "KE", "+254700000101", "amina@example.com"),
    _Person("m_baraka", FAMILY_ID, "relative", "Baraka", "KE", "+254700000102"),
    _Person("m_nakato", FAMILY_ID, "relative", "Nakato", "UG", "+256770000103"),
    _Person("m_jabali", FAMILY_ID, "student", "Jabali", "GB"),  # no phone: he uses the web page
    _Person("m_wanjiru", OTHER_FAMILY_ID, "main_payer", "Wanjiru", "KE"),
)

_FAMILIES = (
    (FAMILY_ID, "Otieno family"),
    (OTHER_FAMILY_ID, "Mwangi family"),
)

_STUDENT_ACCOUNT = {"country": "GB", "currency": "GBP", "sort_code": "30-90-11", "account_number": "70012345"}


def _whatsapp_identities(whatsapp: dict[str, str]) -> list[tuple[str, str]]:
    by_name = {p.name.lower(): p for p in _PEOPLE}
    unknown = sorted(set(whatsapp) - set(by_name))
    if unknown:
        raise ValueError(f"No demo member is called {', '.join(unknown)}. Use one of: {', '.join(sorted(by_name))}.")
    # Each number is read as the member's own country writes it, so a Ugandan number is not taken for a Kenyan one.
    return [
        (f"whatsapp:{countries.normalise_phone(number, by_name[name].country)}", by_name[name].id)
        for name, number in whatsapp.items()
    ]


def _add_identity(conn: sqlite3.Connection, channel: str, address: str, member_id: str) -> None:
    conn.execute("INSERT INTO identities (channel, address, member_id) VALUES (?, ?, ?)", (channel, address, member_id))


def _add_trusted_payee(
    conn: sqlite3.Connection, payee_id: str, name: str, details: dict[str, str], since: datetime, source: str
) -> None:
    conn.execute(
        "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (payee_id, FAMILY_ID, name, json.dumps(details, sort_keys=True), iso(since), source),
    )


def seed_demo(
    conn: sqlite3.Connection, *, pin: str, pepper: str, now: datetime, whatsapp: dict[str, str] | None = None
) -> str:
    """Builds both demo families and returns the Otieno family's id. Reset means a new database file."""
    whatsapp_identities = _whatsapp_identities(whatsapp or {})
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM families WHERE id = ?", (FAMILY_ID,)).fetchone() is not None:
            raise ValueError("The demo family already exists. To start again, use a new database file.")

        for family_id, name in _FAMILIES:
            conn.execute(
                "INSERT INTO families (id, name, home_country, home_currency, timezone, policy_json) "
                "VALUES (?, ?, 'KE', 'KES', 'Africa/Nairobi', ?)",
                (family_id, name, json.dumps(POLICY)),
            )
        for person in _PEOPLE:
            conn.execute(
                "INSERT INTO members (id, family_id, role, name, country, phone, email) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (person.id, person.family_id, person.role, person.name, person.country, person.phone, person.email),
            )
            _add_identity(conn, "web", person.name.lower(), person.id)
        for address, member_id in whatsapp_identities:
            _add_identity(conn, "whatsapp", address, member_id)

        institution = next(i for i in countries.institutions() if i["name"] == "University of Westbridge")
        # The directory's own labels (name, test_data) are not bank details; extra keys would stop an invoice matching.
        details = {k: v for k, v in institution.items() if k not in ("name", "test_data")}
        since = now - timedelta(days=30)
        _add_trusted_payee(conn, "tp_westbridge", institution["name"], details, since, "directory")
        _add_trusted_payee(conn, "tp_jabali", "Jabali Otieno", _STUDENT_ACCOUNT, since, "added")

        for family_id, _ in _FAMILIES:
            approvals.set_pin(conn, family_id, pin, pepper, now)
        audit.record(
            conn,
            at=now,
            family_id=FAMILY_ID,
            actor="seed",
            event="seed.demo",
            data={"families": [family_id for family_id, _ in _FAMILIES]},
        )
    return FAMILY_ID
