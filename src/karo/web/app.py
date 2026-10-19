"""The web door: login, chat, the PIN page, the log view and the demo controls.

One SQLite connection per request, never shared between requests; the scheduler's tick opens its own. Anything that
waits on the AI or the payment provider runs in a worker thread, so a slow call doesn't hold up the page for everyone
else.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import re
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware

from karo import approvals, audit, clock, db, executor, notices, outbox, proposals, scheduler, seed, store
from karo.agent.claude import Claude, ClaudeClient
from karo.config import Settings, load_settings
from karo.doors import base
from karo.doors.base import InboundMessage
from karo.domain import Member
from karo.money import Money
from karo.providers.base import Provider
from karo.providers.standin import StandInProvider

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=_HERE / "templates")  # autoescape is on by default

WEB = outbox.WEB
SESSION_SECONDS = 12 * 60 * 60
MAX_ADVANCE_HOURS = 720
TICK_SECONDS = 30
_HOURS = re.compile(r"[0-9]{1,4}")
_MESSAGE_KEY = re.compile(r"message([0-9]+)")
# Deep Chat adds the id; it only has to be unique per browser message. Anything else is ignored rather than trusted.
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")

EXPIRED = "This link has expired or was already used."
NOT_WAITING = "This request is no longer waiting for your PIN."
PROCESSING = "Your payment is being processed. You'll get a message when it's done."
ALREADY_RECEIVED = "I already have that message."
PIN_FORMAT_HELP = "Enter your PIN: 4 to 6 digits."

# The confirm link is a one-time credential in a URL: keep it out of caches, Referer headers and other people's frames.
_PIN_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action 'self'",
}

_RESULT_HEADINGS = {
    "executed": ("Done", "ok"),
    "pending": ("On its way", "info"),
    "waiting": ("Waiting", "info"),
    "failed": ("That didn't work", "bad"),
    "blocked": ("Not allowed", "bad"),
}

_CONFIRM_PATH = re.compile(r"(/confirm/)[^\s/?#\"]+")

router = APIRouter()


class HideConfirmTokens(logging.Filter):
    """The confirm token is a credential in the URL path, and uvicorn's access log prints the whole request line."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._mask(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._mask(arg) for arg in record.args)
        return True

    @staticmethod
    def _mask(value):
        return _CONFIRM_PATH.sub(r"\1<redacted>", value) if isinstance(value, str) else value


# One shared instance: Logger.addFilter ignores a filter it already has, so building the app twice adds it once.
_HIDE_TOKENS = HideConfirmTokens()


class LoginRequired(Exception):
    pass


class BadRequest(Exception):
    """The request can't be read. The message is safe to show the person."""


# --- the database, the session and the page helper -----------------------------------------------


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def require_member(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Member:
    address = request.session.get("member_address")
    member = store.member_for_identity(conn, WEB, address) if address else None
    if member is None:
        raise LoginRequired
    return member


def require_demo(request: Request) -> None:
    # The audit log covers every family, so it and the demo controls sit behind the demo password and nothing else.
    if not request.session.get("demo"):
        raise LoginRequired


def _login_required(request: Request, _: LoginRequired) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Please log in again."}, status_code=401)
    return RedirectResponse("/", status_code=303)


def _page(
    request: Request,
    conn: sqlite3.Connection,
    template: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **context,
) -> Response:
    offset = clock.offset(conn)
    context["demo_time"] = f"+{offset / 3600:g}h" if offset else None
    context["demo_nav"] = bool(request.session.get("demo"))
    return TEMPLATES.TemplateResponse(request, template, context, status_code=status_code, headers=headers)


def _notice(
    request: Request,
    conn: sqlite3.Connection,
    message: str,
    *,
    status_code: int = 200,
    heading: str = "",
    tone: str = "info",
) -> Response:
    return _page(
        request, conn, "result.html", status_code, _PIN_PAGE_HEADERS, heading=heading, message=message, tone=tone
    )


# --- logging in ----------------------------------------------------------------------------------


def _web_people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Every family on purpose: the demo login is how a judge picks which role to try.
    return conn.execute(
        "SELECT i.address, m.name, m.role, f.name AS family FROM identities i "
        "JOIN members m ON m.id = i.member_id JOIN families f ON f.id = m.family_id "
        "WHERE i.channel = ? ORDER BY m.rowid",
        (WEB,),
    ).fetchall()


@router.get("/")
def login_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, "login.html", people=_web_people(conn))


