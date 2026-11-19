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
                UNIQUE (repo, issue_number)
            );
            CREATE TABLE IF NOT EXISTS seen_events (
                event_id TEXT PRIMARY KEY,
                seen_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pr_links (
                plane_item_id TEXT NOT NULL,
                pr_url        TEXT NOT NULL,
                PRIMARY KEY (plane_item_id, pr_url)
            );
            """
        )
        self._db.commit()

    def _row_to_link(self, row) -> Link | None:
        return Link(*row) if row else None

    def by_plane_item(self, item_id: str) -> Link | None:
        with self._lock:
            row = self._db.execute(
                "SELECT plane_item_id, plane_project_id, repo, issue_number, issue_url "
                "FROM links WHERE plane_item_id = ?", (item_id,)).fetchone()
        return self._row_to_link(row)

    def by_issue(self, repo: str, number: int) -> Link | None:
        with self._lock:
            row = self._db.execute(
                "SELECT plane_item_id, plane_project_id, repo, issue_number, issue_url "
                "FROM links WHERE lower(repo) = lower(?) AND issue_number = ?",
                (repo, number)).fetchone()
        return self._row_to_link(row)

    def save(self, link: Link) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO links VALUES (?, ?, ?, ?, ?)",
                (link.plane_item_id, link.plane_project_id, link.repo,
                 link.issue_number, link.issue_url))
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

    def pr_link_is_new(self, item_id: str, pr_url: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO pr_links VALUES (?, ?)", (item_id, pr_url))
            self._db.commit()
            return cur.rowcount == 1
