"""根拠のブロックの終わりの印（#3806）。本文を隣の根拠の ID に結び付けないよう、各ブロックを自分の ID で閉じる。"""

from typing import Any

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedCitation, GroundedItem

# 補助情報が長く、本文が短い根拠（保存入力で出典を取り違えた形）。
NOTE = "出典の補助情報。" * 60
FIRST = "メニューから設定を選び、登録画面を開きます。"
SECOND = "(1)割当画面を開きます。\n(2)名前を一覧から選択します。"


def span(label: str, text: str) -> dict[str, Any]:
    return {
        "evidence_id": "id-" + label,
        "label": label,
        "source_id": "parent-" + label,
        "source": "synthetic.pdf",
        "text": text,
        "function": ("synthetic.pdf", "合成の説明"),
        "tags": [],
        "answer_context": NOTE,
    }


def test_each_block_closes_with_its_own_label_right_after_the_body():
    spans = [span("E1", FIRST), span("E2", SECOND)]
    packet = grounded.format_functions(spans)
    for evidence in spans:
        label = evidence["label"]
        view = grounded._citation_view(evidence)
        assert view.rendered.startswith(f"[{label}] ")
        assert view.rendered.endswith("\n[/" + label + "]")
        # 本文の最後の行の直後が自分の終わりの印（次の根拠の見出しではない）。
        last_line = evidence["text"].splitlines()[-1]
        assert view.rendered.index(f"[/{label}]") - view.rendered.rindex(last_line) == len(last_line) + 1
    # 終わりの印は本文と次の根拠の見出しの間にある。
    assert packet.index("[/E1]") < packet.index("[E2] ")


def test_the_end_line_does_not_change_units_or_extracted_quotes():
    evidence = span("E2", SECOND)
    grounded.format_functions([evidence])  # 生成の表示（今の契約）を記録し、取り出しも同じ単位で読む。
    view = grounded._citation_view(evidence)
    assert view.contract_version == grounded.CITATION_UNIT_CONTRACT
    assert all("[/E2]" not in evidence["text"][a:b] for a, b in view.units)
    last = str(len(view.units))
    item = GroundedItem(kind="operation", text="名前を選びます。", citations=[GroundedCitation(evidence_id="E2", lines=last)])
    quoted, error = grounded.cited_quote(item, [evidence])
    assert not error and "[/E2]" not in quoted.quote and quoted.quote in SECOND


def test_the_input_description_names_the_end_line():
    # 入力の説明（1 か所）が、始まりの行と終わりの行の両方を示す。
    assert "`[E1] p.頁" in grounded.GENERATE_SYSTEM_PROMPT and "`[/E1]` の行で終わる" in grounded.GENERATE_SYSTEM_PROMPT
