"""#368: 表の後に続く番号付き操作を本文予算で欠落させない。"""

from rag_engine.retrieval.evidence_selection import evidence_excerpt

QUESTION = "新しい配送業者の業者コードが配送業者登録画面の一覧に見当たらない。どうすればよいか。"
STEPS = [
    "１. 業者の基本情報を入力します。",
    "２. 対応できる配送区分を選択します。",
    "３. 備考を入力します。",
    "４. 実行ボタンを押して登録を完了します。",
]


def test_table_does_not_displace_completion_steps():
    text = (
        "配送業者を登録する画面です。\nB【操作説明】\n"
        + STEPS[0]
        + "\n新規追加は採番ボタンで業者コードを自動採番します。\n"
        + ("業者コード 配送業者登録 配送区分\n" * 100)
        + "\n".join(STEPS[1:])
    )
    excerpt = evidence_excerpt(QUESTION, text, 700)
    for step in STEPS:
        assert step in excerpt
    assert "自動採番" in excerpt
    assert len(excerpt) <= 700
    assert "OK" not in excerpt and "管理業者入力" not in excerpt


def test_unrelated_numbered_labels_are_not_mandatory_steps():
    text = "１. 業者コードの例\n" + "説明\n" * 200 + "目的の定義。"
    excerpt = evidence_excerpt("目的の定義は？", text, 40)
    assert "目的の定義" in excerpt
