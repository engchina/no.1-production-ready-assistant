"""Visionの応答・後処理結果と、Picture/Table別の途中状態を保存する。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from rag_engine.models.llm import FORM_FIELD_KINDS
from rag_engine.parsing.checkpoints import atomic_json, digest_json, file_digest

# prompt以外の後処理・画像入力契約を変更した場合も版を上げて再利用を無効化する。
# 2: Form の項目に種類（kind）を足した応答の契約（#2404）。
VISION_CHECKPOINT_VERSION = 2

_LOG = logging.getLogger(__name__)


class VisionCheckpoints:
    """同一runの再開で入力が一致する応答を再利用する。呼出し元が排他を保持する。

    ``listener`` は途中状態(progress.json と同じ内容)の保存ごとに呼ぶ(取込の job の進捗。
    #1999)。listener の失敗は警告に留め、Vision の処理を止めない。
    """

    def __init__(self, directory: Path, source_hash: str, listener: Callable[[dict], None] | None = None):
        self.directory = directory
        self.source_hash = source_hash
        self.listener = listener
        self.entries: dict[str, dict] = {}
        self.phase = "pictures"
        self.discovery_complete = False
        self.current = None

    def register(self, records, state="pending") -> None:
        """検出済み対象を登録する。TableはPicture処理後に追加検出される。"""
        for record in records:
            self.entries[record.id] = {"page": record.page, "kind": record.category, "state": state, "reused": False}

    def update(self, record=None, state=None, *, reused=False) -> None:
        """状態をatomic保存する。完了は応答・結果の永続化後にのみ通知する。"""
        if record is not None:
            self.current = {"record_id": record.id, "page": record.page, "kind": record.category}
            self.entries[record.id].update(state=state, reused=reused)
        counts = {
            name: sum(e["state"] == name for e in self.entries.values())
            for name in ("pending", "running", "succeeded", "failed", "skipped")
        }
        pages = {}
        for entry in self.entries.values():
            bucket = pages.setdefault(str(entry["page"]), {})
            key = entry["kind"] + ":" + entry["state"]
            bucket[key] = bucket.get(key, 0) + 1
        payload = {
            "schema_version": 1,
            "phase": self.phase,
            "total_known": len(self.entries),
            "total_final": self.discovery_complete,
            "counts": counts,
            "pages": pages,
            "current": self.current,
            "items": self.entries,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        atomic_json(self.directory / "progress.json", payload)
        if self.listener is not None:
            try:
                self.listener(payload)
            except Exception:  # noqa: BLE001 - 進捗の通知は補助。Vision の処理を止めない。
                _LOG.warning("Vision の進捗の通知に失敗しました", exc_info=True)
        # 進捗は logging へ出す(backend の Vision の段の stdout へ print しない。#502)。
        _LOG.info(
            "[vision-progress] phase=%s current=%s succeeded=%s failed=%s known=%s total_final=%s",
            self.phase,
            self.current,
            counts["succeeded"],
            counts["failed"],
            len(self.entries),
            self.discovery_complete,
        )

    def key(
        self, *, crop: Path, context: Path, prompt: str, system_prompt: str, provider, max_tokens: int, kind: str, api_mode: str
    ) -> str:
        """認証値を除き、入力画像・実prompt・接続先・model・出力上限をhash化する。"""
        return digest_json(
            {
                "version": VISION_CHECKPOINT_VERSION,
                "source": self.source_hash,
                "images": [file_digest(crop), file_digest(context)],
                "prompt": prompt,
                "system": system_prompt,
                "kind": kind,
                "provider": provider.provider_id,
                "model": provider.model,
                "api_mode": api_mode,
                "region": provider.region,
                "endpoint": provider.base_url,
                "project": provider.project_id,
                "max_tokens": max_tokens,
                "temperature": 0,
            }
        )

    def load(self, key: str) -> dict | None:
        """破損・失敗・入力違いはcache missとする。後処理失敗後の有効応答は再利用する。"""
        try:
            saved = json.loads((self.directory / "checkpoints" / (key + ".json")).read_text(encoding="utf-8"))
            response = saved["response"]
            if (
                saved.get("schema_version") == 1
                and saved["key"] == key
                and saved.get("reusable")
                and isinstance(response, dict)
                and saved["response_sha256"] == digest_json(response)
                and _has_current_contract(response)
            ):
                return response
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def save(self, key: str, response: dict | None, record, *, state: str, reusable: bool = True) -> None:
        """API応答を先に保存し、後処理終了時は完成recordと状態を追記する。"""
        atomic_json(
            self.directory / "checkpoints" / (key + ".json"),
            {
                "schema_version": 1,
                "key": key,
                "response": response,
                "response_sha256": digest_json(response),
                "reusable": reusable,
                "state": state,
                "record": asdict(record),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )


def _has_current_contract(response: dict) -> bool:
    """今の応答の契約の field を持つ応答か。

    台帳の key は版・prompt を含むが、key が一致しても（台帳の artifact を別の版から戻したなど）Form の項目に
    種類（kind）の無い応答は使い回さず、Vision を呼び直す（#2404）。
    """
    fields = response.get("form_fields")
    if fields is None:
        return True
    return isinstance(fields, list) and all(isinstance(item, dict) and item.get("kind") in FORM_FIELD_KINDS for item in fields)
