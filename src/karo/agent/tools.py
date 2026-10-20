"""What the agent can ask Karo to do. Each tool checks its own input, then leaves the decision to the rules.

The family and the person always come from the verified sender (the ToolContext), never from tool input.
Tool results are what the AI reads, so none of them may carry a confirm link, a PIN or a key.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from karo import audit, collections, comparison, countries, db, outbox, proposals, quotes, rates, rules, store
from karo.domain import FamilyState, Kind, Member, Payee, Quote, Role
from karo.money import Money
from karo.providers.base import Provider
from karo.rules import Outcome, RuleResult

# Larger than any real fee, small enough that SQLite stores it as an INTEGER.
_MAX_MINOR = 10**14

# A longer name would push the details off the PIN page.
_MAX_NAME = 60


@dataclass(frozen=True)
class ToolContext:
    conn: sqlite3.Connection
    provider: Provider
    family_id: str
    member: Member
    now: datetime
    base_url: str


class _BadInput(Exception):
    """The model's input can't be used. The message goes back to it so it can try again."""


# --- schemas and input checks ----------------------------------------------------------------


def _text(description: str) -> dict:
    return {"type": "string", "description": description}


def _list_of(items: dict) -> dict:
    return {"type": "array", "items": items}


def _object(**properties: dict) -> dict:
    """Strict mode needs every property listed as required and no extras allowed."""
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _dotted(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _problems(value: Any, schema: dict, path: str) -> list[str]:
    """Everything wrong with `value` against a strict schema. The API's strict mode should prevent all of it,
    but the checks that keep one family's data from another cannot depend on that."""
    match schema["type"]:
        case "string":
            return [] if isinstance(value, str) else [f"{path} must be text."]
        case "array":
            if not isinstance(value, list):
                return [f"{path} must be a list."]
            return [p for i, item in enumerate(value) for p in _problems(item, schema["items"], f"{path}[{i}]")]
    if not isinstance(value, dict):
        return [f"{path or 'The input'} must be an object."]
    found = [f"Unknown field: {_dotted(path, key)}." for key in sorted(set(value) - set(schema["properties"]))]
    found += [f"Missing field: {_dotted(path, key)}." for key in schema["required"] if key not in value]
    for key, inner in schema["properties"].items():
        if key in value:
            found += _problems(value[key], inner, _dotted(path, key))
    return found


def _money(amount: str, currency: str) -> Money:
    try:
        money = Money.parse(amount.strip(), currency.strip().upper())
    except (ValueError, ArithmeticError) as exc:
        raise _BadInput(
            f"I can't read {amount!r} {currency!r} as an amount. "
            "Use plain digits like 6000.00 and a currency code like GBP."
        ) from exc
    if not 0 < money.minor <= _MAX_MINOR:
        raise _BadInput("The amount has to be more than zero, and a realistic size.")
    return money


# --- lookups shared by several tools ---------------------------------------------------------


def _fee(ctx: ToolContext, fee_id: str) -> sqlite3.Row:
    fee = store.get_fee(ctx.conn, ctx.family_id, fee_id.strip())
    if fee is None:
        raise _BadInput("That fee wasn't found. Use get_status to see the fees.")
    return fee


def _fee_amount(fee: sqlite3.Row) -> Money:
    return Money(fee["amount_minor"], fee["currency"])


def _member_named(ctx: ToolContext, name: str) -> Member:
    family = store.members(ctx.conn, ctx.family_id)
    matches = [m for m in family if m.name.casefold() == name.strip().casefold()]
    if len(matches) != 1:
        who = ", ".join(m.name for m in family)
        problem = "is more than one family member" if matches else "isn't a family member"
        raise _BadInput(f"{name!r} {problem}. The family is: {who}.")
    return matches[0]


def _new_quote(ctx: ToolContext, target: Money) -> Quote:
    home = store.family_config(ctx.conn, ctx.family_id).policy.home_currency
    try:
        return quotes.create_quote(
            ctx.conn, ctx.provider, family_id=ctx.family_id, source_currency=home, target=target, now=ctx.now
        )
    except ValueError as exc:  # the provider has no price for this pair of currencies
        raise _BadInput(str(exc)) from exc


