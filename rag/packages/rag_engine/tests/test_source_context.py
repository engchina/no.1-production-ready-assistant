"""完全な出典を実際の最終プロンプトで照合する。"""

import json
from dataclasses import replace

import pytest

from rag_engine.generation import grounded
from rag_engine.generation.answering import AnswerContext, AnswerRecord, _grounded_prompt, _grounded_spans
from rag_engine.retrieval.evidence_selection import evidence_spans


def make_record(index, path, *, enabled=None, source="document-a.txt"):
    metadata = {"section_path": path}
    if enabled is not None:
        metadata["context_header_enabled"] = enabled
        if enabled:
            metadata["context_header"] = " > ".join([source, *path])
    return AnswerRecord(
        id=f"r{index}",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=index,
        category="Text",
        text=f"項目 {index} の説明本文です。",
        source=source,
        metadata=metadata,
    )


@pytest.mark.parametrize("enabled", [True, False, None])
def test_final_prompt_keeps_each_complete_path_separate_from_quote(enabled):
    records = [
        make_record(1, ["分類 A", "機能 A", "説明"], enabled=enabled),
        make_record(2, ["分類 B", "機能 A", "説明"], enabled=enabled),
        make_record(3, ["分類 C", "機能 A", "説明"], source="document-b.txt", enabled=enabled),
    ]
    ctx = AnswerContext(records=records, text="\n".join(r.text for r in records))
    spans = _grounded_spans("各項目の説明", ctx, (), None)
    # 最終回答の組立関数を通し、グループ名だけでなく各 Evidence 内を検査する。
    prompt = _grounded_prompt("各項目の説明", spans, ctx, (), None, None, None, None, None)
    for record in records:
        path_json = json.dumps(record.metadata["section_path"], ensure_ascii=False)
        assert path_json in prompt
        assert record.source in prompt
        span = next(s for s in spans if s["text"] == record.text)
        assert "出典情報" not in span["text"]
        note = grounded._source_note(span)
        assert path_json in note and grounded.CONTEXT_NOTE_BEGIN in note
        assert record.text not in note
    assert (
        prompt.index("\nBEGIN_UNTRUSTED_RETRIEVED_CONTEXT\n")
        < prompt.index("出典情報:")
        < prompt.rindex("\nEND_UNTRUSTED_RETRIEVED_CONTEXT")
    )


def test_unknown_path_is_not_invented_and_long_path_is_not_truncated():
    missing = replace(make_record(1, []), metadata={})
    known = make_record(2, ["階層" + str(i) for i in range(80)])
    spans = evidence_spans("説明", [known, missing], budget=3000)
    assert spans[0]["source_context"]["section_path"] == known.metadata["section_path"]
    assert "section_path" not in spans[1]["source_context"]
    # 完全なヘッダを収められない予算では、パスの途中を削って渡さない。
    assert evidence_spans("説明", [known], budget=100) == []


def test_parent_excerpt_keeps_only_corresponding_child_source_context():
    child_a = replace(make_record(1, ["分類 A", "説明"], enabled=True), parent_chunk_id="parent")
    child_b = replace(make_record(2, ["分類 B", "説明"], enabled=False), parent_chunk_id="parent")
    parent = replace(
        make_record(0, ["共通分類"]), id="parent", chunk_id="parent", chunk_level="parent", text=child_a.text + "\n" + child_b.text
    )
    spans = evidence_spans("説明", [parent], scope_records=[child_a, child_b], budget=3000)
    for child in (child_a, child_b):
        span = next(s for s in spans if child.text in s["text"])
        children = span["source_context"]["children"]
        info = next(c for c in children if c["source_id"] == child.id)
        assert info["section_path"] == child.metadata["section_path"]
        assert info.get("search_context_header") == child.metadata.get("context_header")
        other = child_b if child is child_a else child_a
        assert all(c["source_id"] != other.id for c in children)


