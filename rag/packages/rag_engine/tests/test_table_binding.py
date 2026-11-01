"""表・一覧の行の対応に反して、別の行の値を述べた文の検出（#2659）。根拠・文は合成。"""

import pytest

from rag_engine.generation import grounded
from rag_engine.generation.answer_policy import RELATION_SUPPORT_RULES
from rag_engine.generation.table_binding import evidence_tables, unbound_table_values
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem

# 区分と選択肢の値の表（見出しは <th>）。区分の名前はローマ数字だけが違う。
TABLE = (
    "<table><tr><th>区分</th><th>選択肢の値</th></tr>"
    "<tr><td>区分Ⅰ</td><td>01</td></tr>"
    "<tr><td>区分Ⅱ</td><td>02</td></tr>"
    "<tr><td>区分Ⅲ</td><td>03</td></tr></table>"
)


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        # 正しい対応は残す。
        ("区分Ⅱでは選択肢の値 02 を選びます。", []),
        ("区分Ⅰは 01、区分Ⅱは 02 を選びます。", []),
        # 隣の行の値を述べた文。
        ("区分Ⅱでは選択肢の値 01 を選びます。", [("01", "区分Ⅱ")]),
        ("区分Ⅱでは選択肢の値 03 を選びます。", [("03", "区分Ⅱ")]),
        # 1 文の中で入れ替えた対応は、読点で区切った区間ごとに見る。
        ("区分Ⅰは 02、区分Ⅱは 01 を選びます。", [("01", "区分Ⅱ"), ("02", "区分Ⅰ")]),
        # 値の無い区間（「区分Ⅱの場合、」）は続く区間と合わせて見る。
        ("区分Ⅱの場合、01 を選びます。", [("01", "区分Ⅱ")]),
        # 似た表記（全角・半角のローマ数字）も同じ区分として照合する。
        ("区分IIでは 01 を選びます。", [("01", "区分Ⅱ")]),
        # 見出しの行の語だけを述べた文は疑わない。
        ("選択肢の値は 01 です。", []),
        # 根拠の ID・頁の参照の数字を値として照合しない。
        ("区分Ⅱでは 02 を選びます（p.1）【証拠 ID 0123abcd:1:1】", []),
    ],
    ids=["correct", "both-correct", "adjacent-row", "other-row", "swapped-pair", "condition-clause", "roman-width", "header", "citation"],
)
def test_values_from_another_row_are_found(claim: str, expected: list[tuple[str, str]]) -> None:
    assert unbound_table_values(claim, [TABLE]) == expected


# コード・名称・開始・終了・単価の表（見出しの印が無く、値の無い最初の行を見出しとみなす。終了は空欄）。
PRICE_TABLE = (
    "<table><tr><td>コード</td><td>名称</td><td>開始</td><td>終了</td><td>単価</td></tr>"
    "<tr><td>A101</td><td>項目甲</td><td>202504</td><td></td><td>254</td></tr>"
    "<tr><td>A102</td><td>項目乙</td><td>202504</td><td></td><td>253</td></tr>"
    "<tr><td>A103</td><td>項目丙</td><td>202504</td><td></td><td>300</td></tr></table>"
)


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        # 複数の行の値の列挙・範囲は、区分と値の結び付けではない（#2734）。
        ("E 列が 202504、F 列が空欄、Q 列に 254・253・300 などの単価が表示されている。", []),
        ("対象はコード A101〜A103 で、単価は 254 から 300 です。", []),
        # 同じ行のコードと値。同じ行のコードがあれば、別の行の名前を述べても疑わない。
        ("A101 の単価は 254 です。", []),
        ("A101 の単価は 254 で項目乙より高い。", []),
        # 別の行の名前と値の結び付けは検出する。
        ("項目甲の単価は 253 です。", [("253", "項目甲")]),
    ],
    ids=["value-list", "value-range", "same-row-code", "same-row-code-with-other-name", "other-row-name"],
)
def test_values_listed_from_several_rows_are_not_suspected(claim: str, expected: list[tuple[str, str]]) -> None:
    assert unbound_table_values(claim, [PRICE_TABLE]) == expected


