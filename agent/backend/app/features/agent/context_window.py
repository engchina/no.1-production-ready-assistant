"""モデルに渡す入力を、モデルの入力の上限に収める（コンテキストの管理。#2383）。

組み込み Runtime は Run の中のツールの結果をすべて次のモデルの呼び出しに積む（SDK の既定）。
根拠の多い検索を何回か呼ぶと、入力がモデルの入力の上限を超えて呼び出しが 400 で失敗する。
主要な実装の型（OpenAI Agents SDK の `call_model_input_filter` と `ToolOutputTrimmer`、Anthropic の
context editing が古いツールの結果から消す、LangChain の `trim_messages`）にそろえ、呼び出しの直前に
入力を見積もり、予算を超えるときだけ古いツールの結果から順に縮める。

- **予算**: 登録モデルの入力の上限（`max_input_tokens`。prompt と応答の合計）から、出力の上限
  （`max_output_tokens`。入力の上限の半分までを引く。小さいモデルで予算が 0 にならないように）と
  安全の余白（入力の上限 × `AGENT_CONTEXT_SAFETY_MARGIN_RATIO`）を引いた token の数。指示と
  ツールの定義は縮めず、残りに会話・ツールの結果を収める。
- **見積もり**: platform の `estimate_input_tokens`（3 製品で共通の 1 つ）に
  `AGENT_CONTEXT_TOKEN_ESTIMATE_SCALE` を掛ける。プロバイダが入力の超過を返したら、返った
  prompt の token 数との比で Run の残りの見積もりを補正する。
- **縮め方**: 古いツールの結果から順に、段階を 1 つずつ上げる（すべての古い結果を段階 1 にしても
  収まらなければ段階 2 …）。収まった時点で止める。最新のツールの結果（最後のツールの呼び出しの
  まとまり）は、古い結果をすべて縮めても収まらないときだけ段階 3 まで縮める。利用者の質問・会話の
  履歴・指示・モデルの発話は縮めない。
  1. 一覧の項目の長い文字列を先頭だけにする（`truncated` を持つ項目は true にする）
  2. 加えて、一覧の先頭の数件の後ろの項目を ID だけにする
  3. 一覧の項目をすべて ID だけにし、一覧の外の長い文字列も先頭だけにする
  4. ID だけを残す（古い結果だけ）
  どの段階でも ID の項目（`id`・`*_id`。根拠の `document_id`・`chunk_id`・`evidence_id` など）は
  消さない（引用の照合・回答の検証に使う）。次の手を選ぶのに要る作業の状態（`STATE_KEYS`。続きの
  cursor・読み終えたか・状態・結論・条件の名前と値と出典・除外の理由・版。#71）も、短い値
  （`STATE_VALUE_MAX_CHARS` 文字まで）なら元のまま残す（cursor を切ると続きを読めない）。
  未確認の点・分かった条件の文の一覧（`STATE_LIST_KEYS`）も短い文を `STATE_LIST_MAX_ITEMS` 件まで
  残す。
  長い自由文は残さない（本文と同じく縮める）。縮めた結果には `context_compaction`（元の文字数と
  次の手。読み直すツールがあればその名前）を足す。同じツールの呼び直しを案内するのは、信頼できる
  読み取り専用のツールだけ（副作用があり得るツールは、呼び直すと同じ操作をもう一度実行する。#2632）。
- Run の step に残すツールの結果は全文のまま（回答の検証・引用の照合は step を使う）。縮めるのは
  モデルに渡す入力だけで、呼び出しごとに元の入力から作り直す。
- **画像**（ツールの結果の `input_image`。#2650）: 見積もりは画像の大きさから（`tool_media`）。
  同じ画像は最新の結果の 1 つだけに添付し、入力の画像は新しい結果から `MAX_IMAGES_IN_INPUT` 枚まで
  にする（予算に関わらず）。予算を超えるときは、古い結果の画像を文字を縮める前に外し、最新の結果の
  画像は文字を縮めても足りないときだけ外す。外した画像は、ID と読み直しの手を書いた文に置き換える。

ここは見積もり・予算・縮め方だけを持つ（Run への記録と SDK・プロバイダへの組み込みは
`builtin_runtime`）。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from pr_system_settings.model import estimate_input_tokens

from app.features.agent.tool_media import (
    MAX_IMAGES_IN_INPUT,
    ToolImage,
    data_url_image,
    estimate_image_tokens,
)

# 縮めた結果に足す項目の名前。
COMPACTION_KEY = "context_compaction"
# 段階ごとの（一覧の項目の文字列の長さの上限, 一覧で全体を残す項目の数〔None はすべて〕）。
_LEVELS: dict[int, tuple[int, int | None]] = {1: (400, None), 2: (200, 5), 3: (100, 0)}
OLDER_MAX_LEVEL = 4
LATEST_MAX_LEVEL = 3
# JSON ではない（文字列の）結果の、段階ごとの残す長さ。
_TEXT_LIMITS = {1: 4000, 2: 1500, 3: 500, 4: 200}
# 一覧の外の文字列を段階 3 で残す長さ。
_TOP_LEVEL_TEXT_CHARS = 200
# 1 件の入力の項目ごとの見積もりの上乗せ（役割・区切りの token）。
_ITEM_OVERHEAD_TOKENS = 8
# プロバイダが入力の超過を返したが prompt の token 数が読めないときに、見積もりに掛ける倍率。
FALLBACK_PROVIDER_SCALE = 1.5
_ID_KEY = re.compile(r"(?:^|_)id$")
# どの段階でも元のまま残す作業の状態の項目（短い値だけ。#71）。続きの位置・読み終えたか・状態と
# 結論・条件（名前・値・出典・不明の扱い）・除外や不足の理由・版と置き換え。
STATE_KEYS = frozenset(
    {
        "next_cursor",
        "cursor",
        "has_more",
        "section_complete",
        "status",
        "state",
        "outcome",
        "error_code",
        "handling",
        "label",
        "value",
        "source",
        "reason",
        "insufficient_reason",
        "title",
        "version",
        "as_of",
        "superseded",
        "needs_human_review",
        "evidence_omitted",
    }
)
# 文の一覧で残す作業の状態（結論に効いた条件・資料で確かめられない点。#71）。
STATE_LIST_KEYS = frozenset({"conditions", "gaps"})
# 元のまま残す値の長さの上限（RAG の cursor の上限 256 文字に合わせる）と、一覧で残す件数。
STATE_VALUE_MAX_CHARS = 256
STATE_LIST_MAX_ITEMS = 10
_TOOL_OUTPUT_TYPES = {"function_call_output"}


@dataclass(frozen=True)
class ContextLimits:
    """Run のモデルの入力・出力の上限（tokens。システム設定 > モデル の登録モデルの値）。"""

    model_id: str
    max_input_tokens: int
    max_output_tokens: int


@dataclass
class ContextBudget:
    """入力の予算と見積もりの係数（係数はプロバイダの超過で Run の中で補正する）。"""

    limits: ContextLimits
    estimate_scale: float = 1.0
    margin_ratio: float = 0.05

    @property
    def output_reserve_tokens(self) -> int:
        return max(0, min(self.limits.max_output_tokens, self.limits.max_input_tokens // 2))

    @property
    def margin_tokens(self) -> int:
        return max(0, math.ceil(self.limits.max_input_tokens * self.margin_ratio))

    def input_tokens(self, max_input_tokens: int | None = None) -> int:
        """会話・ツールの結果と指示・ツールの定義に使える token の数。"""
        limit = self.limits.max_input_tokens
        if max_input_tokens is not None and max_input_tokens > 0:
            limit = min(limit, max_input_tokens)
        return max(0, limit - self.output_reserve_tokens - self.margin_tokens)

    def estimate(self, text: str) -> int:
        return math.ceil(estimate_input_tokens(text) * self.estimate_scale)


@dataclass(frozen=True)
class CompactedOutput:
    """縮めたツールの結果 1 件（Run の記録に残す）。

    `level` は文字を縮めた段階（0 は文字を縮めず画像だけを外した）。`images_removed` は外した画像の
    数、`image_reason` はその理由（`duplicate`＝後の結果に同じ画像 / `image_limit`＝入力の画像の
    枚数の上限 / `budget`＝入力の予算）。
    """

    call_id: str
    tool_name: str
    level: int
    original_chars: int
    compacted_chars: int
    latest: bool
    images_removed: int = 0
    image_reason: str | None = None

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "level": self.level,
            "original_chars": self.original_chars,
            "compacted_chars": self.compacted_chars,
            "latest": self.latest,
        }
        if self.images_removed:
            record["images_removed"] = self.images_removed
            record["image_reason"] = self.image_reason
        return record


@dataclass
class CompactionResult:
    """縮めた入力と、予算・見積もりの前後。"""

    items: list[Any]
    budget_tokens: int
    fixed_tokens: int
    tokens_before: int
    tokens_after: int
    outputs: list[CompactedOutput] = field(default_factory=list)
    # 入力に残した画像の数と、その token の見積もり（#2650）。
    images: int = 0
    image_tokens: int = 0

    @property
    def fits(self) -> bool:
        return self.tokens_after <= self.budget_tokens

    @property
    def fixed_exceeds(self) -> bool:
        """指示とツールの定義だけで予算を超えるか（ツールの結果を縮めても収まらない）。"""
        return self.fixed_tokens > self.budget_tokens


def _as_dict(item: Any) -> Any:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        dumped = item.model_dump(exclude_none=True)
        return dumped if isinstance(dumped, dict) else item
    return item


def _output_texts(output: Sequence[Any]) -> list[str]:
    """文字と画像の出力の列（#2650）の文字の部分。"""
    texts: list[str] = []
    for part in output:
        data = _as_dict(part)
        if isinstance(data, dict) and data.get("type") in {"input_text", "text"}:
            texts.append(str(data.get("text") or ""))
    return texts


