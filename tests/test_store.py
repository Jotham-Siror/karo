"""The store (family-scoped reads and writes), the outbox, and the proposal JSON round trip.

Rows are seeded with plain SQL: the real demo seed comes later. Every test names the bug it would catch.
"""

import json
import sqlite3
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from karo import clock, db, ledger, outbox, store
from karo.domain import (
    FamilyState,
    Kind,
    Member,
    Payee,
    Policy,
    Proposal,
    Quote,
    Role,
    Share,
    StandingOrder,
    TrustedPayee,
)
from karo.money import Money
from karo.rules import Decision, Outcome, RuleResult

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
OTHER = "fam_other"

WESTBRIDGE = Payee.of(
    "University of Westbridge", country="GB", currency="GBP", sort_code="20-40-60", account_number="41234567"
)
JABALI_ACCOUNT = Payee.of(
    "Jabali Otieno", country="GB", currency="GBP", sort_code="30-90-11", account_number="70012345"
)

KENYA_POLICY = {
    "payment_cap": {"minor": 260000000, "currency": "KES"},
    "allowance_monthly_cap": {"minor": 17300000, "currency": "KES"},
    "daily_cap": {"minor": 346000000, "currency": "KES"},
    "new_payee_threshold": {"minor": 3460000, "currency": "KES"},
    "new_payee_wait_seconds": 86400,
    "quote_ttl_seconds": 900,
    "pin_max_attempts": 3,
    "pin_lockout_seconds": 900,
}
BRITAIN_POLICY = {
    "payment_cap": {"minor": 1500000, "currency": "GBP"},
    "allowance_monthly_cap": {"minor": 100000, "currency": "GBP"},
    "daily_cap": {"minor": 2000000, "currency": "GBP"},
    "new_payee_threshold": {"minor": 20000, "currency": "GBP"},
    "new_payee_wait_seconds": 3600,
    "quote_ttl_seconds": 600,
    "pin_max_attempts": 5,
    "pin_lockout_seconds": 1800,
}


def add_family(conn, family_id, *, country, currency, timezone, policy, name="Test family"):
    conn.execute(
        "INSERT INTO families (id, name, home_country, home_currency, timezone, policy_json) VALUES (?, ?, ?, ?, ?, ?)",
        (family_id, name, country, currency, timezone, json.dumps(policy)),
    )


def add_member(conn, member_id, family_id, role, name, country="KE"):
    conn.execute(
        "INSERT INTO members (id, family_id, role, name, country) VALUES (?, ?, ?, ?, ?)",
        (member_id, family_id, role, name, country),
    )


def add_identity(conn, channel, address, member_id):
    conn.execute("INSERT INTO identities (channel, address, member_id) VALUES (?, ?, ?)", (channel, address, member_id))


def add_trusted_payee(conn, payee_id, family_id, payee, since, source="directory"):
    conn.execute(
        "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) VALUES (?, ?, ?, ?, ?, ?)",
        (payee_id, family_id, payee.name, json.dumps(dict(payee.details)), clock.iso(since), source),
    )


def add_fee_row(conn, fee_id, family_id, *, minor=600000, created_at=NOW):
    conn.execute(
        "INSERT INTO fees (id, family_id, payee_name, payee_details_json, amount_minor, currency, created_at) "
        "VALUES (?, ?, 'University of Westbridge', '{}', ?, 'GBP', ?)",
        (fee_id, family_id, minor, clock.iso(created_at)),
    )


def add_share(conn, share_id, family_id, member_id, fee_id, *, minor, paid=0):
    conn.execute(
        "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, paid_minor, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'GBP', ?, ?)",
        (share_id, family_id, fee_id, member_id, minor, paid, clock.iso(NOW)),
    )


def add_standing_order(conn, order_id, family_id, payee_id, *, approved_by, active=1):
    conn.execute(
        "INSERT INTO standing_orders (id, family_id, payee_id, amount_minor, currency, day_of_month, approved_by, active) "
        "VALUES (?, ?, ?, 50000, 'GBP', 1, ?, ?)",
        (order_id, family_id, payee_id, approved_by, active),
    )