def test_shared_values_spanned_cells_and_notes_are_not_suspected() -> None:
    shared = "<table><tr><td>区分Ⅰ</td><td>01</td></tr><tr><td>区分Ⅱ</td><td>01</td></tr></table>"
    assert unbound_table_values("区分Ⅰと区分Ⅱはどちらも 01 です。", [shared]) == []
    # rowspan の区分は覆う行の値にも対応する。
    spanned = "<table><tr><td rowspan=2>区分Ⅰ</td><td>01</td></tr><tr><td>02</td></tr><tr><td>区分Ⅱ</td><td>03</td></tr></table>"
    assert unbound_table_values("区分Ⅰでは 02 も選べます。", [spanned]) == []
    assert unbound_table_values("区分Ⅱでは 02 を選びます。", [spanned]) == [("02", "区分Ⅱ")]
    # 「名前：値」の行の並び。値の無い注記の行の語は、行を見分ける手がかりにしない。
    pairs = "一般：01\n特別：02\n注意：入力は必須"
    assert unbound_table_values("特別の場合は 02 を指定する。", [pairs]) == []
    assert unbound_table_values("一般の場合は 02 を指定する。", [pairs]) == [("02", "一般")]
    assert unbound_table_values("注意して 01 を指定する。", [pairs]) == []
    # 手順の番号・数量の 1 文字の数字を、表の 1 文字の値として照合しない。
    digits = "<table><tr><td>区分Ⅰ</td><td>1</td></tr><tr><td>区分Ⅱ</td><td>2</td></tr></table>"
    assert unbound_table_values("1. 区分Ⅱの値を選びます。", [digits]) == []
    assert unbound_table_values("手順1で区分Ⅱを選びます。", [digits]) == []
    assert unbound_table_values("区分Ⅱでは「1」を選びます。", [digits]) == [("1", "区分Ⅱ")]
    # 時刻のコロンは区切りにしない（時刻は値として照合する）。
    times = "区分Ⅰ：9:00\n区分Ⅱ：10:00"
    assert unbound_table_values("区分Ⅱは 10:00 に始まる。", [times]) == []
    assert unbound_table_values("区分Ⅱは 9:00 に始まる。", [times]) == [("9:00", "区分Ⅱ")]


def test_markdown_tables_and_other_tables_binding_the_value() -> None:
    markdown = "| 区分 | 値 |\n|---|---|\n| 区分Ⅰ | 01 |\n| 区分Ⅱ | 02 |"
    assert unbound_table_values("区分Ⅱは 01 です。", [markdown]) == [("01", "区分Ⅱ")]
    # 同じ値を別の表が同じ行の区分と結び付けていれば、表をまたいで疑わない。
    other = "<table><tr><td>区分Ⅱ</td><td>01</td></tr><tr><td>区分Ⅳ</td><td>04</td></tr></table>"
    assert unbound_table_values("区分Ⅱは 01 です。", [markdown, other]) == []
    assert len(evidence_tables(TABLE)) == 1 and len(evidence_tables(TABLE)[0].rows) == 3  # 見出しの行は除く
    assert unbound_table_values("区分Ⅱは 01 です。", ["区分Ⅰは 01 です。区分Ⅱは 02 です。"]) == []  # 表でない本文は見ない


def test_word_table_with_vertical_merge_filled_binds_each_row_to_its_label() -> None:
    """Word の表の縦の結合の続きを前処理が埋めた表では、続きの行の値もその区分に結び付く（#3565）。"""
    filled = (
        "| 分類 | 名称 | 番号 | 設定 |\n| --- | --- | --- | --- |\n"
        "| 分類 K | 名称 k1 | 101 | 有効 |\n| 分類 K | 名称 k2 | 102 | 無効 |\n"
        "| 分類 L | 名称 l1 | 201 | 有効 |"
    )
    assert unbound_table_values("分類 K の番号は 102 です。", [filled]) == []
    assert unbound_table_values("分類 K の名称 k2 の番号は 102 です。", [filled]) == []
    # 別の区分の行の値は今までどおり疑う。
    assert unbound_table_values("分類 L の番号は 102 です。", [filled]) == [("102", "分類 L")]


