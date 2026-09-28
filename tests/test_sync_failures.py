import asyncio
import unittest
import uuid
from pathlib import Path

from canvas_rag.browser import PageLoadError, _FailureLog, _PageLoader, _crawl_course, _download_attachment
from canvas_rag.catalog import Catalog

ORIGIN = "https://canvas.test"


def _state(*links):
    return {"html": "<p>x</p>", "text": "x", "title": "T", "images": [], "embeds": [],
            "links": [{"href": href, "text": "", "download": False} for href in links]}


class FakePage:
    def __init__(self, broken=False, states=None):
        self.broken = broken
        self.states = states or {}
        self.url = "about:blank"
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def goto(self, url, **kwargs):
        if self.broken or url not in self.states:
            raise TimeoutError(f"Timeout 90000ms exceeded navigating to {url}")
        self.url = url

    async def wait_for_timeout(self, ms):
        return None

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def wait_for_function(self, expression, timeout=None, polling=None):
        return None

    async def evaluate(self, script):
        state = self.states[self.url]
        return {**state, "loading": False}


class FakeContext:
    """The first tab hangs forever; tabs opened later work."""

    def __init__(self, states, broken_tabs=1):
        self.states = states
        self.broken_tabs = broken_tabs
        self.pages = []

    async def new_page(self):
        page = FakePage(broken=len(self.pages) < self.broken_tabs, states=self.states)
        self.pages.append(page)
        return page


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", url=""):
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = headers or {}
        self._body = body
        self.url = url

    async def body(self):
        return self._body


class FakeRequest:
    """Canvas API calls (/api/v1/) are answered with 404 unless listed; they are tracked separately
    from page/file requests."""

    def __init__(self, responses):
        self.responses = responses
        self.requested = []
        self.api_requested = []

    async def get(self, url, timeout=None, **kwargs):
        if "/api/v1/" in url:
            self.api_requested.append(url)
            return self.responses.get(url, FakeResponse(status=404, url=url))
        self.requested.append(url)
        return self.responses[url]


class SyncFailureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test-data" / f"sync-{uuid.uuid4().hex}"
        self.catalog = Catalog(self.root / "catalog.sqlite3")
        self.run_id = self.catalog.start_run()
        self.failures = _FailureLog(self.catalog, self.run_id)

    def test_stuck_tab_is_replaced_and_failed_urls_are_retried(self):
        urls = [f"{ORIGIN}/courses/12/pages/p{i}" for i in range(4)]
        context = FakeContext({url: _state() for url in urls})

        async def run():
            loader = _PageLoader(context, ORIGIN, recover_after=3)
            await loader.fresh_page()
            for url in urls[:2]:
                with self.assertRaises(PageLoadError):
                    await loader.load(url)
            state = await loader.load(urls[2])
            return loader, state

        with self.assertLogs("canvas_rag", level="WARNING") as logs:
            loader, state = asyncio.run(run())
        self.assertEqual(state["title"], "T")
        self.assertEqual(len(context.pages), 2)
        self.assertTrue(context.pages[0].closed)
        self.assertEqual(loader.take_retries(), urls[:2])
        self.assertIn("fresh tab", "\n".join(logs.output))

    def test_crawl_recovers_skipped_pages_and_records_what_still_fails(self):
        course = f"{ORIGIN}/courses/12"
        states = {
            course: _state("/courses/12/pages/a", "/courses/12/pages/b", "/courses/12/pages/c", "/courses/12/pages/gone",
                           "/courses/12/files/5?verifier=s3cret", "/courses/12/files/5/download?download_frd=1",
                           "/courses/12/files/5/preview", "/courses/13/pages/other"),
        }
        for name in "abc":
            states[f"{course}/pages/{name}"] = _state()
        context = FakeContext(states, broken_tabs=0)
        loader = _PageLoader(context, ORIGIN, recover_after=3)

        async def run():
            await loader.fresh_page()
            return await _crawl_course(loader, origin=ORIGIN, course_id="12", course_url=course, label="c",
                                       failures=self.failures)

        with self.assertLogs("canvas_rag", level="WARNING"):
            captured, attachments = asyncio.run(run())
        self.assertEqual(len(captured), 4)
        self.assertEqual(list(attachments), [f"{course}/files/5"])
        download_url, aliases = attachments[f"{course}/files/5"]
        self.assertEqual(download_url, f"{course}/files/5/download?download_frd=1&verifier=s3cret")
        self.assertEqual(len(aliases), 3)
        recorded = self.catalog.failures_for_run(self.run_id)
        self.assertEqual([(row["kind"], row["url"]) for row in recorded], [("page", f"{course}/pages/gone")])
        self.assertIn("TimeoutError", recorded[0]["reason"])

    def test_attachment_failures_are_recorded_without_tokens_and_successes_are_catalogued_once(self):
        canonical = f"{ORIGIN}/courses/12/files/5"
        ok_url = f"{canonical}/download?download_frd=1&verifier=s3cret"
        missing = f"{ORIGIN}/courses/12/files/6"
        responses = {
            ok_url: FakeResponse(headers={"content-type": "text/plain", "content-disposition": 'attachment; filename="notes.txt"'},
                                 body=b"lecture notes", url=ok_url),
            f"{missing}/download?download_frd=1&verifier=s3cret": FakeResponse(status=403),
        }
        context = type("Context", (), {"request": FakeRequest(responses)})()

        async def run():
            first = await _download_attachment(context, ok_url, canonical, self.root, "12", self.catalog, self.failures)
            again = await _download_attachment(context, ok_url, canonical, self.root, "12", self.catalog, self.failures)
            failed = await _download_attachment(context, f"{missing}/download?download_frd=1&verifier=s3cret", missing,
                                                self.root, "12", self.catalog, self.failures)
            return first, again, failed

        with self.assertLogs("canvas_rag", level="WARNING"):
            first, again, failed = asyncio.run(run())
        self.assertEqual(first, again)
        self.assertIsNone(failed)
        rows = self.catalog.all_attachments()
        self.assertEqual([(row["url"], row["extracted_text"]) for row in rows], [(canonical, "lecture notes")])
        recorded = self.catalog.failures_for_run(self.run_id)
        self.assertEqual([(row["kind"], row["url"], row["reason"]) for row in recorded], [("attachment", missing, "HTTP 403")])
        self.assertEqual(len(self.failures.items), 1)
        self.assertNotIn("s3cret", repr(rows) + repr(recorded))


