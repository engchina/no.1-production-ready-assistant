"""table structure の挙動を保護するテスト。"""

import unittest

from rag_engine.parsing.table_structure import parse_html_table_structure, table_row_group_text, table_row_groups


class TableStructureTests(unittest.TestCase):
    def test_parses_caption_headers_and_spans(self):
        structure = parse_html_table_structure(
            "<table>"
            "<caption>取引先情報</caption>"
            '<tr><th colspan="2">見出し</th><th>説明</th></tr>'
            '<tr><th rowspan="2">種別</th><td>基本</td><td>21件</td></tr>'
            "<tr><td>出荷</td><td>25件</td></tr>"
            "</table>"
        )

        self.assertEqual(structure["caption"], "取引先情報")
        self.assertEqual(structure["row_count"], 3)
        self.assertEqual(structure["column_count"], 3)
        self.assertEqual(structure["header_row_count"], 1)
        self.assertEqual(
            [(item["column_index"], item["text"]) for item in structure["column_headers"]],
            [(1, "見出し"), (2, "見出し"), (3, "説明")],
        )
        self.assertEqual(structure["rows"][0]["cells"][0]["colspan"], 2)
        self.assertEqual(structure["rows"][1]["cells"][0]["rowspan"], 2)
        self.assertEqual(structure["rows"][2]["cells"][0]["column_index"], 2)

    def test_nested_table_is_flattened_into_the_outer_cell(self):
        structure = parse_html_table_structure(
            "<table><tr><td>外側A</td><td><table><tr><td>内1</td><td>内2</td></tr>"
            "<tr><td>内3</td></tr></table></td><td>外側B</td></tr></table>"
        )

        self.assertEqual(structure["row_count"], 1)
        self.assertEqual(
            [cell["text"] for cell in structure["rows"][0]["cells"]],
            ["外側A", "内1 内2\n内3", "外側B"],
        )

    def test_omitted_end_tags_still_yield_rows_and_cells(self):
        # HTML5 で合法な </td> </tr> の省略（Vision 由来の HTML で起きる）でも行を落とさない (#788)。
        structure = parse_html_table_structure("<table><tr><th>項目<th>値<tr><td>a<td>b<tr><td>c<td>d</table>")

        self.assertEqual(structure["row_count"], 3)
        self.assertEqual([[cell["text"] for cell in row["cells"]] for row in structure["rows"]], [["項目", "値"], ["a", "b"], ["c", "d"]])

    def test_extreme_spans_are_capped(self):
        # Vision 由来の HTML の span をそのまま展開すると、処理量が入力値に比例する。
        structure = parse_html_table_structure('<table><tr><td colspan="100000000" rowspan="100000000">x</td></tr></table>')

        cell = structure["rows"][0]["cells"][0]
        self.assertEqual((cell["rowspan"], cell["colspan"]), (50, 50))

    def test_row_groups_repeat_table_caption_and_headers(self):
        structure = parse_html_table_structure(
            "<table><caption>処理確認</caption>"
            "<tr><th>画面</th><th>説明</th></tr>"
            f"<tr><td>基本情報</td><td>{'OK を押します。' * 8}</td></tr>"
            f"<tr><td>出荷情報</td><td>{'取消します。' * 8}</td></tr>"
            "</table>"
        )

        groups = table_row_groups(structure, target_chars=120)

        self.assertEqual(len(groups), 2)
        self.assertIn("表タイトル: 処理確認", groups[0]["text"])
        self.assertIn("列見出し: 画面 / 説明", groups[0]["text"])
        self.assertIn("行 2: 画面: 基本情報 / 説明: OK を押します。", groups[0]["text"])
        self.assertEqual(groups[0]["structure"]["row_count"], 3)
        self.assertEqual(groups[0]["row_start"], 2)

    def test_row_text_keeps_the_range_of_merged_cells_and_inherited_row_headers(self):
        """期間全体の値を初日だけの値にしない。内訳行が何の内訳かを失わない。"""
        html = (
            "<table><tr><td></td><td></td><th>4月27日</th><th>4月28日</th><th>4月29日</th><th>4月30日</th></tr>"
            '<tr><th colspan="2">当行ATM</th><td colspan="4">24時間</td></tr>'
            '<tr><th rowspan="2">提携銀行のATM</th><th>入金</th><td colspan="4">9時～17時</td></tr>'
            '<tr><th>出金</th><td>0時05分～23時55分</td><td>0時05分～20時</td><td colspan="2">6時30分～20時</td></tr></table>'
        )
        structure = parse_html_table_structure(html)
        text = table_row_group_text(structure, structure["rows"][1:])
        self.assertIn("行 2: 列1: 当行ATM / 4月27日～4月30日: 24時間", text)
        self.assertIn("行 3: 列1: 提携銀行のATM / 列2: 入金 / 4月27日～4月30日: 9時～17時", text)
        self.assertIn(
            "行 4: 列1: 提携銀行のATM / 列2: 出金 / 4月27日: 0時05分～23時55分 / 4月28日: 0時05分～20時 / 4月29日～4月30日: 6時30分～20時",
            text,
        )


if __name__ == "__main__":
    unittest.main()


