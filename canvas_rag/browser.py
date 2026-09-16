from __future__ import annotations

import asyncio
import hashlib
import html
import mimetypes
import posixpath
import re
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .archive import safe_component, save_page, write_course_index
from .catalog import Catalog
from .extract import extract_attachment_text, html_to_markdown


class BrowserConnectionError(RuntimeError):
    pass


def normalize_canvas_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {
                 "fbclid", "gclid", "token", "access_token", "signature", "sig", "credential",
                 "auth", "authorization", "password", "session", "secret", "code", "ticket", "jwt",
             }]
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host.lower()}:{parts.port}" if parts.port else host.lower()
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), urlencode(query), ""))


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
    if re.search(r"/(?:courses/\d+/)?files/\d+(?:/|$)", parts.path):
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/download", parts.query, ""))
    return None


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


async def _download_attachment(context, url: str, root: Path, course_id: str, catalog: Catalog) -> str | None:
    try:
        response = await context.request.get(url, timeout=90000)
        if not response.ok:
            return None
        length = int(response.headers.get("content-length", "0") or 0)
        if length > 200 * 1024 * 1024:
            return None
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type in {"text/html", "application/xhtml+xml"}:
            return None
        raw_name = Path(urlsplit(response.url).path).name or "attachment"
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
        filename = _safe_filename(match.group(1) if match else raw_name)
        name_path = Path(filename)
        filename = f"{name_path.stem}-{hashlib.sha1(url.encode()).hexdigest()[:8]}{name_path.suffix}"
        suffix = Path(filename).suffix or mimetypes.guess_extension(content_type) or ".bin"
        if not Path(filename).suffix:
            filename += suffix
        rel = Path("courses") / safe_component(course_id) / "files" / filename
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = await response.body()
        if len(data) > 200 * 1024 * 1024:
            return None
        path.write_bytes(data)
        try:
            extracted = extract_attachment_text(path)
        except Exception:
            extracted = ""
        if extracted:
            path.with_name(path.name + ".md").write_text(extracted, encoding="utf-8")
        catalog.save_attachment(
            url=normalize_canvas_url(url), course_id=course_id, filename=filename,
            relative_path=rel.as_posix(), sha256=hashlib.sha256(data).hexdigest(), extracted_text=extracted,
        )
        return rel.as_posix()
    except Exception:
        return None


async def _download_image(context, url: str, root: Path, course_id: str) -> str | None:
    try:
        response = await context.request.get(url, timeout=45000)
        if not response.ok or not response.headers.get("content-type", "").lower().startswith("image/"):
            return None
        size = int(response.headers.get("content-length", "0") or 0)
        if size > 25 * 1024 * 1024:
            return None
        data = await response.body()
        if len(data) > 25 * 1024 * 1024:
            return None
        suffix = Path(urlsplit(response.url).path).suffix or mimetypes.guess_extension(response.headers["content-type"].split(";", 1)[0]) or ".img"
        name = f"{hashlib.sha1(url.encode()).hexdigest()[:12]}{suffix}"
        relative = (Path("courses") / safe_component(course_id) / "assets" / name).as_posix()
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return relative
    except Exception:
        return None


