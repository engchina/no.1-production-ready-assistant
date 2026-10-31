"""OCI Enterprise AI の HTTP の失敗の分類と、確かめる所の案内（#1684。3 製品で共通の対応表）。

3 製品は OCI Enterprise AI（OpenAI 互換の `/responses`）の失敗を、ここで分類して案内する。
分類は status code と応答の本文で決め、設定の誤り（時間をおいても直らない）と一時的な失敗を分ける。

2026-10-13 の実測（us-chicago-1 / ap-osaka-1 の `/openai/v1/responses`。#1684）:
- 実在しない・そのリージョンに無いモデル: 400 `Entity with key <model> not found`
  （例: 大阪の `xai.grok-4.3` / `xai.grok-4.6`、両リージョンの `xai.grok-9.9`）
- 無効な API key・別のリージョンの API key: 401 `The required information to complete
  authentication was not provided or was incorrect.`
- 有効な API key でも、その key に推論を許す IAM policy が無い: 404 `Authorization failed or
  requested resource not found.`（実在するモデルでも 404。モデルが無いときの 400 とは違う）
- 非 OpenAI のモデルで `OpenAI-Project` ヘッダーが無い: 400 `invalid_value`

OCI の 404 は「モデルが無い」ではなく「認可されない」ことが多いので、403 と同じ `authorization` に
分ける。OpenAI 互換の別の API（ターシャリ接続など）の 404 は Endpoint URL / API パスの誤りのことも
あるので、案内に両方を書く。

HTTP の失敗のほかに、応答が届かない・使えない失敗も同じ表で種類に分ける（#1715）:
`timeout`（待ち時間の上限）・`connection`（接続できない）・`incomplete`（出力の上限などで応答が
途中で終わった）・`response_format`（応答が指定の形式ではない）・`refusal`（モデルが応答を拒否した）。
2026-10-13 の実測（#1715。NL2SQL の回答前の確認の構造化出力）: `openai.gpt-oss-120b` は出力の
上限 1200 で推論の token が上限を使い切り `status=incomplete`
（`incomplete_details.reason=max_output_tokens`）、上限を上げても JSON が崩れて空白を上限まで
出し続けることがある。`xai.grok-4.3` はときどき HTTP 500 `Internal server error`
（`internal_error`）を返す。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

EnterpriseAiFailureKind = Literal[
    "model_not_found",
    "authentication",
    "authorization",
    "rate_limit",
    "unavailable",
    "request",
]

# HTTP の失敗以外の種類（#1715）。
EnterpriseAiCallFailureKind = Literal[
    "model_not_found",
    "authentication",
    "authorization",
    "rate_limit",
    "unavailable",
    "request",
    "timeout",
    "connection",
    "incomplete",
    "response_format",
    "refusal",
]

# 再試行してよい status code（一時的な失敗）。これ以外の 4xx は再試行しても直らない。
ENTERPRISE_AI_RETRYABLE_STATUS_CODES: Final = frozenset({429, 500, 502, 503, 504})
# 設定（モデル・接続・API key・IAM policy）の誤りで、時間をおいても直らない種類。
ENTERPRISE_AI_CONFIGURATION_FAILURE_KINDS: Final[frozenset[EnterpriseAiFailureKind]] = frozenset(
    {"model_not_found", "authentication", "authorization"}
)
# 種類ごとの error code（ジョブ・API の `error_code`。3 製品で同じ値）。
ENTERPRISE_AI_FAILURE_ERROR_CODES: Final[dict[EnterpriseAiFailureKind, str]] = {
    "model_not_found": "ENTERPRISE_AI_MODEL_NOT_FOUND",
    "authentication": "ENTERPRISE_AI_AUTHENTICATION_FAILED",
    "authorization": "ENTERPRISE_AI_AUTHORIZATION_FAILED",
    "rate_limit": "ENTERPRISE_AI_RATE_LIMITED",
    "unavailable": "ENTERPRISE_AI_UNAVAILABLE",
    "request": "ENTERPRISE_AI_REQUEST_REJECTED",
}

# HTTP 以外の種類の error code（#1715。HTTP の種類は ENTERPRISE_AI_FAILURE_ERROR_CODES）。
ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES: Final[dict[EnterpriseAiCallFailureKind, str]] = {
    "model_not_found": ENTERPRISE_AI_FAILURE_ERROR_CODES["model_not_found"],
    "authentication": ENTERPRISE_AI_FAILURE_ERROR_CODES["authentication"],
    "authorization": ENTERPRISE_AI_FAILURE_ERROR_CODES["authorization"],
    "rate_limit": ENTERPRISE_AI_FAILURE_ERROR_CODES["rate_limit"],
    "unavailable": ENTERPRISE_AI_FAILURE_ERROR_CODES["unavailable"],
    "request": ENTERPRISE_AI_FAILURE_ERROR_CODES["request"],
    "timeout": "ENTERPRISE_AI_TIMEOUT",
    "connection": "ENTERPRISE_AI_CONNECTION_FAILED",
    "incomplete": "ENTERPRISE_AI_OUTPUT_INCOMPLETE",
    "response_format": "ENTERPRISE_AI_INVALID_RESPONSE",
    "refusal": "ENTERPRISE_AI_REFUSED",
}
# 画面の「詳細」に出す種類の名前（#1715）。
ENTERPRISE_AI_FAILURE_KIND_LABELS_JA: Final[dict[EnterpriseAiCallFailureKind, str]] = {
    "model_not_found": "モデルが無い（設定の誤り）",
    "authentication": "認証の失敗（設定の誤り）",
    "authorization": "認可の失敗（設定の誤り）",
    "rate_limit": "呼び出しの上限（429）",
    "unavailable": "サービスの一時的な失敗（5xx）",
    "request": "要求の誤り（4xx）",
    "timeout": "タイムアウト",
    "connection": "接続の失敗",
    "incomplete": "応答が途中で終わった",
    "response_format": "応答の形式の誤り",
    "refusal": "応答の拒否",
}

# 応答の本文から外す秘密の値（API key・Bearer token など。#1715）。
_SECRET_PATTERNS: Final = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|authorization|password|secret|token)\b\s*[:=]\s*\S+"),
)
_PROVIDER_MESSAGE_MAX_LENGTH: Final = 300

_MODEL_NOT_FOUND_RE: Final = re.compile(r"entity with key .+ not found", re.IGNORECASE)
# 3 製品の失敗の文の書き方: NL2SQL / Agent `OCI Enterprise AI HTTP 404: …`、
# RAG `… returned 404: …`、httpx の既定 `Client error '404 Not Found' for url …`。
_HTTP_STATUS_RE: Final = re.compile(
    r"\bHTTP (\d{3})\b|\breturned (\d{3})\b|\b(?:Client|Server) error '(\d{3})\b"
)


@dataclass(frozen=True)
class EnterpriseAiHttpFailure:
    """OCI Enterprise AI の HTTP の失敗の分類。"""

    kind: EnterpriseAiFailureKind
    status_code: int

    @property
    def retryable(self) -> bool:
        """再試行で直る見込みがあるか（429 / 5xx）。"""
        return self.status_code in ENTERPRISE_AI_RETRYABLE_STATUS_CODES

    @property
    def configuration_error(self) -> bool:
        """設定の誤りか（時間をおいても直らない）。"""
        return self.kind in ENTERPRISE_AI_CONFIGURATION_FAILURE_KINDS

    @property
    def error_code(self) -> str:
        return ENTERPRISE_AI_FAILURE_ERROR_CODES[self.kind]


def classify_enterprise_ai_http_failure(
    status_code: int, body: str = ""
) -> EnterpriseAiHttpFailure:
    """HTTP の失敗（status code と応答の本文）を分類する。"""
    if status_code == 401:
        kind: EnterpriseAiFailureKind = "authentication"
    elif status_code in {403, 404}:
        kind = "authorization"
    elif status_code == 429:
        kind = "rate_limit"
    elif status_code >= 500:
        kind = "unavailable"
    elif status_code == 400 and _MODEL_NOT_FOUND_RE.search(body or ""):
        kind = "model_not_found"
    else:
        kind = "request"
    return EnterpriseAiHttpFailure(kind=kind, status_code=status_code)


def enterprise_ai_http_status_from_message(message: str) -> int | None:
    """`OCI Enterprise AI HTTP 404: …` のような文から status code を取り出す（無ければ None）。"""
    match = _HTTP_STATUS_RE.search(message or "")
    if match is None:
        return None
    return int(next(group for group in match.groups() if group))


def enterprise_ai_endpoint_host(endpoint: str) -> str:
    """案内に出す接続の名前（Endpoint URL のホスト。path・query は出さない）。"""
    text = (endpoint or "").strip()
    if not text:
        return ""
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host


def enterprise_ai_failure_actions(
    failure: EnterpriseAiHttpFailure,
    *,
    model_id: str = "",
    endpoint_host: str = "",
) -> list[str]:
    """失敗の種類ごとの、確かめる所の案内（利用者・管理者向けの日本語の文）。"""
    model = f"「{model_id}」" if model_id else ""
    connection = f"（{endpoint_host}）" if endpoint_host else ""
    status = failure.status_code
    if failure.kind == "model_not_found":
        return [
            f"モデル{model}は、この接続{connection}のリージョンにありません（HTTP {status}）。"
            "システム設定の「モデル」で、接続のリージョンにあるモデル ID を登録するか、"
            "モデルを登録する接続を変えてください。"
        ]
    if failure.kind == "authentication":
        return [
            f"接続{connection}の API key が受け付けられませんでした（HTTP {status}）。"
            "システム設定の「モデル」で、接続の API key が Endpoint URL と同じリージョンで"
            "発行した有効な key か確かめ、保存し直してください。"
        ]
    if failure.kind == "authorization":
        return [
            f"接続{connection}でモデル{model}の呼び出しが認可されませんでした（HTTP {status}）。",
            "OCI の IAM で、この API key（request.principal.type = 'generativeaiapikey'）に"
            "Generative AI の推論を許す policy が、Project の compartment にあるか"
            "確かめてください。",
            "システム設定の「モデル」で、接続の Project OCID が Endpoint URL と同じリージョンの"
            " Project か確かめてください。OpenAI 互換の別の API では、Endpoint URL と API パスも"
            "確かめてください。",
        ]
    if failure.kind == "rate_limit":
        return [
            f"レート制限または quota の可能性があります（HTTP {status}）。"
            "しばらく待つか service limit を確認してください。"
        ]
    if failure.kind == "unavailable":
        return [
            f"サービス側または gateway 側の一時障害の可能性があります（HTTP {status}）。"
            "少し待って再試行し、OCI 側の稼働状況を確認してください。"
        ]
    return [
        f"リクエストが受け付けられませんでした（HTTP {status}）。"
        "「詳細」の応答の理由（モデルが受け付けない設定など）を確かめてください。"
    ]


def enterprise_ai_provider_message(body: str, *, limit: int = _PROVIDER_MESSAGE_MAX_LENGTH) -> str:
    """失敗の応答の本文から、画面の「詳細」に出せる短い理由を取り出す（#1715）。

    OpenAI 互換の `{"error": {"message": …, "code": …}}` は message と code だけを使う。秘密の値
    （Bearer token・API key など）は伏せ、空白をまとめて `limit` 文字で切る。
    """
    text = (body or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error", parsed)
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            code = str(error.get("code") or error.get("type") or "").strip()
            if message or code:
                text = f"{message} ({code})" if message and code else message or code
        elif isinstance(error, str) and error.strip():
            text = error.strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


# --------------------------------------------------------------------------- 未完了の応答（#1719）

# Responses API の `incomplete_details.reason` のうち、出力の上限に達したもの。
ENTERPRISE_AI_INCOMPLETE_OUTPUT_LIMIT_REASON: Final = "max_output_tokens"
# 未完了の応答の error code（ジョブ・API の `error_code`。3 製品で同じ値）。
ENTERPRISE_AI_INCOMPLETE_ERROR_CODE: Final = "ENTERPRISE_AI_OUTPUT_INCOMPLETE"


def _usage_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True)
class EnterpriseAiIncomplete:
    """Responses API の未完了の応答（`status` が completed 以外）の理由と使った token（#1719）。

    2026-10-13 の実測（NL2SQL のコメント管理の SQL 生成と同じ要求。5 表・32 列）:
    - openai.gpt-oss-120b（推論 medium・上限 1200）: `status=incomplete`・
      `incomplete_details.reason=max_output_tokens`・output 1200 のうち reasoning 1198
      （本文 0 文字）
    - xai.grok-4.3（推論 medium）: 上限 1200 でも completed のことがあり、上限 300 では incomplete
    推論のあるモデルは推論の token も出力の上限に数える。
    """

    status: str
    reason: str = ""
    max_output_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def output_limit_reached(self) -> bool:
        """出力の上限に達して未完了か（上限を広げた送り直しで直る見込みがある）。"""
        return self.reason == ENTERPRISE_AI_INCOMPLETE_OUTPUT_LIMIT_REASON

    def detail(self) -> str:
        """「詳細」に出す文（理由・上限・使った token。応答の本文・secret は含めない）。"""
        parts = [f"status={self.status or '-'}", f"理由={self.reason or '不明'}"]
        if self.max_output_tokens is not None:
            parts.append(f"出力の上限={self.max_output_tokens} tokens")
        if self.output_tokens is not None:
            used = f"使った出力={self.output_tokens} tokens"
            if self.reasoning_tokens is not None:
                used += f"（うち推論 {self.reasoning_tokens}）"
            parts.append(used)
        return "・".join(parts)


def enterprise_ai_incomplete_from_response(
    response: Mapping[str, object], *, max_output_tokens: int | None = None
) -> EnterpriseAiIncomplete | None:
    """Responses API の応答が未完了なら、その理由と使った token を返す（完了なら None）。"""
    status = str(response.get("status") or "").strip()
    if status not in {"incomplete", "failed", "cancelled", "queued", "in_progress"}:
        return None
    details = response.get("incomplete_details")
    reason = ""
    if isinstance(details, Mapping):
        reason = str(details.get("reason") or "").strip()
    usage = response.get("usage")
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    if isinstance(usage, Mapping):
        output_tokens = _usage_int(usage.get("output_tokens"))
        output_details = usage.get("output_tokens_details")
        if isinstance(output_details, Mapping):
            reasoning_tokens = _usage_int(output_details.get("reasoning_tokens"))
    return EnterpriseAiIncomplete(
        status=status,
        reason=reason,
        max_output_tokens=max_output_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
    )


def incomplete_retry_phrase(*, widened: bool, lowered_effort: bool) -> str:
    """未完了で終わった呼び出しの、試した送り直しの句（#2023。3 製品と rag_engine で同じ文）。"""
    if widened and lowered_effort:
        return "出力の上限を広げ、推論の量を下げて送り直しても"
    if widened:
        return "上限を広げて送り直しても"
    if lowered_effort:
        return "推論の量を下げて送り直しても"
    return ""