def _proposed(ctx: ToolContext, kind: Kind, **fields: Any) -> dict:
    outcome = proposals.propose(
        ctx.conn,
        family_id=ctx.family_id,
        actor=ctx.member,
        kind=kind,
        now=ctx.now,
        base_url=ctx.base_url,
        **fields,
    )
    return {"status": outcome.status, "message": outcome.message}


# --- the tools -------------------------------------------------------------------------------


def _allowance_of(state: FamilyState) -> dict | None:
    order = next(iter(state.standing_orders), None)  # setting a new allowance switches the old one off
    if order is None:
        return None
    return {"payee": order.payee.name, "amount": str(order.amount), "day_of_month": order.day_of_month}


def _get_status(ctx: ToolContext, _args: dict) -> dict:
    state = store.load_state(ctx.conn, ctx.family_id, ctx.now)
    role = ctx.member.role
    fees = [
        {
            "fee_id": f["id"],
            "payee": f["payee_name"],
            "amount": str(_fee_amount(f)),
            "due": f["due_date"],
            "status": f["status"],
        }
        for f in store.fees(ctx.conn, ctx.family_id)
    ]
    if role is Role.STUDENT:
        return {"fees": fees, "allowance": _allowance_of(state)}
    names = {m.id: m.name for m in state.members}
    shares = [
        {"member": names[s.member_id], "amount": str(s.amount), "paid": str(s.paid)}
        for s in state.shares
        if role is Role.MAIN_PAYER or s.member_id == ctx.member.id
    ]
    status = {"fees": fees, "shares": shares, "frozen": state.frozen}
    if role is Role.RELATIVE:
        return status
    pending = [
        {"kind": p.kind.value, "status": row["status"], "summary": proposals.summary(p, state)}
        for p, row in store.open_proposals(ctx.conn, ctx.family_id)
    ]
    return status | {
        "balances": [str(b) for b in state.balances],
        "allowance": _allowance_of(state),
        "open_proposals": pending,
    }


def _record_invoice(ctx: ToolContext, args: dict) -> dict:
    details = {key: value.strip() for key, value in args["payee_details"].items() if value.strip()}
    payee = Payee.of(args["payee_name"].strip(), **details)
    amount = _money(args["amount"], args["currency"])
    account_currency = details.get("currency", amount.currency).upper()
    if account_currency != amount.currency:
        raise _BadInput(
            f"The invoice is in {amount.currency} but the payee's account is in {account_currency}. "
            "Check the invoice and ask the main payer."
        )
    due_date = args["due_date"].strip()
    if due_date:
        try:
            date.fromisoformat(due_date)
        except ValueError as exc:
            raise _BadInput("due_date has to look like 2026-11-30, or be an empty string.") from exc

    # One transaction for the checks and the insert, so a repeat sent at the same moment can't slip between them.
    with db.transaction(ctx.conn):
        return _file_invoice(ctx, payee, amount, due_date, args["invoice_ref"].strip())


def _file_invoice(ctx: ToolContext, payee: Payee, amount: Money, due_date: str, ref: str) -> dict:
    state = store.load_state(ctx.conn, ctx.family_id, ctx.now)
    verdict = rules.check_payee(payee, state)
    if verdict.outcome is Outcome.BLOCK:
        return _refuse_invoice(ctx, payee, amount, verdict)
    # After the payee check, so a fake copy of a real invoice is still caught and reported.
    earlier = _earlier_fees(ctx, state, payee, ref)
    paid = next((f for f in earlier if f["status"] == "paid"), None)
    if paid is not None:
        _audit_duplicate(ctx, paid, payee, ref, already_paid=True)
        return {"accepted": False, "reason": "This invoice has already been paid."}
    same = next((f for f in earlier if _fee_amount(f) == amount), None)
    if same is not None:
        _audit_duplicate(ctx, same, payee, ref, already_paid=False)
        return {
            "accepted": True,
            "fee_id": same["id"],
            "duplicate": True,
            "amount": str(amount),
            "payee": same["payee_name"],
        }
    fee_id = store.insert_fee(
        ctx.conn,
        family_id=ctx.family_id,
        payee=payee,
        amount=amount,
        due_date=due_date,
        invoice_ref=ref or None,
        now=ctx.now,
    )
    audit.record(
        ctx.conn,
        at=ctx.now,
        family_id=ctx.family_id,
        actor=ctx.member.id,
        event="invoice.recorded",
        data={"fee_id": fee_id, "payee": payee.name, "amount": str(amount)},
    )
    return {"accepted": True, "fee_id": fee_id, "amount": str(amount), "payee": payee.name}


