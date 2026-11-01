"""SQLite token store for gmail-mcp.

One ``accounts`` table keyed by email holds the refresh token, the most
recent access-token blob (as JSON), the granted scopes, and timestamps.
The store uses only stdlib ``sqlite3``; the database directory and file
are created on first use.

Schema::

    accounts(
        email        TEXT PRIMARY KEY,
        refresh_token TEXT NOT NULL,
        token        TEXT,          -- access-token JSON blob
        scopes       TEXT,          -- space-separated scope list
        added_at     TEXT NOT NULL, -- ISO-8601 UTC
        last_used_at TEXT           -- ISO-8601 UTC, nullable
    )
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gmail_mcp.config import db_path


@dataclass
class Account:
    """A stored Gmail account record."""

    email: str
    refresh_token: str
    token: str | None = None
    scopes: str | None = None
    added_at: str | None = None
    last_used_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TokenStore:
    """CRUD wrapper around the SQLite accounts table."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    email         TEXT PRIMARY KEY,
                    refresh_token TEXT NOT NULL,
                    token         TEXT,
                    scopes        TEXT,
                    added_at      TEXT NOT NULL,
                    last_used_at  TEXT
                )
                """
            )

    def upsert(
        self,
        email: str,
        refresh_token: str,
        token: str | None = None,
        scopes: str | None = None,
    ) -> None:
        """Insert or update an account, preserving the original added_at."""
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT added_at FROM accounts WHERE email = ?", (email,)
            ).fetchone()
            added_at = existing["added_at"] if existing else _now()
            conn.execute(
                """
                INSERT INTO accounts (email, refresh_token, token, scopes, added_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    refresh_token = excluded.refresh_token,
                    token         = excluded.token,
                    scopes        = excluded.scopes
                """,
                (email, refresh_token, token, scopes, added_at, None),
            )

    def update_token(self, email: str, token: str) -> None:
        """Persist a refreshed access-token blob."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET token = ? WHERE email = ?", (token, email)
            )

    def touch(self, email: str) -> None:
        """Mark an account as just used (updates last_used_at).

        Best-effort: if the token store is read-only, the timestamp update
        is skipped instead of failing the whole tool call.
        """
        with self._connect() as conn:
            try:
                conn.execute(
                    "UPDATE accounts SET last_used_at = ? WHERE email = ?",
                    (_now(), email),
                )
            except sqlite3.OperationalError:
                pass  # read-only store; last_used_at update is non-critical

    def get(self, email: str) -> Account | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)
            ).fetchone()
        return _row_to_account(row) if row else None

    def list_accounts(self) -> list[Account]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY email"
            ).fetchall()
        return [_row_to_account(r) for r in rows]

    def remove(self, email: str) -> bool:
        """Delete an account. Returns True if a row was removed."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE email = ?", (email,))
            return cur.rowcount > 0


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        email=row["email"],
        refresh_token=row["refresh_token"],
        token=row["token"],
        scopes=row["scopes"],
        added_at=row["added_at"],
        last_used_at=row["last_used_at"],
    )
