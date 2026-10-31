"""文書分類の推定、手入力値の正規化、検索フィルター化を扱う。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rag_parser_core.language.markers import marker_patterns

CLASSIFICATION_SCHEMA_VERSION = 1
DEFAULT_CATEGORY_VALUE = ""
LEGACY_UNCATEGORIZED_VALUE = "未分類"


@dataclass(frozen=True)
class DocumentClassification:
    """文書の大分類・中分類・小分類と推定元を保持します。"""

    large_category: str = DEFAULT_CATEGORY_VALUE
    middle_category: str = DEFAULT_CATEGORY_VALUE
    small_category: str = DEFAULT_CATEGORY_VALUE
    source: str = "default"
    path_parts: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        """検索・保存で共有する metadata dict へ変換します。"""
        return {
            "schema_version": CLASSIFICATION_SCHEMA_VERSION,
            "large_category": self.large_category or DEFAULT_CATEGORY_VALUE,
            "middle_category": self.middle_category or DEFAULT_CATEGORY_VALUE,
            "small_category": self.small_category or DEFAULT_CATEGORY_VALUE,
            "source": self.source or "default",
            "path_parts": list(self.path_parts),
        }


@dataclass(frozen=True)
class ClassificationFilter:
    """ADB/FAQ 検索で使う分類フィルターと、有効期間の基準日を保持します。

    as_of は YYYY-MM-DD または空（検索時に今日として扱う）。分類とは独立した条件だが、
    ponytail: 検索経路の全呼び出しが既にこの型を運ぶため、新しい引数を通す代わりにここへ載せる。
    分類以外の条件が増えたら RetrievalFilter へ分離する。
    """

    large_category: str = ""
    middle_category: str = ""
    small_category: str = ""
    as_of: str = ""

    @property
    def active(self) -> bool:
        """検索や prompt へ反映する有効条件かどうかを返します。"""
        return bool(self.large_category or self.middle_category or self.small_category)

    def to_metadata(self) -> dict[str, str]:
        """検索・保存で共有する metadata dict へ変換します。"""
        return {
            key: value
            for key, value in {
                "large_category": self.large_category,
                "middle_category": self.middle_category,
                "small_category": self.small_category,
            }.items()
            if value
        }

    def to_request_filters(self) -> dict[str, str]:
        """HTTP API / SDK の `filters` dict へ変換します。

        `to_metadata()` は保存 payload と SQL の分類 bind に共有されるため分類だけを返す。
        こちらは検索要求専用で、基準日 as_of も含める（#976）。
        """
        filters = self.to_metadata()
        if self.as_of:
            filters["as_of"] = self.as_of
        return filters


# HTTP API / SDK の `filters` で受け付けるキー。classification_filter_from_values の引数名と一致させる。
REQUEST_FILTER_KEYS = frozenset({"large_category", "middle_category", "small_category", "as_of"})


@dataclass(frozen=True)
class ClassificationOptions:
    """UI の分類 selector に提示する候補群を保持します。"""

    large_categories: tuple[str, ...]
    middle_categories: tuple[str, ...]
    small_categories: tuple[str, ...]
    middle_categories_by_large: dict[str, tuple[str, ...]] = field(default_factory=dict)


def infer_classification_from_path(
    path: str | Path,
    *,
    source_root: str | Path | None = None,
) -> DocumentClassification:
    """share 配下のパス構造から文書分類を推定します。"""
    source_path = Path(path)
    path_parts = _classification_path_parts(source_path, source_root=source_root)
    large = middle = ""
    small = _small_category_from_path(source_path, path_parts)
    source = "path_default" if large or middle or small else "default"
    return DocumentClassification(
        large_category=large or DEFAULT_CATEGORY_VALUE,
        middle_category=middle or DEFAULT_CATEGORY_VALUE,
        small_category=small or DEFAULT_CATEGORY_VALUE,
        source=source,
        path_parts=tuple(path_parts),
    )


def classification_from_selection(
    *,
    source_path: str | Path | None = None,
    source_root: str | Path | None = None,
    large_category: Any = "",
    middle_category: Any = "",
    small_category: Any = "",
) -> DocumentClassification:
    """UI 手入力値と path 推定値を統合し、任意の中・小分類を保持します。"""
    inferred = infer_classification_from_path(source_path, source_root=source_root) if source_path else DocumentClassification()
    manual_large = _large_category_value(large_category)
    manual_middle = _middle_category_value(middle_category)
    manual_small = _small_category_value(small_category)
    has_parent_manual = bool(manual_large or manual_middle)
    has_small_manual = bool(manual_small)
    if has_parent_manual:
        large = manual_large or inferred.large_category
        middle = manual_middle
        small = manual_small
    elif has_small_manual:
        large = inferred.large_category
        middle = inferred.middle_category
        small = manual_small
    else:
        large = inferred.large_category
        middle = inferred.middle_category
        small = inferred.small_category
    return DocumentClassification(
        large_category=large or DEFAULT_CATEGORY_VALUE,
        middle_category=middle or DEFAULT_CATEGORY_VALUE,
        small_category=small or DEFAULT_CATEGORY_VALUE,
        source="manual" if has_parent_manual or has_small_manual else inferred.source,
        path_parts=inferred.path_parts,
    )


def as_of_value(value: Any) -> str:
    """基準日入力を YYYY-MM-DD に正規化する。空は空のまま返し、形式不正は ValueError。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("基準日は YYYY-MM-DD で指定してください。")
    date.fromisoformat(text)
    return text


