"""PIN checks, lockout, confirm links and the confirm step. Every test names the bug it would catch."""

import hashlib
import hmac
import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from karo import approvals, audit, clock, db, rules, store
from karo.approvals import ConfirmResult, LinkInfo, PinResult
from karo.domain import CheckContext, Confirmation, Kind, Payee, Proposal, Stage
from karo.money import Money

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
OTHER = "fam_mwangi"
PIN = "4821"
PEPPER = "test-pepper"
BASE_URL = "https://karo.example"
LOCKOUT = timedelta(minutes=15)

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
# Hashed once: Argon2 is deliberately slow, and only the tests of set_pin itself need to pay for it again.
PIN_HASH = approvals.hash_pin(PIN, PEPPER)
WESTBRIDGE = Payee.of(
    "University of Westbridge", country="GB", currency="GBP", sort_code="20-40-60", account_number="41234567"
)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "karo.db")
    db.init(connection)
    for family_id in (FAM, OTHER):
        connection.execute(
            "INSERT INTO families (id, name, home_country, home_currency, timezone, policy_json) "
            "VALUES (?, ?, 'KE', 'KES', 'Africa/Nairobi', ?)",
            (family_id, f"Family {family_id}", json.dumps(POLICY)),
        )
    for member_id, family_id, role in (
        ("m_amina", FAM, "main_payer"),
        ("m_baraka", FAM, "relative"),
        ("m_wanjiru", OTHER, "main_payer"),
    ):
        connection.execute(
            "INSERT INTO members (id, family_id, role, name, country) VALUES (?, ?, ?, ?, 'KE')",
            (member_id, family_id, role, member_id),
        )
    connection.execute("UPDATE families SET pin_hash = ?", (PIN_HASH,))
    yield connection
    connection.close()


def verify(conn, pin=PIN, *, member="m_amina", family=FAM, now=NOW, pepper=PEPPER) -> PinResult:
    return approvals.verify_pin(conn, family, member, pin, pepper, now)


def lock(conn) -> None:
    for _ in range(3):
        verify(conn, "0000")


def failures(conn, family=FAM) -> int:
    return conn.execute("SELECT pin_failures FROM families WHERE id = ?", (family,)).fetchone()[0]


def events(conn, name, family=FAM) -> list[dict]:
    return [e for e in audit.events(conn, family) if e["event"] == name]


def audit_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]


def add_proposal(conn, *, status="awaiting_pin", proposal_id="prop_1", family=FAM) -> Proposal:
    proposal = Proposal(
        id=proposal_id,
        family_id=family,
        kind=Kind.PAY_FEE,
        requested_by="m_amina",
        amount=Money.parse("6000", "GBP"),
        payee=WESTBRIDGE,
    )
    store.insert_proposal(conn, proposal, status=status, decision=rules.Decision(()), now=NOW)
    return proposal


def link_url(conn, *, proposal_id="prop_1", member="m_amina", ttl=timedelta(minutes=30), base_url=BASE_URL) -> str:
    return approvals.create_link(
        conn, family_id=FAM, proposal_id=proposal_id, member_id=member, now=NOW, base_url=base_url, ttl=ttl
    )


def new_token(conn, **kwargs) -> str:
    return link_url(conn, **kwargs).rsplit("/", 1)[1]


def confirm(conn, token, pin=PIN, *, now=NOW) -> ConfirmResult:
    return approvals.confirm(conn, token=token, pin=pin, pepper=PEPPER, now=now)


def proposal_status(conn, proposal_id="prop_1") -> str:
    return conn.execute("SELECT status FROM proposals WHERE id = ?", (proposal_id,)).fetchone()[0]


def whole_database_text(conn) -> str:
    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    ]
    return "\n".join(str(tuple(row)) for table in tables for row in conn.execute(f"SELECT * FROM {table}"))


# --- hashing -----------------------------------------------------------------------------------