@pytest.mark.parametrize(
    "number",
    ["5.4", "6.2.1", "step-3", "step‑3", "step−3", "５．４", "-3", "+3", "−3"],
    ids=["section", "nested-section", "step", "nonbreaking-hyphen", "minus", "full-width", "negative", "positive-sign", "unicode-negative"],
)
def test_number_parts_are_not_independent_table_values(number: str) -> None:
    """節・小数・コードの内部や符号付きの数を、別の行の正の値として扱わない。"""
    table = "| 番号 | 名称 |\n|---|---|\n| 2 | 項目甲 |\n| 3 | 項目乙 |\n| 4 | 項目丙 |\n| 5 | 項目丁 |\n| 6 | 項目戊 |"
    assert unbound_table_values(f"項目甲については「{number}」を参照する。", [table]) == []


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("「5.4 区分A・登録保留との順序」を参照する。", []),
        ("**3．登録期限（step-3）**", []),
        ("登録保留の期間は「4」です。", []),
        ("仮登録の番号は「4」です。", []),
        ("登録データは「4」です。", []),
        ("登録では「2」を指定する。", []),
        ("登録では「4」を指定する。", [("4", "登録、変更、取消等")]),
        ("「登録、変更、取消等」では「4」を指定する。", [("4", "登録、変更、取消等")]),
    ],
    ids=["section-and-compound", "heading", "suffix", "prefix", "katakana-suffix", "correct-row", "wrong-row", "full-cell-wrong-row"],
)
def test_shortened_cell_label_does_not_match_inside_a_compound(claim: str, expected: list[tuple[str, str]]) -> None:
    """読点で切ったセルの頭は、複合語の一部分だけでは行の手がかりにしない（#2752）。"""
    table = "| 章 | 操作 |\n|---|---|\n| 2 | 登録、変更、取消等 |\n| 3 | 参照、出力 |\n| 4 | 照会、抽出 |\n| 5 | 相談、案内 |"
    assert unbound_table_values(claim, [table]) == expected


@pytest.mark.parametrize("value", ["1", "A", "B2", "3.2", "step-3"])
def test_standalone_values_still_detect_wrong_rows(value: str) -> None:
    table = f"| 名称 | 値 |\n|---|---|\n| 項目甲 | {value} |\n| 項目乙 | 9 |"
    assert unbound_table_values(f"項目乙では「{value}」を指定する。", [table]) == [(value, "項目乙")]


def test_grounded_item_with_a_value_from_another_row_is_removed_with_the_right_value() -> None:
    """表の別の行の値を結び付けた説明は、根拠と矛盾した説明として外し、表の正しい対応を理由に出す（#2998）。"""
    question = "区分Ⅱで選ぶ選択肢の値は？"
    spans = grounded.tag_spans(
        question, [{"evidence_id": "E1", "source_id": "c1", "source": "m.pdf", "page": 1, "text": TABLE, "section_path": []}]
    )
    wrong = GroundedItem(
        kind="rule", text="区分Ⅱの選択肢の値は 01 です。", citations=[GroundedCitation(evidence_id="E1", quote="区分Ⅱ 02")]
    )
    right = GroundedItem(
        kind="rule", text="区分Ⅱの選択肢の値は 02 です。", citations=[GroundedCitation(evidence_id="E1", quote="区分Ⅱ 02")]
    )

    def checked(item: GroundedItem) -> grounded.CheckedItem:
        entries, _ = grounded.verify(question, GroundedDraft(summary="", confidence="high", items=[item]), spans)
        return next(entry for entry in entries if entry.item.text == item.text)

    entry = checked(wrong)
    assert entry.removed and not entry.published
    assert [mark["code"] for mark in entry.marks] == ["contradicted"]
    assert entry.marks[0]["detail"].startswith("根拠の表では『区分Ⅱ』の値は『02』（『01』は別の行の値）です。")
    assert "原文「区分Ⅱ | 02」" in entry.marks[0]["detail"]
    kept = checked(right)
    assert not kept.removed and kept.reasons == [] and kept.state == ""


