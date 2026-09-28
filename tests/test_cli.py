import contextlib
import io
import unittest
import uuid
from pathlib import Path

from canvas_rag.catalog import Catalog
from canvas_rag.cli import main


class CliTests(unittest.TestCase):
    def test_status_and_repair_report_legacy_tokens_and_failures(self):
        root = Path.cwd() / ".test-data" / f"cli-{uuid.uuid4().hex}"
        catalog = Catalog(root / "catalog.sqlite3")
        catalog.save_attachment(url="https://canvas.test/courses/12/files/5/download?wrap=1", course_id="12",
                                filename="a.pdf", relative_path="courses/12/files/a.pdf", sha256="x")
        catalog.save_attachment(url="https://canvas.test/courses/12/files/5/download?download_frd=1", course_id="12",
                                filename="b.pdf", relative_path="courses/12/files/b.pdf", sha256="x")
        run_id = catalog.start_run()
        catalog.record_failure(run_id=run_id, course_id="12", kind="page", url="https://canvas.test/courses/12/pages/x",
                               reason="TimeoutError")
        catalog.finish_run(run_id, courses=1, pages_captured=0, pages_changed=0, attachments=2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["-q", "--data-dir", str(root), "status"]), 0)
            self.assertEqual(main(["-q", "--data-dir", str(root), "repair", "--dry-run"]), 0)
        text = output.getvalue()
        self.assertIn("1 skipped", text)
        self.assertIn("pages/x: TimeoutError", text)
        self.assertIn("Run `course-rag repair`", text)
        self.assertIn("Would merge 1 duplicate attachment row(s)", text)
        self.assertEqual(len(catalog.all_attachments()), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            main(["-q", "--data-dir", str(root), "repair"])
        self.assertEqual([row["url"] for row in catalog.all_attachments()], ["https://canvas.test/courses/12/files/5"])


if __name__ == "__main__":
    unittest.main()