def _refuse_invoice(ctx: ToolContext, payee: Payee, amount: Money, verdict: RuleResult) -> dict:
    audit.record(
        ctx.conn,
        at=ctx.now,
        family_id=ctx.family_id,
        actor=ctx.member.id,
        event="invoice.blocked",
        data={
            "payee": payee.name,
            "details": dict(payee.details),
            "amount": str(amount),
            "reason": verdict.reason,
            "warned": verdict.warn,
        },
    )
    if verdict.warn:
        main = store.main_payer(ctx.conn, ctx.family_id)
        outbox.notify(ctx.conn, main.id, f"Warning: {verdict.reason}", ctx.now)
    return {"accepted": False, "reason": verdict.reason}


def _earlier_fees(ctx: ToolContext, state: FamilyState, claim: Payee, ref: str) -> list[sqlite3.Row]:
    """This family's fees with the same invoice reference from the same trusted payee as `claim`.

    With no reference there is nothing to say two invoices are the same one, so none are returned."""
    if not ref:
        return []
    claimed, _ = rules.match_payee(claim, state.trusted_payees)
    earlier = []
    for fee in store.fees(ctx.conn, ctx.family_id):
        if (fee["invoice_ref"] or "").strip().casefold() != ref.casefold():
            continue
        owed, _ = rules.match_payee(
            Payee.of(fee["payee_name"], **json.loads(fee["payee_details_json"])), state.trusted_payees
        )
        if owed is not None and claimed is not None and owed.id == claimed.id:
            earlier.append(fee)
    return earlier


def _audit_duplicate(ctx: ToolContext, fee: sqlite3.Row, payee: Payee, ref: str, *, already_paid: bool) -> None:
    audit.record(
        ctx.conn,
        at=ctx.now,
        family_id=ctx.family_id,
        actor=ctx.member.id,
        event="invoice.duplicate",
        data={"fee_id": fee["id"], "payee": payee.name, "invoice_ref": ref, "already_paid": already_paid},
    )


def _propose_split(ctx: ToolContext, args: dict) -> dict:
    fee = _fee(ctx, args["fee_id"])
    split = tuple(
        (_member_named(ctx, share["member_name"]).id, _money(share["amount"], fee["currency"]))
        for share in args["shares"]
    )
    return _proposed(ctx, Kind.SPLIT, amount=_fee_amount(fee), fee_id=fee["id"], split=split)


def _get_quote(ctx: ToolContext, args: dict) -> dict:
    fee = _fee(ctx, args["fee_id"])
    quote = _new_quote(ctx, _fee_amount(fee))
    tz = store.family_config(ctx.conn, ctx.family_id).timezone
    return {
        "quote_id": quote.id,
        "you_pay": str(quote.source),
        "they_receive": str(quote.target),
        "expires_at": proposals.when_text(quote.expires_at, tz),
    }


def _compare_routes(ctx: ToolContext, args: dict) -> dict:
    fee = _fee(ctx, args["fee_id"])
    try:
        found = comparison.compare(
            ctx.conn, ctx.provider, family_id=ctx.family_id, target=_fee_amount(fee), now=ctx.now
        )
    except ValueError as exc:  # the provider has no price for this pair of currencies
        raise _BadInput(str(exc)) from exc
    result = {
        "quote_id": found.quote_id,
        "line": found.line,
        "options": [
            {
                "name": o.name,
                "you_pay": str(o.you_pay),
                "they_receive": str(o.they_receive),
                "speed": o.speed,
                "is_example": o.is_example,
            }
            for o in found.options
        ],
    }
    if found.markup_pct is not None:
        result["rate_source"] = rates.ATTRIBUTION
    return result


