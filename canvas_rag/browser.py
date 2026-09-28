from __future__ import annotations

import hashlib
import html
import logging
import mimetypes
import posixpath
import asyncio
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

from .archive import page_file_stem, safe_component, save_page, write_course_index
from .canvas_api import CanvasAPI, CourseCollector, SessionExpiredError, is_media_file
from .catalog import Catalog
from .extract import extract_attachment_text, html_to_markdown
from .urls import (
    canonical_attachment_url, canvas_file_key, normalize_canvas_url, redact_text, redact_url, transient_access_params,
)

logger = logging.getLogger("canvas_rag")

MAX_PAGES_PER_COURSE = 1000
MAX_ATTACHMENT_BYTES = 200 * 1024 * 1024

# Course URLs the browser never opens: editing/creation forms, settings and calendar noise,
# LTI launches (Gradescope, Kaltura/Media Gallery, ...), and discussions/announcements, which
# the API reads without marking them read. Files come from the API and page links instead of
# the Files browser.
_JUNK_COURSE_PATH = re.compile(
    r"""^/courses/\d+(?:
        /(?:calendar|calendar_events|settings|notebook|lti_collaborations|collaborations|conferences|gradebook|
            speed_grader|analytics|statistics|question_banks|content_migrations|copy|link_validator|student_view|
            confirm_action|enrollment_invitation|rubrics/\d+/edit|external_tools|lti|media_download|
            media_objects|media_attachments|discussion_topics|announcements|files|search|offline_web_exports|
            content_exports|pace_plans|course_pacing)(?:/.*)?
        |/files/folder(?:/.*)?
        |/users/\d+(?:/.*)?
        |/pages/[^/]+/(?:revisions|edit)(?:/.*)?
        |/assignments/\d+/(?:submissions|moderate|peer_reviews|edit|rubric|launch)(?:/.*)?
        |/modules/items/\d+/launch(?:/.*)?
        |/quizzes/\d+/(?:take|history|submissions|statistics|moderate|edit|managed_quiz_data)(?:/.*)?
        |/.*/(?:new|edit)
        |/(?:new|edit)
        |/.*(?:\$|%24)canvas_[a-z_]+(?:\$|%24).*
        |/.*/file_ref/.*
    )$""",
    re.I | re.X,
)


class BrowserConnectionError(RuntimeError):
    pass


class PageLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncFailure:
    course_id: str
    kind: str
    url: str
    reason: str


@dataclass
class SyncSummary:
    courses: int = 0
    pages_captured: int = 0
    pages_changed: int = 0
    attachments: int = 0
    api_documents: int = 0
    failures: list[SyncFailure] = field(default_factory=list)
    skipped: list[SyncFailure] = field(default_factory=list)
    empty_courses: list[str] = field(default_factory=list)


class _FailureLog:
    """Logs every skipped URL, keeps it for the sync summary, and records it in the catalog."""

    def __init__(self, catalog: Catalog | None = None, run_id: str | None = None):
        self.catalog = catalog
        self.run_id = run_id
        self.items: list[SyncFailure] = []

    def record(self, *, course_id: str, kind: str, url: str, reason: str, log: bool = True) -> None:
        reason = redact_text(" ".join(str(reason).split()))[:500] or "unknown error"
        failure = SyncFailure(str(course_id), kind, redact_url(url), reason)
        self.items.append(failure)
        if log:
            logger.warning("Skipped %s %s: %s", failure.kind, failure.url, failure.reason)
        if self.catalog is not None and self.run_id is not None:
            self.catalog.record_failure(
                run_id=self.run_id, course_id=failure.course_id, kind=failure.kind, url=failure.url, reason=failure.reason,
            )


def _error_reason(exc: BaseException) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def is_course_page_url(url: str, origin: str, course_id: str) -> bool:
    parts = urlsplit(url)
    root = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or parts.netloc.lower() != root.netloc.lower():
        return False
    path = unquote(parts.path.rstrip("/"))
    if re.search(r"(?:^|/)api(?:/|$)", path, re.I):
        return False
    if not re.match(rf"^/courses/{re.escape(str(course_id))}(?:/|$)", path):
        return False
    if _JUNK_COURSE_PATH.match(path) or "$CANVAS_" in path.upper():
        return False
    if _junk_query(parts.query):
        return False
    return True


def _junk_query(query: str) -> bool:
    return bool(query and re.search(r"(?:^|&)view=(?:notifications|feed)(?:&|$)", query, re.I))


def is_junk_course_url(url: str) -> bool:
    """True for course URLs that are forms, settings, LTI launches or other non-content noise."""
    parts = urlsplit(url)
    path = unquote(parts.path.rstrip("/"))
    return bool(_JUNK_COURSE_PATH.match(path)) or _junk_query(parts.query) or "$CANVAS_" in path.upper()


