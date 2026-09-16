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


if __name__ == "__main__":
    unittest.main()
