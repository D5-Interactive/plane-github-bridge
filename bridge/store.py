"""SQLite record of which Plane work item is linked to which GitHub issue."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass


@dataclass
class Link:
    plane_item_id: str
    plane_project_id: str
    repo: str
    issue_number: int
    issue_url: str
    plane_linked: bool = True      # False until the issue link has been added to the Plane item


class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS links (
                plane_item_id    TEXT PRIMARY KEY,
                plane_project_id TEXT NOT NULL,
                repo             TEXT NOT NULL,
                issue_number     INTEGER NOT NULL,
                issue_url        TEXT NOT NULL,
                plane_linked     INTEGER NOT NULL DEFAULT 1,
                last_link_attempt REAL NOT NULL DEFAULT 0,
                UNIQUE (repo, issue_number)
            );
            CREATE TABLE IF NOT EXISTS seen_events (
                event_id TEXT PRIMARY KEY,
                seen_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS posted (
                plane_item_id TEXT NOT NULL,
                key           TEXT NOT NULL,
                PRIMARY KEY (plane_item_id, key)
            );
            CREATE TABLE IF NOT EXISTS pr_links (
                plane_item_id TEXT NOT NULL,
                pr_url        TEXT NOT NULL,
                PRIMARY KEY (plane_item_id, pr_url)
            );
            """
        )
        # Databases from before plane_linked existed: their links were all added to Plane.
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(links)")}
        if "plane_linked" not in cols:
            self._db.execute("ALTER TABLE links ADD COLUMN plane_linked INTEGER NOT NULL DEFAULT 1")
        if "last_link_attempt" not in cols:
            self._db.execute("ALTER TABLE links ADD COLUMN last_link_attempt REAL NOT NULL DEFAULT 0")
        # Parked items, keyed on (item, repo). An older table without repo only held
        # short-lived parks, so it's simply rebuilt.
        fcols = {row[1] for row in self._db.execute("PRAGMA table_info(sync_failures)")}
        if fcols and "repo" not in fcols:
            self._db.execute("DROP TABLE sync_failures")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS sync_failures (
                   plane_item_id TEXT PRIMARY KEY,
                   repo          TEXT NOT NULL,
                   reason        TEXT NOT NULL,
                   failed_at     REAL NOT NULL)""")
        self._db.commit()

    _COLS = "plane_item_id, plane_project_id, repo, issue_number, issue_url, plane_linked"

    def _row_to_link(self, row) -> Link | None:
        if not row:
            return None
        return Link(*row[:5], plane_linked=bool(row[5]))

    def by_plane_item(self, item_id: str) -> Link | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {self._COLS} FROM links WHERE plane_item_id = ?", (item_id,)).fetchone()
        return self._row_to_link(row)

    def by_issue(self, repo: str, number: int) -> Link | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {self._COLS} FROM links WHERE lower(repo) = lower(?) AND issue_number = ?",
                (repo, number)).fetchone()
        return self._row_to_link(row)

    def save(self, link: Link) -> None:
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO links ({self._COLS}) VALUES (?, ?, ?, ?, ?, ?)",
                (link.plane_item_id, link.plane_project_id, link.repo,
                 link.issue_number, link.issue_url, int(link.plane_linked)))
            self._db.commit()

    def mark_plane_linked(self, item_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE links SET plane_linked = 1 WHERE plane_item_id = ?", (item_id,))
            self._db.commit()

    def missing_plane_links(self, limit: int) -> list[Link]:
        """Issues created on GitHub whose link never made it onto the Plane item,
        least recently tried first, so rows that keep failing can't starve the rest."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {self._COLS} FROM links WHERE plane_linked = 0 "
                "ORDER BY last_link_attempt, rowid LIMIT ?", (limit,)).fetchall()
        return [self._row_to_link(r) for r in rows]

    def link_attempted(self, item_id: str, at: float) -> None:
        with self._lock:
            self._db.execute("UPDATE links SET last_link_attempt = ? WHERE plane_item_id = ?", (at, item_id))
            self._db.commit()

    def recheck_all_links(self) -> int:
        """Mark every link as possibly missing in Plane, so the catch-up sync checks each one
        (BRIDGE_RECHECK_LINKS). Returns how many rows were marked."""
        with self._lock:
            cur = self._db.execute("UPDATE links SET plane_linked = 0, last_link_attempt = 0")
            self._db.commit()
            return cur.rowcount

    # ---------- items the catch-up sync couldn't send to GitHub for a lasting reason

    def record_failure(self, item_id: str, repo: str, reason: str, at: float) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO sync_failures VALUES (?, ?, ?, ?)",
                             (item_id, repo, reason, at))
            self._db.commit()

    def failure(self, item_id: str) -> tuple[str, str, float] | None:
        """(repo, reason, failed_at) if the item is parked."""
        with self._lock:
            row = self._db.execute(
                "SELECT repo, reason, failed_at FROM sync_failures WHERE plane_item_id = ?",
                (item_id,)).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def clear_failures(self) -> int:
        """Forget every parked item (done at startup: a restart often follows a fix)."""
        with self._lock:
            cur = self._db.execute("DELETE FROM sync_failures")
            self._db.commit()
            return cur.rowcount

    def clear_failures_for_owner(self, owner: str) -> int:
        """Forget parked items for one GitHub account's repos (after an installation change)."""
        prefix = owner.lower() + "/"
        with self._lock:
            cur = self._db.execute("DELETE FROM sync_failures WHERE lower(substr(repo, 1, ?)) = ?",
                                   (len(prefix), prefix))
            self._db.commit()
            return cur.rowcount

    def clear_failure(self, item_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sync_failures WHERE plane_item_id = ?", (item_id,))
            self._db.commit()

    def first_time(self, event_id: str) -> bool:
        """True the first time an event id is seen (for de-duplicating retries)."""
        if not event_id:
            return True
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO seen_events (event_id) VALUES (?)", (event_id,))
            self._db.commit()
            return cur.rowcount == 1

    def post_is_new(self, item_id: str, key: str) -> bool:
        """True the first time (item, key) is seen; used so GitHub activity is posted to Plane once."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO posted VALUES (?, ?)", (item_id, key))
            self._db.commit()
            return cur.rowcount == 1

    def pr_link_is_new(self, item_id: str, pr_url: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO pr_links VALUES (?, ?)", (item_id, pr_url))
            self._db.commit()
            return cur.rowcount == 1
