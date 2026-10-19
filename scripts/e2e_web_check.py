"""End-to-end check of scenes 0:30 and 1:00 through the running web app, with the real Claude.

Usage: start the server (uv run uvicorn karo.web.app:app --port 8000), then
    uv run python scripts/e2e_web_check.py
Reads the demo password and PIN from .env and never prints them.
"""
import html, os, re, sys, uuid
import httpx
from dotenv import load_dotenv

load_dotenv(".env")
BASE = "http://localhost:8000"
PASSWORD, PIN = os.environ["KARO_DEMO_PASSWORD"], os.environ["DEMO_PIN"]

def text_of(page: str) -> str:
    page = re.sub(r"(?s)<(script|style).*?</\1>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()

def login(c: httpx.Client, person: str) -> None:
    r = c.post("/login", data={"person": person, "password": PASSWORD})
    assert r.status_code in (200, 303) and "/chat" in str(r.url) + r.headers.get("location", ""), f"login failed {r.status_code}"

def chat(c: httpx.Client, text: str) -> str:
    r = c.post("/api/chat", json={"messages": [{"role": "user", "text": text}], "message_id": uuid.uuid4().hex}, timeout=180)
    r.raise_for_status()
    return r.json()["text"]

with httpx.Client(base_url=BASE, follow_redirects=True) as c:
    print("GET /:", c.get("/").status_code)
    login(c, "amina")
    print("\n[0:30] fake invoice ->", chat(c, "Forwarding the fees invoice: University of Westbridge, tuition 2026/27, GBP 6,000.00, due 2026-11-15, ref INV-2026-118. Bank: sort code 20-40-60, account number 99887766.")[:400])
    print("\n[1:00] real invoice ->", chat(c, "That was wrong. The real invoice: University of Westbridge, GBP 6,000.00, due 2026-11-15, ref INV-2026-118, sort code 20-40-60, account number 41234567. Split it equally between me, Baraka and Nakato.")[:400])
    inbox = [m["text"] for m in c.get("/api/inbox").json()]
    links = [u for t in inbox for u in re.findall(r"https?://\S+/confirm/\S+", t)]
    print("\ninbox:", [re.sub(r"/confirm/\S+", "/confirm/<redacted>", t)[:160] for t in inbox])
    if not links:
        sys.exit("no confirm link in Amina's inbox")
    path = links[-1].split("8000", 1)[-1]
    print("\nPIN page:", text_of(c.get(path).text)[:300])
    print("\nwrong PIN ->", text_of(c.post(path, data={"pin": "000000" if PIN != "000000" else "111111"}).text)[:200])
    print("\nright PIN ->", text_of(c.post(path, data={"pin": PIN}).text)[:300])
    print("\nreuse link ->", text_of(c.get(path).text)[:120])
    print("\n/log:", "Chain intact" in c.get("/log").text)
with httpx.Client(base_url=BASE, follow_redirects=True) as c:
    login(c, "baraka")
    print("\nBaraka's inbox:", [t["text"][:160] for t in c.get("/api/inbox").json()])