async def sync_canvas(*, root: Path, catalog: Catalog, cdp_url: str = "http://127.0.0.1:9222", only_course: str | None = None) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserConnectionError("Install the browser extra with `pip install -e .[browser]`.") from exc

    try:
        page = None
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=5000)
            context = browser.contexts[0]
            page = await context.new_page()
            # Derive Canvas origin from an already-open, authenticated Canvas tab.
            canvas_tab = None
            for candidate in context.pages:
                if "instructure.com" in urlsplit(candidate.url).netloc.lower() or "canvas" in urlsplit(candidate.url).netloc.lower():
                    canvas_tab = candidate
                    break
            if canvas_tab is None:
                raise BrowserConnectionError("No open Canvas tab was found in the connected browser. Open Canvas and retry.")
            origin = f"{urlsplit(canvas_tab.url).scheme}://{urlsplit(canvas_tab.url).netloc}"
            courses = await _courses(page, origin)
            if only_course:
                wanted = _course_id(only_course) or only_course
                courses = [item for item in courses if item[0] == wanted]
                if not courses:
                    raise BrowserConnectionError(f"Course {wanted} is not visible in the signed-in account's course list.")
            count = 0
            for course_id, course_title, course_url in courses:
                queue = deque([(course_url, 0)])
                visited: set[str] = set()
                captured: list[dict] = []
                attachments: set[str] = set()
                attachment_aliases: dict[str, set[str]] = {}
                while queue and len(visited) < 1000:
                    url, depth = queue.popleft()
                    url = normalize_canvas_url(url)
                    if url in visited or not is_course_page_url(url, origin, course_id):
                        continue
                    visited.add(url)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                        await page.wait_for_timeout(500)
                        await _ensure_logged_in(page, origin)
                        state = await _visible_content(page)
                    except BrowserConnectionError:
                        raise
                    except Exception:
                        continue
                    captured.append({"url": url, "title": state["title"] or url, "html": state["html"], "text": state["text"], "images": state["images"], "embeds": state["embeds"]})
                    for link in state["links"]:
                        target = normalize_canvas_url(urljoin(origin, link["href"]))
                        file_url = _canvas_file_download_url(target)
                        if file_url and urlsplit(target).netloc.lower() == urlsplit(origin).netloc.lower():
                            attachments.add(file_url)
                            attachment_aliases.setdefault(file_url, set()).add(target)
                        elif link["download"] and urlsplit(target).netloc.lower() == urlsplit(origin).netloc.lower():
                            attachments.add(target)
                            attachment_aliases.setdefault(target, set()).add(target)
                        elif is_course_page_url(target, origin, course_id) and target not in visited:
                            queue.append((target, depth + 1))
                url_to_relative = {}
                for item in captured:
                    pid = re.search(r"/pages/([^/]+)", urlsplit(item["url"]).path)
                    page_id = pid.group(1) if pid else hashlib.sha1(item["url"].encode()).hexdigest()[:12]
                    url_to_relative[item["url"]] = (Path("courses") / safe_component(course_id) / "pages" / f"{safe_component(page_id)}.html").as_posix()
                page_base = Path("courses") / safe_component(course_id) / "pages"
                for attach_url in sorted(attachments):
                    rel = await _download_attachment(context, attach_url, root, course_id, catalog)
                    if rel:
                        url_to_relative[attach_url] = rel
                        for alias in attachment_aliases.get(attach_url, set()):
                            url_to_relative[alias] = rel
                origin_host = urlsplit(origin).netloc.lower()
                image_urls = {image["src"] for item in captured for image in item["images"]
                              if urlsplit(image["src"]).netloc.lower() == origin_host}
                for image_url in sorted(image_urls):
                    rel = await _download_image(context, image_url, root, course_id)
                    if rel:
                        url_to_relative[image_url] = rel
                for item in captured:
                    pid = re.search(r"/pages/([^/]+)", urlsplit(item["url"]).path)
                    page_id = pid.group(1) if pid else hashlib.sha1(item["url"].encode()).hexdigest()[:12]
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
                        root=root, course_id=course_id, page_id=page_id, title=item["title"], url=item["url"],
                        body=body, markdown=html_to_markdown(body), local_targets=local_targets,
                    )
                    changed = catalog.save_page(
                        url=item["url"], course_id=course_id, title=item["title"],
                        relative_path=saved.relative_html_path, text=html_to_markdown(body), fingerprint=body,
                    )
                    if changed:
                        count += 1
                write_course_index(root, course_id, course_title, catalog.pages_for_course(course_id))
            await _close_page(page)
            return count
    except BrowserConnectionError:
        await _close_page(page)
        raise
    except Exception as exc:
        await _close_page(page)
        message = str(exc)
        if "connect" in message.lower() or "websocket" in message.lower() or "ECONNREFUSED" in message:
            raise BrowserConnectionError(
                f"Could not connect to {cdp_url}. Start Edge with remote debugging enabled, open Canvas, and retry."
            ) from exc
        raise
