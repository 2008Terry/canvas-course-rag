import unittest

from canvas_rag.vectors import chunk_text, cosine_similarity, rank_by_similarity


class VectorTests(unittest.TestCase):
    def test_chunk_text_keeps_overlap_and_source_offsets(self):
        chunks = chunk_text("one two three four five six", chunk_words=3, overlap_words=1)
        self.assertEqual([c.text for c in chunks], ["one two three", "three four five", "five six"])
        self.assertEqual(chunks[1].start_index, 2)

    def test_chinese_text_is_split_before_model_token_limit(self):
        chunks = chunk_text("课程内容需要覆盖重点。" * 120)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.text) <= 380 for chunk in chunks))
        self.assertEqual(chunks[1].start_index, 300)

    def test_cosine_ranking_returns_semantically_closest_vector_first(self):
        ranked = rank_by_similarity(
            query_vector=[1.0, 0.0],
            rows=[{"id": "orthogonal", "vector": [0.0, 1.0]}, {"id": "close", "vector": [0.8, 0.2]}],
            vector_field="vector",
        )
        self.assertEqual(ranked[0]["id"], "close")
        self.assertGreater(cosine_similarity([1, 0], [0.8, 0.2]), cosine_similarity([1, 0], [0, 1]))


if __name__ == "__main__":
    unittest.main()