def test_prompt_note_omits_repeated_values_but_keeps_each_full_path():
    """モデルへ示す出典情報は、機能の見出しと同じ文書名・索引用のヘッダ・子の uid を繰り返さない（#3831）。"""
    child_a = replace(make_record(1, ["分類 A", "説明"], enabled=True), parent_chunk_id="parent")
    child_b = replace(make_record(2, ["共通分類"], enabled=True), parent_chunk_id="parent")
    parent = replace(
        make_record(0, ["共通分類"], enabled=True),
        id="parent",
        chunk_id="parent",
        chunk_level="parent",
        text=child_a.text + "\n" + child_b.text,
    )
    spans = evidence_spans("説明", [parent], scope_records=[child_a, child_b], budget=3000)
    for span in spans:
        stored = span["source_context"]
        # 保存・照合用の完全な出典は変えない。
        assert stored["search_context_header"] == "document-a.txt > 共通分類"
        note = grounded._source_note(span)
        assert grounded.CONTEXT_NOTE_BEGIN in note and span["text"] not in note
        shown = json.loads(note.split("出典情報: ", 1)[1].split("\n", 1)[0])
        assert shown["section_path"] == ["共通分類"]
        assert set(shown) <= {"section_path", "children"}
        for child in stored.get("children", []):
            assert child["source_id"] not in note
        # 子はブロックと違う章節だけを示す。
        assert shown.get("children", []) == [
            {"section_path": c["section_path"]} for c in stored.get("children", []) if c["section_path"] != ["共通分類"]
        ]
    assert any(span.get("source_context", {}).get("children") for span in spans)


def test_prompt_note_keeps_values_that_differ_from_the_heading():
    from rag_engine.retrieval.source_context import prompt_source_context

    context = {
        "document_name": "document-a.txt",
        "section_path": ["分類 A"],
        "search_context_header": "保存された別のヘッダ",
        "search_context_header_enabled": True,
        "children": [{"source_id": "c1", "document_name": "document-b.txt", "section_path": ["分類 A"]}],
    }
    assert prompt_source_context(context, document_name="document-a.txt") == {
        "section_path": ["分類 A"],
        "search_context_header": "保存された別のヘッダ",
        "children": [{"document_name": "document-b.txt"}],
    }
    # 機能の見出しの文書名と違えば、文書名も示す。
    assert prompt_source_context(context, document_name="other.txt")["document_name"] == "document-a.txt"


def test_derivable_search_header_does_not_use_the_body_budget():
    """索引用のヘッダは描かないので、ON / OFF で根拠に入る本文が変わらない（予算は描いた補助情報を数える。#3831）。"""
    records = {
        enabled: [replace(make_record(i, ["分類" + str(i), "機能", "説明"], enabled=enabled), text="本文" * 40) for i in range(6)]
        for enabled in (True, False)
    }
    chosen = {enabled: evidence_spans("説明", rows, budget=700) for enabled, rows in records.items()}
    assert [s["text"] for s in chosen[True]] == [s["text"] for s in chosen[False]]
    assert chosen[True]


def test_budget_trace_separates_body_metadata_and_format():
    spans = _grounded_spans(
        "各項目の説明",
        AnswerContext(records=[make_record(1, ["分類 A", "説明"], enabled=True)], text=""),
        (),
        None,
    )
    from rag_engine.config import get_settings
    from rag_engine.generation.answering import _evidence_budget_trace

    trace = _evidence_budget_trace(get_settings(environ={}, dotenv_path=None), None, spans)
    assert trace["budget_counts"] == "body_and_metadata"
    assert trace["evidence_metadata_chars"] > 0 and trace["evidence_format_chars"] > 0
    assert trace["evidence_text_chars"] + trace["evidence_metadata_chars"] + trace["evidence_format_chars"] == trace["evidence_block_chars"]


def test_html_roles_and_table_captions_reach_prompt_without_changing_citation_body():
    from rag_engine.retrieval.source_context import source_context

    footer = replace(make_record(1, ["最後の節"]), text="原文の末尾です。", metadata={"html_region_role": "footer"})
    table = replace(make_record(2, ["上位の節"]), metadata={"table_caption": "原文の表題"})
    assert source_context(footer)["html_region"] == {"role": "footer", "text": footer.text}
    assert source_context(table)["table_caption"] == "原文の表題"
    spans = evidence_spans("末尾と表題", [footer, table], budget=3000)
    for index, span in enumerate(spans, 1):
        span.update(label=f"E{index}", function=("synthetic.html", "節"), tags=[])
    assert "footer" in grounded.evidence_block_text(spans)
    assert "原文の表題" in grounded.evidence_block_text(spans)
    assert {span["text"] for span in spans} == {footer.text, table.text}
