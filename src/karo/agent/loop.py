"""One chat turn: load the conversation, let the AI answer and use tools, keep the history, return the reply.

The history is append-only. The API ties each thinking block to the exact messages before it, so earlier turns are
never edited or dropped; a long conversation is replaced by a new one instead.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime

from karo import clock, db, store
from karo.agent.claude import Claude
from karo.agent.prompt import system_prompt
from karo.agent.tools import ToolContext, run_tool, tools_for
from karo.domain import Member
from karo.providers.base import Provider

MAX_MODEL_CALLS = 8
MAX_MESSAGES = 60
REFUSAL_REPLY = "Sorry, I can't help with that one."
TOO_MANY_STEPS_REPLY = "Sorry, that took too many steps. Please try again."
NO_ANSWER_REPLY = "Sorry, I couldn't put an answer together. Please try again."
STATUS_NOTE = "From Karo's records as this message arrives, replacing any status earlier in this chat:"

_TURN_LOCKS: dict[str, threading.Lock] = {}


def run_turn(
    conn: sqlite3.Connection,
    client: Claude,
    provider: Provider,
    *,
    member: Member,
    channel: str,
    content: list[dict],
    now: datetime,
    base_url: str,
) -> str:
    """`content` is Anthropic content blocks: text, base64 images and base64 PDFs. Returns the reply to send."""
    # One turn at a time per person and channel: two turns would each load the same history and the later write
    # back would drop the earlier turn.
    lock = _TURN_LOCKS.setdefault(f"{member.id}/{channel}", threading.Lock())
    with lock:
        return _run_turn(conn, client, provider, member, channel, content, now, base_url)


def _run_turn(
    conn: sqlite3.Connection,
    client: Claude,
    provider: Provider,
    member: Member,
    channel: str,
    content: list[dict],
    now: datetime,
    base_url: str,
) -> str:
    conversation_id, messages = _open_conversation(conn, member, channel, now)
    ctx = ToolContext(conn, provider, member.family_id, member, now, base_url)
    system = system_prompt(member, store.family_row(conn, member.family_id)["name"], member.role)
    tools = tools_for(member.role)
    reads_a_document = any(block.get("type") in ("image", "document") for block in content)
    effort = "medium" if reads_a_document else "low"

    note = _status_note(ctx) if messages else []
    _append(conn, conversation_id, messages, {"role": "user", "content": _unanswered_calls(messages) + note + content})
    for _ in range(MAX_MODEL_CALLS):
        response = client.create(system=system, messages=messages, tools=tools, effort=effort)
        # Checked before the content is read: a refusal can cut a tool call off halfway, and that must never run.
        if response.stop_reason == "refusal":
            reply = {"role": "assistant", "content": [{"type": "text", "text": REFUSAL_REPLY}]}
            _append(conn, conversation_id, messages, reply)
            # The refused message would be re-sent, and refused again, on every later turn. The record of it stays.
            conn.execute("UPDATE conversations SET active = 0 WHERE id = ?", (conversation_id,))
            return REFUSAL_REPLY
        # by_alias, because a fallback block's `from` field is `from_` in Python; exclude_none, because the API
        # rejects the null fields the SDK adds.
        blocks = [b.model_dump(mode="json", by_alias=True, exclude_none=True) for b in response.content]
        if not blocks:
            return NO_ANSWER_REPLY
        _append(conn, conversation_id, messages, {"role": "assistant", "content": blocks})
        if response.stop_reason != "tool_use":
            return _text_of(blocks) or NO_ANSWER_REPLY
        results = [_tool_result(block, ctx) for block in blocks if block["type"] == "tool_use"]
        _append(conn, conversation_id, messages, {"role": "user", "content": results})
    return TOO_MANY_STEPS_REPLY


def _tool_result(call: dict, ctx: ToolContext) -> dict:
    result = run_tool(call["name"], call["input"], ctx)
    block = {"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(result)}
    if "error" in result:
        block["is_error"] = True
    return block


def _status_note(ctx: ToolContext) -> list[dict]:
    """Where things stand, fresh, for every message after the first.

    It changes outside the chat (the PIN page, payments coming in), and the prompt telling the model to call
    get_status first was not enough: it repeated an old "awaiting_pin" result. A first message has no old result."""
    return [{"type": "text", "text": f"{STATUS_NOTE} {json.dumps(run_tool('get_status', {}, ctx))}"}]


def _text_of(blocks: list[dict]) -> str:
    return "\n".join(b["text"] for b in blocks if b["type"] == "text").strip()


def _unanswered_calls(messages: list[dict]) -> list[dict]:
    """Error results for tool calls a crashed or cut-off turn left open. The API rejects history that ends on one."""
    if not messages or messages[-1]["role"] != "assistant":
        return []
    interrupted = json.dumps({"error": "This request was interrupted before it ran."})
    return [
        {"type": "tool_result", "tool_use_id": b["id"], "content": interrupted, "is_error": True}
        for b in messages[-1]["content"]
        if b["type"] == "tool_use"
    ]


def _open_conversation(conn: sqlite3.Connection, member: Member, channel: str, now: datetime) -> tuple[str, list[dict]]:
    row = conn.execute(
        "SELECT c.id, c.messages_json FROM conversations c JOIN members m ON m.id = c.member_id "
        "WHERE m.family_id = ? AND c.member_id = ? AND c.channel = ? AND c.active = 1 "
        "ORDER BY c.started_at DESC, c.rowid DESC LIMIT 1",
        (member.family_id, member.id, channel),
    ).fetchone()
    if row is not None:
        messages = json.loads(row["messages_json"])
        if len(messages) <= MAX_MESSAGES:
            return row["id"], messages
    with db.transaction(conn):
        if row is not None:
            conn.execute("UPDATE conversations SET active = 0 WHERE id = ?", (row["id"],))
        conversation_id = db.new_id("conv")
        conn.execute(
            "INSERT INTO conversations (id, member_id, channel, messages_json, started_at, active) "
            "VALUES (?, ?, ?, '[]', ?, 1)",
            (conversation_id, member.id, channel, clock.iso(now)),
        )
    return conversation_id, []


def _append(conn: sqlite3.Connection, conversation_id: str, messages: list[dict], message: dict) -> None:
    messages.append(message)
    conn.execute("UPDATE conversations SET messages_json = ? WHERE id = ?", (json.dumps(messages), conversation_id))
