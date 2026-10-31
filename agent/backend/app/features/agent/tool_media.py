"""ツールの結果の画像を、モデルの入力まで届ける（#2650）。

ツールの結果（MCP の `tools/call` の content の image、会話の添付の読み取りなど）の画像を、
文字の結果（JSON）と分けて持ち、SDK の正式な画像入力（function tool の出力の
`ToolOutputImage`。Responses API の `function_call_output` の `input_image`）でモデルへ渡す。
MCP に依らない型と変換だけを持つ（MCP の content の読み取りは `tools.parse_mcp_tool_result`、
Run への記録と SDK・入力の管理への組み込みは `builtin_runtime` / `context_window`）。

- **持ち方**: handler は `ToolOutputWithImages`（文字の結果 + `ToolImage`）を返す。画像のバイト列は
  `ToolResult` の非保存の属性（`tool_result_images`）だけに置き、Run の step・イベント・ログ・
  成果物には参照（`image_id`＝sha256・MIME・大きさ・由来）だけを残す。
- **届け方**: 呼び出しの境界（`_ToolRecorder`）が、モデルの画像対応・1 回の結果の枚数の上限で
  添付する画像を決め（`plan_image_delivery`）、結果の参照に `model_input`（添付した / しない理由）を
  書く。添付する画像は `model_tool_output` で `[ToolOutputText, ToolOutputImage, ...]` にする。
- **入力の管理**: 同じ画像の重複・入力の画像の枚数の上限・予算は、モデルの呼び出しの直前に
  `context_window` が決める（後の結果に同じ画像があれば前の添付を外し、参照は残す）。
- **保存**: 承認待ちで保存する SDK の状態からは画像のデータを外す（`strip_image_data`）。再開・
  会話の続きでは画像を持ち越さず、要るときはツールで取り直す（取り直すたびに呼び先が認可する）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Literal

from agents import ToolOutputImage, ToolOutputText

# 結果の画像の参照を入れる項目（呼び先の構造化データの項目と分ける）。
CONTENT_IMAGES_KEY = "content_images"
# 画像を読めなかったことの案内（モデルへの結果に足す）。
IMAGE_INPUT_NOTE_KEY = "image_input_note"
# モデルに渡せる画像の MIME（Responses API の input_image が受け付ける形式）。
TOOL_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
# 1 枚の画像のバイト数の上限（デコードした後）。RAG の図の画像（1.5MB 以下）が収まる大きさ。
MAX_IMAGE_BYTES = 5_000_000
# 1 回のツールの結果でモデルに添付する画像の数の上限。
MAX_IMAGES_PER_RESULT = 4
# 1 回のモデルの入力に添付する画像の数の上限（新しい結果の画像から残す）。
MAX_IMAGES_IN_INPUT = 8
# 画像の token の見積もり（高い解像度の扱い: 長い辺を 2048、短い辺を 768 に縮め、512 px の
# 区画ごとに数える。主要な Vision の API の計算と同じ形で、どの登録モデルでも多めになる値）。
IMAGE_BASE_TOKENS = 85
IMAGE_TILE_TOKENS = 170
_IMAGE_MAX_EDGE = 2048
_IMAGE_SHORT_EDGE = 768
_IMAGE_TILE = 512
# 大きさが読めない画像の見積もり（2048 × 2048 を縮めた 768 × 768 = 4 区画）。
UNKNOWN_IMAGE_TOKENS = IMAGE_BASE_TOKENS + IMAGE_TILE_TOKENS * 4

ImageInputStatus = Literal[
    "attached",
    "unsupported_model",
    "over_limit",
    "too_large",
    "unsupported_type",
    "invalid",
    "user_only",
]
ATTACHED: ImageInputStatus = "attached"
UNSUPPORTED_MODEL: ImageInputStatus = "unsupported_model"
OVER_LIMIT: ImageInputStatus = "over_limit"
TOO_LARGE: ImageInputStatus = "too_large"
UNSUPPORTED_TYPE: ImageInputStatus = "unsupported_type"
INVALID: ImageInputStatus = "invalid"
USER_ONLY: ImageInputStatus = "user_only"

# モデルに渡す、画像を渡さなかった理由（状態で変わる案内。値は正本の定数から差し込む）。
_STATUS_NOTES: dict[str, str] = {
    UNSUPPORTED_MODEL: (
        "この業務 Agent のモデルは画像の入力に対応していないため、画像を読めていません。"
        "画像の内容は確認できていないものとして扱い、画像だけにある内容を回答で断定しないで"
        "ください。"
    ),
    OVER_LIMIT: (
        f"1 回の結果で渡せる画像は {MAX_IMAGES_PER_RESULT} 枚までのため、残りの画像は渡して"
        "いません。要る画像は対象を絞って読み直してください。"
    ),
    TOO_LARGE: (
        f"画像が大きすぎる（{MAX_IMAGE_BYTES} バイトを超える）ため渡していません。"
        "画像の内容は確認できていないものとして扱ってください。"
    ),
    UNSUPPORTED_TYPE: (
        "画像の形式がモデルの入力に使えないため渡していません。"
        "画像の内容は確認できていないものとして扱ってください。"
    ),
    INVALID: "画像のデータが読めないため渡していません。",
}


def _image_dimensions(data: bytes, mime_type: str) -> tuple[int, int] | None:
    """画像の幅と高さ（px。ヘッダーから読む。読めなければ None）。"""
    try:
        if mime_type == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
            return int(width), int(height)
        if mime_type == "image/gif" and data[:3] == b"GIF" and len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
            return int(width), int(height)
        if mime_type == "image/jpeg" and data[:2] == b"\xff\xd8":
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    return None
                marker = data[index + 1]
                length = struct.unpack(">H", data[index + 2 : index + 4])[0]
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
                    height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                    return int(width), int(height)
                index += 2 + length
    except struct.error:
        return None
    return None


def estimate_image_tokens(width: int | None, height: int | None) -> int:
    """画像 1 枚の入力 token の見積もり（大きさが分からなければ多めの既定）。"""
    if not width or not height or width <= 0 or height <= 0:
        return UNKNOWN_IMAGE_TOKENS
    scale = min(1.0, _IMAGE_MAX_EDGE / max(width, height))
    w, h = width * scale, height * scale
    shrink = min(1.0, _IMAGE_SHORT_EDGE / min(w, h))
    w, h = w * shrink, h * shrink
    tiles = math.ceil(w / _IMAGE_TILE) * math.ceil(h / _IMAGE_TILE)
    return IMAGE_BASE_TOKENS + IMAGE_TILE_TOKENS * tiles


@dataclass(frozen=True)
class ToolImage:
    """ツールの結果の画像 1 枚（バイト列と由来。由来に画像のデータは入れない）。

    `source` は画像の由来（呼び先の接続・ツール・content の位置、文書の版・page・bbox など。
    生成と検証で同じ版・領域を使えるように参照に残す）。
    """

    data: bytes = field(repr=False)
    mime_type: str
    source: Mapping[str, Any] = field(default_factory=dict)

    @cached_property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def image_id(self) -> str:
        return f"sha256:{self.sha256}"

    @cached_property
    def dimensions(self) -> tuple[int, int] | None:
        return _image_dimensions(self.data, self.mime_type)

    @property
    def estimated_tokens(self) -> int:
        size = self.dimensions
        return estimate_image_tokens(*(size or (None, None)))

    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{base64.b64encode(self.data).decode('ascii')}"

    def reference(self) -> dict[str, Any]:
        """Run の step・モデルへの文字の結果に残す参照（画像のデータは含めない）。"""
        ref: dict[str, Any] = {
            "image_id": self.image_id,
            "mime_type": self.mime_type,
            "byte_size": len(self.data),
        }
        if self.dimensions is not None:
            ref["width"], ref["height"] = self.dimensions
        if self.source:
            ref["source"] = dict(self.source)
        return ref


@dataclass(frozen=True)
class ToolOutputWithImages:
    """handler の戻り値: 文字の結果（JSON）と画像。

    `skipped` は使えなかった画像の参照（形式・大きさ・対象が利用者だけなど。データは含めない）。
    `tool_registry.invoke` が分け、文字の結果は従来どおり検査・記録し、画像は `ToolResult` の
    非保存の属性に置く。
    """

    output: dict[str, Any]
    images: tuple[ToolImage, ...] = ()
    skipped: tuple[dict[str, Any], ...] = ()


def decode_image(
    data: object, mime_type: object, *, source: Mapping[str, Any] | None = None
) -> ToolImage | dict[str, Any]:
    """base64 の画像を `ToolImage` にする（使えなければ理由つきの参照。データは含めない）。"""
    mime = str(mime_type or "").strip().lower()
    base: dict[str, Any] = {"mime_type": mime or None}
    if source:
        base["source"] = dict(source)
    if mime not in TOOL_IMAGE_MIME_TYPES:
        return {**base, "model_input": UNSUPPORTED_TYPE}
    if not isinstance(data, str) or not data:
        return {**base, "model_input": INVALID}
    # デコードの前に大きさを見る（base64 の 4 文字 = 3 バイト）。
    if len(data) * 3 // 4 > MAX_IMAGE_BYTES + 3:
        return {**base, "byte_size": len(data) * 3 // 4, "model_input": TOO_LARGE}
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return {**base, "model_input": INVALID}
    if not raw:
        return {**base, "model_input": INVALID}
    if len(raw) > MAX_IMAGE_BYTES:
        return {**base, "byte_size": len(raw), "model_input": TOO_LARGE}
    return ToolImage(data=raw, mime_type=mime, source=dict(source or {}))


@dataclass(frozen=True)
class ImageDelivery:
    """1 回のツールの結果の画像を、モデルに添付するかの決定。"""

    output: dict[str, Any]
    attached: tuple[ToolImage, ...]
    summary: dict[str, Any]


def plan_image_delivery(
    output: Mapping[str, Any],
    images: Sequence[ToolImage],
    *,
    image_input: bool,
    max_images: int = MAX_IMAGES_PER_RESULT,
) -> ImageDelivery:
    """結果の画像のうちモデルに添付するものを決め、参照に `model_input` を書く。

    同じ結果の中の同じ画像は 1 回だけ添付する。画像に対応しないモデルには添付せず、読めなかった
    ことを結果に書く（`image_input_note`）。参照は既存の `content_images` の順を保つ。
    """
    refs = [
        dict(item) for item in (output.get(CONTENT_IMAGES_KEY) or []) if isinstance(item, Mapping)
    ]
    by_id: dict[str, list[int]] = {}
    for index, ref in enumerate(refs):
        image_id = ref.get("image_id")
        if isinstance(image_id, str):
            by_id.setdefault(image_id, []).append(index)
    attached: list[ToolImage] = []
    seen: set[str] = set()
    for image in images:
        if image.image_id in seen:
            continue
        seen.add(image.image_id)
        if not image_input:
            status: ImageInputStatus = UNSUPPORTED_MODEL
        elif len(attached) >= max_images:
            status = OVER_LIMIT
        else:
            status = ATTACHED
            attached.append(image)
        for index in by_id.get(image.image_id, []):
            refs[index]["model_input"] = status
    statuses = [str(ref.get("model_input") or "") for ref in refs]
    notes = [
        _STATUS_NOTES[status]
        for status in dict.fromkeys(statuses)
        if status in _STATUS_NOTES and status != ATTACHED
    ]
    result = dict(output)
    if refs:
        result[CONTENT_IMAGES_KEY] = refs
    if notes:
        result[IMAGE_INPUT_NOTE_KEY] = "".join(notes)
    summary: dict[str, Any] = {
        "images": len(refs),
        "attached": len(attached),
        "estimated_tokens": sum(image.estimated_tokens for image in attached),
        "statuses": {status: statuses.count(status) for status in dict.fromkeys(statuses)},
        "image_input": image_input,
    }
    return ImageDelivery(output=result, attached=tuple(attached), summary=summary)


def model_tool_output(
    text: str, images: Sequence[ToolImage]
) -> str | list[ToolOutputText | ToolOutputImage]:
    """モデルに渡す function tool の出力（画像があれば SDK の文字と画像の出力の列）。"""
    if not images:
        return text
    return [
        ToolOutputText(text=text),
        *(ToolOutputImage(image_url=image.data_url(), detail="auto") for image in images),
    ]


# 保存する状態から外した画像の代わりの文（モデルが再開の後に読む）。
IMAGE_REMOVED_ON_SAVE_NOTE = (
    "この画像は実行の再開のために外しました。画像の内容が要るときは、同じツールで読み直して"
    "ください。読み直せなければ、画像の内容は確認できていないものとして扱ってください。"
)


def _is_data_image(value: Mapping[str, Any]) -> bool:
    url = value.get("image_url")
    return value.get("type") in {"input_image", "image"} and (
        isinstance(url, str) and url.startswith("data:")
    )


def _strip(value: Any) -> tuple[Any, int]:
    if isinstance(value, Mapping):
        if _is_data_image(value):
            kind = "input_text" if value.get("type") == "input_image" else "text"
            return {"type": kind, "text": IMAGE_REMOVED_ON_SAVE_NOTE}, 1
        removed = 0
        result: dict[str, Any] = {}
        for key, item in value.items():
            result[key], count = _strip(item)
            removed += count
        return result, removed
    if isinstance(value, list):
        items: list[Any] = []
        removed = 0
        for item in value:
            stripped, count = _strip(item)
            items.append(stripped)
            removed += count
        return items, removed
    return value, 0


def strip_image_data(state_text: str) -> tuple[str, int]:
    """保存する SDK の状態（JSON の文字列）から画像のデータを外す（外した数も返す）。

    画像（`input_image` / SDK の `image` の出力）の data URL を、読み直しの案内の文に置き換える。
    画像の無い状態は文字列をそのまま返す。
    """
    if "data:" not in state_text:
        return state_text, 0
    try:
        data = json.loads(state_text)
    except ValueError:
        return state_text, 0
    stripped, removed = _strip(data)
    if not removed:
        return state_text, 0
    return json.dumps(stripped, ensure_ascii=False), removed


def data_url_image(url: str) -> tuple[str, bytes] | None:
    """data URL の画像の MIME とバイト列（data URL でなければ None）。"""
    if not url.startswith("data:"):
        return None
    header, _, payload = url.partition(",")
    if ";base64" not in header:
        return None
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    return header[5:].split(";", 1)[0].lower(), raw


__all__ = [
    "ATTACHED",
    "CONTENT_IMAGES_KEY",
    "IMAGE_INPUT_NOTE_KEY",
    "IMAGE_REMOVED_ON_SAVE_NOTE",
    "MAX_IMAGES_IN_INPUT",
    "MAX_IMAGES_PER_RESULT",
    "MAX_IMAGE_BYTES",
    "TOOL_IMAGE_MIME_TYPES",
    "UNSUPPORTED_MODEL",
    "ImageDelivery",
    "ToolImage",
    "ToolOutputWithImages",
    "data_url_image",
    "decode_image",
    "estimate_image_tokens",
    "model_tool_output",
    "plan_image_delivery",
    "strip_image_data",
]
