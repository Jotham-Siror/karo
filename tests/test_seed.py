"""The demo family seed. Every test names the bug it would catch."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from karo import approvals, audit, clock, countries, db, rules, store
from karo.domain import Payee, Role
from karo.money import Money
from karo.seed import seed_demo

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
OTHER = "fam_mwangi"
PIN = "4821"
PEPPER = "test-pepper"

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


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "karo.db")
    db.init(connection)
    yield connection
    connection.close()


def seed(conn, **kwargs) -> str:
    return seed_demo(conn, pin=PIN, pepper=PEPPER, now=NOW, **kwargs)


def identities(conn, channel) -> dict[str, str]:
    rows = conn.execute("SELECT address, member_id FROM identities WHERE channel = ?", (channel,))
    return {r["address"]: r["member_id"] for r in rows}


def row_counts(conn) -> tuple[int, ...]:
    tables = ("families", "members", "identities", "trusted_payees", "audit")
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables)


def test_seed_returns_the_main_family_id(conn):
    # Callers use the returned id to find the demo family.
    assert seed(conn) == FAM


def test_otieno_family_has_the_demo_settings(conn):
    # A wrong country, currency, timezone or limit would make every demo amount and date wrong.
    seed(conn)
    row = store.family_row(conn, FAM)
    assert (row["name"], row["home_country"], row["home_currency"], row["timezone"]) == (
        "Otieno family",
        "KE",
        "KES",
        "Africa/Nairobi",
    )
    assert json.loads(row["policy_json"]) == POLICY


def test_otieno_members_have_the_demo_roles_and_contacts(conn):
    # Jabali has no phone (he uses the web page); a made-up number would send him WhatsApp messages.
    seed(conn)
    rows = conn.execute(
        "SELECT id, role, country, phone, email FROM members WHERE family_id = ? ORDER BY rowid", (FAM,)
    )
    assert [tuple(r) for r in rows] == [
        ("m_amina", "main_payer", "KE", "+254700000101", "amina@example.com"),
        ("m_baraka", "relative", "KE", "+254700000102", None),
        ("m_nakato", "relative", "UG", "+256770000103", None),
        ("m_jabali", "student", "GB", None, None),
    ]


def test_everyone_gets_a_web_login(conn):
    # A member without a web identity cannot sign in to the demo page.
    seed(conn)
    assert identities(conn, "web") == {
        "amina": "m_amina",
        "baraka": "m_baraka",
        "nakato": "m_nakato",
        "jabali": "m_jabali",
        "wanjiru": "m_wanjiru",
    }


def test_no_whatsapp_identity_unless_asked(conn):
    # A default WhatsApp number would route someone else's messages to a demo member.
    seed(conn)
    assert identities(conn, "whatsapp") == {}


def test_whatsapp_numbers_are_normalised_in_each_members_country(conn):
    # Using one region for everyone would turn Nakato's Ugandan number into a Kenyan one.
    seed(conn, whatsapp={"amina": "0700 000 101", "nakato": "0770 000 103", "wanjiru": "0700 000 104"})
    assert identities(conn, "whatsapp") == {
        "whatsapp:+254700000101": "m_amina",
        "whatsapp:+256770000103": "m_nakato",
        "whatsapp:+254700000104": "m_wanjiru",
    }


def test_unknown_whatsapp_name_is_refused_and_nothing_is_written(conn):
    # A misspelt name would silently drop the number, and a half-built demo would block the retry.
    with pytest.raises(ValueError, match="amna"):
        seed(conn, whatsapp={"amina": "0700 000 101", "amna": "0700 000 105"})
    assert row_counts(conn) == (0, 0, 0, 0, 0)


def test_invalid_whatsapp_number_is_refused_and_nothing_is_written(conn):
    # A bad number must stop the seed, not store a nonsense identity.
    with pytest.raises(ValueError):
        seed(conn, whatsapp={"baraka": "12"})
    assert row_counts(conn) == (0, 0, 0, 0, 0)


def test_trusted_payees_are_the_university_and_the_students_account(conn):
    # The ids, sources and the 30-day history decide whether the new-payee wait applies to the demo payments.
    seed(conn)
    rows = conn.execute(
        "SELECT id, name, source, trusted_since FROM trusted_payees WHERE family_id = ? ORDER BY rowid", (FAM,)
    )
    since = clock.iso(NOW - timedelta(days=30))
    assert [tuple(r) for r in rows] == [
        ("tp_westbridge", "University of Westbridge", "directory", since),
        ("tp_jabali", "Jabali Otieno", "added", since),
    ]


def test_seeded_payees_match_what_an_invoice_for_them_claims(conn):
    # Extra keys such as the directory's test_data flag would stop the rule checker matching the real invoice.
    seed(conn)
    state = store.load_state(conn, FAM, NOW)
    westbridge = next(i for i in countries.institutions() if i["name"] == "University of Westbridge")
    claim = Payee.of(
        westbridge["name"], **{k: v for k, v in westbridge.items() if k not in ("name", "test_data")}
    )
    jabali = Payee.of("Jabali Otieno", country="GB", currency="GBP", sort_code="30-90-11", account_number="70012345")
    assert rules.check_payee(claim, state).outcome is rules.Outcome.PASS
    assert rules.check_payee(jabali, state).outcome is rules.Outcome.PASS


def test_pin_is_set_for_both_families(conn):
    # A family seeded without its PIN could never confirm anything.
    seed(conn)
    assert approvals.verify_pin(conn, FAM, "m_amina", PIN, PEPPER, NOW).ok
    assert approvals.verify_pin(conn, OTHER, "m_wanjiru", PIN, PEPPER, NOW).ok


def test_load_state_works_and_keeps_the_two_families_apart(conn):
    # The privacy check needs a second family whose rows never show up in the first one's state.
    seed(conn)
    otieno, mwangi = store.load_state(conn, FAM, NOW), store.load_state(conn, OTHER, NOW)
    assert [m.id for m in otieno.members] == ["m_amina", "m_baraka", "m_nakato", "m_jabali"]
    assert [t.id for t in otieno.trusted_payees] == ["tp_westbridge", "tp_jabali"]
    assert [(m.id, m.role) for m in mwangi.members] == [("m_wanjiru", Role.MAIN_PAYER)]
    assert mwangi.trusted_payees == ()


def test_second_seed_raises_and_changes_nothing(conn):
    # Seeding twice must not duplicate or overwrite; reset means a new database file.
    seed(conn)
    before = row_counts(conn)
    with pytest.raises(ValueError, match="already"):
        seed(conn)
    assert row_counts(conn) == before


def test_bad_pin_is_refused_and_nothing_is_written(conn):
    # A half-built demo would make the corrected retry fail with "already exists".
    with pytest.raises(ValueError):
        seed_demo(conn, pin="12", pepper=PEPPER, now=NOW)
    assert row_counts(conn) == (0, 0, 0, 0, 0)


def test_seed_is_audited_and_the_chain_holds(conn):
    # The seed is a state change and belongs in the tamper-evident log.
    seed(conn)
    assert [e["event"] for e in audit.events(conn, FAM) if e["event"] == "seed.demo"] == ["seed.demo"]
    assert audit.verify(conn) == (True, None)


def test_the_otieno_family_starts_with_a_simulated_opening_balance_that_is_in_the_log(conn):
    # Without it the first monthly allowance has nothing to draw on once the fee is paid, and the funds rule stops it.
    seed(conn)
    (opening,) = conn.execute("SELECT * FROM ledger WHERE ref = 'opening_balance'").fetchall()
    assert (opening["family_id"], opening["kind"], opening["currency"], opening["amount_minor"]) == (
        FAM,
        "collection",
        "KES",
        20000000,
    )
    assert opening["at"] == clock.iso(NOW)
    (logged,) = [e for e in audit.events(conn, FAM) if e["event"] == "balance.opening"]
    assert logged["data"] == {"amount": "200000.00", "currency": "KES", "simulated": True}
    assert store.load_state(conn, FAM, NOW).balances == (Money(20000000, "KES"),)
    assert store.load_state(conn, OTHER, NOW).balances == ()
    assert audit.verify(conn) == (True, None)
