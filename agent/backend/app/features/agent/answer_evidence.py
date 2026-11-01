"""回答の根拠（成果物 kind=`answer_evidence`・「回答の根拠」。#2328）。

チャットの回答の下の「AI に渡した N 件 ／ 回答で引用 M 件」（RAG のチャットと同じ
共有の部品）と、外部の MCP クライアントへの `sources` の正本。組み込み Runtime は
回答を確定するときに 1 回作る。

- AI に渡した根拠: その Run でモデルに渡したツールの結果のうち、根拠を返すツール
  （`rag_search` / `rag_retrieve_evidence`）の `evidence`。同じ chunk
  （`document_id`・`chunk_id`）は 1 回だけ、出てきた順。
- 回答で引用した根拠: 利用者に見せる回答（最終の検証の後）が書いた根拠の ID
  （省いて書いた形を含む。最終の検証が根拠を並べるときと同じ照合
  `answer_validation.cited_chunk_ids`）と、最終の検証が回答の段落を裏付けた
  （`supported`）根拠。
- 回答での使われ方（`answer_use`。該当確認済み / 参考）は、回答で引用した根拠に、
  RAG が同じ根拠に付けた値があれば残す。
- 会話の添付の根拠（#2554）: 添付を読むツール（`chat_attachment_read_section` /
  `chat_attachment_read_rows` / `chat_attachment_find`）が返した節・行・一致した箇所。
  `document_id` は `attachment:<添付の ID>`（RAG の文書ではないので、根拠の原文・図を
  開く確かめ（`passed_evidence`）には入れない）。添付は最終の検証の対象外で根拠の ID の
  印も無いため、回答で引用は回答がその添付のファイル名を書いたときに付ける。

- 結び付けの方法（`binding`。#2707）: 最終の検証が判定を出した（`completed`）ときは、その
  判定（`supported`）と根拠の ID の印だけで結び付ける（`validation`）。検証が時間切れ・失敗・
  無効のときは、モデルを呼ばない決定的な照合（`text`）で、回答の段落と AI に渡した根拠を結び
  付ける: 段落の特徴（引用・ID・コード・見出し・同じ文字の並び・章節の参照。最終の検証に渡す
  根拠の並べ替えと同じ `evidence_match`）と、文書名と頁を書いた行（出典の行・段落の中の
  「文書 A.pdf p.3」）。照合で確かめていない結び付けなので、回答での使われ方は参考
  （`reference`）にし、画面は RAG の「参考情報」（根拠の原文と、該当は確認できていないことの
  説明）として出す（`references`。「資料により確認できた事項」にしない）。照合が時間の上限までに
  どの段落も確かめ終わらなかった（`timed_out`。成果物は `unvalidated`）ときも `text`。最終の検証が
  根拠で確かめていない段落に印を付けた（未確認・規則の検査の警告。`passage_marks`。#2700・#2738）
  ときは `partial`: 確かめた段落は `validation` と同じに結び付け、印の段落は本文の根拠の行にせず、
  決定的な照合で結び付けた根拠を「参考情報」にする（確かめた段落の根拠の行にある根拠は重ねない）。

内容は画面と MCP が読む形（RAG の MCP の根拠の項目の一部と `cited`）。本文の抜粋は
RAG が返した長さのまま（添付は `ATTACHMENT_EXCERPT_CHARACTERS` まで）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pr_system_settings.chat_attachments.tools import (
    FIND_TOOL_NAME,
    LIST_TOOL_NAME,
    READ_ROWS_TOOL_NAME,
    READ_SECTION_TOOL_NAME,
    VIEW_IMAGE_TOOL_NAME,
)

from app.features.agent.answer_passages import (
    KIND_CITATION,
    answer_passage_positions,
    non_claim_passages,
    passage_spans,
)
from app.features.agent.answer_validation import (
    FIGURE_IMAGE_READ,
    STATUS_COMPLETED,
    cited_chunk_ids,
    locator_labels,
)
from app.features.agent.evidence_match import (
    EvidenceText,
    answer_features,
    matched_features,
    order_weights,
)
from app.features.agent.support_task import RAG_RETRIEVE_EVIDENCE, RAG_SEARCH
from app.features.agent.tools import mcp_base_tool_name

if TYPE_CHECKING:
    from app.features.agent.runtime import RunStep

JsonObject = dict[str, Any]

ANSWER_EVIDENCE_KIND = "answer_evidence"
ANSWER_EVIDENCE_NAME = "回答の根拠"
# 根拠を返すツール（AI に渡した根拠として数える）。本文を読むツールは根拠の一覧を
# 返さないので数えない。
EVIDENCE_LIST_TOOLS = frozenset({RAG_SEARCH, RAG_RETRIEVE_EVIDENCE})
# 1 つの Run の根拠の上限（画面の一覧と成果物の大きさ）。
MAX_ANSWER_EVIDENCE = 200
# 会話の添付の根拠（#2554）。
ATTACHMENT_DOCUMENT_PREFIX = "attachment:"
ATTACHMENT_EVIDENCE_TYPE = "chat_attachment"
# 添付の根拠の抜粋の上限（節は 1 回に 8000 文字まで読めるので、画面と成果物では縮める）。
ATTACHMENT_EXCERPT_CHARACTERS = 2_000
_ANSWER_USES = frozenset({"confirmed", "reference"})
# 結び付けの方法（#2707）。
BINDING_VALIDATION = "validation"
BINDING_TEXT = "text"
# 最終の検証が根拠で確かめていない段落に印を付けた（#2700・#2738）。確かめた段落は `validation`、
# 印の段落は `text` と同じに結び付ける。
BINDING_PARTIAL = "partial"
# 決定的な照合で 1 つの段落に結び付ける根拠の上限（当たりの重みの大きい順）。
MAX_TEXT_BOUND_PER_PASSAGE = 3
# 最終の検証に渡す段落ごとの出典の上限（`rag_validate_answer` の契約の `passage_citations` の
# maxItems と、1 つの段落の chunk_ids の maxItems。#104）。
MAX_VALIDATION_PASSAGE_CITATIONS = 300
MAX_VALIDATION_CITATION_CHUNKS = 30
# 「参考情報」に出す根拠の原文の上限の文字数。
REFERENCE_QUOTE_CHARACTERS = 200
# RAG の根拠の項目のうち、そのまま写すもの。
_COPIED_FIELDS = (
    "document_id",
    "chunk_id",
    "file_name",
    "excerpt",
    "truncated",
    # 本文を省いた・切った範囲（#3321。公開の MCP の sources が chunk の中の位置を返す）。
    "text_omitted",
    "text_length",
    "excerpt_range",
    "header_range",
    "next_offset",
    "locator",
    "role",
    "content_kind",
    "evidence_type",
    "image_ref",
    "score",
    "rerank_score",
    "superseded",
    "chat_attachment_id",
)


def passed_evidence(steps: Sequence[RunStep]) -> list[JsonObject]:
    """その Run でモデルに渡した根拠（chunk で重複を除き、出てきた順）。

    画面の根拠の一覧（`items`）と、画面から根拠の原文・図を開くときの確かめ（`rag_figures`）が
    同じこの関数を使う（2 つの集合をずらさない。#2427）。

    同じ chunk を後の呼び出しが返し、RAG がそこで回答での使われ方（`answer_use`）を付けていれば、
    その値を足す。
    """
    items: dict[tuple[str, str], JsonObject] = {}
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success or step.status != "completed":
            continue
        if mcp_base_tool_name(call.name) not in EVIDENCE_LIST_TOOLS:
            continue
        output = result.output
        evidence = output.get("evidence") if isinstance(output, Mapping) else None
        for item in evidence if isinstance(evidence, list) else []:
            if not isinstance(item, Mapping):
                continue
            document_id, chunk_id = item.get("document_id"), item.get("chunk_id")
            if not isinstance(document_id, str) or not isinstance(chunk_id, str):
                continue
            if not document_id or not chunk_id:
                continue
            known = items.get((document_id, chunk_id))
            if known is not None:
                if known.get("answer_use") not in _ANSWER_USES:
                    known["answer_use"] = item.get("answer_use")
                continue
            if len(items) < MAX_ANSWER_EVIDENCE:
                items[(document_id, chunk_id)] = dict(item)
    return list(items.values())


def _completed_outputs(steps: Sequence[RunStep], names: frozenset[str]) -> list[tuple[str, Any]]:
    outputs: list[tuple[str, Any]] = []
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success or step.status != "completed":
            continue
        if call.name in names:
            outputs.append((call.name, result.output))
    return outputs


def _excerpt(text: str) -> tuple[str, bool]:
    if len(text) <= ATTACHMENT_EXCERPT_CHARACTERS:
        return text, False
    return text[:ATTACHMENT_EXCERPT_CHARACTERS], True


def _row_line(values: Any) -> str:
    if not isinstance(values, Mapping):
        return ""
    return " / ".join(f"{column}: {value}" for column, value in values.items() if value is not None)


def _attachment_item(
    attachment_id: str,
    file_name: str,
    chunk_id: str,
    text: str,
    *,
    locator: JsonObject,
    section_title: str | None = None,
    content_kind: str,
) -> JsonObject:
    excerpt, truncated = _excerpt(text)
    item: JsonObject = {
        "document_id": f"{ATTACHMENT_DOCUMENT_PREFIX}{attachment_id}",
        "chunk_id": chunk_id,
        "file_name": file_name,
        "excerpt": excerpt,
        "truncated": truncated,
        "locator": locator,
        "content_kind": content_kind,
        "evidence_type": ATTACHMENT_EVIDENCE_TYPE,
        "chat_attachment_id": attachment_id,
    }
    # PDF の節の見出しは「p.N」（頁と同じ）なので、頁があれば見出しに重ねない。
    if section_title and section_title != f"p.{locator.get('page_start')}":
        item["section"] = {"title": section_title}
    return item


def _section_locator(page: Any) -> JsonObject:
    if isinstance(page, int) and not isinstance(page, bool) and page > 0:
        return {"page_start": page, "page_end": page}
    return {}


def _rows_locator(sheet: str, start: int, end: int) -> JsonObject:
    return {"sheet_name": sheet, "row_start": start, "row_end": end}


def attachment_evidence(steps: Sequence[RunStep]) -> list[JsonObject]:
    """その Run で添付を読むツールが返した根拠（#2554。節・行の範囲で重複なし、出てきた順）。

    `chat_attachment_find` の結果は頁・見出しを返さないので、同じ Run の
    `chat_attachment_list` の節から引く。
    """
    sections: dict[tuple[str, str], tuple[str | None, Any]] = {}
    for _name, output in _completed_outputs(steps, frozenset({LIST_TOOL_NAME})):
        attachments = output.get("attachments") if isinstance(output, Mapping) else None
        for attachment in attachments if isinstance(attachments, list) else []:
            if not isinstance(attachment, Mapping):
                continue
            for section in attachment.get("sections") or []:
                if isinstance(section, Mapping):
                    sections[
                        (str(attachment.get("attachment_id")), str(section.get("section_id")))
                    ] = (
                        section.get("title") if isinstance(section.get("title"), str) else None,
                        section.get("page"),
                    )
    items: dict[str, JsonObject] = {}

    def add(item: JsonObject) -> None:
        if item["chunk_id"] not in items and len(items) < MAX_ANSWER_EVIDENCE:
            items[item["chunk_id"]] = item

    read_tools = frozenset(
        {READ_SECTION_TOOL_NAME, READ_ROWS_TOOL_NAME, FIND_TOOL_NAME, VIEW_IMAGE_TOOL_NAME}
    )
    for name, output in _completed_outputs(steps, read_tools):
        if not isinstance(output, Mapping):
            continue
        if name == VIEW_IMAGE_TOOL_NAME:
            image = _image_evidence(output)
            if image is not None:
                add(image)
        elif name == READ_SECTION_TOOL_NAME:
            attachment_id, section_id = output.get("attachment_id"), output.get("section_id")
            content = output.get("content")
            if not isinstance(attachment_id, str) or not isinstance(section_id, str):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            add(
                _attachment_item(
                    attachment_id,
                    str(output.get("file_name") or attachment_id),
                    f"{ATTACHMENT_DOCUMENT_PREFIX}{attachment_id}:{section_id}",
                    content,
                    locator=_section_locator(output.get("page")),
                    section_title=output.get("title")
                    if isinstance(output.get("title"), str)
                    else None,
                    content_kind="text",
                )
            )
        elif name == READ_ROWS_TOOL_NAME:
            attachment_id, sheet = output.get("attachment_id"), output.get("sheet")
            rows = [row for row in output.get("rows") or [] if isinstance(row, Mapping)]
            numbers: list[int] = [
                number for row in rows if isinstance(number := row.get("row_number"), int)
            ]
            if not isinstance(attachment_id, str) or not isinstance(sheet, str) or not numbers:
                continue
            start, end = min(numbers), max(numbers)
            add(
                _attachment_item(
                    attachment_id,
                    str(output.get("file_name") or attachment_id),
                    f"{ATTACHMENT_DOCUMENT_PREFIX}{attachment_id}:{sheet}:{start}-{end}",
                    "\n".join(_row_line(row.get("values")) for row in rows),
                    locator=_rows_locator(sheet, start, end),
                    content_kind="sheet",
                )
            )
        else:
            for match in output.get("matches") or []:
                if not isinstance(match, Mapping):
                    continue
                attachment_id, snippet = match.get("attachment_id"), match.get("snippet")
                if not isinstance(attachment_id, str) or not isinstance(snippet, str):
                    continue
                file_name = str(match.get("file_name") or attachment_id)
                section_id, sheet, row = (
                    match.get("section_id"),
                    match.get("sheet"),
                    match.get("row_number"),
                )
                if isinstance(section_id, str):
                    title, page = sections.get((attachment_id, section_id), (None, None))
                    add(
                        _attachment_item(
                            attachment_id,
                            file_name,
                            f"{ATTACHMENT_DOCUMENT_PREFIX}{attachment_id}:{section_id}",
                            snippet,
                            locator=_section_locator(page),
                            section_title=title,
                            content_kind="text",
                        )
                    )
                elif isinstance(sheet, str) and isinstance(row, int):
                    add(
                        _attachment_item(
                            attachment_id,
                            file_name,
                            f"{ATTACHMENT_DOCUMENT_PREFIX}{attachment_id}:{sheet}:{row}-{row}",
                            snippet,
                            locator=_rows_locator(sheet, row, row),
                            content_kind="sheet",
                        )
                    )
    return list(items.values())


def _image_evidence(output: Mapping[str, Any]) -> JsonObject | None:
    """画像の原本を読んだ根拠（#2652）。モデルへ届いた画像か、Vision モデルが読んだ文だけ。

    モデルに渡らなかった画像（非対応・上限）は読んだ扱いにしない。
    """
    attachment_id, sha = output.get("attachment_id"), output.get("content_sha256")
    if not isinstance(attachment_id, str) or not isinstance(sha, str):
        return None
    focus = str(output.get("focus") or "")
    refs = [ref for ref in output.get("content_images") or [] if isinstance(ref, Mapping)]
    delivered = [ref for ref in refs if ref.get("model_input") == "attached"]
    reading = output.get("content")
    if delivered:
        text = f"原画像（観点: {focus}）"
        provenance: JsonObject = {
            "image_id": delivered[0].get("image_id"),
            "model_input": "attached",
        }
    elif isinstance(reading, str) and reading.strip():
        text = reading
        provenance = {
            "read_by": output.get("read_by"),
            "content_source": output.get("content_source"),
        }
    else:
        return None
    item = _attachment_item(
        attachment_id,
        str(output.get("file_name") or attachment_id),
        f"{ATTACHMENT_DOCUMENT_PREFIX}{attachment_id}:image:{sha[:16]}",
        text,
        locator={},
        content_kind="image",
    )
    item["image_reading"] = {**provenance, "focus": focus, "content_sha256": sha}
    return item


# ファイル名の照合で同じとみなすダッシュ（モデルは「-」を「‑」などに書き換えることがある）。
_DASHES = str.maketrans(dict.fromkeys("‐‑‒–—―−－", "-"))


def _comparable(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_DASHES).casefold()


def _attachment_cited(answer: str, file_name: str) -> bool:
    """回答が添付のファイル名（拡張子つき、または拡張子なし）を書いたか。"""
    name = _comparable(file_name).strip()
    if not name:
        return False
    text = _comparable(answer)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return name in text or (len(stem) >= 3 and stem in text)


def _supported_claims(validation: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """最終の検証が裏付けた（`supported`）段落と根拠の chunk（段落の原文, chunk）。"""
    if not isinstance(validation, Mapping) or validation.get("status") != STATUS_COMPLETED:
        return []
    result = validation.get("result")
    claims = result.get("claims") if isinstance(result, Mapping) else None
    found: list[tuple[str, str]] = []
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, Mapping) or claim.get("status") != "supported":
            continue
        quote, chunk_id = claim.get("answer_quote"), claim.get("chunk_id")
        if isinstance(chunk_id, str) and chunk_id:
            found.append((quote if isinstance(quote, str) else "", chunk_id))
    return found


def _figure_image_readings(validation: Mapping[str, Any] | None) -> dict[str, JsonObject]:
    """最終の検証が図の画像で確かめた段落の図（chunk → 画像の読み取りの由来。#2678）。

    由来は画面と MCP の根拠の `image_reading`（会話の添付の画像と同じ項目。#2652）に入れる: モデルへ
    届いた画像（`model_input=attached`・`image_id`）と、図の版（`chunk_set_id`）・領域（`region`）。
    """
    if not isinstance(validation, Mapping) or validation.get("status") != STATUS_COMPLETED:
        return {}
    result = validation.get("result")
    claims = result.get("claims") if isinstance(result, Mapping) else None
    found: dict[str, JsonObject] = {}
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, Mapping) or claim.get("figure_check") != FIGURE_IMAGE_READ:
            continue
        chunk_id, image = claim.get("chunk_id"), claim.get("figure_image")
        if isinstance(chunk_id, str) and isinstance(image, Mapping) and chunk_id not in found:
            found[chunk_id] = {
                "model_input": "attached",
                **{
                    key: image[key]
                    for key in ("image_id", "chunk_set_id", "region", "carried")
                    if key in image
                },
            }
    return found


def _pending_quotes(validation: Mapping[str, Any] | None) -> set[str]:
    """最終の検証が印を付けた段落のうち、根拠で確かめていない段落（#2700・#2738）。

    `answer_validation.publish_answer` が本文に残して印（`passage_marks`）を付けた段落から、
    検証が裏付けた（`supported`）段落を除いたもの（未確認の段落と、規則の検査の警告の段落）。
    その根拠は該当を確かめた記載にしない（「参考情報」にする）。
    """
    if not isinstance(validation, Mapping) or validation.get("status") != STATUS_COMPLETED:
        return set()
    marks = validation.get("passage_marks")
    marked = {
        item["text"]
        for item in (marks if isinstance(marks, list) else [])
        if isinstance(item, Mapping) and isinstance(item.get("text"), str) and item["text"]
    }
    return marked - {quote for quote, _chunk_id in _supported_claims(validation)}


# 回答が本文に書いた根拠の ID の印（「【証拠 ID: 0f3a…:7】」「〔根拠: …〕」「[evidence_id: …]」）。
# 中の ID を根拠に引き当てられた印だけを数える（Markdown のリンク `[文](URL)` は除く）。
_MARKER = re.compile(r"【[^】\n]{1,200}】|〔[^〕\n]{1,200}〕|\[[^\]\n]{1,200}\](?!\()")


def _ordered_ids(ids: set[str], order: Sequence[str]) -> list[str]:
    return [chunk_id for chunk_id in order if chunk_id in ids]


def _markers(answer: str, chunk_ids: Sequence[str]) -> list[JsonObject]:
    """回答の中の根拠の ID の印と、引き当てた根拠（印の原文, chunk の一覧）。"""
    markers: list[JsonObject] = []
    for text in dict.fromkeys(match.group(0) for match in _MARKER.finditer(answer)):
        ids = cited_chunk_ids(text, chunk_ids)
        if ids:
            markers.append({"text": text, "chunk_ids": _ordered_ids(ids, chunk_ids)})
    return markers


def _passages(
    answer: str,
    chunk_ids: Sequence[str],
    supported: list[tuple[str, str]],
    markers: list[JsonObject],
    text_bound: Mapping[str, list[str]],
) -> list[JsonObject]:
    """回答の段落ごとの根拠（段落の原文, chunk の一覧）。

    根拠は、最終の検証がその段落を裏付けた根拠と、段落が書いた根拠の ID の印の根拠と、決定的な
    照合で結び付けた根拠（`text_bound`。検証が判定を出さなかったときだけ）。画面は段落の下に
    「根拠：文書 p.N」を出す。根拠の無い段落は入れない。
    """
    known = set(chunk_ids)
    passages: list[JsonObject] = []
    for text in dict.fromkeys(_answer_passages(answer)):
        ids = {chunk_id for quote, chunk_id in supported if quote == text and chunk_id in known}
        for marker in markers:
            if marker["text"] in text:
                ids.update(marker["chunk_ids"])
        ids.update(text_bound.get(text, ()))
        if ids:
            passages.append({"text": text, "chunk_ids": _ordered_ids(ids, chunk_ids)})
    return passages


def _answer_passages(answer: str) -> list[str]:
    """回答の段落の原文（行ごとに分けた文。最終の検証の `answer_quote` と同じ分け方）。"""
    return [
        text for line in answer.split("\n") for _start, _end, text in passage_spans(line) if text
    ]


# ---- 決定的な照合（検証が判定を出さなかったとき。#2707） ------------------------------

# 文書名の後ろの頁（「p.3」「p. 3-5」「pp.3–5」「3 頁」「3ページ」）。
_PAGE = re.compile(
    r"(?<![A-Za-z])p{1,2}\.?\s*([0-9]{1,4})(?:\s*[-–〜~]\s*([0-9]{1,4}))?"
    r"|([0-9]{1,4})\s*(?:[-–〜~]\s*([0-9]{1,4})\s*)?(?:ページ|頁)",
    re.IGNORECASE,
)
# 抜粋の中の HTML の印（表の抜粋）と、原文を文に分ける区切り。
_TAG = re.compile(r"<[^>]+>")
_SENTENCE_END = re.compile(r"(?<=[。！？!?])|\n+")
_MIN_FILE_STEM_CHARS = 3


def _evidence_text(item: Mapping[str, Any]) -> EvidenceText:
    evidence = EvidenceText()
    evidence.add(item.get("excerpt"), locator_labels(dict(item)), [item.get("file_name")])
    return evidence


def _pages(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _PAGE.finditer(unicodedata.normalize("NFKC", text)):
        start = int(match.group(1) or match.group(3))
        end = int(match.group(2) or match.group(4) or start)
        ranges.append((start, max(start, end)))
    return ranges


def _page_range(item: Mapping[str, Any]) -> tuple[int, int] | None:
    locator = item.get("locator")
    if not isinstance(locator, Mapping):
        return None
    start, end = locator.get("page_start"), locator.get("page_end")
    if not isinstance(start, int) or isinstance(start, bool) or start <= 0:
        return None
    if isinstance(end, int) and not isinstance(end, bool) and end >= start:
        return start, end
    return start, start


def _names_document(text: str, file_name: str) -> bool:
    """段落が文書のファイル名（拡張子つき、または 3 文字以上の拡張子なしの名前）を書いたか。"""
    name = _comparable(file_name).strip()
    if not name:
        return False
    value = _comparable(text)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return name in value or (len(stem) >= _MIN_FILE_STEM_CHARS and stem in value)


def _located(text: str, items: Sequence[Mapping[str, Any]], *, citation: bool) -> list[str]:
    """段落が文書名と頁で指した根拠（文書名を書き、書いた頁が根拠の頁に重なるもの）。

    出典の行（`citation`）で頁を書いていないときは、その文書の最初の根拠に結び付ける（どの根拠か
    決まらないが、文書は回答が示した）。主張の段落の頁の無い文書名は結び付けない（特徴の照合に任せる）。
    """
    pages = _pages(text)
    found: list[str] = []
    for file_name in dict.fromkeys(str(item.get("file_name") or "") for item in items):
        if not file_name or not _names_document(text, file_name):
            continue
        same = [item for item in items if item.get("file_name") == file_name]
        if pages:
            for item in same:
                page = _page_range(item)
                if page and any(start <= page[1] and page[0] <= end for start, end in pages):
                    found.append(str(item["chunk_id"]))
        elif citation:
            found.append(str(same[0]["chunk_id"]))
    return found


def _featured(
    passage: str, items: Sequence[Mapping[str, Any]], texts: list[EvidenceText]
) -> list[str]:
    """段落の特徴（`evidence_match`）で当たった根拠。回答に固有の特徴（その特徴を持つ根拠が少ない）
    で当たったものを、当たりの重みの大きい順に `MAX_TEXT_BOUND_PER_PASSAGE` 件まで。"""
    features = answer_features(passage)
    matched = [matched_features(features, text) for text in texts]
    ranked = sorted(
        (
            (-weight, index)
            for index, ((distinct, weight), keys) in enumerate(
                zip(order_weights(matched), matched, strict=True)
            )
            if keys and distinct
        ),
    )
    return [str(items[index]["chunk_id"]) for _weight, index in ranked[:MAX_TEXT_BOUND_PER_PASSAGE]]


def _text_bound(
    answer: str, items: Sequence[Mapping[str, Any]], only: set[str] | None = None
) -> dict[str, list[str]]:
    """決定的な照合で段落に結び付けた根拠（段落の原文 → chunk の一覧）。

    主張の段落は、段落の特徴で当たった根拠と、書いた文書名と頁で指した根拠。出典の行は、書いた
    文書名と頁で指した根拠。見出し・質問・不足の文などには結び付けない。`only` を渡したら、その
    段落（最終の検証が印を付けた、根拠で確かめていない主張の段落）だけを結び付ける。
    """
    if not items:
        return {}
    kinds = non_claim_passages(answer)
    texts = [_evidence_text(item) for item in items]
    bound: dict[str, list[str]] = {}
    for passage in dict.fromkeys(_answer_passages(answer)):
        kind = kinds.get(passage)
        if only is not None and (passage not in only or kind is not None):
            continue
        if kind is not None and kind != KIND_CITATION:
            continue
        ids = _located(passage, items, citation=kind == KIND_CITATION)
        if kind is None:
            ids.extend(_featured(passage, items, texts))
        if ids:
            bound[passage] = list(dict.fromkeys(ids))
    return bound


def validation_passage_citations(
    answer: str, items: Sequence[Mapping[str, Any]]
) -> list[JsonObject]:
    """最終の検証（`rag_validate_answer`）に渡す段落ごとの出典（`passage_citations`。#104）。

    業務 Agent の回答は、使ったツールと根拠を本文とは別に示す（#2328）ため、段落に根拠の ID や
    出典の行を書かないことが多い。RAG は段落の実際の出典の集合だけで主張を裏付ける（未引用の資料で
    裏付けない）ので、出典を渡さなければ正しい段落もすべて「確かめが終わっていない」（unassessed）に
    なる。そこで、この Run の根拠（`items`。この接続に渡す根拠だけ）と回答の段落を、検証が判定を
    出さなかったときの「回答の根拠」と同じ決定的な照合（`_text_bound`。段落が書いた文書名と頁・
    章節の参照・引用・同じ文字の並び）で結び付けて渡す。

    - 段落そのものに結び付いた根拠があれば、その根拠。
    - 無い段落（表の値の行など）は、回答全体が出典にした根拠（他の段落・出典の行が結び付けた根拠と、
      本文の根拠の ID の印）。RAG が ID を書かない段落を回答の他の段落が引いた根拠で確かめ直す
      （#1412）のと同じ考え方。
    - 回答のどこにも根拠が結び付かなければ渡さない（RAG は今までどおり unassessed にする）。
    - 主張ではない段落（見出し・出典の行・表の形・質問・不足の文）は渡さない。

    出典は RAG に「この段落はこの根拠に拠る」と申告するだけで、裏付けの判定は RAG が根拠の本文で
    行う（結び付けが誤っていれば supported にならない）。本文に出典を書いた段落は RAG が本文の
    出典を使う（ここで広げない）。位置は `answer_passage_positions`（RAG の `answer_range` と
    同じ数え方）。
    """
    if not answer.strip() or not items:
        return []
    known = [str(item["chunk_id"]) for item in items if item.get("chunk_id")]
    bound = _text_bound(answer, items)
    whole = _ordered_ids(
        {chunk_id for ids in bound.values() for chunk_id in ids} | cited_chunk_ids(answer, known),
        known,
    )
    kinds = non_claim_passages(answer)
    citations: list[JsonObject] = []
    for start, end, text in answer_passage_positions(answer):
        if not text or kinds.get(text) is not None:
            continue
        ids = _ordered_ids(set(bound.get(text) or ()), known) or whole
        if not ids:
            continue
        citations.append(
            {
                "answer_range": {"start": start, "end": end},
                "chunk_ids": ids[:MAX_VALIDATION_CITATION_CHUNKS],
            }
        )
    return citations[:MAX_VALIDATION_PASSAGE_CITATIONS]


def run_validation_passage_citations(
    steps: Sequence[RunStep], answer: str, refs: Sequence[Mapping[str, Any]]
) -> list[JsonObject]:
    """この Run で AI に渡した根拠のうち `refs`（1 つの接続に渡す根拠の参照）の根拠で作る、
    最終の検証の段落ごとの出典（`validation_passage_citations`。#104）。

    前のターンから引き継いだ根拠（`carried_evidence`）は今のターンの根拠の本文を持たないので
    結び付けない（RAG は本文の出典が無ければ今までどおり確かめる）。
    """
    wanted = {str(ref.get("chunk_id") or "") for ref in refs} - {""}
    items = [item for item in passed_evidence(steps) if str(item.get("chunk_id")) in wanted]
    return validation_passage_citations(answer, items)


def _reference_quote(item: Mapping[str, Any], passages: Sequence[str]) -> str:
    """「参考情報」に出す根拠の原文（抜粋の中で、結び付けた段落と最も多くの特徴が当たる文）。

    当たる文が無ければ抜粋の先頭の文。`REFERENCE_QUOTE_CHARACTERS` を超えたら縮めて「…」を付ける。
    原文の文字は変えない（表の抜粋の HTML の印だけを除き、空白をつめる）。
    """
    excerpt = _TAG.sub(" ", str(item.get("excerpt") or ""))
    sentences = [" ".join(part.split()) for part in _SENTENCE_END.split(excerpt)]
    sentences = [sentence for sentence in sentences if sentence]
    if not sentences:
        return ""
    features = [answer_features(passage) for passage in passages]
    best, score = sentences[0], 0
    for sentence in sentences:
        evidence = EvidenceText()
        evidence.add(sentence, [])
        found = sum(len(matched_features(feature, evidence)) for feature in features)
        if found > score:
            best, score = sentence, found
    if len(best) > REFERENCE_QUOTE_CHARACTERS:
        return best[:REFERENCE_QUOTE_CHARACTERS].rstrip() + "…"
    return best


def _references(
    passed: Sequence[Mapping[str, Any]], passages: Sequence[JsonObject]
) -> list[JsonObject]:
    """「参考情報」の根拠（根拠ごとの原文。回答の段落に結び付いた順）。"""
    by_chunk = {str(item["chunk_id"]): item for item in passed}
    used: dict[str, list[str]] = {}
    for passage in passages:
        for chunk_id in passage["chunk_ids"]:
            used.setdefault(chunk_id, []).append(passage["text"])
    references: list[JsonObject] = []
    for chunk_id, texts in used.items():
        item = by_chunk.get(chunk_id)
        quote = _reference_quote(item, texts) if item is not None else ""
        if quote:
            references.append({"quote": quote, "chunk_ids": [chunk_id]})
    return references


def answer_evidence(
    steps: Sequence[RunStep], answer: str, validation: Mapping[str, Any] | None
) -> JsonObject:
    """成果物「回答の根拠」の内容。

    - `items`・`passed`・`cited`: AI に渡した根拠と、回答で引用した根拠の数。
    - `markers`: 回答の中の根拠の ID の印と引き当てた根拠（画面は印を文字で出さず、根拠の
      行にする）。
    - `passages`: 段落ごとの根拠（画面は段落の下に「根拠：文書 p.N」を出す）。
    - `binding`: 結び付けの方法（`validation` / `partial` / `text`。#2707）。
    - `references`: `text` / `partial` のときの「参考情報」の根拠（根拠の原文 `quote` と chunk）。
    - `citation_lines`: `text` のときの、根拠に結び付いた出典の行（画面は「参考情報」の根拠の
      行で示し、平文の行を重ねて出さない）。

    `answer` は利用者に見せる回答（最終の検証の後の本文）、`validation` は最終の検証の成果物の内容
    （無ければ None）。
    """
    passed = passed_evidence(steps)
    chunk_ids = [str(item["chunk_id"]) for item in passed]
    supported = _supported_claims(validation)
    figures = _figure_image_readings(validation)
    pending = _pending_quotes(validation)
    if not isinstance(validation, Mapping) or validation.get("status") != STATUS_COMPLETED:
        binding = BINDING_TEXT
    else:
        binding = BINDING_PARTIAL if pending else BINDING_VALIDATION
    markers = _markers(answer, chunk_ids)
    if binding == BINDING_TEXT:
        text_bound = _text_bound(answer, passed)
    elif binding == BINDING_PARTIAL:
        text_bound = _text_bound(answer, passed, only=pending)
    else:
        text_bound = {}
    passages = _passages(
        answer, chunk_ids, supported, markers, text_bound if binding == BINDING_TEXT else {}
    )
    # 印の段落（根拠で確かめていない）は本文の根拠の行にせず、「参考情報」の根拠にする。
    reference_passages = passages
    if binding == BINDING_PARTIAL:
        reference_passages = _passages(answer, chunk_ids, [], markers, text_bound)
        reference_passages = [item for item in reference_passages if item["text"] in pending]
        passages = [item for item in passages if item["text"] not in pending]
    confirmed = {chunk_id for item in passages for chunk_id in item["chunk_ids"]} | {
        chunk_id for _quote, chunk_id in supported
    }
    cited = (
        cited_chunk_ids(answer, chunk_ids)
        | {chunk_id for _quote, chunk_id in supported}
        | {chunk_id for ids in text_bound.values() for chunk_id in ids}
    )
    items: list[JsonObject] = []
    for item in passed:
        view: JsonObject = {key: item.get(key) for key in _COPIED_FIELDS if key in item}
        section = item.get("section")
        if isinstance(section, Mapping) and isinstance(section.get("title"), str):
            view["section_title"] = section["title"]
        used = item["chunk_id"] in cited
        view["cited"] = used
        answer_use = item.get("answer_use")
        if binding == BINDING_TEXT or (
            binding == BINDING_PARTIAL and used and item["chunk_id"] not in confirmed
        ):
            # 照合で確かめていない引用は、該当を確認した記載にしない（#2707）。
            view["answer_use"] = "reference" if used else None
        else:
            view["answer_use"] = answer_use if used and answer_use in _ANSWER_USES else None
        if used and item["chunk_id"] in figures:
            # 図の見た目の段落を、モデルへ届いた図の画像で確かめた（#2678）。
            view["image_reading"] = figures[item["chunk_id"]]
        items.append(view)
    for item in attachment_evidence(steps)[: max(MAX_ANSWER_EVIDENCE - len(items), 0)]:
        view = {key: item.get(key) for key in _COPIED_FIELDS if key in item}
        section = item.get("section")
        if isinstance(section, Mapping) and isinstance(section.get("title"), str):
            view["section_title"] = section["title"]
        if isinstance(item.get("image_reading"), Mapping):
            # 画像を読んだ由来（モデルへ届いた画像か、読んだ Vision モデルと観点。#2652）。
            view["image_reading"] = dict(item["image_reading"])
        view["cited"] = _attachment_cited(answer, str(item.get("file_name") or ""))
        view["answer_use"] = None
        items.append(view)
    content: JsonObject = {
        "items": items,
        "passed": len(items),
        "cited": sum(1 for item in items if item["cited"]),
        "markers": markers,
        "passages": passages,
        "binding": binding,
        "references": [],
        "citation_lines": [],
    }
    if binding == BINDING_PARTIAL:
        # 確かめた段落の根拠の行にある根拠は、「参考情報」に重ねない。
        content["references"] = [
            item
            for item in _references(passed, reference_passages)
            if not set(item["chunk_ids"]) & confirmed
        ]
    if binding == BINDING_TEXT:
        content["references"] = _references(passed, passages)
        kinds = non_claim_passages(answer)
        content["citation_lines"] = [
            passage["text"] for passage in passages if kinds.get(passage["text"]) == KIND_CITATION
        ]
    return content
