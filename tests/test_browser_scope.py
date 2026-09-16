import unittest

from canvas_rag.browser import normalize_canvas_url, is_course_page_url, _canvas_file_download_url


class BrowserScopeTests(unittest.TestCase):
    def test_normalization_removes_tracking_and_fragment(self):
        self.assertEqual(
            normalize_canvas_url("https://canvas.test/courses/12/pages/a?utm_source=x&access_token=secret#top"),
            "https://canvas.test/courses/12/pages/a",
        )

    def test_crawler_is_limited_to_same_course_and_never_canvas_api(self):
        origin = "https://canvas.test"
        self.assertTrue(is_course_page_url("https://canvas.test/courses/12/grades", origin, "12"))
        self.assertFalse(is_course_page_url("https://canvas.test/courses/13/pages/a", origin, "12"))
        self.assertFalse(is_course_page_url("https://canvas.test/api/v1/courses/12", origin, "12"))
        self.assertFalse(is_course_page_url("https://outside.test/courses/12", origin, "12"))

    def test_canvas_file_preview_is_converted_to_normal_download_route(self):
        self.assertEqual(
            _canvas_file_download_url("https://canvas.test/courses/12/files/77?wrap=1"),
            "https://canvas.test/courses/12/files/77/download?wrap=1",
        )


if __name__ == "__main__":
    unittest.main()
