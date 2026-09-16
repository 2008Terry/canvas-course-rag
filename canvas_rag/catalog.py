from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Catalog:
    """SQLite metadata catalog. Canvas credentials and browser state are never stored here."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _database(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._database() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS pages (
                    url TEXT PRIMARY KEY,
                    course_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS pages_by_course ON pages(course_id);
                CREATE TABLE IF NOT EXISTS attachments (
                    url TEXT PRIMARY KEY,
                    course_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    extracted_text TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL,
                    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS attachments_by_course ON attachments(course_id);
                CREATE TABLE IF NOT EXISTS index_status (
                    source_url TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def page_needs_update(self, url: str, text: str, fingerprint: str | None = None) -> bool:
        with self._database() as db:
            row = db.execute("SELECT sha256 FROM pages WHERE url = ?", (url,)).fetchone()
        return row is None or row["sha256"] != self.digest(fingerprint if fingerprint is not None else text)

    def save_page(self, *, url: str, course_id: str, title: str, relative_path: str, text: str, fingerprint: str | None = None) -> bool:
        digest = self.digest(fingerprint if fingerprint is not None else text)
        with self._database() as db:
            row = db.execute("SELECT sha256 FROM pages WHERE url = ?", (url,)).fetchone()
            changed = row is None or row["sha256"] != digest
            db.execute(
                """INSERT INTO pages(url, course_id, title, relative_path, text, sha256, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(url) DO UPDATE SET course_id=excluded.course_id,
                     title=excluded.title, relative_path=excluded.relative_path, text=excluded.text,
                     sha256=excluded.sha256, last_seen=CURRENT_TIMESTAMP""",
                (url, course_id, title, relative_path, text, digest),
            )
        return changed

    def pages_for_course(self, course_id: str) -> list[dict[str, Any]]:
        with self._database() as db:
            rows = db.execute("SELECT * FROM pages WHERE course_id=? ORDER BY title, url", (course_id,))
            return [dict(row) for row in rows]

    def all_pages(self) -> list[dict[str, Any]]:
        with self._database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM pages ORDER BY course_id, title")]

    def save_attachment(
        self, *, url: str, course_id: str, filename: str, relative_path: str, sha256: str,
        extracted_text: str = "",
    ) -> bool:
        digest = sha256
        with self._database() as db:
            old = db.execute("SELECT sha256 FROM attachments WHERE url=?", (url,)).fetchone()
            changed = old is None or old["sha256"] != digest
            db.execute(
                """INSERT INTO attachments(url, course_id, filename, relative_path, extracted_text, sha256, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(url) DO UPDATE SET course_id=excluded.course_id,
                     filename=excluded.filename, relative_path=excluded.relative_path,
                     extracted_text=excluded.extracted_text, sha256=excluded.sha256,
                     last_seen=CURRENT_TIMESTAMP""",
                (url, course_id, filename, relative_path, extracted_text, digest),
            )
        return changed

    def attachments_for_course(self, course_id: str) -> list[dict[str, Any]]:
        with self._database() as db:
            rows = db.execute("SELECT * FROM attachments WHERE course_id=? ORDER BY filename", (course_id,))
            return [dict(row) for row in rows]

    def all_attachments(self) -> list[dict[str, Any]]:
        with self._database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM attachments ORDER BY course_id, filename")]

    def documents_needing_index(self) -> list[dict[str, Any]]:
        with self._database() as db:
            pages = [dict(row) for row in db.execute(
                """SELECT p.url AS source_url, p.course_id, p.title, p.relative_path, p.text,
                          p.sha256 AS source_hash
                   FROM pages p LEFT JOIN index_status i ON i.source_url=p.url
                   WHERE p.text != '' AND (i.source_hash IS NULL OR i.source_hash != p.sha256)"""
            )]
            attachments = [dict(row) for row in db.execute(
                """SELECT a.url AS source_url, a.course_id, a.filename AS title, a.relative_path,
                          a.extracted_text AS text, a.sha256 AS source_hash
                   FROM attachments a LEFT JOIN index_status i ON i.source_url=a.url
                   WHERE a.extracted_text != '' AND (i.source_hash IS NULL OR i.source_hash != a.sha256)"""
            )]
        return pages + attachments

    def mark_indexed(self, source_url: str, source_hash: str) -> None:
        with self._database() as db:
            db.execute(
                "INSERT INTO index_status(source_url, source_hash) VALUES (?, ?) ON CONFLICT(source_url) DO UPDATE SET source_hash=excluded.source_hash",
                (source_url, source_hash),
            )

    def clear_index_status(self) -> None:
        with self._database() as db:
            db.execute("DELETE FROM index_status")
