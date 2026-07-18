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


def latest(limit=20):
    """Most recent snapshots, newest first — handy for a quick sanity check."""
    sql = """
        SELECT captured_at, visa, mc, buda
        FROM rate_snapshots ORDER BY captured_at DESC LIMIT %s;
    """
    with connect() as c:
        return c.execute(sql, (limit,)).fetchall()
