"""noneの条文回答でも、LLM監査を呼ばずID・行範囲の基礎検査を維持する。"""

import pytest
from test_citation_extraction_provenance import spans
from test_cite_lines import item
from test_grounded_answer import FakeModel, draft

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.retrieval.article_catalog import ARTICLE_UNIT_KEY


@pytest.mark.parametrize(
    "identity,lines,reason",
    [("absent", "1", "citation_id_invalid"), (None, "2-999", "citation_range_out_of_bounds"), (None, "abc", "citation_range_invalid")],
)
def test_none_retains_basic_citation_checks_without_article_audit(identity, lines, reason):
    evidence = spans()
    evidence[0][ARTICLE_UNIT_KEY] = True
    proposal = item(evidence[0]["label"] if identity is None else identity, lines).model_copy(update={"text": "資料にない別の説明"})
    model = FakeModel([draft(proposal.model_dump(), summary="")], [])
    result = grounded.run_round(
        "対象の操作手順は？",
        evidence,
        get_settings(environ={}, dotenv_path=None),
        prompt=grounded.format_functions(evidence),
        image_paths=(),
        provider_id=None,
        parse_text=model,
        parse_images=lambda *args, **kwargs: pytest.fail("画像は不要"),
        audit_mode="none",
    )
    assert [name for name, _prompt in model.prompts] == ["GroundedDraftWire"]
    assert result.audit_calls == 0
    assert result.checked[0].citation_resolution["reason_code"] == reason
    assert any(mark["code"] == reason for mark in result.checked[0].marks)
    semantic = result.trace()["validation_dimensions"]["claim_support"]
    assert semantic["execution"] == "not_run"
    assert semantic["items"][0]["status"] == "unknown"
