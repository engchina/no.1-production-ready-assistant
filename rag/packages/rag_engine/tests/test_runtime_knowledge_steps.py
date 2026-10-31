"""処理の経過の「用語・同義語の確認」と「回答ルールの確認」（#3451）。

検索・回答プロファイルの知識の用語・同義語（別名を検索に使う）と回答ルール（回答の参考にする）を、別の工程
（別の trace の span。同じ親の子でこの順）に記録し、それぞれの登録の数・一致・影響を示すことを、合成の登録と
質問で確かめる（外部のサービスには接続しない）。
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from grounded_stub import echo_model
from test_answer_generation import _chunk_run_stub, _hybrid_result, _rerank_ready_settings, _stored_child

from rag_engine.adapters.oci import RerankTextRank
from rag_engine.generation import answering
from rag_engine.generation.answering import SIMPLE_RETRIEVAL_LABEL, STANDARD_ANSWER_FLOW_LABEL, answer_question_result
from rag_engine.resources.tracing import bind_trace_observer

TERMS_STEP = "用語・同義語の確認"
RULES_STEP = "回答ルールの確認"

_TERM = {"term": "合成用語A", "aliases": ["合成別名A"], "description": "合成の説明"}
_INACTIVE_TERM = {"term": "合成用語Z", "status": "deprecated"}
_RULE = {"id": "rule-a", "title": "合成回答ルールA", "triggers": ["合成の期限"], "content": "合成のルールの本文"}
_INACTIVE_RULE = {"id": "rule-z", "title": "合成回答ルールZ", "triggers": ["合成の下書き"], "content": "未承認", "status": "draft"}


class _SpanRecorder:
    """工程の観測（span）の開いた順と親（開いている span）を記録する。"""

    def __init__(self) -> None:
        self.spans: list[tuple[str, str | None]] = []
        self._stack: list[str] = []

    @contextmanager
    def observe(self, operation: str, model: str) -> Iterator["_SpanRecorder"]:
        name = f"{operation}:{model}"
        self.spans.append((name, self._stack[-1] if self._stack else None))
        self._stack.append(name)
        try:
            yield self
        finally:
            self._stack.pop()

    def usage(self, value: object) -> None:
        pass

    def display(self, kind: str | None = None, params: Mapping[str, object] | None = None, details: Any = None) -> None:
        pass


def _write(path: Path, *, terms: list[dict] | None = None, rules: list[dict] | None = None) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "terms": terms or [], "rules": rules or []}, ensure_ascii=False), encoding="utf-8")
    return path


def _run(knowledge_path: Path, question: str = "合成用語Aの合成の期限は？", observer: _SpanRecorder | None = None):
    settings = replace(_rerank_ready_settings(), runtime_knowledge_path=knowledge_path)
    search_result = _hybrid_result(_stored_child("chunk-docling-c000001", 1, "合成の本文"))
    with (
        patch("rag_engine.generation.answering.load_latest_or_source_chunk_run", return_value=_chunk_run_stub()),
        patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
        patch("rag_engine.generation.answering.search_adb_hybrid_chunks", return_value=search_result),
        patch("rag_engine.generation.answering.rerank_text_with_scores", return_value=[RerankTextRank(0, 0.9)]),
        patch("rag_engine.generation.answering.parse_text_response", side_effect=echo_model),
    ):
        if observer is None:
            return answer_question_result(
                question,
                "abcdef",
                ["docling"],
                settings,
                query_strategy=SIMPLE_RETRIEVAL_LABEL,
                answer_flow=STANDARD_ANSWER_FLOW_LABEL,
            )
        with bind_trace_observer(observer.observe):
            return answer_question_result(
                question,
                "abcdef",
                ["docling"],
                settings,
                query_strategy=SIMPLE_RETRIEVAL_LABEL,
                answer_flow=STANDARD_ANSWER_FLOW_LABEL,
            )


def _steps(result) -> dict[str, dict]:
    return {step["name"]: step for step in result.execution_steps}


def _fields(step: dict) -> dict[str, str]:
    return {field["label"]: field["value"] for field in step.get("details", {}).get("fields", [])}


def test_terms_and_rules_are_separate_steps_in_order(tmp_path: Path) -> None:
    """2 つの工程は別の記録・別の span で、同じ親の子として用語・同義語 → 回答ルールの順に並ぶ。"""
    recorder = _SpanRecorder()
    result = _run(_write(tmp_path / "rk.json", terms=[_TERM], rules=[_RULE]), observer=recorder)

    names = [step["name"] for step in result.execution_steps]
    assert "用語・ルールの確認" not in names
    index = names.index(TERMS_STEP)
    assert names[index + 1] == RULES_STEP
    steps = result.execution_steps
    assert steps[index]["depth"] == steps[index + 1]["depth"]
    for step in (steps[index], steps[index + 1]):
        assert step["status"] == "complete"
        assert step["elapsed_seconds"] >= 0

    spans = [(name, parent) for name, parent in recorder.spans if name in {f"stage:{TERMS_STEP}", f"stage:{RULES_STEP}"}]
    assert [name for name, _ in spans] == [f"stage:{TERMS_STEP}", f"stage:{RULES_STEP}"]
    # 同じ親の子（片方がもう片方の中に入れ子にならない）。
    assert spans[0][1] == spans[1][1]


def test_matched_terms_and_rules_show_their_own_counts_matches_and_impacts(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "rk.json",
        terms=[_TERM, _INACTIVE_TERM],
        rules=[_RULE, _RULE | {"id": "rule-b", "title": "合成回答ルールB", "triggers": ["合成の別条件"]}, _INACTIVE_RULE],
    )
    steps = _steps(_run(path))

    terms = _fields(steps[TERMS_STEP])
    assert terms["目的"] == "登録済みの用語・同義語を質問に当て、別名を検索に使います。"
    assert terms["結果"] == "一致: 合成用語A"
    assert terms["有効な登録"] == "1 件（登録 2 件）"
    assert terms["一致した用語"] == "合成用語A（別名: 合成別名A）"
    assert terms["影響"] == "一致した用語の別名を補助の検索文に加えます。"
    assert "採用した確認・回答ルール" not in terms

    rules = _fields(steps[RULES_STEP])
    assert rules["目的"] == "確認・回答ルールの用途・優先度・排他設定から、確認と回答の指示を選びます。"
    assert rules["結果"] == "一致: 合成回答ルールA"
    assert rules["有効な登録"] == "2 件（登録 3 件）"
    assert rules["採用した確認・回答ルール"] == "合成回答ルールA"
    assert rules["影響"] == "回答生成に使う指示 1 件を渡します。"
    assert "一致した用語" not in rules and "一致したルール" not in rules


def test_no_registration_guides_to_each_tab(tmp_path: Path) -> None:
    """登録が無い（ファイルが無い）ときは、それぞれの登録先のタブへ案内し、件数と影響は出さない。"""
    steps = _steps(_run(tmp_path / "missing.json"))

    terms = _fields(steps[TERMS_STEP])
    rules = _fields(steps[RULES_STEP])
    assert terms["結果"] == "未登録：検索・回答プロファイルの「プロファイルの知識」の「用語・同義語」で登録できます"
    assert rules["結果"] == "未登録：検索・回答プロファイルの「プロファイルの知識」の「回答ルール」で登録できます"
    for fields in (terms, rules):
        assert "有効な登録" not in fields and "影響" not in fields


def test_only_one_kind_registered_is_distinguished(tmp_path: Path) -> None:
    """用語・同義語だけを登録したとき、回答ルールの工程は未登録として示す（件数も種類ごと）。"""
    steps = _steps(_run(_write(tmp_path / "rk.json", terms=[_TERM])))

    assert _fields(steps[TERMS_STEP])["有効な登録"] == "1 件（登録 1 件）"
    rules = _fields(steps[RULES_STEP])
    assert rules["結果"].startswith("未登録：")
    assert rules["有効な登録"] == "0 件（登録 0 件）"
    assert "影響" not in rules


def test_no_match_shows_result_without_impact(tmp_path: Path) -> None:
    path = _write(tmp_path / "rk.json", terms=[_TERM], rules=[_RULE])
    steps = _steps(_run(path, question="合成の別の質問は？"))

    terms = _fields(steps[TERMS_STEP])
    rules = _fields(steps[RULES_STEP])
    assert terms["結果"] == "一致なし：登録済みの有効な用語・同義語に一致しませんでした"
    assert rules["結果"] == "一致なし：登録済みの有効な回答ルールに一致しませんでした"
    assert terms["有効な登録"] == "1 件（登録 1 件）" and rules["有効な登録"] == "1 件（登録 1 件）"
    for fields in (terms, rules):
        assert "影響" not in fields
    assert "一致した用語" not in terms and "採用した確認・回答ルール" not in rules


def test_only_inactive_registrations_guide_to_the_use_setting(tmp_path: Path) -> None:
    steps = _steps(_run(_write(tmp_path / "rk.json", terms=[_INACTIVE_TERM], rules=[_INACTIVE_RULE])))

    for name, tab in ((TERMS_STEP, "用語・同義語"), (RULES_STEP, "回答ルール")):
        fields = _fields(steps[name])
        assert fields["結果"].startswith(f"有効な登録なし：検索・回答プロファイルの「プロファイルの知識」の「{tab}」")
        assert fields["有効な登録"] == "0 件（登録 1 件）"


def test_load_error_continues_in_both_steps_and_is_reported_once(tmp_path: Path) -> None:
    path = tmp_path / "rk.json"
    path.write_text("{broken", encoding="utf-8")
    reported: list[str] = []
    with patch.object(answering, "report_degraded", side_effect=lambda kind, exc: reported.append(kind)):
        steps = _steps(_run(path))

    assert reported.count("runtime_knowledge") == 1
    for name in (TERMS_STEP, RULES_STEP):
        assert steps[name]["status"] == "補助情報なしで続行"
        assert "読込エラー: JSONDecodeError" in steps[name]["details"]["lines"]


@pytest.mark.parametrize("scope", [("合成の条件 A", ())], ids=["scope"])
def test_chosen_conditions_are_shown_in_the_rules_step(tmp_path: Path, scope: tuple[str, tuple[str, ...]]) -> None:
    """確認で選んだ条件（回答ルールの確認の答え）は、回答ルールの工程に出す。"""
    settings = replace(_rerank_ready_settings(), runtime_knowledge_path=_write(tmp_path / "rk.json", rules=[_RULE]))
    with (
        patch("rag_engine.generation.answering.load_latest_or_source_chunk_run", return_value=_chunk_run_stub()),
        patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
        patch(
            "rag_engine.generation.answering.search_adb_hybrid_chunks",
            return_value=_hybrid_result(_stored_child("chunk-docling-c000001", 1, "合成の本文")),
        ),
        patch("rag_engine.generation.answering.rerank_text_with_scores", return_value=[RerankTextRank(0, 0.9)]),
        patch("rag_engine.generation.answering.parse_text_response", side_effect=echo_model),
    ):
        result = answer_question_result(
            "合成の期限は？",
            "abcdef",
            ["docling"],
            settings,
            query_strategy=SIMPLE_RETRIEVAL_LABEL,
            answer_flow=STANDARD_ANSWER_FLOW_LABEL,
            scope=scope,
        )
    steps = _steps(result)
    assert "確認で選んだ条件: 合成の条件 A" in steps[RULES_STEP]["details"]["lines"]
    assert not any("確認で選んだ条件" in line for line in steps[TERMS_STEP].get("details", {}).get("lines", []))


def test_confirmation_only_rules_describe_confirmation_without_generation_impact(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "rk.json",
        rules=[_RULE | {"purpose": "confirmation", "content": "", "clarification": {"question": "対象は？", "options": []}}],
    )
    rules = _fields(_steps(_run(path))[RULES_STEP])
    assert rules["採用した確認・回答ルール"] == "合成回答ルールA"
    assert rules["影響"] == "回答前の確認に使う設定 1 件を採用しました。"
    assert "回答生成に使う" not in str(rules)