def _output_images(output: Sequence[Any]) -> list[tuple[int, str]]:
    """文字と画像の出力の列の画像（位置と data URL か URL）。"""
    images: list[tuple[int, str]] = []
    for index, part in enumerate(output):
        data = _as_dict(part)
        if isinstance(data, dict) and data.get("type") in {"input_image", "image"}:
            url = data.get("image_url")
            images.append((index, url if isinstance(url, str) else ""))
    return images


@lru_cache(maxsize=64)
def _image_facts(url: str) -> tuple[str, int]:
    """画像の ID（sha256）と token の見積もり（data URL でなければ ID は空、見積もりは既定）。"""
    decoded = data_url_image(url)
    if decoded is None:
        return "", estimate_image_tokens(None, None)
    mime, raw = decoded
    image = ToolImage(data=raw, mime_type=mime)
    return image.image_id, image.estimated_tokens


def _image_tokens(item: Any) -> int:
    data = _as_dict(item)
    if not isinstance(data, dict) or data.get("type") not in _TOOL_OUTPUT_TYPES:
        return 0
    output = data.get("output")
    if not isinstance(output, list):
        return 0
    return sum(_image_facts(url)[1] for _index, url in _output_images(output))


def _item_text(item: Any) -> str:
    data = _as_dict(item)
    if isinstance(data, dict):
        output = data.get("output")
        if data.get("type") in _TOOL_OUTPUT_TYPES and isinstance(output, str):
            return output
        if data.get("type") in _TOOL_OUTPUT_TYPES and isinstance(output, list):
            # 画像は文字として数えない（`_image_tokens` が大きさから見積もる）。
            return "\n".join(_output_texts(output))
        content = data.get("content")
        if isinstance(content, str) and set(data) <= {"role", "content", "type"}:
            return content
        return json.dumps(data, ensure_ascii=False, default=str)
    return str(item)


