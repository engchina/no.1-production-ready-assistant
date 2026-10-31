"""管理対象ドメインキーワードの保存、編集、抽出を扱う。"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DOMAIN_KEYWORDS_SCHEMA_VERSION = 1
DOMAIN_KEYWORDS_FILE_NAME = "domain_keywords.json"
DOMAIN_KEYWORDS_BACKUP_DIR_NAME = "domain_keyword_backups"
MAX_DOMAIN_KEYWORDS = 1000
MAX_DOMAIN_KEYWORD_LENGTH = 120
MAX_EXTRACTED_DOMAIN_KEYWORDS = 16


@dataclass(frozen=True)
class DomainKeywordSaveResult:
    """ドメインキーワード保存時のパス、backup、件数を保持します。"""

    path: Path
    backup_path: Path | None
    keyword_count: int
    saved_at: datetime


def domain_keywords_path(output_dir: str | Path) -> Path:
    """output_dir 配下のドメインキーワード保存パスを返します。"""
    return Path(output_dir) / DOMAIN_KEYWORDS_FILE_NAME


def load_domain_keywords(output_dir: str | Path) -> list[str]:
    """保存済みドメインキーワードを読み込み、壊れた payload は空として扱います。"""
    path = domain_keywords_path(output_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []

    raw_keywords = payload.get("keywords") if isinstance(payload, dict) else payload
    return normalize_domain_keywords(raw_keywords)


def save_domain_keywords(
    output_dir: str | Path,
    keywords: Sequence[str],
    *,
    now: datetime | None = None,
) -> DomainKeywordSaveResult:
    """ドメインキーワードを正規化し、既存ファイルを backup して保存します。"""
    saved_at = _utc_now(now)
    path = domain_keywords_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_domain_keywords(keywords)

    backup_path: Path | None = None
    if path.exists():
        backup_dir = path.parent / DOMAIN_KEYWORDS_BACKUP_DIR_NAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = _unique_backup_path(path, backup_dir, saved_at)
        shutil.copy2(path, backup_path)

    temp_path = path.with_name(f".{path.name}.{_timestamp(saved_at)}.tmp")
    payload = {
        "schema_version": DOMAIN_KEYWORDS_SCHEMA_VERSION,
        "updated_at_utc": saved_at.isoformat(),
        "keywords": normalized,
    }
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    return DomainKeywordSaveResult(path=path, backup_path=backup_path, keyword_count=len(normalized), saved_at=saved_at)


def load_domain_keywords_text(output_dir: str | Path) -> str:
    """ドメインキーワードを複数行 text として読み込みます。"""
    return format_domain_keywords_text(load_domain_keywords(output_dir))


def save_domain_keywords_text(output_dir: str | Path, text: str) -> DomainKeywordSaveResult:
    """複数行 text を keyword に解析して保存します。"""
    return save_domain_keywords(output_dir, parse_domain_keywords_text(text))


def parse_domain_keywords_text(text: str) -> list[str]:
    """複数行またはカンマ区切り text から keyword 候補を抽出します。"""
    candidates: list[str] = []
    for raw_line in str(text or "").replace("，", "\n").replace(",", "\n").splitlines():
        line = raw_line.split("#", 1)[0]
        keyword = normalize_domain_keyword(line)
        if keyword:
            candidates.append(keyword)
    return normalize_domain_keywords(candidates)


def format_domain_keywords_text(keywords: Sequence[str]) -> str:
    """keyword list を 1 行 1 keyword の text に整形します。"""
    return "\n".join(normalize_domain_keywords(keywords))


def add_domain_keywords(existing_keywords: Sequence[str], additions: Sequence[str]) -> list[str]:
    """既存 keyword と追加候補を重複排除して統合します。"""
    return normalize_domain_keywords([*existing_keywords, *additions])


def remove_domain_keywords(existing_keywords: Sequence[str], removals: Sequence[str]) -> list[str]:
    """指定 keyword を正規化 key で既存 list から取り除きます。"""
    removal_keys = {_keyword_key(keyword) for keyword in removals if _keyword_key(keyword)}
    return [keyword for keyword in normalize_domain_keywords(existing_keywords) if _keyword_key(keyword) not in removal_keys]


def extract_domain_keywords(
    text: str,
    keywords: Sequence[str],
    *,
    limit: int = MAX_EXTRACTED_DOMAIN_KEYWORDS,
) -> list[str]:
    """本文に含まれる管理済み keyword を長い語優先で抽出します。"""
    normalized_text = _keyword_key(text)
    if not normalized_text:
        return []
    ordered = sorted(
        normalize_domain_keywords(keywords),
        key=lambda keyword: (-len(_keyword_key(keyword)), _keyword_key(keyword)),
    )
    matches: list[str] = []
    seen: set[str] = set()
    matched_keys: list[str] = []
    for keyword in ordered:
        key = _keyword_key(keyword)
        if not key or key in seen:
            continue
        if any(key in matched_key for matched_key in matched_keys):
            continue
        if not _keyword_matches(normalized_text, key):
            continue
        matches.append(keyword)
        seen.add(key)
        matched_keys.append(key)
        if len(matches) >= max(1, int(limit or 1)):
            break
    return matches


def normalize_domain_keywords(raw_keywords: Any) -> list[str]:
    """keyword list を正規化、重複排除、件数上限適用します。"""
    if not isinstance(raw_keywords, Sequence) or isinstance(raw_keywords, (str, bytes)):
        return []
    keywords: list[str] = []
    seen: set[str] = set()
    for raw_keyword in raw_keywords:
        keyword = normalize_domain_keyword(raw_keyword)
        key = _keyword_key(keyword)
        if not keyword or not key or key in seen:
            continue
        keywords.append(keyword)
        seen.add(key)
        if len(keywords) >= MAX_DOMAIN_KEYWORDS:
            break
    return keywords


def normalize_domain_keyword(value: Any) -> str:
    """1 件の keyword を NFKC 正規化し長さ上限へ収めます。"""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if len(normalized) > MAX_DOMAIN_KEYWORD_LENGTH:
        normalized = normalized[:MAX_DOMAIN_KEYWORD_LENGTH].rstrip()
    return normalized


def _keyword_matches(normalized_text: str, normalized_keyword: str) -> bool:
    if re.fullmatch(r"[0-9a-z_]+", normalized_keyword):
        pattern = rf"(?<![0-9a-z_]){re.escape(normalized_keyword)}(?![0-9a-z_])"
        return re.search(pattern, normalized_text) is not None
    return normalized_keyword in normalized_text


def _keyword_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


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