def test_table_mismatch_notes_name_the_right_row_or_column_value() -> None:
    from rag_engine.generation.table_binding import table_mismatch_notes

    assert table_mismatch_notes([("01", "区分Ⅱ")], [TABLE]) == ["『区分Ⅱ』の値は『02』（『01』は別の行の値）"]
    revised = "| 項目 | 改定前 | 改定後 |\n|---|---|---|\n| 申請の期限 | 30日以内 | 60日以内 |\n| 保存期間 | 3年 | 5年 |"
    pairs = unbound_table_values("改定後の申請の期限は30日以内です。", [revised])
    assert table_mismatch_notes(pairs, [revised]) == ["『申請の期限』の『改定後』は『60日以内』（『30日以内』は別の列の値）"]
    # 正しい対応が決まらないときは、別の行の値であることだけを書く。
    assert table_mismatch_notes([("99", "区分Ⅹ")], [TABLE]) == ["値『99』は『区分Ⅹ』と別の行の値"]


def test_grounded_item_listing_values_of_several_rows_is_kept() -> None:
    """複数の行の値を列挙した説明は、表の別の行の値として落とさない（#2734）。"""
    question = "単価の列に表示される値は？"
    spans = grounded.tag_spans(
        question, [{"evidence_id": "E1", "source_id": "c1", "source": "m.pdf", "page": 1, "text": PRICE_TABLE, "section_path": []}]
    )
    listed = GroundedItem(
        kind="rule", text="単価の列に 254・253・300 が表示されます。", citations=[GroundedCitation(evidence_id="E1", quote="254")]
    )
    entries, _ = grounded.verify(question, GroundedDraft(summary="", confidence="high", items=[listed]), spans)
    entry = next(entry for entry in entries if entry.item.text == listed.text)
    assert not any("表の別の行の値" in reason for reason in entry.reasons)


def test_audit_prompt_judges_the_relation_not_the_shared_words() -> None:
    assert RELATION_SUPPORT_RULES in grounded.AUDIT_SYSTEM_PROMPT
    section = grounded.AUDIT_SYSTEM_PROMPT.split("## Step 2:", 1)[1].split("## Step 3:", 1)[0]
    assert RELATION_SUPPORT_RULES in section  # support の判定の規則
    # 同じ対象の記載でも、未引用の補助資料を支持根拠へ転用しない。
    rules = [line for line in section.splitlines() if line.startswith("- support は実際の引用集合だけで判定する。")]
    assert len(rules) == 1
    assert "function_context・未引用の補助資料・見出し" in rules[0]
    assert "支持根拠として使わない" in rules[0]


# ---- 行と列の構造での照合（#2659。値の形に依らない） ----------------------------------------------------

WORD_TABLE = (
    "<table><tr><th>区分</th><th>処理の周期</th><th>保存期間</th></tr>"
    "<tr><td>区分A</td><td>月次処理</td><td>30日以内</td></tr>"
    "<tr><td>区分B</td><td>日次処理</td><td>60日以内</td></tr></table>"
)


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("区分Aは月次処理で、保存期間は30日以内です。", []),
        # 日本語の値・単位付きの値も、別の行の値なら検出する。
        ("区分Bは月次処理です。", [("月次処理", "区分B")]),
        ("区分Bの保存期間は30日以内です。", [("30日以内", "区分B")]),
        # 同じ列の値の列挙（区分の名前を述べない）は疑わない。
        ("処理の周期は月次処理・日次処理のどちらかです。", []),
        ("区分Aと区分Bは、それぞれ月次処理と日次処理です。", []),
    ],
    ids=["correct", "word-other-row", "unit-other-row", "word-list", "both-rows"],
)
def test_word_and_unit_values_are_checked_by_the_row(claim: str, expected: list[tuple[str, str]]) -> None:
    assert unbound_table_values(claim, [WORD_TABLE]) == expected


