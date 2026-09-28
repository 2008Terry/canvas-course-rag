from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .vectors import LocalE5Embedder, chunk_text

WRITER_LOCK = ".course-rag-writer.lock"
# Query-string markers that must not survive in old LanceDB versions after a repair.
TOKEN_MARKERS = (b"verifier=", b"access_token=")


class IndexBusyError(RuntimeError):
    pass


@contextmanager
def index_writer_lock(path: Path, *, stale_after_seconds: float = 6 * 3600):
    """Advisory lock so only one course-rag process writes or cleans the index at a time.

    A lock left behind by a crashed process is reclaimed once it is older than ``stale_after_seconds``.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    lock = path / WRITER_LOCK
    for attempt in range(2):
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if attempt == 0 and age > stale_after_seconds:
                lock.unlink(missing_ok=True)
                continue
            raise IndexBusyError(
                f"Another course-rag process is updating {path} (lock file {lock.name}). "
                "Wait for it to finish, or delete the lock file if no course-rag process is running."
            ) from None
    else:
        raise IndexBusyError(f"Could not acquire the index lock in {path}.")
    try:
        os.write(descriptor, f"{os.getpid()}\n".encode())
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


class LocalVectorIndex:
    """LanceDB index stored under the local corpus directory."""

    def __init__(self, path: Path, embedder=None):
        try:
            import lancedb
        except ImportError as exc:
            raise RuntimeError("Install the local RAG dependencies with `pip install -e .[rag]`.") from exc
        self.path = Path(path)
        self.db = lancedb.connect(str(path))
        self._embedder = embedder
        self.table = self.db.open_table("canvas_chunks") if "canvas_chunks" in self.db.table_names() else None

    def _get_embedder(self):
        if self._embedder is None:
            self._embedder = LocalE5Embedder()
        return self._embedder

    def add_document(self, *, source_url: str, course_id: str, title: str, relative_path: str, text: str) -> int:
        chunks = chunk_text(text)
        if self.table is not None:
            escaped_url = source_url.replace("'", "''")
            self.table.delete(f"source_url = '{escaped_url}'")
        if not chunks:
            return 0
        rows = []
        vectors = self._get_embedder().encode_passages([chunk.text for chunk in chunks])
        for chunk, vector in zip(chunks, vectors):
            key = hashlib.sha256(f"{source_url}\0{chunk.start_index}\0{chunk.end_index}\0{chunk.text}".encode()).hexdigest()
            rows.append({
                "id": key, "source_url": source_url, "course_id": str(course_id), "title": title,
                "relative_path": relative_path, "text": chunk.text, "vector": vector,
            })
        if self.table is None:
            self.table = self.db.create_table("canvas_chunks", data=rows)
        else:
            self.table.add(rows)
        return len(rows)

    @staticmethod
    def _quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def source_urls(self) -> set[str]:
        if self.table is None:
            return set()
        return set(self.table.to_arrow().column("source_url").to_pylist())

    def _chunk_ids_by_source(self) -> dict[str, list[str]]:
        columns = self.table.to_arrow().select(["id", "source_url"]).to_pydict()
        found: dict[str, list[str]] = {}
        for chunk_id, source_url in zip(columns["id"], columns["source_url"]):
            found.setdefault(source_url, []).append(chunk_id)
        return found

    def _id_filters(self, ids: list[str]):
        for start in range(0, len(ids), 500):
            yield "id IN (" + ", ".join(self._quoted(chunk_id) for chunk_id in ids[start:start + 500]) + ")"

    def repair_sources(self, *, renamed: dict[str, str], valid: set[str]) -> tuple[int, int]:
        """Point chunks at renamed catalog URLs and drop chunks whose source left the catalog.

        Rows are addressed by chunk id, never by the old URL, so an old URL's token is not copied
        into the filter text LanceDB keeps in its transaction log.
        """
        if self.table is None:
            return 0, 0
        chunks = self._chunk_ids_by_source()
        moved = 0
        for old, new in renamed.items():
            if old not in chunks:
                continue
            self.table.delete(f"source_url = {self._quoted(new)}")
            for where in self._id_filters(chunks[old]):
                self.table.update(where=where, values={"source_url": new})
            chunks[new] = chunks.pop(old)
            moved += 1
        stale = sorted(set(chunks) - valid)
        stale_ids = [chunk_id for url in stale for chunk_id in chunks[url]]
        for where in self._id_filters(stale_ids):
            self.table.delete(where)
        return moved, len(stale)

    def version_count(self) -> int:
        return len(self.table.list_versions()) if self.table is not None else 0

    def compact(self, *, remove_unverified: bool = False) -> tuple[int, int]:
        """Compact data files and delete every old table version. Returns (versions before, after).

        Old versions keep deleted and renamed rows on disk, including URLs from before a repair.
        ``remove_unverified`` also deletes files no version references; only use it while holding
        ``index_writer_lock``, because an in-progress write from another process looks the same.
        """
        if self.table is None:
            return 0, 0
        before = self.version_count()
        self.table.optimize(cleanup_older_than=timedelta(0), delete_unverified=remove_unverified)
        return before, self.version_count()

    def token_residue(self) -> int:
        """Number of files in the table directory that still contain a credential query marker."""
        if self.table is None:
            return 0
        folder = self.path / "canvas_chunks.lance"
        found = 0
        for path in folder.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                found += any(marker in data for marker in TOKEN_MARKERS)
        return found

    def search(self, query: str, *, limit: int = 8) -> list[dict]:
        if self.table is None:
            return []
        vector = self._get_embedder().encode_query(query)
        return self.table.search(vector).distance_type("cosine").limit(limit).to_list()


def rebuild_index(catalog, index: LocalVectorIndex) -> int:
    if index.table is None:
        catalog.clear_index_status()
    count = 0
    for document in catalog.documents_needing_index():
        count += index.add_document(
            source_url=document["source_url"], course_id=document["course_id"], title=document["title"],
            relative_path=document["relative_path"], text=document["text"],
        )
        catalog.mark_indexed(document["source_url"], document["source_hash"])
    return count


def maintain_index(index: LocalVectorIndex, *, wrote: bool, max_versions: int = 20) -> tuple[int, int] | None:
    """Compact after a sync that wrote to the index, or once versions pile up, so they never reach thousands.

    Uses LanceDB's safe cleanup (recent files that no version references are kept) and should run
    while holding ``index_writer_lock``.
    """
    if index.table is None or (not wrote and index.version_count() <= max_versions):
        return None
    return index.compact()
