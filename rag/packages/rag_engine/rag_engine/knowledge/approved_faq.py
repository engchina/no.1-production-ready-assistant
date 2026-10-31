"""Approved FAQ の登録、検索、semantic cache、feedback 昇格を扱う。"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import re
import shutil
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rag_engine.knowledge.answer_feedback import ANSWER_FEEDBACK_TYPE_VALUES
from rag_engine.retrieval.scope import RETRIEVAL_SCOPE_KNOWLEDGE_BASE, normalize_retrieval_scope

PROJECT_ROOT = Path.cwd()
APPROVED_FAQ_FILE = PROJECT_ROOT / "knowledge_assets" / "approved_faq" / "approved_faq_golden_qa.json"
APPROVED_FAQ_SCHEMA_VERSION = 1
APPROVED_FAQ_APPROVED_STATUS = "approved"
APPROVED_FAQ_BACKUP_DIR_NAME = "approved_faq_backups"
APPROVED_FAQ_IMPORT_MODE_INSERT = "INSERT"
APPROVED_FAQ_IMPORT_MODE_DELETE_THEN_INSERT = "DELETE_THEN_INSERT"
APPROVED_FAQ_IMPORT_MODES = (
    APPROVED_FAQ_IMPORT_MODE_INSERT,
    APPROVED_FAQ_IMPORT_MODE_DELETE_THEN_INSERT,
)
DEFAULT_APPROVED_FAQ_SUGGESTION_LIMIT = 5
DEFAULT_APPROVED_FAQ_MIN_SCORE = 0.1
DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE = 0.92
DEFAULT_APPROVED_FAQ_SEMANTIC_MIN_SCORE = 0.72
APPROVED_FAQ_FEEDBACK_SOURCE_FILE = "answer_feedback"
# FAQ 直接回答の payload が持つ retrieval_scope。feedback 昇格で RAG 回答と区別するためここに置く。
APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE = "approved_faq"
APPROVED_FAQ_SEMANTIC_CACHE_SCHEMA_VERSION = 1
APPROVED_FAQ_SEMANTIC_CACHE_DIR_NAME = "approved_faq_semantic"
APPROVED_FAQ_SEMANTIC_CACHE_FILE_NAME = "semantic_index.json"
APPROVED_FAQ_SCOPE_MODE_PREFER = "prefer"
APPROVED_FAQ_SCOPE_MODE_STRICT_WHEN_AVAILABLE = "strict_when_available"
APPROVED_FAQ_SCOPE_MODES = (
    APPROVED_FAQ_SCOPE_MODE_PREFER,
    APPROVED_FAQ_SCOPE_MODE_STRICT_WHEN_AVAILABLE,
)
MAX_APPROVED_FAQ_QUESTION_LENGTH = 1000
MAX_APPROVED_FAQ_ANSWER_LENGTH = 20000


@dataclass(frozen=True)
class ApprovedFaqRecord:
    """Approved FAQ の canonical question、回答、分類、出典を保持します。"""

    id: str
    question: str
    approved_answer: str
    alternate_questions: tuple[str, ...] = ()
    status: str = ""
    business: str = ""
    classification: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    timestamps: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()

    @property
    def match_questions(self) -> tuple[str, ...]:
        """canonical question と alternate question を照合候補として返します。"""
        return tuple(dict.fromkeys((self.question, *self.alternate_questions)))


@dataclass(frozen=True)
class ApprovedFaqSuggestion:
    """質問入力に対する FAQ 候補と一致 score を保持します。"""

    record: ApprovedFaqRecord
    matched_question: str
    score: float
    text_score: float = 0.0
    semantic_score: float | None = None
    scope_score: float = 0.0
    scope_matches: tuple[str, ...] = ()
    match_method: str = "text"


@dataclass(frozen=True)
class ApprovedFaqSemanticItem:
    """FAQ semantic index 内の 1 質問 embedding を保持します。"""

    record_id: str
    question: str
    question_hash: str
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class ApprovedFaqSemanticIndex:
    """FAQ semantic matching 用 cache の内容と署名を保持します。"""

    model: str
    dimensions: int
    records_signature: str
    items: tuple[ApprovedFaqSemanticItem, ...] = ()


@dataclass(frozen=True)
class ApprovedFaqImportRow:
    """Excel 取り込みで読み取った FAQ 行を表します。"""

    question: str
    approved_answer: str
    source_file: str = ""
    sheet: str = ""
    row: int = 0
    classification: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovedFaqSaveResult:
    """FAQ JSON 保存時のパス、backup、件数を保持します。"""

    path: Path
    backup_path: Path | None
    record_count: int
    saved_at: datetime


@dataclass(frozen=True)
class ApprovedFaqMutationResult:
    """FAQ の追加・削除・取込操作の結果を UI 向けに保持します。"""

    path: Path
    backup_path: Path | None
    record_count: int
    inserted_count: int
    deleted_count: int
    skipped_count: int
    saved_at: datetime


def load_approved_faq_payload(path: str | Path = APPROVED_FAQ_FILE) -> dict[str, Any]:
    """Approved FAQ JSON の payload を安全に読み込みます。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _default_approved_faq_payload()
    if not isinstance(payload, dict) or payload.get("schema_version") != APPROVED_FAQ_SCHEMA_VERSION:
        return _default_approved_faq_payload()
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        payload = dict(payload)
        payload["records"] = []
    return payload


def load_approved_faq_records(path: str | Path = APPROVED_FAQ_FILE) -> list[ApprovedFaqRecord]:
    """Approved FAQ JSON から有効な FAQ record を読み込みます。"""
    payload = load_approved_faq_payload(path)
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        return []

    records: list[ApprovedFaqRecord] = []
    seen_ids: set[str] = set()
    for index, raw_record in enumerate(raw_records, start=1):
        record = _approved_faq_record_from_raw(raw_record, index, seen_ids)
        if record is not None:
            records.append(record)
    return records


def approved_faq_table_rows(path: str | Path = APPROVED_FAQ_FILE) -> list[list[str]]:
    """Approved FAQ record を Gradio table 表示用の行へ変換します。"""
    return [
        [
            record.id,
            record.question,
            record.approved_answer,
            record.status or "",
        ]
        for record in load_approved_faq_records(path)
    ]


