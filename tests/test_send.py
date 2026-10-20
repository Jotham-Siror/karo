"""Sending money to a person abroad: adding a recipient, proposing a transfer, and paying it out.
Every test names the bug it would catch."""

import json
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from karo import approvals, audit, clock, db, executor, ledger, proposals, quotes, seed, store
from karo.agent.claude import FakeClaude, text_response
from karo.agent.loop import run_turn
from karo.agent.prompt import system_prompt
from karo.agent.tools import ToolContext, run_tool, tools_for
from karo.domain import Kind, Payee, Role
from karo.money import Money
from karo.providers.standin import StandInProvider

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
PIN = "4821"
PEPPER = "test-pepper"
BASE_URL = "https://karo.example"
RATES = {
    ("KES", "GBP"): Decimal("0.00578"),
    ("KES", "XAF"): Decimal("4.34"),
    ("KES", "UGX"): Decimal("31.6"),
}
# A made-up but valid Cameroon mobile number.
PAUL_PHONE = "+237671234567"
PAUL = {"country": "CM", "currency": "XAF", "method": "mtn_cm", "phone": PAUL_PHONE}


def xaf(amount: str) -> Money:
    return Money.parse(amount, "XAF")


class RecordingProvider(StandInProvider):
    """The stand-in, remembering what each payout was asked to do."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.asked: list[dict] = []

    def payout(self, **kwargs):
        self.asked.append(kwargs)
        return super().payout(**kwargs)


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    # Seeding hashes two PINs with Argon2, which is deliberately slow: do it once and copy the file per test.
    path = tmp_path_factory.mktemp("template") / "karo.db"
    connection = db.connect(path)
    db.init(connection)
    seed.seed_demo(connection, pin=PIN, pepper=PEPPER, now=NOW)
    connection.close()
    return path


@pytest.fixture
def conn(seeded_db, tmp_path):
    target = tmp_path / "karo.db"
    shutil.copyfile(seeded_db, target)
    connection = db.connect(target)
    yield connection
    connection.close()


@pytest.fixture
def provider() -> RecordingProvider:
    return RecordingProvider(rates=RATES, secret="standin-test-secret", base_url=BASE_URL)


def ctx_for(conn, provider, member_id: str = "m_amina", now: datetime = NOW) -> ToolContext:
    return ToolContext(conn, provider, FAM, store.member(conn, FAM, member_id), now, BASE_URL)


def save_recipient(
    conn, name: str = "Paul Mbarga", details: dict | None = None, *, since: datetime = NOW - timedelta(days=30)
) -> str:
    payee_id = db.new_id("tp")
    conn.execute(
        "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) "
        "VALUES (?, ?, ?, ?, ?, 'added')",
        (payee_id, FAM, name, json.dumps(details or PAUL, sort_keys=True), clock.iso(since)),
    )
    return payee_id


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def texts(conn, member_id: str = "m_amina") -> list[str]:
    return [r["text"] for r in conn.execute("SELECT text FROM outbox WHERE member_id = ? ORDER BY id", (member_id,))]


def events(conn, name: str) -> list[dict]:
    return [e for e in audit.events(conn, FAM) if e["event"] == name]


def confirm(conn, now: datetime = NOW) -> str:
    """Follows the newest confirm link in the main payer's outbox, as she would, and enters the PIN."""
    row = conn.execute(
        "SELECT text FROM outbox WHERE member_id = 'm_amina' AND text LIKE '%/confirm/%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    token = re.search(r"/confirm/(\S+)", row["text"]).group(1)
    result = approvals.confirm(conn, token=token, pin=PIN, pepper=PEPPER, now=now)
    assert result.status == "confirmed"
    return result.proposal_id


def run(conn, provider, proposal_id: str, *, now: datetime = NOW):
    return executor.execute(conn, provider, family_id=FAM, proposal_id=proposal_id, now=now, base_url=BASE_URL)


def propose_send(conn, provider, *, name: str = "Paul Mbarga", amount: str = "5000", now: datetime = NOW):
    """Proposes a send as the tool does: a fresh quote for the amount, and the payee as it is stored."""
    target = xaf(amount)
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=target, now=now)
    stored = next(t.payee for t in store.added_payees(conn, FAM) if t.payee.name == name)
    return proposals.propose(
        conn,
        family_id=FAM,
        actor=store.member(conn, FAM, "m_amina"),
        kind=Kind.SEND,
        now=now,
        base_url=BASE_URL,
        amount=target,
        payee=stored,
        quote_id=quote.id,
    )


