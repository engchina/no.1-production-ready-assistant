"""外部の AI の API（LLM・VLM・Embedding）を上限つきの並行で呼ぶ実行器（#2002）。

文書の取込のような一括の処理は、同じ API を数十〜数千回呼ぶ。1 件ずつの再試行だけでは、
並行にしたときに同時に 429 を受けた呼び出しが一斉に再試行して再び 429 になる（thundering herd）。
この部品は、呼び出しの全体で次を共有する。

- **エラーの種類**（``ErrorKind``）: HTTP status・例外の型・SDK の例外（openai など）・応答の本文
  （quota・安全の拒否・context の長さの超過）から分ける（``classify_error``。差し替えられる）。
  種類ごとに、再試行の有無と回数・件の失敗か job の停止か・全体の待ちに数えるかを ``KindRule`` で
  決める（既定は ``DEFAULT_KIND_RULES``。AWS SDK の retry mode・Google Cloud / Azure の retry の
  指針・OpenAI / Anthropic の error の約束に合わせた）。
- **待ち**: 指数の backoff と full jitter（上限を倍にし、0〜上限の乱数。上限は既定 60 秒）。
  ``Retry-After``（``retry-after-ms`` を含む）があれば自分の計算より優先する。
- **全体で共有する待ち**: 流量の制限（429）を受けたら、同じ実行器の呼び出し全体を止める
  （ばらばらに待って一斉に再開しない）。
- **並行の数の自動の調整（AIMD）**: 429 で半分（最小 1）、成功が続いたら 1 つずつ上限まで戻す。
- **再試行の予算**: 再試行の総数を「最初の予算 + 成功の数 × 割合」までにする（AWS SDK の retry
  quota と同じ型）。使い切ったら circuit breaker を開く。
- **circuit breaker**: 同じ種類の一時的な失敗が続いたら（既定 10 回）呼び出しを止め、時間をおいて
  1 件だけ試す（half-open）。試しても回復しなければ ``BoundedCallAborted`` で止める。
- **件の失敗**: 再試行を使い切った件は ``map`` の最後にまとめて長めの間隔でもう一度呼ぶ。それでも
  失敗した件は ``CallOutcome.error`` に記録して続ける（``fail_job_on_item_error`` で job の失敗にも
  できる）。設定の誤り・権限・割り当ての使い切りは件を飛ばさず、すぐ ``BoundedCallAborted``。
- **冪等な呼び出しだけを再試行する**（``idempotent=False`` は 1 回だけ呼ぶ。DB に書く処理は
  この部品で再試行しない）。
- **観測**: 再試行ごとに種類・回数・待ちの時間をログに出し、``on_event`` に ``CallEvent`` を渡す。
  待っている間は ``heartbeat_interval_seconds`` ごとに ``wait`` の event を出す（取込の進捗の
  「API の制限のため N 秒待っています」と、停滞の検出が待ちを停滞と誤らないための heartbeat）。

SDK の再試行との分担: この部品を通す呼び出しは、SDK の再試行を切る（openai SDK の
``max_retries=0``）。SDK の中で再試行すると、全体の待ち・AIMD・予算・circuit breaker を
通らないため。SDK が独自の circuit breaker を持つときも切る。残すと 429 が続いたときに先に開き、
この部品が知らない例外で件が失敗する（#2211）。

時計・乱数・sleep は差し替えられる（テストは実時間で待たない）。並行の制御は 1 つの event loop
の中で行う（同期のコード・thread から使うときは ``SyncBoundedCaller``。#2004）。
"""

from __future__ import annotations

import asyncio
import contextvars
import email.utils
import json
import logging
import math
import random as _random
import ssl
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

logger = logging.getLogger(__name__)


class ErrorKind(StrEnum):
    """呼び出しの失敗の種類（再試行と扱いを分ける単位）。"""

    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    UNAVAILABLE = "unavailable"
    SERVER_ERROR = "server_error"
    NOT_SUPPORTED = "not_supported"
    NETWORK = "network"
    TLS = "tls"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    TOO_LARGE = "too_large"
    SAFETY = "safety"
    INVALID_OUTPUT = "invalid_output"
    TRUNCATED = "truncated"
    UNKNOWN = "unknown"


KIND_LABELS: Mapping[ErrorKind, str] = {
    ErrorKind.RATE_LIMITED: "流量の制限",
    ErrorKind.QUOTA_EXHAUSTED: "割り当ての使い切り",
    ErrorKind.UNAVAILABLE: "一時的な不可用",
    ErrorKind.SERVER_ERROR: "サービスの内部の誤り",
    ErrorKind.NOT_SUPPORTED: "対応していない要求",
    ErrorKind.NETWORK: "ネットワークの誤り",
    ErrorKind.TLS: "TLS の証明書の誤り",
    ErrorKind.TIMEOUT: "時間切れ",
    ErrorKind.AUTHENTICATION: "認証の失敗",
    ErrorKind.PERMISSION: "権限の不足",
    ErrorKind.NOT_FOUND: "モデル・エンドポイントが無い",
    ErrorKind.BAD_REQUEST: "要求の誤り",
    ErrorKind.TOO_LARGE: "入力が大きすぎる",
    ErrorKind.SAFETY: "安全のための拒否",
    ErrorKind.INVALID_OUTPUT: "出力の形の誤り",
    ErrorKind.TRUNCATED: "出力の打ち切り",
    ErrorKind.UNKNOWN: "分類できない誤り",
}

# job を止めるときの、利用者が直す所の案内。
_ABORT_HINTS: Mapping[ErrorKind, str] = {
    ErrorKind.QUOTA_EXHAUSTED: (
        "API の割り当て（quota・上限）を使い切りました。待っても直らないため止めました。"
        "割り当てを確かめてから再実行してください。"
    ),
    ErrorKind.NOT_SUPPORTED: (
        "API がこの要求に対応していません。エンドポイントと API の形式の設定を確かめてください。"
    ),
    ErrorKind.TLS: (
        "TLS の証明書を検証できませんでした。エンドポイントの URL と証明書（CA）の設定を"
        "確かめてください。"
    ),
    ErrorKind.AUTHENTICATION: (
        "API の認証に失敗しました（資格情報を読み直しても失敗しました）。API キー・認証の設定を"
        "確かめてください。"
    ),
    ErrorKind.PERMISSION: (
        "API を使う権限がありません。API key・project の設定で、このモデル・エンドポイントを"
        "使う権限を確かめてください。"
    ),
    ErrorKind.NOT_FOUND: (
        "モデルまたはエンドポイントが見つかりません。モデルの設定（モデル ID・エンドポイント・"
        "リージョン）を確かめてください。"
    ),
}