def _propose_fee_payment(ctx: ToolContext, args: dict) -> dict:
    fee = _fee(ctx, args["fee_id"])
    payee = Payee.of(fee["payee_name"], **json.loads(fee["payee_details_json"]))
    return _proposed(
        ctx,
        Kind.PAY_FEE,
        amount=_fee_amount(fee),
        payee=payee,
        quote_id=args["quote_id"].strip(),
        fee_id=fee["id"],
    )


def _students_account(ctx: ToolContext) -> Payee:
    """The account the family added for the student: a trusted payee they added whose name contains the student's."""
    students = [m for m in store.members(ctx.conn, ctx.family_id) if m.role is Role.STUDENT]
    if len(students) != 1:
        raise _BadInput("I can only set an allowance for a family with exactly one student.")
    student = students[0]
    matches = [
        t.payee
        for t in store.added_payees(ctx.conn, ctx.family_id)
        if student.name.casefold() in t.payee.name.casefold()
    ]
    if len(matches) != 1:
        raise _BadInput(
            f"I need exactly one account on file for {student.name}, and found {len(matches)}. "
            "Ask the main payer to add or tidy it."
        )
    return matches[0]


def _propose_allowance(ctx: ToolContext, args: dict) -> dict:
    amount = _money(args["amount"], args["currency"])
    payee = _students_account(ctx)
    account_currency = dict(payee.details).get("currency", amount.currency).upper()
    if account_currency != amount.currency:
        raise _BadInput(
            f"{payee.name}'s account is in {account_currency}, so the allowance has to be in {account_currency}."
        )
    quote_id = args["quote_id"].strip()
    if not quote_id:
        quote_id = _new_quote(ctx, amount).id
    else:
        # The rules value the allowance through its quote, so a quote for a smaller amount would hide a large one.
        quote = store.get_quote(ctx.conn, ctx.family_id, quote_id)
        if quote is not None and quote.target != amount:
            raise _BadInput(f"That quote is for {quote.target}, not {amount}.")
    return _proposed(ctx, Kind.ALLOWANCE_SET, amount=amount, payee=payee, quote_id=quote_id)


def _sendable() -> dict[str, countries.Country]:
    return {code: c for code, c in sorted(countries.load().items()) if c.payout_methods}


def _recipient_help() -> str:
    """The countries, methods and details from the settings, so the model has the ids and need not guess."""
    return " ".join(
        f"{code} ({c.name}, {c.currency}): methods {', '.join(f'{m.id} = {m.label}' for m in c.payout_methods)}; "
        f"details {', '.join(c.payout_fields)}."
        for code, c in _sendable().items()
    )


def _wait_text(wait: timedelta) -> str:
    minutes = int(wait.total_seconds()) // 60
    if minutes % 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'}"


def _recipient_details(code: str, country: countries.Country, pairs: list[dict]) -> dict[str, str]:
    given: dict[str, str] = {}
    for pair in pairs:
        field, value = pair["field"].strip().casefold(), pair["value"].strip()
        if field in given:
            raise _BadInput(f"{field} was given twice.")
        given[field] = value
    unknown = sorted(set(given) - set(country.payout_fields))
    missing = [f for f in country.payout_fields if not given.get(f)]
    if unknown or missing:
        needs = ", ".join(country.payout_fields)
        extra = f" Not used there: {', '.join(unknown)}." if unknown else ""
        raise _BadInput(f"Sending to {country.name} needs exactly these details: {needs}.{extra}")
    if "phone" in given:
        try:
            given["phone"] = countries.normalise_phone(given["phone"], code)
        except ValueError as exc:
            raise _BadInput(str(exc)) from exc
        if not countries.phone_is_in(given["phone"], code):
            raise _BadInput(f"{given['phone']} isn't a {country.name} number.")
    return given