def _course_id(url: str) -> str | None:
    match = re.search(r"/courses/(\d+)(?:/|$)", urlsplit(url).path)
    return match.group(1) if match else None


def _safe_filename(name: str) -> str:
    return safe_component(Path(name).name, "attachment")


def _canvas_file_download_url(url: str) -> str | None:
    parts = urlsplit(url)
    if re.search(r"/(?:courses/\d+/)?files/\d+/download(?:/|$)", parts.path):
        return url
    canonical = canonical_attachment_url(url)
    if canonical:
        return urlunsplit((parts.scheme, parts.netloc, urlsplit(canonical).path + "/download", parts.query, ""))
    return None


def attachment_download_url(canonical_url: str, source_url: str) -> str:
    """Download route for a canonical Canvas file. A ``verifier`` grant from the page link is kept
    for this request only; it is never stored or printed."""
    query = urlencode([("download_frd", "1"), *transient_access_params(source_url)[:1]])
    return _canvas_file_download_url(f"{canonical_url}?{query}") or canonical_url


# Canvas renders assignments, announcements, grades and list pages client-side. Content is ready
# when the main content area has text other than "Loading" and no visible spinner.
_READY_JS = r"""() => {
  const el = document.querySelector('#content') || document.querySelector('[role="main"]') || document.querySelector('main');
  if (!el) return document.readyState === 'complete';
  const text = el.innerText || '';
  if (/(^|\n)\s*Loading(\.\.\.|…)?\s*(\n|$)/i.test(text)) return false;
  if (!text.replace(/Loading(\.\.\.|…)?/gi, '').trim()) return false;
  return ![...el.querySelectorAll('[role="progressbar"], .loading-indicator, [class*="spinner"], [class*="Spinner"]')]
    .some(x => x.getClientRects().length && getComputedStyle(x).visibility !== 'hidden');
}"""

# Prefer #content (the page body next to the course menu), then role=main/main; drop navigation,
# hidden elements and scripts so the course menu is not prepended to every page.
_CONTENT_JS = r"""() => {
  const candidates = ['#content', '[role="main"]', 'main', '#main'];
  let root = null;
  for (const s of candidates) { const e = document.querySelector(s); if (e && (e.innerText || '').trim()) { root = e; break; } }
  root = root || document.querySelector('#content') || document.querySelector('[role="main"]') || document.querySelector('main') || document.body;
  const hidden = [];
  for (const e of root.querySelectorAll('*')) {
    if (getComputedStyle(e).display === 'none') { e.setAttribute('data-crs-hidden', '1'); hidden.push(e); }
  }
  const clone = root.cloneNode(true);
  hidden.forEach(e => e.removeAttribute('data-crs-hidden'));
  clone.querySelectorAll('[data-crs-hidden], nav, [role="navigation"], #left-side, #section-tabs, .ic-app-course-menu, '
    + '#courseMenuToggle, #breadcrumbs, .ic-app-crumbs, header#header, .ic-app-header, script, style, noscript, template')
    .forEach(e => e.remove());
  let visible = root.innerText || '';
  if (root.matches('#main') || root === document.body) {
    const menu = document.querySelector('#section-tabs, nav[aria-label], #left-side');
    if (menu && menu.innerText) visible = visible.replace(menu.innerText, '');
  }
  const links = [...document.querySelectorAll('a[href]')].filter(a => {
    const s = getComputedStyle(a); return a.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
  }).map(a => ({href:a.href, text:(a.innerText || a.getAttribute('aria-label') || '').trim(), download:a.hasAttribute('download')}));
  const images = [...clone.querySelectorAll('img[src]')].map(i => ({src:i.src, alt:i.alt || ''}));
  const embeds = [...clone.querySelectorAll('iframe[src]')].map(i => ({src:i.src, title:i.title || 'External media'}));
  let title = document.title || '';
  if (!title.trim() || /^Canvas( LMS)?$/i.test(title.trim())) {
    const h = clone.querySelector('h1, h2'); if (h && h.textContent.trim()) title = h.textContent.trim();
  }
  const loading = !visible.replace(/Loading(\.\.\.|…)?/gi, '').trim();
  return {html:clone.innerHTML, links, images, embeds, title, text:visible, loading};
}"""