def classification_filter_from_values(
    *,
    large_category: Any = "",
    middle_category: Any = "",
    small_category: Any = "",
    as_of: Any = "",
) -> ClassificationFilter:
    """UI / API の任意分類 filter と基準日入力を検索用 ClassificationFilter に変換します。"""
    large = _large_category_value(large_category)
    middle = _middle_category_value(middle_category)
    return ClassificationFilter(
        large_category=large,
        middle_category=middle,
        small_category=_small_category_value(small_category),
        as_of=as_of_value(as_of),
    )


def business_today(timezone: str, *, now: datetime | None = None) -> date:
    """設定した時間帯の今日(検索の基準日の既定。#3249)。OS の時間帯に左右されない。

    不明な時間帯の名前は既定(``DEFAULT_TIMEZONE``)にする。``now`` の時間帯の無い値は UTC とみなす。
    """
    from rag_engine.config import DEFAULT_TIMEZONE

    try:
        zone = ZoneInfo(timezone.strip() or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone).date()


def with_effective_as_of(classification_filter: ClassificationFilter | None, today: date) -> ClassificationFilter:
    """基準日の無い条件に今日を入れる(#3249)。明示した基準日は変えない。

    1 回の検索の入口で 1 回だけ決め、下流(vector・Oracle Text・chunk の読み取り)は同じ日で有効期間を判定する。
    """
    current = classification_filter or ClassificationFilter()
    return current if current.as_of else replace(current, as_of=today.isoformat())


def classification_filter_from_metadata(value: Any) -> ClassificationFilter:
    """保存済み metadata から分類 filter を復元します。"""
    if isinstance(value, ClassificationFilter):
        return value
    if not isinstance(value, dict):
        return ClassificationFilter()
    return classification_filter_from_values(
        large_category=value.get("large_category"),
        middle_category=value.get("middle_category"),
        small_category=value.get("small_category"),
    )


def classification_from_metadata(value: Any) -> DocumentClassification:
    """保存済み metadata から DocumentClassification を復元します。"""
    if isinstance(value, DocumentClassification):
        value = value.to_metadata()
    if not isinstance(value, dict):
        return DocumentClassification()
    large = _large_category_value(value.get("large_category"))
    middle = _middle_category_value(value.get("middle_category"))
    return DocumentClassification(
        large_category=large or DEFAULT_CATEGORY_VALUE,
        middle_category=middle or DEFAULT_CATEGORY_VALUE,
        small_category=_small_category_value(value.get("small_category")) or DEFAULT_CATEGORY_VALUE,
        source=str(value.get("source") or "default").strip() or "default",
        path_parts=tuple(str(part) for part in value.get("path_parts") or [] if str(part)),
    )


def classification_options_from_share(
    source_root: str | Path,
    *,
    supported_suffixes: Iterable[str] = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"),
) -> ClassificationOptions:
    """share 配下のファイル構成から UI 分類候補を収集します。"""
    root = Path(source_root)
    suffixes = {str(suffix).lower() for suffix in supported_suffixes}
    large: set[str] = set()
    middle: set[str] = set()
    middle_by_large: dict[str, set[str]] = {}
    small: set[str] = set()
    if root.exists():
        try:
            paths = sorted(root.rglob("*"), key=lambda item: item.as_posix())
        except OSError:
            paths = []
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            classification = infer_classification_from_path(path, source_root=root)
            if classification.large_category:
                large.add(classification.large_category)
            if classification.middle_category:
                middle.add(classification.middle_category)
                if classification.large_category:
                    middle_by_large.setdefault(classification.large_category, set()).add(classification.middle_category)
            if classification.small_category:
                small.add(classification.small_category)
    return ClassificationOptions(
        large_categories=_sorted_categories(large),
        middle_categories=_sorted_categories(middle),
        small_categories=_sorted_categories(small),
        middle_categories_by_large={category: _sorted_categories(values) for category, values in middle_by_large.items() if category},
    )


def small_categories_from_saved_metadata(
    output_dir: str | Path,
    *,
    max_files: int = 2000,
) -> tuple[str, ...]:
    """保存済み解析結果から既知の小分類候補を収集します。"""
    root = Path(output_dir)
    if not root.exists():
        return ()
    small: set[str] = set()
    paths = _saved_classification_metadata_paths(root)
    for path in paths[: max(0, int(max_files))]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        _collect_small_categories(payload, small)
    return _sorted_categories(small)


