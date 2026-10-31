"""同一CRAG評価での意味判定と、原文の決定論的照合を分けて検証する。"""

from dataclasses import replace
from unittest.mock import patch

import pytest
from test_crag_context_recovery import record

from rag_engine.config import get_settings
from rag_engine.generation import answering as a
from rag_engine.generation.crag_support import _crag_task_contract
from rag_engine.models.llm import CragRetrievalGradeOutput


def evaluate(question, texts, *, checks=None, verdicts=None, compact=False, answerable_parts=()):
    records = [record(f"c{i}", i, text) for i, text in enumerate(texts)]
    if checks is None:
        checks = [
            dict(
                aspect=aspect,
                status="supported",
                source_ids=["run:c0"],
                evidence=[dict(source_id="run:c0", quote=texts[0])],
                reason="原文が要求を説明する",
            )
            for aspect in _crag_task_contract(question)["required_aspects"]
        ]
    output = CragRetrievalGradeOutput(
        candidate_verdicts=verdicts
        if verdicts is not None
        else [dict(chunk_uid=f"run:c{i}", relevant=True, reason="回答に有用") for i in range(len(texts))],
        aspect_checks=checks,
        answerable_parts=list(answerable_parts),
        sufficient=True,
        confidence=0.8,
        reason="取得済み材料を評価",
        rewritten_query="",
    )
    settings = replace(get_settings(environ={}, dotenv_path=None), crag_compact_input=compact)
    context = a.AnswerContext(records=records, text="\n".join(texts))
    with patch.object(a, "parse_text_response", return_value=output) as call:
        result = a._grade_crag_retrieval(1, question, question, [question], context, settings)
    assert call.call_count == 1
    return result, context


@pytest.mark.parametrize(
    "text",
    [
        "行の確認が必要になったとき、表示印を付けます。",
        "表示印\n確認が必要な行を知らせるために付けます。",
        "<table><tr><th>表示印</th><td>確認が必要な行を知らせるもの</td></tr></table>",
    ],
)
def test_definition_semantics_can_support_text_not_recognized_by_the_rule(text):
    question = "『表示印』の意味は？"
    result, _ = evaluate(
        question,
        [text],
        answerable_parts=[dict(aspect="definition:Q1.D1", kind="definition", claim=text, evidence=[dict(source_id="run:c0", quote=text)])],
    )
    assert result.rule_signals["definitions"] == [{"label": "表示印", "recognition": "not_identified"}]
    assert result.generation_usable and result.required_aspect_coverage == "complete"
    definition = next(c for c in result.aspect_checks if c["aspect"].startswith("definition:"))
    assert definition["status"] == "supported" and definition["evidence_validation"] == "matched"


def test_field_name_alone_does_not_override_semantic_missing():
    question = "『表示印』の意味は？"
    required = _crag_task_contract(question)["required_aspects"]
    checks = [dict(aspect=k, status="missing", source_ids=[], reason="項目名だけで意味の説明がない") for k in required]
    result, _ = evaluate(question, ["項目一覧：名称、表示印、区分"], checks=checks)
    assert not result.generation_usable and result.required_aspect_coverage == "missing"
    assert not result.unknown_aspects


@pytest.mark.parametrize("evidence", [[], [dict(source_id="outside", quote="保存する。")], [dict(source_id="run:c0", quote="削除する。")]])
def test_unverified_supported_check_becomes_unknown_not_missing(evidence):
    question = "設定を保存するには？"
    checks = [
        dict(aspect=k, status="supported", source_ids=["run:c0"], evidence=evidence, reason="モデルの判定")
        for k in _crag_task_contract(question)["required_aspects"]
    ]
    result, _ = evaluate(question, ["保存する。"], checks=checks)
    assert result.required_aspect_coverage == "unknown" and result.unknown_aspects
    assert result.missing_aspects == ()


def test_unknown_is_not_replaced_with_missing_and_duplicates_are_not_complete():
    question = "設定を保存するには？"
    checks = [
        dict(
            aspect="business_object",
            status="supported",
            source_ids=["run:c0"],
            evidence=[dict(source_id="run:c0", quote="設定")],
            reason="対象",
        )
    ] * 2
    result, _ = evaluate(question, ["設定を保存する。"], checks=checks)
    assert result.grade_execution == "partial"
    assert result.required_aspect_coverage == "unknown" and result.missing_aspects == ()