def enterprise_ai_incomplete_message(
    incomplete: EnterpriseAiIncomplete,
    *,
    model_id: str = "",
    retried: bool = False,
    lowered_effort: bool = False,
    reasoning_effort: str = "",
) -> str:
    """未完了の応答の、利用者向けの文（理由・上限・使った token と、確かめる所）。

    ``retried`` は出力の上限を広げて送り直したか、``lowered_effort`` は推論の量を下げて
    送り直したか（#2023）。``reasoning_effort`` は最後に送った推論の量で、まだ下げられる
    （medium 以上）ときだけ「推論の量を下げる」を直し方に入れる（low より下は送らない＝
    endpoint の既定になり、増えることがある）。
    """
    model = f"モデル「{model_id}」の" if model_id else ""
    if incomplete.output_limit_reached:
        retry = incomplete_retry_phrase(widened=retried, lowered_effort=lowered_effort)
        effort = "「推論の量」を下げるか、" if reasoning_effort in {"medium", "high"} else ""
        return (
            f"{model}応答が{retry}出力の上限に達して未完了でした（{incomplete.detail()}）。"
            "推論のあるモデルは推論の token も出力の上限に数えます。"
            "システム設定 › モデル で、このモデルの「出力の上限（tokens）」を大きくするか、"
            f"{effort}対象を減らしてやり直してください。"
        )
    if incomplete.reason == "content_filter":
        return (
            f"{model}応答が安全の判定で途中で止められました（{incomplete.detail()}）。"
            "表現を変えるか、対象を分けてやり直してください。"
        )
    return (
        f"{model}応答が未完了でした（{incomplete.detail()}）。"
        "時間をおいてやり直し、続くときはモデルの設定を確かめてください。"
    )
