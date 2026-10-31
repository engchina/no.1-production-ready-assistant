"""実 Oracle の統合テストの決定論スタブが、回答の生成の 1 組を最後まで返す（#3846）。

統合テストは nightly だけで動くので、生成の schema が変わってスタブが追従しないと main が赤になる。
ここでは Oracle なしで、実際の回答の生成の 1 組（run_round）にスタブを渡して確かめる。
"""

from __future__ import annotations

from typing import Any

import pytest
from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.generation.answering import _grounded_spans

from tests._ai_stubs import deterministic_parse_text_response

QUESTION = "申請の承認者は？"
TEXT = "申請は担当者が作成する。\n申請は部門長の承認を受ける。\n承認後に申請は確定する。"


def _spans() -> list[dict[str, Any]]:
    record = AnswerRecord(
        id="r0",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=TEXT,
        source="文書 A.pdf",
        chunk_level="parent",
        chunk_id="r0",
        source_run_id="run",
        metadata={"section_path": ["文書 A", "申請"]},
    )
    return _grounded_spans(QUESTION, AnswerContext(records=[record], text=TEXT), (), None)


@pytest.mark.parametrize("audit_mode", [grounded.AUDIT_MODE_NONE, grounded.AUDIT_MODE_DETAILED])
def test_stub_completes_a_generation_round_with_a_verified_citation(audit_mode: str) -> None:
    spans = _spans()
    schemas: list[str] = []

    def stub(
        system_prompt: str, prompt: str, settings: Any, text_format: type, **options: Any
    ) -> Any:
        schemas.append(text_format.__name__)
        return deterministic_parse_text_response(
            system_prompt, prompt, settings, text_format, **options
        )

    result = grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt=grounded.format_functions(spans),
        image_paths=(),
        provider_id=None,
        parse_text=stub,
        parse_images=lambda *args, **kwargs: pytest.fail("画像は不要"),
        audit_mode=audit_mode,
    )

    assert schemas[0] == "GroundedDraftWire"
    assert len(result.checked) == 1
    citation = result.checked[0].item.citations[0]
    assert citation.evidence_id == spans[0]["evidence_id"]
    assert citation.quote and citation.quote in TEXT
    if audit_mode == grounded.AUDIT_MODE_DETAILED:
        # 監査の wire をスタブが返せず、監査の失敗として草稿だけが残る形で通らないこと。
        assert result.audit_calls == 1
        assert result.checked[0].review_assessment["observed_support"] == "supported"
