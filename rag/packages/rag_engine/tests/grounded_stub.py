"""検索・実行フローのテストから回答生成を切り離すための差し替え。

生成そのものは tests/test_grounded_answer.py が検証する。ここでは、テストが
`parse_text_response` に用意した最後の応答を生成結果として受け取り、検索・CRAG・
payload の検証を生成の出力形式から独立させる。
"""

import re
from unittest.mock import patch

from audit_fixtures import legacy_audit_result
from pydantic import BaseModel, ConfigDict

from rag_engine.generation import answering, grounded
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.models.audit_wire import is_audit_wire
from rag_engine.models.llm import CitedEvidenceOutput, GroundedAudit, GroundedDraft, GroundedFactAudit


class AnswerOutput(BaseModel):
    """テストが生成結果を指定するための入れ物。本番の LLM 出力スキーマではない。"""

    model_config = ConfigDict(extra="forbid")
    answer: str
    confidence: str = ""
    question_type: list[str] = []
    cited_evidence: list[CitedEvidenceOutput] = []
    reasoning_summary: str = ""
    insufficient_reason: str = ""
    needs_human_review: bool | None = None
    external_data_required: bool | None = None
    external_data_items: list[str] = []


def stub_generation(testcase, target: str = "rag_engine.generation.answering.synthesize_grounded_answer"):
    """生成を1回の LLM 呼出へ置き換える。呼出順・provider・画像添付の検証はそのまま使える。"""

    def fake(question, context, settings, **options):
        provider = options.get("answer_llm_provider")
        images = tuple(options.get("image_evidence") or ())
        mode = options["image_prompt_mode"]
        # 実際に生成へ渡る入力で呼び、根拠・用語補助・質問理解が prompt へ届くことを検証できるようにする。
        spans = answering._grounded_spans(question, context, images, options.get("runtime_knowledge"))
        prompt = answering._grounded_prompt(
            question,
            spans,
            context,
            images,
            None,
            options.get("question_plan"),
            options.get("runtime_knowledge"),
            options.get("inquiry_conditions"),
            options.get("query_expansion"),
        )
        if mode == "vision_attachments":
            output = answering.parse_multimodal_response(
                grounded.GENERATE_SYSTEM_PROMPT,
                prompt,
                [i["prompt_path"] for i in images if i.get("prompt_path")],
                settings,
                AnswerOutput,
                provider_id=provider,
            )  # 画像の回も回答のモデル（#3444）
        else:
            output = answering.parse_text_response(grounded.GENERATE_SYSTEM_PROMPT, prompt, settings, AnswerOutput, provider_id=provider)
        response = answering._normalize_answer_response(
            answering.AnswerResponse(
                answer_text=output.answer,
                confidence=output.confidence,
                question_type=tuple(output.question_type),
                cited_evidence=tuple(answering._cited_evidence_entries([i.model_dump() for i in output.cited_evidence])),
                reasoning_summary=output.reasoning_summary,
                insufficient_reason=output.insufficient_reason,
                needs_human_review=output.needs_human_review,
                external_data_required=output.external_data_required,
                external_data_items=tuple(output.external_data_items),
            )
        )
        return answering.GroundedAnswer(response, context, images, mode)

    mocked = patch(target, side_effect=fake)
    started = mocked.start()
    testcase.addCleanup(mocked.stop)
    return started


def echo_model(system, prompt, settings, schema, **options):
    """最初の根拠の先頭行をそのまま引用する。実際の生成経路で prompt の内容を確認する用途。"""
    if schema in (GroundedDraft, generation_draft_wire_model()):
        # 既存fixtureの自由引用をcanonicalで再生する。生成wireの構造検証は専用テストで行う。
        source_prompt = re.sub(r"<補助情報[（(]quote 不可[）)]>.*?</補助情報>\n?", "", prompt, flags=re.S)
        match = re.search(r"\[(E[0-9]+)\][^\n]*\n([^\n]+)", source_prompt)
        # 長い根拠の行には行番号（`1| `）が付く（#2830）。番号を外した原文を引く。
        line = re.sub(r"^[0-9]+\| ", "", match.group(2)) if match else ""
        items = [{"kind": "rule", "text": line, "citations": [{"evidence_id": match.group(1), "quote": line}]}] if match else []
        return GroundedDraft.model_validate({"summary": "answer", "items": items, "confidence": "high"})
    # 通常の回答の監査（#2795）は事実の監査の schema で呼ぶ。
    if is_audit_wire(schema) or schema in (GroundedAudit, GroundedFactAudit):
        parse_fixture = (lambda value: legacy_audit_result(value, schema)) if is_audit_wire(schema) else schema.model_validate
        return parse_fixture(
            {
                "goal_alignment": "aligned",
                "summary_supported": True,
                "reviews": [{"index": 0, "support": "supported", "applicability": "matched", "reason": "原文どおり"}],
            }
        )
    raise AssertionError(f"unexpected schema: {schema.__name__}")
