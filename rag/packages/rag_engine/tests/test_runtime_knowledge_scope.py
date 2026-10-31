from pathlib import Path

from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext


def _context() -> RuntimeKnowledgeContext:
    return RuntimeKnowledgeContext(original_question="期限は？", expanded_question="期限は？", path=Path("rk.json"))


def test_scope_goes_to_prompt_and_search_terms_to_the_expanded_question() -> None:
    context = _context().with_scope("選択: 出張旅費\n対象範囲: 「出張旅費規程」の「第6条」（p.2）", ["精算", " "])

    assert context.has_matches
    assert context.expanded_question == "期限は？\n精算"
    prompt = context.prompt_context()
    assert "[利用者が確認の質問で選んだ条件]" in prompt
    assert "「第6条」（p.2）" in prompt
    assert "[Runtime Glossary / Rules]" not in prompt
    assert context.to_payload()["scope_context"].startswith("選択: 出張旅費")


def test_empty_scope_keeps_the_context() -> None:
    context = _context()
    assert context.with_scope(" ", []) is context