def test_code_inside_a_cell_with_a_unit_is_the_value_of_its_row() -> None:
    """「コード／数量 単位」のセルは、セル全体とセルの中のコードを、その行の値として照合する。"""
    table = (
        "<table><tr><td>項目</td><td>設定値</td><td>確認方法</td></tr>"
        "<tr><td>項目X1</td><td>K-01-1／140 分</td><td>記録1と画面を照合</td></tr>"
        "<tr><td>項目X2</td><td>K-01-2／141 分</td><td>記録2と画面を照合</td></tr>"
        "<tr><td>項目X3</td><td>K-01-3／142 分</td><td>記録3と画面を照合</td></tr></table>"
    )
    assert unbound_table_values("項目X2 の設定値は K-01-2／141 分です。", [table]) == []
    assert unbound_table_values("項目X2 の設定値は K-01-3／142 分です。", [table]) == [("K-01-3／142 分", "項目X2")]
    assert unbound_table_values("項目X2 は K-01-1 を設定します。", [table]) == [("k-01-1", "項目X2")]
    assert unbound_table_values("項目X3 は記録2と画面を照合します。", [table]) == [("記録2と画面を照合", "項目X3")]
    # 数字だけの並び（数量）はセルの中から取り出さない（ほかの意味の数と見分けられない）。
    assert unbound_table_values("項目X2 は 140 分ごとに確認します。", [table]) == []


def test_value_of_another_column_is_found_by_the_header() -> None:
    """行は合っていても、見出しが別の列（改定前と改定後）の値を述べた文を検出する。"""
    table = "| 項目 | 改定前 | 改定後 |\n|---|---|---|\n| 申請の期限 | 30日以内 | 60日以内 |\n| 保存期間 | 3年 | 5年 |"
    assert unbound_table_values("改定後の申請の期限は60日以内です。", [table]) == []
    assert unbound_table_values("改定前の申請の期限は30日以内です。", [table]) == []
    assert unbound_table_values("改定後の申請の期限は30日以内です。", [table]) == [("30日以内", "改定後")]
    # 見出しを述べない文・両方の列を述べる文は疑わない。
    assert unbound_table_values("申請の期限は30日以内です。", [table]) == []
    assert unbound_table_values("申請の期限は改定前の30日以内から改定後の60日以内に変わります。", [table]) == []


@pytest.mark.parametrize(
    ("evidence", "claim", "expected"),
    [
        # 1 文の中で並べた対応（表でない本文）。
        ("区分Aは値x01、区分Bは値y02とする。", "区分Bは値x01です。", [("値x01", "区分B")]),
        ("区分Aは値x01、区分Bは値y02とする。", "区分Bは値y02です。", []),
        # 別々の文の記載は並べた対応とみなさない。
        ("区分Aは値x01です。区分Bは値y02です。", "区分Bは値x01です。", []),
        # 箇条書きの「名前：値」・等号の区切り。
        ("- 区分A：x01\n- 区分B：y02", "区分Bは x01 です。", [("x01", "区分B")]),
        ("区分A = x01\n区分B = y02", "区分Bは x01 です。", [("x01", "区分B")]),
    ],
    ids=["prose-swapped", "prose-correct", "separate-sentences", "bullet-pairs", "equals-pairs"],
)
def test_enumerated_pairs_outside_tables(evidence: str, claim: str, expected: list[tuple[str, str]]) -> None:
    assert unbound_table_values(claim, [evidence]) == expected


def test_single_roman_numeral_rows_are_names_not_values() -> None:
    """ローマ数字だけの行の名前（Ⅰ・Ⅱ）は区分の名前として照合し、似た表記（l・1）とは区別する。"""
    table = "<table><tr><th>区分</th><th>期限</th></tr><tr><td>Ⅰ</td><td>30日</td></tr><tr><td>Ⅱ</td><td>60日</td></tr></table>"
    assert unbound_table_values("区分Ⅰは30日です。", [table]) == []
    assert unbound_table_values("区分Ⅰは60日です。", [table]) == [("60日", "Ⅰ")]
    assert unbound_table_values("区分Ⅱは30日です。", [table]) == [("30日", "Ⅱ")]
    assert unbound_table_values("区分IIは30日です。", [table]) == [("30日", "Ⅱ")]
    assert unbound_table_values("区分lは60日です。", [table]) == []