# --- the payout ---------------------------------------------------------------------------------


def test_a_confirmed_send_pays_the_saved_recipient_and_the_receipt_says_simulated(conn, provider):
    # Bug caught: a send rejected by the payouts table (kind not allowed) or paid with no receipt.
    save_recipient(conn)
    assert propose_send(conn, provider).status == "awaiting_pin"
    outcome = run(conn, provider, confirm(conn))
    assert outcome.status == "executed"
    assert outcome.message.startswith("Paid XAF 5,000 to Paul Mbarga (cost KES 1,152.08).")
    assert "(simulated)" in outcome.message
    row = conn.execute("SELECT * FROM payouts").fetchone()
    assert (row["kind"], row["status"], row["amount_minor"], row["currency"]) == ("send", "succeeded", 5000, "XAF")


def test_a_send_pays_the_stored_details_with_the_transfer_narration(conn, provider):
    # Bug caught: the provider told to pay the wrong number, or the bank statement narration being the fee's.
    save_recipient(conn)
    propose_send(conn, provider)
    run(conn, provider, confirm(conn))
    (asked,) = provider.asked
    assert asked["narration"] == "Transfer to Paul Mbarga"
    assert dict(asked["payee"].details) == PAUL
    assert asked["amount"] == xaf("5000")


def test_a_send_takes_the_cost_from_the_family_balance_and_counts_in_todays_outflow(conn, provider):
    # Bug caught: a send that moves money without a ledger debit, so the daily cap never sees it.
    save_recipient(conn)
    before = ledger.balance(conn, FAM, "KES")
    propose_send(conn, provider)
    run(conn, provider, confirm(conn))
    cost = Money(115208, "KES")
    assert ledger.balance(conn, FAM, "KES") == before - cost
    assert store.load_state(conn, FAM, NOW).outflow_today == cost


def test_the_pin_page_summary_names_the_amount_the_recipient_and_the_cost(conn, provider):
    # Bug caught: a PIN request that does not say what is being sent, to whom, or what it costs.
    save_recipient(conn)
    propose_send(conn, provider)
    (message,) = texts(conn)
    assert message.startswith("Send XAF 5,000 to Paul Mbarga (costs KES 1,152.08)")


# --- which tools who gets -------------------------------------------------------------------------


def test_only_the_main_payer_is_offered_the_sending_tools():
    # Bug caught: a relative or the student offered a tool that can start a payment abroad.
    offered = {role: {t["name"] for t in tools_for(role)} for role in Role}
    assert {"add_recipient", "propose_transfer"} <= offered[Role.MAIN_PAYER]
    assert not ({"add_recipient", "propose_transfer"} & (offered[Role.RELATIVE] | offered[Role.STUDENT]))


def test_a_relative_calling_the_sending_tools_anyway_is_refused_before_anything_is_stored(conn, provider):
    # Bug caught: the role check living only in the tool list, so a made-up call from a relative still runs.
    save_recipient(conn)
    relative = ctx_for(conn, provider, "m_baraka")
    transfer_input = {"recipient": "Paul Mbarga", "amount": "5000", "currency": "XAF"}
    adding_input = {
        "name": "Eve",
        "country": "CM",
        "method": "mtn_cm",
        "details": [{"field": "phone", "value": PAUL_PHONE}],
    }
    assert run_tool("propose_transfer", transfer_input, relative) == {"error": "Only the main payer can do that."}
    assert run_tool("add_recipient", adding_input, relative) == {"error": "Only the main payer can do that."}
    assert (count(conn, "proposals"), count(conn, "quotes"), texts(conn)) == (0, 0, [])