@pytest.mark.parametrize("pin", ["123", "1234567", "12a4", "", " 1234", "1234\n", "١٢٣٤"])
def test_hash_pin_refuses_anything_but_four_to_six_ascii_digits(pin):
    # A `^\d{4,6}$` check lets "1234\n" and non-ASCII digits through, so they would become real PINs.
    with pytest.raises(ValueError):
        approvals.hash_pin(pin, PEPPER)


@pytest.mark.parametrize("pin", ["1234", "12345", "123456"])
def test_hash_pin_accepts_four_to_six_digits(pin):
    # A length bound off by one would refuse a PIN the brief allows.
    assert approvals.hash_pin(pin, PEPPER).startswith("$argon2id$")


def test_stored_hash_is_argon2id_over_the_peppered_pin():
    # Hashing the bare PIN would let a stolen database be brute-forced offline without the pepper.
    stored = approvals.hash_pin("1234", PEPPER)
    hasher = PasswordHasher()
    with pytest.raises(VerifyMismatchError):
        hasher.verify(stored, "1234")
    assert hasher.verify(stored, hmac.new(PEPPER.encode(), b"1234", hashlib.sha256).hexdigest())


def test_right_pin_with_the_wrong_pepper_does_not_verify(conn):
    # A verify that ignored the pepper would accept the PIN even from someone holding only the database.
    assert not verify(conn, pepper="another-pepper").ok


def test_set_pin_replaces_the_old_pin(conn):
    # A set_pin that left the old hash in place would keep the PIN the family meant to retire.
    approvals.set_pin(conn, FAM, "9876", PEPPER, NOW)
    assert not verify(conn, PIN).ok
    assert verify(conn, "9876").ok


def test_set_pin_on_an_unknown_family_raises(conn):
    # A silent no-op would leave a family with no PIN and nobody told.
    with pytest.raises(LookupError):
        approvals.set_pin(conn, "fam_nobody", PIN, PEPPER, NOW)


def test_set_pin_is_audited(conn):
    # A PIN change that leaves no trace in the log.
    approvals.set_pin(conn, FAM, PIN, PEPPER, NOW)
    assert len(events(conn, "pin.set")) == 1


# --- verify_pin --------------------------------------------------------------------------------


def test_right_pin_verifies(conn):
    # The basic path: the main payer with the right PIN is let through, with every attempt still available.
    assert verify(conn) == PinResult(ok=True, locked_until=None, attempts_left=3)


def test_wrong_pin_counts_attempts_down(conn):
    # Failures that aren't saved between calls would give unlimited guesses.
    first, second = verify(conn, "0000"), verify(conn, "0000")
    assert (first.ok, first.attempts_left) == (False, 2)
    assert (second.ok, second.attempts_left) == (False, 1)
    assert failures(conn) == 2
    assert len(events(conn, "pin.failed")) == 2


def test_third_wrong_pin_locks_for_the_lockout_period(conn):
    # A lock that starts late, or lasts the wrong time, weakens the only real protection for a 4-digit PIN.
    verify(conn, "0000")
    verify(conn, "0000")
    result = verify(conn, "0000")
    assert result == PinResult(ok=False, locked_until=NOW + LOCKOUT, attempts_left=0)


def test_lock_is_stored_resets_failures_and_is_audited(conn):
    # The rule checker reads pin_locked_until from the family row; if it isn't stored the lock is invisible to it.
    lock(conn)
    row = store.family_row(conn, FAM)
    assert clock.parse(row["pin_locked_until"]) == NOW + LOCKOUT
    assert row["pin_failures"] == 0
    assert len(events(conn, "pin.locked")) == 1
    assert len(events(conn, "pin.failed")) == 2


def test_correct_pin_is_refused_while_locked(conn):
    # The lockout must hold against the right PIN, otherwise guessing just continues until it works.
    lock(conn)
    result = verify(conn, PIN, now=NOW + LOCKOUT - timedelta(seconds=1))
    assert result == PinResult(ok=False, locked_until=NOW + LOCKOUT, attempts_left=0)