class Disposition(StrEnum):
    """再試行の後も失敗したときの扱い。"""

    ITEM = "item"  # その件の失敗として記録し、ほかの件を続ける。
    JOB = "job"  # 設定の誤り・権限など。すぐ job を止める（同じ失敗をくり返さない）。


@dataclass(frozen=True)
class KindRule:
    """1 つの種類の扱い。

    - ``retries``: すぐの再試行の回数（同じ種類の失敗の数で数える）。
    - ``throttle``: 全体で待ち、並行の数を減らす（流量の制限）。
    - ``breaker``: 続いたら circuit breaker を開く種類（一時的な失敗）。
    - ``final_retry``: 再試行を使い切った件を、最後にまとめてもう一度呼ぶ。
    - ``refresh_credentials``: 再試行の前に資格情報を読み直す（401）。
    - ``adaptive_only``: 呼び出しが入力を小さくできる（``adaptive=True``）ときだけ再試行する
      （413・context の長さの超過・出力の打ち切り。同じ要求はくり返さない）。
    """

    retries: int = 0
    disposition: Disposition = Disposition.ITEM
    throttle: bool = False
    breaker: bool = False
    final_retry: bool = False
    refresh_credentials: bool = False
    adaptive_only: bool = False


DEFAULT_KIND_RULES: Mapping[ErrorKind, KindRule] = {
    ErrorKind.RATE_LIMITED: KindRule(retries=8, throttle=True, final_retry=True),
    ErrorKind.QUOTA_EXHAUSTED: KindRule(disposition=Disposition.JOB),
    ErrorKind.UNAVAILABLE: KindRule(retries=4, breaker=True, final_retry=True),
    ErrorKind.SERVER_ERROR: KindRule(retries=2, breaker=True, final_retry=True),
    ErrorKind.NOT_SUPPORTED: KindRule(disposition=Disposition.JOB),
    ErrorKind.NETWORK: KindRule(retries=4, breaker=True, final_retry=True),
    ErrorKind.TLS: KindRule(disposition=Disposition.JOB),
    ErrorKind.TIMEOUT: KindRule(retries=3, breaker=True, final_retry=True),
    ErrorKind.AUTHENTICATION: KindRule(
        retries=1, disposition=Disposition.JOB, refresh_credentials=True
    ),
    ErrorKind.PERMISSION: KindRule(disposition=Disposition.JOB),
    ErrorKind.NOT_FOUND: KindRule(disposition=Disposition.JOB),
    ErrorKind.BAD_REQUEST: KindRule(),
    ErrorKind.TOO_LARGE: KindRule(retries=2, adaptive_only=True),
    ErrorKind.SAFETY: KindRule(),
    ErrorKind.INVALID_OUTPUT: KindRule(retries=2),
    ErrorKind.TRUNCATED: KindRule(retries=1, adaptive_only=True),
    ErrorKind.UNKNOWN: KindRule(),
}


@dataclass(frozen=True)
class ClassifiedError:
    """分類の結果。``retry_after`` はサーバーが示した待ちの秒数（無ければ None）。"""

    kind: ErrorKind
    status: int | None = None
    retry_after: float | None = None
    detail: str = ""


ErrorClassifier = Callable[[BaseException], "ClassifiedError | ErrorKind | None"]


# --- 分類 -------------------------------------------------------------------------------

_QUOTA_MARKERS = (
    "insufficient_quota",
    "quota exceeded",
    "quotaexceeded",
    "exceeded your current quota",
    "quota has been exhausted",
    "limitexceeded",
    "billing hard limit",
    "割り当て",
)
_SAFETY_MARKERS = (
    "content_filter",
    "content filter",
    "content_policy",
    "content policy",
    "safety",
    "responsible ai",
    "inappropriate content",
    "blocked by",
)
# 入力（prompt）の長さの超過。vLLM 系のモデル（openai.gpt-oss-120b など）は
# 400 の本文で「The engine prompt length N exceeds the max_model_len N」（type は
# invalid_request_error）と返す（2026-10-20 の合成の入力での実測。#2720）。この印が無いと
# 要求の誤りに分かれ、入力を分け直せない。
_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "context length",
    "maximum context",
    "max_model_len",
    "maximum prompt length",
    "too many tokens",
    "input is too long",
    "prompt is too long",
    "request too large",
    "payload too large",
)
_TRUNCATED_MARKERS = ("max_output_tokens", "max_tokens", "finish_reason=length")
_TLS_MARKERS = ("certificate_verify_failed", "certificate verify failed", "ssl: certificate")

# HTTP status の無い SDK の例外（stream の中の error event の `APIError` など）が本文の構造化した
# エラーで示す code（#2667）。gRPC の status の名前と OpenAI 互換の error code を、相当する HTTP
# status に読み替えて HTTP status と同じ規則で分ける。区切り（`-`・空白）は `_` にそろえて引く。
_ERROR_CODE_STATUS: Mapping[str, int] = {
    "resource_exhausted": 429,
    "rate_limit_exceeded": 429,
    "rate_limit_error": 429,
    "rate_limited": 429,
    "too_many_requests": 429,
    "toomanyrequests": 429,
    "insufficient_quota": 429,
    "quota_exceeded": 429,
    "unauthenticated": 401,
    "notauthenticated": 401,
    "invalid_api_key": 401,
    "authentication_error": 401,
    "permission_denied": 403,
    "permission_error": 403,
    "forbidden": 403,
    "notauthorizedornotfound": 404,
    "not_found": 404,
    "not_found_error": 404,
    "model_not_found": 404,
    "invalid_argument": 400,
    "invalid_request_error": 400,
    "invalidparameter": 400,
    "bad_request": 400,
    "failed_precondition": 400,
    "deadline_exceeded": 408,
    "request_timeout": 408,
    "payload_too_large": 413,
    "unimplemented": 501,
    "internal": 500,
    "internal_error": 500,
    "internalservererror": 500,
    "server_error": 500,
    "api_error": 500,
    "unavailable": 503,
    "service_unavailable": 503,
    "overloaded": 503,
    "overloaded_error": 503,
}
# 構造化したエラーで code を読む key（小文字）。
_ERROR_CODE_KEYS = frozenset({"code", "status", "status_code", "http_status", "http_code", "type"})
# 構造化したエラーをたどる上限（入れ子の深さ・見る値の数・JSON として読む文字列の長さ）。
_STRUCTURED_MAX_DEPTH = 6
_STRUCTURED_MAX_NODES = 200
_STRUCTURED_MAX_TEXT = 20_000