def load_approved_faq_excel_rows(path: str | Path) -> list[ApprovedFaqImportRow]:
    """QUESTION/ANSWER 列を持つ Excel から FAQ import 行を読み込みます。"""
    file_path = Path(path)
    if not file_path.exists():
        raise ValueError("Excelファイルが見つかりません。")
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on deployment packaging
        raise ValueError("Excel の読み込みには pandas / openpyxl / xlrd が必要です。") from exc

    try:
        excel_file = pd.ExcelFile(file_path)
        sheet_name = excel_file.sheet_names[0] if excel_file.sheet_names else 0
        frame = excel_file.parse(sheet_name=sheet_name, dtype=object)
    except Exception as exc:
        raise ValueError(f"Excel の読み込みに失敗しました: {exc}") from exc

    if frame.empty:
        raise ValueError("Excelファイルにデータがありません。")

    columns = {_column_key(column): column for column in frame.columns}
    missing = [name for name in ("QUESTION", "ANSWER") if name not in columns]
    if missing:
        raise ValueError("Excelには QUESTION と ANSWER 列が必要です。")

    question_column = columns["QUESTION"]
    answer_column = columns["ANSWER"]
    rows: list[ApprovedFaqImportRow] = []
    for index, row in frame.iterrows():
        question_value = "" if pd.isna(row.get(question_column)) else row.get(question_column)
        answer_value = "" if pd.isna(row.get(answer_column)) else row.get(answer_column)
        question = _clean_single_line_text(question_value, MAX_APPROVED_FAQ_QUESTION_LENGTH)
        approved_answer = _clean_multiline_text(answer_value, MAX_APPROVED_FAQ_ANSWER_LENGTH)
        if not question and not approved_answer:
            continue
        if not question or not approved_answer:
            continue
        rows.append(
            ApprovedFaqImportRow(
                question=question,
                approved_answer=approved_answer,
                source_file=file_path.name,
                sheet=str(sheet_name),
                row=int(index) + 2,
            )
        )

    if not rows:
        raise ValueError("QUESTION と ANSWER が入力された行がありません。")
    return rows


def apply_approved_faq_import_rows(
    path: str | Path,
    rows: Sequence[ApprovedFaqImportRow],
    *,
    mode: str = APPROVED_FAQ_IMPORT_MODE_INSERT,
    now: datetime | None = None,
    inherit_replaced: bool = False,
) -> ApprovedFaqMutationResult:
    """FAQ 行を insert または置換モードで保存します。

    inherit_replaced=True の置換では、同じ QUESTION の既存 record から別名質問・業務・
    出典・タグ・作成日時を引き継ぎます。Excel 取込は既定の False で全項目を置き換えます。
    """
    normalized_mode = _normalize_import_mode(mode)
    with _approved_faq_write_lock(path):
        return _apply_approved_faq_import_rows_locked(path, rows, normalized_mode, now, inherit_replaced)


def _apply_approved_faq_import_rows_locked(
    path: str | Path,
    rows: Sequence[ApprovedFaqImportRow],
    normalized_mode: str,
    now: datetime | None,
    inherit_replaced: bool,
) -> ApprovedFaqMutationResult:
    """apply_approved_faq_import_rows の本体。呼び出し元が _approved_faq_write_lock を保持していること。"""
    payload = load_approved_faq_payload(path)
    records = _raw_records(payload)
    saved_at = _utc_now(now)
    existing_ids = _record_ids(records)
    question_keys = _record_question_keys(records)
    inserted_count = 0
    deleted_count = 0
    skipped_count = 0

    for row in rows:
        question = _clean_single_line_text(row.question, MAX_APPROVED_FAQ_QUESTION_LENGTH)
        approved_answer = _clean_multiline_text(row.approved_answer, MAX_APPROVED_FAQ_ANSWER_LENGTH)
        question_key = _question_key(question)
        if not question_key or not approved_answer:
            skipped_count += 1
            continue

        replaced: dict[str, Any] | None = None
        if normalized_mode == APPROVED_FAQ_IMPORT_MODE_DELETE_THEN_INSERT:
            if inherit_replaced:
                replaced = next((item for item in records if _question_key(item.get("question")) == question_key), None)
            records, removed = _remove_records_by_question_key(records, question_key)
            deleted_count += removed
            if removed:
                existing_ids = _record_ids(records)
                question_keys = _record_question_keys(records)
        elif question_key in question_keys:
            skipped_count += 1
            continue

        normalized_row = ApprovedFaqImportRow(
            question=question,
            approved_answer=approved_answer,
            source_file=row.source_file,
            sheet=row.sheet,
            row=row.row,
            classification=_dict_or_none(row.classification),
            source=_dict_or_none(row.source),
            review=_dict_or_none(row.review),
            tags=_string_tuple(row.tags),
        )
        record = _new_approved_faq_record(normalized_row, existing_ids, saved_at)
        if replaced is not None:
            _inherit_replaced_record(record, replaced)
        records.append(record)
        inserted_count += 1
        existing_ids.add(str(record.get("id") or ""))
        question_keys.add(question_key)

    backup_path: Path | None = None
    if inserted_count or deleted_count:
        payload = dict(payload)
        payload["records"] = records
        save_result = save_approved_faq_payload(payload, path=path, now=saved_at)
        backup_path = save_result.backup_path

    return ApprovedFaqMutationResult(
        path=Path(path),
        backup_path=backup_path,
        record_count=len(records),
        inserted_count=inserted_count,
        deleted_count=deleted_count,
        skipped_count=skipped_count,
        saved_at=saved_at,
    )


def add_approved_faq_record(
    path: str | Path,
    *,
    question: str,
    approved_answer: str,
    now: datetime | None = None,
) -> ApprovedFaqMutationResult:
    """手入力された QUESTION/ANSWER を Approved FAQ に追加します。"""
    row = ApprovedFaqImportRow(
        question=question,
        approved_answer=approved_answer,
        source_file="manual",
    )
    return apply_approved_faq_import_rows(path, [row], mode=APPROVED_FAQ_IMPORT_MODE_INSERT, now=now)