def test_short_words_and_note_rows_are_not_values() -> None:
    """他の語の一部に現れやすい短い語（あり・なし）と、値の列に別の種類の文しか無い注記の行では疑わない。"""
    flags = "<table><tr><th>区分</th><th>承認</th></tr><tr><td>区分A</td><td>あり</td></tr><tr><td>区分B</td><td>なし</td></tr></table>"
    assert unbound_table_values("区分Bの申請は承認がありません。", [flags]) == []
    notes = "区分A：x01\n区分B：y02\n備考：入力は必須"
    assert unbound_table_values("備考のとおり x01 を入力します。", [notes]) == []


@pytest.mark.parametrize("dash", ["-", "‐", "‑", "‒", "–", "−", "－"])
def test_identifier_with_unicode_dash_is_not_a_different_row_label(dash: str) -> None:
    table = f"| id | value |\n| row-a | 10 |\n| row{dash}b | 20 |\n| row-c | 30 |"
    reason = f"CSV に row{dash}b の value が 20 と記載されています。"
    assert unbound_table_values(reason, [table]) == []


def _figure_description() -> str:
    """合成の画面例の図の生成説明（Vision の欄の形）と図の中の文字（OCR）。保存の形のとおり空行を詰める。"""
    from rag_engine.parsing.picture_descriptions import vision_description_text
    from rag_engine.parsing.picture_text import format_docling_picture_ocr_text

    description = vision_description_text(
        {
            "retrieval_text": "画面 A では番号と状態が表示され、下部に保存・承認・差し戻し・一時保存のボタンがある。",
            "visual_kind": "screenshot",
            "menu_route": "左メニュー → 画面 A",
            "visible_screen_names": ["画面 A"],
            "visible_buttons": ["保存", "承認", "差し戻し", "一時保存"],
            "visible_fields": ["番号", "状態"],
        }
    )
    ocr = format_docling_picture_ocr_text("画面 A\n番号: X-01\n状態: 確認待ち\n保存\n差し戻し")
    return "\n".join(line for line in f"{description}\n{ocr}".split("\n") if line.strip())


def test_figure_description_fields_are_not_table_rows() -> None:
    """図の生成説明の欄（画面名・ボタン）は 1 つの図の属性で、表の行ではない（#3482）。"""
    figure = _figure_description()
    claim = "画面 A の下部に差し戻しボタンがあります。"
    assert unbound_table_values(claim, [figure]) == []
    # 本文の段落と同じ親の本文（要素の間は空行）でも、生成説明の欄だけを表にしない。
    parent = f"1.3 画面 A\n画面 A の下に操作のボタンが並びます。\n\n{figure}\n\n- 区分A：x01\n- 区分B：y02"
    assert unbound_table_values(claim, [parent]) == []
    # 生成説明の後の本文の「名前：値」の並びは、今までどおり照合する。
    assert unbound_table_values("区分Bは x01 です。", [parent]) == [("x01", "区分B")]


def test_real_tables_in_figure_descriptions_are_still_checked() -> None:
    """生成説明の抜粋の根拠でも、本当の表の構造（Markdown・HTML の表）の取り違えは検出する。"""
    figure = _figure_description() + "\n" + TABLE
    assert unbound_table_values("区分Ⅱの選択肢の値は 01 です。", [figure], line_pairs=False) == [("01", "区分Ⅱ")]
    # 見出しの無い抜粋（画像の生成説明の途中）の「名前：値」は、line_pairs=False では表にしない。
    excerpt = "\n".join(_figure_description().split("\n")[4:])
    assert evidence_tables(excerpt, line_pairs=False) == []


