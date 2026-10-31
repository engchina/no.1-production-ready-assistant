"""モデルが受け付けない LLM の要求の引数を外して送り直す部品（標準 library だけ。#3539）。

業界の型（LiteLLM の ``drop_params``）にそろえる。OpenAI 互換の API が HTTP 400 で
「受け付けない引数」（``Unsupported parameter: 'temperature'`` /
``Unknown parameter: 'seed'`` など）を返したら、その引数が許可の一覧
（:data:`DROPPABLE_PARAMETERS`）にあり、要求の top-level にあるときだけ外して
1 回だけ送り直す。外した引数は（接続の URL, モデル ID）ごとにプロセスの間だけ覚え、
次の呼び出しから最初に外す。

- 外してよいのは、サンプリングと生成の調整（``temperature``・``top_p`` など）と、
  引数そのものを受け付けない場合の推論の量（``reasoning`` / ``reasoning_effort``）だけ。
  ``model``・``input`` / ``messages``・応答の形式・``tools``・出力の上限などは外さない
  （許可の一覧に無い引数の 400 は今までどおり失敗にする）。
- 推論の量の「値」を拒む 400（``Unsupported value``）は、利用者が選んだ値の誤りなので
  外さない（モデルのテストで利用者に見せる。#1425）。サンプリングの引数は値を拒まれても
  外す（推論モデルは既定の値しか受け付けない）。

HTTP の client への組み込みは :mod:`pr_backend_core.llm_request_params_httpx`。
この module は httpx・HTTP の client に依存しない（判定・記憶・payload の絞り込みだけ）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

__all__ = [
    "DROPPABLE_PARAMETERS",
    "DROPPABLE_REASONING_PARAMETERS",
    "DROPPABLE_SAMPLING_PARAMETERS",
    "RejectedParameter",
    "collect_dropped_parameters",
    "droppable_parameter",
    "endpoint_key",
    "has_remembered_parameters",
    "note_dropped_parameters",
    "rejected_parameter",
    "remember_dropped_parameter",
    "remembered_parameters",
    "reset_dropped_parameters",
    "without_parameters",
]

logger = logging.getLogger(__name__)

# サンプリングと生成の調整。推論モデルは推論の量が none 以外のとき、これらを受け付けない
# （OpenAI の文書）。
DROPPABLE_SAMPLING_PARAMETERS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "frequency_penalty",
        "presence_penalty",
        "logprobs",
        "top_logprobs",
        "logit_bias",
        "seed",
    }
)
# 推論の量。引数そのものを受け付けないモデル（推論しないモデル）のときだけ外す。
DROPPABLE_REASONING_PARAMETERS = frozenset({"reasoning", "reasoning_effort"})
DROPPABLE_PARAMETERS = DROPPABLE_SAMPLING_PARAMETERS | DROPPABLE_REASONING_PARAMETERS

RejectionKind = Literal["unsupported_parameter", "unsupported_value"]


@dataclass(frozen=True, slots=True)
class RejectedParameter:
    """400 の本文が名指しした引数（``name`` は要求の top-level の名前）。"""

    name: str
    kind: RejectionKind


# message の中の引数の名前（``'temperature'`` / ``"reasoning.effort"``）。
_QUOTED_NAME = r"""['"`]([A-Za-z_][A-Za-z0-9_.\[\]]*)['"`]"""
_UNSUPPORTED_PARAMETER_PATTERNS = (
    re.compile(
        r"(?:unsupported|unknown|unrecognized)\s+(?:request\s+)?(?:parameter|argument)s?\b[^'\"`]*"
        + _QUOTED_NAME,
        re.IGNORECASE,
    ),
    # Azure OpenAI の形: ``Unrecognized request argument supplied: temperature``
    re.compile(
        r"unrecognized\s+request\s+arguments?\s+supplied:\s*([A-Za-z_][A-Za-z0-9_.]*)",
        re.IGNORECASE,
    ),
    # ``'temperature' is not supported with this model``（名前が先に来る形）
    re.compile(_QUOTED_NAME + r"\s+is\s+not\s+(?:supported|permitted|allowed)\b", re.IGNORECASE),
)
_UNSUPPORTED_VALUE_PATTERN = re.compile(
    r"unsupported\s+value\b[^'\"`]*" + _QUOTED_NAME, re.IGNORECASE
)
_UNSUPPORTED_PARAMETER_CODES = frozenset(
    {"unsupported_parameter", "unknown_parameter", "unrecognized_parameter"}
)
_UNSUPPORTED_VALUE_CODES = frozenset({"unsupported_value"})


def _top_level(name: str) -> str:
    """``reasoning.effort`` / ``input[0].content`` を top-level の名前にする。"""
    return re.split(r"[.\[]", name.strip(), maxsplit=1)[0]


def _error_object(body: object) -> Mapping[str, Any] | None:
    if isinstance(body, bytes | bytearray):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return {"message": body}
    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    if isinstance(error, Mapping):
        return error
    if isinstance(error, str):
        return {"message": error}
    return body if "message" in body else None


def rejected_parameter(status_code: int, body: object) -> RejectedParameter | None:
    """HTTP 400 の本文（OpenAI 形式）から、受け付けないと名指しされた引数を返す。

    ``error.param`` を先に、無ければ message の引用（``Unsupported parameter: 'X'`` など）を
    読む。message・``error.code`` が「受け付けない引数」か「受け付けない値」を示さない 400
    （型の誤りなど）と、400 以外は None。
    """
    if status_code != 400:
        return None
    error = _error_object(body)
    if error is None:
        return None
    message = str(error.get("message") or "")
    code = str(error.get("code") or "").strip().lower()
    param = error.get("param")
    kind: RejectionKind | None = None
    quoted = ""
    if code in _UNSUPPORTED_PARAMETER_CODES:
        kind = "unsupported_parameter"
    elif code in _UNSUPPORTED_VALUE_CODES:
        kind = "unsupported_value"
    # 「受け付けない値」を先に見る（``Unsupported value: 'X' is not supported …`` を
    # 引数の拒否と取り違えない）。
    value_match = _UNSUPPORTED_VALUE_PATTERN.search(message)
    if value_match:
        kind = kind or "unsupported_value"
        quoted = value_match.group(1)
    else:
        for pattern in _UNSUPPORTED_PARAMETER_PATTERNS:
            match = pattern.search(message)
            if match:
                kind = kind or "unsupported_parameter"
                quoted = match.group(1)
                break
    if kind is None:
        return None
    name = param if isinstance(param, str) and param.strip() else quoted
    top = _top_level(name) if name else ""
    return RejectedParameter(name=top, kind=kind) if top else None


def droppable_parameter(
    rejection: RejectedParameter | None, payload: Mapping[str, Any]
) -> str | None:
    """拒まれた引数を外してよいなら、その top-level の名前を返す。

    許可の一覧にあり、要求の top-level にあるときだけ。推論の量は「引数を受け付けない」
    ときだけ（値の誤りは外さない）。
    """
    if rejection is None or rejection.name not in payload:
        return None
    if rejection.name in DROPPABLE_SAMPLING_PARAMETERS:
        return rejection.name
    if (
        rejection.name in DROPPABLE_REASONING_PARAMETERS
        and rejection.kind == "unsupported_parameter"
    ):
        return rejection.name
    return None


def endpoint_key(url: str) -> str:
    """覚える単位の接続の URL（scheme・host・port・path。query と末尾の / は使わない）。"""
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


_lock = threading.Lock()
_dropped: dict[tuple[str, str], frozenset[str]] = {}


def remembered_parameters(url: str, model_id: str) -> frozenset[str]:
    """この接続とモデルで、受け付けないと覚えた引数（無ければ空）。"""
    if not _dropped:
        return frozenset()
    with _lock:
        return _dropped.get((endpoint_key(url), model_id.strip()), frozenset())


def has_remembered_parameters() -> bool:
    """どれかの接続・モデルで覚えた引数があるか（無ければ要求の本文を読まずに済ませる）。"""
    return bool(_dropped)


def remember_dropped_parameter(url: str, model_id: str, name: str) -> bool:
    """受け付けない引数を覚える。初めてのときだけ True を返し、WARNING の log を残す。

    log は接続・モデル・引数ごとに 1 回だけで、payload は出さない。
    """
    key = (endpoint_key(url), model_id.strip())
    with _lock:
        current = _dropped.get(key, frozenset())
        if name in current:
            return False
        _dropped[key] = current | {name}
    logger.warning(
        "モデルが受け付けない引数を外します（このプロセスでは以後このモデルに送りません）: "
        "model=%s parameter=%s endpoint_host=%s",
        key[1],
        name,
        urlsplit(url).hostname or "",
    )
    return True


def reset_dropped_parameters() -> None:
    """覚えた引数を消す（テスト用。テストの間で状態を持ち越さない）。"""
    with _lock:
        _dropped.clear()


def without_parameters(
    payload: Mapping[str, Any], names: frozenset[str] | set[str]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """payload から ``names`` の top-level の引数を外した写しと、実際に外した名前を返す。"""
    removed = tuple(key for key in payload if key in names)
    return {key: value for key, value in payload.items() if key not in names}, removed


# モデルのテストなどが「この呼び出しで外した引数」を知るための入れ物（contextvars。asyncio の task・
# ``asyncio.to_thread`` に引き継がれる）。
_collector: ContextVar[set[str] | None] = ContextVar("pr_dropped_llm_parameters", default=None)


@contextmanager
def collect_dropped_parameters() -> Iterator[set[str]]:
    """この中の呼び出しで外した引数（覚えていて最初から外したものを含む）を集める。"""
    names: set[str] = set()
    token = _collector.set(names)
    try:
        yield names
    finally:
        _collector.reset(token)


def note_dropped_parameters(names: tuple[str, ...] | set[str] | frozenset[str]) -> None:
    """外した引数を、集めている入れ物（:func:`collect_dropped_parameters`）があれば足す。"""
    collector = _collector.get()
    if collector is not None and names:
        collector.update(names)
