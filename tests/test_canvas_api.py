import asyncio
import unittest
import uuid
from pathlib import Path

from canvas_rag.browser import (
    EmptyPageError, _FailureLog, _PageLoader, _SkipLog, _download_attachment, is_course_page_url, is_junk_course_url,
    looks_like_shell,
)
from canvas_rag.canvas_api import CanvasAPI, CourseCollector, is_media_file, next_link, parse_json_body
from canvas_rag.catalog import Catalog


ORIGIN = "https://canvas.test"


class ParseTests(unittest.TestCase):
    def test_json_prefix_and_link_header(self):
        self.assertEqual(parse_json_body('while(1);[{"id":1}]'), [{"id": 1}])
        self.assertEqual(
            next_link('<https://canvas.test/a?page=2>; rel="next", <https://canvas.test/a?page=1>; rel="prev"'),
            "https://canvas.test/a?page=2",
        )
        self.assertIsNone(next_link(None))

    def test_media_detection(self):
        self.assertTrue(is_media_file(name="clip.MP4"))
        self.assertTrue(is_media_file(content_type="video/mp4"))
        self.assertTrue(is_media_file(content_type="audio/x-m4a"))
        self.assertTrue(is_media_file(url="https://aakaf.mivideo.it.umich.edu/media/x"))
        self.assertFalse(is_media_file(name="notes.pdf", content_type="application/pdf"))

    def test_junk_and_scope_filters(self):
        origin = ORIGIN
        self.assertFalse(is_course_page_url(f"{origin}/courses/12/discussion_topics/new", origin, "12"))
        self.assertFalse(is_course_page_url(f"{origin}/courses/12/announcements", origin, "12"))
        self.assertFalse(is_course_page_url(f"{origin}/courses/12/external_tools/5", origin, "12"))
        self.assertFalse(is_course_page_url(f"{origin}/courses/12?view=feed", origin, "12"))
        self.assertFalse(is_course_page_url(f"{origin}/courses/12/pages/$CANVAS_COURSE_REFERENCE$/file_ref/x", origin, "12"))
        self.assertTrue(is_course_page_url(f"{origin}/courses/12/assignments/9", origin, "12"))
        self.assertTrue(is_course_page_url(f"{origin}/courses/12/grades", origin, "12"))
        self.assertTrue(is_junk_course_url(f"{origin}/courses/12/discussion_topics/new"))
        self.assertFalse(looks_like_shell("Real assignment text"))
        self.assertTrue(looks_like_shell("Home\nAnnouncements\nLoading"))


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", url="", text=None):
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = headers or {}
        self._body = body
        self.url = url
        self._text = text

    async def body(self):
        return self._body

    async def text(self):
        return self._text if self._text is not None else self._body.decode("utf-8", "replace")


class FakeRequest:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    async def get(self, url, timeout=None, **kwargs):
        self.requested.append((url, kwargs))
        key = url.split("?", 1)[0]
        if url in self.responses:
            return self.responses[url]
        if key in self.responses:
            return self.responses[key]
        for pattern, response in self.responses.items():
            if pattern.endswith("*") and url.startswith(pattern[:-1]):
                return response
        return FakeResponse(status=404, url=url)


