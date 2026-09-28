import contextlib
import io
import os
import time
import unittest
import uuid
from pathlib import Path

from canvas_rag.catalog import Catalog
from canvas_rag.cli import main
from canvas_rag.index import WRITER_LOCK, IndexBusyError, LocalVectorIndex, index_writer_lock, maintain_index


class FakeEmbedder:
    def encode_passages(self, texts):
        return [[1.0, float(len(text) % 7)] for text in texts]

    def encode_query(self, text):
        return [1.0, 0.0]


def _index(path):
    return LocalVectorIndex(path, embedder=FakeEmbedder())


class IndexMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test-data" / f"index-{uuid.uuid4().hex}"

    def test_compaction_removes_old_versions_that_still_hold_tokens(self):
        index = _index(self.root / "vectors")
        token_url = "https://canvas.test/courses/12/files/5/download?verifier=s3cret"
        for number in range(5):
            index.add_document(source_url=f"{token_url}&n={number}", course_id="12", title="n.pdf",
                               relative_path="courses/12/files/n.pdf", text=f"lecture notes {number}")
        index.repair_sources(renamed={f"{token_url}&n={n}": f"https://canvas.test/courses/12/files/{n}" for n in range(5)},
                             valid={f"https://canvas.test/courses/12/files/{n}" for n in range(5)})
        self.assertGreater(index.version_count(), 5)
        self.assertGreater(index.token_residue(), 0, "old versions keep the tokenized rows")
        before, after = index.compact(remove_unverified=True)
        self.assertEqual((after, index.token_residue()), (1, 0))
        self.assertGreater(before, after)
        self.assertEqual(index.table.count_rows(), 5)
        self.assertEqual(len(_index(self.root / "vectors").search("notes", limit=10)), 5)

    def test_sync_maintenance_only_compacts_after_writes_or_version_buildup(self):
        index = _index(self.root / "vectors")
        self.assertIsNone(maintain_index(index, wrote=True), "no table yet")
        index.add_document(source_url="https://canvas.test/courses/12/pages/a", course_id="12", title="A",
                           relative_path="courses/12/pages/a.html", text="one")
        index.add_document(source_url="https://canvas.test/courses/12/pages/a", course_id="12", title="A",
                           relative_path="courses/12/pages/a.html", text="two")
        self.assertIsNone(maintain_index(index, wrote=False, max_versions=20))
        self.assertEqual(maintain_index(index, wrote=False, max_versions=2)[1], 1)
        index.add_document(source_url="https://canvas.test/courses/12/pages/b", course_id="12", title="B",
                           relative_path="courses/12/pages/b.html", text="three")
        self.assertEqual(maintain_index(index, wrote=True)[1], 1)
        self.assertEqual(index.table.count_rows(), 2)

    def test_writer_lock_blocks_a_second_writer_and_reclaims_stale_locks(self):
        vectors = self.root / "vectors"
        with index_writer_lock(vectors):
            self.assertTrue((vectors / WRITER_LOCK).is_file())
            with self.assertRaises(IndexBusyError):
                with index_writer_lock(vectors):
                    pass
        self.assertFalse((vectors / WRITER_LOCK).exists())
        (vectors / WRITER_LOCK).write_text("12345\n")
        old = time.time() - 7 * 3600
        os.utime(vectors / WRITER_LOCK, (old, old))
        with index_writer_lock(vectors):
            pass
        self.assertFalse((vectors / WRITER_LOCK).exists())

    def test_repair_command_leaves_no_tokens_in_old_index_versions(self):
        catalog = Catalog(self.root / "catalog.sqlite3")
        base = "https://canvas.test/courses/12/files/5/download"
        index = _index(self.root / "vectors")
        for suffix, name in (("?verifier=s3cret&wrap=1", "a"), ("?download_frd=1", "b")):
            catalog.save_attachment(url=base + suffix, course_id="12", filename=f"{name}.txt",
                                    relative_path=f"courses/12/files/{name}.txt", sha256="same", extracted_text="notes")
        # Simulate an index written by an older version, where the stored URL still had the token.
        index.add_document(source_url=base + "?verifier=s3cret&wrap=1", course_id="12", title="a.txt",
                           relative_path="courses/12/files/a.txt", text="notes")
        index.add_document(source_url=base + "?download_frd=1", course_id="12", title="b.txt",
                           relative_path="courses/12/files/b.txt", text="notes")
        self.assertGreater(index.token_residue(), 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["-q", "--data-dir", str(self.root), "repair"]), 0)
        self.assertIn("compacted and removed old versions", output.getvalue())
        reopened = _index(self.root / "vectors")
        self.assertEqual((reopened.version_count(), reopened.token_residue()), (1, 0))
        self.assertEqual(reopened.source_urls(), {"https://canvas.test/courses/12/files/5"})
        self.assertFalse((self.root / "vectors" / WRITER_LOCK).exists())

    def test_repair_refuses_to_run_while_another_writer_holds_the_lock(self):
        Catalog(self.root / "catalog.sqlite3")
        output = io.StringIO()
        with index_writer_lock(self.root / "vectors"), contextlib.redirect_stdout(output):
            self.assertEqual(main(["-q", "--data-dir", str(self.root), "repair"]), 2)
        self.assertIn("Repair not started", output.getvalue())


if __name__ == "__main__":
    unittest.main()
