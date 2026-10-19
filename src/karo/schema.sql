PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS families (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  home_country TEXT NOT NULL,
  home_currency TEXT NOT NULL,
  timezone TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  frozen INTEGER NOT NULL DEFAULT 0,
  pin_hash TEXT,
  pin_failures INTEGER NOT NULL DEFAULT 0,
  pin_locked_until TEXT
);

CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES families(id),
  role TEXT NOT NULL CHECK (role IN ('main_payer','relative','student')),
  name TEXT NOT NULL,
  country TEXT NOT NULL,
  phone TEXT,
  email TEXT
);

CREATE TABLE IF NOT EXISTS identities (
  channel TEXT NOT NULL,
  address TEXT NOT NULL,
  member_id TEXT NOT NULL REFERENCES members(id),
  PRIMARY KEY (channel, address)
);

CREATE TABLE IF NOT EXISTS trusted_payees (
  id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES families(id),
  name TEXT NOT NULL,
  details_json TEXT NOT NULL,
  trusted_since TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('directory','added'))
);

CREATE TABLE IF NOT EXISTS fees (
  id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES families(id),
  payee_name TEXT NOT NULL,
  payee_details_json TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  due_date TEXT,
  invoice_ref TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','collecting','paid')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shares (
  id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES families(id),
  fee_id TEXT REFERENCES fees(id),
  member_id TEXT NOT NULL REFERENCES members(id),
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  paid_minor INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
  id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES families(id),
  provider TEXT NOT NULL,
  source_minor INTEGER NOT NULL,
  source_currency TEXT NOT NULL,
  target_minor INTEGER NOT NULL,
  target_currency TEXT NOT NULL,
  rate TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES families(id),
  kind TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('blocked','waiting','awaiting_pin','confirmed','executed','failed')),
  decision_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmations (
  proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
  member_id TEXT NOT NULL REFERENCES members(id),
  digest TEXT NOT NULL,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirm_links (
  token_hash TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  member_id TEXT NOT NULL REFERENCES members(id),
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE IF NOT EXISTS standing_orders (
  id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES families(id),
  payee_id TEXT NOT NULL REFERENCES trusted_payees(id),
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  day_of_month INTEGER NOT NULL CHECK (day_of_month BETWEEN 1 AND 28),
  approved_by TEXT REFERENCES members(id),
  proposal_id TEXT REFERENCES proposals(id),
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payouts (
  id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
  family_id TEXT NOT NULL REFERENCES families(id),
  provider TEXT NOT NULL,
  reference TEXT NOT NULL UNIQUE,
  provider_id TEXT,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  debit_minor INTEGER NOT NULL,
  debit_currency TEXT NOT NULL,
  home_value_minor INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('pay_fee','allowance_run')),
  status TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed')),
  simulated INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collections (
  id TEXT PRIMARY KEY,
  share_id TEXT NOT NULL REFERENCES shares(id),
  family_id TEXT NOT NULL REFERENCES families(id),
  provider TEXT NOT NULL,
  tx_ref TEXT NOT NULL UNIQUE,
  link TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  credit_minor INTEGER NOT NULL,
  credit_currency TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed')),
  provider_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  family_id TEXT NOT NULL REFERENCES families(id),
  at TEXT NOT NULL,
  currency TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('collection','payout','payout_reversal','conversion')),
  ref TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_messages (
  channel TEXT NOT NULL,
  message_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (channel, message_id)
);

CREATE TABLE IF NOT EXISTS provider_events (
  provider TEXT NOT NULL,
  event_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (provider, event_id)
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  member_id TEXT NOT NULL REFERENCES members(id),
  channel TEXT NOT NULL,
  messages_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id TEXT NOT NULL REFERENCES members(id),
  channel TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  family_id TEXT,
  actor TEXT NOT NULL,
  event TEXT NOT NULL,
  data_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