if __name__ == "__main__":
    unittest.main()


class FakeBrowserContext(FakeContext):
    def __init__(self, states, responses):
        super().__init__(states, broken_tabs=0)
        self.request = FakeRequest(responses)
        self.pages = [FakePage(states=states)]
        self.pages[0].url = f"{ORIGIN}/"


class FakePlaywright:
    def __init__(self, context):
        self.chromium = self
        self.context = context

    async def connect_over_cdp(self, url, timeout=None):
        return type("Browser", (), {"contexts": [self.context]})()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class SyncCanvasTests(unittest.TestCase):
    def test_full_sync_dedupes_files_keeps_distinct_snapshots_and_summarizes_failures(self):
        from unittest import mock

        from canvas_rag.browser import sync_canvas

        course = f"{ORIGIN}/courses/12"
        states = {
            f"{ORIGIN}/courses": _state("/courses/12"),
            course: _state("/courses/12/pages/notice?note_id=1", "/courses/12/pages/notice?note_id=2",
                           "/courses/12/pages/notice?module_item_id=7", "/courses/12/pages/broken",
                           "/courses/12/files/5?verifier=s3cret", "/courses/12/files/5/download?download_frd=1",
                           "/courses/12/files/5/preview"),
            f"{course}/pages/notice?note_id=1": _state(),
            f"{course}/pages/notice?note_id=2": _state(),
            f"{course}/pages/notice": _state(),
        }
        states[f"{course}/pages/notice?note_id=2"] = dict(states[course], html="<p>second</p>", links=[])
        states[course]["html"] = '<a href="/courses/12/files/5/preview">slides</a>'
        download = f"{course}/files/5/download?download_frd=1&verifier=s3cret"
        responses = {download: FakeResponse(headers={"content-type": "text/plain", "content-disposition": 'filename="n.txt"'},
                                            body=b"notes", url=download)}
        context = FakeBrowserContext(states, responses)
        root = Path.cwd() / ".test-data" / f"sync-full-{uuid.uuid4().hex}"
        catalog = Catalog(root / "catalog.sqlite3")
        with mock.patch("playwright.async_api.async_playwright", lambda: FakePlaywright(context)):
            with self.assertLogs("canvas_rag", level="INFO") as logs:
                summary = asyncio.run(sync_canvas(root=root, catalog=catalog))
        self.assertEqual(context.request.requested, [download], "one download per Canvas file")
        self.assertEqual((summary.courses, summary.pages_captured, summary.attachments), (1, 4, 1))
        self.assertEqual([(f.kind, f.url) for f in summary.failures], [("page", f"{course}/pages/broken")])
        pages = catalog.pages_for_course("12")
        self.assertEqual(len({page["relative_path"] for page in pages}), len(pages))
        self.assertTrue(all((root / page["relative_path"]).is_file() for page in pages))
        self.assertEqual([row["url"] for row in catalog.all_attachments()], [f"{course}/files/5"])
        run = catalog.last_run()
        self.assertEqual((run["failures"], run["pages_captured"], run["attachments"]), (1, 4, 1))
        self.assertIn("[1/1]", "\n".join(logs.output))
        stored = "".join(path.read_text(encoding="utf-8") for path in root.rglob("*.html")) + repr(catalog.all_pages())
        self.assertNotIn("s3cret", stored)
        home = next(page for page in pages if page["url"] == course)
        attachment = catalog.all_attachments()[0]["relative_path"]
        self.assertIn(f'href="../files/{Path(attachment).name}"', (root / home["relative_path"]).read_text(encoding="utf-8"))