_TIMEOUT_NAMES = ("timeout", "timedout")
_NETWORK_NAMES = (
    "connecterror",
    "connectionerror",
    "transporterror",
    "remoteprotocolerror",
    "readerror",
    "writeerror",
    "networkerror",
    "apiconnectionerror",
    "protocolerror",
    "gaierror",
)
_INVALID_OUTPUT_NAMES = (
    "validationerror",
    "jsondecodeerror",
    "responseerror",
    "schemaerror",
)
_TRUNCATED_NAMES = ("incompleteresponseerror",)
_UNSUPPORTED_NAMES = ("unsupportedinputerror",)


def _exception_chain(exc: BaseException, limit: int = 8) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < limit:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _status_of(exc: BaseException) -> int | None:
    """HTTP status を返す。

    openai の ``status_code``・httpx の ``response.status_code``・SDK の例外の ``status`` を読む。

    status の無い例外は None。
    """
    for value in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "status", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    return None


def _headers_of(exc: BaseException) -> Mapping[str, Any] | None:
    for value in (
        getattr(getattr(exc, "response", None), "headers", None),
        getattr(exc, "headers", None),
    ):
        if isinstance(value, Mapping) or hasattr(value, "get"):
            return value
    return None


def _text_of(exc: BaseException) -> str:
    """分類に使う文字（例外の文・SDK の code / message / body）。小文字にする。"""
    parts = [str(exc)]
    for name in ("code", "message", "body"):
        value = getattr(exc, name, None)
        if value is not None and not callable(value):
            parts.append(str(value))
    return " ".join(parts).casefold()


def parse_retry_after(
    headers: Mapping[str, Any] | Any | None, *, now: Callable[[], datetime] | None = None
) -> float | None:
    """``Retry-After``（秒・HTTP の日付）と ``retry-after-ms`` を秒にする。

    無い・読めないときは None。
    """
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    for name in ("retry-after-ms", "Retry-After-Ms"):
        raw = getter(name)
        if raw is not None:
            try:
                return max(0.0, float(str(raw).strip()) / 1000.0)
            except ValueError:
                pass
    for name in ("retry-after", "Retry-After"):
        raw = getter(name)
        if raw is None:
            continue
        text = str(raw).strip()
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        current = now() if now is not None else datetime.now(UTC)
        return max(0.0, (when - current).total_seconds())
    return None


def _json_in(text: str) -> object | None:
    """文字列の中の JSON（最初の ``{`` / ``[`` から）。読めない・長すぎるときは None。"""
    if len(text) > _STRUCTURED_MAX_TEXT:
        return None
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, min(starts))
    except ValueError:
        return None
    return value if isinstance(value, Mapping | list) else None