def test_rowspan_value_cells_are_inherited_by_following_rows():
    # 「入金」の内訳行が何の内訳か分からなくならないよう、th でない結合セルも引き継ぐ (#594)。
    from rag_engine.parsing.table_structure import parse_html_table_structure, table_row_groups

    html = (
        "<table><tr><th>区分</th><th>項目</th><th>金額</th></tr>"
        "<tr><td rowspan='2'>入金</td><td>振込</td><td>100</td></tr><tr><td>現金</td><td>200</td></tr>"
        "<tr><th rowspan='2'>出金</th><td>振込</td><td>300</td></tr><tr><td>現金</td><td>400</td></tr></table>"
    )
    structure = parse_html_table_structure(html)
    text = "\n".join(group["text"] for group in table_row_groups(structure, target_chars=10000))
    assert "行 3: 区分: 入金 / 項目: 現金 / 金額: 200" in text
    assert "行 5: 区分: 出金 / 項目: 現金 / 金額: 400" in text


def _own(*texts: str) -> list[tuple[str, bool]]:
    return [(text, True) for text in texts]


def test_misplaced_cell_text_is_moved_back_from_the_neighbor_row():
    # グリフの bbox が下へずれ、26 行目の「Input」が 25 行目のセルへ入った表を text layer の行で戻す (#597)。
    from rag_engine.parsing.table_structure import misplaced_cell_repairs

    rows = [_own("項番", "連携データ", "連携ID", "連携の向き", "連携手法"), _own("24", "取引先情報", "LNK-002", "Input", "")]
    rows[1][4] = ("ファイル連携（※2）", False)  # 上の行から続く rowspan セル
    rows += [
        _own("25", "倉庫入出庫情報", "LNK-006", "Input Input") + [("ファイル連携（※2）", False)],
        _own("26", "取引先与信情報", "LNK-007", "") + [("ファイル連携（※2）", False)],
        _own("27", "取引先契約・割引情報", "LNK-008", "Input") + [("ファイル連携（※2）", False)],
    ]
    lines = [
        "項番 連携データ 連携ID 連携の向き 連携手法",
        "24 取引先情報 LNK-002 Input",
        "25 倉庫入出庫情報 LNK-006 Input",
        "26 取引先与信情報 LNK-007 Input",
        "27 取引先契約・割引情報 LNK-008 Input",
        "ファイル連携（※2）",
    ]
    assert misplaced_cell_repairs(rows, lines) == [(2, 3, "Input"), (3, 3, "Input")]


def test_misplaced_cell_repair_handles_different_values_and_refuses_invented_text():
    from rag_engine.parsing.table_structure import misplaced_cell_repairs

    # 値が異なる場合も、空セルの候補は隣のセルにある文字に限る。
    rows = [_own("1", "A", "Output"), _own("2", "B", "Output"), _own("3", "C", "Input Output"), _own("4", "D", "")]
    lines = ["1 A Output", "2 B Output", "3 C Input", "4 D Output"]
    assert misplaced_cell_repairs(rows, lines) == [(2, 2, "Input"), (3, 2, "Output")]
    # text layer にはあるが隣のセルにない文字は発明しない（unassigned の補足に任せる）。
    lines = ["1 A Output", "2 B Output", "3 C Input Output", "4 D Bidirectional"]
    assert misplaced_cell_repairs(rows, lines) == []
    # 行の対応が一意に決まらない表、他のセルが空の行、正当に空の列は直さない。
    assert misplaced_cell_repairs([_own("x", "Input Input"), _own("x", "")], ["x Input", "x Input", "x"]) == []
    assert misplaced_cell_repairs([_own("", "Input Input"), _own("", "")], ["Input", "Input"]) == []
    assert misplaced_cell_repairs([_own("1", ""), _own("2", ""), _own("3", "v"), _own("4", "v")], ["1", "2", "3 v", "4 v"]) == []


def test_row_groups_match_the_full_text_reference_on_a_large_table():
    """長さの累積で決める group 境界が、全文を作って測る従来の判定と一致する (#805)。"""
    from rag_engine.parsing.table_structure import table_row_group_text, table_row_groups

    rows = ["<tr><th>区分</th><th>種別</th><th>金額</th><th>備考</th></tr>"]
    for i in range(300):
        if i % 7 == 0:
            rows.append(f"<tr><th rowspan='3'>区分{i}</th><td>種別{i}</td><td>{i * 100}</td><td colspan='1'>備考 {i}</td></tr>")
        elif i % 11 == 0:
            rows.append(f"<tr><td>種別{i}</td><td colspan='2'>{i * 100} 円（全期間）</td></tr>")
        else:
            rows.append(f"<tr><td>種別{i}</td><td>{i * 100}</td><td></td></tr>")
    structure = parse_html_table_structure("<table><caption>料金表</caption>" + "".join(rows) + "</table>")

    def reference(target):
        data_rows = structure["rows"][structure["header_row_count"] :]
        groups, current = [], []
        for row in data_rows:
            candidate = [*current, row]
            if current and len(table_row_group_text(structure, candidate, heading_text="料金 > 一覧")) > target:
                groups.append(current)
                current = [row]
            else:
                current = candidate
        return [(g[0]["row_index"], g[-1]["row_index"]) for g in groups + ([current] if current else [])]

    for target in (120, 300, 900):
        got = [(g["row_start"], g["row_end"]) for g in table_row_groups(structure, target_chars=target, heading_text="料金 > 一覧")]
        assert got == reference(target), target