def tools_text(tools: Sequence[Any]) -> str:
    """ツールの定義（名前・説明・引数の schema）を見積もる文字列にする。"""
    parts: list[str] = []
    for tool in tools:
        data = _as_dict(tool)
        if isinstance(data, dict):
            parts.append(json.dumps(data, ensure_ascii=False, default=str))
            continue
        parts.append(
            json.dumps(
                {
                    "name": getattr(tool, "name", ""),
                    "description": getattr(tool, "description", ""),
                    "parameters": getattr(tool, "params_json_schema", None),
                },
                ensure_ascii=False,
                default=str,
            )
        )
    return "\n".join(parts)


def is_id_key(key: object) -> bool:
    """ID の項目か（`id`・`*_id`）。縮めても消さない。"""
    return isinstance(key, str) and bool(_ID_KEY.search(key))


def _is_state(key: object, value: object) -> bool:
    """縮めずに元のまま残す作業の状態か（`STATE_KEYS` の短い値。#71）。"""
    if not isinstance(key, str) or key not in STATE_KEYS:
        return False
    if isinstance(value, str):
        return len(value) <= STATE_VALUE_MAX_CHARS
    return value is None or isinstance(value, bool | int | float)


def _state_list(key: object, value: object) -> list[str] | None:
    """残す文の一覧（`STATE_LIST_KEYS` の短い文。当たらなければ None）。"""
    if not isinstance(key, str) or key not in STATE_LIST_KEYS or not isinstance(value, list):
        return None
    texts = [item for item in value if isinstance(item, str)]
    if len(texts) != len(value) or any(len(item) > STATE_VALUE_MAX_CHARS for item in texts):
        return None
    return texts[:STATE_LIST_MAX_ITEMS]


