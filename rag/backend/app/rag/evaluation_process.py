"""評価のケースの「回答の過程」を、評価の期待（手順・条件・根拠・対応）の下書きにする（#2402）。

利用者の担当者は、問ごとの「回答の過程」を決まった見出しの半構造の文で書いている（回答の手順・必要な
確認事項・検索する資料と頁・検索の語・補足）。この文を次の順で期待の下書きに直す（#2397）。

1. 節に分ける（``split_process_sections``）: 言語資源の見出しの語（``process_headings``）で分ける。
2. 構造化（``draft_process_expectations``）: 1 行で LLM を 1 回呼ぶ（共通の上限つき並行の部品。
   #2004）。応答は ``ProcessDraftOutput``（Pydantic）で検証する。
3. 資料名の解決（``resolve_document_name``）: 資料の名前をナレッジベースの文書の名前と照合する。同じ
   正本を指す重複のアップロードは正本にまとめてから決める（#2662）。
4. 資料の位置（``_location_problem``）: 表計算の資料はシート・行・セル、それ以外の資料は頁で位置を
   書く。書き方が資料の種類に合わない・読めない位置は頁にせず、警告にする（#2663）。

期待への対応（LLM を呼ばない決定的な対応）:

- 手順 → `required_answer_points`: 手順の題名と説明を使い、意味上の網羅性を測る。
- 確認事項 → `required_conditions`: 条件の名前。
- 資料と頁 → 解決できた資料は正解の文書（`relevant_document_ids`）にし、頁・章節があれば必要な根拠
  （`required_evidence`）にする。解決できない・複数に当たる資料は下書きの警告にする。
- 対応 → `expected_outcomes`: 手順があれば回答・条件付きの回答、利用者に確かめる条件があれば回答前の
  確認、窓口への引き継ぎがあれば人への引き継ぎを受け入れる。

下書きは保存しない。画面で確かめてから評価のケースに足す。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pr_backend_core.bounded_calls import Attempt
from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rag_engine.resources.runtime import resolve_prompt
from rag_parser_core.language import LanguagePack
from rag_parser_core.language.markers import marker_patterns

from app.rag.cross_references import document_name_match, title_key
from app.rag.parser_source_guard import is_spreadsheet_file
from app.schemas.evaluation import (
    EvaluationEvidence,
    EvaluationOutcome,
    EvaluationProcessDraft,
    EvaluationProcessReference,
    EvaluationProcessRow,
)

logger = logging.getLogger(__name__)

PROCESS_FIELDS: tuple[str, ...] = ("steps", "conditions", "references", "search_terms", "notes")
MAX_PAGE_NUMBER = 100_000
MAX_SHEET_ROW = 1_048_576
_MAX_REFERENCES = 30

# 頁の書き方（「12」「12-14」「p.12」「12〜14 頁」）。数字の範囲だけを読む。
_PAGE_RANGE = re.compile(r"(\d{1,6})\s*(?:[-~〜～–—]\s*(\d{1,6}))?")
# 頁の書き方の略記（p. / pp.）。頁の語そのもの（「頁」「ページ」）は言語資源の markers.page_words。
_PAGE_ABBREVIATION = re.compile(r"(?<![A-Za-z])pp?(?![A-Za-z])", re.IGNORECASE)
# 表計算のセルの範囲（A1 形式）。
_CELL_RANGE = re.compile(r"[A-Za-z]{1,3}[1-9]\d{0,6}(?:\s*:\s*[A-Za-z]{1,3}[1-9]\d{0,6})?")
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")
# 見出しの後ろに続く飾りの分類（閉じ括弧・区切りの記号・ダッシュ）。
_HEADING_TAIL = frozenset({"Pe", "Pf", "Po", "Pd"})


# ---- 節に分ける -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessSection:
    """回答の過程の 1 つの節（見出しの無い先頭の文は ``field`` が None）。"""

    field: str | None
    heading: str
    text: str


def process_headings(pack: LanguagePack) -> dict[str, str]:
    """言語資源の見出しの語 → 回答の過程の項目。"""
    mapping = pack.mapping("evaluation_process_cues", "process_headings")
    return {word: field for word, field in mapping.items() if field in PROCESS_FIELDS}


def _is_mark(char: str) -> bool:
    """見出しの飾り（記号・句読点・空白）か。言語に依らない Unicode の分類で決める。"""
    return char.isspace() or unicodedata.category(char)[0] in {"P", "S"}


def _heading(line: str, keys: Sequence[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """行が見出しなら (項目, 見出しの語, 見出しの後ろの文)。

    行の前後の飾り（【】・■・「：」など）を外した文が見出しの語で始まり、その後ろが空か飾りで
    始まるときだけ見出しとみなす（「手順書を開く」の「手順」を見出しにしない）。
    """
    text = unicodedata.normalize("NFKC", line).strip()
    start = 0
    while start < len(text) and _is_mark(text[start]):
        start += 1
    body = text[start:]
    folded = body.casefold()
    for key, word, field in keys:
        if not folded.startswith(key):
            continue
        rest = body[len(key) :]
        if rest and not _is_mark(rest[0]):
            continue
        # 見出しの後ろの閉じ括弧・区切り（「】」「：」）を外す。開き括弧から始まる内容は残す。
        index = 0
        while index < len(rest) and (
            rest[index].isspace() or unicodedata.category(rest[index]) in _HEADING_TAIL
        ):
            index += 1
        return field, word, rest[index:].strip()
    return None


def split_process_sections(text: str, headings: Mapping[str, str]) -> list[ProcessSection]:
    """回答の過程の文を、見出しの語の行で節に分ける（見出しが無ければ全体を 1 つの節にする）。"""
    keys = sorted(
        (
            (unicodedata.normalize("NFKC", word).casefold().strip(), word, field)
            for word, field in headings.items()
            if word.strip()
        ),
        key=lambda item: -len(item[0]),
    )
    sections: list[ProcessSection] = []
    field: str | None = None
    heading = ""
    lines: list[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body or field is not None:
            sections.append(ProcessSection(field, heading, body))

    for line in text.splitlines():
        found = _heading(line, keys) if keys else None
        if found is None:
            lines.append(line)
            continue
        flush()
        field, heading, rest = found
        lines = [rest] if rest else []
    flush()
    return [section for section in sections if section.text or section.field]


# ---- LLM の応答の形 -------------------------------------------------------------------------


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class DraftStep(_Out):
    title: str = Field(description="手順の題名（画面・操作が分かる短い文）。")
    purpose: str = Field(default="", description="手順の目的・内容。")
    note: str = Field(default="", description="この手順の注意（補足のうち手順に係るもの）。")


class DraftCondition(_Out):
    label: str = Field(description="条件の名前。")
    handling: Literal["ask", "branch", "handoff"] = Field(
        default="ask", description="分からないとき: ask=確かめる / branch=分岐 / handoff=窓口へ。"
    )


class DraftReference(_Out):
    document_name: str = Field(description="資料の名前（回答の過程に書いたまま）。")
    pages: str = Field(
        default="", description="頁（書いたまま。例: 12-14）。表計算の行・セルは入れない。"
    )
    sheet_name: str = Field(default="", description="表計算の資料のシート名（書いたまま）。")
    rows: str = Field(default="", description="表計算の資料の行（書いたまま。例: 42-45）。")
    cells: str = Field(
        default="", description="表計算の資料のセルの範囲（書いたまま。例: B3:D10）。"
    )
    section: str = Field(default="", description="章節（書いたまま）。")


class ProcessDraftOutput(_Out):
    """LLM の応答（回答の過程を構造にしたもの）。"""

    steps: list[DraftStep] = Field(default_factory=list)
    conditions: list[DraftCondition] = Field(default_factory=list)
    references: list[DraftReference] = Field(default_factory=list)
    handoff_contact: str = Field(default="", description="窓口へ連絡するときの窓口の名前。")
    handoff_conditions: list[str] = Field(default_factory=list, description="窓口へ連絡する条件。")


PROCESS_DRAFT_RESPONSE_SCHEMA = ProcessDraftOutput.model_json_schema()

PROCESS_DRAFT_SYSTEM_PROMPT = (
    "## Step 1: 評価の期待の下書きを作る\n"
    "あなたは、問い合わせ対応の「回答の過程」を、回答の評価で使う期待の下書き（JSON）に直す"
    "担当です。\n"
    "\n"
    "## Step 2: 入力の範囲を守る\n"
    "- 回答の過程に書いてあることだけを使い、書いていない手順・条件・資料・頁を足さないで"
    "ください。\n"
)

PROCESS_DRAFT_PROMPT = (
    "## Step 1: 入力の項目を確認する\n"
    "\n"
    "入力の JSON は、利用者の質問（question）、回答の過程を見出しで分けた節（sections）、任意の"
    "標準回答\n"
    "（standard_answer）です。節の field は次の項目を表します（null は見出しの無い文）。\n"
    "\n"
    "## Step 2: 手順・条件・根拠を対応付ける\n"
    "- steps（回答の手順）: steps に順に入れる。\n"
    "- conditions（必要な確認事項）: 確かめる事項を conditions に入れる。\n"
    "  利用者に聞けば決まる事項は handling=ask、値で手順が分かれるだけの事項は handling=branch "
    "にする。\n"
    "  「窓口へ連絡」のような人へ引き継ぐ場合は handling=handoff にし、handoff_contact に窓口の"
    "名前、\n"
    "  handoff_conditions にその場合を入れる。\n"
    "- references（検索する資料と根拠の頁）: 資料の名前は書いたまま document_name に、頁は pages、"
    "\n"
    "  表計算のシート・行・セルの範囲は sheet_name・rows・cells、章節は section に入れる。\n"
    "- notes（補足）: 1 つの手順に係る注意はその手順の note に入れる。\n"
    "- search_terms（検索の語）: どの項目にも入れない。\n"
)


class ProcessDraftLlm(Protocol):
    async def generate(
        self,
        prompt: str,
        context: str,
        *,
        system_prompt: str | None = None,
        response_schema: Mapping[str, Any] | None = None,
        response_schema_name: str = ...,
    ) -> str: ...


def parse_draft_output(raw: str) -> ProcessDraftOutput:
    """LLM の応答を検証する（JSON の前後の文は外す）。形が合わなければ ``ValueError``。"""
    match = _JSON_OBJECT.search(str(raw or ""))
    if match is None:
        raise ValueError("応答に JSON がありません。")
    try:
        return ProcessDraftOutput.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("応答の JSON が期待の下書きの形ではありません。") from exc


def draft_context(row: EvaluationProcessRow, headings: Mapping[str, str]) -> str:
    """LLM に渡す 1 行の入力（質問・節に分けた回答の過程・標準回答）。"""
    sections = split_process_sections(row.answer_process, headings)
    payload = {
        "question": row.question,
        "sections": [
            {"field": section.field, "heading": section.heading, "text": section.text}
            for section in sections
        ],
        "standard_answer": row.standard_answer,
    }
    return json.dumps(payload, ensure_ascii=False)


# ---- 資料名の解決 ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeDocument:
    """ナレッジベースの文書（照合に使う名前）。"""

    document_id: str
    file_name: str
    # 同じ内容の重複のアップロードなら正本の文書の id（``duplicate_of_document_id``。#2662）。
    duplicate_of: str = ""
    # 表計算の資料か（頁ではなくシート・行で位置を書く。#2663）。
    spreadsheet: bool = False

    @property
    def identity(self) -> str:
        """同じ内容の文書をまとめる id（重複なら正本の id）。"""
        return self.duplicate_of or self.document_id


async def list_knowledge_documents(
    oracle: Any, knowledge_base_ids: Sequence[str]
) -> list[KnowledgeDocument]:
    """ナレッジベースの文書（利用者が見られるもの。新しい版に置き換えた文書は除く）。

    重複のアップロードは正本の id（``duplicate_of_document_id``）を持たせ、資料名の解決で正本に
    まとめる（#2662）。表計算の資料かは、頁ではなくシート・行で位置を書く資料かの判定に使う（#2663）。
    """
    documents: dict[str, KnowledgeDocument] = {}
    for knowledge_base_id in knowledge_base_ids:
        for summary in await oracle.list_documents(knowledge_base_id=knowledge_base_id):
            if summary.is_superseded:
                continue
            documents[summary.id] = KnowledgeDocument(
                summary.id,
                summary.file_name,
                duplicate_of=summary.duplicate_of_document_id or "",
                spreadsheet=is_spreadsheet_file(summary.file_name, summary.content_type),
            )
    return list(documents.values())


def _stem_key(file_name: str) -> str:
    return title_key(_EXTENSION.sub("", unicodedata.normalize("NFKC", file_name).strip()))


def _settle(
    candidates: Sequence[KnowledgeDocument], documents: Sequence[KnowledgeDocument]
) -> tuple[KnowledgeDocument | None, list[KnowledgeDocument]]:
    """候補を同じ内容の文書（正本と、その正本を指す重複）ごとにまとめて決める（#2662）。

    正本が候補の文書の中に見えるとき（選んだナレッジベースにあり、利用者が見られる）だけ、その正本と
    重複を 1 つにまとめ、正本の文書にする。正本が見えない（範囲の外・見られない・無い）重複は、
    まとめずにそれぞれの文書のまま扱う（見えない正本の id を参照にしない）。
    """
    visible = {item.document_id: item for item in documents}
    groups: dict[str, KnowledgeDocument] = {}
    for item in candidates:
        canonical = visible.get(item.identity)
        key = item.identity if canonical is not None else item.document_id
        groups.setdefault(key, canonical or item)
    settled = list(groups.values())
    return (settled[0], []) if len(settled) == 1 else (None, settled)


def resolve_document_name(
    name: str, documents: Sequence[KnowledgeDocument]
) -> tuple[KnowledgeDocument | None, list[KnowledgeDocument]]:
    """資料の名前に当たる文書（1 つに決まれば文書、決まらなければ None と当たった文書）。

    確かな順に照合し、最初に当たった順位の文書だけを候補にする。
    1. ファイル名（拡張子を除く形を含む）が同じ。
    2. 文書のタイトル・ファイル名の別名と同じ、またはファイル名が資料の名前を含む
       （``document_name_match``。他の文書への参照の解決と同じ規則）。
    3. 資料の名前がファイル名（拡張子を除く）を含む（利用者のファイル名の接頭辞・接尾辞）。最も
       長いファイル名だけを候補にする。
    候補は同じ正本の重複をまとめてから数える（``_settle``）。
    """
    key = title_key(name)
    stem = _stem_key(name)
    if len(key) < 2:
        return None, []
    exact = [
        item
        for item in documents
        if title_key(item.file_name) == key or (stem and _stem_key(item.file_name) == stem)
    ]
    if exact:
        return _settle(exact, documents)
    ranked: dict[int, list[KnowledgeDocument]] = {}
    for item in documents:
        rank = document_name_match(name, item.file_name)
        if rank is not None:
            ranked.setdefault(rank, []).append(item)
    if ranked:
        return _settle(ranked[min(ranked)], documents)
    contained = [
        (len(_stem_key(item.file_name)), item)
        for item in documents
        if len(_stem_key(item.file_name)) >= 2 and _stem_key(item.file_name) in key
    ]
    if not contained:
        return None, []
    longest = max(length for length, _ in contained)
    return _settle([item for length, item in contained if length == longest], documents)


# ---- 資料の位置 -----------------------------------------------------------------------------


def page_range(text: str) -> tuple[int, int | None] | None:
    """頁の書き方から最初の範囲（始まり, 終わり）を読む。読めなければ None。"""
    match = _PAGE_RANGE.search(unicodedata.normalize("NFKC", text or ""))
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else None
    if start < 1 or start > MAX_PAGE_NUMBER:
        return None
    if end is not None and end < start:
        end = None
    return start, end if end != start else None


def readable_as_pages(text: str) -> bool:
    """頁の書き方として読めるか（数字・範囲・区切りと、頁の語・略記だけ）。

    「行42」「A1:C3」のように、頁の語でない文字（行・セル・シートの名前など）を含む書き方は頁として
    読まない（行の番号を頁にしない。#2663）。頁の語は言語資源（``markers.page_words``）。
    """
    normalized = unicodedata.normalize("NFKC", text)
    stripped = re.sub(marker_patterns().page_words, " ", normalized, flags=re.IGNORECASE)
    stripped = _PAGE_ABBREVIATION.sub(" ", stripped)
    return not any(unicodedata.category(char)[0] == "L" for char in stripped)


def _sheet_location_readable(item: DraftReference) -> bool:
    """シート・行・セルの書き方が読めるか。

    シートの名前の無い行・セルは、どのシートの行か決まらないので読まない。
    """
    if not item.sheet_name.strip():
        return False
    rows = item.rows.strip()
    if rows and not any(
        1 <= int(match.group(1)) <= MAX_SHEET_ROW
        for match in _PAGE_RANGE.finditer(unicodedata.normalize("NFKC", rows))
    ):
        return False
    cells = item.cells.strip()
    return not cells or _CELL_RANGE.search(unicodedata.normalize("NFKC", cells)) is not None


def _location_problem(item: DraftReference, document: KnowledgeDocument) -> str | None:
    """解決した資料の根拠の位置の誤り（#2663）。無ければ None。

    表計算の資料はシート・行・セルで、それ以外の資料は頁で位置を書く。書き方が資料の種類に合わない・
    読めない位置は変換せず、誤りにする（行の番号を頁の範囲にしない）。
    """
    name = item.document_name.strip()
    pages = item.pages.strip()
    has_pages = bool(re.search(r"\d", unicodedata.normalize("NFKC", pages)))
    has_sheet = bool(item.sheet_name.strip() or item.rows.strip() or item.cells.strip())
    if document.spreadsheet:
        if has_pages and not has_sheet:
            return (
                f"資料「{name}」は表計算の資料のため、頁「{pages}」を根拠の位置にできません。"
                "シート・行・セルの範囲で指定してください。"
            )
        if has_sheet and not _sheet_location_readable(item):
            location = " ".join(
                part for part in (item.sheet_name, item.rows, item.cells) if part.strip()
            )
            return (
                f"資料「{name}」のシート・行・セル「{location.strip()}」を読めません。"
                "シートの名前と、行の番号（例: 42-45）かセルの範囲（例: B3:D10）で"
                "指定してください。"
            )
        return None
    if has_sheet:
        return (
            f"資料「{name}」は表計算の資料ではないため、シート・行・セルを根拠の位置に"
            "できません。頁か章節で指定してください。"
        )
    if pages and not readable_as_pages(pages):
        return (
            f"資料「{name}」の頁「{pages}」を頁の範囲として読めません。"
            "頁の番号と範囲（例: 12-14）で指定してください。"
        )
    return None


# ---- 期待の下書きにする ---------------------------------------------------------------------


def _step_terms(output: ProcessDraftOutput) -> list[str]:
    terms: list[str] = []
    for step in output.steps:
        purpose = step.purpose.strip()
        if step.note.strip():
            purpose = f"{purpose}\n注意: {step.note.strip()}" if purpose else f"注意: {step.note}"
        term = " — ".join(part for part in (step.title.strip(), purpose) if part)
        if term and term not in terms:
            terms.append(term)
    return terms[:30]


def _outcomes(output: ProcessDraftOutput) -> list[EvaluationOutcome]:
    outcomes: list[EvaluationOutcome] = []
    if output.steps:
        outcomes += ["answered", "conditional"]
    if any(condition.handling == "ask" for condition in output.conditions):
        outcomes.append("needs_clarification")
    handoff = bool(output.handoff_contact.strip() or output.handoff_conditions) or any(
        condition.handling == "handoff" for condition in output.conditions
    )
    if handoff:
        outcomes.append("needs_human")
        if "conditional" not in outcomes:
            outcomes.append("conditional")
    return outcomes


def _references(
    output: ProcessDraftOutput, documents: Sequence[KnowledgeDocument]
) -> tuple[list[EvaluationProcessReference], list[EvaluationEvidence], list[str]]:
    """資料と文書の対応、資料から作る必要な根拠、解決・位置の警告。"""
    references: list[EvaluationProcessReference] = []
    evidence: list[EvaluationEvidence] = []
    warnings: list[str] = []
    for item in output.references[:_MAX_REFERENCES]:
        name = item.document_name.strip()
        if not name:
            continue
        written = {"name": name, "pages": item.pages[:64], "section": item.section[:200]}
        found, candidates = resolve_document_name(name, documents)
        if found is None:
            references.append(
                EvaluationProcessReference(
                    **written,
                    status="ambiguous" if candidates else "unresolved",
                    candidates=[candidate.file_name for candidate in candidates[:10]],
                )
            )
            warnings.append(
                f"資料「{name}」に当たる文書が複数あります（"
                + "、".join(candidate.file_name for candidate in candidates[:5])
                + "）。回答の過程の資料の名前を文書の名前に合わせてください。"
                if candidates
                else f"資料「{name}」は、選んだナレッジベースの文書にありません。"
                "資料をナレッジベースに取り込んでから、もう一度下書きを作ってください。"
            )
            continue
        references.append(
            EvaluationProcessReference(
                **written,
                status="resolved",
                document_id=found.document_id,
                file_name=found.file_name,
            )
        )
        problem = _location_problem(item, found)
        if problem:
            warnings.append(problem)
        # 頁として読めた資料だけ（表計算の行・頁として読めない書き方は頁にしない。#2663）。
        pages = (
            page_range(item.pages)
            if not found.spreadsheet and problem is None and item.pages.strip()
            else None
        )
        section = item.section.strip() or None
        if pages is None and section is None:
            continue
        label = " | ".join(
            part
            for part in (
                found.file_name or name,
                f"p.{item.pages.strip()}" if pages else "",
                f"章節: {section}" if section else "",
            )
            if part
        )
        try:
            evidence.append(
                EvaluationEvidence(
                    id=f"p{len(evidence) + 1}",
                    document_id=found.document_id,
                    page_start=pages[0] if pages else None,
                    page_end=pages[1] if pages else None,
                    section=section[:300] if section else None,
                    label=label[:1000],
                )
            )
        except ValidationError:
            continue
    return references, evidence[:30], warnings


def process_draft(
    row: EvaluationProcessRow,
    output: ProcessDraftOutput,
    documents: Sequence[KnowledgeDocument],
) -> EvaluationProcessDraft:
    """LLM の応答（1 行）を、評価のケースの期待の下書きにする。"""
    references, evidence, warnings = _references(output, documents)
    relevant = list(
        dict.fromkeys(item.document_id for item in references if item.document_id is not None)
    )
    return EvaluationProcessDraft(
        case_id=row.case_id,
        row=row.row,
        required_answer_points=_step_terms(output),
        required_conditions=list(
            dict.fromkeys(
                condition.label.strip()
                for condition in output.conditions
                if condition.label.strip()
            )
        )[:30],
        expected_outcomes=_outcomes(output),
        relevant_document_ids=relevant,
        required_evidence=evidence,
        references=references,
        warnings=warnings,
    )


def failed_draft(row: EvaluationProcessRow, message: str) -> EvaluationProcessDraft:
    """下書きを作れなかった行（LLM の失敗・応答の形の誤り）。"""
    return EvaluationProcessDraft(case_id=row.case_id, row=row.row, error=message)


ProcessDraftCaller = Callable[
    [Sequence[EvaluationProcessRow], Callable[[EvaluationProcessRow, Attempt], Awaitable[str]]],
    Awaitable[list[tuple[str | None, str | None]]],
]


async def draft_process_expectations(
    rows: Sequence[EvaluationProcessRow],
    documents: Sequence[KnowledgeDocument],
    *,
    llm: ProcessDraftLlm,
    headings: Mapping[str, str],
    run: ProcessDraftCaller,
) -> list[EvaluationProcessDraft]:
    """行ごとに LLM で期待の下書きを作る。

    ``run`` は上限つき並行で呼び、行の順に (応答, 失敗の文) を返す。
    """

    async def once(row: EvaluationProcessRow, _: Attempt) -> str:
        return await async_call_with_model_purpose(
            "evaluation.process_draft",
            llm.generate,
            resolve_prompt("evaluation_process_draft.instruction", PROCESS_DRAFT_PROMPT),
            draft_context(row, headings),
            system_prompt=resolve_prompt(
                "evaluation_process_draft.system", PROCESS_DRAFT_SYSTEM_PROMPT
            ),
            response_schema=PROCESS_DRAFT_RESPONSE_SCHEMA,
            response_schema_name="evaluation_process_draft",
        )

    outcomes = await run(rows, once)
    drafts: list[EvaluationProcessDraft] = []
    for row, (raw, error) in zip(rows, outcomes, strict=True):
        if error is not None or raw is None:
            drafts.append(failed_draft(row, error or "下書きを作れませんでした。"))
            continue
        try:
            output = parse_draft_output(raw)
        except ValueError as exc:
            logger.warning("evaluation process draft invalid", extra={"row": row.row})
            drafts.append(failed_draft(row, f"下書きを作れませんでした（{exc}）。"))
            continue
        drafts.append(process_draft(row, output, documents))
    return drafts


__all__ = [
    "PROCESS_DRAFT_PROMPT",
    "PROCESS_DRAFT_SYSTEM_PROMPT",
    "KnowledgeDocument",
    "ProcessDraftOutput",
    "draft_process_expectations",
    "failed_draft",
    "list_knowledge_documents",
    "page_range",
    "parse_draft_output",
    "process_draft",
    "process_headings",
    "readable_as_pages",
    "resolve_document_name",
    "split_process_sections",
]
