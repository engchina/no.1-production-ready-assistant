"""Approved FAQ の embedding 意味照合(rag_poc semantic index)。"""

from app.rag.search_answer_profile_knowledge import (
    FAQ_SEMANTIC_CACHE_KEY,
    add_approved_faq,
    suggest_approved_faq,
)
from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle


class StubEmbedder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    async def __call__(self, texts: list[str], input_type: str) -> list[list[float]]:
        self.calls.append((list(texts), input_type))
        return [
            (
                [1.0, 0.0, 0.0, 0.0]
                if ("取り消" in text or "キャンセル" in text)
                else [0.0, 1.0, 0.0, 0.0]
            )
            for text in texts
        ]


async def test_semantic_match_finds_paraphrased_question_and_reuses_cache() -> None:
    store = FakeKnowledgeOracle()
    await add_approved_faq(
        store, "bv-1", question="受注を取り消すには？", answer="受注一覧で取消を押します。"
    )
    embed = StubEmbedder()

    text_only = await suggest_approved_faq(store, "bv-1", "注文のキャンセル方法")
    semantic = await suggest_approved_faq(
        store, "bv-1", "注文のキャンセル方法", embed=embed, embedding_dimensions=4
    )

    assert not any(item.match_method == "semantic" for item in text_only)
    assert semantic and semantic[0].record.question == "受注を取り消すには？"
    assert semantic[0].semantic_score is not None and semantic[0].semantic_score > 0.9
    assert FAQ_SEMANTIC_CACHE_KEY in (store.payloads[("bv-1", "approved_faq")] or {})
    assert [input_type for _, input_type in embed.calls] == ["SEARCH_DOCUMENT", "SEARCH_QUERY"]

    await suggest_approved_faq(
        store, "bv-1", "注文のキャンセル方法", embed=embed, embedding_dimensions=4
    )
    # FAQ が変わらなければ index を再利用し、質問だけを embedding する。
    assert [input_type for _, input_type in embed.calls][2:] == ["SEARCH_QUERY"]


async def test_semantic_failure_falls_back_to_text_match() -> None:
    store = FakeKnowledgeOracle()
    await add_approved_faq(store, "bv-1", question="受注を取り消すには？", answer="取消します。")

    async def broken(texts: list[str], input_type: str) -> list[list[float]]:
        raise RuntimeError("embedding unavailable")

    result = await suggest_approved_faq(
        store, "bv-1", "受注を取り消すには？", embed=broken, embedding_dimensions=4
    )

    assert result and result[0].score >= 0.92
