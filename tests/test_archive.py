import unittest
from pathlib import Path

import uuid

from canvas_rag.archive import page_file_stem, relink_saved_pages, render_offline_html, save_page


class ArchiveTests(unittest.TestCase):
    def test_offline_html_rewrites_saved_links_and_strips_active_content(self):
        page = render_offline_html(
            title="Week 1",
            source_url="https://canvas.test/courses/12/pages/week-1",
            body=(
                '<p onclick="alert(1)" style="background:url(https://tracking.test/pixel)">Read <a href="../pages/week-2">next</a>'
                '<a href="https://outside.test/video?token=do-not-save&view=1">media</a>'
                '<a href="javascript:alert(2)">bad</a><img src="/courses/12/files/img.png"></p><script>alert(3)</script>'
            ),
            local_targets={"https://canvas.test/courses/12/pages/week-2": "week-2.html", "https://canvas.test/courses/12/files/img.png": "img.png"},
        )
        self.assertIn('href="week-2.html"', page)
        self.assertIn('src="img.png"', page)
        self.assertNotIn("onclick", page)
        self.assertNotIn("javascript:", page)
        self.assertNotIn("alert(3)", page)
        self.assertNotIn("tracking.test", page)
        self.assertNotIn("token=do-not-save", page)
        self.assertIn("view=1", page)

    def test_save_page_creates_html_markdown_and_stable_path(self):
        temp_root = Path.cwd() / ".test-data"
        temp_root.mkdir(exist_ok=True)
        saved = save_page(
            root=temp_root / "archive-case",
            course_id="12",
            page_id="page-9",
            title="Week 1 / Intro",
            url="https://canvas.test/courses/12/pages/week-1",
            body="<h1>Intro</h1><p>Course text</p>",
            markdown="# Intro\n\nCourse text",
            local_targets={},
        )
        self.assertTrue(saved.html_path.is_file())
        self.assertTrue(saved.markdown_path.is_file())
        self.assertIn("Course text", saved.html_path.read_text(encoding="utf-8"))
        self.assertEqual(saved.html_path, save_page(
            root=temp_root / "archive-case", course_id="12", page_id="page-9", title="Renamed",
            url="https://canvas.test/courses/12/pages/week-1", body="<p>New</p>",
            markdown="New", local_targets={}
        ).html_path)

    def test_offline_html_links_any_file_route_to_the_single_downloaded_copy(self):
        page = render_offline_html(
            title="Files", source_url="https://canvas.test/courses/12/pages/files",
            body='<a href="/courses/12/files/77/download?verifier=abc&wrap=1">a</a><a href="/courses/12/files/77/preview">b</a>'
                 '<a href="https://canvas.test/courses/12/files/88?verifier=abc">c</a>',
            local_targets={"https://canvas.test/courses/12/files/77": "../files/notes.pdf"},
        )
        self.assertEqual(page.count('href="../files/notes.pdf"'), 2)
        self.assertIn('href="https://canvas.test/courses/12/files/88"', page)
        self.assertNotIn("verifier", page)

    def test_page_file_stems_are_unique_per_url_and_readable_for_plain_pages(self):
        urls = [
            "https://canvas.test/courses/12/pages/syllabus",
            "https://canvas.test/courses/12/pages/syllabus?note_id=1214",
            "https://canvas.test/courses/12/pages/syllabus?note_id=1215",
            "https://canvas.test/courses/12/pages/$CANVAS_COURSE_REFERENCE$/file_ref/g5c7",
            "https://canvas.test/courses/12/pages/$CANVAS_COURSE_REFERENCE$/file_ref/ga1a",
            "https://canvas.test/courses/12/pages/Week%201",
            "https://canvas.test/courses/12/pages/Week-1",
            "https://canvas.test/courses/12/assignments/5",
            "https://canvas.test/courses/12/pages/" + "x" * 90,
            "https://canvas.test/courses/12/pages/" + "x" * 91,
        ]
        stems = [page_file_stem(url) for url in urls]
        self.assertEqual(len(set(stems)), len(urls))
        self.assertEqual(stems[0], "syllabus")
        self.assertEqual(page_file_stem(urls[1]), stems[1])
        self.assertTrue(all(len(stem) <= 80 for stem in stems))

    def test_relinking_points_snapshots_at_the_kept_attachment(self):
        root = Path.cwd() / ".test-data" / f"relink-{uuid.uuid4().hex}"
        pages = root / "courses" / "12" / "pages"
        pages.mkdir(parents=True)
        (pages / "a.html").write_text('<a href="../files/notes-old.pdf">n</a><a href="b.html">b</a>', encoding="utf-8")
        (pages / "b.html").write_text('<a href="https://canvas.test">x</a>', encoding="utf-8")
        rewritten = relink_saved_pages(root, {"courses/12/files/notes-old.pdf": "courses/12/files/notes-new.pdf"})
        self.assertEqual(rewritten, 1)
        self.assertIn('href="../files/notes-new.pdf"', (pages / "a.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
