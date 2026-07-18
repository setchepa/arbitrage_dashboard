"""
Postgres storage for periodic rate snapshots.

Connection comes from the DATABASE_URL env var (Railway injects this automatically
when a Postgres service is attached). Schema is created on demand, so the first
collector run bootstraps the table.
"""

import os

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_snapshots (
    id             BIGSERIAL PRIMARY KEY,
    captured_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    visa           NUMERIC(12,2) NOT NULL,   -- CLP per USD  (Visa)
    mc             NUMERIC(12,2) NOT NULL,   -- CLP per USD  (Mastercard)
    buda           NUMERIC(12,2) NOT NULL    -- CLP per USDC (Buda best ask)
);
CREATE INDEX IF NOT EXISTS idx_rate_snapshots_captured_at
    ON rate_snapshots (captured_at DESC);

-- Idempotent migration: an earlier version used visa_fx / mc_fx / buda_best_ask
-- with 4-6 decimals. Rename to visa / mc / buda and force 2 decimals.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'rate_snapshots' AND column_name = 'visa_fx') THEN
        ALTER TABLE rate_snapshots RENAME COLUMN visa_fx TO visa;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'rate_snapshots' AND column_name = 'mc_fx') THEN
        ALTER TABLE rate_snapshots RENAME COLUMN mc_fx TO mc;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'rate_snapshots' AND column_name = 'buda_best_ask') THEN
        ALTER TABLE rate_snapshots RENAME COLUMN buda_best_ask TO buda;
    END IF;
END $$;

ALTER TABLE rate_snapshots ALTER COLUMN visa TYPE NUMERIC(12,2);
ALTER TABLE rate_snapshots ALTER COLUMN mc   TYPE NUMERIC(12,2);
ALTER TABLE rate_snapshots ALTER COLUMN buda TYPE NUMERIC(12,2);

-- Numbers only: drop the non-numeric context columns kept by earlier versions.
ALTER TABLE rate_snapshots DROP COLUMN IF EXISTS visa_as_of;
ALTER TABLE rate_snapshots DROP COLUMN IF EXISTS mc_as_of;
ALTER TABLE rate_snapshots DROP COLUMN IF EXISTS buda_levels;

-- Single-row latch for edge-triggered ROI alerts. Not a history table: it only
-- remembers whether we were already above the threshold, so a persistent window
-- alerts once instead of every 10 minutes (and survives container restarts).
CREATE TABLE IF NOT EXISTS alert_state (
    id            SMALLINT PRIMARY KEY DEFAULT 1,
    was_above     BOOLEAN     NOT NULL DEFAULT FALSE,
    last_alert_at TIMESTAMPTZ,
    CONSTRAINT alert_state_single_row CHECK (id = 1)
);
INSERT INTO alert_state (id, was_above) VALUES (1, FALSE) ON CONFLICT (id) DO NOTHING;
"""


def get_dsn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. On Railway attach the Postgres service; "
            "locally export DATABASE_URL=postgresql://user:pass@host:port/dbname"
        )
    return dsn


def connect():
    return psycopg.connect(get_dsn())


def init_schema(conn=None):
    """Create the table/index if they don't exist. Safe to call every run."""
    if conn is None:
        with connect() as c:
            c.execute(SCHEMA)
            c.commit()
    else:
        conn.execute(SCHEMA)
        conn.commit()


def insert_snapshot(row, conn=None):
    """
    row: dict with visa, mc, buda.
    Returns the new row's id and captured_at.
    """
    sql = """
        INSERT INTO rate_snapshots (visa, mc, buda)
        VALUES (%(visa)s, %(mc)s, %(buda)s)
        RETURNING id, captured_at;
    """
    own = conn is None
    c = connect() if own else conn
    try:
        cur = c.execute(sql, row)
        result = cur.fetchone()
        c.commit()
        return result
    finally:
        if own:
            c.close()


def get_was_above(conn=None):
    """Were we already above the ROI threshold on the previous run?"""
    own = conn is None
    c = connect() if own else conn
    try:
        row = c.execute("SELECT was_above FROM alert_state WHERE id = 1").fetchone()
        return bool(row[0]) if row else False
    finally:
        if own:
            c.close()


def set_was_above(value, mark_alert=False, conn=None):
    """
    Latch the above/below state. `mark_alert=True` also stamps last_alert_at,
    which we do only when an alert was actually sent.
    """
    sql = ("UPDATE alert_state SET was_above = %s"
           + (", last_alert_at = now()" if mark_alert else "")
           + " WHERE id = 1")
    own = conn is None
    c = connect() if own else conn
    try:
        c.execute(sql, (bool(value),))
        c.commit()
    finally:
        if own:
            c.close()


def latest(limit=20):
    """Most recent snapshots, newest first — handy for a quick sanity check."""
    sql = """
        SELECT captured_at, visa, mc, buda
        FROM rate_snapshots ORDER BY captured_at DESC LIMIT %s;
    """
    with connect() as c:
        return c.execute(sql, (limit,)).fetchall()
