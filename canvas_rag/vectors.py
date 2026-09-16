from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    text: str
    start_index: int
    end_index: int


def chunk_text(text: str, *, chunk_words: int = 220, overlap_words: int = 40) -> list[TextChunk]:
    if chunk_words < 1 or overlap_words < 0 or overlap_words >= chunk_words:
        raise ValueError("chunk_words must be positive and overlap_words must be smaller")
    cjk_chars = sum(
        1 for char in text
        if 0x3400 <= ord(char) <= 0x9FFF or 0xF900 <= ord(char) <= 0xFAFF
        or 0x3040 <= ord(char) <= 0x30FF or 0xAC00 <= ord(char) <= 0xD7AF
    )
    if cjk_chars >= 8:
        chunk_chars, overlap_chars = 380, 80
        chunks: list[TextChunk] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_chars)
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(TextChunk(chunk, start, end))
            if end == len(text):
                break
            start = end - overlap_chars
        return chunks
    words = text.split()
    chunks: list[TextChunk] = []
    start = 0
    while start < len(words):
        end = min(len(words), start + chunk_words)
        chunks.append(TextChunk(" ".join(words[start:end]), start, end))
        if end == len(words):
            break
        start = end - overlap_words
    return chunks


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have equal dimensions")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def rank_by_similarity(query_vector, rows, *, vector_field: str = "vector"):
    return sorted(rows, key=lambda row: cosine_similarity(query_vector, row[vector_field]), reverse=True)


class LocalE5Embedder:
    MODEL_NAME = "intfloat/multilingual-e5-small"

    def __init__(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("Install the local RAG dependencies with `pip install -e .[rag]`.") from exc
        self.model = SentenceTransformer(self.MODEL_NAME)

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode([f"passage: {text}" for text in texts], normalize_embeddings=True).tolist()

    def encode_query(self, text: str) -> list[float]:
        return self.model.encode([f"query: {text}"], normalize_embeddings=True)[0].tolist()
