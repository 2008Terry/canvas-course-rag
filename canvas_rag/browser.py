from __future__ import annotations

import hashlib
import html
import logging
import mimetypes
import posixpath
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from .archive import page_file_stem, safe_component, save_page, write_course_index
from .catalog import Catalog
from .extract import extract_attachment_text, html_to_markdown
from .urls import canonical_attachment_url, normalize_canvas_url, redact_text, redact_url, transient_access_params

logger = logging.getLogger("canvas_rag")

MAX_PAGES_PER_COURSE = 1000


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
    failures: list[SyncFailure] = field(default_factory=list)
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
    path = parts.path.rstrip("/")
    if re.search(r"(?:^|/)api(?:/|$)", path, re.I):
        return False
    if not re.match(rf"^/courses/{re.escape(str(course_id))}(?:/|$)", path):
        return False
    if re.search(r"/assignments/\d+/(?:submissions|moderate)(?:/|$)", path):
        return False
    if re.search(r"/quizzes/\d+/(?:take|history)(?:/|$)", path):
        return False
    return True


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


async def _visible_content(page):
    return await page.evaluate("""() => {
      const main = document.querySelector('main,[role="main"],#content,#main') || document.body;
      const links = [...document.querySelectorAll('a[href]')].filter(a => {
        const s = getComputedStyle(a); return a.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none';
      }).map(a => ({href:a.href, text:(a.innerText || a.getAttribute('aria-label') || '').trim(), download:a.hasAttribute('download')}));
      const images = [...main.querySelectorAll('img[src]')].map(i => ({src:i.src, alt:i.alt || ''}));
      const embeds = [...main.querySelectorAll('iframe[src]')].map(i => ({src:i.src, title:i.title || 'External media'}));
      return {html:main.innerHTML, links, images, embeds, title:document.title, text:main.innerText || ''};
    }""")


async def _close_page(page):
    if page is not None and not page.is_closed():
        try:
            await page.close()
        except Exception:
            pass


class _PageLoader:
    """Loads course pages in one tab and replaces the tab when it appears stuck.

    After ``recover_after`` consecutive failures a fresh tab is opened and the current URL is
    retried. If the retry works, the URLs that failed during the streak are handed back through
    ``take_retries`` so the crawler can try them once more.
    """

    def __init__(self, context, origin: str, *, recover_after: int = 3, max_recoveries: int = 3,
                 settle_ms: int = 500, timeout_ms: int = 90000):
        self.context = context
        self.origin = origin
        self.recover_after = recover_after
        self.max_recoveries = max_recoveries
        self.settle_ms = settle_ms
        self.timeout_ms = timeout_ms
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

    async def _attempt(self, url: str) -> dict:
        if self.page is None or self.page.is_closed():
            await self.fresh_page(reset_recoveries=False)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        await self.page.wait_for_timeout(self.settle_ms)
        await _ensure_logged_in(self.page, self.origin)
        return await _visible_content(self.page)

    async def load(self, url: str) -> dict:
        try:
            state = await self._attempt(url)
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
            except BrowserConnectionError:
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


async def _download_attachment(
    context, download_url: str, canonical_url: str, root: Path, course_id: str, catalog: Catalog,
    failures: _FailureLog | None = None,
) -> str | None:
    def skip(reason: str) -> None:
        if failures is not None:
            failures.record(course_id=course_id, kind="attachment", url=canonical_url, reason=reason)
        return None

    try:
        response = await context.request.get(download_url, timeout=90000)
        if not response.ok:
            return skip(f"HTTP {response.status}")
        length = int(response.headers.get("content-length", "0") or 0)
        if length > 200 * 1024 * 1024:
            return skip(f"larger than 200 MB ({length} bytes)")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type in {"text/html", "application/xhtml+xml"}:
            return skip("server returned an HTML page instead of a file (preview or access page)")
        raw_name = Path(urlsplit(response.url).path).name or "attachment"
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
        filename = _safe_filename(match.group(1) if match else raw_name)
        name_path = Path(filename)
        filename = f"{name_path.stem}-{hashlib.sha1(canonical_url.encode()).hexdigest()[:8]}{name_path.suffix}"
        suffix = Path(filename).suffix or mimetypes.guess_extension(content_type) or ".bin"
        if not Path(filename).suffix:
            filename += suffix
        rel = Path("courses") / safe_component(course_id) / "files" / filename
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = await response.body()
        if len(data) > 200 * 1024 * 1024:
            return skip(f"larger than 200 MB ({len(data)} bytes)")
        path.write_bytes(data)
        try:
            extracted = extract_attachment_text(path)
        except Exception as exc:
            logger.warning("Could not extract text from %s: %s", rel.as_posix(), _error_reason(exc))
            extracted = ""
        if extracted:
            path.with_name(path.name + ".md").write_text(extracted, encoding="utf-8")
        catalog.save_attachment(
            url=canonical_url, course_id=course_id, filename=filename,
            relative_path=rel.as_posix(), sha256=hashlib.sha256(data).hexdigest(), extracted_text=extracted,
        )
        return rel.as_posix()
    except Exception as exc:
        return skip(_error_reason(exc))


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
                        failures: _FailureLog, max_pages: int = MAX_PAGES_PER_COURSE):
    """Breadth-first crawl of one course. Returns (captured pages, {canonical file URL: (download URL, aliases)})."""
    origin_host = urlsplit(origin).netloc.lower()
    queue = deque([(course_url, 0)])
    visited: set[str] = set()
    retried: set[str] = set()
    pending_failures: dict[str, str] = {}
    captured: list[dict] = []
    attachments: dict[str, tuple[str, set[str]]] = {}
    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        url = normalize_canvas_url(url)
        if url in visited or not is_course_page_url(url, origin, course_id):
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
            elif link["download"] and same_host:
                download_url, aliases = attachments.get(target, (raw.split("#", 1)[0], set()))
                aliases.add(target)
                attachments[target] = (download_url, aliases)
            elif is_course_page_url(target, origin, course_id) and target not in visited:
                queue.append((target, depth + 1))
    if queue and len(visited) >= max_pages:
        logger.warning("  %s: stopped at the %d-page limit with %d URLs still queued", label, max_pages, len(queue))
    for url, reason in pending_failures.items():
        # Already warned when the load failed; record it once it is clear no retry recovered it.
        failures.record(course_id=course_id, kind="page", url=url, reason=reason, log=False)
    return captured, attachments


