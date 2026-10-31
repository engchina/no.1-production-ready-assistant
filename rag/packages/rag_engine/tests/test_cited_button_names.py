"""手順の説明が案内したボタン名が、引いたどの原文にも無いときの警告を合成資料で確かめる（#3822）。

保存した実応答と同じ形を匿名の文で作る: 画面を開く手順だけの原文を引いて「入力して実行ボタンを押す」まで書いた説明、
コピーの操作の原文を引いて「実行ボタンで確定する」まで書いた説明。名前の有無だけを見て、意味の支持は判定しない。
"""

from rag_engine.generation.grounded import CITED_BUTTON_CHECK, verify
from rag_engine.generation.operation_audit import button_names_absent
from rag_engine.models.llm import GroundedDraft

OPEN = "メニューから設定を選び、設定タブの登録画面を開きます。"
COPY = '(3)同じ割当にするときは"複写"ボタンを押し、一覧から元にする名前を選択します。'
EXECUTE = "(4)内容を確かめ、実行のボタンを押して登録を完了します。"

SPANS = [
    {"evidence_id": "id-open", "label": "E1", "text": OPEN},
    {"evidence_id": "id-copy", "label": "E2", "text": COPY},
    {"evidence_id": "id-execute", "label": "E3", "text": EXECUTE},
]


def operation(text, *labels, kind="operation"):
    return {"kind": kind, "request_id": "Q1", "text": text, "citations": [{"evidence_id": label} for label in labels]}


def draft(*items):
    labels = list(dict.fromkeys(c["evidence_id"] for item in items for c in item["citations"]))
    texts = {span["label"]: span["text"] for span in SPANS}
    return GroundedDraft.model_validate(
        {
            "request_coverage": [
                {
                    "request_id": "Q1",
                    "evidence_ids": labels,
                    "evidence_scope": [
                        {"evidence_id": label, "target": "対象", "relation": "direct", "link_quote": texts[label]} for label in labels
                    ],
                }
            ],
            "items": list(items),
            "summary": "設定の手順です。",
            "confidence": "high",
        }
    )


def button_marks(entry):
    return [mark for mark in entry.marks if mark["code"] == "item_check" and mark["detail"].startswith(CITED_BUTTON_CHECK)]


def checked_by_text(question, *items):
    checked, _ = verify(question, draft(*items), SPANS)
    return {entry.item.text: entry for entry in checked if not entry.added}


def test_a_button_absent_from_every_cited_unit_marks_only_that_passage_and_keeps_text_and_citation():
    opened = "設定タブの登録画面を開く。"
    detailed = "登録画面で必要な項目を入力し、実行ボタンを押して登録する。"
    by_text = checked_by_text("登録の手順は？", operation(opened, "E1"), operation(detailed, "E1"))
    entry = by_text[detailed]
    # 本文も出典も変えず、外さない。段落に警告だけを付ける。
    assert entry.span["evidence_id"] == "id-open"
    assert not entry.removed and entry.published
    assert entry.state == "warning"
    assert len(button_marks(entry)) == 1
    assert "実行" in button_marks(entry)[0]["detail"]
    # 原文にある操作だけを書いた説明には印を付けない。
    assert not by_text[opened].marks


def test_a_button_named_in_any_cited_unit_is_not_marked():
    text = "「複写」ボタンを押して元の名前を選び、実行ボタンで登録を完了する。"
    by_text = checked_by_text("割当の手順は？", operation(text, "E2", "E3"))
    assert not button_marks(by_text[text])
    # 複写の原文だけを引いたときは、実行の名前がどこにも無い。
    by_text = checked_by_text("割当の手順は？", operation(text, "E2"))
    assert [mark["detail"].split("。")[0] for mark in button_marks(by_text[text])] == [f"{CITED_BUTTON_CHECK}：引いた原文に無い: 実行"]


def test_names_written_differently_in_the_source_or_named_by_the_question_are_not_marked():
    cited = [EXECUTE, '"複写"ボタン', "(F5)確定 / (F1)戻る"]
    # 「実行のボタン」「"複写"ボタン」「(F5)確定」の書き方の違いでは判定しない。
    assert button_names_absent("実行ボタンを押し、複写ボタンとF5確定ボタンを使う。", cited) == []
    # 質問にある名前・1 字の名前・総称語は対象にしない。
    assert button_names_absent("保存ボタンを押す。", cited, question="保存ボタンはどこ？") == []
    assert button_names_absent("各ボタンとデータボタンを押す。", cited) == []
    assert button_names_absent("保存ボタンを押す。", cited) == ["保存"]


def test_non_procedure_and_already_marked_passages_get_no_button_mark():
    rule = "実行ボタンは管理者だけが使える。"
    by_text = checked_by_text("登録の手順は？", operation(rule, "E1", kind="rule"))
    assert not button_marks(by_text[rule])
    # 計画に写した原文が出典の根拠に無い（出典そのものが確かでない）説明には、ボタン名の印を重ねない。
    shifted = GroundedDraft.model_validate(
        {
            "request_coverage": [
                {
                    "request_id": "Q1",
                    "evidence_ids": ["E1"],
                    "evidence_scope": [{"evidence_id": "E1", "target": "対象", "relation": "direct", "link_quote": EXECUTE}],
                }
            ],
            "items": [operation("必要な項目を入力し、保存ボタンを押す。", "E1")],
            "summary": "設定の手順です。",
            "confidence": "high",
        }
    )
    checked, _ = verify("登録の手順は？", shifted, SPANS)
    entry = next(entry for entry in checked if not entry.added)
    assert any(mark["code"] == "unassessed" for mark in entry.marks)
    assert not button_marks(entry)