def test_attempts_while_locked_are_not_counted_or_logged(conn):
    # Counting attempts during a lock would extend the lock for anyone who keeps trying.
    lock(conn)
    before = audit_count(conn)
    verify(conn, "0000", now=NOW + timedelta(minutes=5))
    assert failures(conn) == 0
    assert audit_count(conn) == before


def test_correct_pin_works_when_the_lockout_ends(conn):
    # A lock that never lifts would strand the family's money.
    lock(conn)
    assert verify(conn, PIN, now=NOW + LOCKOUT).ok


def test_success_clears_the_old_lock_time(conn):
    # A stale pin_locked_until would re-lock the family if the demo clock is ever reset to an earlier time.
    lock(conn)
    verify(conn, PIN, now=NOW + LOCKOUT)
    assert store.family_row(conn, FAM)["pin_locked_until"] is None


def test_wrong_pin_after_the_lock_starts_a_fresh_count(conn):
    # Failures carried over from before the lock would re-lock on the very next typo.
    lock(conn)
    result = verify(conn, "0000", now=NOW + LOCKOUT)
    assert result == PinResult(ok=False, locked_until=None, attempts_left=2)


def test_right_pin_resets_the_failure_count(conn):
    # Without a reset, three typos spread over a year would lock the mother out.
    verify(conn, "0000")
    verify(conn, "0000")
    assert verify(conn).ok
    assert failures(conn) == 0
    assert len(events(conn, "pin.ok")) == 1


def test_relative_cannot_verify_even_with_the_right_pin(conn):
    # Only the main payer's PIN may approve money.
    assert not verify(conn, PIN, member="m_baraka").ok


def test_relative_attempts_are_not_counted(conn):
    # A relative guessing would otherwise lock the main payer out of her own account.
    for _ in range(5):
        verify(conn, "0000", member="m_baraka")
    assert failures(conn) == 0
    assert verify(conn).ok


def test_another_familys_main_payer_cannot_verify(conn):
    # A member check that isn't scoped to the family would let any family's payer approve this one's money.
    assert not verify(conn, PIN, member="m_wanjiru").ok


def test_pin_appears_nowhere_in_the_audit_log_or_the_family_row(conn):
    # The PIN, or a wrong guess at it, written to the audit log or the family row.
    secret, guess = "739152", "804261"
    approvals.set_pin(conn, FAM, secret, PEPPER, NOW)
    for _ in range(3):
        verify(conn, guess)
    verify(conn, secret, now=NOW + LOCKOUT)

    logged = {e["event"] for e in audit.events(conn, FAM)}
    assert {"pin.set", "pin.failed", "pin.locked", "pin.ok"} <= logged
    text = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM audit"))
    text += "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM families"))
    assert secret not in text
    assert guess not in text


# --- links -------------------------------------------------------------------------------------


def test_link_url_ends_in_a_long_random_token(conn):
    # A short or guessable token, or a base URL slash that leaves "//confirm", breaks the link.
    add_proposal(conn)
    url = link_url(conn, base_url=BASE_URL + "/")
    prefix = f"{BASE_URL}/confirm/"
    assert url.startswith(prefix)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", url.removeprefix(prefix))


def test_each_link_gets_a_different_token(conn):
    # A fixed or repeating token would let one link open another proposal's PIN page.
    add_proposal(conn)
    assert new_token(conn) != new_token(conn)


def test_token_is_stored_only_as_a_hash_and_never_logged(conn):
    # A plain token in any table (audit and outbox included) would turn a database read into live approvals.
    add_proposal(conn)
    token = new_token(conn)
    confirm(conn, token, "0000")
    confirm(conn, token)
    text = whole_database_text(conn)
    assert token not in text
    assert hashlib.sha256(token.encode()).hexdigest() in text


def test_create_link_refuses_another_familys_proposal(conn):
    # A link that joins one family's payer to another family's proposal would cross the privacy wall.
    add_proposal(conn, proposal_id="prop_other", family=OTHER)
    with pytest.raises(LookupError):
        link_url(conn, proposal_id="prop_other")