def _status_from_code(value: object) -> int | None:
    """構造化したエラーの code の値（数・数の文字列・名前）を HTTP status にする。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 400 <= value <= 599 else None
    if not isinstance(value, str):
        return None
    name = value.strip().casefold()
    if name.isdigit():
        number = int(name)
        return number if 400 <= number <= 599 else None
    return _ERROR_CODE_STATUS.get(name.replace("-", "_").replace(" ", "_"))


def _structured_status(exc: BaseException) -> int | None:
    """例外の本文の構造化したエラーが示す HTTP status（#2667）。無ければ None。

    SDK の例外の ``body``（dict・文字列）と ``message`` を読み、値の中の JSON の文字列（エラーの
    message に入れ子にした呼び先のエラー）もたどる。最も内側（呼び先の元のエラーに近い）の code を
    使う。例外の文（``str``）は読まない（出力の検証の失敗の文に入ったモデルの出力の JSON を、
    呼び先のエラーと取り違えない）。
    """
    roots = [getattr(exc, "body", None), getattr(exc, "message", None)]
    pending: list[tuple[object, int]] = [(root, 0) for root in roots if root is not None]
    found: tuple[int, int] | None = None  # (深さ, status)
    nodes = 0
    while pending and nodes < _STRUCTURED_MAX_NODES:
        value, depth = pending.pop(0)
        nodes += 1
        if depth > _STRUCTURED_MAX_DEPTH:
            continue
        if isinstance(value, str):
            parsed = _json_in(value)
            if parsed is not None:
                pending.append((parsed, depth + 1))
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() in _ERROR_CODE_KEYS:
                    status = _status_from_code(item)
                    if status is not None and (found is None or depth > found[0]):
                        found = (depth, status)
                if isinstance(item, str | Mapping | list):
                    pending.append((item, depth + 1))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return found[1] if found is not None else None


def _kind_from_status(status: int, text: str) -> ErrorKind:
    if status == 429:
        # 割り当て（quota。期間の上限）を使い切った 429 は、待っても今の要求の間は戻らないので、
        # 流量の制限と分ける（#2667）。
        if "quota" in text or any(marker in text for marker in _QUOTA_MARKERS):
            return ErrorKind.QUOTA_EXHAUSTED
        return ErrorKind.RATE_LIMITED
    if status == 401:
        return ErrorKind.AUTHENTICATION
    if status == 403:
        return ErrorKind.PERMISSION
    if status == 404:
        if "notauthorizedornotfound" in text:
            return ErrorKind.PERMISSION
        return ErrorKind.NOT_FOUND
    if status == 408:
        return ErrorKind.TIMEOUT
    if status == 413:
        return ErrorKind.TOO_LARGE
    if status in (501, 505):
        return ErrorKind.NOT_SUPPORTED
    if status == 500:
        return ErrorKind.SERVER_ERROR
    if status >= 500:
        return ErrorKind.UNAVAILABLE
    # 400・422 などの要求の誤り。本文で、安全の拒否・context の長さ・割り当てを分ける。
    if any(marker in text for marker in _SAFETY_MARKERS):
        return ErrorKind.SAFETY
    if any(marker in text for marker in _CONTEXT_MARKERS):
        return ErrorKind.TOO_LARGE
    if any(marker in text for marker in _QUOTA_MARKERS):
        return ErrorKind.QUOTA_EXHAUSTED
    return ErrorKind.BAD_REQUEST


def _kind_from_type(exc: BaseException, text: str) -> ErrorKind | None:
    names = [klass.__name__.casefold() for klass in type(exc).__mro__]
    if isinstance(exc, ssl.SSLCertVerificationError) or (
        isinstance(exc, ssl.SSLError) and any(marker in text for marker in _TLS_MARKERS)
    ):
        return ErrorKind.TLS
    if any(name.endswith(_TRUNCATED_NAMES) for name in names):
        return ErrorKind.TRUNCATED
    if any(name.endswith(_UNSUPPORTED_NAMES) for name in names):
        return ErrorKind.NOT_SUPPORTED
    if isinstance(exc, TimeoutError) or any(
        part in name for name in names for part in _TIMEOUT_NAMES
    ):
        return ErrorKind.TIMEOUT
    if isinstance(exc, ConnectionError) or any(name in _NETWORK_NAMES for name in names):
        return ErrorKind.NETWORK
    if any(name.endswith(suffix) for name in names for suffix in _INVALID_OUTPUT_NAMES):
        return ErrorKind.INVALID_OUTPUT
    return None


def classify_error(exc: BaseException) -> ClassifiedError:
    """例外をエラーの種類に分ける（既定の分類）。

    原因の連鎖（``__cause__`` / ``__context__``）をたどり、次の順で決める。

    1. TLS の証明書の誤り（``ssl.SSLCertVerificationError`` など）。
    2. HTTP status（openai・httpx・SDK の例外の ``status``）と本文（quota・安全・context の長さ・
       ``NotAuthorizedOrNotFound``）。``Retry-After`` を読む。
    3. HTTP status の無い SDK の例外の、本文の構造化したエラーの code（``RESOURCE_EXHAUSTED``・
       ``rate_limit_exceeded``・入れ子の JSON の ``"code": 429`` など）。2 と同じ規則で分ける。
    4. 例外の型（時間切れ・接続・出力の打ち切り・出力の形の誤り）と、本文の印（安全の拒否・
       出力の上限）。
    """
    chain = _exception_chain(exc)
    detail = f"{type(exc).__name__}: {exc}"[:500]
    for item in chain:
        if isinstance(item, ssl.SSLCertVerificationError):
            return ClassifiedError(ErrorKind.TLS, detail=detail)
        if isinstance(item, ssl.SSLError) and any(m in _text_of(item) for m in _TLS_MARKERS):
            return ClassifiedError(ErrorKind.TLS, detail=detail)
    for item in chain:
        if any(marker in _text_of(item) for marker in _TLS_MARKERS):
            return ClassifiedError(ErrorKind.TLS, detail=detail)
    for item in chain:
        status = _status_of(item)
        if status is None or status < 400:
            continue
        text = _text_of(item)
        return ClassifiedError(
            _kind_from_status(status, text),
            status=status,
            retry_after=parse_retry_after(_headers_of(item)),
            detail=detail,
        )
    for item in chain:
        # HTTP status の無い SDK の例外（stream の中の error event など）は、本文の構造化した
        # エラーの code を HTTP status と同じ規則で分ける（#2667）。status は推定なので残さない。
        structured = _structured_status(item)
        if structured is None:
            continue
        return ClassifiedError(
            _kind_from_status(structured, _text_of(item)),
            retry_after=parse_retry_after(_headers_of(item)),
            detail=detail,
        )
    for item in chain:
        text = _text_of(item)
        kind = _kind_from_type(item, text)
        if kind is not None:
            return ClassifiedError(kind, detail=detail)
    text = " ".join(_text_of(item) for item in chain)
    if any(marker in text for marker in _SAFETY_MARKERS):
        return ClassifiedError(ErrorKind.SAFETY, detail=detail)
    if any(marker in text for marker in _TRUNCATED_MARKERS):
        return ClassifiedError(ErrorKind.TRUNCATED, detail=detail)
    return ClassifiedError(ErrorKind.UNKNOWN, detail=detail)


def chain_classifiers(*classifiers: ErrorClassifier) -> Callable[[BaseException], ClassifiedError]:
    """先の分類が決めなかった（None）例外を次へ渡し、最後は ``classify_error``。"""

    def classify(exc: BaseException) -> ClassifiedError:
        base = classify_error(exc)
        for classifier in classifiers:
            result = classifier(exc)
            if result is None:
                continue
            if isinstance(result, ClassifiedError):
                return result
            return ClassifiedError(
                result, status=base.status, retry_after=base.retry_after, detail=base.detail
            )
        return base

    return classify


# --- 例外・結果・event -------------------------------------------------------------------


class BoundedCallError(Exception):
    """この部品が出す失敗の基底。``kind`` は最後の失敗の種類。"""

    safe_for_user = True

    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind,
        status: int | None = None,
        attempts: int = 0,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.attempts = attempts
        self.detail = detail


class BoundedCallFailed(BoundedCallError):
    """1 件の失敗（再試行を使い切った・再試行しない種類）。ほかの件は続けてよい。"""

    def __init__(self, message: str, *, final_retry: bool = False, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.final_retry = final_retry


class BoundedCallAborted(BoundedCallError):
    """job を止める失敗（設定の誤り・権限・割り当て・circuit breaker が回復しない）。

    ``stops_batch`` は、この部品に依存しない一括の処理（rag_engine の Vision など）が、件の失敗と
    区別して処理の全体を止めるための印。
    """

    stops_batch = True


@dataclass(frozen=True)
class Attempt:
    """呼び出しに渡す、今の試行の情報。

    ``adaptation`` は「入力を小さくしてやり直す」回数（413・context の長さの超過・出力の打ち切り
    のあと。``adaptive=True`` の呼び出しだけ）。``last_error`` は前の試行の失敗の種類。
    """

    number: int
    final: bool = False
    last_error: ErrorKind | None = None
    adaptation: int = 0


@dataclass(frozen=True)
class CallOutcome[T]:
    """``map`` の 1 件の結果（成功は ``value``、失敗は ``error``）。"""

    value: T | None = None
    error: BoundedCallFailed | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


EventType = Literal[
    "success",
    "retry",
    "wait",
    "wait_end",
    "concurrency",
    "breaker",
    "item_failed",
    "aborted",
    "final_retry",
]


@dataclass(frozen=True)
class CallEvent:
    """観測の event（ログ・進捗へ写す）。"""

    type: EventType
    name: str
    kind: ErrorKind | None = None
    attempt: int | None = None
    delay_seconds: float | None = None
    remaining_seconds: float | None = None
    reason: str | None = None
    concurrency: int | None = None
    label: str = ""
    message: str = ""


@dataclass(frozen=True)
class CallerStatus:
    """今の状態（進捗の表示に使う）。"""

    concurrency: int
    max_concurrency: int
    in_flight: int
    waiting_reason: str | None
    waiting_seconds: float | None
    calls: int
    successes: int
    retries: int
    failures: int
    breaker: Literal["closed", "open", "half_open"]
    # 最後の再試行の対象にならず失敗のまま終わった件の数（``failures`` は失敗した試行の数）。
    items_failed: int = 0


@dataclass(frozen=True)
class BoundedCallPolicy:
    """実行器の設定。既定は Issue #2002 の表と共通の規則の例。"""

    max_concurrency: int = 4
    min_concurrency: int = 1
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    # サーバーが示した待ち（Retry-After）の上限。これより長い指示は、割り当ての使い切りに近い。
    retry_after_max_seconds: float = 300.0
    # AIMD: 減らすときの割合と、1 つ戻すまでの成功の数。
    decrease_factor: float = 0.5
    increase_after_successes: int = 5
    # 再試行の予算: 最初の予算 + 成功の数 × 割合。
    retry_budget_initial: float = 10.0
    retry_budget_ratio: float = 0.2
    # circuit breaker: 同じ種類の一時的な失敗が続いた回数・止める時間・試しの回数。
    breaker_threshold: int = 10
    breaker_cooldown_seconds: float = 30.0
    breaker_probe_attempts: int = 3
    # 件の最後の再試行: 待ちと回数（回数ごとに待ちを倍にする）。
    final_retry_delay_seconds: float = 30.0
    final_retry_attempts: int = 1
    fail_job_on_item_error: bool = False
    # 待っている間の heartbeat（wait の event）の間隔。
    heartbeat_interval_seconds: float = 10.0
    # 1 回ごとの timeout（None は呼び出し・SDK の timeout に任せる）と、実行器の全体の期限。
    attempt_timeout_seconds: float | None = None
    deadline_seconds: float | None = None
    rules: Mapping[ErrorKind, KindRule] = field(default_factory=lambda: dict(DEFAULT_KIND_RULES))

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency は 1 以上にしてください。")
        if not 1 <= self.min_concurrency <= self.max_concurrency:
            raise ValueError("min_concurrency は 1 以上 max_concurrency 以下にしてください。")
        if not 0 < self.decrease_factor < 1:
            raise ValueError("decrease_factor は 0 より大きく 1 より小さくしてください。")

    def rule(self, kind: ErrorKind) -> KindRule:
        return self.rules.get(kind) or DEFAULT_KIND_RULES.get(kind) or KindRule()


Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
RandomSource = Callable[[], float]
EventListener = Callable[[CallEvent], None]
CredentialRefresher = Callable[[], Awaitable[None] | None]


