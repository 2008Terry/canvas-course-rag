import unittest
from pathlib import Path

from canvas_rag.archive import render_offline_html, save_page


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


if __name__ == "__main__":
    unittest.main()