def _id_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    """ID と作業の状態だけの参照（一覧の項目を縮めるときに残す）。"""
    ref: dict[str, Any] = {}
    for key, value in item.items():
        identifier = is_id_key(key) and isinstance(value, str | int) and not isinstance(value, bool)
        if identifier or _is_state(key, value):
            ref[key] = value
        elif (texts := _state_list(key, value)) is not None:
            ref[key] = texts
    return ref


@dataclass
class _Stats:
    clipped: int = 0
    id_only: int = 0
    dropped: int = 0


def _clip(text: str, chars: int, stats: _Stats) -> str:
    if len(text) <= chars:
        return text
    stats.clipped += 1
    return text[:chars] + "…"


def _shrink(value: Any, *, level: int, stats: _Stats, in_list: bool = False) -> Any:
    chars, keep = _LEVELS[level]
    if isinstance(value, str):
        if in_list:
            return _clip(value, chars, stats)
        return _clip(value, _TOP_LEVEL_TEXT_CHARS, stats) if level >= 3 else value
    if isinstance(value, list):
        shrunk: list[Any] = []
        for index, item in enumerate(value):
            if keep is not None and index >= keep:
                ref = _id_ref(item) if isinstance(item, dict) else {}
                if ref:
                    shrunk.append(ref)
                    stats.id_only += 1
                else:
                    stats.dropped += 1
                continue
            shrunk.append(_shrink(item, level=level, stats=stats, in_list=True))
        return shrunk
    if isinstance(value, dict):
        clipped = stats.clipped
        result = {
            key: item
            if is_id_key(key) or _is_state(key, item) or _state_list(key, item) == item
            else _shrink(item, level=level, stats=stats, in_list=in_list)
            for key, item in value.items()
            if key != COMPACTION_KEY
        }
        if isinstance(result.get("truncated"), bool) and stats.clipped > clipped:
            result["truncated"] = True
        return result
    return value


def _ids_only(value: Any, stats: _Stats) -> Any:
    if isinstance(value, dict):
        kept: dict[str, Any] = _id_ref(value)
        for key, item in value.items():
            if key in kept or key == COMPACTION_KEY:
                continue
            if isinstance(item, dict | list):
                nested = _ids_only(item, stats)
                if nested:
                    kept[key] = nested
        return kept
    if isinstance(value, list):
        refs: list[Any] = []
        for item in value:
            ref = _ids_only(item, stats) if isinstance(item, dict) else None
            if ref:
                refs.append(ref)
                stats.id_only += 1
            else:
                stats.dropped += 1
        return refs
    return None


def _note(
    original_chars: int, stats: _Stats, read_tool: str | None, rerunnable: bool
) -> dict[str, Any]:
    done: list[str] = []
    if stats.clipped:
        done.append(f"長い文字列 {stats.clipped} 件を先頭だけ")
    if stats.id_only:
        done.append(f"{stats.id_only} 件を ID だけ")
    if stats.dropped:
        done.append(f"ID の無い {stats.dropped} 件を省略")
    summary = "、".join(done) or "一部を省略"
    text = (
        f"この結果は長いため縮めました（元は {original_chars} 文字。{summary}）。"
        "ID の項目は元のまま残しているので、引用にそのまま使えます。続きの cursor・状態・条件・"
        "除外の理由などの短い値も元のままです。"
    )
    if read_tool:
        text += (
            f"本文が要る根拠は、document_id と chunk_id を {read_tool} に渡して読み直してください。"
        )
    elif rerunnable:
        text += "省いた内容が要るときは、対象を絞って同じツールを呼び直してください。"
    else:
        # 副作用があり得るツール（書き込み・注釈を信頼しない接続）の結果は、呼び直すと同じ操作を
        # もう一度実行する（#2632）。省いた内容を得る手段として呼び直させない。
        text += (
            "省いた内容は読み直せません。このツールは呼び直さず、残っている内容で続けてください。"
        )
    return {"original_chars": original_chars, "next_step": text}