class BoundedCaller:
    """上限つきの並行で呼び出しを実行する（1 つの段・1 つの job ごとに作る）。

    ``label`` は利用者向けの文（「図・表の説明（Vision）」など）、``name`` はログの名前。
    """

    def __init__(
        self,
        name: str,
        policy: BoundedCallPolicy | None = None,
        *,
        label: str = "",
        classifier: Callable[[BaseException], ClassifiedError] = classify_error,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        random: RandomSource = _random.random,
        on_event: EventListener | None = None,
        refresh_credentials: CredentialRefresher | None = None,
    ) -> None:
        self.name = name
        self.label = label or name
        self.policy = policy or BoundedCallPolicy()
        self._classify = classifier
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._listeners: list[EventListener] = [on_event] if on_event is not None else []
        self._refresh_credentials = refresh_credentials
        self._started_at = clock()
        self._limit = self.policy.max_concurrency
        self._in_flight = 0
        self._condition: asyncio.Condition | None = None
        self._pause_until = 0.0
        self._pause_reason: str | None = None
        self._decrease_guard_until = -math.inf
        self._successes_since_change = 0
        self._budget = float(self.policy.retry_budget_initial)
        self._consecutive: dict[ErrorKind, int] = {}
        self._breaker: Literal["closed", "open", "half_open"] = "closed"
        self._breaker_kind: ErrorKind | None = None
        self._breaker_probes = 0
        self._probe_in_flight = False
        self._aborted: BoundedCallAborted | None = None
        self._waiting_until: float | None = None
        self._waiting_reason: str | None = None
        self.calls = 0
        self.successes = 0
        self.retries = 0
        self.failures = 0
        self.items_failed = 0

    # --- 観測 -----------------------------------------------------------------------------

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    def status(self) -> CallerStatus:
        now = self._clock()
        waiting = (
            max(0.0, self._waiting_until - now)
            if self._waiting_until is not None and self._waiting_until > now
            else None
        )
        return CallerStatus(
            concurrency=self._limit,
            max_concurrency=self.policy.max_concurrency,
            in_flight=self._in_flight,
            waiting_reason=self._waiting_reason if waiting is not None else None,
            waiting_seconds=waiting,
            calls=self.calls,
            successes=self.successes,
            retries=self.retries,
            failures=self.failures,
            breaker=self._breaker,
            items_failed=self.items_failed,
        )

    def _emit(self, event: CallEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - 観測は補助。呼び出しを止めない。
                logger.warning("bounded_call_listener_failed", exc_info=True)

    # --- 並行の数・待ち -----------------------------------------------------------------

    def _cond(self) -> asyncio.Condition:
        if self._condition is None:
            self._condition = asyncio.Condition()
        return self._condition

    async def _notify(self) -> None:
        condition = self._cond()
        async with condition:
            condition.notify_all()

    async def _wait(self, seconds: float, *, reason: str, kind: ErrorKind | None) -> None:
        """``seconds`` 待つ。待っている間は heartbeat の間隔で wait の event を出す。"""
        if seconds <= 0:
            return
        deadline = self._clock() + seconds
        slept = 0.0
        if self._waiting_until is None or deadline > self._waiting_until:
            self._waiting_until = deadline
            self._waiting_reason = reason
        interval = max(0.001, self.policy.heartbeat_interval_seconds)
        while True:
            remaining = min(deadline - self._clock(), seconds - slept)
            if remaining <= 1e-9:
                break
            self._emit(
                CallEvent(
                    "wait",
                    self.name,
                    kind=kind,
                    remaining_seconds=remaining,
                    reason=reason,
                    concurrency=self._limit,
                )
            )
            chunk = min(remaining, interval)
            await self._sleep(chunk)
            slept += chunk
        if self._waiting_until is not None and self._clock() >= self._waiting_until - 1e-9:
            self._waiting_until = None
            self._waiting_reason = None
            self._emit(CallEvent("wait_end", self.name, kind=kind, reason=reason))

    async def _acquire(self) -> bool:
        """呼び出しの枠を取る。circuit breaker の試し（half-open）の 1 件なら True。"""
        while True:
            if self._aborted is not None:
                raise self._aborted
            now = self._clock()
            if self._pause_until > now:
                await self._wait(
                    self._pause_until - now,
                    reason=self._pause_reason or "rate_limited",
                    kind=self._breaker_kind,
                )
                continue
            if self._breaker == "open":
                self._breaker = "half_open"
                self._probe_in_flight = False
                self._emit(
                    CallEvent("breaker", self.name, kind=self._breaker_kind, reason="half_open")
                )
            if self._breaker == "half_open":
                if not self._probe_in_flight and self._in_flight == 0:
                    self._probe_in_flight = True
                    self._in_flight += 1
                    return True
            elif self._in_flight < self._limit:
                self._in_flight += 1
                return False
            condition = self._cond()
            async with condition:
                await condition.wait()

    async def _release(self, *, probe: bool) -> None:
        self._in_flight -= 1
        if probe:
            self._probe_in_flight = False
        await self._notify()

    def _pause(self, seconds: float, reason: str) -> None:
        until = self._clock() + seconds
        if until > self._pause_until:
            self._pause_until = until
            self._pause_reason = reason

    def _decrease(self, window: float) -> None:
        now = self._clock()
        if now < self._decrease_guard_until:
            return
        self._decrease_guard_until = now + max(window, self.policy.backoff_base_seconds)
        # 半分（切り上げ）にし、必ず 1 つは減らす（4 → 2、3 → 2、2 → 1）。最小は min_concurrency。
        new_limit = max(
            self.policy.min_concurrency,
            min(self._limit - 1, math.ceil(self._limit * self.policy.decrease_factor)),
        )
        self._successes_since_change = 0
        if new_limit != self._limit:
            self._limit = new_limit
            self._emit(
                CallEvent("concurrency", self.name, concurrency=new_limit, reason="decrease")
            )
            logger.info(
                "bounded_call_concurrency",
                extra={"caller": self.name, "concurrency": new_limit, "reason": "decrease"},
            )

    def _on_success(self, *, probe: bool, label: str = "") -> None:
        self.successes += 1
        self._budget += self.policy.retry_budget_ratio
        self._consecutive.clear()
        if probe or self._breaker != "closed":
            self._breaker = "closed"
            self._breaker_kind = None
            self._breaker_probes = 0
            self._emit(CallEvent("breaker", self.name, reason="closed"))
        self._successes_since_change += 1
        if (
            self._limit < self.policy.max_concurrency
            and self._successes_since_change >= self.policy.increase_after_successes
        ):
            self._limit += 1
            self._successes_since_change = 0
            self._emit(
                CallEvent("concurrency", self.name, concurrency=self._limit, reason="increase")
            )
        self._emit(CallEvent("success", self.name, label=label, concurrency=self._limit))

    def _open_breaker(self, kind: ErrorKind, reason: str) -> None:
        if self._breaker == "open":
            return
        self._breaker = "open"
        self._breaker_kind = kind
        cooldown = self.policy.breaker_cooldown_seconds * (2.0**self._breaker_probes)
        self._pause(cooldown, "breaker_open")
        self._emit(
            CallEvent("breaker", self.name, kind=kind, reason=reason, delay_seconds=cooldown)
        )
        logger.warning(
            "bounded_call_breaker_open",
            extra={"caller": self.name, "kind": kind.value, "reason": reason, "cooldown": cooldown},
        )

    def _abort(self, error: BoundedCallAborted) -> BoundedCallAborted:
        if self._aborted is None:
            self._aborted = error
            self._emit(CallEvent("aborted", self.name, kind=error.kind, message=str(error)))
            logger.warning(
                "bounded_call_aborted",
                extra={"caller": self.name, "kind": error.kind.value, "status": error.status},
            )
        return self._aborted

    def _backoff(self, retry_number: int) -> float:
        """full jitter: 上限 ``min(max, base × 2^n)`` の 0〜上限の乱数。"""
        cap = min(
            self.policy.backoff_max_seconds,
            self.policy.backoff_base_seconds * (2.0 ** max(0, retry_number - 1)),
        )
        return self._random() * cap

    def _retry_delay(self, info: ClassifiedError, rule: KindRule, retry_number: int) -> float:
        """次の試行までの待ち。``Retry-After`` を優先し、無ければ full jitter の backoff。"""
        if info.retry_after is not None:
            # サーバーの指示を守ったうえで、再開を少しずらす（一斉に再開しない）。
            return min(info.retry_after, self.policy.retry_after_max_seconds) + (
                self._random() * self.policy.backoff_base_seconds
            )
        if rule.adaptive_only:
            return 0.0
        return self._backoff(retry_number)

    def _abort_message(self, info: ClassifiedError, *, reason: str | None = None) -> str:
        status = f"（HTTP {info.status}）" if info.status else ""
        hint = reason or _ABORT_HINTS.get(info.kind) or "呼び出しを止めました。"
        detail = f"詳細: {info.detail}" if info.detail else ""
        return " ".join(
            part
            for part in (f"{self.label}: {KIND_LABELS[info.kind]}{status}。{hint}", detail)
            if part
        )

    def _failed_message(self, info: ClassifiedError) -> str:
        status = f"（HTTP {info.status}）" if info.status else ""
        return f"{KIND_LABELS[info.kind]}{status}: {info.detail}"[:600]

    # --- 呼び出し -------------------------------------------------------------------------

    async def call[T](
        self,
        fn: Callable[[Attempt], Awaitable[T]],
        *,
        label: str = "",
        idempotent: bool = True,
        adaptive: bool = False,
        final: bool = False,
    ) -> T:
        """``fn`` を呼ぶ。失敗は種類ごとの規則で再試行し、だめなら例外にする。

        - 件の失敗: ``BoundedCallFailed``（``final_retry`` が真なら最後の再試行の対象）。
        - job の停止: ``BoundedCallAborted``（以後のこの実行器の呼び出しもすべて止まる）。
        """
        retries_by_kind: dict[ErrorKind, int] = {}
        retry_count = 0
        last_kind: ErrorKind | None = None
        adaptation = 0
        number = 0
        final_attempts = max(1, self.policy.final_retry_attempts)
        while True:
            number += 1
            if final:
                await self._wait(
                    self.policy.final_retry_delay_seconds * (2.0 ** (number - 1)),
                    reason="final_retry",
                    kind=last_kind,
                )
            probe = await self._acquire()
            self.calls += 1
            try:
                awaitable = fn(Attempt(number, final, last_kind, adaptation))
                timeout = self.policy.attempt_timeout_seconds
                value = await (asyncio.wait_for(awaitable, timeout) if timeout else awaitable)
            except asyncio.CancelledError:
                await self._release(probe=probe)
                raise
            except BoundedCallError:
                await self._release(probe=probe)
                raise
            except Exception as exc:  # noqa: BLE001 - 種類に分けて扱う。
                await self._release(probe=probe)
                info = self._classify(exc)
                rule = self.policy.rule(info.kind)
                self.failures += 1
                last_kind = info.kind
                now = self._clock()

                delay = self._retry_delay(info, rule, retry_count + 1)
                if rule.throttle:
                    self._decrease(delay)
                if rule.breaker or rule.throttle:
                    count = self._consecutive.get(info.kind, 0) + 1
                    self._consecutive[info.kind] = count
                    if probe:
                        self._breaker_probes += 1
                        if self._breaker_probes >= self.policy.breaker_probe_attempts:
                            raise self._abort(
                                BoundedCallAborted(
                                    self._abort_message(
                                        info,
                                        reason=(
                                            f"失敗が続いたため呼び出しを止め、時間をおいて "
                                            f"{self._breaker_probes} 回試しましたが"
                                            "回復しませんでした。"
                                            "サービスの状態と設定を確かめてから再実行してください。"
                                        ),
                                    ),
                                    kind=info.kind,
                                    status=info.status,
                                    attempts=number,
                                    detail=info.detail,
                                )
                            ) from exc
                        self._breaker = "closed"
                        self._open_breaker(info.kind, "probe_failed")
                    elif rule.breaker and count >= self.policy.breaker_threshold:
                        self._open_breaker(info.kind, "consecutive_failures")
                elif probe:
                    # 一時的でない失敗（その件の誤り）は、サービスの回復の判定に数えない。
                    self._breaker = "closed"
                    self._breaker_probes = 0

                used = retries_by_kind.get(info.kind, 0)
                can_retry = (
                    idempotent
                    and not final
                    and used < rule.retries
                    and (adaptive or not rule.adaptive_only)
                )
                if can_retry and self.policy.deadline_seconds is not None:
                    can_retry = now < self._started_at + self.policy.deadline_seconds
                if can_retry and self._budget < 1.0:
                    can_retry = False
                    if rule.breaker or rule.throttle:
                        self._open_breaker(info.kind, "retry_budget")
                if not can_retry:
                    if final and number < final_attempts and rule.disposition is Disposition.ITEM:
                        continue
                    if rule.disposition is Disposition.JOB:
                        raise self._abort(
                            BoundedCallAborted(
                                self._abort_message(info),
                                kind=info.kind,
                                status=info.status,
                                attempts=number,
                                detail=info.detail,
                            )
                        ) from exc
                    deferred = rule.final_retry and not final and idempotent
                    if not deferred:
                        self.items_failed += 1
                    self._emit(
                        CallEvent(
                            "item_failed",
                            self.name,
                            kind=info.kind,
                            attempt=number,
                            label=label,
                            message=info.detail,
                        )
                    )
                    raise BoundedCallFailed(
                        self._failed_message(info),
                        kind=info.kind,
                        status=info.status,
                        attempts=number,
                        detail=info.detail,
                        final_retry=deferred,
                    ) from exc

                retries_by_kind[info.kind] = used + 1
                retry_count += 1
                self.retries += 1
                self._budget -= 1.0
                if rule.adaptive_only:
                    adaptation += 1
                self._emit(
                    CallEvent(
                        "retry",
                        self.name,
                        kind=info.kind,
                        attempt=number,
                        delay_seconds=delay,
                        label=label,
                        message=info.detail,
                    )
                )
                logger.info(
                    "bounded_call_retry",
                    extra={
                        "caller": self.name,
                        "kind": info.kind.value,
                        "status": info.status,
                        "attempt": number,
                        "delay_seconds": round(delay, 3),
                        "retry_after": info.retry_after,
                    },
                )
                if rule.refresh_credentials and self._refresh_credentials is not None:
                    refreshed = self._refresh_credentials()
                    if refreshed is not None:
                        await refreshed
                if rule.throttle:
                    # 全体で待つ（この呼び出しも枠を取り直すときに待つ）。
                    self._pause(delay, info.kind.value)
                else:
                    await self._wait(delay, reason=info.kind.value, kind=info.kind)
                continue
            await self._release(probe=probe)
            self._on_success(probe=probe, label=label)
            return value

    async def map[I, T](
        self,
        items: Sequence[I],
        fn: Callable[[I, Attempt], Awaitable[T]],
        *,
        labels: Sequence[str] | None = None,
        adaptive: bool = False,
    ) -> list[CallOutcome[T]]:
        """``items`` をそれぞれ ``fn`` で呼び、入力の順の結果を返す（上限つきの並行）。

        再試行を使い切った一時的な失敗の件は、すべての件の後にまとめてもう一度呼ぶ
        （``final_retry_delay_seconds`` 待ってから）。それでも失敗した件は ``CallOutcome.error``。
        ``fail_job_on_item_error`` なら最初の失敗の件で ``BoundedCallFailed`` を出す。
        ``BoundedCallAborted`` は残りを呼ばずにそのまま出す。
        """
        results: list[CallOutcome[T] | None] = [None] * len(items)
        deferred: list[int] = []

        def label_of(index: int) -> str:
            return labels[index] if labels is not None and index < len(labels) else str(index)

        async def run(index: int, *, final: bool) -> None:
            item = items[index]

            async def invoke(attempt: Attempt) -> T:
                return await fn(item, attempt)

            try:
                value = await self.call(
                    invoke, label=label_of(index), adaptive=adaptive, final=final
                )
            except BoundedCallFailed as exc:
                if exc.final_retry and not final:
                    deferred.append(index)
                    return
                results[index] = CallOutcome(error=exc)
                return
            results[index] = CallOutcome(value=value)

        await self._gather([run(index, final=False) for index in range(len(items))])
        if deferred:
            self._emit(
                CallEvent("final_retry", self.name, reason="final_retry", attempt=len(deferred))
            )
            pending = sorted(deferred)
            deferred.clear()
            await self._gather([run(index, final=True) for index in pending])
        outcomes = [
            outcome if outcome is not None else CallOutcome[T](error=None) for outcome in results
        ]
        if self.policy.fail_job_on_item_error:
            for outcome in outcomes:
                if outcome.error is not None:
                    raise outcome.error
        return outcomes

    async def _gather(self, coroutines: list[Coroutine[Any, Any, None]]) -> None:
        if not coroutines:
            return
        try:
            async with asyncio.TaskGroup() as group:
                for coroutine in coroutines:
                    group.create_task(coroutine)
        except BaseExceptionGroup as group_error:
            aborted = [e for e in group_error.exceptions if isinstance(e, BoundedCallAborted)]
            if aborted:
                raise aborted[0] from None
            others = [
                e for e in group_error.exceptions if not isinstance(e, asyncio.CancelledError)
            ]
            if len(others) == 1:
                raise others[0] from None
            raise


class SyncBoundedCaller:
    """同期のコード（worker thread）から ``BoundedCaller`` を使う橋（#2004）。

    専用の thread で event loop を 1 つ動かし、どの thread からの ``call`` / ``map`` も、
    その loop の 1 つの実行器へ投げる。そのため、同期の処理を thread で並行にしても
    （入れ子の再試行・分割を含む）、並行の上限・429 の全体の待ち・AIMD・circuit breaker を
    全体で共有する。呼び出す関数は worker thread で動き、呼び出し元の thread の context
    （``contextvars``。利用者・監査・進捗）を引き継ぐ。``with`` で使う（抜けると loop を
    止める）。event loop の thread からは呼ばない（結果を待つ間 loop を止めるため。async の
    コードは ``BoundedCaller`` を直接使う）。
    """

    def __init__(self, caller: BoundedCaller) -> None:
        self.caller = caller
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> SyncBoundedCaller:
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._loop = loop
        self._thread = threading.Thread(
            target=run, name=f"bounded-calls-{self.caller.name}", daemon=True
        )
        self._thread.start()
        ready.wait()
        return self

    def __exit__(self, *_: object) -> None:
        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()

    def _submit[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        loop = self._loop
        if loop is None:
            coroutine.close()
            raise RuntimeError("SyncBoundedCaller は with の中で使ってください。")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result()
        except BaseException:
            future.cancel()
            raise

    def call[T](
        self,
        fn: Callable[[Attempt], T],
        *,
        label: str = "",
        idempotent: bool = True,
        adaptive: bool = False,
        final: bool = False,
    ) -> T:
        """同期の関数 ``fn`` を ``BoundedCaller.call`` と同じ規則で呼ぶ（thread safe）。"""
        context = contextvars.copy_context()

        async def invoke(attempt: Attempt) -> T:
            return await asyncio.to_thread(context.copy().run, fn, attempt)

        return self._submit(
            self.caller.call(
                invoke, label=label, idempotent=idempotent, adaptive=adaptive, final=final
            )
        )

    def map[I, T](
        self,
        items: Sequence[I],
        fn: Callable[[I, Attempt], T],
        *,
        labels: Sequence[str] | None = None,
        adaptive: bool = False,
    ) -> list[CallOutcome[T]]:
        """同期の関数 ``fn`` で ``BoundedCaller.map`` を行う（入力の順の結果。最後の再試行も）。"""
        context = contextvars.copy_context()

        async def invoke(item: I, attempt: Attempt) -> T:
            return await asyncio.to_thread(context.copy().run, fn, item, attempt)

        return self._submit(self.caller.map(items, invoke, labels=labels, adaptive=adaptive))


def policy_with_concurrency(
    policy: BoundedCallPolicy, max_concurrency: int, **changes: Any
) -> BoundedCallPolicy:
    """``policy`` の並行の上限（とほかの値）を変えた写しを返す。"""
    from dataclasses import replace

    return replace(policy, max_concurrency=max(1, int(max_concurrency)), **changes)