def delete_approved_faq_records(
    path: str | Path,
    selectors: str | Sequence[str],
    *,
    now: datetime | None = None,
) -> ApprovedFaqMutationResult:
    """指定 ID と完全一致する Approved FAQ record を削除します。

    selectors は改行・カンマ区切りの ID、または ID の列です。前後の空白と
    重複は除去し、空入力や存在しない ID は無視します。QUESTION は照合しません。
    削除時のみ path の JSON をバックアップして保存し、件数と保存結果を返します。
    読込・保存エラーは呼び出し元へ伝播します。now は保存日時の指定に使います。
    """
    selector_values = parse_approved_faq_delete_text(selectors) if isinstance(selectors, str) else _clean_selectors(selectors)
    with _approved_faq_write_lock(path):
        return _delete_approved_faq_records_locked(path, selector_values, now)


def _delete_approved_faq_records_locked(
    path: str | Path,
    selector_values: Sequence[str],
    now: datetime | None,
) -> ApprovedFaqMutationResult:
    """delete_approved_faq_records の本体。呼び出し元が _approved_faq_write_lock を保持していること。"""
    saved_at = _utc_now(now)
    payload = load_approved_faq_payload(path)
    records = _raw_records(payload)
    if not selector_values:
        return ApprovedFaqMutationResult(
            path=Path(path),
            backup_path=None,
            record_count=len(records),
            inserted_count=0,
            deleted_count=0,
            skipped_count=0,
            saved_at=saved_at,
        )

    selector_ids = set(selector_values)
    remaining_records: list[dict[str, Any]] = []
    deleted_count = 0
    for record in records:
        record_id = str(record.get("id") or "").strip()
        if record_id in selector_ids:
            deleted_count += 1
            continue
        remaining_records.append(record)

    backup_path: Path | None = None
    if deleted_count:
        payload = dict(payload)
        payload["records"] = remaining_records
        save_result = save_approved_faq_payload(payload, path=path, now=saved_at)
        backup_path = save_result.backup_path

    return ApprovedFaqMutationResult(
        path=Path(path),
        backup_path=backup_path,
        record_count=len(remaining_records),
        inserted_count=0,
        deleted_count=deleted_count,
        skipped_count=max(0, len(selector_values) - deleted_count),
        saved_at=saved_at,
    )