async def sync_canvas(*, root: Path, catalog: Catalog, cdp_url: str = "http://127.0.0.1:9222", only_course: str | None = None) -> SyncSummary:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserConnectionError("Install the browser extra with `pip install -e .[browser]`.") from exc

    summary = SyncSummary()
    run_id = catalog.start_run()
    failures = _FailureLog(catalog, run_id)
    summary.failures = failures.items
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
            for number, (course_id, course_title, course_url) in enumerate(courses, 1):
                label = f"[{number}/{len(courses)}] {' '.join(course_title.split())} ({course_id})"
                logger.info("%s: crawling", label)
                # A long download phase can leave the previous tab unresponsive; start each course fresh.
                await loader.fresh_page()
                failures_before = len(failures.items)
                captured, attachments = await _crawl_course(
                    loader, origin=origin, course_id=course_id, course_url=course_url, label=label, failures=failures,
                )
                summary.courses += 1
                summary.pages_captured += len(captured)
                if not captured:
                    summary.empty_courses.append(course_id)
                    failures.record(
                        course_id=course_id, kind="course", url=course_url,
                        reason=f"no pages captured; rerun with `course-rag sync --course {course_id}`",
                    )
                    write_course_index(root, course_id, course_title, catalog.pages_for_course(course_id))
                    continue
                url_to_relative = {}
                for item in captured:
                    url_to_relative[item["url"]] = (Path("courses") / safe_component(course_id) / "pages" / f"{page_file_stem(item['url'])}.html").as_posix()
                page_base = Path("courses") / safe_component(course_id) / "pages"
                if attachments:
                    logger.info("  %s: downloading %d unique attachment(s)", label, len(attachments))
                downloaded = 0
                for position, canonical in enumerate(sorted(attachments), 1):
                    download_url, aliases = attachments[canonical]
                    rel = await _download_attachment(context, download_url, canonical, root, course_id, catalog, failures)
                    if rel:
                        downloaded += 1
                        url_to_relative[canonical] = rel
                        for alias in aliases:
                            url_to_relative[alias] = rel
                    if position % 10 == 0:
                        logger.info("  %s: %d/%d attachments processed", label, position, len(attachments))
                summary.attachments += downloaded
                image_urls = {image["src"] for item in captured for image in item["images"]
                              if urlsplit(image["src"]).netloc.lower() == origin_host}
                for image_url in sorted(image_urls):
                    rel = await _download_image(context, image_url, root, course_id, failures)
                    if rel:
                        url_to_relative[image_url] = rel
                changed_before = summary.pages_changed
                for item in captured:
                    local_targets = {
                        remote: posixpath.relpath(target, page_base.as_posix())
                        for remote, target in url_to_relative.items()
                    }
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
                    saved = save_page(
                        root=root, course_id=course_id, page_id=page_file_stem(item["url"]), title=item["title"], url=item["url"],
                        body=body, markdown=html_to_markdown(body), local_targets=local_targets,
                    )
                    changed = catalog.save_page(
                        url=item["url"], course_id=course_id, title=item["title"],
                        relative_path=saved.relative_html_path, text=html_to_markdown(body), fingerprint=body,
                    )
                    if changed:
                        summary.pages_changed += 1
                write_course_index(root, course_id, course_title, catalog.pages_for_course(course_id))
                logger.info(
                    "  %s: done - %d pages (%d updated), %d/%d attachments, %d skipped",
                    label, len(captured), summary.pages_changed - changed_before, downloaded, len(attachments),
                    len(failures.items) - failures_before,
                )
            await loader.close()
            return summary
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
