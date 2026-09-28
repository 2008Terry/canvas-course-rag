from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .urls import attachment_identity, normalize_canvas_url, redact_text, redact_url


@dataclass
class RepairReport:
    renamed: dict[str, str] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    moved_files: dict[str, str] = field(default_factory=dict)
    redacted_urls: int = 0
    merged_pages: int = 0
    merged_attachments: int = 0
    shared_page_files: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.renamed or self.removed)


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
                CREATE TABLE IF NOT EXISTS sync_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT,
                    courses INTEGER NOT NULL DEFAULT 0,
                    pages_captured INTEGER NOT NULL DEFAULT 0,
                    pages_changed INTEGER NOT NULL DEFAULT 0,
                    attachments INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sync_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    url TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS sync_failures_by_run ON sync_failures(run_id);
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
        url = redact_url(url)
        digest = self.digest(fingerprint if fingerprint is not None else text)
        with self._database() as db:
            row = db.execute("SELECT sha256, relative_path FROM pages WHERE url = ?", (url,)).fetchone()
            changed = row is None or row["sha256"] != digest
            if row is not None and row["relative_path"] != relative_path:
                # Search results carry the local path, so a moved snapshot is re-indexed.
                db.execute("DELETE FROM index_status WHERE source_url=?", (url,))
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
        url = redact_url(url)
        with self._database() as db:
            old = db.execute("SELECT sha256, relative_path FROM attachments WHERE url=?", (url,)).fetchone()
            changed = old is None or old["sha256"] != digest
            if old is not None and old["relative_path"] != relative_path:
                db.execute("DELETE FROM index_status WHERE source_url=?", (url,))
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

    # Sync run bookkeeping -------------------------------------------------------------

    def start_run(self, keep_runs: int = 20) -> str:
        run_id = uuid.uuid4().hex
        with self._database() as db:
            db.execute("INSERT INTO sync_runs(run_id) VALUES (?)", (run_id,))
            stale = [row["run_id"] for row in db.execute(
                "SELECT run_id FROM sync_runs ORDER BY started_at DESC, rowid DESC LIMIT -1 OFFSET ?", (keep_runs,)
            )]
            for old in stale:
                db.execute("DELETE FROM sync_failures WHERE run_id=?", (old,))
                db.execute("DELETE FROM sync_runs WHERE run_id=?", (old,))
        return run_id

    def record_failure(self, *, run_id: str, course_id: str, kind: str, url: str, reason: str) -> None:
        with self._database() as db:
            db.execute(
                "INSERT INTO sync_failures(run_id, course_id, kind, url, reason) VALUES (?, ?, ?, ?, ?)",
                (run_id, str(course_id), kind, redact_url(url), redact_text(reason)[:500]),
            )

    def finish_run(self, run_id: str, *, courses: int, pages_captured: int, pages_changed: int, attachments: int) -> None:
        with self._database() as db:
            db.execute(
                """UPDATE sync_runs SET finished_at=CURRENT_TIMESTAMP, courses=?, pages_captured=?, pages_changed=?,
                     attachments=?, failures=(SELECT COUNT(*) FROM sync_failures WHERE run_id=?)
                   WHERE run_id=?""",
                (courses, pages_captured, pages_changed, attachments, run_id, run_id),
            )

    def last_run(self) -> dict[str, Any] | None:
        with self._database() as db:
            row = db.execute("SELECT * FROM sync_runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def failures_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._database() as db:
            rows = db.execute("SELECT * FROM sync_failures WHERE run_id=? ORDER BY id", (run_id,))
            return [dict(row) for row in rows]

    # Migration of archives written by older versions ----------------------------------

    def repair_needed(self) -> RepairReport:
        """Report what `repair` would change without writing anything."""
        return self.repair(dry_run=True)

    def repair(self, *, root: Path | None = None, dry_run: bool = False) -> RepairReport:
        """Redact credential query parameters and merge rows that refer to the same page or Canvas file.

        Older versions stored ``?verifier=...`` URLs and one attachment row per link variant. The
        surviving row of each group is renamed to the canonical URL; the index status follows it.
        """
        report = RepairReport()
        db = self._connect()
        try:
            for table, key_for, merged_attr in (
                ("pages", normalize_canvas_url, "merged_pages"),
                ("attachments", attachment_identity, "merged_attachments"),
            ):
                groups: dict[str, list[dict[str, Any]]] = {}
                for row in db.execute(f"SELECT url, relative_path, last_seen FROM {table} ORDER BY last_seen DESC, url"):
                    groups.setdefault(key_for(row["url"]), []).append(dict(row))
                for canonical, rows in groups.items():
                    def rank(row, canonical=canonical):
                        on_disk = root is not None and (Path(root) / row["relative_path"]).is_file()
                        return (row["url"] != canonical, not on_disk)
                    keeper, *duplicates = sorted(rows, key=rank)
                    for row in duplicates:
                        db.execute(f"DELETE FROM {table} WHERE url=?", (row["url"],))
                        report.removed.append(row["url"])
                        if row["relative_path"] != keeper["relative_path"]:
                            report.moved_files[row["relative_path"]] = keeper["relative_path"]
                    setattr(report, merged_attr, getattr(report, merged_attr) + len(duplicates))
                    if keeper["url"] != canonical:
                        db.execute(f"UPDATE {table} SET url=? WHERE url=?", (canonical, keeper["url"]))
                        report.renamed[keeper["url"]] = canonical
            for row in db.execute("SELECT url FROM sync_failures"):
                if redact_url(row["url"]) != row["url"]:
                    db.execute("UPDATE sync_failures SET url=? WHERE url=?", (redact_url(row["url"]), row["url"]))
            report.redacted_urls = sum(1 for old in [*report.renamed, *report.removed] if redact_url(old) != old)
            for old in report.removed:
                db.execute("DELETE FROM index_status WHERE source_url=?", (old,))
            for old, new in report.renamed.items():
                status = db.execute("SELECT source_hash FROM index_status WHERE source_url=?", (old,)).fetchone()
                db.execute("DELETE FROM index_status WHERE source_url IN (?, ?)", (old, new))
                if status:
                    db.execute("INSERT INTO index_status(source_url, source_hash) VALUES (?, ?)", (new, status["source_hash"]))
            db.execute(
                """DELETE FROM index_status WHERE source_url NOT IN (SELECT url FROM pages)
                   AND source_url NOT IN (SELECT url FROM attachments)"""
            )
            report.shared_page_files = db.execute(
                "SELECT COALESCE(SUM(n - 1), 0) FROM (SELECT COUNT(*) AS n FROM pages GROUP BY relative_path HAVING n > 1)"
            ).fetchone()[0]
            if dry_run:
                db.rollback()
            else:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return report

    def source_urls(self) -> set[str]:
        with self._database() as db:
            return {row["url"] for row in db.execute("SELECT url FROM pages UNION SELECT url FROM attachments")}

    def referenced_files(self) -> set[str]:
        """Archive-relative paths the catalog points at (snapshots, sidecars, attachments)."""
        paths: set[str] = set()
        with self._database() as db:
            for row in db.execute("SELECT relative_path FROM pages"):
                paths.add(row["relative_path"])
                paths.add(str(Path(row["relative_path"]).with_suffix(".md").as_posix()))
            for row in db.execute("SELECT relative_path FROM attachments"):
                paths.add(row["relative_path"])
                paths.add(row["relative_path"] + ".md")
        return paths