def normalize_classification_payload(value: Any) -> int:
    """旧形式を含む分類 payload を現行 schema へ正規化します。"""
    return _normalize_classification_payload(value)


def merge_classification_metadata(
    metadata: dict[str, Any],
    classification: DocumentClassification | dict[str, Any] | None,
) -> dict[str, Any]:
    """既存 metadata に分類 payload を上書き統合します。"""
    merged = dict(metadata)
    merged["classification"] = classification_from_metadata(classification).to_metadata()
    return merged


def _classification_path_parts(path: Path, *, source_root: str | Path | None) -> list[str]:
    parts: Sequence[str]
    if source_root:
        try:
            parts = path.resolve().relative_to(Path(source_root).resolve()).parts
        except (OSError, ValueError):
            parts = path.parts
    else:
        parts = path.parts
    cleaned = [part for part in parts if part and part not in {path.anchor, "."}]
    if "share" in cleaned:
        cleaned = cleaned[cleaned.index("share") + 1 :]
    return [unicodedata.normalize("NFC", part) for part in cleaned]


def _small_category_from_path(path: Path, path_parts: Sequence[str]) -> str:
    candidates = [_clean_topic(path.stem)]
    candidates.extend(_clean_topic(part) for part in reversed(path_parts[:-1]))
    for candidate in candidates:
        if candidate and not _is_version_or_date(candidate):
            return candidate
    return ""


def _clean_topic(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\.[^.]+$", "", text)
    text = re.sub(r"^[\s_\\/-]*\d+[\s_\\/-]+", "", text)
    text = re.sub(r"^[(（]\s*\d+\s*[)）]\s*", "", text)
    text = re.sub(rf"[\s_\\/-]+\d+\s*{marker_patterns().page_words}?$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" _-/\\")
    return text


def _is_version_or_date(value: str) -> bool:
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    return bool(re.fullmatch(r"(?:ver)?\d+(?:\.\d+)*", text) or re.fullmatch(r"\d{6,8}", text))


def _large_category_value(value: Any) -> str:
    return _blankable_category_value(value)


def _middle_category_value(value: Any) -> str:
    return _blankable_category_value(value)


def _small_category_value(value: Any) -> str:
    return _blankable_category_value(value)


def _blankable_category_value(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if text in {"", "-", "任意", "未選択", "(未選択)", "指定なし", LEGACY_UNCATEGORIZED_VALUE}:
        return ""
    return text


def _saved_classification_metadata_paths(root: Path) -> list[Path]:
    patterns = (
        "*/viewer-data.json",
        "parse_inputs/**/*.json",
        "*/chunks/*/chunks.json",
    )
    paths: dict[str, Path] = {}
    for pattern in patterns:
        try:
            matches = root.glob(pattern)
            for path in matches:
                if path.is_file():
                    paths[path.as_posix()] = path
        except OSError:
            continue
    return [paths[key] for key in sorted(paths)]


def _collect_small_categories(value: Any, small: set[str], *, depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(value, dict):
        if isinstance(value.get("classification"), dict):
            category = classification_from_metadata(value.get("classification")).small_category
            if category:
                small.add(category)
        elif {"large_category", "middle_category", "small_category"} & set(value):
            category = classification_from_metadata(value).small_category
            if category:
                small.add(category)
        for child in value.values():
            _collect_small_categories(child, small, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _collect_small_categories(child, small, depth=depth + 1)


def _normalize_classification_payload(value: Any, *, depth: int = 0) -> int:
    if depth > 12:
        return 0
    changes = 0
    if isinstance(value, dict):
        raw_classification = value.get("classification")
        if isinstance(raw_classification, dict):
            normalized = classification_from_metadata(raw_classification).to_metadata()
            if raw_classification != normalized:
                value["classification"] = normalized
                changes += 1
            raw_classification = value.get("classification")
        raw_filter = value.get("classification_filter")
        if isinstance(raw_filter, dict):
            normalized_filter = classification_filter_from_metadata(raw_filter).to_metadata()
            if raw_filter != normalized_filter:
                value["classification_filter"] = normalized_filter
                changes += 1
            raw_filter = value.get("classification_filter")
        for key, child in list(value.items()):
            if key == "classification" and isinstance(raw_classification, dict):
                continue
            if key == "classification_filter" and isinstance(raw_filter, dict):
                continue
            changes += _normalize_classification_payload(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            changes += _normalize_classification_payload(child, depth=depth + 1)
    return changes


def _sorted_categories(
    values: Iterable[str],
    *,
    preferred_order: Sequence[str] = (),
) -> tuple[str, ...]:
    selected = {value for value in values if value}
    ordered = [value for value in preferred_order if value in selected]
    remaining = sorted(selected - set(ordered))
    return tuple(ordered + remaining)