@router.post("/login")
def login(
    request: Request,
    password: str = Form(""),
    person: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    people = _web_people(conn)
    if not hmac.compare_digest(password.encode(), request.app.state.settings.demo_password.encode()):
        return _page(request, conn, "login.html", 401, people=people, error="That password isn't right.", chosen=person)
    if person not in {p["address"] for p in people}:
        return _page(request, conn, "login.html", 400, people=people, error="Choose who you are.")
    request.session.clear()
    request.session.update(member_address=person, demo=True)
    return RedirectResponse("/chat", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# --- the chat ------------------------------------------------------------------------------------


@router.get("/chat")
def chat_page(
    request: Request, member: Member = Depends(require_member), conn: sqlite3.Connection = Depends(get_conn)
):
    # Delivered messages (the confirm link, receipts) are not sent again, so the browser keeps the chat across reloads.
    # The key names the person and the database: a demo reset makes a new database, and its old messages stay behind.
    epoch = conn.execute("SELECT substr(hash, 1, 8) FROM audit ORDER BY seq LIMIT 1").fetchone()[0]
    return _page(request, conn, "chat.html", member=member, storage_key=f"karo-chat-{member.id}-{epoch}")


async def _read_chat_request(request: Request) -> tuple[str, str | None, list[tuple[str, bytes]]]:
    """Deep Chat sends JSON for text, and multipart form data once a file is attached."""
    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            keys = sorted((k for k in form.keys() if _MESSAGE_KEY.fullmatch(k)), key=lambda k: int(k[7:]))
            # A photo sent with no words has no message field at all, only the file.
            raw = form[keys[-1]] if keys else "{}"
            if not isinstance(raw, str):
                raise BadRequest("That request could not be read.")
            newest = json.loads(raw)
            files = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
            attachments = [(f.content_type or "", await f.read()) for f in files]
            client_id = form.get("message_id")
        else:
            body = await request.json()
            messages = body.get("messages") if isinstance(body, dict) else None
            if not isinstance(messages, list) or not messages:
                raise BadRequest("There was no message in that request.")
            newest, attachments, client_id = messages[-1], [], body.get("message_id")
    except ValueError as problem:  # unreadable JSON, which includes UnicodeDecodeError
        raise BadRequest("That request could not be read.") from problem
    text = newest.get("text") if isinstance(newest, dict) else None
    return text if isinstance(text, str) else "", client_id if isinstance(client_id, str) else None, attachments


@router.post("/api/chat", dependencies=[Depends(require_member)])
async def chat(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        text, client_id, attachments = await _read_chat_request(request)
    except BadRequest as problem:
        return JSONResponse({"error": str(problem)}, status_code=400)
    address = request.session["member_address"]
    # Scoped to the person, so one person's chosen id can't make Karo drop another person's message.
    message_id = f"{address}:{client_id}" if client_id and _CLIENT_ID.fullmatch(client_id) else db.new_id("web")
    message = InboundMessage(WEB, message_id, address, text, attachments)
    reply = await run_in_threadpool(_reply, conn, request.app.state, message)
    return {"text": reply or ALREADY_RECEIVED}


def _reply(conn: sqlite3.Connection, state, message: InboundMessage) -> str | None:
    return base.handle(
        conn,
        message,
        client=state.client,
        provider=state.provider,
        base_url=state.settings.public_base_url,
        now=clock.now(conn),
    )


@router.get("/api/inbox")
def inbox(member: Member = Depends(require_member), conn: sqlite3.Connection = Depends(get_conn)):
    # One transaction, so two polls at the same moment can't both be handed the same message.
    with db.transaction(conn):
        waiting = outbox.pending(conn, member.id, WEB)
        outbox.mark_delivered(conn, [row["id"] for row in waiting], clock.now(conn))
    return [{"text": row["text"]} for row in waiting]


# --- the PIN page --------------------------------------------------------------------------------


def _words(outcome: str, reason: str) -> str:
    label = {"pass": "Passed", "wait": "Has to wait", "block": "Blocked"}[outcome]
    return label if reason == "ok" else f"{label}. {reason}"


def _confirm_page(
    request: Request,
    conn: sqlite3.Connection,
    link: approvals.LinkInfo,
    now: datetime,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    p, row = store.get_proposal(conn, link.family_id, link.proposal_id)
    if row["status"] != "awaiting_pin":
        return _notice(request, conn, NOT_WAITING, status_code=409)
    config = store.family_config(conn, link.family_id)
    locked_until = store.family_row(conn, link.family_id)["pin_locked_until"]
    locked_until = clock.parse(locked_until) if locked_until else None
    payee = None
    if p.payee is not None:
        details = [(field.replace("_", " ").capitalize(), value) for field, value in p.payee.details]
        payee = {"name": p.payee.name, "details": details}
    checks = [
        {
            "label": r["rule"].replace("_", " ").capitalize(),
            "outcome": r["outcome"],
            "words": _words(r["outcome"], r["reason"]),
        }
        for r in json.loads(row["decision_json"])
    ]
    still_locked = locked_until is not None and now < locked_until
    return _page(
        request,
        conn,
        "confirm.html",
        status_code,
        _PIN_PAGE_HEADERS,
        summary=proposals.summary(p, store.load_state(conn, link.family_id, now)),
        payee=payee,
        checks=checks,
        error=error,
        locked_until=proposals.when_text(locked_until, config.timezone) if still_locked else None,
    )


@router.get("/confirm/{token}")
def confirm_page(token: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    now = clock.now(conn)
    link = approvals.open_link(conn, token, now)
    if link is None:
        return _notice(request, conn, EXPIRED, status_code=404)
    return _confirm_page(request, conn, link, now)


@router.post("/confirm/{token}")
def confirm_submit(
    token: str, request: Request, pin: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)
):
    state = request.app.state
    now = clock.now(conn)
    link = approvals.open_link(conn, token, now)
    if link is None:
        return _notice(request, conn, EXPIRED, status_code=404)
    pin = pin.strip()
    if not base.PIN_SHAPE.fullmatch(pin):
        # Not passed on: a typo must not use up one of the three attempts.
        return _confirm_page(request, conn, link, now, error=PIN_FORMAT_HELP, status_code=400)

    result = approvals.confirm(conn, token=token, pin=pin, pepper=state.settings.pin_pepper, now=now)
    match result.status:
        case "confirmed":
            return _run_confirmed(request, conn, link, now)
        case "not_awaiting":
            return _notice(request, conn, NOT_WAITING, status_code=409)
        case "invalid_link":
            return _notice(request, conn, EXPIRED, status_code=404)
        case "locked":
            return _confirm_page(request, conn, link, now, status_code=429)
        case "wrong_pin":
            config = store.family_config(conn, link.family_id)
            failures = store.family_row(conn, link.family_id)["pin_failures"]
            left = config.pin_max_attempts - failures
            error = f"That PIN isn't right. {left} {'attempt' if left == 1 else 'attempts'} left."
            return _confirm_page(request, conn, link, now, error=error, status_code=403)


def _run_confirmed(request: Request, conn: sqlite3.Connection, link: approvals.LinkInfo, now: datetime) -> Response:
    state = request.app.state
    try:
        outcome = executor.execute(
            conn,
            state.provider,
            family_id=link.family_id,
            proposal_id=link.proposal_id,
            now=now,
            base_url=state.settings.public_base_url,
        )
    except Exception as error:
        # The payout row stays pending and a retry reconciles it: say it is under way, not that it failed.
        logger.exception("execute failed after a confirmed PIN")
        audit.record(
            conn,
            at=now,
            family_id=link.family_id,
            actor=link.member_id,
            event="execute.error",
            data={"proposal_id": link.proposal_id, "error": type(error).__name__},
        )
        return _notice(request, conn, PROCESSING, heading="On its way", tone="info")
    heading, tone = _RESULT_HEADINGS.get(outcome.status, ("Not done", "bad"))
    return _notice(request, conn, outcome.message, heading=heading, tone=tone)


# --- money in: the stand-in checkout and the provider's notices ----------------------------------

# Answers that tell the provider "received, don't send it again". A 401 is for a notice that isn't to be believed.
_ACCEPTED = frozenset({"credited", "duplicate", "unknown"})
CHECKOUT_GONE = "This checkout isn't available. Ask the main payer to send a reminder for a new link."


def _standin(request: Request) -> StandInProvider | None:
    provider = request.app.state.provider
    return provider if isinstance(provider, StandInProvider) else None


def _checkout_missing(request: Request, conn: sqlite3.Connection) -> Response:
    return _notice(request, conn, CHECKOUT_GONE, status_code=404)


@router.get("/standin/pay/{tx_ref}")
def standin_checkout(tx_ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    collection = store.collection_by_tx_ref(conn, tx_ref) if _standin(request) else None
    if collection is None:
        return _checkout_missing(request, conn)
    amount = Money(collection["amount_minor"], collection["currency"])
    return _page(
        request,
        conn,
        "standin_pay.html",
        headers=_PIN_PAGE_HEADERS,
        amount=str(amount),
        state=collection["status"],
    )


@router.post("/standin/pay/{tx_ref}")
def standin_pay(tx_ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    provider = _standin(request)
    collection = None if provider is None else store.collection_by_tx_ref(conn, tx_ref)
    if collection is None or collection["status"] == "failed":
        return _checkout_missing(request, conn)
    try:
        headers, body = provider.mark_paid(tx_ref)
    except KeyError:
        # The stand-in keeps its payments in memory, so a restart makes it forget the links it gave out.
        return _checkout_missing(request, conn)
    _take_notice(conn, provider, headers, body)
    return RedirectResponse("/paid", status_code=303)


@router.get("/paid")
def paid_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, "paid.html")


def _take_notice(conn: sqlite3.Connection, provider: Provider, headers, body: bytes) -> str:
    return notices.handle_notice(conn, provider, headers=headers, body=body, now=clock.now(conn))


@router.post("/webhooks/{provider_name}")
async def webhook(provider_name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    provider = request.app.state.provider
    if provider_name != provider.name:
        return JSONResponse({"error": "Unknown provider."}, status_code=404)
    # The raw bytes: the signature covers exactly what was sent, so the body must not be parsed first.
    body = await request.body()
    result = await run_in_threadpool(_take_notice, conn, provider, request.headers, body)
    return JSONResponse({"result": result}, status_code=200 if result in _ACCEPTED else 401)


# --- the log and the demo controls ---------------------------------------------------------------


@router.get("/log", dependencies=[Depends(require_demo)])
def log_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    intact, broken_at = audit.verify(conn)
    events = [
        {
            "seq": e["seq"],
            "at": e["at"][:19].replace("T", " "),
            "family": e["family_id"] or "",
            "actor": e["actor"],
            "event": e["event"],
            "data": json.dumps(e["data"], sort_keys=True, ensure_ascii=False),
            "hash": (e["hash"] or "")[:12],
        }
        for e in audit.events(conn, None, limit=200)
    ]
    return _page(request, conn, "log.html", intact=intact, broken_at=broken_at, events=events)


@router.get("/demo", dependencies=[Depends(require_demo)])
def demo_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, "demo.html", flash=request.session.pop("flash", None), max_hours=MAX_ADVANCE_HOURS)


@router.post("/demo/advance", dependencies=[Depends(require_demo)])
def demo_advance(request: Request, hours: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    text = hours.strip()
    count = int(text) if _HOURS.fullmatch(text) else 0
    if not 1 <= count <= MAX_ADVANCE_HOURS:
        error = f"Enter a whole number of hours, from 1 to {MAX_ADVANCE_HOURS}."
        return _page(request, conn, "demo.html", 400, error=error, max_hours=MAX_ADVANCE_HOURS)
    with db.transaction(conn):
        clock.advance(conn, count * 3600)
        audit.record(
            conn, at=clock.now(conn), family_id=None, actor="demo", event="demo.advanced", data={"hours": count}
        )
    request.session["flash"] = f"The demo clock moved forward {count} hours."
    return RedirectResponse("/demo", status_code=303)


def first_of_next_month_at_nine(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    return datetime(local.year + local.month // 12, local.month % 12 + 1, 1, 9, 0, tzinfo=tz)


@router.post("/demo/next-month", dependencies=[Depends(require_demo)])
def demo_next_month(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    state = request.app.state
    tz = store.family_config(conn, seed.FAMILY_ID).timezone
    now = clock.now(conn)
    target = first_of_next_month_at_nine(now, tz)
    # Rounded up, so the clock never lands a moment before 09:00. A fixed target, so the hours limit doesn't apply.
    seconds = math.ceil((target.astimezone(UTC) - now).total_seconds())
    with db.transaction(conn):
        clock.advance(conn, seconds)
        audit.record(
            conn,
            at=clock.now(conn),
            family_id=None,
            actor="demo",
            event="demo.advanced",
            data={"seconds": seconds, "to": clock.iso(target)},
        )
    scheduler.tick(conn, state.provider, now=clock.now(conn), base_url=state.settings.public_base_url)
    request.session["flash"] = f"The demo clock moved to {proposals.when_text(target, tz)}. Karo ran its checks."
    return RedirectResponse("/demo", status_code=303)


@router.post("/demo/reset", dependencies=[Depends(require_demo)])
def demo_reset(request: Request):
    # No connection here: the file is deleted, and an open connection would keep it from being deleted on Windows.
    settings = request.app.state.settings
    settings.require("demo_pin", "pin_pepper")  # before the delete: without them the new database can't be seeded
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.db_path}{suffix}").unlink(missing_ok=True)
    ensure_database(settings)
    request.session["flash"] = "The demo is back at its starting point."
    return RedirectResponse("/demo", status_code=303)


# --- building the app ----------------------------------------------------------------------------


def ensure_database(settings: Settings) -> None:
    """Creates the schema and, if there are no families yet, seeds the demo family with the demo PIN."""
    with closing(db.connect(settings.db_path)) as conn:
        db.init(conn)
        if conn.execute("SELECT 1 FROM families").fetchone() is None:
            settings.require("demo_pin", "pin_pepper")
            seed.seed_demo(conn, pin=settings.demo_pin, pepper=settings.pin_pepper, now=clock.now(conn))


def run_scheduled_tick(settings: Settings, provider: Provider) -> None:
    # Its own connection, opened and closed here: the scheduler's thread must never share one with a request.
    with closing(db.connect(settings.db_path)) as conn:
        scheduler.tick(conn, provider, now=clock.now(conn), base_url=settings.public_base_url)


def build_provider(settings: Settings) -> Provider:
    if settings.provider != "standin":
        raise RuntimeError(f"KARO_PROVIDER={settings.provider!r} is not available yet. Use 'standin'.")
    settings.require("standin_secret")
    return StandInProvider.from_config(settings.standin_secret, settings.public_base_url)


def create_app(
    settings: Settings,
    *,
    client: Claude | None = None,
    provider: Provider | None = None,
    start_scheduler: bool = False,
) -> FastAPI:
    if client is None:
        settings.require("anthropic_api_key")
        client = ClaudeClient(settings.anthropic_api_key)
    if provider is None:
        provider = build_provider(settings)
    ensure_database(settings)
    # On the logger, not in a uvicorn flag, so a plain `uvicorn karo.web.app:app` is covered too.
    logging.getLogger("uvicorn.access").addFilter(_HIDE_TOKENS)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not start_scheduler:
            yield
            return
        background = BackgroundScheduler(timezone=UTC)
        # One at a time: a tick that outlasts 30 seconds must not be started a second time on top of itself.
        background.add_job(
            run_scheduled_tick,
            "interval",
            seconds=TICK_SECONDS,
            args=[settings, provider],
            max_instances=1,
            coalesce=True,
        )
        background.start()
        try:
            yield
        finally:
            # Not waited for: a tick cut short is safe, because a payout is two commits and the next tick reconciles it.
            background.shutdown(wait=False)

    # No /docs or /openapi.json: they would list every route to anyone who asks.
    app = FastAPI(title="Karo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.client = client
    app.state.provider = provider
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="karo_session",
        max_age=SESSION_SECONDS,
        same_site="lax",
        https_only=settings.public_base_url.startswith("https://"),
    )
    app.add_exception_handler(LoginRequired, _login_required)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    app.include_router(router)
    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    """`uvicorn karo.web.app:app` finds the app here, built on first use, so importing this module needs no secrets."""
    global _app
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if _app is None:
        _app = create_app(load_settings(), start_scheduler=True)
    return _app
