"""
Database abstraction so tracker.py runs unmodified against either
local SQLite (CLI runs, backfills) or Postgres (the Vercel deployment).

Set POSTGRES_URL or DATABASE_URL (Vercel Postgres injects one of these
automatically once the integration is attached) to use Postgres.
Leave both unset to use a local tracker.db file, exactly as before.

The schema and all queries in tracker.py are written to be portable
between SQLite and Postgres (same CREATE TABLE / INDEX syntax, same
INSERT ... ON CONFLICT ... DO UPDATE upsert shape, same excluded.*
references) -- the only real differences are the placeholder style
('?' vs '%s') and that psycopg2 needs an explicit cursor where
sqlite3.Connection lets you call .execute() straight on the
connection. This module hides that gap.
"""

import os
import re
import sqlite3


def _qmark_to_pyformat(sql):
    """Translate sqlite3-style '?' placeholders to psycopg2's '%s'."""
    return re.sub(r"\?", "%s", sql)


class PostgresConn:
    """Wraps a psycopg2 connection with sqlite3.Connection's convenience
    surface (.execute / .executemany / .executescript / .commit) so
    tracker.py's call sites don't need to know which backend they're on."""

    def __init__(self, dsn):
        import psycopg2
        self._conn = psycopg2.connect(dsn)

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(_qmark_to_pyformat(sql), params)
        return cur

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(_qmark_to_pyformat(sql), seq)
        return cur

    def executescript(self, sql):
        """sqlite3's executescript runs a whole ;-separated block.
        psycopg2 has no equivalent, so split and run each statement --
        fine here since the schema carries no parameters."""
        cur = self._conn.cursor()
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def connect(path="tracker.db"):
    """Return a Postgres connection when POSTGRES_URL/DATABASE_URL is
    set (Vercel), else a local SQLite connection (CLI / dev)."""
    dsn = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
    if dsn:
        return PostgresConn(dsn)
    return sqlite3.connect(path)