# --- add_recipient --------------------------------------------------------------------------------


def add_recipient(conn, provider, **changes) -> dict:
    args = {
        "name": "Paul Mbarga",
        "country": "CM",
        "method": "mtn_cm",
        "details": [{"field": "phone", "value": PAUL_PHONE}],
    } | changes
    return run_tool("add_recipient", args, ctx_for(conn, provider))


def saved_names(conn) -> list[str]:
    return [t.payee.name for t in store.added_payees(conn, FAM)]


def test_a_new_recipient_is_held_for_the_pin_and_is_not_trusted_yet(conn, provider):
    # Bug caught: a recipient trusted the moment the chat names them, before the main payer has confirmed.
    result = add_recipient(conn, provider)
    assert result["status"] == "awaiting_pin"
    assert saved_names(conn) == ["Jabali Otieno"]
    (row,) = conn.execute("SELECT kind FROM proposals").fetchall()
    assert row["kind"] == "add_payee"


def test_the_pin_request_shows_every_detail_and_the_ai_never_sees_the_link(conn, provider):
    # Bug caught: the main payer approving a number she was never shown, or the link leaking into the tool result.
    result = add_recipient(conn, provider)
    assert "/confirm/" not in json.dumps(result) and "http" not in json.dumps(result)
    (message,) = texts(conn)
    shown = f"Add Paul Mbarga to the trusted payees (country CM, currency XAF, method mtn_cm, phone {PAUL_PHONE})"
    assert message.startswith(shown)
    assert "/confirm/" in message


def test_confirming_adds_the_recipient_with_the_details_the_pin_showed(conn, provider):
    # Bug caught: the stored recipient differing from what was proposed, or not stored as an added payee.
    add_recipient(conn, provider)
    outcome = run(conn, provider, confirm(conn))
    assert outcome.status == "executed"
    row = conn.execute("SELECT * FROM trusted_payees WHERE name = 'Paul Mbarga'").fetchone()
    assert row["source"] == "added" and row["family_id"] == FAM
    assert json.loads(row["details_json"]) == PAUL


def test_the_result_tells_the_ai_about_the_wait_on_larger_sends_to_a_new_recipient(conn, provider):
    # Bug caught: the main payer surprised by a held payment because nobody mentioned the new-recipient wait.
    result = add_recipient(conn, provider)
    assert "24 hours" in result["message"]
    assert "KES 34,600" in result["message"]


def test_a_phone_number_is_stored_in_international_form(conn, provider):
    # Bug caught: a local number saved as typed, so the payout goes to a number that does not exist.
    add_recipient(conn, provider, details=[{"field": "phone", "value": "6 71 23 45 67"}])
    run(conn, provider, confirm(conn))
    row = conn.execute("SELECT details_json FROM trusted_payees WHERE name = 'Paul Mbarga'").fetchone()
    assert json.loads(row["details_json"])["phone"] == PAUL_PHONE


def test_a_number_from_another_country_is_refused(conn, provider):
    # Bug caught: a Kenyan number accepted as a Cameroon mobile money account.
    result = add_recipient(conn, provider, details=[{"field": "phone", "value": "+254712345678"}])
    assert set(result) == {"error"} and "Cameroon" in result["error"]
    assert count(conn, "proposals") == 0


@pytest.mark.parametrize("value", ["", "abc", "0712"])
def test_a_number_that_is_not_a_phone_number_is_refused(conn, provider, value):
    # Bug caught: garbage saved as a payout number.
    result = add_recipient(conn, provider, details=[{"field": "phone", "value": value}])
    assert set(result) == {"error"}
    assert count(conn, "proposals") == 0