@contextlib.contextmanager
def _approved_faq_write_lock(path: str | Path):
    """FAQ JSON の「読込 → 変更 → 保存」を、スレッド間・プロセス間で直列化します。

    ロックなしでは、同時操作が同じ内容を読み込み、後から保存した側が先の更新を消します。
    flock は open ごとに独立するため同一プロセスのスレッド同士も排他されます。再入は
    できないので、ロック保持中に apply/delete を呼び出さないでください。
    """
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.with_name(target_path.name + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_approved_faq_payload(
    payload: dict[str, Any],
    *,
    path: str | Path = APPROVED_FAQ_FILE,
    now: datetime | None = None,
) -> ApprovedFaqSaveResult:
    """Approved FAQ payload を backup 作成後に原子的に保存します。"""
    saved_at = _utc_now(now)
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    next_payload = dict(payload)
    next_payload["schema_version"] = APPROVED_FAQ_SCHEMA_VERSION
    next_payload["records"] = _raw_records(next_payload)

    backup_path: Path | None = None
    if target_path.exists():
        backup_dir = target_path.parent / APPROVED_FAQ_BACKUP_DIR_NAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = _unique_backup_path(target_path, backup_dir, saved_at)
        shutil.copy2(target_path, backup_path)

    # timestamp は秒精度なので、ロック外から直接呼ばれた同一秒の保存でも一時ファイルを共有しない。
    temp_path = target_path.with_name(f".{target_path.name}.{_timestamp(saved_at)}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(next_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(target_path)
    except Exception:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    return ApprovedFaqSaveResult(
        path=target_path,
        backup_path=backup_path,
        record_count=len(next_payload["records"]),
        saved_at=saved_at,
    )


def parse_approved_faq_delete_text(value: str) -> list[str]:
    """削除入力欄の改行・カンマ区切り ID を重複と空白を除いた列へ分割します。"""
    return _clean_selectors(str(value or "").replace("，", "\n").replace(",", "\n").splitlines())


def suggest_approved_faq_questions(
    question: str,
    records: Sequence[ApprovedFaqRecord] | None = None,
    *,
    classification_filter: Any = None,
    scope: Any = None,
    scope_mode: str = APPROVED_FAQ_SCOPE_MODE_PREFER,
    semantic_index: ApprovedFaqSemanticIndex | None = None,
    semantic_query_embedding: Sequence[float] | None = None,
    semantic_min_score: float = DEFAULT_APPROVED_FAQ_SEMANTIC_MIN_SCORE,
    limit: int = DEFAULT_APPROVED_FAQ_SUGGESTION_LIMIT,
    min_score: float = DEFAULT_APPROVED_FAQ_MIN_SCORE,
    statuses: Iterable[str] = (APPROVED_FAQ_APPROVED_STATUS,),
) -> list[ApprovedFaqSuggestion]:
    """質問と分類条件に合う Approved FAQ 候補を順位付けします。"""
    normalized_question = str(question or "").strip()
    if not normalized_question:
        return []

    allowed_statuses = {str(status) for status in statuses if str(status)}
    scope_metadata = _scope_metadata(scope)
    normalized_scope_mode = _normalize_scope_mode(scope_mode)
    suggestions: list[ApprovedFaqSuggestion] = []
    for record in records if records is not None else load_approved_faq_records():
        if allowed_statuses and record.status not in allowed_statuses:
            continue
        if not _classification_matches(record, classification_filter):
            continue
        matched_question, text_score = _best_question_match(normalized_question, record.match_questions)
        semantic_question, semantic_score = _best_semantic_question_match(
            record,
            semantic_index,
            semantic_query_embedding,
        )
        if semantic_score is not None and semantic_score >= semantic_min_score and semantic_score > text_score:
            matched_question = semantic_question
            score = semantic_score
            match_method = "semantic"
        else:
            score = text_score
            match_method = "text"
        if score >= min_score:
            scope_score, scope_matches = _scope_match_score(record, scope_metadata)
            suggestions.append(
                ApprovedFaqSuggestion(
                    record=record,
                    matched_question=matched_question,
                    score=score,
                    text_score=text_score,
                    semantic_score=semantic_score,
                    scope_score=scope_score,
                    scope_matches=scope_matches,
                    match_method=match_method,
                )
            )

    if normalized_scope_mode == APPROVED_FAQ_SCOPE_MODE_STRICT_WHEN_AVAILABLE and any(
        _has_source_scope_match(item) for item in suggestions
    ):
        suggestions = [item for item in suggestions if _has_source_scope_match(item)]

    suggestions.sort(key=lambda item: (-_suggestion_rank_score(item), -item.scope_score, item.record.id))
    return suggestions[: max(0, int(limit))]


def find_direct_approved_faq_answer(
    question: str,
    records: Sequence[ApprovedFaqRecord] | None = None,
    *,
    classification_filter: Any = None,
    scope: Any = None,
    scope_mode: str = APPROVED_FAQ_SCOPE_MODE_PREFER,
    semantic_index: ApprovedFaqSemanticIndex | None = None,
    semantic_query_embedding: Sequence[float] | None = None,
    semantic_min_score: float = DEFAULT_APPROVED_FAQ_SEMANTIC_MIN_SCORE,
    min_score: float = DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE,
) -> ApprovedFaqSuggestion | None:
    """十分に近い Approved FAQ がある場合に direct answer 候補を返します。"""
    suggestions = suggest_approved_faq_questions(
        question,
        records,
        classification_filter=classification_filter,
        scope=scope,
        scope_mode=scope_mode,
        semantic_index=semantic_index,
        semantic_query_embedding=semantic_query_embedding,
        semantic_min_score=semantic_min_score,
        limit=1,
        min_score=min_score,
    )
    return suggestions[0] if suggestions else None


def approved_faq_semantic_cache_path(
    output_dir: str | Path,
    *,
    faq_path: str | Path = APPROVED_FAQ_FILE,
) -> Path:
    """FAQ semantic index の保存パスを内容元 JSON から決定します。"""
    faq_digest = hashlib.sha1(str(Path(faq_path)).encode("utf-8")).hexdigest()[:12]
    return Path(output_dir) / APPROVED_FAQ_SEMANTIC_CACHE_DIR_NAME / f"{faq_digest}-{APPROVED_FAQ_SEMANTIC_CACHE_FILE_NAME}"


def load_or_build_approved_faq_semantic_index(
    cache_path: str | Path,
    records: Sequence[ApprovedFaqRecord],
    *,
    model: str,
    dimensions: int,
    embedder: Any,
    settings: Any,
    now: datetime | None = None,
) -> ApprovedFaqSemanticIndex:
    """FAQ semantic index を cache から読み、必要なら再構築します。"""
    normalized_model = str(model or "").strip()
    normalized_dimensions = max(1, int(dimensions or 1))
    records_signature = approved_faq_records_signature(records)
    cached = load_approved_faq_semantic_index(
        cache_path,
        model=normalized_model,
        dimensions=normalized_dimensions,
        records_signature=records_signature,
    )
    if cached is not None:
        return cached
    return build_approved_faq_semantic_index(
        cache_path,
        records,
        model=normalized_model,
        dimensions=normalized_dimensions,
        records_signature=records_signature,
        embedder=embedder,
        settings=settings,
        now=now,
    )


def load_approved_faq_semantic_index(
    cache_path: str | Path,
    *,
    model: str,
    dimensions: int,
    records_signature: str,
) -> ApprovedFaqSemanticIndex | None:
    """署名と model が一致する FAQ semantic index cache を読み込みます。"""
    path = Path(cache_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("schema_version") or 0) != APPROVED_FAQ_SEMANTIC_CACHE_SCHEMA_VERSION:
        return None
    if str(payload.get("model") or "") != str(model or ""):
        return None
    if int(payload.get("dimensions") or 0) != int(dimensions or 0):
        return None
    if str(payload.get("records_signature") or "") != str(records_signature or ""):
        return None

    items: list[ApprovedFaqSemanticItem] = []
    for raw_item in payload.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        record_id = str(raw_item.get("record_id") or "").strip()
        question = str(raw_item.get("question") or "").strip()
        question_hash = str(raw_item.get("question_hash") or "").strip()
        embedding = _embedding_tuple(raw_item.get("embedding"), dimensions)
        if record_id and question and question_hash and embedding is not None:
            items.append(
                ApprovedFaqSemanticItem(
                    record_id=record_id,
                    question=question,
                    question_hash=question_hash,
                    embedding=embedding,
                )
            )
    return ApprovedFaqSemanticIndex(
        model=str(model or ""),
        dimensions=int(dimensions or 0),
        records_signature=str(records_signature or ""),
        items=tuple(items),
    )


def build_approved_faq_semantic_index(
    cache_path: str | Path,
    records: Sequence[ApprovedFaqRecord],
    *,
    model: str,
    dimensions: int,
    embedder: Any,
    settings: Any,
    records_signature: str | None = None,
    now: datetime | None = None,
) -> ApprovedFaqSemanticIndex:
    """FAQ question と alternate question の embedding index を作成します。"""
    normalized_dimensions = max(1, int(dimensions or 1))
    question_items = _approved_faq_semantic_questions(records)
    questions = [question for _, question in question_items]
    embeddings = embedder(questions, settings) if questions else []
    items: list[ApprovedFaqSemanticItem] = []
    for (record_id, question), embedding in zip(question_items, embeddings, strict=False):
        embedding_tuple = _embedding_tuple(embedding, normalized_dimensions)
        if embedding_tuple is None:
            continue
        items.append(
            ApprovedFaqSemanticItem(
                record_id=record_id,
                question=question,
                question_hash=_text_hash(question),
                embedding=embedding_tuple,
            )
        )
    index = ApprovedFaqSemanticIndex(
        model=str(model or ""),
        dimensions=normalized_dimensions,
        records_signature=records_signature or approved_faq_records_signature(records),
        items=tuple(items),
    )
    save_approved_faq_semantic_index(cache_path, index, now=now)
    return index


def save_approved_faq_semantic_index(
    cache_path: str | Path,
    index: ApprovedFaqSemanticIndex,
    *,
    now: datetime | None = None,
) -> None:
    """FAQ semantic index を timestamp 付き payload として保存します。"""
    saved_at = _utc_now(now)
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": APPROVED_FAQ_SEMANTIC_CACHE_SCHEMA_VERSION,
        "model": index.model,
        "dimensions": index.dimensions,
        "records_signature": index.records_signature,
        "created_at": saved_at.isoformat(),
        "items": [
            {
                "record_id": item.record_id,
                "question": item.question,
                "question_hash": item.question_hash,
                "embedding": list(item.embedding),
            }
            for item in index.items
        ],
    }
    # save_approved_faq_payload と同じく一時ファイル → replace で原子的に書く。書込中の中断で壊れた cache が残ると、
    # 読込側は None 扱いで再構築するため embedding の再計算（外部 API 呼出）が走る (#828)。
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def approved_faq_records_signature(records: Sequence[ApprovedFaqRecord]) -> str:
    """FAQ record 集合の内容変更検知に使う署名を作ります。"""
    payload = [
        {
            "id": record.id,
            "match_questions": list(record.match_questions),
            "status": record.status,
            "business": record.business,
            "classification": record.classification or {},
            "source": record.source or {},
        }
        for record in records
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def approved_faq_feedback_skip_reason(record: dict[str, Any]) -> str:
    """回答 feedback を FAQ へ昇格しない理由を返します。昇格できる場合は空文字です。"""
    answer_trace = _dict_or_none(record.get("answer_trace")) or {}
    question = _clean_single_line_text(
        record.get("question") or answer_trace.get("question"),
        MAX_APPROVED_FAQ_QUESTION_LENGTH,
    )
    if str(record.get("feedback_type") or "").strip() != "correct":
        has_answer = _clean_multiline_text(record.get("corrected_answer"), MAX_APPROVED_FAQ_ANSWER_LENGTH)
        return "" if question and has_answer else "FAQへ反映する回答がありません。"
    if str(answer_trace.get("retrieval_scope") or "").strip() == APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE:
        # 回答は既存 FAQ そのものなので、作り直すと出典が FAQ 回答の保存先に置き換わるだけになる。
        return "Approved FAQ の回答のため、再登録は不要です。"
    # ponytail: 「回答できない」の判定は insufficient_reason と confidence=low の組で近似する。
    # 一部だけ不足する回答も insufficient_reason を持つため、これだけでは除外しない。
    if str(answer_trace.get("insufficient_reason") or "").strip() and str(answer_trace.get("confidence") or "").strip() == "low":
        return "根拠不足の回答は FAQ 化しません。登録する内容がある場合は修正回答を入力してください。"
    if not question or not _clean_multiline_text(answer_trace.get("answer_text"), MAX_APPROVED_FAQ_ANSWER_LENGTH):
        return "FAQへ反映する回答がありません。"
    return ""


def approved_faq_import_row_from_answer_feedback(record: dict[str, Any]) -> ApprovedFaqImportRow | None:
    """回答 feedback を FAQ 行へ変換し、正しい評価では残存修正値より保存済み回答を優先します。

    approved_faq_feedback_skip_reason が理由を返す feedback は None を返します。
    """
    if approved_faq_feedback_skip_reason(record):
        return None
    answer_trace = _dict_or_none(record.get("answer_trace")) or {}
    feedback_type = str(record.get("feedback_type") or "").strip()
    question = _clean_single_line_text(
        record.get("question") or answer_trace.get("question"),
        MAX_APPROVED_FAQ_QUESTION_LENGTH,
    )
    corrected_answer = _clean_multiline_text(record.get("corrected_answer"), MAX_APPROVED_FAQ_ANSWER_LENGTH)
    traced_answer = _clean_multiline_text(answer_trace.get("answer_text"), MAX_APPROVED_FAQ_ANSWER_LENGTH)

    approved_answer = traced_answer if feedback_type == "correct" else corrected_answer

    feedback_id = str(record.get("feedback_id") or "").strip()
    answer_run_id = str(record.get("run_id") or "").strip()
    # feedback の run_id は回答の保存先で、Knowledge Base 検索では UI で開いていたファイルを指す。
    # _scope_match_score は run_id を出典として照合するため、そのまま入れると別ファイルの FAQ になる。
    # FAQ 直接回答の run_id は approved-faq-* の保存先で文書ではないため、同様に出典へ入れない。
    answer_scope = str(answer_trace.get("retrieval_scope") or "").strip()
    from_knowledge_base = (
        answer_scope == APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE or normalize_retrieval_scope(answer_scope) == RETRIEVAL_SCOPE_KNOWLEDGE_BASE
    )
    source = _compact_dict(
        {
            "source_file": APPROVED_FAQ_FEEDBACK_SOURCE_FILE,
            "feedback_id": feedback_id,
            "feedback_type": feedback_type,
            "run_id": "" if from_knowledge_base else answer_run_id,
            "answer_run_id": answer_run_id if from_knowledge_base else "",
            "source_run_id": str(answer_trace.get("primary_source_run_id") or "").strip() if from_knowledge_base else "",
            "answer_id": str(record.get("answer_id") or "").strip(),
            "answer_payload_path": str(answer_trace.get("answer_payload_path") or "").strip(),
            "evidence_item_ids": _string_list(answer_trace.get("evidence_item_ids")),
        }
    )
    review_notes = "Promoted from ナレッジ還流 feedback."
    comment = _clean_multiline_text(record.get("comment"), 2000)
    if comment:
        review_notes = f"{review_notes}\n{comment}"
    review = _compact_dict(
        {
            "review_notes": review_notes,
            "confidence": str(answer_trace.get("confidence") or "").strip(),
        }
    )
    tags = _string_tuple(("knowledge_return", "answer_feedback", feedback_type))
    return ApprovedFaqImportRow(
        question=question,
        approved_answer=approved_answer,
        source_file=APPROVED_FAQ_FEEDBACK_SOURCE_FILE,
        classification=_classification_from_feedback_record(record, answer_trace),
        source=source,
        review=review,
        tags=tags,
    )


def promote_answer_feedback_to_approved_faq(
    path: str | Path,
    record: dict[str, Any],
    *,
    now: datetime | None = None,
) -> ApprovedFaqMutationResult | None:
    """回答 feedback を Approved FAQ に昇格します。昇格対象でなければ None を返します。

    同じ QUESTION の既存 record は置き換えますが、別名質問や出典などは引き継ぎます。
    """
    row = approved_faq_import_row_from_answer_feedback(record)
    if row is None:
        return None
    return apply_approved_faq_import_rows(
        path,
        [row],
        mode=APPROVED_FAQ_IMPORT_MODE_DELETE_THEN_INSERT,
        now=now,
        inherit_replaced=True,
    )


def approved_faq_suggestion_choices(suggestions: Sequence[ApprovedFaqSuggestion]) -> list[list[str]]:
    """FAQ 候補を Gradio choice value へ変換します。"""
    return [[suggestion.record.id] for suggestion in suggestions]


def approved_faq_suggestion_labels(suggestions: Sequence[ApprovedFaqSuggestion]) -> list[str]:
    """FAQ 候補を Gradio 表示ラベルへ変換します。"""
    labels: list[str] = []
    for suggestion in suggestions:
        suffix = f" / {suggestion.record.business}" if suggestion.record.business else ""
        labels.append(f"{suggestion.record.question}{suffix}")
    return labels


def select_approved_faq_suggestion(
    suggestion_value: Any,
    records: Sequence[ApprovedFaqRecord] | None = None,
) -> tuple[str, str]:
    """選択された FAQ 候補 value から record を復元します。"""
    selected_id = _dataset_selected_id(suggestion_value)
    if not selected_id:
        return "", ""
    for record in records if records is not None else load_approved_faq_records():
        if record.id == selected_id:
            return record.question, format_approved_faq_reference(record)
    return "", ""


def format_approved_faq_reference(record: ApprovedFaqRecord) -> str:
    """Approved FAQ の出典 metadata を表示用に整形します。"""
    lines = [record.approved_answer.strip()]
    metadata = [
        f"ID: {record.id}",
        f"Status: {record.status or '-'}",
    ]
    source = record.source or {}
    file_name = str(source.get("file_name") or "").strip()
    page = str(source.get("page") or "").strip()
    if file_name:
        metadata.append(f"Source: {file_name}" + (f" p.{page}" if page else ""))
    source_uri = str(source.get("source_uri") or "").strip()
    if source_uri:
        metadata.append(f"URI: {source_uri}")
    source_file = str(source.get("source_file") or "").strip()
    if source_file:
        sheet = str(source.get("sheet") or "").strip()
        row = str(source.get("row") or "").strip()
        import_source = f"Import: {source_file}"
        if sheet:
            import_source += f" / sheet: {sheet}"
        if row:
            import_source += f" / row: {row}"
        metadata.append(import_source)
    updated_at = str((record.timestamps or {}).get("updated_at") or "").strip()
    if updated_at:
        metadata.append(f"Updated: {updated_at}")
    lines.extend(["", "\n".join(metadata)])
    return "\n".join(line for line in lines if line is not None).strip()


def _approved_faq_record_from_raw(
    raw_record: Any,
    index: int,
    seen_ids: set[str],
) -> ApprovedFaqRecord | None:
    if not isinstance(raw_record, dict):
        return None
    question = str(raw_record.get("question") or "").strip()
    approved_answer = str(raw_record.get("approved_answer") or "").strip()
    if not question or not approved_answer:
        return None

    record_id = str(raw_record.get("id") or f"approved-faq-{index}").strip() or f"approved-faq-{index}"
    if record_id in seen_ids:
        record_id = f"{record_id}-{index}"
    seen_ids.add(record_id)
    return ApprovedFaqRecord(
        id=record_id,
        question=question,
        approved_answer=approved_answer,
        alternate_questions=tuple(str(item).strip() for item in raw_record.get("alternate_questions") or [] if str(item).strip()),
        status=str(raw_record.get("status") or "").strip(),
        business=str(raw_record.get("business") or "").strip(),
        classification=_dict_or_none(raw_record.get("classification")),
        source=_dict_or_none(raw_record.get("source")),
        review=_dict_or_none(raw_record.get("review")),
        timestamps=_dict_or_none(raw_record.get("timestamps")),
        tags=tuple(str(item).strip() for item in raw_record.get("tags") or [] if str(item).strip()),
    )


def _best_question_match(question: str, candidates: Sequence[str]) -> tuple[str, float]:
    best_question = ""
    best_score = 0.0
    for candidate in candidates:
        score = _similarity_score(question, candidate)
        if score > best_score:
            best_question = candidate
            best_score = score
    return best_question, best_score


# 包含でも「実質同じ質問」とみなす、短い側 / 長い側の長さの比の下限。
_CONTAINMENT_SAME_QUESTION_MIN_RATIO = 0.8


def _similarity_score(left: str, right: str) -> float:
    # 末尾の句読点だけの違い（「。」と「？」）は同じ質問として扱う。
    left_key = _compact_text(left).rstrip("。.?？!！")
    right_key = _compact_text(right).rstrip("。.?？!！")
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    if left_key in right_key or right_key in left_key:
        # 一律 0.92 にすると直答の閾値（0.92）と同値になり、「エラー」のような短い語や、FAQ の質問に条件を
        # 足した質問まで高閾値一致になる。長さが近いときだけ 0.92 とし、それ以外は入力中の候補提示向けに割り引く。
        ratio = min(len(left_key), len(right_key)) / max(len(left_key), len(right_key))
        return 0.92 if ratio >= _CONTAINMENT_SAME_QUESTION_MIN_RATIO else 0.5 + 0.4 * ratio

    left_units = _search_units(left)
    right_units = _search_units(right)
    if not left_units or not right_units:
        return 0.0
    overlap = len(left_units & right_units)
    union = len(left_units | right_units)
    jaccard = overlap / union if union else 0.0
    containment = overlap / len(left_units)
    return max(jaccard, containment * 0.75)


def _search_units(value: str) -> set[str]:
    normalized = _normalize_text(value)
    compact = re.sub(r"\s+", "", normalized)
    units = {token for token in re.findall(r"[a-z0-9_.-]+", normalized) if token}
    if compact:
        units.add(compact)
    for width in (2, 3):
        if len(compact) >= width:
            units.update(compact[index : index + width] for index in range(len(compact) - width + 1))
    return units


def _classification_matches(record: ApprovedFaqRecord, classification_filter: Any) -> bool:
    filter_metadata = _filter_metadata(classification_filter)
    if not filter_metadata:
        return True
    record_classification = record.classification or {}
    pairs = (
        ("large_category", "major"),
        ("middle_category", "middle"),
        ("small_category", "minor"),
    )
    if not any(
        str(record_classification.get(filter_key) or record_classification.get(record_key) or "").strip()
        for filter_key, record_key in pairs
    ):
        return True
    for filter_key, record_key in pairs:
        expected = str(filter_metadata.get(filter_key) or "").strip()
        if not expected:
            continue
        actual = str(record_classification.get(filter_key) or record_classification.get(record_key) or "").strip()
        if actual != expected:
            return False
    return True


def _filter_metadata(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_metadata"):
        value = value.to_metadata()
    return dict(value) if isinstance(value, dict) else {}


def _approved_faq_semantic_questions(records: Sequence[ApprovedFaqRecord]) -> list[tuple[str, str]]:
    questions: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        for question in record.match_questions:
            normalized_question = str(question or "").strip()
            key = (record.id, _text_hash(normalized_question))
            if not normalized_question or key in seen:
                continue
            questions.append((record.id, normalized_question))
            seen.add(key)
    return questions


def _best_semantic_question_match(
    record: ApprovedFaqRecord,
    semantic_index: ApprovedFaqSemanticIndex | None,
    semantic_query_embedding: Sequence[float] | None,
) -> tuple[str, float | None]:
    if semantic_index is None or semantic_query_embedding is None:
        return "", None
    query_embedding = _embedding_tuple(semantic_query_embedding, semantic_index.dimensions)
    if query_embedding is None:
        return "", None

    question_hashes = {_text_hash(question) for question in record.match_questions}
    best_question = ""
    best_score: float | None = None
    for item in semantic_index.items:
        if item.record_id != record.id or item.question_hash not in question_hashes:
            continue
        score = _cosine_similarity(query_embedding, item.embedding)
        if best_score is None or score > best_score:
            best_question = item.question
            best_score = score
    return best_question, best_score


def _embedding_tuple(value: Any, dimensions: int) -> tuple[float, ...] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    try:
        embedding = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if int(dimensions or 0) > 0 and len(embedding) != int(dimensions):
        return None
    if not embedding or not all(math.isfinite(item) for item in embedding):
        return None
    return embedding


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_norm = math.sqrt(sum(item * item for item in left))
    right_norm = math.sqrt(sum(item * item for item in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    score = sum(left_item * right_item for left_item, right_item in zip(left, right, strict=False)) / (left_norm * right_norm)
    return max(-1.0, min(1.0, score))


def _scope_metadata(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_metadata"):
        value = value.to_metadata()
    if not isinstance(value, dict):
        return {}
    metadata = dict(value)
    classification = metadata.get("classification")
    if hasattr(classification, "to_metadata"):
        metadata["classification"] = classification.to_metadata()
    return {key: item for key, item in metadata.items() if item not in ("", [], {}, None)}


def _normalize_scope_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in APPROVED_FAQ_SCOPE_MODES else APPROVED_FAQ_SCOPE_MODE_PREFER


def _scope_match_score(record: ApprovedFaqRecord, scope: dict[str, Any]) -> tuple[float, tuple[str, ...]]:
    if not scope:
        return 0.0, ()
    source = record.source or {}
    score = 0.0
    matches: list[str] = []

    scope_run_id = _normalized_scope_text(scope.get("source_run_id") or scope.get("run_id"))
    if scope_run_id and any(_same_scope_text(source.get(key), scope_run_id) for key in ("source_run_id", "run_id", "analysis_run_id")):
        score += 1.0
        matches.append("source_run_id")

    scope_sha = _normalized_scope_text(scope.get("source_file_sha256") or scope.get("file_sha256"))
    if scope_sha and any(_same_scope_text(source.get(key), scope_sha) for key in ("source_file_sha256", "file_sha256", "sha256")):
        score += 0.9
        matches.append("source_file_sha256")

    scope_file_names = _source_file_values(scope)
    record_file_names = _source_file_values(source)
    if scope_file_names and record_file_names and scope_file_names & record_file_names:
        score += 0.7
        matches.append("source_file_name")

    scope_business = _normalized_scope_text(scope.get("business"))
    record_business = _normalized_scope_text(record.business)
    if scope_business and record_business and scope_business == record_business:
        score += 0.25
        matches.append("business")

    scope_classification = scope.get("classification") if isinstance(scope.get("classification"), dict) else {}
    record_classification = record.classification or {}
    for key, aliases in {
        "major": ("major", "large_category"),
        "middle": ("middle", "middle_category"),
        "minor": ("minor", "small_category"),
    }.items():
        expected = next(
            (_normalized_scope_text(scope_classification.get(alias)) for alias in aliases if scope_classification.get(alias)),
            "",
        )
        actual = next(
            (_normalized_scope_text(record_classification.get(alias)) for alias in aliases if record_classification.get(alias)),
            "",
        )
        if expected and actual and expected == actual:
            score += 0.1
            matches.append(f"classification_{key}")

    return score, tuple(dict.fromkeys(matches))


def _source_file_values(metadata: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("source_file_name", "file_name", "pdf_name"):
        value = _normalized_file_name(metadata.get(key))
        if value:
            values.add(value)
    source_file = _normalized_file_name(metadata.get("source_file"))
    if source_file and source_file != APPROVED_FAQ_FEEDBACK_SOURCE_FILE:
        values.add(source_file)
    return values


def _normalized_file_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _normalized_scope_text(Path(text).name)


def _same_scope_text(left: Any, right: Any) -> bool:
    left_text = _normalized_scope_text(left)
    right_text = _normalized_scope_text(right)
    return bool(left_text and right_text and left_text == right_text)


def _normalized_scope_text(value: Any) -> str:
    return _normalize_text(str(value or "")).casefold()


def _suggestion_rank_score(suggestion: ApprovedFaqSuggestion) -> float:
    return suggestion.score + min(0.06, max(0.0, suggestion.scope_score) * 0.06)


def _has_source_scope_match(suggestion: ApprovedFaqSuggestion) -> bool:
    return bool(
        set(suggestion.scope_matches)
        & {
            "source_run_id",
            "source_file_sha256",
            "source_file_name",
        }
    )


def _text_hash(value: Any) -> str:
    return hashlib.sha256(_compact_text(str(value or "")).encode("utf-8")).hexdigest()


def _dataset_selected_id(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, (list, tuple)):
            return _dataset_selected_id(first)
        return str(first or "").strip()
    return str(value or "").strip()


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"\s+", " ", text).strip()


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", _normalize_text(value))


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _default_approved_faq_payload() -> dict[str, Any]:
    return {
        "schema_version": APPROVED_FAQ_SCHEMA_VERSION,
        "name": "approved_faq_golden_qa",
        "description": "Production-approved FAQ and golden QA corpus.",
        "usage_policy": {},
        "record_schema": {},
        "records": [],
    }


def _raw_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        return []
    return [dict(record) for record in raw_records if isinstance(record, dict)]


def _record_ids(records: Sequence[dict[str, Any]]) -> set[str]:
    return {str(record.get("id") or "").strip() for record in records if str(record.get("id") or "").strip()}


def _record_question_keys(records: Sequence[dict[str, Any]]) -> set[str]:
    return {_question_key(record.get("question")) for record in records if _question_key(record.get("question"))}


def _remove_records_by_question_key(
    records: Sequence[dict[str, Any]],
    question_key: str,
) -> tuple[list[dict[str, Any]], int]:
    remaining = [record for record in records if _question_key(record.get("question")) != question_key]
    return remaining, len(records) - len(remaining)


def _new_approved_faq_record(
    row: ApprovedFaqImportRow,
    existing_ids: set[str],
    saved_at: datetime,
) -> dict[str, Any]:
    record_id = _unique_approved_faq_id(row.question, existing_ids)
    timestamp = saved_at.isoformat()
    source: dict[str, Any] = dict(row.source or {})
    if row.source_file and "source_file" not in source:
        source["source_file"] = row.source_file
    if row.sheet and "sheet" not in source:
        source["sheet"] = row.sheet
    if row.row and "row" not in source:
        source["row"] = row.row
    review = {
        "owner": "",
        "reviewer": "",
        "review_notes": "Added from Approved FAQ management UI.",
        "confidence": "",
    }
    review.update(row.review or {})
    return {
        "id": record_id,
        "question": row.question,
        "alternate_questions": [],
        "approved_answer": row.approved_answer,
        "status": APPROVED_FAQ_APPROVED_STATUS,
        "business": "",
        "classification": _approved_faq_classification(row.classification),
        "source": source,
        "review": review,
        "timestamps": {
            "created_at": timestamp,
            "updated_at": timestamp,
            "reviewed_at": timestamp,
            "next_review_at": "",
        },
        "tags": list(_string_tuple(row.tags)),
    }


def _inherit_replaced_record(record: dict[str, Any], replaced: dict[str, Any]) -> None:
    """feedback で置き換える record へ、回答以外のキュレーション済み項目を引き継ぎます（record を変更）。

    引き継がないと、別名質問での一致と出典ファイルへの scope 一致が feedback のたびに失われます。
    """
    record["alternate_questions"] = _string_list(replaced.get("alternate_questions"))
    record["business"] = str(replaced.get("business") or "").strip()
    if not any(record["classification"].get(key) for key in ("major", "middle", "minor")):
        record["classification"] = _approved_faq_classification(replaced.get("classification"))
    record["source"] = {**(_dict_or_none(replaced.get("source")) or {}), **record["source"]}
    # 旧 feedback 種別のタグは今回の回答に当てはまらないため残さない。
    kept_tags = [tag for tag in _string_list(replaced.get("tags")) if tag not in ANSWER_FEEDBACK_TYPE_VALUES]
    record["tags"] = list(_string_tuple([*kept_tags, *record["tags"]]))
    created_at = str((_dict_or_none(replaced.get("timestamps")) or {}).get("created_at") or "").strip()
    if created_at:
        record["timestamps"]["created_at"] = created_at


def _unique_approved_faq_id(question: str, existing_ids: set[str]) -> str:
    digest = hashlib.sha1(_question_key(question).encode("utf-8")).hexdigest()[:12]
    base_id = f"approved-faq-{digest}"
    if base_id not in existing_ids:
        return base_id
    for index in range(2, 1000):
        candidate = f"{base_id}-{index}"
        if candidate not in existing_ids:
            return candidate
    raise RuntimeError("Approved FAQ ID を採番できませんでした。")


def _normalize_import_mode(mode: str) -> str:
    normalized = str(mode or "").strip().upper()
    if normalized not in APPROVED_FAQ_IMPORT_MODES:
        raise ValueError("取込モードは INSERT または DELETE_THEN_INSERT を選択してください。")
    return normalized


def _clean_selectors(values: Iterable[Any]) -> list[str]:
    selectors: list[str] = []
    seen: set[str] = set()
    for value in values:
        selector = _clean_single_line_text(value, MAX_APPROVED_FAQ_QUESTION_LENGTH)
        key = selector.casefold()
        if not selector or key in seen:
            continue
        selectors.append(selector)
        seen.add(key)
    return selectors


def _approved_faq_classification(value: Any) -> dict[str, Any]:
    metadata = dict(value) if isinstance(value, dict) else {}
    metadata["major"] = str(metadata.get("major") or metadata.get("large_category") or "").strip()
    metadata["middle"] = str(metadata.get("middle") or metadata.get("middle_category") or "").strip()
    metadata["minor"] = str(metadata.get("minor") or metadata.get("small_category") or "").strip()
    return metadata


def _classification_from_feedback_record(
    record: dict[str, Any],
    answer_trace: dict[str, Any],
) -> dict[str, Any]:
    classification = record.get("classification_filter")
    if not isinstance(classification, dict) or not classification:
        classification = answer_trace.get("classification_filter")
    return _approved_faq_classification(classification)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _string_tuple(values: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        out.append(text)
        seen.add(key)
    return tuple(out)


def _compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if item in ("", [], {}, None):
            continue
        compact[key] = item
    return compact


def _clean_single_line_text(value: Any, max_length: int) -> str:
    text = _clean_multiline_text(value, max_length)
    return re.sub(r"\s+", " ", text).strip()


def _clean_multiline_text(value: Any, max_length: int) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if len(normalized) > max_length:
        normalized = normalized[:max_length].rstrip()
    return normalized


def _question_key(value: Any) -> str:
    return _compact_text(_clean_single_line_text(value, MAX_APPROVED_FAQ_QUESTION_LENGTH))


def _column_key(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).upper()


def _unique_backup_path(path: Path, backup_dir: Path, saved_at: datetime) -> Path:
    timestamp = _timestamp(saved_at)
    candidate = backup_dir / f"{path.stem}.{timestamp}{path.suffix}.bak"
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = backup_dir / f"{path.stem}.{timestamp}.{index}{path.suffix}.bak"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate backup path for {path}")


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")
