"""説明の配置と、根拠・手順の対応の不確定を分離する。"""

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedCitation, GroundedItem


def checked(text: str, quote: str, span: dict, *, kind: str = "operation") -> grounded.CheckedItem:
    return grounded.CheckedItem(
        GroundedItem(kind=kind, text=text, citations=[GroundedCitation(evidence_id="E1", quote=quote)]),
        span=span,
    )


def test_repeated_main_sentences_in_shared_evidence_do_not_hide_explained_step() -> None:
    first = "条件を入力して一覧を抽出します。"
    second = "確認画面へ戻って検証結果を出力します。"
    # 同じ主の操作を述べる文が二つあるため、従来の一意な最長の文は決まらない。
    span = {"source_id": "shared", "evidence_id": "E1", "text": first + second + "確認画面へ戻って検証結果を出力してください。"}
    steps = (("a", "条件を入力して一覧を抽出する", ("shared",)), ("b", "確認画面へ戻って検証結果を出力する", ("shared",)))
    entries = [checked(first, first, span), checked(second, second, span, kind="rule")]
    coverage, _ = grounded.guide_step_assignment(steps, entries, [span])
    assert [step["status"] for step in coverage] == ["unknown", "unknown"]
    assert [step["assignment"]["candidate_status"] for step in coverage] == ["matched", "matched"]
    assert coverage[1]["assignment"]["assigned_item_indexes"] == [1]
    assert coverage[1]["assignment"]["candidates"][1]["method"] == "distinctive_operation"


def test_common_action_does_not_complete_distinct_operations() -> None:
    text = "条件を入力します。確認画面で検証結果を出力します。最後に実行ボタンを押します。"
    span = {"source_id": "shared", "evidence_id": "E1", "text": text}
    steps = (("a", "条件を入力する", ("shared",)), ("b", "確認画面で検証結果を出力する", ("shared",)))
    entries = [checked("最後に実行ボタンを押します。", "最後に実行ボタンを押します。", span)]
    coverage, _ = grounded.guide_step_assignment(steps, entries, [span])
    assert all(step["status"] == "unknown" for step in coverage)
    assert all(step["assignment"]["candidate_status"] == "unmatched" for step in coverage)
    assert all(not step["assignment"]["assigned_item_indexes"] for step in coverage)


def test_unresolved_assignment_does_not_claim_the_document_has_no_evidence() -> None:
    line = grounded.guide_step_gap_line({"title": "操作 A", "status": "unused"})
    assert "対応を確定できていません" in line
    assert "資料で説明を確かめられませんでした" not in line


def test_deduplicated_source_alias_is_kept_in_prompt_and_coverage() -> None:
    span = {"source_id": "canonical", "source_aliases": ["guide-source"], "evidence_id": "E1", "label": "E1", "text": "設定を開く。"}
    steps = (("a", "設定を開く", ("guide-source",)),)
    entries = [checked("設定を開きます。", "設定を開く。", span)]
    assert grounded.guide_step_evidence_lines(steps, [span]) == ["- a「設定を開く」: E1"]
    coverage, _ = grounded.guide_step_assignment(steps, entries, [span])
    assert coverage[0]["status"] == "unknown"
    assert coverage[0]["assignment"]["candidate_status"] == "matched"
    assert coverage[0]["evidence_flow"] == {"linked_source_count": 1, "context_evidence_ids": ["E1"], "cited_evidence_ids": ["E1"]}
