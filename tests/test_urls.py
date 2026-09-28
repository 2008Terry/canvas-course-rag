import unittest

from canvas_rag.browser import attachment_download_url
from canvas_rag.urls import attachment_identity, canonical_attachment_url, canvas_file_key, normalize_canvas_url, redact_text, redact_url


class CanonicalAttachmentTests(unittest.TestCase):
    def test_every_route_to_one_canvas_file_has_one_identity(self):
        variants = [
            "https://canvas.test/courses/12/files/123",
            "https://canvas.test/courses/12/files/123/download?download_frd=1",
            "https://canvas.test/courses/12/files/123/download?verifier=abc123&wrap=1",
            "https://canvas.test/courses/12/files/123/preview",
            "https://canvas.test/courses/12/files/123?module_item_id=9#top",
            "https://CANVAS.test/courses/12/files/123/",
        ]
        self.assertEqual({canonical_attachment_url(url) for url in variants}, {"https://canvas.test/courses/12/files/123"})
        self.assertEqual(canvas_file_key(variants[2]), ("12", "123"))

    def test_different_files_courses_and_non_file_routes_stay_distinct(self):
        self.assertNotEqual(
            canonical_attachment_url("https://canvas.test/courses/12/files/123"),
            canonical_attachment_url("https://canvas.test/courses/12/files/124"),
        )
        self.assertNotEqual(
            canonical_attachment_url("https://canvas.test/courses/12/files/123"),
            canonical_attachment_url("https://canvas.test/courses/13/files/123"),
        )
        self.assertEqual(canonical_attachment_url("https://canvas.test/files/123/download"), "https://canvas.test/files/123")
        self.assertIsNone(canonical_attachment_url("https://canvas.test/courses/12/files"))
        self.assertIsNone(canonical_attachment_url("https://canvas.test/courses/12/files/folder/week-1"))
        self.assertEqual(
            attachment_identity("https://canvas.test/courses/12/media/clip.mp4?verifier=x"),
            "https://canvas.test/courses/12/media/clip.mp4",
        )

    def test_download_url_uses_verifier_transiently_but_identity_never_does(self):
        source = "https://canvas.test/courses/12/files/123/download?verifier=abc123&wrap=1"
        canonical = canonical_attachment_url(source)
        self.assertEqual(
            attachment_download_url(canonical, source),
            "https://canvas.test/courses/12/files/123/download?download_frd=1&verifier=abc123",
        )
        self.assertEqual(
            attachment_download_url(canonical, "https://canvas.test/courses/12/files/123/preview"),
            "https://canvas.test/courses/12/files/123/download?download_frd=1",
        )
        self.assertNotIn("verifier", canonical)


class RedactionTests(unittest.TestCase):
    def test_redaction_strips_credentials_and_keeps_other_parameters(self):
        self.assertEqual(
            redact_url("https://canvas.test/files/1/download?verifier=s3cret&download_frd=1&access_token=t&X-Amz-Signature=z"),
            "https://canvas.test/files/1/download?download_frd=1",
        )
        self.assertEqual(
            redact_text("Error: net::ERR_ABORTED at https://canvas.test/files/1/download?verifier=s3cret&x=1."),
            "Error: net::ERR_ABORTED at https://canvas.test/files/1/download?x=1.",
        )
        url = "https://canvas.test/courses/12/pages/a?note_id=4"
        self.assertIs(redact_url(url), url)

    def test_normalization_drops_verifier_and_module_navigation_but_keeps_distinct_queries(self):
        self.assertEqual(
            normalize_canvas_url("https://canvas.test/courses/12/pages/a?module_item_id=77&verifier=s3cret"),
            "https://canvas.test/courses/12/pages/a",
        )
        self.assertEqual(
            normalize_canvas_url("https://canvas.test/courses/12/pages/a?note_id=4"),
            "https://canvas.test/courses/12/pages/a?note_id=4",
        )


if __name__ == "__main__":
    unittest.main()
