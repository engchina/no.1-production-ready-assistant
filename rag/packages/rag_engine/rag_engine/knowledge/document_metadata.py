"""文書の同一性・有効期間の検証を扱う。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

DOCUMENT_METADATA_SCHEMA_VERSION = 1
DOCUMENT_FIELDS = ("source_document_id", "source_uri", "effective_from", "effective_to")
_DATE_FIELDS = ("effective_from", "effective_to")
# 以前保存した metadata に残る項目。検索 filter にも回答にも使わないため読み込み時に捨てる (#893)。
_LEGACY_FIELDS = frozenset({"title", "document_version", "published_at", "source_updated_at"})


def normalize_document_metadata(value: Mapping[str, Any] | None, *, source_file_name: str) -> dict[str, Any]:
    """文書情報を検証して新しい dict を返す。未知値は省略し、不正入力は ValueError にする。

    日付は YYYY-MM-DD。有効期間の終端は排他的で、検索時の as_of filter が参照する。
    URL は永続的な HTTP(S) URL のみ。ID 未指定時は URL、なければローカルファイル名を名前空間として
    内容と独立に導出する。同名の別文書・改名・複数コーパスでは管理側の ID を明示すること。
    旧版で保存した版・タイトル・公開日・原文更新日は黙って除外する。
    """
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("文書情報はオブジェクトで指定してください。")
    raw = value or {}
    if raw.get("schema_version", DOCUMENT_METADATA_SCHEMA_VERSION) != DOCUMENT_METADATA_SCHEMA_VERSION:
        raise ValueError("対応していない文書情報の schema_version です。")
    unknown = set(raw) - set(DOCUMENT_FIELDS) - _LEGACY_FIELDS - {"schema_version", "identity_source", "first_page_context"}
    if unknown:
        raise ValueError("文書情報に未対応の項目があります。")
    result: dict[str, Any] = {"schema_version": DOCUMENT_METADATA_SCHEMA_VERSION}
    if "first_page_context" in raw:
        result["first_page_context"] = normalize_first_page_context(raw["first_page_context"])
    for key in DOCUMENT_FIELDS:
        item = raw.get(key)
        if item is None or item == "":
            continue
        if not isinstance(item, str):
            raise ValueError(f"{key} は文字列で指定してください。")
        item = unicodedata.normalize("NFC", item.strip())
        if not item:
            continue
        limit = 2048 if key == "source_uri" else 256
        if len(item) > limit or any(ord(char) < 32 for char in item):
            raise ValueError(f"{key} の長さまたは制御文字が不正です。")
        result[key] = item
    for key in _DATE_FIELDS:
        if key in result:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result[key]):
                    raise ValueError
                date.fromisoformat(result[key])
            except ValueError as exc:
                raise ValueError(f"{key} は有効な日付 YYYY-MM-DD で指定してください。") from exc
    if result.get("effective_from") and result.get("effective_to") and result["effective_from"] >= result["effective_to"]:
        raise ValueError("有効終了日は有効開始日より後の日付にしてください。")
    if "source_uri" in result:
        try:
            parts = urlsplit(result["source_uri"])
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError
            secret_keys = {
                "token",
                "access_token",
                "sig",
                "signature",
                "x-amz-signature",
                "x-amz-credential",
                "x-goog-signature",
                "api_key",
                "apikey",
                "password",
                "secret",
                "code",
            }
            if any(key.lower() in secret_keys for key, _ in parse_qsl(parts.query)) or any(char.isspace() for char in result["source_uri"]):
                raise ValueError
            _ = parts.port
        except ValueError as exc:
            raise ValueError("出典URLは認証情報・署名tokenのない永続的な HTTP(S) URL を指定してください。") from exc
        result["source_uri"] = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))
    if "source_document_id" in result:
        # 保存済み由来ラベルを引き継ぐが、未知のラベルを監査情報として採用しない。
        basis = raw.get("identity_source")
        result["identity_source"] = basis if basis in {"source_uri", "local_filename"} else "explicit"
    else:
        uri = result.get("source_uri")
        name = unicodedata.normalize("NFC", source_file_name)
        if not uri and not name:
            return result
        basis = "source_uri" if uri else "local_filename"
        # ページ/節への fragment は同じ文書の位置指定であり、文書の同一性に含めない。
        identity_uri = uri.split("#", 1)[0] if uri else None
        # seed の接頭辞は package の改名(#599)の前の名前のまま。変えると保存済みの chunk の
        # source_document_id(同じ文書の判定に使う)と、新しく作る chunk の値が食い違う。
        seed = f"docrag:{basis}:{identity_uri or name}"
        result["source_document_id"] = "source-" + hashlib.sha256(seed.encode()).hexdigest()[:32]
        result["identity_source"] = basis
    return result


def document_context_text(metadata: Mapping[str, Any]) -> str:
    """回答 context 用に有効期間だけを返す。検索テキストには含めず、技術 ID も含めない。"""
    labels = {"effective_from": "Effective from", "effective_to": "Effective until (exclusive)"}
    return " / ".join(f"{label}: {metadata[key]}" for key, label in labels.items() if metadata.get(key))


def normalize_first_page_context(value: Any) -> dict[str, Any]:
    """回答専用の物理第1ページ情報を検証する。検索テキストには追加しない。"""
    keys = {"page", "status", "text", "engine", "record_ids", "truncated"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("first_page_context の項目が不正です。")
    if (
        type(value["page"]) is not int
        or value["page"] != 1
        or value["status"] not in {"available", "empty", "not_analyzed", "not_paginated"}
    ):
        raise ValueError("first_page_context の取得状態・物理ページが不正です。")
    if (
        not isinstance(value["text"], str)
        or len(value["text"]) > 8000
        or not isinstance(value["engine"], str)
        or len(value["engine"]) > 128
        or not isinstance(value["truncated"], bool)
        or not isinstance(value["record_ids"], list)
        or len(value["record_ids"]) > 128
        or not all(isinstance(v, str) and len(v) <= 256 for v in value["record_ids"])
    ):
        raise ValueError("first_page_context の本文・出典が不正です。")
    if bool(value["text"].strip()) != (value["status"] == "available"):
        raise ValueError("first_page_context の本文と取得状態が一致しません。")
    return {**value, "record_ids": list(value["record_ids"])}


def extract_first_page_context(
    records: Iterable[Mapping[str, Any]], *, engine: str, analyzed_pages: Iterable[int], paginated: bool
) -> dict[str, Any]:
    """既存解析の物理第1ページを読順で保存する。I/O・追加OCR・推測は行わない。

    Picture の生成説明と OCR（picture_ocr_text）は本文に含めず、文字レイヤーから抽出されたレコードだけを使う。
    1 ページ目が画面キャプチャ中心の説明書では、OCR の項目名の羅列が「文書背景」の先頭 800 字を占めていた (#812)。
    保存上限8000文字、参照128件。取得できない場合も理由を保存する。
    """
    selected = sorted((r for r in records if r.get("page") == 1), key=lambda r: int(r.get("seq_no") or 0)) if paginated else []
    native = [r for r in selected if r.get("category") != "Picture"]
    texts = list(dict.fromkeys(str(r.get("text") or "").strip() for r in native if str(r.get("text") or "").strip()))
    text = "\n".join(texts)
    status = "available" if text else "not_paginated" if not paginated else "empty" if selected or 1 in analyzed_pages else "not_analyzed"
    return dict(
        page=1,
        status=status,
        text=text[:8000],
        engine=engine,
        record_ids=[str(r.get("id") or r.get("seq_no") or "")[:256] for r in native][:128],
        truncated=len(text) > 8000,
    )
