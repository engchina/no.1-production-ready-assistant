"""資料用の任意依存がある環境で、内容正本と新しい図形配置を検証する。"""

from pathlib import Path

import pytest

pytest.importorskip("pptx", reason="PPTX 作成環境だけで必要な任意依存")
pytest.importorskip("lxml", reason="PPTX XML 検証の任意依存")

from scripts.update_analysis_presentation import NS, make_body, read_design

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "filename,count",
    [
        ("file-analysis-20260920.md", 18),
        ("file-analysis-chunking-20260921.md", 24),
        ("answer-management-20260921.md", 30),
        ("full-guide-polish-20260921.md", 57),
        ("full-guide-v17-20260921.md", 57),
    ],
)
def test_presentation_source_remains_parseable(filename, count):
    """旧正本の互換性と新正本の全対象ページを保護する。"""
    design = read_design(ROOT / "docs/presentations" / filename)
    assert len(design) == count
    for page in design.values():
        assert page["notes"] and page["evidence"]
        assert make_body(page)


def test_full_review_polished_shapes_and_notes():
    """全本文頁を図形で生成し、日付制約と混植指定の回帰を防ぐ。"""
    from scripts.presentation_polish import make_polished_body

    design = read_design(ROOT / "docs/presentations/full-guide-polish-20260921.md")
    assert set(design) == set(range(3, 60))
    for number, page in design.items():
        shapes = make_polished_body(page, number)
        assert shapes
        for shape in shapes:
            for rpr in shape.xpath(".//a:rPr", namespaces=NS):
                assert rpr.find("a:latin", NS).get("typeface") == "Oracle Sans Tab"
                assert rpr.find("a:ea", NS).get("typeface") == "Meiryo UI"
                assert rpr.get("lang") in {"ja-JP", "en-US"}
    for number in (5, 15, 48):
        assert "当日" in design[number]["subtitle"]
    assert "単純検索は検索文を追加しない" in design[16]["notes"]
    assert "この用語を検索・回答に使用する" in design[23]["notes"]
    assert "RerankのON/OFFと別" in design[51]["subtitle"]
    assert "一致候補がない場合は候補をそのまま返す" in design[51]["notes"]


def test_design_include_rejects_cycles_and_directory_escape(tmp_path):
    """再利用正本の誤参照で無限再帰や想定外ファイル読取りをしない。"""
    path = tmp_path / "source.md"
    path.write_text("Include: source.md\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Circular"):
        read_design(path)
    path.write_text("Include: ../outside.md\n", encoding="utf-8")
    with pytest.raises(ValueError, match="same directory"):
        read_design(path)


def test_design_amendment_requires_exact_source_match(tmp_path):
    """上流文言が変わった補正を黙って無視せず、正本の再確認を求める。"""
    source = (ROOT / "docs/presentations/full-guide-polish-20260921.md").read_text(encoding="utf-8")
    # Include先は一時ディレクトリに置き、実正本を変更しない。
    for name in ("file-analysis-chunking-20260921.md", "answer-management-20260921.md"):
        (tmp_path / name).write_text((ROOT / "docs/presentations" / name).read_text(encoding="utf-8"), encoding="utf-8")
    path = tmp_path / "review.md"
    path.write_text(source + "\n## Amend 14\n\nReplace notes: nonexistent-phrase => replacement\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no longer matches"):
        read_design(path)


@pytest.mark.parametrize("filename", ["full-guide-audit-20260921.md", "full-guide-v17-audit-20260921.md"])
def test_full_review_audit_has_every_page_and_real_source_paths(filename):
    """全60頁の独立核対表に欠落や存在しない根拠パスを残さない。"""
    import re

    content = (ROOT / "docs/presentations" / filename).read_text(encoding="utf-8")
    assert [int(n) for n in re.findall(r"^\| (\d+) \|", content, re.M)] == list(range(1, 61))
    for path in set(re.findall(r"(?:[a-z_]+/)+[a-z_]+\.(?:py|tsx)", content)):
        candidate = ROOT / path if path.startswith(("viewer/", "tests/")) else ROOT / "rag_engine" / path
        assert candidate.is_file(), path


def test_v17_refined_layout_and_prompt_boundaries():
    """新版の図形・字体を全頁検査し、Prompt自動置換や精度保証の誤記を防ぐ。"""
    import re

    from scripts.presentation_polish import make_polished_body

    design = read_design(ROOT / "docs/presentations/full-guide-v17-20260921.md")
    assert set(design) == set(range(3, 60))
    for number, page in design.items():
        shapes = make_polished_body(page, number, refined=True)
        assert shapes
        assert not re.search(r"\.(py|tsx):\d+", page["evidence"])
        assert "full-guide-v17-audit-20260921.md" in page["evidence"]
        for shape in shapes:
            for rpr in shape.xpath(".//a:rPr", namespaces=NS):
                assert rpr.find("a:latin", NS).get("typeface") == "Oracle Sans Tab"
                assert rpr.find("a:ea", NS).get("typeface") == "Meiryo UI"
            assert not shape.xpath(".//a:normAutofit", namespaces=NS)
    assert "本文全体が最新の短縮版に置き換わるわけではない" in design[39]["notes"]
    assert "4軸各5点" in design[58]["notes"]
    assert "監査用system prompt" in design[57]["notes"]
    assert "実データが必要かどうかの暫定判定" in design[16]["notes"]


@pytest.mark.parametrize("index", ["missing.md", "../outside.md"])
def test_reference_index_requires_local_existing_file(tmp_path, index):
    """参照位置を集約する核対表が未作成・範囲外なら生成を止める。"""
    source = tmp_path / "source.md"
    source.write_text(f"Reference index: {index}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Reference index"):
        read_design(source)


def test_six_row_settings_table_has_room_without_font_reduction():
    """長い設定ラベルを 14pt のまま表示できる行高を確保する。"""
    table_shape = make_body({"layout": "table", "items": [["項目", "値", "説明"]] * 6})[0]
    extent = table_shape.find("p:xfrm/a:ext", NS)
    assert int(extent.get("cy")) >= int(4.19 * 914400)
    assert set(table_shape.xpath(".//a:rPr/@sz", namespaces=NS)) == {"1400"}


def test_hierarchy_arrows_point_inside_parent():
    """端の子の矢印が親の枠外を指す視覚回帰を防ぐ。"""
    shapes = make_body(
        {
            "layout": "hierarchy",
            "items": [
                ["親", "文脈"],
                ["本文", "手順"],
                ["表", "行"],
                ["図", "説明"],
            ],
        }
    )
    parent = next(shape for shape in shapes if shape.find(".//p:cNvPr", NS).get("name") == "parent-context")
    parent_rect = parent.find("p:sp/p:spPr/a:xfrm", NS)
    start = int(parent_rect.find("a:off", NS).get("x"))
    end = start + int(parent_rect.find("a:ext", NS).get("cx"))
    arrows = [shape for shape in shapes if shape.find(".//p:cNvPr", NS).get("name", "").startswith("child-to-parent-")]
    assert len(arrows) == 3
    for arrow in arrows:
        transform = arrow.find("p:spPr/a:xfrm", NS)
        center = int(transform.find("a:off", NS).get("x")) + int(transform.find("a:ext", NS).get("cx")) / 2
        assert start < center < end