def test_create_link_refuses_another_familys_member(conn):
    # The confirm link must be for someone in the proposal's own family.
    add_proposal(conn)
    with pytest.raises(LookupError):
        link_url(conn, member="m_wanjiru")


def test_open_link_names_the_proposal_member_and_family(conn):
    # The PIN page needs all three to show the right proposal and check the right person.
    add_proposal(conn)
    token = new_token(conn)
    assert approvals.open_link(conn, token, NOW) == LinkInfo("prop_1", "m_amina", FAM)


def test_unknown_token_opens_nothing(conn):
    # A guessed token must not open anything.
    add_proposal(conn)
    new_token(conn)
    assert approvals.open_link(conn, "not-a-real-token", NOW) is None


def test_link_expires_after_its_ttl(conn):
    # An expiry that is never checked, or off by a window, leaves old links live.
    add_proposal(conn)
    token = new_token(conn, ttl=timedelta(minutes=30))
    assert approvals.open_link(conn, token, NOW + timedelta(minutes=29, seconds=59)) is not None
    assert approvals.open_link(conn, token, NOW + timedelta(minutes=30)) is None


def test_any_link_live_sees_only_this_familys_unused_unexpired_links(conn):
    # Catches: counting a used, expired or other family's link, so bare digits are refused for no reason.
    assert not approvals.any_link_live(conn, FAM, NOW)
    add_proposal(conn)
    token = new_token(conn, ttl=timedelta(minutes=30))
    assert approvals.any_link_live(conn, FAM, NOW)
    assert not approvals.any_link_live(conn, OTHER, NOW)
    assert not approvals.any_link_live(conn, FAM, NOW + timedelta(minutes=30))
    assert confirm(conn, token).status == "confirmed"
    assert not approvals.any_link_live(conn, FAM, NOW)


def test_link_is_single_use(conn):
    # A link that survives a successful confirm could be replayed.
    add_proposal(conn)
    token = new_token(conn)
    assert confirm(conn, token).status == "confirmed"
    assert approvals.open_link(conn, token, NOW) is None
    assert confirm(conn, token) == ConfirmResult("invalid_link", None)


# --- confirm -----------------------------------------------------------------------------------


def test_confirm_with_the_right_pin_confirms_the_proposal(conn):
    # The happy path: right PIN, awaiting proposal, so it becomes confirmed.
    add_proposal(conn)
    assert confirm(conn, new_token(conn)) == ConfirmResult("confirmed", "prop_1")
    assert proposal_status(conn) == "confirmed"


def test_confirm_records_who_and_when(conn):
    # The confirmation row is what the rule checker trusts at execute time.
    add_proposal(conn)
    confirm(conn, new_token(conn), now=NOW + timedelta(minutes=2))
    row = conn.execute("SELECT * FROM confirmations").fetchone()
    assert (row["proposal_id"], row["member_id"]) == ("prop_1", "m_amina")
    assert clock.parse(row["at"]) == NOW + timedelta(minutes=2)


def test_confirm_writes_the_stored_digest_not_a_recomputed_one(conn):
    # Recomputing from the payload would bless a payload edited after the proposal was made.
    proposal = add_proposal(conn)
    payload = json.loads(conn.execute("SELECT payload_json FROM proposals").fetchone()[0])
    payload["amount"]["minor"] += 1
    conn.execute("UPDATE proposals SET payload_json = ?", (json.dumps(payload),))
    confirm(conn, new_token(conn))
    edited, _ = store.get_proposal(conn, FAM, "prop_1")
    written = approvals.confirmation_for(conn, FAM, "prop_1").digest
    assert written == proposal.digest()
    assert written != edited.digest()


def test_confirm_is_audited_without_the_token(conn):
    # The confirmation must be on the audit trail, but the token must not be.
    add_proposal(conn)
    token = new_token(conn)
    confirm(conn, token)
    (event,) = events(conn, "proposal.confirmed")
    assert event["actor"] == "m_amina"
    assert event["data"]["proposal_id"] == "prop_1"
    assert token not in json.dumps(event)


