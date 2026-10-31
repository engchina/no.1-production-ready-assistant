"""アップロード済み PDF の一覧・再選択・削除。

`output_dir/<run_id>/source.pdf` と `viewer-data.json` を正とし、同じ内容（SHA-256）で同じ
ファイル名の run を 1 つのファイルとして扱う。UI に依存しない。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from rag_engine.chunking.storage import CHUNKS_DIRECTORY, load_chunk_run_by_id
from rag_engine.config import Settings
from rag_engine.generation.answer_records import RUN_ID_PATTERN
from rag_engine.parsing.parse_inputs import PARSE_INPUT_DIRECTORY, file_sha256, filename_key

UPLOADED_FILE_COLUMNS = ("ファイル名", "ページ数", "サイズ", "解析", "AI 読み取り", "チャンク", "Embedding")
SOURCE_FILE_NAME = "source.pdf"


@dataclass(frozen=True)
class UploadedFile:
    """同じ内容・同じファイル名の run をまとめた 1 ファイル。run_ids は新しい順。"""

    sha256: str
    file_name: str
    page_count: int
    run_ids: tuple[str, ...]
    updated_at: float  # 最新 run の更新時刻（POSIX 秒）
    chunk_run_ids: tuple[str, ...] = ()
    size_bytes: int = 0
    analyzed: bool = False  # いずれかの run に解析結果（results.json）がある
    vision: bool | None = None  # 最新の解析 run が Vision（図・画像の AI 読み取り）ありだったか。None は未解析
    embedded: bool | None = None  # ADB に Embedding がある。None は ADB 未設定・接続失敗で未確認

    @property
    def key(self) -> str:
        """UI の選択値に使う識別子。ファイル名を含めず、パスとしても解釈されない。"""
        return f"{self.sha256}:{filename_key(self.file_name)}"

    @property
    def columns(self) -> list[str]:
        """一覧の表に表示する値。UPLOADED_FILE_COLUMNS と同じ並び。

        実行回数と最終更新は preview run も数えて解析の回数・時刻を表さないため、表には出さない。
        """
        size = f"{self.size_bytes / 1024 / 1024:.1f} MB" if self.size_bytes >= 1024 * 1024 else f"{max(1, self.size_bytes // 1024)} KB"
        return [
            self.file_name,
            f"{self.page_count} ページ",
            size,
            "解析済み" if self.analyzed else "未解析",
            "-" if self.vision is None else ("あり" if self.vision else "なし"),
            "チャンク済み" if self.chunk_run_ids else "未作成",
            "未確認" if self.embedded is None else ("保存済み" if self.embedded else "未保存"),
        ]

    @property
    def label(self) -> str:
        """一覧に表示する 1 行。"""
        updated = datetime.fromtimestamp(self.updated_at).strftime("%Y-%m-%d %H:%M")
        state = "チャンク済み" if self.chunk_run_ids else "チャンク未作成"
        return f"{self.file_name}（{self.page_count} ページ / {state} / {updated}）"


@dataclass(frozen=True)
class UploadedFileDeletion:
    """削除結果。database_deleted が None のときは ADB が未設定で、ローカルだけを削除した。"""

    file_name: str
    run_count: int
    database_deleted: int | None


@lru_cache(maxsize=512)
def _cached_sha256(path: str, mtime_ns: int, size: int) -> str:
    # 一覧を開くたびに全 run の PDF を読み直さない。source.pdf は書き換えないので mtime と size で足りる。
    return file_sha256(path)


@lru_cache(maxsize=512)
def _cached_used_docling_vision(path: str, mtime_ns: int, size: int) -> bool:
    """results.json の statuses から、その run が Vision 説明を有効にして解析したかを読みます。

    解析結果は 1 run で数十 MB になるため、一覧を開くたびに読み直さないよう mtime と size をキーに
    caching する（results.json は run の完了時に書いたあと更新しない）。読めない結果は「なし」扱い。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    statuses = payload.get("statuses") if isinstance(payload, dict) else None
    return any(isinstance(status, dict) and status.get("use_docling_vision") for status in statuses or ())


