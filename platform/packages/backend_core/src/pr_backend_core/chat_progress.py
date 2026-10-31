"""チャットの処理の段階のイベント（3 製品共通の契約・記録・配信。#1359）。

チャットの回答の処理の段階（画面の `ChatProgress`）を、段階の一覧の snapshot の置き換え
ではなく、**追記型のイベント**で配る。業界の定石（AG-UI の STEP_STARTED / STEP_FINISHED、
SSE の `id:` と `Last-Event-ID`、OpenAI の background mode の `starting_after`）に倣う。

- **契約**: `ChatProgressStepEvent`（段階の状態）と `ChatProgressTerminalEvent`（終端）。
  どちらも対象（`target_id`。RAG は回答のメッセージ、NL2SQL はジョブ、Agent は Run）
  ごとに単調に増える `seq`（1 から連続）と、試行（`attempt`。引き継いだ実行で増える）を
  持つ。段階のイベントはその段階の今の状態の全体（状態・時刻・補足）を持つので、受け取る
  側は `step_id` で結んで新しい方を使えばよい。正本の JSON は
  `platform/contracts/chat-progress/chat-progress-events.json`
  （`tests/test_chat_progress_contract.py` が生成と一致を確かめる）。
- **記録**: `ChatProgressRecorder`。製品は「どこでどの段階を始め・終えるか」だけを書く。
  保存先は製品の既存の保存（RAG の回答の記録・NL2SQL のジョブ・Agent の Run）で、記録は
  イベントの一覧（`events`）を返し、`sink` に 1 件ずつ渡す。保存済みの一覧から作り直せば
  続きの番号から記録する（再起動・引き継ぎ）。
- **配信**: polling（`chat_progress_page`。`since=<seq>` より後のイベント）と SSE
  （`chat_progress_sse_response`。`id:` に `seq` を入れ、`Last-Event-ID` / `since` の
  続きから送る。heartbeat を `heartbeat` のイベントで送る。保存先を読み直して送るので、
  別の worker が記録していても届く）。

段階の名前（利用者向けの文言）は契約に入れない。画面が製品の段階の定義（`kind` →
i18n の名前）で付ける。`detail` は短い補足だけ（件数・回数など）。SQL・ORA コード・
例外の種類などの技術的な詳細は入れない。

段を開いて見る詳細（`details`。#2294）は `ChatProgressStepDetails`（見出しと値・行・SQL や
JSON の箱）で送る。上限を超えた分は切って `truncated` を付け、秘密らしい値（password・token・
API キー・Bearer・URL の資格情報）は伏せる（`ChatProgressStepDetails` の検証で必ず通る）。

**段は span（#3226）**: 処理の経過は trace（span の木）の投影で、段のイベントは span の開始・
表示用のデータの変更・終了を表す（`ChatProgressRecorder.record_span`。`step_id` は span の id、
`parent_id` は親の span、`name` は span の名前、`root` は実行の全体の span）。画面は親子から入れ子を
作り、`kind`（工程の種類）があれば製品の定義で、無ければ共通の規則（技術の span の名前）で
名前を付ける。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    model_serializer,
    model_validator,
)
from starlette.responses import Response, StreamingResponse

CHAT_PROGRESS_SCHEMA_VERSION = 1
# SSE のイベントの名前（`event:`）。段階のイベントと、イベントの無い間の heartbeat。
CHAT_PROGRESS_SSE_EVENT = "chat_progress"
CHAT_PROGRESS_SSE_HEARTBEAT_EVENT = "heartbeat"
# SSE の応答の header（中継の buffering を切る。Nginx は `X-Accel-Buffering: no` を見る）。
CHAT_PROGRESS_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
# イベントの無い間に heartbeat を送る間隔（秒）。画面はこれで配信が途絶えていないかを判定する。
CHAT_PROGRESS_SSE_HEARTBEAT_SECONDS = 10.0
# SSE の配信が保存先を読み直す間隔（秒）。
CHAT_PROGRESS_SSE_POLL_SECONDS = 1.0
# 1 本の SSE の接続の上限（秒）。超えたら閉じ、ブラウザが `Last-Event-ID` で続きから張り直す。
CHAT_PROGRESS_SSE_MAX_SECONDS = 600.0
# ブラウザが張り直すまでの待ち（ms。SSE の `retry:`）。
CHAT_PROGRESS_SSE_RETRY_MS = 2000


@dataclass(frozen=True, slots=True)
class ChatProgressSseTiming:
    """SSE の配信の間隔（秒）。`configure_chat_progress_sse` で process 全体の既定を変えられる。"""

    poll_seconds: float = CHAT_PROGRESS_SSE_POLL_SECONDS
    heartbeat_seconds: float = CHAT_PROGRESS_SSE_HEARTBEAT_SECONDS
    max_seconds: float = CHAT_PROGRESS_SSE_MAX_SECONDS


_sse_timing = ChatProgressSseTiming()


def chat_progress_sse_timing() -> ChatProgressSseTiming:
    """今の SSE の配信の間隔（引数で渡さなかった値に使う）。"""
    return _sse_timing


def configure_chat_progress_sse(
    *,
    poll_seconds: float | None = None,
    heartbeat_seconds: float | None = None,
    max_seconds: float | None = None,
) -> ChatProgressSseTiming:
    """SSE の配信の間隔の既定を変える（起動時の設定・テスト）。前の値を返す（テストで戻す）。"""
    global _sse_timing
    previous = _sse_timing
    for value in (poll_seconds, heartbeat_seconds, max_seconds):
        if value is not None and value <= 0:
            raise ValueError("配信の間隔は 0 より大きい値にしてください。")
    _sse_timing = ChatProgressSseTiming(
        poll_seconds=poll_seconds if poll_seconds is not None else previous.poll_seconds,
        heartbeat_seconds=(
            heartbeat_seconds if heartbeat_seconds is not None else previous.heartbeat_seconds
        ),
        max_seconds=max_seconds if max_seconds is not None else previous.max_seconds,
    )
    return previous


def restore_chat_progress_sse(timing: ChatProgressSseTiming) -> None:
    """`configure_chat_progress_sse` の前の値に戻す。"""
    global _sse_timing
    _sse_timing = timing


type ChatProgressStepStatus = Literal["pending", "running", "done", "failed", "skipped"]
type ChatProgressTerminalStatus = Literal["done", "failed", "cancelled"]
type ChatProgressParamValue = str | int | float | bool
type ChatProgressClock = Callable[[], datetime]

# 状態の進み具合（完了・失敗・スキップは終わった段階として同じ）。状態は進む方へだけ変える。
STATUS_RANK: dict[str, int] = {"pending": 0, "running": 1, "done": 2, "failed": 2, "skipped": 2}

_ID_MAX = 200
_DETAIL_MAX = 200


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """値の無い任意の項目（None）は出さない（契約: 省く。API の応答・保存・SSE で同じ形）。"""
        data: dict[str, Any] = handler(self)
        return {key: value for key, value in data.items() if value is not None}

    schema_version: Literal[1] = Field(default=1, description="イベントの形の版。")
    seq: int = Field(ge=1, description="対象ごとに 1 から連続して増える番号（SSE の `id:`）。")
    target_id: str = Field(
        min_length=1,
        max_length=_ID_MAX,
        description="対象（RAG の回答のメッセージ・NL2SQL のジョブ・Agent の Run）の id。",
    )
    attempt: int = Field(
        default=0,
        ge=0,
        description="試行。引き継いだ実行で増え、増えたら段階の一覧を作り直す。",
    )
    emitted_at: datetime = Field(description="記録した時刻（ISO 8601）。")


# ---------------------------------------------------------------------------
# 段の詳細（#2294）
# ---------------------------------------------------------------------------

# 詳細の上限（超えた分は切って `truncated` を付ける。イベントは段の状態が変わるたびに
# 全体を送るので小さく保つ）。
DETAILS_LABEL_MAX = 80
DETAILS_VALUE_MAX = 1000
DETAILS_LINE_MAX = 500
DETAILS_CODE_MAX = 4000
DETAILS_MAX_FIELDS = 20
DETAILS_MAX_LINES = 20
DETAILS_MAX_CODE_BLOCKS = 3
_MASKED = "***"

# 伏せる値: `password=…` / `"api_key": "…"` などの代入、`Bearer …`、API キー（`prak_…`）、
# URL の資格情報。
_SENSITIVE_NAME = (
    r"[A-Za-z_\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|credential)[A-Za-z_\-]*"
)
_SECRET_JSON_RE = re.compile(rf'(?i)("{_SENSITIVE_NAME}"\s*:\s*)"(?:[^"\\]|\\.)*"')
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b({_SENSITIVE_NAME})(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;)\"'}}]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_API_KEY_RE = re.compile(r"\bprak_[A-Za-z0-9_-]+")
_URL_CREDENTIAL_RE = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)


def redact_chat_progress_text(text: str) -> str:
    """段の詳細に入れる文から秘密らしい値を伏せる（3 製品共通）。"""

    redacted = _SECRET_JSON_RE.sub(rf'\1"{_MASKED}"', text)
    redacted = _SECRET_ASSIGNMENT_RE.sub(rf"\1\2{_MASKED}", redacted)
    redacted = _BEARER_RE.sub(rf"\1 {_MASKED}", redacted)
    redacted = _API_KEY_RE.sub(_MASKED, redacted)
    return _URL_CREDENTIAL_RE.sub(rf"\1{_MASKED}@", redacted)


def _clip(value: object, limit: int) -> tuple[str, bool]:
    text = redact_chat_progress_text(value if isinstance(value, str) else str(value))
    if len(text) <= limit:
        return text, False
    return f"{text[:limit]}…", True


class _DetailsBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """値の無い任意の項目（None・空の list）は出さない（イベントと同じ契約: 省く）。"""
        data: dict[str, Any] = handler(self)
        return {key: value for key, value in data.items() if value is not None and value != []}


class ChatProgressDetailField(_DetailsBase):
    """詳細の見出しと値（エンジン・行数・選んだ条件など）。"""

    label: str = Field(min_length=1, max_length=DETAILS_LABEL_MAX + 1)
    value: str = Field(min_length=1, max_length=DETAILS_VALUE_MAX + 1)
    mono: bool | None = Field(default=None, description="識別子・SQL の断片などを等幅で出す。")


class ChatProgressDetailCode(_DetailsBase):
    """詳細の長い値（SQL・ツールの引数と結果の JSON）。画面は箱の中でスクロールして出す。"""

    label: str | None = Field(default=None, max_length=DETAILS_LABEL_MAX + 1)
    language: Literal["json", "sql", "text"] | None = None
    text: str = Field(min_length=1, max_length=DETAILS_CODE_MAX + 1)


CALL_CONTENT_MAX = 64000


class CallContent(_DetailsBase):
    """呼び出し時点の内容。未記録と空の応答を区別する。"""

    status: Literal["recorded", "truncated", "not_recorded", "withheld"] = "not_recorded"
    text: str = Field(default="", max_length=CALL_CONTENT_MAX + 1)

    @model_validator(mode="before")
    @classmethod
    def _safe(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        text, cut = _clip(data.get("text", ""), CALL_CONTENT_MAX)
        return {
            **data,
            "text": text,
            "status": "truncated" if cut else data.get("status", "not_recorded"),
        }


class CallDetails(_DetailsBase):
    """入力・結果・Schema の snapshot。外部の telemetry には送らない。"""

    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)

    capture_enabled: bool = True
    visibility: Literal["owner", "system_admin"] = "owner"
    input: CallContent = Field(default_factory=CallContent)
    output: CallContent = Field(default_factory=CallContent)
    schema_: CallContent = Field(default_factory=CallContent, alias="schema")


class ChatProgressStepDetails(_DetailsBase):
    """段を開いて見る詳細（3 製品共通。画面の `RunStepDetails` と同じ形。#2294）。

    検証の前に、上限を超えた値・要素を切り（`truncated` を付ける）、秘密らしい値を伏せる。保存済みの
    記録も同じ規則で読み直すので、長すぎる記録でイベントが捨てられることはない。
    """

    fields: list[ChatProgressDetailField] = Field(
        default_factory=list, max_length=DETAILS_MAX_FIELDS
    )
    lines: list[str] = Field(default_factory=list, max_length=DETAILS_MAX_LINES)
    code: list[ChatProgressDetailCode] = Field(
        default_factory=list, max_length=DETAILS_MAX_CODE_BLOCKS
    )
    call: CallDetails | None = None
    truncated: bool | None = Field(default=None, description="送る側で長さを切った。")

    @model_validator(mode="before")
    @classmethod
    def _bound(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        truncated = data.get("truncated") is True
        fields: list[dict[str, Any]] = []
        for item in _as_items(data.get("fields")):
            raw = item.model_dump() if isinstance(item, BaseModel) else item
            if not isinstance(raw, dict) or not raw.get("label") or raw.get("value") in (None, ""):
                continue
            label, cut_label = _clip(raw["label"], DETAILS_LABEL_MAX)
            value, cut_value = _clip(raw["value"], DETAILS_VALUE_MAX)
            truncated = truncated or cut_label or cut_value
            field_data: dict[str, Any] = {"label": label, "value": value}
            if raw.get("mono") is True:
                field_data["mono"] = True
            fields.append(field_data)
        lines: list[str] = []
        for line in _as_items(data.get("lines")):
            if not isinstance(line, str) or not line:
                continue
            clipped, cut = _clip(line, DETAILS_LINE_MAX)
            truncated = truncated or cut
            lines.append(clipped)
        code: list[dict[str, Any]] = []
        for item in _as_items(data.get("code")):
            raw = item.model_dump() if isinstance(item, BaseModel) else item
            if not isinstance(raw, dict) or not raw.get("text"):
                continue
            text, cut = _clip(raw["text"], DETAILS_CODE_MAX)
            truncated = truncated or cut
            block: dict[str, Any] = {"text": text}
            if raw.get("label"):
                block["label"], cut_label = _clip(raw["label"], DETAILS_LABEL_MAX)
                truncated = truncated or cut_label
            if raw.get("language") in ("json", "sql", "text"):
                block["language"] = raw["language"]
            code.append(block)
        if (
            len(fields) > DETAILS_MAX_FIELDS
            or len(lines) > DETAILS_MAX_LINES
            or len(code) > DETAILS_MAX_CODE_BLOCKS
        ):
            truncated = True
        return {
            **({"call": data["call"]} if data.get("call") is not None else {}),
            "fields": fields[:DETAILS_MAX_FIELDS],
            "lines": lines[:DETAILS_MAX_LINES],
            "code": code[:DETAILS_MAX_CODE_BLOCKS],
            "truncated": True if truncated else None,
        }

    def is_empty(self) -> bool:
        return not (self.fields or self.lines or self.code or self.call)


def _as_items(value: object) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def chat_progress_details(
    *,
    fields: Iterable[tuple[str, object] | tuple[str, object, bool]] = (),
    lines: Iterable[str] = (),
    code: Iterable[tuple[str | None, str, Literal["json", "sql", "text"] | None]] = (),
    truncated: bool = False,
) -> ChatProgressStepDetails | None:
    """段の詳細を作る（値の無い見出しは捨てる。中身が無ければ None）。

    `fields` は `(見出し, 値)` か `(見出し, 値, 等幅)`、`code` は `(見出し, 本文, 種類)`。
    値は文字列にし、上限で切り、秘密らしい値を伏せる。
    """

    field_items: list[dict[str, Any]] = []
    for item in fields:
        label, value = item[0], item[1]
        if value is None or value == "":
            continue
        field_data: dict[str, Any] = {"label": label, "value": str(value)}
        if len(item) > 2 and item[2]:
            field_data["mono"] = True
        field_items.append(field_data)
    code_items = [
        {"label": label, "text": text, "language": language}
        for label, text, language in code
        if text
    ]
    details = ChatProgressStepDetails.model_validate(
        {
            "fields": field_items,
            "lines": [line for line in lines if line],
            "code": code_items,
            "truncated": truncated or None,
        }
    )
    return None if details.is_empty() else details


class ChatProgressStepEvent(_EventBase):
    """1 つの段階の今の状態（状態・時刻・補足の全体）。"""

    type: Literal["step"] = "step"
    step_id: str = Field(
        min_length=1, max_length=_ID_MAX, description="段階の id（対象の中で一意）。"
    )
    kind: str | None = Field(
        default=None,
        max_length=_ID_MAX,
        description="段階の定義の種類（画面の名前の key）。省略時は step_id。",
    )
    status: ChatProgressStepStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str | None = Field(
        default=None, max_length=_DETAIL_MAX, description="短い補足（件数・回数など）。"
    )
    params: dict[str, ChatProgressParamValue] | None = Field(
        default=None, description="画面が名前・補足を組み立てる値（ツール名・件数など）。"
    )
    details: ChatProgressStepDetails | None = Field(
        default=None,
        description="段を開いて見る詳細（見出しと値・行・SQL や JSON。上限で切り、秘密を伏せる）。",
    )
    name: str | None = Field(
        default=None, max_length=128, description="span の名前（段が span のとき。#3226）。"
    )
    parent_id: str | None = Field(
        default=None, max_length=_ID_MAX, description="親の span の id（入れ子を作る）。"
    )
    root: bool | None = Field(default=None, description="実行の全体の span。")
    trace_id: str | None = Field(
        default=None, pattern="^[0-9a-f]{32}$", description="span の trace id。"
    )
    attributes: dict[str, ChatProgressParamValue] | None = Field(
        default=None, description="span の安全な属性（モデル・ツール・利用量・試行）。"
    )
    cancelled: bool | None = Field(
        default=None, description="停止で終わった span（状態は skipped）。"
    )
    product: str | None = Field(
        default=None,
        max_length=32,
        pattern="^[a-z0-9_]+$",
        description="span を書いた製品（rag / nl2sql / agent。#3272）。",
    )
    display_name: str | None = Field(
        default=None,
        max_length=128,
        description="span を書いた製品が付けた工程の名前（名詞。別の製品の画面が出す。#3272）。",
    )


class ChatProgressTerminalEvent(_EventBase):
    """対象の処理の終わり（完了・失敗・停止）。この後、同じ試行の段階のイベントは来ない。"""

    type: Literal["terminal"] = "terminal"
    status: ChatProgressTerminalStatus


ChatProgressEvent = Annotated[
    ChatProgressStepEvent | ChatProgressTerminalEvent, Field(discriminator="type")
]
CHAT_PROGRESS_EVENT_ADAPTER: TypeAdapter[ChatProgressStepEvent | ChatProgressTerminalEvent] = (
    TypeAdapter(ChatProgressEvent)
)
CHAT_PROGRESS_EVENTS_ADAPTER: TypeAdapter[
    list[ChatProgressStepEvent | ChatProgressTerminalEvent]
] = TypeAdapter(list[ChatProgressEvent])


class ChatProgressPage(BaseModel):
    """polling の応答（`since` より後のイベント）。"""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    attempt: int = Field(ge=0, description="今の試行。")
    events: list[ChatProgressEvent] = Field(default_factory=list)
    last_seq: int = Field(ge=0, description="記録の最後の番号（次の `since` に使う）。")
    terminal: bool = Field(description="今の試行が終わったか。")


def parse_chat_progress_events(
    raw: object,
) -> list[ChatProgressStepEvent | ChatProgressTerminalEvent]:
    """保存した JSON（list）をイベントの一覧にする。

    形の違う要素は捨てる（壊れた保存で画面を止めない）。
    """

    if not isinstance(raw, list):
        return []
    events: list[ChatProgressStepEvent | ChatProgressTerminalEvent] = []
    for item in raw:
        try:
            events.append(CHAT_PROGRESS_EVENT_ADAPTER.validate_python(item))
        except ValueError:
            continue
    return events


def dump_chat_progress_events(
    events: Iterable[ChatProgressStepEvent | ChatProgressTerminalEvent],
) -> list[dict[str, Any]]:
    """保存・応答の JSON（list）にする。"""

    return [event.model_dump(mode="json", exclude_none=True) for event in events]


# ---------------------------------------------------------------------------
# 組み立て（イベント → 段階の一覧）。画面の `reduceChatProgressEvents` と同じ規則。
# ---------------------------------------------------------------------------


@dataclass
class ChatProgressStepState:
    """組み立てた 1 つの段階。"""

    step_id: str
    kind: str
    status: ChatProgressStepStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str | None = None
    params: dict[str, ChatProgressParamValue] | None = None
    details: ChatProgressStepDetails | None = None
    span: dict[str, Any] | None = None


@dataclass
class ChatProgressFold:
    """イベントの一覧を組み立てた結果。"""

    target_id: str | None = None
    attempt: int = 0
    last_seq: int = 0
    terminal: ChatProgressTerminalStatus | None = None
    steps: dict[str, ChatProgressStepState] = field(default_factory=dict)

    def step_list(self) -> list[ChatProgressStepState]:
        return list(self.steps.values())


def fold_chat_progress_events(
    events: Iterable[ChatProgressStepEvent | ChatProgressTerminalEvent],
) -> ChatProgressFold:
    """イベントを段階の一覧にまとめる（3 製品共通の規則）。

    - `seq` の順に適用し、適用済みの番号以下（古い・重複）は捨てる。
    - 試行（`attempt`）が増えたら一覧を作り直し、古い試行のイベントは捨てる。
    - 段階は `step_id` で結び、最初に出た順に並べる。状態は進む方へだけ変える
      （戻るイベントは捨てる）。
    - 終端のイベントの後の、同じ試行の段階のイベントは捨てる。
    """

    fold = ChatProgressFold()
    for event in sorted(events, key=lambda item: item.seq):
        apply_chat_progress_event(fold, event)
    return fold


def apply_chat_progress_event(
    fold: ChatProgressFold, event: ChatProgressStepEvent | ChatProgressTerminalEvent
) -> bool:
    """1 件を適用する。適用したら True（捨てたら False）。"""

    if event.seq <= fold.last_seq:
        return False
    if fold.target_id is not None and event.target_id != fold.target_id:
        return False
    fold.last_seq = event.seq
    fold.target_id = event.target_id
    if event.attempt < fold.attempt:
        return False
    if event.attempt > fold.attempt:
        fold.attempt = event.attempt
        fold.terminal = None
        fold.steps = {}
    if isinstance(event, ChatProgressTerminalEvent):
        fold.terminal = event.status
        return True
    if fold.terminal is not None:
        return False
    before = fold.steps.get(event.step_id)
    if before is not None and STATUS_RANK[event.status] < STATUS_RANK[before.status]:
        return False
    fold.steps[event.step_id] = ChatProgressStepState(
        step_id=event.step_id,
        kind=event.kind or (before.kind if before else event.step_id),
        status=event.status,
        started_at=event.started_at or (before.started_at if before else None),
        finished_at=event.finished_at,
        detail=event.detail,
        params=event.params,
        details=event.details,
        span=_span_fields(event),
    )
    return True


_SPAN_FIELDS = (
    "name",
    "parent_id",
    "root",
    "trace_id",
    "attributes",
    "cancelled",
    "product",
    "display_name",
)


def _span_fields(event: ChatProgressStepEvent) -> dict[str, Any] | None:
    values = {key: getattr(event, key) for key in _SPAN_FIELDS if getattr(event, key) is not None}
    return values or None


# ---------------------------------------------------------------------------
# 記録
# ---------------------------------------------------------------------------

type ChatProgressSink = Callable[[ChatProgressStepEvent | ChatProgressTerminalEvent], None]


class ChatProgressSpanDisplay(Protocol):
    """span の表示用のデータ（`tracing.SpanDisplay`）。"""

    @property
    def kind(self) -> str | None: ...
    @property
    def params(self) -> dict[str, ChatProgressParamValue] | None: ...
    @property
    def details(self) -> ChatProgressStepDetails | None: ...


class ChatProgressSpanUpdate(Protocol):
    """span の知らせ（`tracing.SpanUpdate`。循環 import を避けて形だけを決める）。"""

    @property
    def span_id(self) -> str: ...
    @property
    def parent_span_id(self) -> str | None: ...
    @property
    def trace_id(self) -> str: ...
    @property
    def operation_name(self) -> str: ...
    @property
    def status(self) -> Literal["running", "done", "failed", "cancelled", "skipped"]: ...
    @property
    def started_at(self) -> datetime: ...
    @property
    def finished_at(self) -> datetime | None: ...
    @property
    def root(self) -> bool: ...
    @property
    def attributes(self) -> Mapping[str, ChatProgressParamValue]: ...
    @property
    def display(self) -> ChatProgressSpanDisplay | None: ...


class Unchanged(Enum):
    """`start` / `finish` / `update` の補足・値を「前の値のまま」にする印（`UNCHANGED`）。

    None は「消す」、`UNCHANGED`（既定）は「変えない」。
    """

    UNCHANGED = "unchanged"


UNCHANGED: Final = Unchanged.UNCHANGED


class ChatProgressRecorder:
    """1 つの対象の処理の段階のイベントを記録する（3 製品共通。#1359）。

    製品は段階の開始・終了を呼ぶだけでよい。記録はイベントの番号・時刻・状態の進み方（戻さない）・終端の
    確定を持つ。保存は製品が行う（`sink` に 1 件ずつ渡す。全体は `events`）。

    - `declare` は段階を待機中として先に出す（並びを決める。後の段階が先に始まっても
      並びは変わらない）。
    - `start` は実行中にする（`exclusive=True` なら、ほかの実行中の段階を完了にする）。
      完了した段階は実行中に戻さない（何もしない）。
    - `finish` は完了・失敗・スキップにする。`complete` は終端（実行中の段階を状態に合わせて終え、
      待機中の段階をスキップにしてから、終端のイベントを記録する）。
    - 終端の後は何も記録しない（`new_attempt` で次の試行を始めたときを除く）。

    スレッドから同時に呼ばない（製品の保存の lock の中で呼ぶ）。
    """

    def __init__(
        self,
        target_id: str,
        *,
        attempt: int = 0,
        events: Iterable[ChatProgressStepEvent | ChatProgressTerminalEvent] = (),
        sink: ChatProgressSink | None = None,
        clock: ChatProgressClock = _utc_now,
    ) -> None:
        if not target_id:
            raise ValueError("target_id は必須です。")
        self._target_id = target_id
        self._sink = sink
        self._clock = clock
        self._events = sorted(
            (event for event in events if event.target_id == target_id), key=lambda e: e.seq
        )
        self._fold = fold_chat_progress_events(self._events)
        self._seq = self._events[-1].seq if self._events else 0
        if attempt > self._fold.attempt:
            self._reset_attempt(attempt)
        self._attempt = max(attempt, self._fold.attempt)

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def terminal(self) -> ChatProgressTerminalStatus | None:
        return self._fold.terminal

    @property
    def events(self) -> list[ChatProgressStepEvent | ChatProgressTerminalEvent]:
        """記録したイベントの全体（保存済みの分を含む。`seq` の順）。"""
        return list(self._events)

    def steps(self) -> list[ChatProgressStepState]:
        """今の段階の一覧（今の試行）。"""
        return self._fold.step_list()

    def step(self, step_id: str) -> ChatProgressStepState | None:
        return self._fold.steps.get(step_id)

    def page(self, *, since: int = 0) -> ChatProgressPage:
        return chat_progress_page(self._target_id, self._events, since=since)

    def new_attempt(self, attempt: int) -> None:
        """次の試行を始める（段階の一覧を空にする。番号は続ける）。前の試行より大きい値だけ受け付ける。"""
        if attempt <= self._attempt:
            return
        self._reset_attempt(attempt)
        self._attempt = attempt

    def _reset_attempt(self, attempt: int) -> None:
        self._fold.attempt = attempt
        self._fold.terminal = None
        self._fold.steps = {}

    def declare(self, *step_ids: str, kind: str | None = None) -> list[ChatProgressStepEvent]:
        """まだ出ていない段階を待機中として出す（この順に並ぶ）。"""
        recorded: list[ChatProgressStepEvent] = []
        for step_id in step_ids:
            if step_id in self._fold.steps:
                continue
            event = self._record_step(step_id, status="pending", kind=kind)
            if event is not None:
                recorded.append(event)
        return recorded

    def start(
        self,
        step_id: str,
        *,
        kind: str | None = None,
        detail: str | None | Unchanged = UNCHANGED,
        params: dict[str, ChatProgressParamValue] | None | Unchanged = UNCHANGED,
        details: ChatProgressStepDetails | None | Unchanged = UNCHANGED,
        exclusive: bool = False,
        at: datetime | None = None,
    ) -> ChatProgressStepEvent | None:
        """段階を実行中にする。終わった段階は実行中に戻さない（None を返す）。"""
        before = self._fold.steps.get(step_id)
        if self._fold.terminal is not None:
            return None
        if before is not None and STATUS_RANK[before.status] > STATUS_RANK["running"]:
            return None
        if exclusive:
            for other in list(self._fold.steps.values()):
                if other.step_id != step_id and other.status == "running":
                    self.finish(other.step_id, at=at)
        if before is not None and before.status == "running":
            return self.update(step_id, kind=kind, detail=detail, params=params, details=details)
        return self._record_step(
            step_id,
            status="running",
            kind=kind,
            started_at=at or self._clock(),
            finished_at=None,
            detail=detail,
            params=params,
            details=details,
        )

    def finish(
        self,
        step_id: str,
        status: Literal["done", "failed", "skipped"] = "done",
        *,
        kind: str | None = None,
        detail: str | None | Unchanged = UNCHANGED,
        params: dict[str, ChatProgressParamValue] | None | Unchanged = UNCHANGED,
        details: ChatProgressStepDetails | None | Unchanged = UNCHANGED,
        at: datetime | None = None,
    ) -> ChatProgressStepEvent | None:
        """段階を終える（完了・失敗・スキップ）。同じ状態で変わりが無ければ記録しない。"""
        if self._fold.terminal is not None:
            return None
        before = self._fold.steps.get(step_id)
        finished_at: datetime | None
        if before is not None and before.status == status and before.finished_at is not None:
            finished_at = before.finished_at
        elif status == "skipped" and (before is None or before.status == "pending"):
            # 始まらなかった段階には終了の時刻を付けない（所要時間を出さない）。
            finished_at = None
        else:
            finished_at = at or self._clock()
        return self._record_step(
            step_id,
            status=status,
            kind=kind,
            started_at=before.started_at if before is not None else None,
            finished_at=finished_at,
            detail=detail,
            params=params,
            details=details,
        )

    def update(
        self,
        step_id: str,
        *,
        kind: str | None = None,
        detail: str | None | Unchanged = UNCHANGED,
        params: dict[str, ChatProgressParamValue] | None | Unchanged = UNCHANGED,
        details: ChatProgressStepDetails | None | Unchanged = UNCHANGED,
    ) -> ChatProgressStepEvent | None:
        """状態を変えずに補足・種類を変える（出ていない段階は何もしない）。"""
        before = self._fold.steps.get(step_id)
        if before is None or self._fold.terminal is not None:
            return None
        return self._record_step(
            step_id,
            status=before.status,
            kind=kind,
            started_at=before.started_at,
            finished_at=before.finished_at,
            detail=detail,
            params=params,
            details=details,
        )

    def record_span(self, update: ChatProgressSpanUpdate) -> ChatProgressStepEvent | None:
        """span の開始・表示用のデータの変更・終了を段として記録する（#3226）。

        `update` は `pr_backend_core.observability.tracing.SpanUpdate`（span の id・親・名前・状態・
        時刻・安全な属性・表示用のデータ）。停止は `skipped` に `cancelled` を付ける。
        """
        display = update.display
        status: ChatProgressStepStatus = (
            "skipped" if update.status == "cancelled" else update.status
        )
        return self._record_step(
            update.span_id,
            status=status,
            kind=display.kind if display is not None else None,
            started_at=update.started_at,
            finished_at=update.finished_at,
            params=display.params if display is not None else None,
            details=display.details if display is not None else None,
            span={
                "name": update.operation_name,
                "parent_id": update.parent_span_id,
                "root": True if update.root else None,
                "trace_id": update.trace_id,
                "attributes": dict(update.attributes) or None,
                "cancelled": True if update.status == "cancelled" else None,
            },
        )

    def complete(
        self, status: ChatProgressTerminalStatus = "done", *, at: datetime | None = None
    ) -> ChatProgressTerminalEvent | None:
        """終端にする。実行中の段階は完了（失敗なら失敗、停止ならスキップ）、待機中はスキップにする。"""
        if self._fold.terminal is not None:
            return None
        finished_at = at or self._clock()
        running_status: Literal["done", "failed", "skipped"] = (
            "done" if status == "done" else "failed" if status == "failed" else "skipped"
        )
        for step in list(self._fold.steps.values()):
            if step.status == "running" and step.span is not None and status == "cancelled":
                # 停止で終わった span は「停止」と出す（スキップと分ける。#3226）。
                self._record_step(
                    step.step_id,
                    status="skipped",
                    started_at=step.started_at,
                    finished_at=finished_at,
                    span={**step.span, "cancelled": True},
                )
            elif step.status == "running":
                self.finish(step.step_id, running_status, at=finished_at)
            elif step.status == "pending":
                self.finish(step.step_id, "skipped", at=finished_at)
        event = ChatProgressTerminalEvent(
            seq=self._seq + 1,
            target_id=self._target_id,
            attempt=self._attempt,
            emitted_at=finished_at,
            status=status,
        )
        self._append(event)
        return event

    def _record_step(
        self,
        step_id: str,
        *,
        status: ChatProgressStepStatus,
        kind: str | None = None,
        started_at: datetime | None | Unchanged = UNCHANGED,
        finished_at: datetime | None | Unchanged = UNCHANGED,
        detail: str | None | Unchanged = UNCHANGED,
        params: dict[str, ChatProgressParamValue] | None | Unchanged = UNCHANGED,
        details: ChatProgressStepDetails | None | Unchanged = UNCHANGED,
        span: dict[str, Any] | None | Unchanged = UNCHANGED,
    ) -> ChatProgressStepEvent | None:
        before = self._fold.steps.get(step_id)
        if before is not None and STATUS_RANK[status] < STATUS_RANK[before.status]:
            return None
        span_values = (before.span if before else None) if span is UNCHANGED else span
        resolved_kind = kind or (before.kind if before else None) or step_id
        event = ChatProgressStepEvent(
            seq=self._seq + 1,
            target_id=self._target_id,
            attempt=self._attempt,
            emitted_at=self._clock(),
            step_id=step_id,
            kind=None if resolved_kind == step_id else resolved_kind,
            status=status,
            started_at=(before.started_at if before else None)
            if started_at is UNCHANGED
            else started_at,
            finished_at=(before.finished_at if before else None)
            if finished_at is UNCHANGED
            else finished_at,
            detail=(before.detail if before else None) if detail is UNCHANGED else detail,
            params=(before.params if before else None) if params is UNCHANGED else params,
            details=(before.details if before else None) if details is UNCHANGED else details,
            **{key: value for key, value in (span_values or {}).items() if value is not None},
        )
        if before is not None and _same_step(before, event):
            return None
        self._append(event)
        return event

    def _append(self, event: ChatProgressStepEvent | ChatProgressTerminalEvent) -> None:
        self._seq = event.seq
        self._events.append(event)
        apply_chat_progress_event(self._fold, event)
        if self._sink is not None:
            self._sink(event)


def _same_step(before: ChatProgressStepState, event: ChatProgressStepEvent) -> bool:
    return (
        before.status == event.status
        and before.kind == (event.kind or event.step_id)
        and before.started_at == event.started_at
        and before.finished_at == event.finished_at
        and before.detail == event.detail
        and before.params == event.params
        and before.details == event.details
        and before.span == _span_fields(event)
    )


# ---------------------------------------------------------------------------
# 配信（polling と SSE）
# ---------------------------------------------------------------------------


def chat_progress_page(
    target_id: str,
    events: Sequence[ChatProgressStepEvent | ChatProgressTerminalEvent],
    *,
    since: int = 0,
) -> ChatProgressPage:
    """polling の応答（`since` より後のイベント）。`terminal` は今の試行が終わったか。"""

    ordered = sorted((e for e in events if e.target_id == target_id), key=lambda e: e.seq)
    fold = fold_chat_progress_events(ordered)
    return ChatProgressPage(
        target_id=target_id,
        attempt=fold.attempt,
        events=[event for event in ordered if event.seq > since],
        last_seq=ordered[-1].seq if ordered else 0,
        terminal=fold.terminal is not None,
    )


def chat_progress_cursor(*, since: int | None = None, last_event_id: str | None = None) -> int:
    """続きの位置（`since` と `Last-Event-ID` の大きい方。読めない値は 0）。

    ブラウザの `EventSource` は、最初の接続では URL の `since` を、自動で張り直すときは
    最後に受け取った `id:` を `Last-Event-ID` で送る（張り直しのほうが新しい）。
    """

    cursor = max(0, since or 0)
    if last_event_id is not None:
        with contextlib.suppress(ValueError):
            cursor = max(cursor, int(last_event_id.strip()))
    return cursor


def format_sse(*, data: object, event: str | None = None, event_id: int | None = None) -> str:
    """SSE の 1 件（`id:` / `event:` / `data:`）。data は JSON にする。"""

    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def format_chat_progress_sse(event: ChatProgressStepEvent | ChatProgressTerminalEvent) -> str:
    return format_sse(
        data=event.model_dump(mode="json", exclude_none=True),
        event=CHAT_PROGRESS_SSE_EVENT,
        event_id=event.seq,
    )


type ChatProgressFetch = Callable[[int], Awaitable[ChatProgressPage | None]]


async def chat_progress_sse_stream(
    fetch: ChatProgressFetch,
    *,
    since: int = 0,
    first: ChatProgressPage | None = None,
    poll_seconds: float | None = None,
    heartbeat_seconds: float | None = None,
    max_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """保存先を読み直して、`since` より後のイベントを SSE で送る（終端を送ったら閉じる）。

    - `fetch(cursor)` は `cursor` より後のイベント（polling と同じ `ChatProgressPage`）を
      返す。対象が無くなったら None（配信を閉じる）。保存先を読むので、別の worker・別の
      プロセスが記録していても届く。
    - イベントの無い間は `heartbeat_seconds` ごとに `heartbeat` のイベント
      （`{"last_seq": N}`）を送る。
    - `max_seconds` を超えたら閉じる（ブラウザが `Last-Event-ID` で続きから張り直す）。
    - 間隔の引数を省いたら `chat_progress_sse_timing()`（`configure_chat_progress_sse`）の値。
    """

    timing = chat_progress_sse_timing()
    poll = poll_seconds if poll_seconds is not None else timing.poll_seconds
    heartbeat = heartbeat_seconds if heartbeat_seconds is not None else timing.heartbeat_seconds
    limit = max_seconds if max_seconds is not None else timing.max_seconds
    started = monotonic()
    last_sent = started
    cursor = since
    yield f"retry: {CHAT_PROGRESS_SSE_RETRY_MS}\n\n"
    page = first
    while True:
        if page is None:
            page = await fetch(cursor)
            if page is None:
                return
        for event in sorted(page.events, key=lambda item: item.seq):
            if event.seq <= cursor:
                continue
            cursor = event.seq
            last_sent = monotonic()
            yield format_chat_progress_sse(event)
        if page.terminal and cursor >= page.last_seq:
            return
        now = monotonic()
        if now - started >= limit:
            return
        if now - last_sent >= heartbeat:
            last_sent = now
            yield format_sse(data={"last_seq": cursor}, event=CHAT_PROGRESS_SSE_HEARTBEAT_EVENT)
        page = None
        await sleep(poll)


async def chat_progress_sse_response(
    fetch: ChatProgressFetch,
    *,
    since: int | None = None,
    last_event_id: str | None = None,
    poll_seconds: float | None = None,
    heartbeat_seconds: float | None = None,
    max_seconds: float | None = None,
) -> Response:
    """SSE の応答。終わった対象を続きから求められたら 204。

    ブラウザの `EventSource` は 204 で張り直しをやめる。対象が無い・読めないときの
    404 / 403 は、呼ぶ前に製品が判定する（`fetch` が最初に None を返したら 204）。
    """

    cursor = chat_progress_cursor(since=since, last_event_id=last_event_id)
    first = await fetch(cursor)
    if first is None or (first.terminal and cursor >= first.last_seq):
        return Response(status_code=204, headers=CHAT_PROGRESS_SSE_HEADERS)
    return StreamingResponse(
        chat_progress_sse_stream(
            fetch,
            since=cursor,
            first=first,
            poll_seconds=poll_seconds,
            heartbeat_seconds=heartbeat_seconds,
            max_seconds=max_seconds,
        ),
        media_type="text/event-stream",
        headers=CHAT_PROGRESS_SSE_HEADERS,
    )


def chat_progress_contract() -> dict[str, Any]:
    """契約の JSON（`platform/contracts/chat-progress/chat-progress-events.json` の正本）。"""

    return {
        "name": "chat-progress",
        "schema_version": CHAT_PROGRESS_SCHEMA_VERSION,
        "statuses": list(STATUS_RANK),
        "terminal_statuses": ["done", "failed", "cancelled"],
        "sse": {
            "event": CHAT_PROGRESS_SSE_EVENT,
            "heartbeat_event": CHAT_PROGRESS_SSE_HEARTBEAT_EVENT,
            "id": "seq",
            "resume": ["Last-Event-ID", "since"],
            "finished_status": 204,
        },
        "polling": {"query": "since"},
        "absent_fields": "omitted",
        "event": CHAT_PROGRESS_EVENT_ADAPTER.json_schema(),
        "page": ChatProgressPage.model_json_schema(),
    }


__all__ = [
    "CHAT_PROGRESS_EVENTS_ADAPTER",
    "CHAT_PROGRESS_EVENT_ADAPTER",
    "CHAT_PROGRESS_SCHEMA_VERSION",
    "CHAT_PROGRESS_SSE_EVENT",
    "CHAT_PROGRESS_SSE_HEADERS",
    "CHAT_PROGRESS_SSE_HEARTBEAT_EVENT",
    "CHAT_PROGRESS_SSE_HEARTBEAT_SECONDS",
    "CHAT_PROGRESS_SSE_MAX_SECONDS",
    "CHAT_PROGRESS_SSE_POLL_SECONDS",
    "UNCHANGED",
    "STATUS_RANK",
    "ChatProgressEvent",
    "ChatProgressFold",
    "ChatProgressPage",
    "ChatProgressSseTiming",
    "Unchanged",
    "ChatProgressRecorder",
    "ChatProgressSink",
    "ChatProgressSpanDisplay",
    "ChatProgressSpanUpdate",
    "ChatProgressDetailCode",
    "ChatProgressDetailField",
    "ChatProgressStepDetails",
    "ChatProgressStepEvent",
    "ChatProgressStepState",
    "ChatProgressStepStatus",
    "ChatProgressTerminalEvent",
    "ChatProgressTerminalStatus",
    "apply_chat_progress_event",
    "chat_progress_contract",
    "chat_progress_cursor",
    "chat_progress_details",
    "chat_progress_page",
    "chat_progress_sse_response",
    "chat_progress_sse_stream",
    "chat_progress_sse_timing",
    "configure_chat_progress_sse",
    "redact_chat_progress_text",
    "restore_chat_progress_sse",
    "dump_chat_progress_events",
    "fold_chat_progress_events",
    "format_chat_progress_sse",
    "format_sse",
    "parse_chat_progress_events",
]