@pytest.mark.parametrize(
    "text,relation", [("未登録の場合は対象を登録して再試行する。", "shared_handling"), ("E-07 対象未登録。登録して再試行する。", "same")]
)
def test_error_abbreviation_and_shared_handling_are_decided_by_same_grade(text, relation):
    verdict = dict(chunk_uid="run:c0", relevant=True, relation="relevant", error_relation=relation, quote=text, reason="原文の共通処理")
    result, _ = evaluate("『対象が未登録です』のエラーへの対処は？", [text], verdicts=[verdict])
    assert result.candidate_verdicts[0]["relation"] == "relevant"
    assert result.rejected_chunk_ids == ()
    assert result.candidate_verdicts[0]["evidence_validation"] == "matched"


def test_different_error_requires_verified_original_and_explicit_semantic_relation():
    text = "別エラー E-99 は通信停止だけの処理で、対象の登録とは関係しません。"
    verdict = dict(
        chunk_uid="run:c0", relevant=False, relation="irrelevant", error_relation="different", quote=text, reason="別処理で共通対処なし"
    )
    result, _ = evaluate("『対象が未登録です』への対処は？", [text], verdicts=[verdict])
    assert result.rejected_chunk_ids == ("run:c0",) and not result.generation_usable
    invalid = {**verdict, "quote": "候補に無い原文"}
    result, _ = evaluate("『対象が未登録です』への対処は？", [text], verdicts=[invalid])
    assert not result.rejected_chunk_ids and result.candidate_verdicts[0]["relation"] == "unknown"


def test_other_number_and_unclear_relation_are_preserved():
    text = "E-99の場合は、先に登録内容を確認します。"
    verdict = dict(chunk_uid="run:c0", relevant=False, relation="unknown", error_relation="unknown", reason="対応不明")
    result, _ = evaluate("『対象が未登録です』への対処は？", [text], verdicts=[verdict])
    assert not result.rejected_chunk_ids and result.candidate_verdicts[0]["relation"] == "unknown"


def test_multiple_sources_jointly_support_an_aspect_without_one_to_one_assignment():
    question = "設定を保存するには？"
    texts = ["設定画面で保存を押す。", "承認済みの場合だけ実行する。"]
    evidence = [dict(source_id=f"run:c{i}", quote=text) for i, text in enumerate(texts)]
    checks = [
        dict(aspect=k, status="supported", source_ids=["run:c0", "run:c1"], evidence=evidence, reason="二つの原文が操作と条件を共同支持")
        for k in _crag_task_contract(question)["required_aspects"]
    ]
    result, _ = evaluate(question, texts, checks=checks)
    assert result.required_aspect_coverage == "complete"
    assert all(c["source_ids"] == ["run:c0", "run:c1"] for c in result.aspect_checks)


def test_related_candidates_without_direct_answer_part_do_not_enable_generation():
    question = "設定を保存するには？"
    checks = [
        dict(
            aspect=k,
            status="supported" if k == "business_object" else "missing",
            evidence=[dict(source_id="run:c0", quote="設定画面")] if k == "business_object" else [],
            source_ids=[],
            reason="画面はあるが保存手順はない",
        )
        for k in _crag_task_contract(question)["required_aspects"]
    ]
    result, _ = evaluate(question, ["設定画面には名前と履歴を表示する。"], checks=checks)
    payload = result.to_payload()
    assert payload["sufficient"] is payload["generation_usable"] is False
    assert payload["required_aspect_coverage"] == "partial" and payload["missing_aspects"]


def test_known_missing_regrades_available_local_material_within_existing_budget():
    first = record("c0", 0, "入力画面を開く。")
    next_record = record("c1", 1, "保存を押して実行する。")
    context = a.AnswerContext([first], first.text, expansion_records=(first, next_record))
    missing = a.CragRetrievalAttempt(
        1,
        "設定を保存するには？",
        ("設定を保存するには？",),
        True,
        missing_aspects=("procedure",),
        recovery_action="neighbor_children",
        recovery_anchor_ids=("run:c0",),
    )
    complete = replace(missing, attempt=2, missing_aspects=(), required_aspect_coverage="complete")
    with (
        patch.object(a, "build_adb_hybrid_answer_context", return_value=context) as search,
        patch.object(a, "_grade_crag_retrieval", side_effect=[missing, complete]) as grade,
    ):
        result, _, attempts = a.build_crag_answer_context(
            "設定を保存するには？", "source", ["docling"], get_settings(environ={}, dotenv_path=None)
        )
    assert search.call_count == 1 and grade.call_count == 2 and len(attempts) == 2
    assert any("保存" in r.text for r in result.records)


