"""用語・同義語と回答ルールの管理の検証、競合検出、バックアップ付き保存を扱う。"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from rag_engine.knowledge.rule_application import RULE_INSTRUCTION_KINDS, RULE_PURPOSES
from rag_engine.knowledge.runtime_knowledge import (
    MAX_ALIASES_PER_TERM,
    MAX_RULES,
    MAX_TERMS,
    MAX_TEXT_CHARS,
    MAX_TRIGGERS_PER_RULE,
    RUNTIME_KNOWLEDGE_ACTIVE_STATUSES,
    runtime_knowledge_path,
)


@dataclass(frozen=True)
class KnowledgeSnapshot:
    """編集開始時の設定パス・内容・リビジョンを保持する。内容は保存時にコピーする。"""

    path: Path
    payload: dict[str, Any]
    revision: str


def _revision(raw: bytes | None) -> str:
    return hashlib.sha256(raw).hexdigest() if raw is not None else "missing"


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _validate_payload(payload: Any) -> None:
    """暗黙の行破棄を避けるため、管理可能な標準形式だけを書込対象にする。"""
    if not isinstance(payload, dict) or payload.get("schema_version", 1) != 1:
        raise ValueError("管理画面は schema_version: 1 の JSON オブジェクトに対応しています。既存ファイルは変更していません。")
    if any(key in payload for key in ("glossary", "glossaries", "rulebook", "rulebooks")):
        raise ValueError("旧形式の設定です。terms / rules の標準形式へ移行してから再読込してください。回答生成の旧形式読込は継続できます。")
    for kind, identity, limit in (("terms", "term", MAX_TERMS), ("rules", "id", MAX_RULES)):
        rows = payload.get(kind, [])
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError(f"{kind} は {limit} 件以内の配列で指定してください。")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get(identity), str) or not _key(row[identity]):
                raise ValueError(f"{kind} の各行には {identity} が必要です。標準形式を確認してください。")
            key = _key(row[identity])
            if key in seen:
                raise ValueError(f"{kind} の {identity} が重複しています。既存ファイルを確認してください。")
            seen.add(key)
            legacy_fields = (
                ("synonyms", "abbreviations", "abbrev", "alias", "definition", "body", "note", "document", "path", "lifecycle_status")
                if kind == "terms"
                else ("terms", "keywords", "aliases", "when", "body", "text", "rule", "document", "path", "lifecycle_status")
            )
            if any(field in row for field in legacy_fields):
                raise ValueError(
                    f"{kind} に旧形式の項目があります。標準フィールドへ移行してから再読込してください。既存ファイルは変更していません。"
                )
            text_fields = (
                (identity, "source", "status", "description") if kind == "terms" else (identity, "source", "status", "title", "content")
            )
            if any(field in row and not isinstance(row[field], str) for field in text_fields):
                raise ValueError(f"{kind} のテキスト項目は文字列で指定してください。")
            list_field = "aliases" if kind == "terms" else "triggers"
            values = row.get(list_field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"{list_field} は文字列の配列で指定してください。")


def load_knowledge_snapshot(output_dir: Path, configured_path: Path | None = None) -> KnowledgeSnapshot:
    """回答生成と同じパスから編集用コピーを読む。破損・非対応形式は ValueError、I/O は OSError。"""
    path = runtime_knowledge_path(output_dir, configured_path).resolve()
    raw = _read_bytes(path)
    try:
        payload = json.loads(raw.decode("utf-8")) if raw is not None else {"schema_version": 1, "terms": [], "rules": []}
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("設定ファイルを読み込めません。JSON の文字コード・構文を確認してください。上書きは行いません。") from exc
    _validate_payload(payload)
    return KnowledgeSnapshot(path, payload, _revision(raw))


def _subject(label: str) -> str:
    """英数字で終わるラベル（「ルール ID」）は、画面の文言と同じく助詞の前に空白を入れる。"""
    return f"{label} " if label[-1:].isascii() and label[-1:].isalnum() else label


def _text(value: str, label: str, *, required: bool = False, limit: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
    subject = _subject(label)
    if required and not text:
        raise ValueError(f"{subject}を入力してください。")
    if len(text) > limit:
        raise ValueError(f"{subject}は {limit} 文字以内で入力してください（自動切り詰めはしません）。")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{subject}に制御文字は使用できません。")
    return text


def _labels(value: str, label: str, limit: int) -> list[str]:
    """UI の改行・カンマ区切りを正規化し、同一照合キーの重複を除く。切り詰めはしない。"""
    result, seen = [], set()
    for part in re.split(r"[\n,、，]", str(value or "")):
        text = _text(part, label, limit=160)
        if text and _key(text) not in seen:
            result.append(text)
            seen.add(_key(text))
    if len(result) > limit:
        raise ValueError(f"{label}は {limit} 件以内で入力してください。")
    return result


def edit_knowledge(
    snapshot: KnowledgeSnapshot,
    kind: str,
    selected: str | None,
    *,
    name: str = "",
    title: str = "",
    labels: str = "",
    content: str = "",
    source: str = "",
    purpose: str | None = None,
    instruction_kind: str | None = None,
    exclusive_group: str | None = None,
    priority: int | None = None,
    enabled: bool = True,
    delete: bool = False,
    confirmed: bool = False,
) -> tuple[KnowledgeSnapshot, Path | None]:
    """選択行を更新・削除し、未選択なら追加する。未知のメタデータと他行を保持する。

    Linux のファイルロックと SHA-256 により管理画面間の上書きを拒否する。
    既存内容を同じ親ディレクトリの runtime_knowledge_backups に保存してから原子的に置換する。
    入力・競合は ValueError、書込失敗は OSError。削除は confirmed=True が必須。
    外部エディタはロックに協調しないため、管理画面との同時編集を避けること。
    """
    if kind not in ("terms", "rules"):
        raise ValueError("管理対象が不正です。")
    payload = copy.deepcopy(snapshot.payload)
    rows = payload.setdefault(kind, [])
    identity = "term" if kind == "terms" else "id"
    index = next((i for i, row in enumerate(rows) if row[identity] == selected), None)
    if selected and index is None:
        raise ValueError("選択項目がありません。一覧を再読込してください。")
    if delete:
        if index is None or not confirmed:
            raise ValueError("削除対象を選択し、削除確認にチェックしてください。")
        rows.pop(index)
    else:
        row = copy.deepcopy(rows[index]) if index is not None else {}
        if kind == "rules" and not name.strip():
            name = selected or f"rule_{uuid4().hex}"
        name = _text(name, "用語" if kind == "terms" else "ルール ID", required=True, limit=160)
        if any(_key(other[identity]) == _key(name) for i, other in enumerate(rows) if i != index):
            raise ValueError("同じ用語またはルール ID が登録済みです。編集対象を選択してください。")
        row[identity] = name
        row["source"] = _text(source, "出典")
        if kind == "terms":
            row["aliases"] = _labels(labels, "別名", MAX_ALIASES_PER_TERM)
            row["description"] = _text(content, "説明")
        else:
            row["title"] = _text(title, "ルール名", required=True, limit=160)
            row["triggers"] = _labels(labels, "照合キーワード", MAX_TRIGGERS_PER_RULE)
            if not row["triggers"]:
                raise ValueError("照合キーワードを入力してください。")
            rule_purpose = row.get("purpose", "both") if purpose is None else purpose
            rule_kind = row.get("instruction_kind", "requirement") if instruction_kind is None else instruction_kind
            rule_priority = row.get("priority", 0) if priority is None else priority
            if rule_purpose not in RULE_PURPOSES or rule_kind not in RULE_INSTRUCTION_KINDS:
                raise ValueError("ルールの用途・指示の種類を選び直してください。")
            if type(rule_priority) is not int or not 0 <= rule_priority <= 100:
                raise ValueError("適用優先度は 0〜100 の整数で指定してください。")
            row.update(purpose=rule_purpose, instruction_kind=rule_kind, priority=rule_priority)
            row["exclusive_group"] = _text(
                row.get("exclusive_group", "") if exclusive_group is None else exclusive_group, "排他グループ", limit=160
            )
            row["content"] = _text(content, "ルール内容", required=rule_purpose != "confirmation")
        # 既存の承認・再確認・下書き状態は、利用可否を変更した場合だけ置き換える。
        if index is None or bool(enabled) != (row.get("status", "") in RUNTIME_KNOWLEDGE_ACTIVE_STATUSES):
            row["status"] = "approved" if enabled else "deprecated"
        row["updated_at"] = datetime.now(UTC).isoformat()
        if index is None:
            rows.append(row)
        else:
            rows[index] = row
    _validate_payload(payload)
    path = snapshot.path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = _read_bytes(path)
        if _revision(old) != snapshot.revision:
            raise ValueError("他の操作で設定が更新されています。一覧を再読込してから編集してください。未保存の入力は保持しています。")
        backup = None
        if old is not None:
            directory = path.parent / "runtime_knowledge_backups"
            directory.mkdir(parents=True, exist_ok=True)
            backup = directory / f"{path.stem}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}-{uuid4().hex}.json"
            with backup.open("xb") as stream:
                os.chmod(backup, 0o600)
                stream.write(old)
                stream.flush()
                os.fsync(stream.fileno())
        raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
                temp_path = Path(stream.name)
                if path.exists():
                    os.chmod(temp_path, path.stat().st_mode & 0o777)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return KnowledgeSnapshot(path, payload, _revision(raw)), backup
