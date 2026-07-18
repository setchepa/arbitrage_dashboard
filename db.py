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
    captured_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    visa_fx        NUMERIC(14,6) NOT NULL,   -- CLP per USD (Visa)
    visa_as_of     TEXT,
    mc_fx          NUMERIC(14,6) NOT NULL,   -- CLP per USD (Mastercard)
    mc_as_of       TEXT,
    buda_best_ask  NUMERIC(14,4) NOT NULL,   -- CLP per USDC (best ask)
    buda_levels    INTEGER                   -- order-book depth at capture time
);
CREATE INDEX IF NOT EXISTS idx_rate_snapshots_captured_at
    ON rate_snapshots (captured_at DESC);
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
    row: dict with visa_fx, visa_as_of, mc_fx, mc_as_of, buda_best_ask, buda_levels.
    Returns the new row's id and captured_at.
    """
    sql = """
        INSERT INTO rate_snapshots
            (visa_fx, visa_as_of, mc_fx, mc_as_of, buda_best_ask, buda_levels)
        VALUES (%(visa_fx)s, %(visa_as_of)s, %(mc_fx)s, %(mc_as_of)s,
                %(buda_best_ask)s, %(buda_levels)s)
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
        SELECT captured_at, visa_fx, mc_fx, buda_best_ask, buda_levels
        FROM rate_snapshots ORDER BY captured_at DESC LIMIT %s;
    """
    with connect() as c:
        return c.execute(sql, (limit,)).fetchall()
