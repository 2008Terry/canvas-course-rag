import sqlite3
import unittest
import uuid
from pathlib import Path

from canvas_rag.catalog import Catalog


class CatalogTests(unittest.TestCase):
    def test_page_hash_detects_changes_and_preserves_stable_identity(self):
        temp_root = Path.cwd() / ".test-data"
        temp_root.mkdir(exist_ok=True)
        catalog = Catalog(temp_root / f"catalog-page-hash-{uuid.uuid4().hex}.sqlite3")
        self.assertTrue(catalog.page_needs_update("https://canvas.test/courses/12/pages/a", "one"))
        catalog.save_page(
            url="https://canvas.test/courses/12/pages/a",
            course_id="12",
            title="A",
            relative_path="courses/12/pages/a.html",
            text="one",
        )
        self.assertFalse(catalog.page_needs_update("https://canvas.test/courses/12/pages/a", "one"))
        self.assertTrue(catalog.page_needs_update("https://canvas.test/courses/12/pages/a", "two"))
        self.assertEqual(catalog.pages_for_course("12")[0]["relative_path"], "courses/12/pages/a.html")
        pending = catalog.documents_needing_index()
        self.assertEqual(len(pending), 1)
        catalog.mark_indexed(pending[0]["source_url"], pending[0]["source_hash"])
        self.assertEqual(catalog.documents_needing_index(), [])
        catalog.save_page(url="https://canvas.test/courses/12/pages/a", course_id="12", title="A",
                          relative_path="courses/12/pages/a-renamed.html", text="one")
        self.assertEqual(len(catalog.documents_needing_index()), 1, "a moved snapshot is re-indexed with its new path")

    def test_attachment_metadata_is_catalogued_without_credentials(self):
        temp_root = Path.cwd() / ".test-data"
        temp_root.mkdir(exist_ok=True)
        catalog = Catalog(temp_root / f"catalog-attachment-{uuid.uuid4().hex}.sqlite3")
        catalog.save_attachment(
            url="https://canvas.test/courses/12/files/77/download?download_frd=1",
            course_id="12",
            filename="notes.pdf",
            relative_path="courses/12/files/notes.pdf",
            sha256="abc",
        )
        saved = catalog.attachments_for_course("12")[0]
        self.assertEqual(saved["filename"], "notes.pdf")
        self.assertNotIn("cookie", repr(saved).lower())

    def test_stored_urls_never_keep_access_tokens(self):
        catalog = Catalog(Path.cwd() / ".test-data" / f"catalog-redact-{uuid.uuid4().hex}.sqlite3")
        catalog.save_attachment(
            url="https://canvas.test/courses/12/files/77/download?verifier=s3cret&download_frd=1",
            course_id="12", filename="notes.pdf", relative_path="courses/12/files/notes.pdf", sha256="abc",
        )
        catalog.save_page(url="https://canvas.test/courses/12/pages/a?access_token=t", course_id="12", title="A",
                          relative_path="courses/12/pages/a.html", text="one")
        run_id = catalog.start_run()
        catalog.record_failure(run_id=run_id, course_id="12", kind="attachment",
                               url="https://canvas.test/courses/12/files/78/download?verifier=s3cret", reason="HTTP 404")
        stored = repr(catalog.all_attachments() + catalog.all_pages() + catalog.failures_for_run(run_id))
        self.assertNotIn("s3cret", stored)
        self.assertNotIn("access_token", stored)

    def test_sync_runs_record_failures_for_the_summary(self):
        catalog = Catalog(Path.cwd() / ".test-data" / f"catalog-failures-{uuid.uuid4().hex}.sqlite3")
        first = catalog.start_run()
        catalog.record_failure(run_id=first, course_id="12", kind="page", url="https://canvas.test/courses/12/pages/a",
                               reason="TimeoutError: page.goto timed out")
        catalog.finish_run(first, courses=1, pages_captured=4, pages_changed=2, attachments=1)
        run = catalog.last_run()
        self.assertEqual((run["run_id"], run["failures"], run["pages_captured"]), (first, 1, 4))
        self.assertEqual(catalog.failures_for_run(first)[0]["reason"], "TimeoutError: page.goto timed out")
        second = catalog.start_run()
        catalog.finish_run(second, courses=1, pages_captured=5, pages_changed=0, attachments=1)
        self.assertEqual(catalog.last_run()["run_id"], second)
        self.assertEqual(catalog.failures_for_run(second), [])
        for _ in range(3):
            catalog.start_run(keep_runs=2)
        self.assertEqual(catalog.failures_for_run(first), [])

    def test_existing_catalog_from_older_version_opens_and_repairs(self):
        path = Path.cwd() / ".test-data" / f"catalog-legacy-{uuid.uuid4().hex}.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.executescript("""
            CREATE TABLE pages (url TEXT PRIMARY KEY, course_id TEXT NOT NULL, title TEXT NOT NULL,
                relative_path TEXT NOT NULL, text TEXT NOT NULL, sha256 TEXT NOT NULL,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE attachments (url TEXT PRIMARY KEY, course_id TEXT NOT NULL, filename TEXT NOT NULL,
                relative_path TEXT NOT NULL, extracted_text TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE index_status (source_url TEXT PRIMARY KEY, source_hash TEXT NOT NULL);
        """)
        base = "https://canvas.test/courses/12"
        for suffix, name in (("/download?download_frd=1", "a"), ("/download?verifier=s3cret&wrap=1", "b"), ("/download", "c")):
            legacy.execute("INSERT INTO attachments(url, course_id, filename, relative_path, extracted_text, sha256) VALUES (?,?,?,?,?,?)",
                           (f"{base}/files/5{suffix}", "12", f"n-{name}.pdf", f"courses/12/files/n-{name}.pdf", "text", "same"))
            legacy.execute("INSERT INTO index_status VALUES (?, ?)", (f"{base}/files/5{suffix}", "same"))
        legacy.execute("INSERT INTO pages(url, course_id, title, relative_path, text, sha256) VALUES (?,?,?,?,?,?)",
                       (f"{base}/pages/a?module_item_id=3", "12", "A", "courses/12/pages/a.html", "t", "h"))
        legacy.execute("INSERT INTO pages(url, course_id, title, relative_path, text, sha256) VALUES (?,?,?,?,?,?)",
                       (f"{base}/pages/a", "12", "A", "courses/12/pages/a.html", "t", "h"))
        legacy.execute("INSERT INTO index_status VALUES (?, ?)", (f"{base}/pages/a?module_item_id=3", "h"))
        legacy.commit()
        legacy.close()

        catalog = Catalog(path)
        self.assertIsNone(catalog.last_run())
        self.assertEqual(len(catalog.all_attachments()), 3)
        preview = catalog.repair_needed()
        self.assertEqual((preview.merged_attachments, preview.merged_pages, preview.redacted_urls), (2, 1, 1))
        self.assertEqual(len(catalog.all_attachments()), 3, "a dry run must not write")

        report = catalog.repair()
        attachments = catalog.all_attachments()
        self.assertEqual([row["url"] for row in attachments], [f"{base}/files/5"])
        self.assertEqual([row["url"] for row in catalog.all_pages()], [f"{base}/pages/a"])
        self.assertEqual(len(report.moved_files), 2)
        self.assertTrue(all(new == attachments[0]["relative_path"] for new in report.moved_files.values()))
        # The kept attachment keeps its index status; the kept page was never indexed, so it still is pending.
        self.assertEqual([doc["source_url"] for doc in catalog.documents_needing_index()], [f"{base}/pages/a"])
        self.assertNotIn("s3cret", repr(catalog.all_attachments()))
        self.assertFalse(catalog.repair_needed().changed)


if __name__ == "__main__":
    unittest.main()
