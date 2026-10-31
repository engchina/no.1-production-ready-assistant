"""確認の項目の規則へ渡す backend の材料（#1492）。

分かっている条件（業務ガイドの照合の結果）を rag_engine の設定へ、旧版の文書の印
（document_superseded）を rag_engine の根拠へ渡す。LLM・OCI へは接続しない。
"""

from __future__ import annotations

from pathlib import Path

from app.rag.answer_engine import _stored_child, build_engine_settings, guide_known_conditions
from app.schemas.search import RetrievedChunk
from tests.test_oci_enterprise_ai import _oci_settings


def test_guide_known_conditions_take_labels_with_values() -> None:
    guide = {
        "known_conditions": [
            {"id": "approved", "label": "部門長の承認", "value": "はい", "state": "known"},
            {"id": "target", "label": "付与先", "value": " ", "state": "known"},
            "壊れた値",
        ]
    }
    assert guide_known_conditions(guide) == (("部門長の承認", "はい"),)
    assert guide_known_conditions({}) == ()
    assert guide_known_conditions(None) == ()
    assert guide_known_conditions({"known_conditions": "x"}) == ()


def test_engine_settings_carry_known_conditions(tmp_path: Path) -> None:
    settings = _oci_settings()
    assert build_engine_settings(settings, output_dir=tmp_path).known_conditions == ()
    settings.rag_support_guide_enabled = True
    settings.rag_support_guide = {
        "guide_id": "g1",
        "known_conditions": [{"id": "approved", "label": "部門長の承認", "value": "はい"}],
    }
    assert build_engine_settings(settings, output_dir=tmp_path).known_conditions == (
        ("部門長の承認", "はい"),
    )


def _chunk(*, superseded: bool) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="doc-1",
        chunk_id="chunk-1",
        text="手順",
        score=0.5,
        file_name="manual-v1.pdf",
        metadata={"document_superseded": True} if superseded else {},
    )


def test_stored_child_marks_superseded_documents_for_the_engine() -> None:
    superseded = _stored_child(_chunk(superseded=True), rrf_score=0.1).metadata
    current = _stored_child(_chunk(superseded=False), rrf_score=0.1).metadata
    assert superseded["document_superseded"] is True
    assert "document_superseded" not in current
