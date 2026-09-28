from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that can carry credentials or one-time access grants. They are never
# written to the catalog, snapshots, vector index, or CLI output.
SENSITIVE_PARAMS = {
    "token", "access_token", "refresh_token", "id_token", "verifier", "sf_verifier", "signature", "sig",
    "credential", "auth", "authorization", "password", "session", "sessionid", "session_id", "secret",
    "client_secret", "code", "ticket", "jwt", "api_key", "apikey",
}
SENSITIVE_PREFIXES = ("x-amz-",)
TRACKING_PARAMS = {"fbclid", "gclid"}
# Canvas adds these for navigation only; the same page or file is served without them.
NAVIGATION_PARAMS = {"module_item_id"}

_CANVAS_FILE = re.compile(r"^(?:/courses/(\d+))?/files/(\d+)(?:/|$)")


def is_sensitive_param(key: str) -> bool:
    key = key.lower()
    return key in SENSITIVE_PARAMS or key.startswith(SENSITIVE_PREFIXES)


def _netloc(parts) -> str:
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host.lower()}:{parts.port}" if parts.port else host.lower()


def redact_url(url: str) -> str:
    """Remove credential-bearing query parameters, keeping everything else intact."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if not is_sensitive_param(key)]
    if len(kept) == len(pairs):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def normalize_canvas_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS | NAVIGATION_PARAMS
             and not is_sensitive_param(key)]
    return urlunsplit((parts.scheme.lower(), _netloc(parts), parts.path.rstrip("/"), urlencode(query), ""))


def canvas_file_key(url: str) -> tuple[str | None, str] | None:
    """Return (course id, file id) for any Canvas file route, e.g. /files/1, /files/1/download, /files/1/preview."""
    match = _CANVAS_FILE.match(urlsplit(url).path)
    return (match.group(1), match.group(2)) if match else None


def canonical_attachment_url(url: str) -> str | None:
    """One stable, credential-free identity per Canvas file regardless of which route linked to it."""
    key = canvas_file_key(url)
    if key is None:
        return None
    course_id, file_id = key
    parts = urlsplit(url)
    path = f"/courses/{course_id}/files/{file_id}" if course_id else f"/files/{file_id}"
    return urlunsplit((parts.scheme.lower(), _netloc(parts), path, "", ""))


def attachment_identity(url: str) -> str:
    """Catalog key for a downloaded attachment: the Canvas file identity, or the normalized URL otherwise."""
    return canonical_attachment_url(url) or normalize_canvas_url(url)


def transient_access_params(url: str) -> list[tuple[str, str]]:
    """Canvas file-access grants (e.g. verifier) that may be needed for the download request only."""
    return [(key, value) for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
            if key.lower() == "verifier"]


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+")


def redact_text(text: str) -> str:
    """Redact every URL inside free text such as an exception message."""
    return _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), text)