def test_a_country_karo_cannot_send_to_is_an_error_that_lists_the_ones_it_can(conn, provider):
    # Bug caught: a recipient saved in a country with no settings, so no payout could ever be priced.
    result = add_recipient(conn, provider, country="ZZ")
    assert "CM" in result["error"] and "GB" in result["error"]
    assert count(conn, "proposals") == 0


def test_a_method_the_country_does_not_list_is_an_error_that_lists_its_methods(conn, provider):
    # Bug caught: M-Pesa accepted for a number in Cameroon.
    result = add_recipient(conn, provider, method="mpesa")
    assert "mtn_cm" in result["error"] and "orange_cm" in result["error"]
    assert count(conn, "proposals") == 0


def test_the_country_and_method_may_be_written_in_any_case(conn, provider):
    # Bug caught: "cm" or "MTN_CM" refused, or saved in a spelling the payout cannot match.
    assert add_recipient(conn, provider, country=" cm ", method=" MTN_CM ")["status"] == "awaiting_pin"
    run(conn, provider, confirm(conn))
    row = conn.execute("SELECT details_json FROM trusted_payees WHERE name = 'Paul Mbarga'").fetchone()
    assert json.loads(row["details_json"]) == PAUL


def test_a_missing_required_detail_is_named(conn, provider):
    # Bug caught: a recipient saved with no way to pay them.
    result = add_recipient(conn, provider, details=[])
    assert "phone" in result["error"]
    assert count(conn, "proposals") == 0


def test_a_detail_the_country_does_not_use_is_refused(conn, provider):
    # Bug caught: stray bank details saved on a mobile money recipient.
    extra = [{"field": "phone", "value": PAUL_PHONE}, {"field": "account_number", "value": "123456"}]
    result = add_recipient(conn, provider, details=extra)
    assert "account_number" in result["error"]
    assert count(conn, "proposals") == 0


def test_the_same_detail_twice_is_refused(conn, provider):
    # Bug caught: two phone numbers given and one silently dropped.
    twice = [{"field": "phone", "value": PAUL_PHONE}, {"field": "phone", "value": "+237699999999"}]
    assert "twice" in add_recipient(conn, provider, details=twice)["error"]


def test_a_uk_bank_recipient_needs_a_sort_code_and_an_account_number(conn, provider):
    # Bug caught: payout details hard-coded to a phone number, so a bank recipient could not be added.
    bank = [{"field": "sort_code", "value": "20-40-60"}, {"field": "account_number", "value": "12345678"}]
    result = add_recipient(conn, provider, name="Sam Ford", country="GB", method="bank", details=bank)
    assert result["status"] == "awaiting_pin"
    run(conn, provider, confirm(conn))
    row = conn.execute("SELECT details_json FROM trusted_payees WHERE name = 'Sam Ford'").fetchone()
    assert json.loads(row["details_json"]) == {
        "country": "GB",
        "currency": "GBP",
        "method": "bank",
        "sort_code": "20-40-60",
        "account_number": "12345678",
    }


def test_a_name_already_on_the_trusted_list_is_refused_in_any_case(conn, provider):
    # Bug caught: two payees with one name, so "send to Paul" could mean either and a fake copy could hide.
    save_recipient(conn)
    result = add_recipient(conn, provider, name="paul mbarga", details=[{"field": "phone", "value": "+237699999999"}])
    assert set(result) == {"error"} and "already" in result["error"]
    assert count(conn, "proposals") == 0


def test_a_recipient_named_like_an_institution_is_refused(conn, provider):
    # Bug caught: a recipient added under the university's name, which would be mistaken for it on an invoice.
    result = add_recipient(conn, provider, name="University of Westbridge")
    assert "already" in result["error"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 81])
