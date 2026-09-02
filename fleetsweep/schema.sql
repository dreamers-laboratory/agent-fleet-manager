-- fleetsweep schema. Five tables, two state machines.
--
-- Per action:  QUEUED -> LEASED -> RUNNING -> DONE | FAILED (-> QUEUED again while attempts remain)
-- Per source:  next_due_at recomputed on every reconcile; errors back off exponentially.

CREATE TABLE IF NOT EXISTS source (
    source_id       INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    route           TEXT NOT NULL,                 -- meaning belongs to the executor: a URL, a command, an API query
    cadence_seconds INTEGER NOT NULL DEFAULT 86400,
    next_due_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    error_streak    INTEGER NOT NULL DEFAULT 0,
    last_sha256     TEXT,
    last_checked_at TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_source_due ON source(enabled, next_due_at);

CREATE TABLE IF NOT EXISTS action (
    action_id   INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES source(source_id),
    state       TEXT NOT NULL DEFAULT 'QUEUED',
    attempt     INTEGER NOT NULL DEFAULT 0,
    batch_id    TEXT,
    queued_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at  TEXT,
    finished_at TEXT,
    exit_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_state ON action(state);

CREATE TABLE IF NOT EXISTS batch (
    batch_id      TEXT PRIMARY KEY,                -- "<run_id>-batch-<nnnn>"
    run_id        TEXT NOT NULL,
    write_scope   TEXT NOT NULL,                   -- directory only this batch's worker may write
    leased_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    reconciled_at TEXT
);

CREATE TABLE IF NOT EXISTS observation (
    observation_id INTEGER PRIMARY KEY,
    action_id      INTEGER NOT NULL REFERENCES action(action_id),
    source_id      INTEGER NOT NULL,
    sha256         TEXT,
    status         TEXT,
    bytes          INTEGER,
    started_at     TEXT,
    finished_at    TEXT,
    elapsed_ms     REAL,
    receipt_path   TEXT,
    changed        INTEGER                          -- 1 when sha256 differs from the previous observation
);
CREATE INDEX IF NOT EXISTS idx_observation_source ON observation(source_id);

CREATE TABLE IF NOT EXISTS change_log (
    change_id INTEGER PRIMARY KEY,
    at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    actor     TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field     TEXT,
    old_value TEXT,
    new_value TEXT,
    reason    TEXT NOT NULL
);
