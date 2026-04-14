from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from optcg.config import APP_DIR, DB_PATH, RECEIPTS_DIR, EXPORTS_DIR

# ── Schema migrations ─────────────────────────────────────────────────────────
# Each tuple: (version: int, sql: str)
# Append new ones — never modify existing entries.

MIGRATIONS = [
    (1, """
    CREATE TABLE IF NOT EXISTS items (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type         TEXT NOT NULL
                          CHECK(item_type IN ('card','promo','blister','booster_box','sealed_set')),
        name              TEXT NOT NULL,
        set_code          TEXT,
        card_number       TEXT,
        language          TEXT DEFAULT 'EN',
        condition         TEXT,
        foil              INTEGER DEFAULT 0,
        variant           TEXT,
        graded            INTEGER DEFAULT 0,
        grading_company   TEXT,
        grade             TEXT,
        cert_number       TEXT,
        purchase_price    REAL NOT NULL,
        purchase_date     TEXT NOT NULL,
        purchase_currency TEXT DEFAULT 'EUR',
        purchase_source   TEXT,
        notes             TEXT,
        created_at        TEXT DEFAULT (datetime('now')),
        updated_at        TEXT DEFAULT (datetime('now'))
    )
    """),
    (2, """
    CREATE TABLE IF NOT EXISTS price_snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        source     TEXT NOT NULL,
        price_type TEXT NOT NULL,
        price      REAL NOT NULL,
        currency   TEXT DEFAULT 'EUR',
        url        TEXT,
        fetched_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_snaps_item
        ON price_snapshots(item_id, fetched_at DESC)
    """),
    (3, """
    CREATE TABLE IF NOT EXISTS receipts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        filename    TEXT NOT NULL,
        file_type   TEXT,
        description TEXT,
        added_at    TEXT DEFAULT (datetime('now'))
    )
    """),
    (4, """
    CREATE TABLE IF NOT EXISTS watchlist (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        set_code     TEXT,
        card_number  TEXT,
        language     TEXT,
        target_price REAL,
        notes        TEXT,
        added_at     TEXT DEFAULT (datetime('now'))
    )
    """),
]


def _ensure_dirs() -> None:
    for d in (APP_DIR, RECEIPTS_DIR, EXPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # DELETE journal is required for iCloud sync — WAL sidecar files don't
    # sync atomically alongside the main .db file.
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


@contextmanager
def db_conn():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_migrations(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version > current:
            for stmt in sql.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version}")


def init_db() -> None:
    with db_conn() as conn:
        run_migrations(conn)


# ── Thin query wrapper ────────────────────────────────────────────────────────

class Database:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def lastrowid(self, sql: str, params: tuple = ()) -> int:
        cur = self.conn.execute(sql, params)
        return cur.lastrowid