def _add_recipient(ctx: ToolContext, args: dict) -> dict:
    name = args["name"].strip()
    if not name or len(name) > _MAX_NAME:
        raise _BadInput(f"Give the recipient's name, in {_MAX_NAME} characters or fewer.")
    code = args["country"].strip().upper()
    country = _sendable().get(code)
    if country is None:
        raise _BadInput(f"Karo can't send to {args['country']!r} yet. It can send to: {', '.join(_sendable())}.")
    method_id = args["method"].strip().casefold()
    if method_id not in {m.id for m in country.payout_methods}:
        ids = ", ".join(m.id for m in country.payout_methods)
        raise _BadInput(f"{args['method']!r} isn't a way to send to {country.name}. Use one of: {ids}.")
    details = _recipient_details(code, country, args["details"])
    # Every source: a recipient with an institution's name would be mistaken for it, and a repeat name makes "send to
    # Paul" ambiguous.
    trusted = store.load_state(ctx.conn, ctx.family_id, ctx.now).trusted_payees
    if any(t.payee.name.casefold() == name.casefold() for t in trusted):
        raise _BadInput(f"There is already a payee called {name!r}. Use a different name, such as one with a surname.")
    payee = Payee.of(name, country=code, currency=country.currency, method=method_id, **details)
    result = _proposed(ctx, Kind.ADD_PAYEE, payee=payee)
    if result["status"] == "awaiting_pin":
        policy = store.family_config(ctx.conn, ctx.family_id).policy
        result["message"] += (
            f" Once confirmed, any send above {policy.new_payee_threshold} to {name} has to wait "
            f"{_wait_text(policy.new_payee_wait)} after {name} is added. Tell the main payer."
        )
    return result


def _saved_recipient(ctx: ToolContext, name: str) -> Payee:
    """The one payee the family added itself that goes by this name. Nothing said in chat is paid, only this record."""
    saved = store.added_payees(ctx.conn, ctx.family_id)
    found = [t.payee for t in saved if t.payee.name.casefold() == name.strip().casefold()]
    if not found:
        known = ", ".join(t.payee.name for t in saved) or "none"
        raise _BadInput(
            f"There is no saved recipient called {name!r}. Saved: {known}. A new recipient has to be added with "
            "add_recipient and confirmed with the PIN before any money can go to them."
        )
    if len(found) > 1:
        raise _BadInput(f"More than one saved recipient is called {name!r}. Ask the main payer which one they mean.")
    return found[0]


def _propose_transfer(ctx: ToolContext, args: dict) -> dict:
    amount = _money(args["amount"], args["currency"])
    payee = _saved_recipient(ctx, args["recipient"])
    account_currency = dict(payee.details).get("currency", "").upper()
    if not account_currency:
        raise _BadInput(f"I don't have a currency on file for {payee.name}, so I can't send to them.")
    if account_currency != amount.currency:
        raise _BadInput(f"{payee.name} is paid in {account_currency}, so the amount has to be in {account_currency}.")
    quote = _new_quote(ctx, amount)
    return _proposed(ctx, Kind.SEND, amount=amount, payee=payee, quote_id=quote.id)


def _freeze(ctx: ToolContext, _args: dict) -> dict:
    message = proposals.freeze(ctx.conn, family_id=ctx.family_id, actor=ctx.member, now=ctx.now)
    return {"message": message}


def _request_unfreeze(ctx: ToolContext, _args: dict) -> dict:
    return _proposed(ctx, Kind.UNFREEZE)


def _send_reminders(ctx: ToolContext, args: dict) -> dict:
    fee = _fee(ctx, args["fee_id"])
    sent = collections.send_reminders(
        ctx.conn,
        ctx.provider,
        family_id=ctx.family_id,
        fee_id=fee["id"],
        now=ctx.now,
        base_url=ctx.base_url,
        actor=ctx.member.id,
    )
    if sent == 0:
        return {"reminders_sent": 0, "message": "Nobody needed a reminder: everyone has paid, or has no link yet."}
    return {"reminders_sent": sent, "message": f"Sent {sent} {'reminder' if sent == 1 else 'reminders'}."}


# --- the tool list ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    schema: dict
    run: Callable[[ToolContext, dict], dict]
    roles: frozenset[Role]
    # True for a tool that doesn't go through the rule checker, so run_tool itself must keep the other roles out.
    gated: bool = False


