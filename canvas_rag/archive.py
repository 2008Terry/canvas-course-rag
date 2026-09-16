from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


@dataclass(frozen=True)
class SavedPage:
    html_path: Path
    markdown_path: Path
    relative_html_path: str
    relative_markdown_path: str


def safe_component(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip(".-_")
    return value[:80] or fallback


class _OfflineSanitizer(HTMLParser):
    DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "form", "button"}
    VOID = {"area", "base", "br", "col", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, local_targets: dict[str, str], source_url: str):
        super().__init__(convert_charrefs=False)
        self.local_targets = local_targets
        self.source_url = source_url
        self.output: list[str] = []
        self.drop_depth = 0

    @staticmethod
    def _normalized(url: str) -> str:
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.drop_depth:
            if tag in self.DROP_CONTENT:
                self.drop_depth += 1
            return
        if tag in self.DROP_CONTENT:
            self.drop_depth = 1
            return
        if tag in {"html", "head", "body", "meta", "link", "base"}:
            return
        safe_attrs: list[tuple[str, str]] = []
        for key, value in attrs:
            key = key.lower()
            if value is None or key.startswith("on") or key in {"srcdoc", "integrity", "nonce", "style", "srcset", "background"}:
                continue
            if key in {"href", "src", "poster"}:
                lowered = value.strip().lower()
                if lowered.startswith(("javascript:", "data:", "vbscript:")):
                    continue
                absolute = urljoin(self.source_url, value)
                local_value = self.local_targets.get(value) or self.local_targets.get(absolute) or self.local_targets.get(self._normalized(absolute))
                if local_value:
                    value = local_value
                elif key == "href" and urlsplit(absolute).scheme in {"http", "https"}:
                    value = self._normalized(absolute)
                    safe_attrs.append(("target", "_blank"))
                    safe_attrs.append(("rel", "noopener noreferrer"))
                elif key in {"src", "poster"}:
                    continue
            safe_attrs.append((key, value))
        rendered = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in safe_attrs)
        self.output.append(f"<{tag}{rendered}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.drop_depth:
            if tag in self.DROP_CONTENT:
                self.drop_depth -= 1
            return
        if tag not in self.VOID and tag not in {"html", "head", "body"}:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.drop_depth:
            self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self.drop_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.drop_depth:
            self.output.append(f"&#{name};")


def render_offline_html(*, title: str, source_url: str, body: str, local_targets: dict[str, str]) -> str:
    sanitizer = _OfflineSanitizer(local_targets, source_url)
    sanitizer.feed(body)
    safe_body = "".join(sanitizer.output)
    safe_title = html.escape(title)
    safe_source = html.escape(source_url, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title><style>
body{{max-width:920px;margin:2rem auto;padding:0 1.25rem;font:16px/1.65 system-ui,sans-serif;color:#172033}}
header{{border-bottom:1px solid #d6dbe4;margin-bottom:1.5rem;padding-bottom:.75rem;color:#526078;font-size:.9rem}}
img,video{{max-width:100%;height:auto}}table{{border-collapse:collapse;max-width:100%}}th,td{{border:1px solid #cbd2df;padding:.4rem .6rem}}
pre,code{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#155eef}}
</style></head><body><header>Canvas source: <a href="{safe_source}" target="_blank" rel="noopener noreferrer">{safe_source}</a></header>
<main>{safe_body}</main></body></html>"""


def save_page(
    *, root: Path, course_id: str, page_id: str, title: str, url: str, body: str,
    markdown: str, local_targets: dict[str, str],
) -> SavedPage:
    stem = safe_component(page_id or title)
    folder = Path("courses") / safe_component(course_id) / "pages"
    relative_html = folder / f"{stem}.html"
    relative_markdown = folder / f"{stem}.md"
    html_path = root / relative_html
    markdown_path = root / relative_markdown
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(
        render_offline_html(title=title, source_url=url, body=body, local_targets=local_targets),
        encoding="utf-8",
    )
    markdown_path.write_text(
        f"---\ntitle: {json.dumps(title, ensure_ascii=False)}\nsource_url: {url}\ncourse_id: {course_id}\n---\n\n{markdown}\n",
        encoding="utf-8",
    )
    return SavedPage(html_path, markdown_path, relative_html.as_posix(), relative_markdown.as_posix())


def write_course_index(root: Path, course_id: str, title: str, pages: list[dict]) -> None:
    folder = root / "courses" / safe_component(course_id)
    folder.mkdir(parents=True, exist_ok=True)
    links = "\n".join(
        f'<li><a href="pages/{html.escape(Path(item["relative_path"]).name, quote=True)}">{html.escape(item["title"])}</a></li>'
        for item in pages
    )
    (folder / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f'<h1>{html.escape(title)}</h1><ul>{links}</ul>', encoding="utf-8"
    )