def test_grounded_item_about_a_figure_button_is_kept() -> None:
    """図の生成説明を根拠にした正しい説明を、表の対応の取り違えとして外さない（#3482）。"""
    question = "画面 A の差し戻しボタンはどこにありますか？"
    figure = _figure_description()
    spans = grounded.tag_spans(
        question, [{"evidence_id": "E1", "source_id": "c1", "source": "m.pdf", "page": 1, "text": figure, "section_path": []}]
    )
    assert "image_generated" in spans[0]["tags"]
    item = GroundedItem(
        kind="operation",
        text="画面 A の下部に差し戻しボタンがあります。",
        citations=[GroundedCitation(evidence_id="E1", quote="ボタン: 保存 / 承認 / 差し戻し / 一時保存")],
    )
    entries, _ = grounded.verify(question, GroundedDraft(summary="", confidence="high", items=[item]), spans)
    entry = next(entry for entry in entries if entry.item.text == item.text)
    assert not entry.table_conflict
    assert "contradicted" not in [mark["code"] for mark in entry.marks]


def test_index_column_is_not_the_label_column() -> None:
    """先頭が行の番号の列の表は、次の列を区分・名前の列として照合する（#3564）。"""
    table = (
        "| 項番 | 名称 | 数量 | 単価 |\n|---|---|---|---|\n"
        "| 1 | 部品 1 | 3 | 10 |\n| 2 | 部品 2 | 1 | 20 |\n| 3 | 部品 3 | 2 | 30 |\n| 4 | 部品 4 | 3 | 40 |"
    )
    # 名前の中の数字（「部品 1」の「1」）は値として読まず、名前の列を別の列の値と疑わない。
    for claim in (
        "部品 1 の単価は 10 です。",
        "部品 2 の数量は 1 です。",
        "部品 4 の数量は 3 です。",
        "部品 1 と部品 4 の数量はどちらも 3 です。",
    ):
        assert unbound_table_values(claim, [table]) == [], claim
    assert unbound_table_values("部品 3 の単価は 40 です。", [table]) == [("40", "部品 3")]
    assert evidence_tables(table)[0].header == ("名称", "数量", "単価")
    # 番号が上から増えない先頭の列は、今までどおり先頭の列として読む。
    unordered = "| 区分 | 値 | 備考 |\n|---|---|---|\n| 3 | 区分甲 | x1 |\n| 1 | 区分乙 | x2 |"
    assert len(evidence_tables(unordered)[0].header) == 3


def test_short_value_inside_a_stated_name_is_not_a_separate_value() -> None:
    """述べた名前の中にだけ現れる短い値は、別の行の値として疑わない（#3564）。"""
    table = "| 名称 | 数量 |\n|---|---|\n| 区分 1 | 3 |\n| 区分 2 | 1 |\n| 区分 3 | 2 |"
    assert unbound_table_values("区分 1 の数量は 3 です。", [table]) == []
    assert unbound_table_values("区分 3 の数量は 2 です。", [table]) == []
    # 名前の外で述べた値は、今までどおり照合する。
    assert unbound_table_values("区分 3 の数量は 1 です。", [table]) == [("1", "区分 3")]


# #123 の再現（synthetic-test-7）: 桁区切りのカンマで値と区間が切れ、正しい課長以上の宿泊費を「日当の列の値」と誤判定した。
LODGING_TABLE = (
    "第3条（出張旅費）\n<table><tbody><tr><td>区分</td><td>宿泊費の上限（1泊）</td><td>日当</td></tr>"
    "<tr><td>一般社員</td><td>12,000円</td><td>2,000円</td></tr><tr><td>課長以上</td><td>15,000円</td><td>3,000円</td></tr></tbody></table>"
)


def test_digit_group_commas_do_not_split_values_or_clauses() -> None:
    from rag_engine.generation.table_binding import unbound_table_values

    correct = (
        "規程には宿泊費の上限と日当が区分ごとに定められており、一般社員は1泊12,000円、日当2,000円です"
        "（参考：課長以上は1泊15,000円、日当3,000円）。"
    )
    assert unbound_table_values(correct, [LODGING_TABLE]) == []
    assert unbound_table_values("一般社員は1泊12,000円（日当2,000円）です。", [LODGING_TABLE]) == []
    # 取り違えは今までどおり見つける（別の行・別の列の値）。
    assert unbound_table_values("課長以上は1泊12,000円、日当3,000円です。", [LODGING_TABLE]) == [("12,000円", "課長以上")]
    assert unbound_table_values("一般社員の日当は12,000円です。", [LODGING_TABLE]) == [("12,000円", "日当")]
