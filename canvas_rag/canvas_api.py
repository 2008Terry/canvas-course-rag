"""Read-only Canvas REST API collection through the signed-in browser session.

Only GET requests are sent. List endpoints are used for announcements and discussions because
they return the full message without marking anything as read. No token is ever created or
stored: requests reuse the browser context's own session, exactly like the pages it renders.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

from .urls import (
    canonical_attachment_url, canvas_file_key, normalize_canvas_url, redact_text, redact_url, transient_access_params,
)

logger = logging.getLogger("canvas_rag")

JSON_PREFIX = "while(1);"
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ogv",
    # Audio-only recordings are lecture/session media as well and are skipped the same way.
    ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".oga", ".flac", ".wma",
}
MEDIA_HOST_HINTS = ("kaltura", "mivideo", "mediaspace", "media_gallery", "mediagallery")


class SessionExpiredError(RuntimeError):
    """Canvas answered 401 or sent the API request to the sign-in page."""


def is_media_file(*, name: str = "", content_type: str = "", url: str = "") -> bool:
    """Video/audio by content type, file extension or a Kaltura/Media Gallery URL."""
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if content_type.startswith(("video/", "audio/")):
        return True
    for candidate in (name or "", urlsplit(url or "").path):
        suffix = re.search(r"(\.[A-Za-z0-9]{1,5})$", candidate.strip())
        if suffix and suffix.group(1).lower() in VIDEO_EXTENSIONS:
            return True
    lowered = (url or "").lower()
    return any(hint in lowered for hint in MEDIA_HOST_HINTS) or "/media_objects" in lowered or "/media_attachments" in lowered


def parse_json_body(text: str):
    text = text or ""
    if text.startswith(JSON_PREFIX):
        text = text[len(JSON_PREFIX):]
    return json.loads(text) if text.strip() else None


def next_link(link_header: str | None) -> str | None:
    for part in (link_header or "").split(","):
        match = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', part)
        if match:
            return match.group(1)
    return None


@dataclass
class ApiResult:
    data: object = None
    status: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class CanvasAPI:
    """GET-only JSON client that reuses the browser context's signed-in session."""

    def __init__(self, request, origin: str, *, delay: float = 0.05, max_pages: int = 50, timeout_ms: int = 60000):
        self.request = request
        self.origin = origin.rstrip("/")
        self.delay = delay
        self.max_pages = max_pages
        self.timeout_ms = timeout_ms
        self.calls = 0

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else self.origin + path

    async def _get(self, url: str):
        last_exc = None
        for attempt in range(3):
            try:
                self.calls += 1
                response = await self.request.get(url, headers={"Accept": "application/json"}, timeout=self.timeout_ms)
            except Exception as exc:  # network hiccup; retry with backoff
                last_exc = exc
                await asyncio.sleep(self.delay + 1.5 * (attempt + 1))
                continue
            if self.delay:
                await asyncio.sleep(self.delay)
            status = response.status
            final = getattr(response, "url", url) or url
            if status == 401 or re.search(r"/(?:login|signin)(?:/|$)", urlsplit(final).path, re.I):
                raise SessionExpiredError("Canvas API answered 401 / redirected to sign-in; the browser session has expired.")
            if status == 403:
                body = await _text(response)
                if "rate limit" in body.lower() and attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
            if status in {429, 500, 502, 503, 504} and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return response
        raise last_exc or RuntimeError(f"GET {redact_url(url)} failed")

    async def get(self, path: str) -> ApiResult:
        response = await self._get(self._url(path))
        if not response.ok:
            return ApiResult(None, response.status)
        try:
            return ApiResult(parse_json_body(await _text(response)), response.status)
        except ValueError:
            return ApiResult(None, 598)

    async def get_all(self, path: str) -> ApiResult:
        url = self._url(path)
        items: list = []
        status = 0
        for _ in range(self.max_pages):
            response = await self._get(url)
            status = response.status
            if not response.ok:
                return ApiResult(items or None, status) if not items else ApiResult(items, 200)
            try:
                data = parse_json_body(await _text(response))
            except ValueError:
                return ApiResult(None, 598)
            if not isinstance(data, list):
                return ApiResult(data, status)
            items.extend(data)
            headers = getattr(response, "headers", {}) or {}
            url = next_link(headers.get("link") or headers.get("Link"))
            if not url:
                break
        return ApiResult(items, status)