@pytest.mark.parametrize("status", ["blocked", "waiting", "confirmed", "executed", "failed"])
def test_confirm_refuses_a_proposal_that_is_not_awaiting_pin(conn, status):
    # Without the status check a PIN could approve a blocked or already-finished proposal.
    add_proposal(conn, status=status)
    assert confirm(conn, new_token(conn)) == ConfirmResult("not_awaiting", "prop_1")
    assert proposal_status(conn) == status
    assert conn.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


def test_not_awaiting_does_not_spend_a_pin_attempt(conn):
    # Checking the PIN before the status would let a stale link burn the mother's attempts.
    add_proposal(conn, status="executed")
    assert confirm(conn, new_token(conn), "0000").status == "not_awaiting"
    assert failures(conn) == 0


def test_wrong_pin_is_refused_and_leaves_the_link_usable(conn):
    # A typo must not burn the link; the mother tries again on the same page.
    add_proposal(conn)
    token = new_token(conn)
    assert confirm(conn, token, "0000") == ConfirmResult("wrong_pin", "prop_1")
    assert proposal_status(conn) == "awaiting_pin"
    assert confirm(conn, token).status == "confirmed"


def test_wrong_pins_through_confirm_lock_and_the_lock_holds(conn):
    # If confirm rolled back or raised on a wrong PIN the count would never persist and nothing would ever lock.
    add_proposal(conn)
    token = new_token(conn)
    assert confirm(conn, token, "0000").status == "wrong_pin"
    assert confirm(conn, token, "0000").status == "wrong_pin"
    assert confirm(conn, token, "0000").status == "locked"
    assert confirm(conn, token, PIN).status == "locked"
    assert proposal_status(conn) == "awaiting_pin"
    assert confirm(conn, token, PIN, now=NOW + LOCKOUT).status == "confirmed"


def test_unknown_link_is_invalid(conn):
    # An unknown token has no proposal to name and must not reach the PIN check.
    assert confirm(conn, "not-a-real-token") == ConfirmResult("invalid_link", None)


def test_expired_link_is_invalid_even_with_the_right_pin(conn):
    # An expired link must not confirm.
    add_proposal(conn)
    token = new_token(conn, ttl=timedelta(minutes=30))
    assert confirm(conn, token, now=NOW + timedelta(minutes=31)) == ConfirmResult("invalid_link", None)
    assert proposal_status(conn) == "awaiting_pin"


def test_link_made_out_to_a_relative_cannot_confirm(conn):
    # A relative holding a link must not approve money, even knowing the mother's PIN.
    add_proposal(conn)
    token = new_token(conn, member="m_baraka")
    assert confirm(conn, token).status == "wrong_pin"
    assert proposal_status(conn) == "awaiting_pin"


def test_a_confirm_that_fails_part_way_leaves_nothing_behind(conn):
    # The PIN check, the link and the status change must be one transaction, or a crash leaves a half-done approval.
    add_proposal(conn)
    token = new_token(conn)
    confirm(conn, token, "0000")
    confirm(conn, token, "0000")
    # A stray row makes the confirmation insert fail after the PIN has already been checked.
    conn.execute(
        "INSERT INTO confirmations (proposal_id, member_id, digest, at) VALUES ('prop_1', 'm_amina', 'x', ?)",
        (clock.iso(NOW),),
    )
    with pytest.raises(sqlite3.IntegrityError):
        confirm(conn, token)
    assert failures(conn) == 2
    assert events(conn, "pin.ok") == []
    assert proposal_status(conn) == "awaiting_pin"
    assert conn.execute("SELECT used_at FROM confirm_links").fetchone()[0] is None