_EVERYONE = frozenset(Role)
_FAMILY_MEMBERS = frozenset({Role.MAIN_PAYER, Role.RELATIVE})
_MAIN_PAYER = frozenset({Role.MAIN_PAYER})

_AMOUNT = "Plain digits with a decimal point, like 3500.00. No commas and no currency symbol."

# The order is fixed: a changing tool list would break the prompt cache.
_TOOLS: tuple[_Tool, ...] = (
    _Tool(
        "get_status",
        "Look up where things stand: the fees (with their ids), what each relative owes and has paid, the balances, "
        "whether Karo is frozen, the monthly allowance, and anything waiting for a PIN. Call this before answering "
        "any question about money, and whenever you need a fee id. What comes back depends on who is asking.",
        _object(),
        _get_status,
        _EVERYONE,
    ),
    _Tool(
        "record_invoice",
        "Record an invoice the main payer has received, whether it came as text, a PDF or a photo. Call it once per "
        "invoice and copy the payee name, bank details, amount and dates exactly as written: never correct or fill in "
        "a detail yourself. Karo checks the payee against the family's trusted payees and refuses an invoice whose "
        "details have changed. If the invoice was recorded before, you get that fee back with duplicate: true: use it "
        "and do not record the invoice again. Use an empty string for anything that does not apply.",
        _object(
            payee_name=_text("The payee's name exactly as on the invoice, e.g. University of Westbridge."),
            payee_details=_object(
                country=_text("Two-letter country code of the bank account, e.g. GB."),
                currency=_text("Three-letter currency code of the account, e.g. GBP."),
                sort_code=_text("The sort code exactly as written, or an empty string."),
                account_number=_text("The account number exactly as written."),
            ),
            amount=_text(f"The amount due. {_AMOUNT}"),
            currency=_text("Three-letter currency code of the amount due, e.g. GBP."),
            due_date=_text("The due date as YYYY-MM-DD, or an empty string if there is none."),
            invoice_ref=_text("The invoice number or reference, or an empty string."),
        ),
        _record_invoice,
        _MAIN_PAYER,
        gated=True,
    ),
    _Tool(
        "propose_split",
        "Propose asking relatives to share a recorded fee between them. The shares must add up to the whole fee. "
        "Nothing happens until the main payer confirms with a PIN.",
        _object(
            fee_id=_text("The id of the fee, from record_invoice or get_status."),
            shares=_list_of(
                _object(
                    member_name=_text("The family member's first name, e.g. Baraka."),
                    amount=_text(f"What this person is asked to pay, in the fee's currency. {_AMOUNT}"),
                )
            ),
        ),
        _propose_split,
        _MAIN_PAYER,
    ),
    _Tool(
        "get_quote",
        "Get a plain price for paying a recorded fee in full: what the family pays in its home currency and what the "
        "payee receives. To pay a fee, call compare_routes instead. A quote only lasts a short time (see expires_at).",
        _object(fee_id=_text("The id of the fee, from record_invoice or get_status.")),
        _get_quote,
        _MAIN_PAYER,
        gated=True,
    ),
    _Tool(
        "compare_routes",
        "Price paying a recorded fee in full and compare it with other ways to send. Call it before "
        "propose_fee_payment. Karo can only send through its own provider, so that is always the pick: the other "
        "options are illustrative examples (is_example), not live quotes. Tell the main payer the line word for "
        "word, then propose with the quote_id. A quote only lasts a short time.",
        _object(fee_id=_text("The id of the fee, from record_invoice or get_status.")),
        _compare_routes,
        _MAIN_PAYER,
        gated=True,
    ),
    _Tool(
        "propose_fee_payment",
        "Propose paying a recorded fee in full, using the quote_id from compare_routes. Nothing moves until the main "
        "payer confirms with a PIN.",
        _object(
            fee_id=_text("The id of the fee, from record_invoice or get_status."),
            quote_id=_text("The quote_id returned by compare_routes for this fee."),
        ),
        _propose_fee_payment,
        _MAIN_PAYER,
    ),
    _Tool(
        "propose_allowance",
        "Propose setting the student's monthly allowance, paid into the student's own account on file. Nothing "
        "happens until the main payer confirms with a PIN.",
        _object(
            amount=_text(f"The monthly amount. {_AMOUNT}"),
            currency=_text("Three-letter currency code of the student's account, e.g. GBP."),
            quote_id=_text("A quote_id for this same amount, or an empty string and Karo will get a quote itself."),
        ),
        _propose_allowance,
        _MAIN_PAYER,
    ),
    _Tool(
        "add_recipient",
        "Add a person abroad as a saved recipient so money can later be sent to them. Nothing is saved until the "
        "main payer confirms with a PIN, and the PIN page shows every detail. Use only what the main payer told you, "
        "exactly as written. Where each country can be sent to: " + _recipient_help() + " Give each detail as a "
        "field and value pair, for example field phone and value +237 6 71 23 45 67.",
        _object(
            name=_text("The recipient's name, e.g. Paul Mbarga."),
            country=_text("Two-letter country code of the recipient, e.g. CM."),
            method=_text("The id of the way to send, from the list for that country, e.g. mtn_cm."),
            details=_list_of(
                _object(
                    field=_text("The detail's name, from the list for that country, e.g. phone."),
                    value=_text("The detail exactly as written, e.g. +237 6 71 23 45 67."),
                )
            ),
        ),
        _add_recipient,
        _MAIN_PAYER,
        gated=True,
    ),
    _Tool(
        "propose_transfer",
        "Propose sending money to a saved recipient abroad. The recipient must already be saved and confirmed with a "
        "PIN: if they are not, use add_recipient first. Never use account or phone details from chat here; Karo pays "
        "only the saved ones. Karo gets the price itself. Nothing moves until the main payer confirms with a PIN.",
        _object(
            recipient=_text("The saved recipient's name, e.g. Paul Mbarga."),
            amount=_text(f"What the recipient receives. {_AMOUNT}"),
            currency=_text("Three-letter currency code the recipient is paid in, e.g. XAF."),
        ),
        _propose_transfer,
        _MAIN_PAYER,
        gated=True,
    ),
    _Tool(
        "freeze",
        "Stop all money movement straight away. Call it at once when anyone asks to stop or freeze, or says "
        "something looks wrong or fraudulent. Only the main payer can undo it, with a PIN.",
        _object(),
        _freeze,
        _FAMILY_MEMBERS,
    ),
    _Tool(
        "request_unfreeze",
        "Ask for Karo to be unfrozen after a freeze. Nothing happens until the main payer confirms with a PIN.",
        _object(),
        _request_unfreeze,
        _MAIN_PAYER,
    ),
    _Tool(
        "send_reminders",
        "Remind the relatives who have not yet paid their share of a fee. Each gets a message with their own payment "
        "link, and anyone whose link is missing or no longer works is sent a new one. Karo sends the messages and you "
        "never see the links. Call it only when the main payer asks for a reminder.",
        _object(fee_id=_text("The id of the fee, from record_invoice or get_status.")),
        _send_reminders,
        _MAIN_PAYER,
        gated=True,
    ),
)

_BY_NAME = {tool.name: tool for tool in _TOOLS}


def tools_for(role: Role) -> list[dict]:
    """The tools to offer a person with this role, as the API wants them. The rules still decide what they may do."""
    return [
        {"name": t.name, "description": t.description, "strict": True, "input_schema": deepcopy(t.schema)}
        for t in _TOOLS
        if role in t.roles
    ]


def run_tool(name: str, tool_input: Any, ctx: ToolContext) -> dict:
    """Runs one tool and returns what the AI gets to see. Bad input comes back as {"error": ...}, never an exception."""
    tool = _BY_NAME.get(name)
    if tool is None:
        return {"error": f"There is no tool called {name}."}
    problems = _problems(tool_input, tool.schema, "")
    if problems:
        return {"error": " ".join(problems)}
    if tool.gated and ctx.member.role not in tool.roles:
        return {"error": "Only the main payer can do that."}
    try:
        return tool.run(ctx, tool_input)
    except _BadInput as bad:
        return {"error": str(bad)}