def looks_like_shell(text: str) -> bool:
    """A saved page whose only content is a "Loading" placeholder (optionally after the course menu)."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return True
    return bool(re.fullmatch(r"loading(?:\.\.\.|…)?", lines[-1], re.I))


def _is_empty_state(state: dict) -> bool:
    if state.get("loading"):
        return True
    text = re.sub(r"Loading(?:\.\.\.|…)?", "", state.get("text") or "", flags=re.I).strip()
    return not text


async def _visible_content(page):
    return await page.evaluate(_CONTENT_JS)


async def _settle(page, *, idle_ms: int, ready_ms: int) -> None:
    """Wait for network idle and for real content; both waits are best effort."""
    try:
        await page.wait_for_load_state("networkidle", timeout=idle_ms)
    except Exception:
        pass
    try:
        await page.wait_for_function(_READY_JS, timeout=ready_ms, polling=250)
    except Exception:
        pass


async def _close_page(page):
    if page is not None and not page.is_closed():
        try:
            await page.close()
        except Exception:
            pass


class EmptyPageError(PageLoadError):
    """The page loaded but still showed no content (only "Loading") after waiting and a reload."""


class _PageLoader:
    """Loads course pages in one tab and replaces the tab when it appears stuck.

    Each load waits for network idle and for the content area to show real text, and reloads a page
    that is still empty once with longer waits. A page that stays empty raises ``EmptyPageError``.

    After ``recover_after`` consecutive failures a fresh tab is opened and the current URL is
    retried. If the retry works, the URLs that failed during the streak are handed back through
    ``take_retries`` so the crawler can try them once more.
    """

    def __init__(self, context, origin: str, *, recover_after: int = 3, max_recoveries: int = 3,
                 settle_ms: int = 300, timeout_ms: int = 90000, idle_ms: int = 15000, ready_ms: int = 10000):
        self.context = context
        self.origin = origin
        self.recover_after = recover_after
        self.max_recoveries = max_recoveries
        self.settle_ms = settle_ms
        self.timeout_ms = timeout_ms
        self.idle_ms = idle_ms
        self.ready_ms = ready_ms
        self.page = None
        self.consecutive_failures = 0
        self.recoveries = 0
        self._streak: list[str] = []
        self._retries: list[str] = []

    async def fresh_page(self, *, reset_recoveries: bool = True):
        await _close_page(self.page)
        self.page = await self.context.new_page()
        self.consecutive_failures = 0
        self._streak = []
        if reset_recoveries:
            self.recoveries = 0
        return self.page

    async def close(self) -> None:
        await _close_page(self.page)
        self.page = None

    def take_retries(self) -> list[str]:
        retries, self._retries = self._retries, []
        return retries

    async def _open(self, url: str, *, patience: int = 1) -> dict:
        if self.page is None or self.page.is_closed():
            await self.fresh_page(reset_recoveries=False)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        await _ensure_logged_in(self.page, self.origin)
        await _settle(self.page, idle_ms=self.idle_ms * patience, ready_ms=self.ready_ms * patience)
        await self.page.wait_for_timeout(self.settle_ms)
        await _ensure_logged_in(self.page, self.origin)
        return await _visible_content(self.page)

    async def _attempt(self, url: str) -> dict:
        state = await self._open(url)
        if _is_empty_state(state):
            logger.info("  %s showed no content yet; reloading with a longer wait", redact_url(url))
            state = await self._open(url, patience=2)
            if _is_empty_state(state):
                raise EmptyPageError("page rendered no content (still 'Loading' after waiting and one reload)")
        return state

    async def load(self, url: str) -> dict:
        try:
            state = await self._attempt(url)
        except EmptyPageError:
            # The tab works (the page loaded); it just has nothing rendered.
            self.consecutive_failures = 0
            self._streak = []
            raise
        except BrowserConnectionError:
            raise
        except Exception as exc:
            self.consecutive_failures += 1
            self._streak.append(url)
            if self.consecutive_failures < self.recover_after or self.recoveries >= self.max_recoveries:
                raise PageLoadError(_error_reason(exc)) from exc
            self.recoveries += 1
            earlier = self._streak[:-1]
            logger.warning(
                "%d page loads failed in a row; opening a fresh tab and retrying (recovery %d/%d)",
                self.consecutive_failures, self.recoveries, self.max_recoveries,
            )
            await self.fresh_page(reset_recoveries=False)
            try:
                state = await self._attempt(url)
            except (BrowserConnectionError, EmptyPageError):
                raise
            except Exception as retry_exc:
                self.consecutive_failures = 1
                self._streak = [url]
                raise PageLoadError(f"{_error_reason(retry_exc)} (also failed in a fresh tab)") from retry_exc
            self._retries.extend(earlier)
        self.consecutive_failures = 0
        self._streak = []
        return state


async def _ensure_logged_in(page, origin: str | None = None):
    if re.search(r"/(?:login|signin)(?:/|$)", urlsplit(page.url).path, re.I):
        raise BrowserConnectionError("Canvas redirected to sign-in. Sign in in the connected browser, then run sync again.")
    if origin and urlsplit(page.url).netloc.lower() != urlsplit(origin).netloc.lower():
        raise BrowserConnectionError("Canvas left the course site, likely because the session expired. Sign in and retry.")


async def _courses(page, origin: str) -> list[tuple[str, str, str]]:
    await page.goto(urljoin(origin, "/courses"), wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(1200)
    await _ensure_logged_in(page, origin)
    state = await _visible_content(page)
    found = {}
    for item in state["links"]:
        course_id = _course_id(item["href"])
        if course_id:
            normalized = normalize_canvas_url(urljoin(origin, f"/courses/{course_id}"))
            found[course_id] = (course_id, item["text"] or f"Course {course_id}", normalized)
    if not found:
        raise BrowserConnectionError("No courses were visible on Canvas's Courses page. Open the course list and retry.")
    return list(found.values())


class _SkipLog:
    """Content deliberately not downloaded (videos, locked or oversized files). Not failures."""

    def __init__(self, catalog: Catalog | None = None, run_id: str | None = None):
        self.catalog = catalog
        self.run_id = run_id
        self.items: list[SyncFailure] = []

    def record(self, *, course_id: str, kind: str, url: str, reason: str, name: str = "") -> None:
        item = SyncFailure(str(course_id), kind, redact_url(url), redact_text(" ".join(str(reason).split()))[:500])
        self.items.append(item)
        logger.info("  skipped %s %s%s: %s", kind, name + " " if name else "", item.url, item.reason)
        if self.catalog is not None and self.run_id is not None:
            self.catalog.record_skip(run_id=self.run_id, course_id=item.course_id, kind=kind, url=item.url,
                                     reason=item.reason, name=name)


def _filename_from_response(response, fallback: str) -> str:
    disposition = (getattr(response, "headers", {}) or {}).get("content-disposition", "")
    match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I) or re.search(r'filename="?([^";]+)"?', disposition, re.I)
    return unquote(match.group(1).strip().strip('"')) if match else fallback


_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


async def _download_attachment(
    context, download_url: str, canonical_url: str, root: Path, course_id: str, catalog: Catalog,
    failures: _FailureLog | None = None, *, meta: dict | None = None, name_hint: str = "",
    skips: _SkipLog | None = None, attempts: int = 3, retry_delay: float = 2.0,
) -> str | None:
    """Download one Canvas file. Videos/audio are skipped (never fetched); other files are retried on
    network errors and 429/5xx answers, and every file that still cannot be saved is recorded with a reason."""
    meta = meta or {}
    name = meta.get("display_name") or name_hint or ""
    content_type = (meta.get("content-type") or "").lower()
    size = meta.get("size") if isinstance(meta.get("size"), int) else None

    def fail(reason: str) -> None:
        if failures is not None:
            failures.record(course_id=course_id, kind="attachment", url=canonical_url, reason=reason)
        return None

    def skip(kind: str, reason: str) -> None:
        if skips is not None:
            skips.record(course_id=course_id, kind=kind, url=canonical_url, reason=reason, name=name)
        if kind == "video":
            # An archive written before videos were skipped may still list this file.
            catalog.delete_attachment(canonical_url)
        return None

    def media_reason(kind: str, detail: str) -> str:
        return f"video/audio file ({detail}) - videos are not downloaded"

    if is_media_file(name=name, content_type=content_type, url=download_url) or meta.get("media_entry_id") and content_type.startswith(("video/", "audio/")):
        return skip("video", media_reason("video", content_type or Path(name).suffix or "media link"))
    if meta.get("locked_for_user"):
        return skip("locked", meta.get("lock_explanation") or "file is locked for you on Canvas")
    if size is not None and size > MAX_ATTACHMENT_BYTES:
        return skip("too-large", f"larger than 200 MB ({size} bytes)")

    async def fetch(url: str, **kwargs):
        last = "unknown error"
        for attempt in range(attempts):
            try:
                response = await context.request.get(url, timeout=120000, **kwargs)
            except Exception as exc:
                last = _error_reason(exc)
            else:
                if response.status not in _RETRY_STATUSES:
                    return response, None
                last = f"HTTP {response.status}"
            if attempt + 1 < attempts:
                logger.info("  retrying %s after %s (%d/%d)", redact_url(canonical_url), last, attempt + 1, attempts - 1)
                await asyncio.sleep(retry_delay * (attempt + 1))
        return None, f"{last} (after {attempts} attempts)"

    try:
        # Follow redirects one hop at a time: the storage URL Canvas redirects to names the file, so a
        # video is recognised (and skipped) before any bytes are fetched. The last hop is the download.
        url = download_url
        response = None
        for _hop in range(8):
            response, error = await fetch(url, max_redirects=0)
            if response is None:
                return fail(error)
            if not 300 <= response.status < 400:
                break
            location = (response.headers or {}).get("location", "")
            if not location:
                break
            url = urljoin(url, location)
            target_name = unquote(Path(urlsplit(url).path).name)
            if is_media_file(name=target_name, url=url):
                name = name or target_name
                return skip("video", media_reason("video", Path(target_name).suffix or "media link"))
            if target_name and "." in target_name and not name:
                name = target_name
        else:
            return fail("too many redirects")
        if not response.ok:
            return fail(f"HTTP {response.status}" + (" (file deleted or not visible to you)" if response.status == 404 else ""))
        headers = response.headers or {}
        length = int(headers.get("content-length", "0") or 0)
        response_type = headers.get("content-type", "").split(";", 1)[0].lower()
        raw_name = Path(urlsplit(response.url).path).name or "attachment"
        filename = _safe_filename(_filename_from_response(response, name or unquote(raw_name)))
        if is_media_file(name=filename, content_type=response_type):
            return skip("video", media_reason("video", response_type or Path(filename).suffix))
        if length > MAX_ATTACHMENT_BYTES:
            return skip("too-large", f"larger than 200 MB ({length} bytes)")
        if response_type in {"text/html", "application/xhtml+xml"} and not re.search(r"\.html?$", name or "", re.I):
            return fail("server returned an HTML page instead of a file (preview or access page)")
        name_path = Path(filename)
        suffix = name_path.suffix or mimetypes.guess_extension(response_type or content_type) or ".bin"
        filename = f"{name_path.stem}-{hashlib.sha1(canonical_url.encode()).hexdigest()[:8]}{suffix}"
        rel = Path("courses") / safe_component(course_id) / "files" / filename
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = await response.body()
        if len(data) > MAX_ATTACHMENT_BYTES:
            return skip("too-large", f"larger than 200 MB ({len(data)} bytes)")
        path.write_bytes(data)
        try:
            extracted = extract_attachment_text(path)
        except Exception as exc:
            logger.warning("Could not extract text from %s: %s", rel.as_posix(), _error_reason(exc))
            extracted = ""
        sidecar = path.with_name(path.name + ".md")
        if extracted:
            sidecar.write_text(extracted, encoding="utf-8")
        elif sidecar.exists():
            sidecar.unlink()
        catalog.save_attachment(
            url=canonical_url, course_id=course_id, filename=filename,
            relative_path=rel.as_posix(), sha256=hashlib.sha256(data).hexdigest(), extracted_text=extracted,
        )
        return rel.as_posix()
    except Exception as exc:
        return fail(_error_reason(exc))


async def _download_image(context, url: str, root: Path, course_id: str, failures: _FailureLog | None = None) -> str | None:
    def skip(reason: str) -> None:
        if failures is not None:
            failures.record(course_id=course_id, kind="image", url=url, reason=reason)
        return None

    try:
        response = await context.request.get(url, timeout=45000)
        if not response.ok:
            return skip(f"HTTP {response.status}")
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            return skip("response is not an image")
        size = int(response.headers.get("content-length", "0") or 0)
        if size > 25 * 1024 * 1024:
            return skip(f"larger than 25 MB ({size} bytes)")
        data = await response.body()
        if len(data) > 25 * 1024 * 1024:
            return skip(f"larger than 25 MB ({len(data)} bytes)")
        suffix = Path(urlsplit(response.url).path).suffix or mimetypes.guess_extension(response.headers["content-type"].split(";", 1)[0]) or ".img"
        name = f"{hashlib.sha1(normalize_canvas_url(url).encode()).hexdigest()[:12]}{suffix}"
        relative = (Path("courses") / safe_component(course_id) / "assets" / name).as_posix()
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return relative
    except Exception as exc:
        return skip(_error_reason(exc))


async def _crawl_course(loader: _PageLoader, *, origin: str, course_id: str, course_url: str, label: str,
                        failures: _FailureLog, max_pages: int = MAX_PAGES_PER_COURSE,
                        seeds: list[str] | None = None, skip: set[str] | None = None):
    """Breadth-first crawl of one course. Returns (captured pages, {canonical file URL: (download URL, aliases)}).

    ``seeds`` are queued after the course home (e.g. course tabs and links found in API bodies);
    URLs in ``skip`` are already covered by API documents and are not opened, but still count as known.
    """
    origin_host = urlsplit(origin).netloc.lower()
    skip = {normalize_canvas_url(url) for url in skip or ()}
    queue = deque([(course_url, 0)] + [(url, 1) for url in seeds or []])
    visited: set[str] = set()
    retried: set[str] = set()
    pending_failures: dict[str, str] = {}
    captured: list[dict] = []
    attachments: dict[str, tuple[str, set[str]]] = {}
    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        url = normalize_canvas_url(url)
        if url in visited or url in skip or not is_course_page_url(url, origin, course_id):
            continue
        visited.add(url)
        try:
            state = await loader.load(url)
        except PageLoadError as exc:
            pending_failures[url] = str(exc)
            logger.warning("Could not load page %s: %s", redact_url(url), exc)
            continue
        pending_failures.pop(url, None)
        for retry_url in loader.take_retries():
            if retry_url not in retried:
                retried.add(retry_url)
                visited.discard(retry_url)
                queue.appendleft((retry_url, depth))
        captured.append({"url": url, "title": state["title"] or url, "html": state["html"], "text": state["text"], "images": state["images"], "embeds": state["embeds"]})
        if len(captured) % 25 == 0:
            logger.info("  %s: %d pages captured, %d queued", label, len(captured), len(queue))
        for link in state["links"]:
            raw = urljoin(origin, link["href"])
            target = normalize_canvas_url(raw)
            same_host = urlsplit(target).netloc.lower() == origin_host
            canonical = canonical_attachment_url(target) if same_host else None
            if canonical:
                download_url, aliases = attachments.get(canonical, (None, set()))
                if download_url is None or (transient_access_params(raw) and not transient_access_params(download_url)):
                    download_url = attachment_download_url(canonical, raw)
                aliases.add(target)
                attachments[canonical] = (download_url, aliases)
            elif link["download"] and same_host and not is_media_file(name=link.get("text") or "", url=target):
                download_url, aliases = attachments.get(target, (raw.split("#", 1)[0], set()))
                aliases.add(target)
                attachments[target] = (download_url, aliases)
            elif is_course_page_url(target, origin, course_id) and target not in visited and target not in skip:
                queue.append((target, depth + 1))
    if queue and len(visited) >= max_pages:
        logger.warning("  %s: stopped at the %d-page limit with %d URLs still queued", label, max_pages, len(queue))
    for url, reason in pending_failures.items():
        # Already warned when the load failed; record it once it is clear no retry recovered it.
        failures.record(course_id=course_id, kind="page", url=url, reason=reason, log=False)
    return captured, attachments


def _merge_browser_files(collector: CourseCollector, attachments: dict[str, tuple[str, set[str]]]) -> dict[str, tuple[str, set[str]]]:
    """Fold Canvas file links found by the browser into the API file list (one entry per file id).
    Returns the remaining non-Canvas-file downloads."""
    other = {}
    for canonical, (download_url, aliases) in attachments.items():
        key = canvas_file_key(canonical)
        if key is None:
            other[canonical] = (download_url, aliases)
            continue
        link_course, file_id = key
        ref = collector.content.files.get(file_id)
        if ref is None:
            owner = link_course if link_course not in (None, collector.course_id) else None
            ref = collector.add_file(file_id, source="page link", link_course=owner)
            if canonical_attachment_url(ref.canonical_url) == canonical:
                ref.download_url = download_url
        ref.aliases.update(aliases)
        ref.aliases.add(canonical)
        if transient_access_params(download_url) and not transient_access_params(ref.download_url):
            ref.download_url = attachment_download_url(ref.canonical_url, download_url)
    return other


def _page_record(url: str, title: str, html_body: str, images=(), embeds=()) -> dict:
    return {"url": normalize_canvas_url(url), "title": title, "html": html_body, "images": list(images), "embeds": list(embeds)}


def _prune_course_pages(catalog: Catalog, course_id: str, saved: set[str], covered: set[str]) -> list[str]:
    """Drop rows this sync replaced: junk URLs, URLs now served by an API document or alias, and
    old "Loading" shells. Other pages that were not seen this time are kept (the archive keeps
    content that has since disappeared from Canvas)."""
    stale = []
    replaced = saved | covered
    for row in catalog.pages_for_course(course_id):
        url = row["url"]
        if url in saved:
            continue
        # A query variant (e.g. ?note_id=) of a page the API or this crawl now provides is an old copy.
        without_query = urlunsplit(urlsplit(url)._replace(query=""))
        if (is_junk_course_url(url) or url in covered or without_query in replaced
                or looks_like_shell(row["text"])):
            stale.append(url)
    if stale:
        catalog.delete_pages(stale)
    return stale


async def sync_canvas(*, root: Path, catalog: Catalog, cdp_url: str = "http://127.0.0.1:9222", only_course: str | None = None) -> SyncSummary:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserConnectionError("Install the browser extra with `pip install -e .[browser]`.") from exc

    summary = SyncSummary()
    run_id = catalog.start_run()
    failures = _FailureLog(catalog, run_id)
    skips = _SkipLog(catalog, run_id)
    summary.failures = failures.items
    summary.skipped = skips.items
    loader = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=5000)
            context = browser.contexts[0]
            # Derive Canvas origin from an already-open, authenticated Canvas tab.
            canvas_tab = None
            for candidate in context.pages:
                if "instructure.com" in urlsplit(candidate.url).netloc.lower() or "canvas" in urlsplit(candidate.url).netloc.lower():
                    canvas_tab = candidate
                    break
            if canvas_tab is None:
                raise BrowserConnectionError("No open Canvas tab was found in the connected browser. Open Canvas and retry.")
            origin = f"{urlsplit(canvas_tab.url).scheme}://{urlsplit(canvas_tab.url).netloc}"
            loader = _PageLoader(context, origin)
            page = await loader.fresh_page()
            courses = await _courses(page, origin)
            if only_course:
                wanted = _course_id(only_course) or only_course
                courses = [item for item in courses if item[0] == wanted]
                if not courses:
                    raise BrowserConnectionError(f"Course {wanted} is not visible in the signed-in account's course list.")
            logger.info("Found %d course(s) to sync", len(courses))
            origin_host = urlsplit(origin).netloc.lower()
            api = CanvasAPI(context.request, origin)
            for number, (course_id, course_title, course_url) in enumerate(courses, 1):
                label = f"[{number}/{len(courses)}] {' '.join(course_title.split())} ({course_id})"
                await _sync_course(
                    context=context, loader=loader, api=api, root=root, catalog=catalog, origin=origin,
                    origin_host=origin_host, course_id=course_id, course_title=course_title, course_url=course_url,
                    label=label, summary=summary, failures=failures, skips=skips,
                )
            await loader.close()
            return summary
    except SessionExpiredError as exc:
        if loader is not None:
            await loader.close()
        raise BrowserConnectionError(f"{exc} Sign in in the connected browser, then run sync again.") from exc
    except BrowserConnectionError:
        if loader is not None:
            await loader.close()
        raise
    except Exception as exc:
        if loader is not None:
            await loader.close()
        message = str(exc)
        if "connect" in message.lower() or "websocket" in message.lower() or "ECONNREFUSED" in message:
            raise BrowserConnectionError(
                f"Could not connect to {cdp_url}. Start Edge with remote debugging enabled, open Canvas, and retry."
            ) from exc
        raise
    finally:
        catalog.finish_run(
            run_id, courses=summary.courses, pages_captured=summary.pages_captured,
            pages_changed=summary.pages_changed, attachments=summary.attachments,
        )


async def _sync_course(*, context, loader, api, root, catalog, origin, origin_host, course_id, course_title, course_url,
                       label, summary, failures, skips) -> None:
    logger.info("%s: reading course contents from the Canvas API", label)
    failures_before = len(failures.items)
    collector = CourseCollector(api, origin, course_id)
    try:
        content = await collector.collect()
    except (SessionExpiredError, BrowserConnectionError):
        raise
    except Exception as exc:
        failures.record(course_id=course_id, kind="api", url=course_url,
                        reason=f"Canvas API read failed, using the page crawl only: {_error_reason(exc)}")
        content = collector.content
    if content.unavailable:
        logger.info("  %s: API endpoints not available to you: %s", label,
                    ", ".join(f"{key} ({status})" for key, status in sorted(content.unavailable.items()) if ":" not in key))
    logger.info("  %s: %d API documents, %d files referenced (%d API calls so far)", label, len(content.documents), len(content.files), api.calls)
    for media_url, title, reason in content.skipped_media:
        skips.record(course_id=course_id, kind="video", url=media_url, reason=reason, name=title)

    # The browser crawl supplements the API: course home, grades, people and anything the API
    # does not describe. URLs covered by API documents are not opened.
    logger.info("%s: crawling pages", label)
    await loader.fresh_page()
    covered = set(content.covered) | set(content.aliases)
    seeds = [url for url in content.browser_seeds + sorted(content.course_links) if url not in covered]
    captured, attachments = await _crawl_course(
        loader, origin=origin, course_id=course_id, course_url=course_url, label=label, failures=failures,
        seeds=seeds, skip=covered,
    )
    other_downloads = _merge_browser_files(collector, attachments)
    for item in captured:
        for image in item["images"]:
            if urlsplit(image["src"]).netloc.lower() == origin_host and canvas_file_key(image["src"]):
                key = canvas_file_key(image["src"])
                owner = key[0] if key[0] not in (None, course_id) else None
                collector.add_file(key[1], source="inline image", link=image["src"], link_course=owner)
    if any(ref.meta is None for ref in content.files.values()):
        await collector._resolve_file_metadata(True)
    collector._files_doc(getattr(collector, "folders", None))
    summary.courses += 1
    documents = [_page_record(doc.url, doc.title, doc.html) for doc in content.documents]
    summary.api_documents += len(documents)
    doc_urls = content.doc_urls()
    records = documents + [
        _page_record(item["url"], item["title"], item["html"], item["images"], item["embeds"])
        for item in captured if normalize_canvas_url(item["url"]) not in doc_urls
    ]
    summary.pages_captured += len(records)
    if not records:
        summary.empty_courses.append(course_id)
        failures.record(
            course_id=course_id, kind="course", url=course_url,
            reason=f"no pages captured; rerun with `course-rag sync --course {course_id}`",
        )
        write_course_index(root, course_id, course_title, catalog.pages_for_course(course_id))
        return

    page_base = Path("courses") / safe_component(course_id) / "pages"
    url_to_relative = {}
    for item in records:
        url_to_relative[item["url"]] = (page_base / f"{page_file_stem(item['url'])}.html").as_posix()
    for alias, target in content.aliases.items():
        if target in url_to_relative:
            url_to_relative[alias] = url_to_relative[target]

    refs = sorted(content.files.values(), key=lambda ref: ((ref.meta or {}).get("size") or 0, ref.file_id))
    if refs or other_downloads:
        logger.info("  %s: downloading %d unique file(s)", label, len(refs) + len(other_downloads))
    downloaded = 0
    for position, ref in enumerate(refs, 1):
        meta = ref.meta if ref.meta and "_status" not in ref.meta else None
        rel = await _download_attachment(
            context, ref.download_url, ref.canonical_url, root, course_id, catalog, failures,
            meta=meta, name_hint=ref.name, skips=skips,
        )
        if rel:
            downloaded += 1
            for alias in {ref.canonical_url, f"{origin}/files/{ref.file_id}", *ref.aliases}:
                url_to_relative[alias] = rel
        if position % 10 == 0:
            logger.info("  %s: %d/%d files processed", label, position, len(refs))
    for canonical, (download_url, aliases) in sorted(other_downloads.items()):
        rel = await _download_attachment(context, download_url, canonical, root, course_id, catalog, failures, skips=skips)
        if rel:
            downloaded += 1
            url_to_relative[canonical] = rel
            for alias in aliases:
                url_to_relative[alias] = rel
    summary.attachments += downloaded
    image_urls = {image["src"] for item in records for image in item["images"]
                  if urlsplit(image["src"]).netloc.lower() == origin_host and not canvas_file_key(image["src"])}
    image_urls |= {src for src in content.images if not canvas_file_key(src)}
    for image_url in sorted(image_urls):
        rel = await _download_image(context, image_url, root, course_id, failures)
        if rel:
            url_to_relative[image_url] = rel
    changed_before = summary.pages_changed
    local_targets = {remote: posixpath.relpath(target, page_base.as_posix()) for remote, target in url_to_relative.items()}
    saved_urls = set()
    for item in records:
        body = item["html"]
        media_links = []
        for embed in item["embeds"]:
            if urlsplit(embed["src"]).scheme in {"http", "https"}:
                media_url = normalize_canvas_url(embed["src"])
                media_links.append(
                    f'<p>External media: <a href="{html.escape(media_url, quote=True)}">'
                    f'{html.escape(embed["title"])}</a></p>'
                )
        if media_links:
            body += "\n" + "\n".join(media_links)
        markdown = redact_text(html_to_markdown(body))
        saved = save_page(
            root=root, course_id=course_id, page_id=page_file_stem(item["url"]), title=item["title"], url=item["url"],
            body=body, markdown=markdown, local_targets=local_targets,
        )
        changed = catalog.save_page(
            url=item["url"], course_id=course_id, title=item["title"],
            relative_path=saved.relative_html_path, text=markdown, fingerprint=body,
        )
        saved_urls.add(item["url"])
        if changed:
            summary.pages_changed += 1
    pruned = _prune_course_pages(catalog, course_id, saved_urls, covered)
    for row in catalog.attachments_for_course(course_id):
        if is_media_file(name=row["filename"]):
            catalog.delete_attachment(row["url"])
    write_course_index(root, course_id, course_title, catalog.pages_for_course(course_id))
    logger.info(
        "  %s: done - %d documents (%d from the API, %d updated, %d stale rows removed), %d/%d files, %d failures",
        label, len(records), len(documents), summary.pages_changed - changed_before, len(pruned), downloaded,
        len(refs) + len(other_downloads), len(failures.items) - failures_before,
    )
