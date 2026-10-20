"""The proof export: what it writes, and that it never writes a secret. Every test names the bug it would catch."""

import csv
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from karo import approvals, audit, clock, collections, db, executor, ledger, notices, proposals, quotes, seed, store
from karo.config import Settings
from karo.domain import Kind
from karo.money import Money
from karo.providers.standin import StandInProvider

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_proof.py"
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
FAM = "fam_otieno"
PIN = "4821"
PEPPER = "test-pepper"
BASE_URL = "https://karo.example"
STANDIN_SECRET = "standin-test-secret"


def load_script():
    spec = importlib.util.spec_from_file_location("export_proof", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses looks its own module up here
    spec.loader.exec_module(module)
    return module


export_proof = load_script()


def gbp(amount: str) -> Money:
    return Money.parse(amount, "GBP")


def confirm_latest_link(conn) -> str:
    """Follows the newest confirm link in the main payer's outbox, as she would, and enters the PIN. Gives the token."""
    row = conn.execute(
        "SELECT text FROM outbox WHERE member_id = 'm_amina' AND text LIKE '%/confirm/%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    token = re.search(r"/confirm/(\S+)", row["text"]).group(1)
    assert approvals.confirm(conn, token=token, pin=PIN, pepper=PEPPER, now=NOW).status == "confirmed"
    return token


def play_a_small_run(conn, provider) -> str:
    """One share paid through the stand-in's notice, then one fee paid out under the PIN: the rows the export lists.

    Returns the fee payment's confirm token, which the export must never carry."""
    westbridge = next(t.payee for t in store.load_state(conn, FAM, NOW).trusted_payees if t.id == "tp_westbridge")
    fee_id = store.insert_fee(
        conn, family_id=FAM, payee=westbridge, amount=gbp("6000.00"), due_date=None, invoice_ref="INV-77", now=NOW
    )
    share_id = db.new_id("sh")
    conn.execute(
        "INSERT INTO shares (id, family_id, fee_id, member_id, amount_minor, currency, created_at) "
        "VALUES (?, ?, ?, 'm_baraka', ?, 'GBP', ?)",
        (share_id, FAM, fee_id, gbp("3000.00").minor, clock.iso(NOW)),
    )
    collection_id = collections.request_share(
        conn, provider, family_id=FAM, share_id=share_id, now=NOW, base_url=BASE_URL
    )
    tx_ref = conn.execute("SELECT tx_ref FROM collections WHERE id = ?", (collection_id,)).fetchone()["tx_ref"]
    headers, body = provider.mark_paid(tx_ref)
    assert notices.handle_notice(conn, provider, headers=headers, body=body, now=NOW) == "credited"

    ledger.post(
        conn, family_id=FAM, at=NOW, amount=Money.parse("5000000", "KES"), kind="collection", ref="test-funds"
    )
    quote = quotes.create_quote(conn, provider, family_id=FAM, source_currency="KES", target=gbp("6000.00"), now=NOW)
    proposals.propose(
        conn,
        family_id=FAM,
        actor=store.member(conn, FAM, "m_amina"),
        kind=Kind.PAY_FEE,
        now=NOW,
        base_url=BASE_URL,
        amount=gbp("6000.00"),
        payee=westbridge,
        quote_id=quote.id,
        fee_id=fee_id,
    )
    token = confirm_latest_link(conn)
    proposal_id = conn.execute("SELECT id FROM proposals WHERE kind = 'pay_fee'").fetchone()["id"]
    done = executor.execute(conn, provider, family_id=FAM, proposal_id=proposal_id, now=NOW, base_url=BASE_URL)
    assert done.status == "executed"
    return token


@pytest.fixture(scope="module")
def played_db(tmp_path_factory):
    # Seeding hashes two PINs with Argon2, which is deliberately slow: build the run once and copy the file per test.
    path = tmp_path_factory.mktemp("template") / "karo.db"
    connection = db.connect(path)
    db.init(connection)
    seed.seed_demo(connection, pin=PIN, pepper=PEPPER, now=NOW)
    provider = StandInProvider.from_config(STANDIN_SECRET, BASE_URL)
    token = play_a_small_run(connection, provider)
    connection.close()
    return path, token


@pytest.fixture
def db_path(played_db, tmp_path):
    target = tmp_path / "karo.db"
    shutil.copyfile(played_db[0], target)
    return target


@pytest.fixture
def confirm_token(played_db):
    return played_db[1]


@pytest.fixture
def out_root(tmp_path):
    return tmp_path / "proof"


def run_export(db_path, out_root) -> Path:
    assert export_proof.main(["--db", str(db_path), "--out", str(out_root)]) == 0
    (folder,) = out_root.iterdir()
    return folder


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def audit_json(folder: Path) -> dict:
    return json.loads((folder / "audit.json").read_text(encoding="utf-8"))


def rows(db_path, sql: str):
    connection = db.connect(db_path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def tamper(db_path, *sql: str) -> None:
    """Run `sql` after dropping the append-only triggers, as someone holding the database file would."""
    attacker = db.connect(db_path)
    try:
        attacker.execute("DROP TRIGGER audit_no_update")
        attacker.execute("DROP TRIGGER audit_no_delete")
        for statement in sql:
            attacker.execute(statement)
    finally:
        attacker.close()


# --- what it writes ---------------------------------------------------------------------------


def test_the_export_writes_four_files_into_a_folder_named_for_the_run(db_path, out_root):
    # Bug caught: a run folder whose name has a colon or plus sign, which Windows refuses to create.
    folder = run_export(db_path, out_root)
    assert re.fullmatch(r"run-\d{8}T\d{6}Z", folder.name)
    assert sorted(p.name for p in folder.iterdir()) == ["README.md", "audit.json", "collections.csv", "payouts.csv"]


def test_the_audit_file_has_every_event_in_order_with_the_head_hash_and_the_verify_result(db_path, out_root, capsys):
    # Bug caught: an export that drops events, reorders them, or reports a head that is not the newest row's hash.
    folder = run_export(db_path, out_root)
    exported = audit_json(folder)
    stored = rows(db_path, "SELECT seq, event, hash FROM audit ORDER BY seq")
    assert [(e["seq"], e["event"], e["hash"]) for e in exported["events"]] == [
        (r["seq"], r["event"], r["hash"]) for r in stored
    ]
    assert exported["chain_head"] == stored[-1]["hash"]
    assert exported["verify"] == {"intact": True, "broken_at_seq": None}
    assert stored[-1]["hash"] in capsys.readouterr().out


def test_an_empty_log_has_the_starting_hash_as_its_head(tmp_path, out_root):
    # Bug caught: a crash on a database with no audit rows, or a head of None that nobody can write down.
    path = tmp_path / "empty.db"
    connection = db.connect(path)
    db.init(connection)
    connection.close()
    exported = audit_json(run_export(path, out_root))
    assert exported["events"] == [] and exported["chain_head"] == audit.GENESIS


def test_the_head_hash_moves_when_the_newest_rows_are_cut_off_though_the_chain_still_verifies(db_path, out_root):
    # Bug caught: trusting the chain alone. It cannot see truncation, so the printed head is the outside anchor.
    before = audit_json(run_export(db_path, out_root))
    tamper(db_path, "DELETE FROM audit WHERE seq = (SELECT MAX(seq) FROM audit)")
    shutil.rmtree(out_root)
    after = audit_json(run_export(db_path, out_root))
    assert after["verify"]["intact"] is True
    assert after["chain_head"] != before["chain_head"]


def test_a_tampered_row_is_reported_in_the_file_and_in_the_exit_code(db_path, out_root):
    # Bug caught: an export that says "intact" for a log someone edited, or that hides the damage by not writing.
    tamper(db_path, "UPDATE audit SET actor = 'someone-else' WHERE seq = 3")
    assert export_proof.main(["--db", str(db_path), "--out", str(out_root)]) == 1
    (folder,) = out_root.iterdir()
    assert audit_json(folder)["verify"] == {"intact": False, "broken_at_seq": 3}


def test_payouts_and_collections_carry_the_provider_references_and_the_simulated_flag(db_path, out_root):
    # Bug caught: money rows without the references that tie them to the provider, or a stand-in row not labelled so.
    folder = run_export(db_path, out_root)
    (payout,) = read_csv(folder / "payouts.csv")
    stored = rows(db_path, "SELECT * FROM payouts")[0]
    assert (payout["reference"], payout["provider_id"], payout["provider"]) == (
        stored["reference"],
        stored["provider_id"],
        "standin",
    )
    assert (payout["status"], payout["simulated"], payout["kind"]) == ("succeeded", "true", "pay_fee")
    assert (payout["amount"], payout["currency"]) == ("6000.00", "GBP")
    (collection,) = read_csv(folder / "collections.csv")
    stored = rows(db_path, "SELECT * FROM collections")[0]
    assert (collection["tx_ref"], collection["provider_id"], collection["provider"]) == (
        stored["tx_ref"],
        stored["provider_id"],
        "standin",
    )
    assert (collection["status"], collection["simulated"], collection["credit"]) == ("succeeded", "true", "3000.00")


def test_a_collection_from_a_real_provider_is_not_marked_simulated(db_path, out_root):
    # Bug caught: marking every collection simulated, which would hide real test-mode traffic as pretend.
    connection = db.connect(db_path)
    connection.execute("UPDATE collections SET provider = 'flutterwave'")
    connection.close()
    (collection,) = read_csv(run_export(db_path, out_root) / "collections.csv")
    assert collection["simulated"] == "false"


def test_the_readme_says_what_is_real_and_what_is_simulated_and_gives_the_head(db_path, out_root):
    # Bug caught: a proof pack that doesn't say which parts were pretend, or whose head hash can't be found to copy out.
    folder = run_export(db_path, out_root)
    readme = (folder / "README.md").read_text(encoding="utf-8")
    head = audit_json(folder)["chain_head"]
    assert "## What is real" in readme and "## What is simulated" in readme
    assert head in readme
    assert "1 payout" in readme and "1 collection" in readme


def test_the_readme_lists_the_seeded_opening_balance_as_simulated(db_path, out_root):
    # Bug caught: pretend starting money in the ledger that the proof pack never owns up to.
    readme = (run_export(db_path, out_root) / "README.md").read_text(encoding="utf-8")
    simulated = readme.split("## What is simulated")[1].split("## Files")[0]
    assert "opening balance" in simulated and "KES 200,000.00" in simulated and FAM in simulated


def test_the_readme_has_no_opening_balance_line_when_there_was_none(tmp_path, out_root):
    # Bug caught: a proof pack claiming an opening balance that the database never had.
    path = tmp_path / "empty.db"
    connection = db.connect(path)
    db.init(connection)
    connection.close()
    assert "opening balance" not in (run_export(path, out_root) / "README.md").read_text(encoding="utf-8")


def test_an_existing_run_folder_is_a_refusal_and_not_the_chain_broken_exit(db_path, out_root, monkeypatch, capsys):
    # Bug caught: two exports in the same second ending in a traceback and exit 1, which means "chain broken".
    monkeypatch.setattr(export_proof.clock, "now", lambda conn: NOW)
    assert export_proof.main(["--db", str(db_path), "--out", str(out_root)]) == 0
    (folder,) = out_root.iterdir()
    before = (folder / "audit.json").read_bytes()
    capsys.readouterr()

    assert export_proof.main(["--db", str(db_path), "--out", str(out_root)]) == 2

    assert "already exists" in capsys.readouterr().err
    assert (folder / "audit.json").read_bytes() == before


def test_the_readme_says_when_the_demo_clock_was_moved(db_path, out_root):
    # Bug caught: audit times from a fast-forwarded demo clock being read as real dates.
    connection = db.connect(db_path)
    clock.advance(connection, 3600)
    connection.close()
    readme = (run_export(db_path, out_root) / "README.md").read_text(encoding="utf-8")
    assert "demo clock" in readme.lower() and "3600 seconds" in readme


# --- what it must never write -----------------------------------------------------------------


def test_no_confirm_token_pin_or_secret_reaches_the_export(db_path, out_root, confirm_token):
    # Bug caught: a confirm link, the PIN, the pepper or the webhook secret copied into a file that gets published.
    folder = run_export(db_path, out_root)
    files = {p.name: p.read_text(encoding="utf-8") for p in folder.iterdir()}
    for name, text in files.items():
        assert confirm_token not in text, name
        assert "/confirm/" not in text, name
        assert PEPPER not in text and STANDIN_SECRET not in text and "argon2" not in text, name
    # Only the event data is searched for the PIN: a 64-character hash can hold any four digits by chance.
    data = json.dumps([e["data"] for e in audit_json(folder)["events"]])
    assert PIN not in data
    assert "link" not in read_csv(folder / "collections.csv")[0]


# --- how it is run ----------------------------------------------------------------------------


def test_a_missing_database_is_an_error_and_is_not_created(tmp_path, out_root, capsys):
    # Bug caught: a mistyped --db quietly creating an empty database and exporting "no events" as the proof.
    missing = tmp_path / "typo.db"
    assert export_proof.main(["--db", str(missing), "--out", str(out_root)]) == 2
    assert not missing.exists() and not out_root.exists()
    assert "typo.db" in capsys.readouterr().err


def test_without_db_the_path_comes_from_the_settings(db_path, out_root, monkeypatch):
    # Bug caught: the script ignoring KARO_DB, so it exports a different database from the one the app ran on.
    monkeypatch.setattr(export_proof, "load_settings", lambda: Settings(db_path=str(db_path)))
    assert export_proof.main(["--out", str(out_root)]) == 0
    assert len(audit_json(next(out_root.iterdir()))["events"]) == len(rows(db_path, "SELECT seq FROM audit"))


def test_the_export_never_changes_the_database(db_path, out_root):
    # Bug caught: an export that writes to the log it is vouching for.
    before = rows(db_path, "SELECT seq, hash FROM audit ORDER BY seq")
    run_export(db_path, out_root)
    assert [tuple(r) for r in rows(db_path, "SELECT seq, hash FROM audit ORDER BY seq")] == [tuple(r) for r in before]


def test_the_script_runs_from_the_command_line_and_prints_the_head(db_path, out_root):
    # Bug caught: a script that only works when imported, so the instructions in the README fail on first use.
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path), "--out", str(out_root)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    head = rows(db_path, "SELECT hash FROM audit ORDER BY seq DESC LIMIT 1")[0]["hash"]
    assert head in done.stdout
