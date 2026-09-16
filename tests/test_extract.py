import unittest

from canvas_rag.extract import html_to_markdown


class ExtractTests(unittest.TestCase):
    def test_html_extraction_keeps_readable_text_and_ignores_script(self):
        text = html_to_markdown("<h1>Week 2</h1><p>Neural memory</p><script>ignore me</script>")
        self.assertIn("Week 2", text)
        self.assertIn("Neural memory", text)
        self.assertNotIn("ignore me", text)


if __name__ == "__main__":
    unittest.main()