async def _text(response) -> str:
    text = getattr(response, "text", None)
    if callable(text):
        return await text()
    return (await response.body()).decode("utf-8", "replace")


# ---------------------------------------------------------------------------------------------
# Link extraction from HTML bodies

_URL_ATTRIBUTE = re.compile(r"""(\b(?:href|src|data-api-endpoint|data-download-url|data-url)\s*=\s*)(["'])(.*?)\2""", re.I | re.S)


def strip_grants(body: str) -> str:
    """Remove verifier=/access_token= and other credential parameters from every URL in an HTML body,
    relative links included."""
    def clean(match):
        value = html.unescape(match.group(3))
        cleaned = redact_url(value)
        if cleaned == value:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{html.escape(cleaned, quote=True)}{match.group(2)}"
    return redact_text(_URL_ATTRIBUTE.sub(clean, body or ""))



class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[tuple[str, str]] = []  # (attribute, value)

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if value and key in {"href", "src", "data-api-endpoint", "data-download-url"}:
                self.hrefs.append((f"{tag}.{key}", value))


def html_links(body: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    try:
        parser.feed(body or "")
    except Exception:
        pass
    return parser.hrefs


# ---------------------------------------------------------------------------------------------
# Course collection


@dataclass
class ApiDocument:
    url: str
    title: str
    html: str
    kind: str


@dataclass
class FileRef:
    file_id: str
    canonical_url: str
    download_url: str
    aliases: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    name: str = ""
    meta: dict | None = None


@dataclass
class CourseContent:
    course_id: str
    name: str = ""
    documents: list[ApiDocument] = field(default_factory=list)
    covered: set[str] = field(default_factory=set)          # URLs the browser must not visit
    aliases: dict[str, str] = field(default_factory=dict)   # e.g. module item URL -> content URL
    files: dict[str, FileRef] = field(default_factory=dict)  # Canvas file id -> reference
    browser_seeds: list[str] = field(default_factory=list)
    course_links: set[str] = field(default_factory=set)     # same-course URLs found in bodies
    images: set[str] = field(default_factory=set)           # same-host non-file images in bodies
    skipped_media: list[tuple[str, str, str]] = field(default_factory=list)  # (url, title, reason)
    unavailable: dict[str, int] = field(default_factory=dict)

    def doc_urls(self) -> set[str]:
        return {doc.url for doc in self.documents}


_TZ = None


def _local_time(value: str | None) -> str:
    global _TZ
    if not value:
        return ""
    try:
        if _TZ is None:
            from zoneinfo import ZoneInfo
            _TZ = ZoneInfo("America/New_York")
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(_TZ)
        return moment.strftime("%a %b %d, %Y %I:%M %p ET").replace(" 0", " ")
    except Exception:
        return value


def _esc(value) -> str:
    return html.escape(" ".join(str(value).split()) if value is not None else "")


def _meta_list(pairs: list[tuple[str, object]]) -> str:
    rows = [f"<li><strong>{_esc(label)}:</strong> {_esc(value)}</li>" for label, value in pairs if value not in (None, "", [], False)]
    return f"<ul class=\"canvas-meta\">{''.join(rows)}</ul>" if rows else ""


class CourseCollector:
    def __init__(self, api: CanvasAPI, origin: str, course_id: str):
        self.api = api
        self.origin = origin.rstrip("/")
        self.course_id = str(course_id)
        self.base = f"/api/v1/courses/{self.course_id}"
        self.content = CourseContent(self.course_id)
        self.host = urlsplit(origin).netloc.lower()
        self.folders: list | None = None

    def course_url(self, suffix: str = "") -> str:
        return normalize_canvas_url(f"{self.origin}/courses/{self.course_id}{suffix}")

    async def _list(self, key: str, path: str):
        result = await self.api.get_all(path)
        if result.ok and isinstance(result.data, list):
            return result.data
        self.content.unavailable[key] = result.status
        return None

    async def _one(self, key: str, path: str):
        result = await self.api.get(path)
        if result.ok and isinstance(result.data, dict):
            return result.data
        self.content.unavailable[key] = result.status
        return None

    # -- references ---------------------------------------------------------------------------

    def add_file(self, file_id: str, *, source: str, link: str | None = None, name: str = "", meta: dict | None = None,
                 link_course: str | None = None) -> FileRef:
        file_id = str(file_id)
        ref = self.content.files.get(file_id)
        if ref is None:
            owner = link_course or self.course_id
            canonical = f"{self.origin}/courses/{owner}/files/{file_id}"
            ref = FileRef(file_id, canonical, f"{canonical}/download?download_frd=1")
            self.content.files[file_id] = ref
        if link:
            absolute = urljoin(self.origin + "/", link)
            ref.aliases.add(normalize_canvas_url(absolute))
            alias_canonical = canonical_attachment_url(absolute)
            if alias_canonical:
                ref.aliases.add(alias_canonical)
            grant = transient_access_params(absolute)
            if grant and not transient_access_params(ref.download_url):
                from urllib.parse import urlencode
                ref.download_url = f"{ref.canonical_url}/download?{urlencode([('download_frd', '1'), grant[0]])}"
        ref.sources.add(source)
        if name and not ref.name:
            ref.name = name
        if meta and not ref.meta:
            ref.meta = meta
        return ref

    def scan_html(self, body: str | None, source: str) -> None:
        for attribute, value in html_links(body or ""):
            value = value.strip()
            if value.lower().startswith(("javascript:", "mailto:", "data:", "#")):
                continue
            absolute = urljoin(self.origin + "/", value)
            parts = urlsplit(absolute)
            if parts.netloc.lower() != self.host:
                continue
            path = parts.path
            api_file = re.match(r"^/api/v1/(?:courses/(\d+)/)?files/(\d+)", path)
            key = canvas_file_key(absolute)
            if key or api_file:
                link_course, file_id = key if key else (api_file.group(1), api_file.group(2))
                if link_course not in (None, self.course_id):
                    # A link into another course (usually left by a course copy); keep its own route.
                    self.add_file(file_id, source=source, link=value if key else None, link_course=link_course)
                else:
                    self.add_file(file_id, source=source, link=value if key else None)
                continue
            if attribute.startswith("img.") and not path.startswith("/api/"):
                self.content.images.add(absolute.split("#", 1)[0])
                continue
            if re.match(rf"^/courses/{self.course_id}(?:/|$)", path) and attribute.endswith("href"):
                self.content.course_links.add(normalize_canvas_url(absolute))

    def add_doc(self, url: str, title: str, body: str, kind: str) -> None:
        url = normalize_canvas_url(url)
        # Bodies come from Canvas with verifier= grants on file links; strip them before storage.
        body = strip_grants(body or "")
        self.content.documents = [doc for doc in self.content.documents if doc.url != url]
        self.content.documents.append(ApiDocument(url, " ".join((title or url).split()), body, kind))
        self.content.covered.add(url)

    # -- collection ---------------------------------------------------------------------------

    async def collect(self) -> CourseContent:
        course = await self._one("course", f"{self.base}?include[]=syllabus_body&include[]=term")
        if course is None:
            return self.content
        self.content.name = course.get("name") or ""
        await self._syllabus(course)
        tabs = await self._list("tabs", f"{self.base}/tabs") or []
        submissions = {str(s.get("assignment_id")): s for s in await self._list(
            "submissions", f"{self.base}/students/submissions?student_ids[]=self&include[]=submission_comments&include[]=rubric_assessment&per_page=100"
        ) or []}
        assignments = await self._list("assignments", f"{self.base}/assignments?per_page=100") or []
        groups = {str(g["id"]): g.get("name") for g in await self._list("assignment_groups", f"{self.base}/assignment_groups?per_page=100") or []}
        quizzes = await self._list("quizzes", f"{self.base}/quizzes?per_page=100")
        quiz_docs = set()
        for quiz in quizzes or []:
            self._quiz(quiz)
            quiz_docs.add(str(quiz.get("id")))
        pages = await self._list("pages", f"{self.base}/pages?per_page=100&include[]=body")
        for page in pages or []:
            if "body" not in page and page.get("url"):
                page = await self._one(f"page:{page['url']}", f"{self.base}/pages/{quote(page['url'])}") or page
            self._page(page)
        front = await self.api.get(f"{self.base}/front_page")
        if front.ok and isinstance(front.data, dict) and front.data.get("url"):
            self._page(front.data)
        announcements = await self._list("announcements", f"{self.base}/discussion_topics?only_announcements=true&per_page=100") or []
        discussions = await self._list("discussions", f"{self.base}/discussion_topics?per_page=100") or []
        modules = await self._list("modules", f"{self.base}/modules?include[]=items&include[]=content_details&per_page=100")
        # Module items can point at content the list endpoints did not return (hidden Pages/Quizzes tabs).
        known_assignments = {str(a.get("id")) for a in assignments}
        for module in modules or []:
            # Canvas may omit inline items for large modules; list them separately then.
            if "items" not in module or (module.get("items_count") or 0) > len(module.get("items") or []):
                items = await self._list(f"module:{module.get('id')}",
                                         f"{self.base}/modules/{module.get('id')}/items?include[]=content_details&per_page=100")
                if items is not None:
                    module["items"] = items
            if module.get("id"):
                # /modules/<id> is an anchor into the Modules page, which the Modules document replaces.
                module_url = self.course_url(f"/modules/{module['id']}")
                self.content.covered.add(module_url)
                self.content.aliases[module_url] = self.course_url("/modules")
            for item in module.get("items") or []:
                await self._module_item(item, known_assignments, quiz_docs, assignments)
        for assignment in assignments:
            self._assignment(assignment, submissions.get(str(assignment.get("id"))), groups, quiz_docs)
        for topic in announcements:
            await self._topic(topic, "announcement")
        announcement_ids = {str(t.get("id")) for t in announcements}
        for topic in discussions:
            if str(topic.get("id")) not in announcement_ids:
                await self._topic(topic, "discussion")
        files = await self._list("files", f"{self.base}/files?per_page=100")
        folders = await self._list("folders", f"{self.base}/folders?per_page=100")
        self.folders = folders
        for meta in files or []:
            self.add_file(str(meta["id"]), source="files", name=meta.get("display_name") or "", meta=meta)
        await self._resolve_file_metadata(files is not None)
        # Index documents for list pages that the browser no longer needs to render.
        if assignments:
            self._assignment_index(assignments, groups)
        if announcements:
            self._announcement_index(announcements)
        if modules is not None:
            self._modules_doc(modules, tabs)
        self._files_doc(folders)
        self._browser_seeds(tabs)
        if quizzes is not None:
            self.content.covered.add(self.course_url("/quizzes"))
        if pages is not None:
            self.content.covered.add(self.course_url("/pages"))
        return self.content

    async def _syllabus(self, course: dict) -> None:
        body = course.get("syllabus_body") or ""
        url = self.course_url("/assignments/syllabus")
        if body.strip():
            self.add_doc(url, f"Syllabus: {course.get('name') or self.course_id}", f"<h1>Syllabus</h1>{body}", "syllabus")
            self.scan_html(body, "syllabus")

    def _page(self, page: dict) -> None:
        slug = page.get("url")
        if not slug:
            return
        url = self.course_url(f"/pages/{slug}")
        body = page.get("body") or ""
        meta = _meta_list([
            ("Page", "front page" if page.get("front_page") else ""),
            ("Updated", _local_time(page.get("updated_at"))),
            ("Locked", page.get("lock_explanation") if page.get("locked_for_user") else ""),
        ])
        if not body.strip() and not page.get("locked_for_user"):
            if "body" not in page:
                return
        text = body if body.strip() else f"<p>{_esc(page.get('lock_explanation') or 'This page has no content.')}</p>"
        self.add_doc(url, page.get("title") or slug, f"<h1>{_esc(page.get('title') or slug)}</h1>{meta}{text}", "page")
        if page.get("page_id"):
            self.content.aliases[self.course_url(f"/pages/{page['page_id']}")] = url
            self.content.covered.add(self.course_url(f"/pages/{page['page_id']}"))
        self.scan_html(body, f"page:{page.get('title')}")

    def _quiz(self, quiz: dict) -> None:
        url = normalize_canvas_url(quiz.get("html_url") or self.course_url(f"/quizzes/{quiz.get('id')}"))
        description = quiz.get("description") or ""
        limit = quiz.get("time_limit")
        meta = _meta_list([
            ("Quiz type", quiz.get("quiz_type")), ("Questions", quiz.get("question_count")),
            ("Points", quiz.get("points_possible")), ("Time limit", f"{limit} minutes" if limit else ""),
            ("Allowed attempts", "unlimited" if quiz.get("allowed_attempts") == -1 else quiz.get("allowed_attempts")),
            ("Due", _local_time(quiz.get("due_at"))), ("Available from", _local_time(quiz.get("unlock_at"))),
            ("Available until", _local_time(quiz.get("lock_at"))),
            ("Locked", quiz.get("lock_explanation") if quiz.get("locked_for_user") else ""),
        ])
        self.add_doc(url, quiz.get("title") or url, f"<h1>{_esc(quiz.get('title'))}</h1>{meta}{description}", "quiz")
        self.scan_html(description, f"quiz:{quiz.get('title')}")

    def _assignment(self, assignment: dict, submission: dict | None, groups: dict, quiz_docs: set[str]) -> None:
        url = normalize_canvas_url(assignment.get("html_url") or self.course_url(f"/assignments/{assignment.get('id')}"))
        quiz_id = str(assignment.get("quiz_id") or "")
        if quiz_id and quiz_id in quiz_docs:
            self.content.aliases[url] = self.course_url(f"/quizzes/{quiz_id}")
            self.content.covered.add(url)
            return
        description = assignment.get("description") or ""
        tool = (assignment.get("external_tool_tag_attributes") or {}).get("url") or ""
        types = [t.replace("_", " ") for t in assignment.get("submission_types") or [] if t != "none"]
        meta = _meta_list([
            ("Due", _local_time(assignment.get("due_at"))), ("Points", assignment.get("points_possible")),
            ("Assignment group", groups.get(str(assignment.get("assignment_group_id")))),
            ("Submission types", ", ".join(types)),
            ("External tool", urlsplit(tool).netloc if tool else ""),
            ("Available from", _local_time(assignment.get("unlock_at"))),
            ("Available until", _local_time(assignment.get("lock_at"))),
            ("Locked", assignment.get("lock_explanation") if assignment.get("locked_for_user") else ""),
        ])
        body = f"<h1>{_esc(assignment.get('name'))}</h1>{meta}"
        body += description if description.strip() else "<p>No description on Canvas.</p>"
        rubric = assignment.get("rubric") or []
        if rubric:
            rows = "".join(
                f"<tr><td>{_esc(c.get('description'))}</td><td>{_esc(c.get('long_description'))}</td><td>{_esc(c.get('points'))}</td></tr>"
                for c in rubric
            )
            body += f"<h2>Rubric</h2><table><tr><th>Criterion</th><th>Details</th><th>Points</th></tr>{rows}</table>"
        if submission:
            body += self._submission_html(submission)
        self.add_doc(url, assignment.get("name") or url, body, "assignment")
        self.scan_html(description, f"assignment:{assignment.get('name')}")

    def _submission_html(self, submission: dict) -> str:
        parts = _meta_list([
            ("Status", submission.get("workflow_state")),
            ("Submitted", _local_time(submission.get("submitted_at"))),
            ("Score", submission.get("score")), ("Grade", submission.get("grade")),
            ("Graded", _local_time(submission.get("graded_at"))),
            ("Late", "yes" if submission.get("late") else ""), ("Missing", "yes" if submission.get("missing") else ""),
            ("Excused", "yes" if submission.get("excused") else ""),
        ])
        comments = submission.get("submission_comments") or []
        if comments:
            parts += "<h3>Comments</h3>" + "".join(
                f"<blockquote><p><strong>{_esc(c.get('author_name'))}</strong> ({_esc(_local_time(c.get('created_at')))}):</p>"
                f"<p>{html.escape(c.get('comment') or '').replace(chr(10), '<br>')}</p></blockquote>"
                for c in comments
            )
        assessment = submission.get("rubric_assessment") or {}
        if assessment:
            parts += "<h3>Rubric assessment</h3><ul>" + "".join(
                f"<li>{_esc(key)}: {_esc(value.get('points'))} {_esc(value.get('comments') or '')}</li>"
                for key, value in assessment.items() if isinstance(value, dict)
            ) + "</ul>"
        return f"<h2>Your submission</h2>{parts}" if parts else ""

    async def _topic(self, topic: dict, kind: str) -> None:
        url = normalize_canvas_url(topic.get("html_url") or self.course_url(f"/discussion_topics/{topic.get('id')}"))
        message = topic.get("message") or ""
        author = (topic.get("author") or {}).get("display_name") or topic.get("user_name")
        meta = _meta_list([
            ("Type", kind), ("Author", author), ("Posted", _local_time(topic.get("posted_at") or topic.get("created_at"))),
            ("Replies", topic.get("discussion_subentry_count")),
        ])
        body = f"<h1>{_esc(topic.get('title'))}</h1>{meta}{message}"
        attachments = topic.get("attachments") or []
        if attachments:
            items = []
            for attachment in attachments:
                file_id = str(attachment.get("id"))
                ref = self.add_file(file_id, source=f"{kind}:{topic.get('title')}", name=attachment.get("display_name") or "",
                                    meta=attachment)
                items.append(f'<li><a href="{html.escape(ref.canonical_url, quote=True)}">{_esc(attachment.get("display_name"))}</a></li>')
            body += f"<h2>Attachments</h2><ul>{''.join(items)}</ul>"
        if topic.get("discussion_subentry_count"):
            replies = await self._replies(topic)
            if replies:
                body += f"<h2>Replies</h2>{replies}"
        self.add_doc(url, topic.get("title") or url, body, kind)
        self.content.covered.add(self.course_url(f"/announcements/{topic.get('id')}"))
        self.scan_html(message, f"{kind}:{topic.get('title')}")

    async def _replies(self, topic: dict) -> str:
        # The cached "view" structure lists entries and reports which are unread; it does not mark them read.
        result = await self.api.get(f"{self.base}/discussion_topics/{topic.get('id')}/view")
        if not result.ok or not isinstance(result.data, dict):
            return ""
        names = {str(p.get("id")): p.get("display_name") for p in result.data.get("participants") or []}

        def render(entries, depth=0) -> str:
            out = []
            for entry in entries or []:
                if entry.get("deleted"):
                    continue
                self.scan_html(entry.get("message"), f"reply:{topic.get('title')}")
                out.append(
                    f"<blockquote><p><strong>{_esc(names.get(str(entry.get('user_id')), 'Participant'))}</strong> "
                    f"({_esc(_local_time(entry.get('created_at')))}):</p>{entry.get('message') or ''}"
                    f"{render(entry.get('replies'), depth + 1)}</blockquote>"
                )
            return "".join(out)

        return render(result.data.get("view"))

    async def _module_item(self, item: dict, known_assignments: set[str], quiz_docs: set[str], assignments: list) -> None:
        kind = item.get("type")
        # External items carry an /api/v1/.../module_item_redirect/<id> html_url; the course
        # route for every item is /modules/items/<id>.
        item_url = self.course_url(f"/modules/items/{item['id']}") if item.get("id") else None
        if item_url:
            self.content.covered.add(item_url)
        target = None
        if kind == "Page" and item.get("page_url"):
            target = self.course_url(f"/pages/{item['page_url']}")
            if target not in self.content.doc_urls():
                page = await self._one(f"page:{item['page_url']}", f"{self.base}/pages/{quote(item['page_url'])}")
                if page:
                    self._page(page)
                else:
                    self._locked_stub(target, item, "page")
        elif kind == "Assignment" and item.get("content_id"):
            target = self.course_url(f"/assignments/{item['content_id']}")
            if str(item["content_id"]) not in known_assignments:
                assignment = await self._one(f"assignment:{item['content_id']}", f"{self.base}/assignments/{item['content_id']}")
                if assignment:
                    assignments.append(assignment)
                    known_assignments.add(str(item["content_id"]))
                else:
                    self._locked_stub(target, item, "assignment")
        elif kind == "Quiz" and item.get("content_id"):
            target = self.course_url(f"/quizzes/{item['content_id']}")
            if str(item["content_id"]) not in quiz_docs:
                quiz = await self._one(f"quiz:{item['content_id']}", f"{self.base}/quizzes/{item['content_id']}")
                if quiz:
                    self._quiz(quiz)
                    quiz_docs.add(str(item["content_id"]))
                else:
                    self._locked_stub(target, item, "quiz")
        elif kind == "Discussion" and item.get("content_id"):
            target = self.course_url(f"/discussion_topics/{item['content_id']}")
        elif kind == "File" and item.get("content_id"):
            ref = self.add_file(str(item["content_id"]), source=f"module:{item.get('title')}", name=item.get("title") or "")
            target = ref.canonical_url
        elif kind in {"ExternalTool", "ExternalUrl"}:
            if kind == "ExternalTool" and is_media_file(url=item.get("external_url") or ""):
                self.content.skipped_media.append((item.get("external_url") or item_url or "", item.get("title") or "", "Kaltura/Media Gallery item"))
        if item_url and target:
            self.content.aliases[item_url] = target

    def _locked_stub(self, url: str, item: dict, kind: str) -> None:
        details = item.get("content_details") or {}
        explanation = details.get("lock_explanation") or ("Locked for you on Canvas." if details.get("locked_for_user") else "Not available to you through Canvas.")
        meta = _meta_list([("Module item", kind), ("Due", _local_time(details.get("due_at"))),
                           ("Available from", _local_time(details.get("unlock_at"))), ("Points", details.get("points_possible"))])
        self.add_doc(url, item.get("title") or url, f"<h1>{_esc(item.get('title'))}</h1>{meta}<p>{_esc(explanation)}</p>", kind)

    def _assignment_index(self, assignments: list, groups: dict) -> None:
        rows = "".join(
            f"<tr><td><a href=\"{html.escape(a.get('html_url') or '', quote=True)}\">{_esc(a.get('name'))}</a></td>"
            f"<td>{_esc(groups.get(str(a.get('assignment_group_id')), ''))}</td><td>{_esc(_local_time(a.get('due_at')))}</td>"
            f"<td>{_esc(a.get('points_possible'))}</td></tr>"
            for a in sorted(assignments, key=lambda a: (a.get("due_at") or "9999", a.get("name") or ""))
        )
        self.add_doc(self.course_url("/assignments"), f"Assignments: {self.content.name}",
                     f"<h1>Assignments</h1><table><tr><th>Assignment</th><th>Group</th><th>Due</th><th>Points</th></tr>{rows}</table>",
                     "assignment-list")

    def _announcement_index(self, announcements: list) -> None:
        rows = "".join(
            f"<li><a href=\"{html.escape(t.get('html_url') or '', quote=True)}\">{_esc(t.get('title'))}</a> "
            f"({_esc(_local_time(t.get('posted_at')))})</li>"
            for t in announcements
        )
        self.add_doc(self.course_url("/announcements"), f"Announcements: {self.content.name}", f"<h1>Announcements</h1><ul>{rows}</ul>", "announcement-list")
        self.content.covered.add(self.course_url("/discussion_topics"))

    def _modules_doc(self, modules: list, tabs: list) -> None:
        sections = []
        for module in modules:
            state = module.get("state")
            header = _esc(module.get("name"))
            notes = []
            if state == "locked":
                notes.append("locked")
            if module.get("unlock_at"):
                notes.append(f"unlocks {_local_time(module.get('unlock_at'))}")
            items = []
            for item in module.get("items") or []:
                kind = item.get("type")
                title = _esc(item.get("title"))
                if kind == "SubHeader":
                    items.append(f"<li><strong>{title}</strong></li>")
                    continue
                href = item.get("external_url") if kind == "ExternalUrl" else self.course_url(f"/modules/items/{item.get('id')}")
                extra = f" → {_esc(item.get('external_url'))}" if kind in {"ExternalUrl", "ExternalTool"} and item.get("external_url") else ""
                items.append(f"<li>{_esc(kind)}: <a href=\"{html.escape(href or '', quote=True)}\">{title}</a>{extra}</li>")
            sections.append(f"<h2>{header}{' (' + _esc(', '.join(notes)) + ')' if notes else ''}</h2><ul>{''.join(items)}</ul>")
        menu = ", ".join(f"{t.get('label')}{' (external tool)' if t.get('type') == 'external' else ''}" for t in tabs)
        body = f"<h1>Modules</h1>{'<p>Course menu: ' + _esc(menu) + '</p>' if menu else ''}{''.join(sections) or '<p>No modules.</p>'}"
        self.add_doc(self.course_url("/modules"), f"Modules: {self.content.name}", body, "modules")

    async def _resolve_file_metadata(self, listed: bool) -> None:
        for ref in self.content.files.values():
            if ref.meta and "content-type" in ref.meta and "size" in ref.meta:
                continue
            owner = urlsplit(ref.canonical_url).path.split("/")[2] if ref.canonical_url.count("/courses/") else self.course_id
            result = await self.api.get(f"/api/v1/courses/{owner}/files/{ref.file_id}")
            if result.ok and isinstance(result.data, dict):
                ref.meta = result.data
                ref.name = ref.name or result.data.get("display_name") or ""
            else:
                ref.meta = {"_status": result.status}

    def _files_doc(self, folders: list | None) -> None:
        if not self.content.files or "course" in self.content.unavailable:
            return
        folder_names = {str(f.get("id")): (f.get("full_name") or "").replace("course files", "", 1) or "/" for f in folders or []}
        rows = []
        for ref in sorted(self.content.files.values(), key=lambda r: (r.meta or {}).get("display_name") or r.name or r.file_id):
            meta = ref.meta or {}
            name = meta.get("display_name") or ref.name or f"file {ref.file_id}"
            size = meta.get("size")
            rows.append(
                f"<tr><td><a href=\"{html.escape(ref.canonical_url, quote=True)}\">{_esc(name)}</a></td>"
                f"<td>{_esc(meta.get('content-type') or '')}</td><td>{_esc(f'{size / 1e6:.1f} MB' if isinstance(size, (int, float)) else '')}</td>"
                f"<td>{_esc(folder_names.get(str(meta.get('folder_id')), ''))}</td><td>{_esc('; '.join(sorted(ref.sources))[:300])}</td></tr>"
            )
        self.add_doc(self.course_url("/files"), f"Files: {self.content.name}",
                     "<h1>Course files</h1><table><tr><th>File</th><th>Type</th><th>Size</th><th>Folder</th><th>Linked from</th></tr>"
                     + "".join(rows) + "</table>", "files")

    def _browser_seeds(self, tabs: list) -> None:
        seeds = [self.course_url("")]
        for tab in tabs:
            if tab.get("type") == "external" or tab.get("hidden"):
                continue
            target = tab.get("full_url") or urljoin(self.origin, tab.get("html_url") or "")
            if target:
                seeds.append(normalize_canvas_url(target))
        self.content.browser_seeds = list(dict.fromkeys(seeds))


async def collect_course(request, origin: str, course_id: str, *, delay: float = 0.05) -> CourseContent:
    api = CanvasAPI(request, origin, delay=delay)
    collector = CourseCollector(api, origin, course_id)
    content = await collector.collect()
    logger.info("  course %s: %d API documents, %d files referenced (%d API calls)",
                course_id, len(content.documents), len(content.files), api.calls)
    return content