class CollectTests(unittest.TestCase):
    def test_collector_builds_documents_and_file_refs(self):
        course = f"{ORIGIN}/courses/12"
        responses = {
            f"{ORIGIN}/api/v1/courses/12": FakeResponse(text='while(1);{"id":12,"name":"Course","syllabus_body":"<p>Syll <a href=\\"/courses/12/files/9/download\\">s</a></p>"}'),
            f"{ORIGIN}/api/v1/courses/12/tabs": FakeResponse(text='[{"id":"home","html_url":"/courses/12","label":"Home","type":"internal"},'
                                                                  '{"id":"announcements","html_url":"/courses/12/announcements","label":"Announcements","type":"internal"},'
                                                                  '{"id":"gradescope","html_url":"/courses/12/external_tools/1","label":"Gradescope","type":"external"}]'),
            f"{ORIGIN}/api/v1/courses/12/students/submissions": FakeResponse(text="[]"),
            f"{ORIGIN}/api/v1/courses/12/assignments": FakeResponse(text='[{"id":1,"name":"Essay","html_url":"%s/assignments/1","description":"<p>Write <a href=\\"/courses/12/files/5\\">this</a></p>","points_possible":10,"submission_types":["online_upload"],"locked_for_user":false}]' % course),
            f"{ORIGIN}/api/v1/courses/12/assignment_groups": FakeResponse(text="[]"),
            f"{ORIGIN}/api/v1/courses/12/quizzes": FakeResponse(status=404),
            f"{ORIGIN}/api/v1/courses/12/pages": FakeResponse(text='[{"url":"home","title":"Home","body":"<p>Welcome</p>","front_page":true,"updated_at":"2026-01-01T00:00:00Z"}]'),
            f"{ORIGIN}/api/v1/courses/12/front_page": FakeResponse(text='{"url":"home","title":"Home","body":"<p>Welcome</p>","front_page":true}'),
            f"{ORIGIN}/api/v1/courses/12/discussion_topics": FakeResponse(text='[{"id":7,"title":"Hello","html_url":"%s/discussion_topics/7","message":"<p>Hi <a href=\\"/courses/12/files/5/download?verifier=s3cret\\">f</a></p>","posted_at":"2026-01-02T00:00:00Z","discussion_subentry_count":0,"attachments":[{"id":8,"display_name":"handout.pdf","content-type":"application/pdf","size":12}],"author":{"display_name":"A"}}]' % course),
            f"{ORIGIN}/api/v1/courses/12/modules": FakeResponse(text='[{"id":1,"name":"Week 1","items_count":3,"items":[{"id":11,"type":"File","title":"slides.pdf","content_id":5,"html_url":"%s/modules/items/11"},{"id":12,"type":"ExternalTool","title":"Kaltura clip","external_url":"https://aakaf.mivideo.it.umich.edu/x","html_url":"%s/modules/items/12"},'
                                                                     '{"id":13,"type":"ExternalUrl","title":"Syllabus doc","external_url":"https://docs.google.com/d","html_url":"%s/../../api/v1/courses/12/module_item_redirect/13"}]},'
                                                                     '{"id":2,"name":"Hidden","items_count":1}]' % (course, course, course)),
            f"{ORIGIN}/api/v1/courses/12/modules/2/items": FakeResponse(text='[{"id":21,"type":"Page","title":"Intro","page_url":"intro","html_url":"%s/modules/items/21"}]' % course),
            f"{ORIGIN}/api/v1/courses/12/pages/intro": FakeResponse(text='{"url":"intro","title":"Intro","body":"<p>Hidden module page</p>"}'),
            f"{ORIGIN}/api/v1/courses/12/files": FakeResponse(text='[{"id":5,"display_name":"slides.pdf","content-type":"application/pdf","size":100,"folder_id":1},{"id":9,"display_name":"lecture.mp4","content-type":"video/mp4","size":5000000,"folder_id":1}]'),
            f"{ORIGIN}/api/v1/courses/12/folders": FakeResponse(text='[{"id":1,"full_name":"course files/Week 1","files_count":2}]'),
            f"{ORIGIN}/api/v1/courses/12/files/8": FakeResponse(text='{"id":8,"display_name":"handout.pdf","content-type":"application/pdf","size":12}'),
        }
        # Match list endpoints with query strings
        for path in list(responses):
            if path.endswith(("/assignments", "/discussion_topics", "/modules", "/files", "/folders", "/pages", "/students/submissions", "/assignment_groups", "/quizzes", "/items")):
                responses[path + "*"] = responses[path]

        async def run():
            api = CanvasAPI(FakeRequest(responses), ORIGIN, delay=0)
            return await CourseCollector(api, ORIGIN, "12").collect()

        content = asyncio.run(run())
        kinds = {doc.kind for doc in content.documents}
        self.assertIn("assignment", kinds)
        self.assertIn("announcement", kinds)
        self.assertIn("page", kinds)
        self.assertIn("syllabus", kinds)
        self.assertIn("modules", kinds)
        self.assertEqual({str(fid) for fid in content.files}, {"5", "8", "9"})
        self.assertTrue(any(doc.url.endswith("/discussion_topics/7") for doc in content.documents))
        assignment = next(doc for doc in content.documents if doc.kind == "assignment")
        self.assertIn("Write", assignment.html)
        # Bodies and catalogued URLs never keep the verifier; the download_url may, for the request only.
        stored = " ".join(doc.html + doc.url for doc in content.documents)
        self.assertNotIn("s3cret", stored)
        self.assertIn("verifier=s3cret", content.files["5"].download_url)
        self.assertEqual(content.skipped_media[0][1], "Kaltura clip")
        self.assertIn(f"{course}/announcements", content.covered)
        self.assertNotIn(f"{course}/external_tools/1", content.browser_seeds)
        # External items are covered by their course route, module anchors alias the Modules document,
        # and items of a module whose items were not inlined are still listed and fetched.
        self.assertIn(f"{course}/modules/items/13", content.covered)
        self.assertEqual(content.aliases[f"{course}/modules/2"], f"{course}/modules")
        self.assertTrue(any(doc.url == f"{course}/pages/intro" and "Hidden module page" in doc.html for doc in content.documents))

    def test_pagination_follows_link_header(self):
        responses = {
            f"{ORIGIN}/api/v1/courses/12/files?per_page=100": FakeResponse(
                text='[{"id":1}]', headers={"link": f'<{ORIGIN}/api/v1/courses/12/files?page=2&per_page=100>; rel="next"'}
            ),
            f"{ORIGIN}/api/v1/courses/12/files?page=2&per_page=100": FakeResponse(text='[{"id":2}]'),
        }

        async def run():
            return await CanvasAPI(FakeRequest(responses), ORIGIN, delay=0).get_all("/api/v1/courses/12/files?per_page=100")

        result = asyncio.run(run())
        self.assertEqual([row["id"] for row in result.data], [1, 2])


