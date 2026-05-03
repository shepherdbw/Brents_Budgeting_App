
PRAGMA foreign_keys = ON;


-- Envelopes Table
CREATE TABLE IF NOT EXISTS envelopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    recurring_monthly_amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Settings Table
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Subscriptions Table
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    amount REAL NOT NULL,
    renewal_date TEXT NOT NULL,
    frequency TEXT NOT NULL DEFAULT 'monthly',
    envelope_id INTEGER,
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (envelope_id) REFERENCES envelopes(id)
        ON DELETE SET NULL
);


-- Transactions Table
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    payee TEXT NOT NULL,
    envelope_id INTEGER,
    amount REAL NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('INCOME', 'EXPENSE')),
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (envelope_id) REFERENCES envelopes(id)
        ON DELETE SET NULL
);

-- Allocations Table
CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    envelope_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (envelope_id) REFERENCES envelopes(id)
        ON DELETE CASCADE
);

-- Goals Table
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    target_amount REAL NOT NULL,
    target_date TEXT,
    contribution_frequency TEXT NOT NULL DEFAULT 'month',
    envelope_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (envelope_id) REFERENCES envelopes(id)
        ON DELETE SET NULL
);

-- Transfers Table
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_envelope_id INTEGER,
    source_goal_id INTEGER,
    destination_type TEXT NOT NULL,
    destination_envelope_id INTEGER,
    destination_goal_id INTEGER,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_envelope_id) REFERENCES envelopes(id)
        ON DELETE SET NULL,
    FOREIGN KEY (source_goal_id) REFERENCES goals(id)
        ON DELETE SET NULL,
    FOREIGN KEY (destination_envelope_id) REFERENCES envelopes(id)
        ON DELETE SET NULL,
    FOREIGN KEY (destination_goal_id) REFERENCES goals(id)
        ON DELETE SET NULL
);