def test_an_empty_or_very_long_name_is_refused(conn, provider, name):
    # Bug caught: a blank recipient, or a name long enough to push the details off the PIN page.
    assert set(add_recipient(conn, provider, name=name)) == {"error"}
    assert count(conn, "proposals") == 0


@pytest.mark.parametrize(
    "name",
    ["Paul Mbarga +237 6 99 99 99 99", "Paul\n+237699999999", "Paul 2", "Paul (friend)", "Paul_Mbarga", "Paul\tMbarga"],
    ids=["phone_number", "newline", "digit", "brackets", "underscore", "tab"],
)
def test_a_name_with_anything_but_letters_spaces_and_dot_apostrophe_hyphen_is_refused(conn, provider, name):
    # Bug caught: a name that carries a fake phone number or line next to the real one on the PIN message.
    result = add_recipient(conn, provider, name=name)
    assert set(result) == {"error"} and "letters" in result["error"]
    assert (count(conn, "proposals"), texts(conn)) == (0, [])


@pytest.mark.parametrize("name", ["N'Goné Ndiaye-Fall", "Dr. Jean-Luc Obiang", "李小龙", "U" * 80])
def test_real_names_in_any_script_up_to_80_characters_are_accepted(conn, provider, name):
    # Bug caught: the name rule refusing accents, apostrophes, hyphens or names not written in the Latin alphabet.
    assert add_recipient(conn, provider, name=name)["status"] == "awaiting_pin"


def test_a_name_that_differs_only_in_spaces_or_hyphens_counts_as_the_same_name(conn, provider):
    # Bug caught: "Paul-Mbarga" added beside "Paul Mbarga", defeating the one-name-one-payee check.
    save_recipient(conn)
    result = add_recipient(conn, provider, name="Paul-Mbarga", details=[{"field": "phone", "value": "+237699999999"}])
    assert "already" in result["error"]


# --- propose_transfer ------------------------------------------------------------------------------


def transfer(conn, provider, **changes) -> dict:
    args = {"recipient": "Paul Mbarga", "amount": "5000", "currency": "XAF"} | changes
    return run_tool("propose_transfer", args, ctx_for(conn, provider))


def test_a_transfer_to_a_saved_recipient_is_priced_and_held_for_the_pin(conn, provider):
    # Bug caught: a send proposed with no fresh quote, or to details other than the stored ones.
    save_recipient(conn)
    result = transfer(conn, provider)
    assert result["status"] == "awaiting_pin"
    assert "/confirm/" not in json.dumps(result) and "http" not in json.dumps(result)
    (row,) = conn.execute("SELECT kind, payload_json FROM proposals").fetchall()
    assert row["kind"] == "send"
    payload = json.loads(row["payload_json"])
    assert payload["quote"]["source"] == {"minor": 115208, "currency": "KES"}
    assert payload["quote"]["target"] == {"minor": 5000, "currency": "XAF"}
    assert count(conn, "quotes") == 1


def test_the_recipient_is_found_without_regard_to_case(conn, provider):
    # Bug caught: "paul mbarga" not finding "Paul Mbarga".
    save_recipient(conn)
    assert transfer(conn, provider, recipient="  PAUL mbarga ")["status"] == "awaiting_pin"


def test_a_name_nobody_saved_is_an_error_that_says_how_to_add_them(conn, provider):
    # Bug caught: a payment to a name only mentioned in chat.
    result = transfer(conn, provider, recipient="Eve Nkoulou")
    assert set(result) == {"error"} and "add_recipient" in result["error"]
    assert (count(conn, "proposals"), count(conn, "quotes")) == (0, 0)


def test_a_recipient_still_waiting_for_the_pin_cannot_be_paid(conn, provider):
    # Bug caught: a recipient treated as trusted while the ADD_PAYEE is still unconfirmed.
    add_recipient(conn, provider)
    result = transfer(conn, provider)
    assert set(result) == {"error"}
    assert count(conn, "payouts") == 0


