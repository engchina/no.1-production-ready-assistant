"""実 Oracle AI Database と組み合わせて使う決定論的な AI クライアントスタブ。

Oracle はテストでも実 DB を使うが、VLM / embedding / rerank / LLM 回答は
非決定的かつ課金対象のため、エンドポイント経由のテストではこれらの
OCI クライアントを決定論スタブへ差し替える。embedding は全テキストを
同一ベクトルにするため、実 Oracle のベクトル検索でも確実に候補が返る。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

import pytest
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS
from pydantic import BaseModel
from rag_parser_core.result import ParserRegistryResult

from app.clients.oci_enterprise_ai import OciEnterpriseAiClient
from app.clients.oci_genai import OciGenAiClient
from app.schemas.document import SourceProfile
from app.schemas.extraction import StructuredExtraction


def _decode(image_bytes: bytes) -> str:
    # UTF-8 として解釈できないバイナリは抽出不能（空文字）として扱う。
    try:
        return image_bytes.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def _guess_document_type(text: str) -> str:
    if "規程" in text or "規定" in text:
        return "社内規程"
    return "文書"


class DeterministicEnterpriseAi(OciEnterpriseAiClient):
    """VLM 抽出と LLM 回答を決定論的に返す Enterprise AI スタブ。

    extract_with_vlm は raw_text のみ返し、element/page は raw_text から
    推定させる（既定 page_number=1 → citation の page_start=1 を再現）。
    """

    async def extract_with_vlm(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mime_type: str = "application/octet-stream",
        parser_profile: str = "enterprise_ai_generic",
    ) -> dict[str, object]:
        _ = parser_profile
        text = _decode(image_bytes)
        extraction = StructuredExtraction(
            raw_text=text,
            document_type=_guess_document_type(text),
            confidence=0.62 if text else 0.0,
            warnings=[] if text else ["スタブ抽出ではテキストを取得できませんでした。"],
        )
        return extraction.to_document_payload()

    async def generate(
        self,
        prompt: str,
        context: str,
        *,
        system_prompt: str | None = None,
        response_schema: Mapping[str, Any] | None = None,
        response_schema_name: str = "structured_answer",
    ) -> str:
        # 項目抽出は応答の schema を渡して呼ぶ（#2720）。決定論スタブは項目を 1 つも返さない。
        if response_schema_name == "field_extraction":
            return '{"fields": []}'
        if not context.strip():
            return "該当する根拠は見つかりませんでした。条件やキーワードを変えて検索してください。"
        snippet = " ".join(context.split())[:200]
        return (
            "検索された根拠に基づく要約です。"
            f"質問「{prompt}」に関連する内容として、{snippet} が見つかりました。"
        )


class DeterministicGenAi(OciGenAiClient):
    """埋め込みとリランクを決定論的に返す Generative AI スタブ。"""

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: str = "SEARCH_DOCUMENT",
    ) -> list[list[float]]:
        return [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        count = min(top_n, len(documents))
        return [(index, 1.0 - index * 1e-3) for index in range(count)]


def _stub_parser_runner(
    self: object,
    backend: str,
    source_bytes: bytes,
    source_profile: SourceProfile | None,
    content_type: str,
    *,
    fail_fast: bool = False,
) -> ParserRegistryResult:
    """parser マイクロサービス委譲(`ParserServiceClient.runner`)の決定論スタブ。

    in-process 解析は撤去済みのため、統合テストでは実サービスを起動せず本スタブで代替する。
    text 原本を decode して raw_text のみの抽出を返し、element/page は raw_text から推定させる
    (旧 in-process local partition / VLM スタブと同じ抽出形)。
    """
    _ = self, source_profile, content_type, fail_fast
    text = _decode(source_bytes)
    extraction = StructuredExtraction(
        raw_text=text,
        document_type=_guess_document_type(text),
        confidence=0.62 if text else 0.0,
        warnings=[] if text else ["スタブ抽出ではテキストを取得できませんでした。"],
    )
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend=backend,
        parser_version=f"{backend}_stub",
        template="text_blocks",
    )


def patch_ai_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """取込・検索パイプラインが既定構築する OCI クライアントを差し替える。"""
    monkeypatch.setattr("app.rag.ingestion.OciEnterpriseAiClient", DeterministicEnterpriseAi)
    monkeypatch.setattr("app.rag.ingestion.OciGenAiClient", DeterministicGenAi)
    monkeypatch.setattr("app.rag.pipeline.OciEnterpriseAiClient", DeterministicEnterpriseAi)
    monkeypatch.setattr("app.rag.pipeline.OciGenAiClient", DeterministicGenAi)
    # 解析は parser マイクロサービスへ委譲する。
    # 実サービス未起動でも取込できるよう runner を差し替え。
    monkeypatch.setattr(
        "app.clients.parser_service.ParserServiceClient.runner",
        _stub_parser_runner,
    )
    # 回答の生成（検索の API の回答）は rag_engine の LLM 呼び出しを通る。
    # テストは LLM の設定を空にするので、差し替えないと
    # 「LLM 設定が不足しています」で失敗する（#1595）。
    monkeypatch.setattr(
        "rag_engine.adapters.oci.parse_text_response", deterministic_parse_text_response
    )


# 回答の生成の prompt の根拠は「[E1] p.1 origin=…」の行の後に本文が続く
# （rag_engine.generation.grounded の format_functions）。補助情報の範囲は引用の対象外なので飛ばす。
_EVIDENCE_HEADER = re.compile(r"^\[(E[0-9]+)\][^\n]*$", re.MULTILINE)
# 引用単位の番号の付いた本文の行（「3| 本文」）。番号の無い根拠は全文が 1 つの引用になる。
_NUMBERED_LINE = re.compile(r"^([0-9]+)\| (.*)$")


def _first_evidence(prompt: str) -> tuple[str, str] | None:
    """prompt の最初の根拠の番号と、その本文の最初の行（原文どおりの引用）を返す。"""
    from rag_engine.generation.grounded import CONTEXT_NOTE_BEGIN, CONTEXT_NOTE_END

    match = _EVIDENCE_HEADER.search(prompt)
    if match is None:
        return None
    in_note = False
    for line in prompt[match.end() :].splitlines():
        text = line.strip()
        if text == CONTEXT_NOTE_BEGIN:
            in_note = True
        elif text == CONTEXT_NOTE_END:
            in_note = False
        elif text and not in_note:
            return match.group(1), text
    return None


def deterministic_parse_text_response(
    system_prompt: str, prompt: str, settings: Any, text_format: type[BaseModel], **_: Any
) -> BaseModel:
    """回答の生成の LLM 呼び出しを決定論的に返す（最初の根拠の 1 行を引用する 1 項目の回答）。"""
    from rag_engine.generation.citation_wire import generation_draft_wire_model
    from rag_engine.models.audit_wire import is_audit_wire
    from rag_engine.models.llm import (
        CragRetrievalGradeOutput,
        GroundedAudit,
        GroundedDraft,
        GroundedFactAudit,
        QueryExpansionOutput,
        QueryRoutingOutput,
        SectionExtractionOutput,
        TextOutput,
    )

    if text_format is QueryRoutingOutput:
        return QueryRoutingOutput(strategy="simple_retrieval", reason="決定論スタブ", queries=[])
    if text_format is QueryExpansionOutput:
        return QueryExpansionOutput(queries=[])
    if text_format is CragRetrievalGradeOutput:
        return CragRetrievalGradeOutput(
            reason="決定論スタブ", sufficient=True, confidence=0.9, rewritten_query=""
        )
    if text_format is SectionExtractionOutput:
        return SectionExtractionOutput(quotes=[])
    if text_format is TextOutput:
        return TextOutput(text="決定論スタブの回答です。")
    if text_format is GroundedDraft:
        evidence = _first_evidence(prompt)
        items = []
        if evidence:
            evidence_id, quote = evidence
            items = [
                {
                    "kind": "rule",
                    "text": quote,
                    "citations": [{"evidence_id": evidence_id, "quote": quote}],
                }
            ]
        return GroundedDraft.model_validate(
            {"summary": "決定論スタブの回答", "items": items, "confidence": "high"}
        )
    # 回答の生成は、引用の範囲を整数の行番号で受ける生成専用の schema で呼ぶ（#3685）。
    if text_format is generation_draft_wire_model():
        evidence = _first_evidence(prompt)
        items = []
        if evidence:
            evidence_id, line = evidence
            numbered = _NUMBERED_LINE.match(line)
            number = int(numbered.group(1)) if numbered else None
            items = [
                {
                    "kind": "rule",
                    "text": numbered.group(2) if numbered else line,
                    "citations": [
                        {"evidence_id": evidence_id, "line_start": number, "line_end": number}
                    ],
                }
            ]
        return text_format.model_validate(
            {"summary": "決定論スタブの回答", "items": items, "confidence": "high"}
        )
    # 監査は要求・ガイド要件の id を固定のキーにした wire の schema で呼ぶ。
    # キーはすべて埋め、要求の判定は保留にする。
    # 監査は項目があるときだけ呼ばれ、上の生成は項目を 1 つだけ返すので、index 0 を支持ありとする。
    if is_audit_wire(text_format):
        # 固定キーの id は wire の型の ClassVar にある（BaseModel の型からは見えない）。
        wire = cast(Any, text_format)
        reviews = [
            {
                "index": 0,
                "support": "supported",
                "source_applicability": "matched",
                "reason": "原文",
            }
        ]
        request = {"status": "unknown", "reason": "決定論スタブ", "evidence_status": "unknown"}
        requirement = {"answer_state": "not_mentioned", "reason": ""}
        return text_format.model_validate(
            {
                "goal_alignment": "aligned",
                "summary_supported": True,
                "reviews": reviews,
                "request_reviews": dict.fromkeys(wire.expected_request_ids, request),
                "guide_requirement_reviews": dict.fromkeys(
                    wire.expected_requirement_ids, requirement
                ),
            }
        )
    # 通常の回答の監査（#2795）は事実の監査の schema で呼ぶ。
    if text_format in (GroundedAudit, GroundedFactAudit):
        evidence = _first_evidence(prompt)
        reviews = (
            [{"index": 0, "support": "supported", "applicability": "matched", "reason": "原文"}]
            if evidence
            else []
        )
        return text_format.model_validate(
            {"goal_alignment": "aligned", "summary_supported": True, "reviews": reviews}
        )
    raise AssertionError(f"決定論スタブが扱わない出力の型です: {text_format.__name__}")