def compact_output(
    output: str, level: int, *, read_tool: str | None = None, rerunnable: bool = False
) -> str:
    """ツールの結果（JSON の文字列か文字列）を段階 `level` で縮める（縮まなければ元のまま）。

    `read_tool` は本文を読み直すツール、`rerunnable` は同じツールを呼び直しても副作用が無いか
    （信頼できる読み取り専用。既定は False で、呼び直しを案内しない）。
    """
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        previous = data.get(COMPACTION_KEY)
        original_chars = (
            int(previous["original_chars"])
            if isinstance(previous, dict) and isinstance(previous.get("original_chars"), int)
            else len(output)
        )
        stats = _Stats()
        shrunk = (
            _ids_only(data, stats)
            if level >= OLDER_MAX_LEVEL
            else _shrink(data, level=level, stats=stats)
        )
        compacted = json.dumps(
            {**shrunk, COMPACTION_KEY: _note(original_chars, stats, read_tool, rerunnable)},
            ensure_ascii=False,
            default=str,
        )
        return compacted if len(compacted) < len(output) else output
    limit = _TEXT_LIMITS[min(level, OLDER_MAX_LEVEL)]
    if len(output) <= limit:
        return output
    return (
        f"{output[:limit]}\n…（この結果は長いため、{len(output)} 文字のうち先頭の {limit} 文字"
        "だけを渡しています。）"
    )