def test_two_saved_recipients_with_one_name_ask_for_clarification(conn, provider):
    # Bug caught: money going to whichever of two same-named recipients comes first.
    save_recipient(conn)
    save_recipient(conn, details=PAUL | {"phone": "+237699999999"})
    result = transfer(conn, provider)
    assert set(result) == {"error"} and "more than one" in result["error"].lower()
    assert count(conn, "proposals") == 0


def test_an_institution_from_the_directory_is_not_a_recipient(conn, provider):
    # Bug caught: the send tool used to pay the university's account around the invoice checks.
    result = transfer(conn, provider, recipient="University of Westbridge", currency="GBP", amount="100.00")
    assert set(result) == {"error"}
    assert count(conn, "proposals") == 0


def test_another_familys_recipient_cannot_be_found(conn, provider):
    # Bug caught: a recipient lookup that is not limited to the asking family.
    conn.execute(
        "INSERT INTO trusted_payees (id, family_id, name, details_json, trusted_since, source) "
        "VALUES ('tp_other', 'fam_mwangi', 'Zed Other', ?, ?, 'added')",
        (json.dumps(PAUL), clock.iso(NOW - timedelta(days=30))),
    )
    assert set(transfer(conn, provider, recipient="Zed Other")) == {"error"}
    assert count(conn, "proposals") == 0


def test_an_amount_in_another_currency_than_the_recipients_is_an_error(conn, provider):
    # Bug caught: KES 5,000 sent to a recipient who is paid in XAF.
    save_recipient(conn)
    result = transfer(conn, provider, currency="KES")
    assert set(result) == {"error"} and "XAF" in result["error"]
    assert count(conn, "proposals") == 0


def test_a_recipient_with_no_currency_on_file_cannot_be_paid(conn, provider):
    # Bug caught: guessing the currency of an account that does not say.
    save_recipient(conn, details={"country": "CM", "phone": PAUL_PHONE})
    assert set(transfer(conn, provider)) == {"error"}
    assert count(conn, "proposals") == 0


@pytest.mark.parametrize("amount", ["five thousand", "5,000", "5000.50", "0", "-5"])
def test_an_amount_that_cannot_be_read_is_an_error(conn, provider, amount):
    # Bug caught: XAF 5000.50 stored as a fraction of a unit XAF does not have.
    save_recipient(conn)
    assert set(transfer(conn, provider, amount=amount)) == {"error"}
    assert count(conn, "proposals") == 0


def test_a_send_the_provider_cannot_price_is_an_error(conn, provider):
    # Bug caught: a proposal with no quote when the provider has no rate for the pair.
    save_recipient(conn, details=PAUL | {"currency": "RWF"})
    result = transfer(conn, provider, currency="RWF")
    assert set(result) == {"error"} and "RWF" in result["error"]
    assert count(conn, "proposals") == 0


def test_a_send_over_the_payment_limit_is_blocked_with_the_reason(conn, provider):
    # Bug caught: the tool valuing the send some way the caps do not see.
    save_recipient(conn)
    result = transfer(conn, provider, amount="50000000")
    assert result["status"] == "blocked"
    assert "per-payment limit" in result["message"]
    assert texts(conn) == []


def test_a_send_while_frozen_is_blocked(conn, provider):
    # Bug caught: a freeze that stops fees but not sends.
    save_recipient(conn)
    conn.execute("UPDATE families SET frozen = 1 WHERE id = ?", (FAM,))
    assert transfer(conn, provider)["status"] == "blocked"


# --- one send, one confirmation -------------------------------------------------------------------


def test_a_second_identical_send_while_the_first_waits_for_the_pin_is_refused(conn, provider):
    # Bug caught: "send it" said twice (or a model retry) giving two PIN links, and the family paying twice.
    save_recipient(conn)
    assert transfer(conn, provider)["status"] == "awaiting_pin"
    again = transfer(conn, provider)
    assert set(again) == {"error"} and "already waiting" in again["error"]
    assert (count(conn, "proposals"), count(conn, "quotes"), len(texts(conn))) == (1, 1, 1)


