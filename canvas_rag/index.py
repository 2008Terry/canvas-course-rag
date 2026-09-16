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