def _call_names(items: Sequence[Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in items:
        data = _as_dict(item)
        if isinstance(data, dict) and data.get("type") == "function_call":
            call_id, name = data.get("call_id"), data.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                names[call_id] = name
    return names


def _latest_outputs(items: Sequence[Any], outputs: Sequence[int]) -> set[int]:
    """最新のツールの呼び出しのまとまりの結果（最後のツールの結果と、その前に続くツールの結果）。

    SDK は 1 回の応答のツールの呼び出しを並べた後に結果を並べる（`呼び出し, 呼び出し, 結果,
    結果`）ので、続く結果のまとまりが同じ応答の呼び出しの結果になる（前の応答の結果とは、
    次の応答の呼び出しで区切られる）。
    """
    if not outputs:
        return set()
    index = outputs[-1]
    while index > 0:
        data = _as_dict(items[index - 1])
        if not isinstance(data, dict) or data.get("type") not in _TOOL_OUTPUT_TYPES:
            break
        index -= 1
    return {position for position in outputs if position >= index}


def _item_tokens(item: Any, budget: ContextBudget) -> int:
    return (
        budget.estimate(_item_text(item))
        + math.ceil(_image_tokens(item) * budget.estimate_scale)
        + _ITEM_OVERHEAD_TOKENS
    )


def _is_tool_output(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and item.get("type") in _TOOL_OUTPUT_TYPES
        and isinstance(item.get("output"), str | list)
    )


def _primary_text(item: Mapping[str, Any]) -> str:
    """縮める対象の文字（文字の出力そのもの、文字と画像の列なら最初の文字）。"""
    output = item.get("output")
    if isinstance(output, str):
        return output
    texts = _output_texts(output if isinstance(output, list) else [])
    return texts[0] if texts else ""


def _with_primary_text(item: Mapping[str, Any], text: str) -> dict[str, Any]:
    output = item.get("output")
    if isinstance(output, str):
        return {**item, "output": text}
    parts = list(output if isinstance(output, list) else [])
    for index, part in enumerate(parts):
        data = _as_dict(part)
        if isinstance(data, dict) and data.get("type") in {"input_text", "text"}:
            parts[index] = {"type": "input_text", "text": text}
            break
    return {**item, "output": parts}


_IMAGE_REMOVED_NOTES = {
    "duplicate": "同じ画像（{image_id}）を後のツールの結果で渡しているため、ここでは外しました。",
    "image_limit": (
        "モデルの入力に添付する画像は新しい結果から {limit} 枚までのため、画像（{image_id}）を"
        "外しました。"
    ),
    "budget": "モデルの入力の上限に収めるため、画像（{image_id}）を外しました。",
}


def _image_note(reason: str, image_id: str, read_tool: str | None, rerunnable: bool) -> str:
    text = _IMAGE_REMOVED_NOTES[reason].format(
        image_id=image_id or "ID 不明", limit=MAX_IMAGES_IN_INPUT
    )
    if reason == "duplicate":
        return text
    if read_tool:
        return text + f"画像の内容が要るときは {read_tool} で画像を読み直してください。"
    if rerunnable:
        return text + "画像の内容が要るときは、同じツールを呼び直してください。"
    return (
        text + "この画像は読み直せないため、画像の内容は確認できていないものとして扱ってください。"
    )


def _without_images(
    item: Mapping[str, Any],
    positions: Collection[int],
    reason: str,
    *,
    read_tool: str | None,
    rerunnable: bool,
) -> dict[str, Any]:
    """指定の位置の画像を、外した理由と読み直しの手の文に置き換える。"""
    output = item.get("output")
    parts = list(output if isinstance(output, list) else [])
    for index, url in _output_images(parts):
        if index in positions:
            parts[index] = {
                "type": "input_text",
                "text": _image_note(reason, _image_facts(url)[0], read_tool, rerunnable),
            }
    return {**item, "output": parts}


def estimate_tokens(items: Sequence[Any], *, budget: ContextBudget, fixed_text: str) -> int:
    """入力（指示・ツールの定義と、入力の項目）の token の見積もり。"""
    return budget.estimate(fixed_text) + sum(_item_tokens(_as_dict(item), budget) for item in items)


@dataclass(frozen=True)
class InputComposition:
    """モデルの 1 回の呼び出しの入力の、部分ごとの token の見積もり（#2583。本文は持たない）。

    - `static_instructions`: 業務 Agent の指示・スキル・データの範囲の案内（Run の中で変わらない
      部分）
    - `dynamic_state`: 指示の末尾に足す Run の状態（支援タスクの状態・取り消した確認・添付の索引・
      回答前の確認）
    - `tool_definitions`: ツールの定義（名前・説明・引数の schema）
    - `history`: 会話の履歴・利用者の質問・モデルの発話とツールの呼び出し
    - `tool_outputs`: ツールの結果（縮めた後。画像の見積もりを含む）
    """

    static_instructions_tokens: int
    dynamic_state_tokens: int
    tool_definitions_tokens: int
    history_tokens: int
    tool_outputs_tokens: int
    items: int

    @property
    def total_tokens(self) -> int:
        return (
            self.static_instructions_tokens
            + self.dynamic_state_tokens
            + self.tool_definitions_tokens
            + self.history_tokens
            + self.tool_outputs_tokens
        )


def split_instructions(instructions: str, static_instructions: str | None) -> tuple[str, str]:
    """指示を（変わらない部分, Run の状態の部分）に分ける。

    Run の状態は変わらない部分の後ろに足すので、`static_instructions` が先頭に一致するときだけ
    分ける（分からなければ全体を変わらない部分とする）。
    """
    if static_instructions and instructions.startswith(static_instructions):
        return static_instructions, instructions[len(static_instructions) :]
    return instructions, ""


def input_composition(
    items: Any,
    *,
    budget: ContextBudget,
    instructions: str,
    static_instructions: str | None,
    tools: Sequence[Any],
) -> InputComposition:
    """モデルに送る入力の部分ごとの見積もり（予算と同じ見積もり。部分ごとに切り上げる）。"""
    static, dynamic = split_instructions(instructions, static_instructions)
    entries = list(items) if isinstance(items, list) else ([items] if items else [])
    history = 0
    outputs = 0
    for entry in entries:
        data = _as_dict(entry)
        size = _item_tokens(data, budget)
        if isinstance(data, dict) and data.get("type") in _TOOL_OUTPUT_TYPES:
            outputs += size
        else:
            history += size
    return InputComposition(
        static_instructions_tokens=budget.estimate(static),
        dynamic_state_tokens=budget.estimate(dynamic),
        tool_definitions_tokens=budget.estimate(tools_text(tools)) if tools else 0,
        history_tokens=history,
        tool_outputs_tokens=outputs,
        items=len(entries),
    )


def compact_items(
    items: Sequence[Any],
    *,
    budget: ContextBudget,
    fixed_text: str,
    read_tool_for: Callable[[str], str | None] | None = None,
    rerunnable: Callable[[str], bool] | None = None,
    max_input_tokens: int | None = None,
) -> CompactionResult:
    """入力の項目を予算に収める（元の項目は変えない。収まらなければ縮められるだけ縮めて返す）。

    `read_tool_for` はツールの名前 → 本文を読み直すツール、`rerunnable` はツールの名前 → 同じ
    ツールを呼び直しても副作用が無いか（無ければどのツールも呼び直しを案内しない）。
    画像の重複と枚数の上限は予算に関わらず先に直す（#2650）。
    """
    budget_tokens = budget.input_tokens(max_input_tokens)
    fixed_tokens = budget.estimate(fixed_text)
    sized: list[Any] = [_as_dict(item) for item in items]
    compacted: list[Any] = list(items)
    names = _call_names(sized)
    outputs = [index for index, item in enumerate(sized) if _is_tool_output(item)]
    latest = _latest_outputs(sized, outputs)
    levels: dict[int, int] = {}
    removed: dict[int, tuple[int, str]] = {}

    def hints(index: int) -> tuple[str | None, bool]:
        name = names.get(str(sized[index].get("call_id") or ""), "")
        read_tool = read_tool_for(name) if read_tool_for is not None and name else None
        return read_tool, bool(name) and rerunnable is not None and rerunnable(name)

    def drop_images(index: int, positions: Collection[int], reason: str) -> None:
        if not positions:
            return
        read_tool, safe_to_rerun = hints(index)
        compacted[index] = _without_images(
            _as_dict(compacted[index]),
            positions,
            reason,
            read_tool=read_tool,
            rerunnable=safe_to_rerun,
        )
        count = removed.get(index, (0, reason))[0]
        removed[index] = (count + len(positions), reason)

    # 画像の重複と枚数の上限（新しい結果の画像から残す）。
    seen: set[str] = set()
    kept = 0
    for index in reversed(outputs):
        output = sized[index].get("output")
        if not isinstance(output, list):
            continue
        duplicates: list[int] = []
        over: list[int] = []
        for position, url in reversed(_output_images(output)):
            image_id = _image_facts(url)[0] or url
            if image_id in seen:
                duplicates.append(position)
                continue
            seen.add(image_id)
            if kept >= MAX_IMAGES_IN_INPUT:
                over.append(position)
                continue
            kept += 1
        drop_images(index, duplicates, "duplicate")
        drop_images(index, over, "image_limit")

    before = fixed_tokens + sum(_item_tokens(item, budget) for item in sized)
    tokens = [_item_tokens(_as_dict(item), budget) for item in compacted]
    total = fixed_tokens + sum(tokens)

    def refresh(index: int) -> None:
        nonlocal total
        size = _item_tokens(_as_dict(compacted[index]), budget)
        total += size - tokens[index]
        tokens[index] = size

    def apply(index: int, level: int) -> None:
        item = _as_dict(compacted[index])
        text = _primary_text(item)
        read_tool, safe_to_rerun = hints(index)
        shrunk = compact_output(text, level, read_tool=read_tool, rerunnable=safe_to_rerun)
        if shrunk == text:
            return
        compacted[index] = _with_primary_text(item, shrunk)
        refresh(index)
        levels[index] = level

    def remove_images(group: Sequence[int]) -> None:
        for index in group:
            if total <= budget_tokens:
                return
            output = _as_dict(compacted[index]).get("output")
            positions = [
                p for p, _url in _output_images(output if isinstance(output, list) else [])
            ]
            if positions:
                drop_images(index, positions, "budget")
                refresh(index)

    if total > budget_tokens:
        older = [index for index in outputs if index not in latest]
        newest = [index for index in outputs if index in latest]
        # 古い結果の画像は、文字を縮める前に外す（画像は文字よりずっと大きい）。
        remove_images(older)
        for group, max_level in ((older, OLDER_MAX_LEVEL), (newest, LATEST_MAX_LEVEL)):
            for level in range(1, max_level + 1):
                for index in group:
                    if total <= budget_tokens:
                        break
                    apply(index, level)
            if total <= budget_tokens:
                break
        # 最新の結果の画像は、文字を縮めても足りないときだけ外す。
        remove_images(newest)

    remaining = [
        url
        for item in (_as_dict(entry) for entry in compacted)
        if _is_tool_output(item) and isinstance(item.get("output"), list)
        for _position, url in _output_images(item["output"])
    ]
    return CompactionResult(
        items=compacted,
        budget_tokens=budget_tokens,
        fixed_tokens=fixed_tokens,
        tokens_before=before,
        tokens_after=total,
        outputs=[
            CompactedOutput(
                call_id=str(sized[index].get("call_id") or ""),
                tool_name=names.get(str(sized[index].get("call_id") or ""), ""),
                level=levels.get(index, 0),
                original_chars=len(_primary_text(sized[index])),
                compacted_chars=len(_primary_text(_as_dict(compacted[index]))),
                latest=index in latest,
                images_removed=removed[index][0] if index in removed else 0,
                image_reason=removed[index][1] if index in removed else None,
            )
            for index in sorted({*levels, *removed})
        ],
        images=len(remaining),
        image_tokens=math.ceil(
            sum(_image_facts(url)[1] for url in remaining) * budget.estimate_scale
        ),
    )


# プロバイダが返す入力の超過の文（vLLM・OpenAI 互換 API）。
_OVERFLOW_CODES = {"context_length_exceeded", "string_above_max_length", "input_too_long"}
_OVERFLOW_TEXT = re.compile(
    r"max_model_len|context length|context window|maximum context|prompt is too long"
    r"|prompt length|too many (?:input )?tokens|input (?:is )?too long"
    r"|reduce (?:the )?(?:prompt|input)",
    re.IGNORECASE,
)
_PROMPT_AND_LIMIT = (
    # vLLM: "The engine prompt length 160066 exceeds the max_model_len 131072."
    re.compile(r"prompt length (\d+) exceeds the max_model_len (\d+)", re.IGNORECASE),
    # OpenAI 互換: "maximum context length is 131072 tokens. However, ... resulted in 160066 tokens"
    re.compile(
        r"maximum context length is (?P<limit>\d+) tokens.*?(?P<prompt>\d+) tokens",
        re.IGNORECASE | re.DOTALL,
    ),
)


@dataclass(frozen=True)
class ProviderOverflow:
    """プロバイダが返した入力の超過（prompt の token 数と上限。読めなければ None）。"""

    prompt_tokens: int | None = None
    limit_tokens: int | None = None


def provider_overflow(code: str | None, message: str | None) -> ProviderOverflow | None:
    """プロバイダのエラーが入力の超過なら、その token 数（超過でなければ None）。"""
    text = message or ""
    if (code or "") not in _OVERFLOW_CODES and not _OVERFLOW_TEXT.search(text):
        return None
    vllm, openai_style = _PROMPT_AND_LIMIT
    if match := vllm.search(text):
        return ProviderOverflow(prompt_tokens=int(match.group(1)), limit_tokens=int(match.group(2)))
    if match := openai_style.search(text):
        return ProviderOverflow(
            prompt_tokens=int(match.group("prompt")), limit_tokens=int(match.group("limit"))
        )
    return ProviderOverflow()


def overflow_message(result: CompactionResult, budget: ContextBudget) -> str:
    """縮めても収まらないときの、利用者に分かる失敗の理由と直し方。"""
    limits = budget.limits
    numbers = (
        f"見積もり 約 {result.tokens_after:,} tokens、使える量 {result.budget_tokens:,} tokens"
        f"（入力の上限 {limits.max_input_tokens:,} から出力の分 {budget.output_reserve_tokens:,} と"
        f"余白 {budget.margin_tokens:,} を引いた量）"
    )
    if result.fixed_exceeds:
        return (
            f"業務 Agent の指示とツールの定義だけで、モデル「{limits.model_id}」の入力の上限に"
            f"収まりません（{numbers}）。業務 Agent のスキル・指示を減らすか、"
            "システム設定 > モデル で入力の上限の大きいモデルを業務 Agent に設定してください。"
        )
    return (
        f"モデルに渡す入力（会話とツールの結果）が、モデル「{limits.model_id}」の入力の上限に"
        f"収まりません（ツールの結果を縮めた後も{numbers}）。質問の対象（文書・期間・条件）を"
        "絞って質問し直すか、システム設定 > モデル で入力の上限の大きいモデルを業務 Agent に"
        "設定してください。"
    )


def provider_overflow_message(budget: ContextBudget, overflow: ProviderOverflow) -> str:
    """ツールの結果を縮めて送り直しても、プロバイダが入力の超過を返したときの失敗の理由。"""
    counted = (
        f"（プロバイダが数えた入力 {overflow.prompt_tokens:,} tokens、"
        f"上限 {overflow.limit_tokens:,} tokens）"
        if overflow.prompt_tokens is not None and overflow.limit_tokens is not None
        else ""
    )
    return (
        f"モデルに渡す入力が、モデル「{budget.limits.model_id}」の入力の上限を超えました{counted}。"
        "ツールの結果を縮めて送り直しましたが収まりませんでした。質問の対象（文書・期間・条件）を"
        "絞って質問し直すか、システム設定 > モデル で入力の上限を実際のモデルに合わせる・"
        "入力の上限の大きいモデルを業務 Agent に設定してください。"
    )


__all__ = [
    "COMPACTION_KEY",
    "FALLBACK_PROVIDER_SCALE",
    "CompactedOutput",
    "CompactionResult",
    "ContextBudget",
    "ContextLimits",
    "InputComposition",
    "ProviderOverflow",
    "compact_items",
    "compact_output",
    "estimate_tokens",
    "input_composition",
    "is_id_key",
    "overflow_message",
    "provider_overflow",
    "provider_overflow_message",
    "split_instructions",
    "tools_text",
]
