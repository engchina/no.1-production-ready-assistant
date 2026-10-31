"""明細確認の要求と、同じ機能の選択から完了までの本文を保持する。"""

from rag_engine.retrieval.evidence_selection import evidence_excerpt
from rag_engine.retrieval.result_granularity import aggregate_only_evidence
from rag_engine.retrieval.task_contract import LIST_REQUEST, explicit_requests, task_contract


def test_member_request_requires_method_and_population_boundary():
    contract = task_contract("月次集計に計上されている取引明細を確認したい。")
    units = contract["request_units"]
    assert LIST_REQUEST in explicit_requests("月次集計に計上されている取引明細を確認したい。")
    assert "procedure" in contract["required_aspects"]
    assert {"member_details", "population_membership"} <= set(contract["required_aspects"])
    assert {u["id"] for u in units} >= {"Q1.M1", "Q1.M2"}
    assert "最終集計" in next(u["text"] for u in units if u["id"] == "Q1.M2")


def test_aggregate_only_request_does_not_add_member_requirements():
    units = task_contract("月次来店者の人数を確認したい。")["request_units"]
    assert not any(".M" in u["id"] for u in units)
    assert not any(".R" in u["id"] or ".O" in u["id"] for u in units)


def test_parallel_objects_and_chained_operations_become_separate_requests():
    """「AとBを追加」「変更して再発行」は対象・操作ごとの子要求にする。一方だけで addressed にしない (#622)。"""
    units = task_contract("倉庫と棚番を追加したい。")["request_units"]
    by_id = {u["id"]: u["text"] for u in units}
    assert {"Q1.R1", "Q1.R2"} <= set(by_id)
    assert "「倉庫」の追加" in by_id["Q1.R1"] and "「棚番」の追加" in by_id["Q1.R2"]
    assert all(u["request_id"] == "Q1" for u in units if u["id"].startswith("Q1.R"))

    units = task_contract("出荷数量を変更して出荷指示書を再発行したい。")["request_units"]
    by_id = {u["id"]: u["text"] for u in units}
    assert "「変更」" in by_id["Q1.O1"] and "「再発行」" in by_id["Q1.O2"]
    # 原文の語だけを使い、対象や効果を補わない。
    assert all(part in "出荷数量を変更して出荷指示書を再発行したい。" for u in units for part in [u["text"].split("原文: ")[-1]])


def test_circled_steps_survive_report_title_and_sample_repetition():
    steps = [
        "⑭対象月を入力します。",
        "⑮明細の一覧表を選択します。",
        "⑯出力形式を選択します。",
        "⑰並び順を選択します。",
        "⑱実行ボタンを押します。",
    ]
    text = ("月次集計 人数 例示 8名\n" * 100) + "\n".join(steps)
    excerpt = evidence_excerpt("月次集計に計上されている明細を確認したい。", text, 300)
    assert all(step in excerpt for step in steps)
    assert len(excerpt) <= 300


def test_counts_cannot_prove_details_but_explicit_details_remain_candidates():
    assert aggregate_only_evidence([{"text": "集計表に人数を表示します。\n検索語: 個人 明細"}])
    assert not aggregate_only_evidence([{"text": "個人一覧に氏名と対象区分を表示します。"}])
    assert not aggregate_only_evidence([])


def test_parallel_objects_accept_comma_and_ya_separators():
    """実入力の並列対象は読点区切りが多い。「と」以外の区切りでも子要求にし、述語の直後の読点は対象にしない (#702)。"""
    for question in ("倉庫、棚番を追加したい。", "倉庫や棚番の追加はどうすればよいか。", "倉庫及び棚番を追加したい。"):
        by_id = {u["id"]: u["text"] for u in task_contract(question)["request_units"]}
        assert "「倉庫」の追加" in by_id["Q1.R1"] and "「棚番」の追加" in by_id["Q1.R2"], question
    units = task_contract("取引先名が変わった、伝票タイトルを変更したい。")["request_units"]
    assert not any(".R" in u["id"] for u in units)
