from __future__ import annotations

import hashlib
from pathlib import Path

from .vectors import LocalE5Embedder, chunk_text


class LocalVectorIndex:
    """LanceDB index stored under the local corpus directory."""

    def __init__(self, path: Path, embedder=None):
        try:
            import lancedb
        except ImportError as exc:
            raise RuntimeError("Install the local RAG dependencies with `pip install -e .[rag]`.") from exc
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

    def repair_sources(self, *, renamed: dict[str, str], valid: set[str]) -> tuple[int, int]:
        """Point chunks at renamed catalog URLs and drop chunks whose source left the catalog."""
        if self.table is None:
            return 0, 0
        present = self.source_urls()
        moved = 0
        for old, new in renamed.items():
            if old in present:
                self.table.delete(f"source_url = {self._quoted(new)}")
                self.table.update(where=f"source_url = {self._quoted(old)}", values={"source_url": new})
                present.discard(old)
                present.add(new)
                moved += 1
        stale = sorted(present - valid)
        for start in range(0, len(stale), 200):
            batch = ", ".join(self._quoted(url) for url in stale[start:start + 200])
            self.table.delete(f"source_url IN ({batch})")
        return moved, len(stale)

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