def test_a_different_amount_or_recipient_may_be_sent_while_another_send_waits(conn, provider):
    # Bug caught: the guard refusing any second send, so Amina could not pay two people or two amounts at once.
    save_recipient(conn)
    save_recipient(conn, name="Grace Ndong", details=PAUL | {"phone": "+237699999999"})
    transfer(conn, provider)
    assert transfer(conn, provider, amount="6000")["status"] == "awaiting_pin"
    assert transfer(conn, provider, recipient="Grace Ndong")["status"] == "awaiting_pin"


def test_the_same_send_may_be_made_again_once_the_first_has_run(conn, provider):
    # Bug caught: a send blocked for ever because an identical one was paid yesterday.
    save_recipient(conn)
    transfer(conn, provider)
    run(conn, provider, confirm(conn))
    assert transfer(conn, provider)["status"] == "awaiting_pin"


def test_the_same_send_may_be_made_again_once_the_first_ones_link_has_expired(conn, provider):
    # Bug caught: a send stuck for ever because the PIN link was never used and the proposal never leaves awaiting_pin.
    save_recipient(conn)
    transfer(conn, provider)
    later = ctx_for(conn, provider, now=NOW + timedelta(minutes=16))
    args = {"recipient": "Paul Mbarga", "amount": "5000", "currency": "XAF"}
    assert run_tool("propose_transfer", args, later)["status"] == "awaiting_pin"


# --- the prompt -----------------------------------------------------------------------------------


def prompt_for(conn, member_id: str) -> str:
    who = store.member(conn, FAM, member_id)
    return system_prompt(who, "Otieno family", who.role)


def test_the_main_payers_prompt_makes_a_new_recipient_go_through_add_recipient_and_the_pin(conn):
    # Bug caught: the AI paying phone or account details pasted in chat instead of adding the recipient first.
    prompt = prompt_for(conn, "m_amina")
    assert "cross-border" in prompt and "add_recipient" in prompt and "propose_transfer" in prompt
    assert "confirmed with the PIN before any money can go to them" in prompt
    assert "Never pay account or phone details pasted in chat" in prompt


def test_the_main_payers_prompt_has_the_ai_pass_on_the_wait_for_a_new_recipient(conn):
    # Bug caught: the main payer never told that larger sends to a newly added recipient are held.
    assert "wait" in prompt_for(conn, "m_amina") and "new recipient" in prompt_for(conn, "m_amina")


@pytest.mark.parametrize("member", ["m_baraka", "m_jabali"])
def test_a_relatives_and_the_students_prompts_do_not_mention_sending_to_recipients(conn, member):
    # Bug caught: telling someone who cannot move money about a tool they do not have.
    prompt = prompt_for(conn, member)
    assert "add_recipient" not in prompt and "propose_transfer" not in prompt


# --- the status note's tag guard ------------------------------------------------------------------


def test_a_huge_run_of_spaces_after_a_less_than_sign_is_defused_quickly(conn):
    # Bug caught: a tag-matching pattern that takes quadratic time, so one long message stalls the whole server.
    fake = FakeClaude([text_response("Hello.")])
    words = "<" + " " * 50_000 + "x and </ karo_status"
    started = time.perf_counter()
    run_turn(
        conn,
        fake,
        RecordingProvider(rates=RATES, secret="standin-test-secret", base_url=BASE_URL),
        member=store.member(conn, FAM, "m_amina"),
        channel="web",
        content=[{"type": "text", "text": words}],
        now=NOW,
        base_url=BASE_URL,
    )
    assert time.perf_counter() - started < 0.5
    sent = fake.requests[0]["messages"][-1]["content"][-1]["text"]
    assert sent.endswith("\\u003c/ karo_status") and sent.count("\\u003c") == 1  # the real tag is defused, the stray < is not