def _used_docling_vision(results_path: Path) -> bool:
    """解析結果 1 件の Vision 利用有無を返します。読めない場合は False。"""
    try:
        stat = results_path.stat()
    except OSError:
        return False
    return _cached_used_docling_vision(str(results_path), stat.st_mtime_ns, stat.st_size)


def list_uploaded_files(
    output_dir: str | Path,
    *,
    limit: int | None = None,
    embedded_sources: set[tuple[str, str]] | None = None,
) -> list[UploadedFile]:
    """アップロード済み PDF を新しい順に返します。読めない run は飛ばします。limit が None なら全件です。

    embedded_sources は ADB に Embedding がある文書の (SHA-256, ファイル名) の集合。None のときは
    Embedding の有無を判定せず、各ファイルの embedded を None（未確認）にする。
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        run_dirs = [path for path in Path(output_dir).iterdir() if path.is_dir() and RUN_ID_PATTERN.fullmatch(path.name)]
    except OSError:
        return []
    for run_dir in run_dirs:
        source = run_dir / SOURCE_FILE_NAME
        try:
            payload = json.loads((run_dir / "viewer-data.json").read_text(encoding="utf-8"))
            stat = source.stat()
            sha256 = _cached_sha256(str(source), stat.st_mtime_ns, stat.st_size)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        file_name = Path(str(payload.get("pdf_name") or "")).name if isinstance(payload, dict) else ""
        if not file_name.lower().endswith(".pdf"):
            continue
        group = groups.setdefault(
            (sha256, file_name), {"runs": [], "page_count": 0, "chunk_run_ids": [], "size_bytes": stat.st_size, "analyzed_runs": []}
        )
        group["runs"].append((stat.st_mtime, run_dir.name))
        results = run_dir / "results.json"
        if results.is_file():
            # 解析済み run の (更新時刻, Vision 利用) を集め、一覧には最新の解析 run の設定を出す。
            group["analyzed_runs"].append((stat.st_mtime, _used_docling_vision(results)))
        group["page_count"] = max(group["page_count"], int(payload.get("source_page_count") or len(payload.get("pages") or [])))
        chunk_root = run_dir / CHUNKS_DIRECTORY
        if chunk_root.is_dir():
            group["chunk_run_ids"].extend(path.name for path in chunk_root.iterdir() if path.is_dir())
    files = [
        UploadedFile(
            sha256=sha256,
            file_name=file_name,
            page_count=group["page_count"],
            run_ids=tuple(run_id for _, run_id in sorted(group["runs"], reverse=True)),
            updated_at=max(mtime for mtime, _ in group["runs"]),
            chunk_run_ids=tuple(dict.fromkeys(group["chunk_run_ids"])),
            size_bytes=group["size_bytes"],
            analyzed=bool(group["analyzed_runs"]),
            vision=max(group["analyzed_runs"])[1] if group["analyzed_runs"] else None,
            embedded=None if embedded_sources is None else (sha256, file_name) in embedded_sources,
        )
        for (sha256, file_name), group in groups.items()
    ]
    files.sort(key=lambda item: item.updated_at, reverse=True)
    return files if limit is None else files[:limit]


def find_uploaded_file(output_dir: str | Path, key: str) -> UploadedFile | None:
    """UI の選択値から対象ファイルを探します。件数上限の外にあるファイルも対象です。"""
    return next((item for item in list_uploaded_files(output_dir, limit=None) if item.key == str(key or "")), None)


def _materialized_directory(output_dir: str | Path, uploaded: UploadedFile) -> Path:
    workspace = hashlib.sha256(str(Path(output_dir).resolve()).encode()).hexdigest()
    identity = hashlib.sha256(uploaded.key.encode()).hexdigest()
    return Path(tempfile.gettempdir()) / "rag_engine_uploaded_files" / workspace / identity


@contextmanager
def _uploaded_file_lock(output_dir: str | Path, uploaded: UploadedFile) -> Iterator[Path]:
    target = _materialized_directory(output_dir, uploaded)
    # 複製先を消しても lock の inode を残し、待機中の複製・削除が別の lock を取らないようにする。
    lock_dir = target.parent / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / (target.name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield target
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def materialize_uploaded_file(output_dir: str | Path, uploaded: UploadedFile) -> Path:
    """保存済みの原本を元の名前で複製する。作業領域・内容・ファイル名ごとに複製と削除を直列化する。"""
    with _uploaded_file_lock(output_dir, uploaded) as target_dir:
        source = Path(output_dir) / uploaded.run_ids[0] / SOURCE_FILE_NAME
        size = source.stat().st_size
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / Path(uploaded.file_name).name
        if not target.is_file() or target.stat().st_size != size:
            # 書きかけのファイルを既存の読取側へ公開しない。
            with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as temporary:
                staging = Path(temporary.name)
            try:
                shutil.copyfile(source, staging)
                staging.replace(target)
            finally:
                staging.unlink(missing_ok=True)
        return target


def delete_uploaded_file(
    output_dir: str | Path,
    uploaded: UploadedFile,
    settings: Settings,
    *,
    delete_documents: Callable[..., int] | None = None,
) -> UploadedFileDeletion:
    """ファイルに紐づく ADB の文書、全 run（解析結果・チャンク・回答）、保存済み解析入力を削除します。

    ADB を先に消す。失敗したら例外を伝播し、ローカルには触れない（消し残しを同じ操作で再実行できる）。
    ADB が未設定（settings.adb_settings が None）の場合はローカルだけを削除する。
    """
    with _uploaded_file_lock(output_dir, uploaded) as target_dir:
        root = Path(output_dir).resolve()
        run_dirs = [root / run_id for run_id in uploaded.run_ids]
        # 一覧由来の run ID だけを対象にするが、削除の直前にも output_dir の直下であることを確認する。
        if any(not RUN_ID_PATTERN.fullmatch(path.name) or path.resolve().parent != root for path in run_dirs):
            raise ValueError("削除対象の run が出力ディレクトリの外を指しています。")

        database_deleted: int | None = None
        if getattr(settings, "adb_settings", None) is not None:
            if delete_documents is None:
                from rag_engine.adapters.oracle.store import delete_source_documents as delete_documents
            database_deleted = delete_documents(
                settings,
                source_file_sha256=uploaded.sha256,
                source_file_name=uploaded.file_name,
                document_ids=_document_ids(root, uploaded),
            )

        # 一覧取得後に別セッションで消えた run や、前回の削除が途中で失敗した後の再実行では、run が既に無いことがある。
        # 無いものは飛ばし、残りを消す（同じ操作で再実行できる契約を守る。#827）。
        removed = 0
        for run_dir in run_dirs:
            if run_dir.is_dir():
                shutil.rmtree(run_dir, ignore_errors=False)
                removed += 1
        shutil.rmtree(root / PARSE_INPUT_DIRECTORY / filename_key(uploaded.file_name), ignore_errors=True)
        shutil.rmtree(target_dir, ignore_errors=True)
        return UploadedFileDeletion(uploaded.file_name, removed, database_deleted)


def _document_ids(output_dir: Path, uploaded: UploadedFile) -> list[str]:
    """ローカルの chunk run から ADB の document_id を求めます。読めない chunk run は飛ばします。"""
    from rag_engine.adapters.oracle.store import document_id_for_chunk_run

    ids: list[str] = []
    for chunk_run_id in uploaded.chunk_run_ids:
        try:
            chunk_run = load_chunk_run_by_id(output_dir, chunk_run_id)
        except (OSError, ValueError):
            continue
        if chunk_run is not None:
            ids.append(document_id_for_chunk_run(chunk_run))
    return list(dict.fromkeys(ids))
