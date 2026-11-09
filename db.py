"""SQLite storage for MapCord. The database file lives next to this script (mapcord.db)."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get('MAPCORD_DB', str(Path(__file__).with_name('mapcord.db')))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    lat REAL,
    lng REAL,
    address TEXT,
    final_url TEXT,
    method TEXT,
    status TEXT NOT NULL DEFAULT 'pending'   -- pending | processing | done | error
);
CREATE INDEX IF NOT EXISTS idx_locations_list ON locations(list_id, position);
CREATE INDEX IF NOT EXISTS idx_locations_status ON locations(status);

-- Shared lookup cache: the same Google Maps link never has to be opened twice
CREATE TABLE IF NOT EXISTS place_cache (
    url TEXT PRIMARY KEY,
    lat REAL,
    lng REAL,
    address TEXT,
    final_url TEXT,
    updated_at TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')   # lets the website read while the worker writes
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