def test_two_confirms_racing_on_one_link_confirm_exactly_once(conn, tmp_path):
    # Without one transaction both could pass the checks, and the loser would crash on the confirmations row.
    add_proposal(conn)
    token = new_token(conn)
    barrier = threading.Barrier(2)

    def attempt() -> str:
        own = db.connect(tmp_path / "karo.db")
        try:
            barrier.wait()
            return confirm(own, token).status
        finally:
            own.close()

    with ThreadPoolExecutor(2) as pool:
        statuses = sorted(f.result() for f in [pool.submit(attempt), pool.submit(attempt)])
    assert statuses == ["confirmed", "invalid_link"]


# --- confirmation_for --------------------------------------------------------------------------


def test_no_confirmation_before_the_pin_is_entered(conn):
    # A confirmation that exists before the PIN would let the checker approve unconfirmed money.
    add_proposal(conn)
    assert approvals.confirmation_for(conn, FAM, "prop_1") is None


def test_confirmation_for_rebuilds_the_domain_confirmation(conn):
    # The checker compares this value with the proposal, so every field must come back exactly.
    proposal = add_proposal(conn)
    confirm(conn, new_token(conn))
    assert approvals.confirmation_for(conn, FAM, "prop_1") == Confirmation("prop_1", "m_amina", proposal.digest(), NOW)


def test_confirmation_for_is_scoped_to_the_family(conn):
    # Asking with another family's id must not reveal this family's confirmation.
    add_proposal(conn)
    confirm(conn, new_token(conn))
    assert approvals.confirmation_for(conn, OTHER, "prop_1") is None


def test_the_rule_checker_accepts_the_confirmation_it_is_given(conn):
    # A confirmation stored in a form the checker rejects would mean no confirmed payment could ever run.
    add_proposal(conn)
    confirm(conn, new_token(conn))
    proposal, _ = store.get_proposal(conn, FAM, "prop_1")
    state = store.load_state(conn, FAM, NOW)
    context = CheckContext(NOW, Stage.EXECUTE, state.main_payer, approvals.confirmation_for(conn, FAM, "prop_1"))
    assert rules.confirmation(proposal, state, context).outcome is rules.Outcome.PASS


def notices(conn, member="m_amina") -> list[str]:
    rows = conn.execute("SELECT text FROM outbox WHERE member_id = ? ORDER BY id", (member,)).fetchall()
    return [r["text"] for r in rows]


def test_the_lockout_tells_the_main_payer_once_and_only_at_the_lock(conn):
    # Catches: a lock nobody hears about (a guesser could lock her out unseen), or a notice for each wrong try.
    verify(conn, "1357")
    verify(conn, "2468")
    assert notices(conn) == []
    verify(conn, "9753")
    assert len(notices(conn)) == 1
    assert notices(conn, "m_baraka") == []


def test_the_lockout_notice_gives_the_unlock_time_in_the_family_timezone_and_the_freeze_word(conn):
    # Catches: telling her UTC (09:15) instead of Nairobi time (12:15), or leaving out how to stop it.
    lock(conn)
    (notice,) = notices(conn)
    assert "12:15 EAT" in notice
    assert "09:15" not in notice
    assert notice.endswith("If this wasn't you, reply FREEZE.")


def test_the_lockout_notice_holds_no_digits_of_any_pin(conn):
    # Catches: the notice repeating a guessed PIN, or the real one, into the outbox that WhatsApp delivers.
    for guess in ("1357", "2468", "9753"):
        verify(conn, guess)
    (notice,) = notices(conn)
    assert not any(pin in notice for pin in ("1357", "2468", "9753", PIN))


def test_attempts_while_locked_send_no_further_notice(conn):
    # Catches: one notice per refused attempt, which would flood the main payer while someone keeps guessing.
    lock(conn)
    for minutes in range(1, 6):
        verify(conn, "0000", now=NOW + timedelta(minutes=minutes))
    verify(conn, PIN, now=NOW + timedelta(minutes=6))
    assert len(notices(conn)) == 1


def test_a_second_lock_sends_a_second_notice(conn):
    # Catches: telling her about the first lock only, so a later lock goes unannounced.
    lock(conn)
    later = NOW + LOCKOUT
    for _ in range(3):
        verify(conn, "0000", now=later)
    assert len(notices(conn)) == 2