class FakePage:
    def __init__(self, states=None, empty_first=0):
        self.states = states or {}
        self.url = "about:blank"
        self.closed = False
        self.empty_first = empty_first
        self.loads = 0

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def goto(self, url, **kwargs):
        self.url = url
        self.loads += 1

    async def wait_for_timeout(self, ms):
        return None

    async def wait_for_load_state(self, *a, **k):
        return None

    async def wait_for_function(self, *a, **k):
        return None

    async def evaluate(self, script):
        if self.loads <= self.empty_first:
            return {"html": "", "text": "Loading", "title": "Canvas LMS", "images": [], "embeds": [], "links": [], "loading": True}
        state = self.states.get(self.url, {"html": "<p>x</p>", "text": "x", "title": "T", "images": [], "embeds": [], "links": []})
        return {**state, "loading": False}


class FakeContext:
    def __init__(self, states=None, empty_first=0, responses=None):
        self.states = states or {}
        self.empty_first = empty_first
        self.request = FakeRequest(responses or {})
        self.pages = []

    async def new_page(self):
        page = FakePage(self.states, self.empty_first)
        self.pages.append(page)
        return page


class WaitAndSkipTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test-data" / f"api-{uuid.uuid4().hex}"
        self.catalog = Catalog(self.root / "catalog.sqlite3")
        self.run_id = self.catalog.start_run()
        self.failures = _FailureLog(self.catalog, self.run_id)
        self.skips = _SkipLog(self.catalog, self.run_id)

    def test_empty_page_is_retried_then_recorded(self):
        context = FakeContext(empty_first=99)

        async def run():
            loader = _PageLoader(context, ORIGIN, idle_ms=1, ready_ms=1, settle_ms=0)
            await loader.fresh_page()
            with self.assertRaises(EmptyPageError):
                await loader.load(f"{ORIGIN}/courses/12/assignments/1")
            return context.pages[0].loads

        loads = asyncio.run(run())
        self.assertEqual(loads, 2)

    def test_video_is_skipped_without_fetching_bytes_and_pdf_is_saved(self):
        pdf = f"{ORIGIN}/courses/12/files/5/download?download_frd=1"
        video = f"{ORIGIN}/courses/12/files/9/download?download_frd=1"
        responses = {
            pdf: FakeResponse(headers={"content-type": "application/pdf", "content-disposition": 'filename="notes.pdf"'},
                              body=b"%PDF-1.4 notes", url=pdf),
            video: FakeResponse(status=302, headers={"location": "https://cdn.test/lecture.mp4?X-Amz=1"}, url=video),
            "https://cdn.test/lecture.mp4?X-Amz=1": FakeResponse(headers={"content-type": "video/mp4"}, body=b"SHOULD-NOT-FETCH", url="https://cdn.test/lecture.mp4"),
        }
        context = type("C", (), {"request": FakeRequest(responses)})()

        async def run():
            saved = await _download_attachment(context, pdf, f"{ORIGIN}/courses/12/files/5", self.root, "12",
                                               self.catalog, self.failures, meta={"display_name": "notes.pdf", "content-type": "application/pdf", "size": 12},
                                               skips=self.skips)
            skipped = await _download_attachment(context, video, f"{ORIGIN}/courses/12/files/9", self.root, "12",
                                                 self.catalog, self.failures,
                                                 meta={"display_name": "lecture.mp4", "content-type": "video/mp4", "size": 9_000_000},
                                                 skips=self.skips)
            no_meta = await _download_attachment(context, video, f"{ORIGIN}/courses/12/files/99", self.root, "12",
                                                 self.catalog, self.failures, skips=self.skips, name_hint="")
            return saved, skipped, no_meta

        saved, skipped, no_meta = asyncio.run(run())
        self.assertIsNotNone(saved)
        self.assertIsNone(skipped)
        self.assertIsNone(no_meta)
        self.assertEqual([row["url"] for row in self.catalog.all_attachments()], [f"{ORIGIN}/courses/12/files/5"])
        recorded = self.catalog.skipped_for_run(self.run_id)
        self.assertEqual([row["kind"] for row in recorded], ["video", "video"])
        self.assertEqual(self.catalog.failures_for_run(self.run_id), [])
        # With metadata the video is skipped before any request; without metadata the redirect target
        # is inspected with max_redirects=0 so its body is never read.
        fetched_bodies = [url for url, kwargs in context.request.requested if "cdn.test/lecture.mp4" in url and kwargs.get("max_redirects") != 0]
        self.assertEqual(fetched_bodies, [])


