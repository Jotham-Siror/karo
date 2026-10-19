"""Live check: two real Claude turns against a throwaway DB (scenes 0:30 and 1:00)."""
import json, os, re, sqlite3, sys
from pathlib import Path
from dotenv import load_dotenv
from karo import db, clock, seed, store
from karo.agent.claude import ClaudeClient
from karo.agent.loop import run_turn
from karo.providers.standin import StandInProvider

load_dotenv(".env")
path = Path(sys.argv[1]); path.unlink(missing_ok=True)
conn = db.connect(path); db.init(conn)
now = clock.now(conn)
fam = seed.seed_demo(conn, pin="4821", pepper="live-check-pepper", now=now)
amina = store.member(conn, fam, "m_amina")
client = ClaudeClient(os.environ["ANTHROPIC_API_KEY"])
provider = StandInProvider.from_config("live-check-secret", "http://localhost:8000")
turns = [
    "Forwarding the fees invoice from the university: University of Westbridge, Tuition 2026/27, amount GBP 6,000.00, due 2026-11-15, ref INV-2026-118. Bank: sort code 20-40-60, account number 99887766.",
    "Sorry, that one was wrong. Here is the real invoice: University of Westbridge, GBP 6,000.00 due 2026-11-15, ref INV-2026-118, sort code 20-40-60, account number 41234567. Please split it equally between me, Baraka and Nakato.",
]
for i, text in enumerate(turns, 1):
    reply = run_turn(conn, client, provider, member=amina, channel="web", content=[{"type": "text", "text": text}], now=clock.now(conn), base_url="http://localhost:8000")
    print(f"--- turn {i} reply:\n{reply}\n")
row = conn.execute("SELECT messages_json FROM conversations WHERE member_id='m_amina'").fetchone()
calls = [b for m in json.loads(row[0]) if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
print("tool calls:", [(c["name"], {k: v for k, v in c["input"].items() if k != "payee_details"}) for c in calls])
results = [b for m in json.loads(row[0]) if m["role"] == "user" for b in (m["content"] if isinstance(m["content"], list) else []) if b.get("type") == "tool_result"]
print("any /confirm/ in tool results:", any("/confirm/" in json.dumps(r) for r in results))
print("proposals:", [tuple(r) for r in conn.execute("SELECT kind, status FROM proposals")])
print("fees:", [tuple(r) for r in conn.execute("SELECT payee_name, amount_minor, currency, status FROM fees")])
for r in conn.execute("SELECT member_id, text FROM outbox WHERE channel='web'"):
    print("outbox", r[0], ":", re.sub(r"/confirm/\S+", "/confirm/<redacted>", r[1])[:200])
print("audit events:", [r[0] for r in conn.execute("SELECT event FROM audit ORDER BY seq")])
