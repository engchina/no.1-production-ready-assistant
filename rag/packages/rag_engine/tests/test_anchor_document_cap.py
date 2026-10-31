"""rerank の後、起点に採る同じ文書の child を文書ごとの上限までにし、超えた child を後ろへ回す (#2167)。"""

import unittest
from dataclasses import replace

from rag_engine.config import get_settings
from rag_engine.generation import answering as a
from rag_engine.retrieval.reference_follow import REFERENCE_EXPANSION_KEY
from tests.test_document_selection import child


def settings(limit):
    return replace(get_settings(environ={}, dotenv_path=None), max_anchors_per_document=limit)


class Step:
    def __init__(self):
        self.results = []
        self.lines = []

    def result(self, *details):
        self.results.extend(details)

    def add(self, *details):
        self.lines.extend(details)


class AnchorDocumentCapTest(unittest.TestCase):
    def ranked(self):
        # 文書 A の child が上位 4 件を占め、文書 B の child は 5 位。
        records = [child(f"a{i}", i, f"A の節 {i}", "A.pdf", f"pa{i}") for i in range(1, 5)]
        return [*records, child("b1", 5, "B の条", "B.json", "pb1"), child("a5", 6, "A の節 5", "A.pdf", "pa5")]

    def test_moves_children_over_the_limit_after_other_documents(self):
        step = Step()
        capped = a.cap_anchors_per_document(self.ranked(), settings(2), top_k=3, step=step)
        self.assertEqual([r.id for r in capped], ["a1", "a2", "b1", "a3", "a4", "a5"])
        # 上位 3 件のうち a3 を後ろへ回した。
        self.assertEqual(step.results, ["文書ごとの起点の上限 2 件: 上位 3 件のうち 1 件を後ろへ回します"])
        # 後ろへ回した候補と、代わりに起点に入った候補を名前で示す（#2341）。本文は入れない。
        self.assertEqual(
            step.lines,
            [
                "後ろへ回した候補:",
                "1. A.pdf › 管理-(1)登録（p.3）",
                "代わりに起点に入った候補:",
                "1. B.json › 管理-(1)登録（p.5）",
            ],
        )

    def test_zero_keeps_the_order(self):
        ranked = self.ranked()
        self.assertEqual(a.cap_anchors_per_document(ranked, settings(0), top_k=3), ranked)

    def test_engine_default_is_unlimited_and_env_selects_the_limit(self):
        self.assertEqual(get_settings(environ={}, dotenv_path=None).max_anchors_per_document, 0)
        environ = {"RAG_ENGINE_MAX_ANCHORS_PER_DOCUMENT": "5"}
        self.assertEqual(get_settings(environ=environ, dotenv_path=None).max_anchors_per_document, 5)

    def test_followed_references_keep_their_position(self):
        ranked = self.ranked()
        followed = child("a-ref", 7, "A の参照先", "A.pdf", "pa-ref")
        followed.metadata[REFERENCE_EXPANSION_KEY] = {"followed": True}
        ranked.insert(2, followed)
        self.assertTrue(a.is_followed_reference(followed))
        capped = a.cap_anchors_per_document(ranked, settings(2), top_k=3)
        self.assertEqual([r.id for r in capped][:4], ["a1", "a2", "a-ref", "b1"])


if __name__ == "__main__":
    unittest.main()