def test_original_quote_outside_the_actual_grader_input_is_unverified():
    question = "設定を保存するには？"
    texts = ["設定の画面を開く。", "保存を押して実行する。"]
    checks = [
        dict(
            aspect=k,
            status="supported",
            source_ids=["run:c1"],
            evidence=[dict(source_id="run:c1", quote=texts[1])],
            reason="入力に無い候補を挙げた",
        )
        for k in _crag_task_contract(question)["required_aspects"]
    ]
    records = [record(f"c{i}", i, text) for i, text in enumerate(texts)]
    context = a.AnswerContext(records=records, text="\n".join(texts))
    output = CragRetrievalGradeOutput(
        candidate_verdicts=[], aspect_checks=checks, sufficient=True, confidence=0.9, reason="原文は存在する", rewritten_query=""
    )
    settings = replace(get_settings(environ={}, dotenv_path=None), crag_compact_input=True)
    with patch.object(a, "parse_text_response", return_value=output):
        result = a._grade_crag_retrieval(1, question, question, [question], context, settings, max_candidates=1)
    assert all(c["status"] == "unknown" and c["evidence_validation"] == "invalid_source_or_quote" for c in result.aspect_checks)
    assert result.required_aspect_coverage == "unknown" and result.missing_aspects == ()


@pytest.mark.parametrize("status", ["missing", "unknown"])
def test_section_adapter_expands_only_known_missing_and_keeps_diagnostic_generation_usable(status):
    from rag_engine.generation import section_expansion as sections

    context = a.AnswerContext(records=[record("c0", 0, "入力画面を開く。")], text="入力画面を開く。")
    attempt = a.CragRetrievalAttempt(
        1,
        "設定の手順",
        ("設定の手順",),
        True,
        missing_aspects=("procedure",) if status == "missing" else (),
        unknown_aspects=("procedure",) if status == "unknown" else (),
        generation_usable=True,
        required_aspect_coverage="partial",
    )
    settings = get_settings(environ={}, dotenv_path=None)
    observed = []

    def expand(question, candidate, settings, **kwargs):
        control = kwargs["grade"](candidate, 1)
        observed.append(control)
        return sections.SectionExpansionResult(context=candidate, stop_reason=sections.STOP_SUFFICIENT, trace={})

    with (
        patch("rag_engine.dependencies.section_dependencies", return_value=(lambda **k: None, lambda **k: None)),
        patch.object(a, "_grade_crag_retrieval", return_value=attempt) as grader,
        patch.object(sections, "expand_sections", side_effect=expand),
    ):
        _, result = a._expand_context_by_sections(
            "設定の手順", context, settings, (), answer_llm_provider=None, rerank_enabled=False, started_at=0
        )
    assert grader.call_count == 1
    assert observed[0].sufficient == (status == "unknown")
    assert result.trace["grades"][0]["generation_usable"] is True
    assert result.trace["grades"][0]["sufficient"] is True


def test_rejected_candidate_original_cannot_make_retained_candidate_coverage_complete():
    question = "設定を保存するには？"
    texts = ["別の画面で別対象を保存する。", "設定画面には履歴を表示する。"]
    checks = [
        dict(
            aspect=k,
            status="supported",
            source_ids=["run:c0"],
            evidence=[dict(source_id="run:c0", quote=texts[0])],
            reason="除外する候補を支持に使った",
        )
        for k in _crag_task_contract(question)["required_aspects"]
    ]
    verdicts = [dict(chunk_uid="run:c0", relevant=False, reason="対象違い"), dict(chunk_uid="run:c1", relevant=True, reason="対象画面")]
    result, _ = evaluate(question, texts, checks=checks, verdicts=verdicts)
    assert result.rejected_chunk_ids == ("run:c0",) and not result.generation_usable
    assert result.required_aspect_coverage == "unknown" and not result.missing_aspects
    assert all(c["status"] == "unknown" and not c["source_ids"] for c in result.aspect_checks)


def test_unknown_error_relation_does_not_make_supported_aspects_complete():
    question = "『対象が未登録です』のエラーへの対処は？"
    text = "対象は登録画面で追加して再実行する。"
    result, _ = evaluate(
        question,
        [text],
        verdicts=[
            dict(chunk_uid="run:c0", relevant=True, relation="unknown", error_relation="unknown", quote=text, reason="共通処理か不明")
        ],
    )
    assert not result.generation_usable and result.required_aspect_coverage == "unknown"
    assert not result.missing_aspects
    assert all(c["status"] == "unknown" and c["evidence_validation"] == "matched" for c in result.aspect_checks)


def test_non_error_question_does_not_inherit_a_spurious_unknown_error_relation():
    text = "表示印は確認が必要な行を知らせるものです。"
    result, _ = evaluate(
        "「表示印」の意味は？",
        [text],
        verdicts=[dict(chunk_uid="run:c0", relevant=True, relation="relevant", error_relation="unknown", quote=text, reason="定義がある")],
    )
    assert result.candidate_verdicts[0]["relation"] == "relevant"
    assert result.candidate_verdicts[0]["error_relation"] == "not_applicable"
    assert result.required_aspect_coverage == "complete"