def add_payout(conn, created_at, home_minor, *, family_id=FAM, kind="pay_fee"):
    stamp = clock.iso(created_at)
    proposal_id = db.new_id("prop")
    conn.execute(
        "INSERT INTO proposals (id, family_id, kind, requested_by, payload_json, digest, status, decision_json, "
        "created_at, updated_at) VALUES (?, ?, ?, 'm_amina', '{}', 'd', 'executed', '[]', ?, ?)",
        (proposal_id, family_id, kind, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO payouts (id, proposal_id, family_id, provider, reference, amount_minor, currency, debit_minor, "
        "debit_currency, home_value_minor, kind, status, simulated, created_at, updated_at) "
        "VALUES (?, ?, ?, 'sim', ?, 1, 'GBP', ?, 'KES', ?, ?, 'succeeded', 1, ?, ?)",
        (db.new_id("pay"), proposal_id, family_id, db.new_id("ref"), home_minor, home_minor, kind, stamp, stamp),
    )


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "karo.db")
    db.init(connection)

    add_family(
        connection, FAM, country="KE", currency="KES", timezone="Africa/Nairobi", policy=KENYA_POLICY, name="Otieno"
    )
    add_member(connection, "m_amina", FAM, "main_payer", "Amina")
    add_member(connection, "m_baraka", FAM, "relative", "Baraka")
    add_member(connection, "m_jabali", FAM, "student", "Jabali", country="GB")
    add_identity(connection, "web", "amina", "m_amina")
    add_identity(connection, "whatsapp", "whatsapp:+254700000101", "m_amina")
    add_identity(connection, "web", "baraka", "m_baraka")
    add_identity(connection, "web", "jabali", "m_jabali")
    add_trusted_payee(connection, "tp_westbridge", FAM, WESTBRIDGE, NOW - timedelta(days=30))
    add_trusted_payee(connection, "tp_jabali", FAM, JABALI_ACCOUNT, NOW - timedelta(days=2), source="added")
    add_fee_row(connection, "fee_1", FAM)
    add_share(connection, "sh_baraka", FAM, "m_baraka", "fee_1", minor=200000, paid=50000)
    add_standing_order(connection, "so_active", FAM, "tp_jabali", approved_by="m_amina")
    add_standing_order(connection, "so_stopped", FAM, "tp_jabali", approved_by="m_amina", active=0)

    add_family(connection, OTHER, country="GB", currency="GBP", timezone="Europe/London", policy=BRITAIN_POLICY)
    add_member(connection, "m_wanjiru", OTHER, "main_payer", "Wanjiru")
    add_identity(connection, "web", "wanjiru", "m_wanjiru")
    add_trusted_payee(
        connection, "tp_other", OTHER, Payee.of("Other College", sort_code="11-11-11"), NOW - timedelta(days=9)
    )
    add_fee_row(connection, "fee_other", OTHER, minor=999)
    add_share(connection, "sh_other", OTHER, "m_wanjiru", "fee_other", minor=999)
    add_standing_order(connection, "so_other", OTHER, "tp_other", approved_by="m_wanjiru")
    ledger.post(connection, family_id=OTHER, at=NOW, amount=Money(777, "GBP"), kind="collection", ref="c_other")
    yield connection
    connection.close()


def full_proposal(**changes) -> Proposal:
    base = {
        "id": "prop_full",
        "family_id": FAM,
        "kind": Kind.SPLIT,
        "requested_by": "m_amina",
        "amount": Money(600000, "GBP"),
        "payee": WESTBRIDGE,
        "quote": Quote(
            "q_1",
            FAM,
            Money(99000000, "KES"),
            Money(600000, "GBP"),
            datetime(2026, 10, 19, 12, 15, 0, 123456, tzinfo=ZoneInfo("Africa/Nairobi")),
        ),
        "share_id": "sh_baraka",
        "standing_order_id": "so_active",
        "fee_id": "fee_1",
        "split": (("m_baraka", Money(200000, "GBP")), ("m_nakato", Money(400000, "GBP"))),
    }
    return Proposal(**{**base, **changes})


def insert(conn, proposal=None, *, status="waiting", decision=None, now=NOW):
    proposal = proposal or full_proposal()
    store.insert_proposal(conn, proposal, status=status, decision=decision or Decision(()), now=now)
    return proposal


# --- families and members --------------------------------------------------------------------


def test_family_row_raises_lookup_error_for_a_missing_family(conn):
    # Bug caught: a missing family returning None, so the caller crashes later on a None subscript.
    with pytest.raises(LookupError):
        store.family_row(conn, "fam_nobody")


def test_family_config_reads_that_familys_settings(conn):
    # Bug caught: reading the policy, zone or PIN limits of the wrong family, or leaving seconds as bare ints.
    config = store.family_config(conn, OTHER)
    assert config.home_country == "GB"
    assert config.timezone == ZoneInfo("Europe/London")
    assert config.quote_ttl == timedelta(seconds=600)
    assert config.pin_max_attempts == 5
    assert config.pin_lockout == timedelta(seconds=1800)
    assert config.policy == Policy(
        home_currency="GBP",
        payment_cap=Money(1500000, "GBP"),
        allowance_monthly_cap=Money(100000, "GBP"),
        daily_cap=Money(2000000, "GBP"),
        new_payee_threshold=Money(20000, "GBP"),
        new_payee_wait=timedelta(hours=1),
    )


def test_members_lists_only_this_familys_people_in_insertion_order(conn):
    # Bug caught: a missing family filter listing another family's members, or an unstable order.
    assert store.members(conn, FAM) == (
        Member("m_amina", FAM, Role.MAIN_PAYER, "Amina"),
        Member("m_baraka", FAM, Role.RELATIVE, "Baraka"),
        Member("m_jabali", FAM, Role.STUDENT, "Jabali"),
    )


def test_member_returns_none_for_someone_in_another_family(conn):
    # Bug caught: looking a member up by id alone, so one family can act as another's member.
    assert store.member(conn, FAM, "m_baraka") == Member("m_baraka", FAM, Role.RELATIVE, "Baraka")
    assert store.member(conn, FAM, "m_wanjiru") is None


def test_main_payer_is_the_familys_own(conn):
    # Bug caught: picking any main payer in the table instead of this family's.
    assert store.main_payer(conn, FAM).id == "m_amina"
    assert store.main_payer(conn, OTHER).id == "m_wanjiru"


def test_main_payer_raises_when_the_family_has_none(conn):
    # Bug caught: returning None where callers expect a Member, which would make approvals fail with an AttributeError.
    add_family(conn, "fam_empty", country="KE", currency="KES", timezone="Africa/Nairobi", policy=KENYA_POLICY)
    with pytest.raises(LookupError):
        store.main_payer(conn, "fam_empty")


def test_member_for_identity_finds_the_sender_on_any_channel(conn):
    # Bug caught: an unknown or mismatched channel/address being matched, or a known one missed.
    assert store.member_for_identity(conn, "whatsapp", "whatsapp:+254700000101").id == "m_amina"
    assert store.member_for_identity(conn, "web", "wanjiru") == Member("m_wanjiru", OTHER, Role.MAIN_PAYER, "Wanjiru")
    assert store.member_for_identity(conn, "web", "nobody") is None
    assert store.member_for_identity(conn, "whatsapp", "amina") is None


def test_identities_of_lists_channel_and_address_pairs(conn):
    # Bug caught: returning raw rows, or another member's identities.
    assert store.identities_of(conn, "m_amina") == [("web", "amina"), ("whatsapp", "whatsapp:+254700000101")]
    assert store.identities_of(conn, "m_baraka") == [("web", "baraka")]


# --- load_state ------------------------------------------------------------------------------


def test_load_state_for_a_missing_family_raises(conn):
    # Bug caught: an empty FamilyState for an unknown family id, which the rules would then judge.
    with pytest.raises(LookupError):
        store.load_state(conn, "fam_nobody", NOW)


def test_load_state_reads_the_policy_and_members(conn):
    # Bug caught: policy limits parsed into the wrong field or currency, or members of another family included.
    state = store.load_state(conn, FAM, NOW)
    assert isinstance(state, FamilyState)
    assert state.family_id == FAM
    assert state.policy == Policy(
        home_currency="KES",
        payment_cap=Money(260000000, "KES"),
        allowance_monthly_cap=Money(17300000, "KES"),
        daily_cap=Money(346000000, "KES"),
        new_payee_threshold=Money(3460000, "KES"),
        new_payee_wait=timedelta(hours=24),
    )
    assert [m.id for m in state.members] == ["m_amina", "m_baraka", "m_jabali"]
    assert state.main_payer.id == "m_amina"


def test_load_state_builds_trusted_payees_with_sorted_details_and_aware_dates(conn):
    # Bug caught: payee details left as an unsorted dict, or trusted_since left as text, so matching and the wait rule break.
    state = store.load_state(conn, FAM, NOW)
    assert state.trusted_payees == (
        TrustedPayee("tp_westbridge", FAM, WESTBRIDGE, NOW - timedelta(days=30)),
        TrustedPayee("tp_jabali", FAM, JABALI_ACCOUNT, NOW - timedelta(days=2)),
    )


def test_load_state_builds_shares_in_their_own_currency(conn):
    # Bug caught: paid and total amounts mixed up, or given the wrong currency.
    state = store.load_state(conn, FAM, NOW)
    assert state.shares == (Share("sh_baraka", FAM, "m_baraka", Money(200000, "GBP"), Money(50000, "GBP")),)
    assert state.shares[0].remaining == Money(150000, "GBP")


def test_load_state_gives_active_standing_orders_their_trusted_payee(conn):
    # Bug caught: a stopped order still allowing allowance runs, or an order without the payee it pays.
    state = store.load_state(conn, FAM, NOW)
    assert state.standing_orders == (
        StandingOrder("so_active", FAM, JABALI_ACCOUNT, Money(50000, "GBP"), 1, "m_amina", True),
    )


def test_load_state_reads_balances_freeze_and_pin_lock(conn):
    # Bug caught: balances summed across currencies or families, or a freeze / PIN lock not reaching the rules.
    ledger.post(conn, family_id=FAM, at=NOW, amount=Money(500000, "KES"), kind="collection", ref="c1")
    ledger.post(conn, family_id=FAM, at=NOW, amount=Money(-120000, "KES"), kind="payout", ref="p1")
    ledger.post(conn, family_id=FAM, at=NOW, amount=Money(30000, "GBP"), kind="conversion", ref="v1")
    conn.execute(
        "UPDATE families SET frozen = 1, pin_locked_until = ? WHERE id = ?",
        (clock.iso(NOW + timedelta(minutes=15)), FAM),
    )
    state = store.load_state(conn, FAM, NOW)
    assert state.balances == (Money(30000, "GBP"), Money(380000, "KES"))
    assert state.frozen is True
    assert state.pin_locked_until == NOW + timedelta(minutes=15)


def test_load_state_defaults_to_unfrozen_and_unlocked(conn):
    # Bug caught: a NULL pin_locked_until or a 0 flag coming back as something truthy.
    state = store.load_state(conn, FAM, NOW)
    assert state.frozen is False
    assert state.pin_locked_until is None


def test_load_state_outflow_today_uses_the_familys_local_day(conn):
    # Bug caught: counting by the UTC date, so a payout made late the evening before (Nairobi) used up today's daily cap.
    now = datetime(2026, 10, 19, 22, 30, tzinfo=UTC)  # 01:30 on 20 October in Nairobi
    add_payout(conn, datetime(2026, 10, 19, 20, 0, tzinfo=UTC), 1_000)  # 23:00 on 19 October in Nairobi
    add_payout(conn, datetime(2026, 10, 19, 21, 30, tzinfo=UTC), 20)  # 00:30 on 20 October in Nairobi
    assert store.load_state(conn, FAM, now).outflow_today == Money(20, "KES")


def test_load_state_allowance_this_month_uses_the_familys_local_month(conn):
    # Bug caught: counting by the UTC month, so an allowance run just after local midnight on the 1st hit the old month's cap.
    now = datetime(2026, 10, 31, 21, 30, tzinfo=UTC)  # 00:30 on 1 November in Nairobi
    add_payout(conn, datetime(2026, 10, 31, 20, 0, tzinfo=UTC), 7, kind="allowance_run")  # still October in Nairobi
    add_payout(conn, datetime(2026, 10, 31, 21, 15, tzinfo=UTC), 300, kind="allowance_run")  # November in Nairobi
    assert store.load_state(conn, FAM, now).allowance_this_month == Money(300, "KES")


def test_load_state_totals_are_zero_in_the_home_currency_when_nothing_was_paid(conn):
    # Bug caught: None or a wrong-currency zero, which breaks the cap comparison in the rules.
    state = store.load_state(conn, FAM, NOW)
    assert state.outflow_today == Money(0, "KES")
    assert state.allowance_this_month == Money(0, "KES")


def test_load_state_never_includes_another_familys_rows(conn):
    # Bug caught: a missing family filter leaking another family's people, payees, shares, orders or money into the checker.
    state = store.load_state(conn, FAM, NOW)
    assert all(m.family_id == FAM for m in state.members)
    assert all(t.family_id == FAM for t in state.trusted_payees)
    assert all(s.family_id == FAM for s in state.shares)
    assert all(o.family_id == FAM for o in state.standing_orders)
    assert state.balances == ()


# --- quotes ----------------------------------------------------------------------------------


def saved_quote(conn):
    return store.save_quote(
        conn,
        family_id=FAM,
        provider="sim",
        source=Money(99000000, "KES"),
        target=Money(600000, "GBP"),
        rate=Decimal("165.00"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def test_a_saved_quote_reads_back_equal(conn):
    # Bug caught: a quote that loses its amounts, currencies, family or expiry on the way through the database.
    saved = saved_quote(conn)
    assert saved.id.startswith("q_")
    assert store.get_quote(conn, FAM, saved.id) == saved
    assert saved == Quote(saved.id, FAM, Money(99000000, "KES"), Money(600000, "GBP"), NOW + timedelta(minutes=15))


def test_a_quote_keeps_its_rate_as_exact_text(conn):
    # Bug caught: the rate going through a float and losing digits, or trailing zeros being dropped.
    saved = saved_quote(conn)
    row = conn.execute("SELECT provider, rate, created_at FROM quotes WHERE id = ?", (saved.id,)).fetchone()
    assert (row["provider"], row["rate"], row["created_at"]) == ("sim", "165.00", clock.iso(NOW))


def test_get_quote_returns_none_for_another_familys_quote(conn):
    # Bug caught: fetching a quote by id alone, so one family could pay using another family's quote.
    saved = saved_quote(conn)
    assert store.get_quote(conn, OTHER, saved.id) is None


def test_get_quote_returns_none_for_an_unknown_id(conn):
    # Bug caught: an unknown quote id raising instead of letting the caller say "quote not found".
    assert store.get_quote(conn, FAM, "q_nope") is None


# --- proposals -------------------------------------------------------------------------------


def test_the_proposal_round_trip_is_exact_and_keeps_the_digest():
    # Bug caught: a field changing type or value on reload, which would void the PIN approval (the digest would differ).
    p = full_proposal()
    restored = store.proposal_from_json(store.proposal_to_json(p))
    assert restored == p
    assert restored.digest() == p.digest()
    assert isinstance(restored.kind, Kind)


def test_the_round_trip_sample_sets_every_proposal_field():
    # Bug caught: a new Proposal field being added without the store saving it; this sample must grow with it.
    p = full_proposal()
    for f in fields(Proposal):
        assert getattr(p, f.name) != f.default, f"full_proposal() leaves {f.name} at its default"


def test_a_proposal_with_nothing_optional_round_trips():
    # Bug caught: None fields being written as "None" text, or an empty split coming back as a list.
    p = Proposal("prop_min", FAM, Kind.UNFREEZE, "m_amina")
    restored = store.proposal_from_json(store.proposal_to_json(p))
    assert restored == p
    assert restored.split == ()
    assert restored.digest() == p.digest()


def test_insert_proposal_stores_the_payload_digest_and_status(conn):
    # Bug caught: a stored digest that is not the proposal's own, so a later PIN approval binds to the wrong content.
    p = insert(conn, status="awaiting_pin")
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (p.id,)).fetchone()
    assert row["digest"] == p.digest()
    assert row["status"] == "awaiting_pin"
    assert (row["family_id"], row["kind"], row["requested_by"]) == (FAM, "split", "m_amina")
    assert store.proposal_from_json(row["payload_json"]) == p
    assert row["created_at"] == row["updated_at"] == clock.iso(NOW)


def test_insert_proposal_stores_every_rule_result(conn):
    # Bug caught: the decision log dropping a reason, a wait time or the warn flag the main payer must see.
    decision = Decision(
        (
            RuleResult("quote", Outcome.PASS, "ok"),
            RuleResult("new_payee_wait", Outcome.WAIT, "New payee.", until=NOW + timedelta(days=1)),
            RuleResult("trusted_payee", Outcome.BLOCK, "Details changed.", warn=True),
        )
    )
    p = insert(conn, decision=decision)
    stored = json.loads(conn.execute("SELECT decision_json FROM proposals WHERE id = ?", (p.id,)).fetchone()[0])
    assert stored == [
        {"rule": "quote", "outcome": "pass", "reason": "ok", "until": None, "warn": False},
        {
            "rule": "new_payee_wait",
            "outcome": "wait",
            "reason": "New payee.",
            "until": clock.iso(NOW + timedelta(days=1)),
            "warn": False,
        },
        {"rule": "trusted_payee", "outcome": "block", "reason": "Details changed.", "until": None, "warn": True},
    ]


def test_get_proposal_returns_the_proposal_and_its_row(conn):
    # Bug caught: returning only the row, so callers rebuild the proposal by hand and drift from the stored payload.
    p = insert(conn, status="waiting")
    proposal, row = store.get_proposal(conn, FAM, p.id)
    assert proposal == p
    assert row["status"] == "waiting"
    assert row["digest"] == p.digest()


def test_get_proposal_returns_none_for_another_familys_proposal(conn):
    # Bug caught: fetching a proposal by id alone, so one family could confirm or read another family's payment.
    p = insert(conn)
    assert store.get_proposal(conn, OTHER, p.id) is None
    assert store.get_proposal(conn, FAM, "prop_nope") is None


def test_update_proposal_changes_status_and_time_but_not_the_payload_or_digest(conn):
    # Bug caught: a status update rewriting what the PIN approved, or losing when the proposal was made.
    p = insert(conn, status="awaiting_pin")
    later = NOW + timedelta(minutes=5)
    store.update_proposal(conn, FAM, p.id, status="confirmed", now=later)
    _, row = store.get_proposal(conn, FAM, p.id)
    assert row["status"] == "confirmed"
    assert row["updated_at"] == clock.iso(later)
    assert row["created_at"] == clock.iso(NOW)
    assert row["digest"] == p.digest()


def test_update_proposal_keeps_the_decision_unless_a_new_one_is_given(conn):
    # Bug caught: every status change wiping the recorded reasons, or a new decision not being saved.
    first = Decision((RuleResult("quote", Outcome.PASS, "ok"),))
    p = insert(conn, decision=first)
    store.update_proposal(conn, FAM, p.id, status="confirmed", now=NOW)
    assert json.loads(store.get_proposal(conn, FAM, p.id)[1]["decision_json"])[0]["rule"] == "quote"
    store.update_proposal(
        conn,
        FAM,
        p.id,
        status="failed",
        decision=Decision((RuleResult("funds", Outcome.BLOCK, "Not enough."),)),
        now=NOW,
    )
    assert json.loads(store.get_proposal(conn, FAM, p.id)[1]["decision_json"])[0]["rule"] == "funds"


def test_update_proposal_of_another_family_raises_and_changes_nothing(conn):
    # Bug caught: a status update silently doing nothing (or hitting another family's row), so money state drifts unnoticed.
    p = insert(conn, status="awaiting_pin")
    with pytest.raises(LookupError):
        store.update_proposal(conn, OTHER, p.id, status="executed", now=NOW)
    assert store.get_proposal(conn, FAM, p.id)[1]["status"] == "awaiting_pin"


# --- fees and shares -------------------------------------------------------------------------


def new_fee(conn, *, family_id=FAM, due_date="2026-12-01", at=NOW):
    return store.insert_fee(
        conn,
        family_id=family_id,
        payee=WESTBRIDGE,
        amount=Money(600000, "GBP"),
        due_date=due_date,
        invoice_ref="INV-2026-14",
        now=at,
    )


def test_insert_fee_stores_the_claimed_payee_amount_and_dates(conn):
    # Bug caught: fee details lost or mangled, e.g. the payee's account details not stored as JSON the checker can rebuild.
    fee_id = new_fee(conn)
    assert fee_id.startswith("fee_")
    row = store.get_fee(conn, FAM, fee_id)
    assert (row["payee_name"], row["amount_minor"], row["currency"]) == ("University of Westbridge", 600000, "GBP")
    assert Payee.of(row["payee_name"], **json.loads(row["payee_details_json"])) == WESTBRIDGE
    assert (row["due_date"], row["invoice_ref"], row["status"]) == ("2026-12-01", "INV-2026-14", "open")
    assert row["created_at"] == clock.iso(NOW)


@pytest.mark.parametrize("missing", [None, ""])
def test_a_fee_with_no_due_date_stores_null(conn, missing):
    # Bug caught: an empty string stored as a due date, which later date parsing would crash on.
    assert store.get_fee(conn, FAM, new_fee(conn, due_date=missing))["due_date"] is None


def test_a_fee_with_a_bad_due_date_is_refused_and_not_stored(conn):
    # Bug caught: free text such as "next Friday" saved in the due_date column.
    before = len(store.fees(conn, FAM))
    with pytest.raises(ValueError):
        new_fee(conn, due_date="next Friday")
    assert len(store.fees(conn, FAM)) == before


def test_get_fee_returns_none_for_another_familys_fee(conn):
    # Bug caught: fetching a fee by id alone, so one family could see or pay another family's invoice.
    assert store.get_fee(conn, FAM, "fee_other") is None
    assert store.get_fee(conn, OTHER, "fee_other")["amount_minor"] == 999


def test_fees_lists_only_this_familys_fees_oldest_first(conn):
    # Bug caught: a missing family filter, or an order that changes between runs.
    later = new_fee(conn, at=NOW + timedelta(days=1))
    earlier = new_fee(conn, at=NOW - timedelta(days=1))
    assert [r["id"] for r in store.fees(conn, FAM)] == [earlier, "fee_1", later]
    assert [r["id"] for r in store.fees(conn, OTHER)] == ["fee_other"]


def test_shares_for_fee_returns_the_fees_shares_for_this_family_only(conn):
    # Bug caught: asking with another family's fee id and getting that family's shares back.
    assert [r["id"] for r in store.shares_for_fee(conn, FAM, "fee_1")] == ["sh_baraka"]
    assert store.shares_for_fee(conn, FAM, "fee_other") == []
    assert store.shares_for_fee(conn, OTHER, "fee_1") == []


# --- outbox ----------------------------------------------------------------------------------


def outbox_rows(conn):
    return conn.execute(
        "SELECT id, member_id, channel, text, created_at, delivered_at FROM outbox ORDER BY id"
    ).fetchall()


def test_notify_writes_web_and_whatsapp_rows_for_a_member_with_a_whatsapp_identity(conn):
    # Bug caught: a WhatsApp user getting nothing on the web page (or the reverse), or a duplicate row for the web identity.
    ids = outbox.notify(conn, "m_amina", "Confirm with your PIN.", NOW)
    rows = outbox_rows(conn)
    assert [r["id"] for r in rows] == ids
    assert sorted(r["channel"] for r in rows) == ["web", "whatsapp"]
    assert {(r["member_id"], r["text"], r["created_at"], r["delivered_at"]) for r in rows} == {
        ("m_amina", "Confirm with your PIN.", clock.iso(NOW), None)
    }


def test_notify_writes_a_web_row_even_when_the_member_has_no_web_identity(conn):
    # Bug caught: a member who only uses WhatsApp never seeing the message in the web inbox.
    add_member(conn, "m_only_wa", FAM, "relative", "Only WhatsApp")
    add_identity(conn, "whatsapp", "whatsapp:+254700000999", "m_only_wa")
    outbox.notify(conn, "m_only_wa", "Hello", NOW)
    assert sorted(r["channel"] for r in outbox_rows(conn)) == ["web", "whatsapp"]


def test_notify_writes_only_a_web_row_for_a_member_with_no_other_channel(conn):
    # Bug caught: messages being queued for a channel the member never used.
    ids = outbox.notify(conn, "m_baraka", "Hello", NOW)
    assert len(ids) == 1
    assert [r["channel"] for r in outbox_rows(conn)] == ["web"]


def test_notify_for_an_unknown_member_is_refused_and_writes_nothing(conn):
    # Bug caught: a message queued for a member who doesn't exist, with nobody told.
    with pytest.raises(sqlite3.IntegrityError):
        outbox.notify(conn, "m_nobody", "Hello", NOW)
    assert outbox_rows(conn) == []


def test_pending_returns_only_undelivered_rows_for_that_member_and_channel_oldest_first(conn):
    # Bug caught: a poller re-sending delivered messages, or reading another member's or channel's messages.
    outbox.notify(conn, "m_amina", "first", NOW)
    outbox.notify(conn, "m_amina", "second", NOW + timedelta(minutes=1))
    outbox.notify(conn, "m_baraka", "not for Amina", NOW)
    assert [r["text"] for r in outbox.pending(conn, "m_amina", "web")] == ["first", "second"]
    assert [r["text"] for r in outbox.pending(conn, "m_amina", "whatsapp")] == ["first", "second"]
    assert outbox.pending(conn, "m_amina", "sms") == []


def test_mark_delivered_removes_only_the_given_rows_from_pending(conn):
    # Bug caught: marking everything delivered, or leaving the rows pending so they are sent again.
    outbox.notify(conn, "m_amina", "first", NOW)
    outbox.notify(conn, "m_amina", "second", NOW)
    first, second = outbox.pending(conn, "m_amina", "web")
    later = NOW + timedelta(minutes=2)
    outbox.mark_delivered(conn, [first["id"]], later)
    assert [r["id"] for r in outbox.pending(conn, "m_amina", "web")] == [second["id"]]
    assert len(outbox.pending(conn, "m_amina", "whatsapp")) == 2
    assert conn.execute("SELECT delivered_at FROM outbox WHERE id = ?", (first["id"],)).fetchone()[0] == clock.iso(
        later
    )


def test_marking_a_row_delivered_twice_keeps_the_first_time(conn):
    # Bug caught: a second delivery pass overwriting when the message really went out.
    (row_id,) = outbox.notify(conn, "m_baraka", "Hello", NOW)
    outbox.mark_delivered(conn, [row_id], NOW + timedelta(minutes=1))
    outbox.mark_delivered(conn, [row_id], NOW + timedelta(minutes=9))
    stored = conn.execute("SELECT delivered_at FROM outbox WHERE id = ?", (row_id,)).fetchone()[0]
    assert stored == clock.iso(NOW + timedelta(minutes=1))


def test_mark_delivered_with_no_ids_does_nothing(conn):
    # Bug caught: an empty id list producing invalid SQL (IN ()) when a poll finds nothing to send.
    outbox.mark_delivered(conn, [], NOW)