if __name__ == "__main__":
    unittest.main()


class PruneTests(unittest.TestCase):
    def test_prune_removes_junk_shells_and_covered_rows_but_keeps_other_unseen_pages(self):
        from canvas_rag.browser import _prune_course_pages

        root = Path.cwd() / ".test-data" / f"prune-{uuid.uuid4().hex}"
        catalog = Catalog(root / "catalog.sqlite3")
        rows = {
            f"{ORIGIN}/courses/12/discussion_topics/new": "Home\nNew topic form",
            f"{ORIGIN}/courses/12/assignments/1": "Home\nGrades\nLoading",
            f"{ORIGIN}/courses/12/modules/items/5": "Old module item copy",
            f"{ORIGIN}/courses/12/pages/old-but-real": "Real text no longer linked",
            f"{ORIGIN}/courses/12/pages/fresh": "Fresh",
            f"{ORIGIN}/courses/12/pages/fresh?note_id=3": "Old copy of fresh",
            f"{ORIGIN}/courses/12/assignments/4/launch": "Home\nNew Quiz launch",
        }
        for url, text in rows.items():
            catalog.save_page(url=url, course_id="12", title="t", relative_path=f"courses/12/pages/{abs(hash(url))}.html", text=text)
        removed = _prune_course_pages(catalog, "12", saved={f"{ORIGIN}/courses/12/pages/fresh"},
                                      covered={f"{ORIGIN}/courses/12/modules/items/5"})
        self.assertEqual(sorted(removed), sorted([
            f"{ORIGIN}/courses/12/discussion_topics/new", f"{ORIGIN}/courses/12/assignments/1",
            f"{ORIGIN}/courses/12/modules/items/5", f"{ORIGIN}/courses/12/pages/fresh?note_id=3",
            f"{ORIGIN}/courses/12/assignments/4/launch",
        ]))
        self.assertEqual(sorted(row["url"] for row in catalog.pages_for_course("12")),
                         [f"{ORIGIN}/courses/12/pages/fresh", f"{ORIGIN}/courses/12/pages/old-but-real"])
