"""回答生成の実行記録。工程の開始・詳細・終了を呼出単位で記録し、利用者向けに整形する。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from time import monotonic
from typing import Any

from rag_engine.dependencies import llm_call_count
from rag_engine.generation.answer_models import QUESTION_DISPLAY_METADATA_SEPARATOR, extract_original_question
from rag_engine.resources.deadline import check_cancelled
from rag_engine.resources.llm_usage import LlmUsage, usage_delta, usage_snapshot
from rag_engine.resources.llm_usage import llm_usage as _llm_usage
from rag_engine.resources.tracing import observe_call


@dataclass
class _ExecutionStep:
    """一工程の詳細と終了状態。失敗や早期停止を成功と区別して記録する。

    詳細は 2 か所に残す: 実行順の記録の文（``_execution_lines``。``question_display``）と、この工程だけの構造化した
    詳細（``execution_steps[].details``。処理の経過の段を開いて見る。#2294 / #2341）。構造化した詳細は共有の詳細の
    契約（画面の ``RunStepDetails``）と同じ形で、``目的`` / ``結果`` / ``影響`` と ``field()`` の値は見出しと値
    （``fields``）、``add()`` と結果の続きの行は行（``lines``）にする。
    """

    name: str
    status: str = ""
    hidden: bool = False
    # この工程だけの詳細の行（入れ子の工程の行は含めない）。
    details: list[str] = field(default_factory=list)
    # この工程だけの見出しと値（(見出し, 値)。入れ子の工程の値は含めない）。
    fields: list[tuple[str, str]] = field(default_factory=list)
    # 上限で切った（行・値の一部だけを残した）。
    truncated: bool = False

    def add(self, *details: str) -> None:
        """詳細をその場で追加し、入れ子工程との前後関係を保持する。"""
        lines = _execution_lines.get()
        for detail in details:
            if lines is not None:
                lines.extend(f"  {line}" for line in detail.splitlines())
            self._keep(detail)

    def field(self, label: str, value: object) -> None:
        """見出しと値を 1 つ足す（値が空なら何もしない）。実行順の記録の文には「見出し: 値」の行で残す。"""
        text = " ".join(str(value if value is not None else "").split())
        if not label or not text:
            return
        lines = _execution_lines.get()
        if lines is not None:
            lines.append(f"  {label}: {text}")
        self._keep_field(label, text)

    def _keep(self, detail: str) -> None:
        """工程の詳細の行を上限まで残す（長い行は切る。上限を超えた分は印の行 1 つにする）。

        字下げの無い「目的: 」「結果: 」「影響: 」の行（記録の文を組み立てて ``add()`` に渡す関数の書き方）は、
        ``result()`` / ``impact()`` と同じ見出しと値にする。
        """
        for line in detail.splitlines():
            text = line.strip()
            if not text:
                continue
            label = next((name for name in _FIELD_LINE_LABELS if line.startswith(f"{name}: ")), None)
            if label is not None:
                self._keep_field(label, text.removeprefix(f"{label}: ").strip())
                continue
            if len(self.details) >= STEP_DETAILS_MAX_LINES:
                self.truncated = True
                if self.details[-1] != STEP_DETAILS_OMITTED:
                    self.details.append(STEP_DETAILS_OMITTED)
                return
            self.details.append(self._clip(text, STEP_DETAILS_LINE_MAX))

    def _keep_field(self, label: str, value: str) -> None:
        if len(self.fields) >= STEP_DETAILS_MAX_FIELDS:
            self.truncated = True
            return
        self.fields.append((self._clip(label, STEP_DETAILS_LABEL_MAX), self._clip(value, STEP_DETAILS_VALUE_MAX)))

    def _clip(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        self.truncated = True
        return f"{text[:limit]}…"

    def result(self, *details: str) -> None:
        """この工程で分かったこと。最初の行に「結果:」を付ける（構造化した詳細は見出し「結果」と続きの行）。"""
        values = [line for detail in details for line in detail.splitlines() if line.strip()]
        if not values:
            return
        lines = _execution_lines.get()
        if lines is not None:
            lines.append(f"  結果: {values[0]}")
            lines.extend(f"        {value}" for value in values[1:])
        self._keep_field("結果", values[0].strip())
        for value in values[1:]:
            self._keep(value)

    def impact(self, text: str) -> None:
        """この結果が後続の処理をどう変えるか。変えない場合は書かない。"""
        if not text:
            return
        lines = _execution_lines.get()
        if lines is not None:
            lines.append(f"  影響: {text}")
        self._keep_field("影響", text.strip())

    def hide(self) -> None:
        """利用者に伝える内容がない工程は実行順の記録の文から省く（異常終了した場合は省かない）。

        処理の経過の段は省いた工程も出るので、構造化した詳細（``execution_steps[].details``）は残す（#2341）。
        """
        self.hidden = True

    def structured_details(self) -> dict[str, Any]:
        """構造化した詳細（``{"fields": [{"label", "value"}], "lines": [...], "truncated": True}``）。無ければ空。"""
        result: dict[str, Any] = {}
        if self.fields:
            result["fields"] = [{"label": label, "value": value} for label, value in self.fields]
        if self.details:
            result["lines"] = list(self.details)
        if result and self.truncated:
            result["truncated"] = True
        return result


# 工程ごとに残す詳細の上限（構造化した工程の記録 `execution_steps` の ``details``。#2294 / #2341）。backend の
# 上限（`pr_backend_core.chat_progress` の DETAILS_*）以下にする。
STEP_DETAILS_MAX_LINES = 20
STEP_DETAILS_LINE_MAX = 300
STEP_DETAILS_MAX_FIELDS = 16
STEP_DETAILS_LABEL_MAX = 40
STEP_DETAILS_VALUE_MAX = 600
STEP_DETAILS_OMITTED = "（以降の行は省略しました）"
# 見出しと値にする記録の行の見出し（目的・結果・影響）。
_FIELD_LINE_LABELS = ("目的", "結果", "影響")

# Gradio の並行リクエスト間で実行記録が混ざらないよう、呼び出し単位で分離する。
_structured_steps: ContextVar[list[dict[str, Any]] | None] = ContextVar("rag_engine_steps", default=None)

_execution_lines: ContextVar[list[str] | None] = ContextVar("answer_execution_lines", default=None)

# 工程の入れ子の深さ（0 が最上位）。画面の処理の経過で字下げする（#2096）。
_step_depth: ContextVar[int] = ContextVar("answer_step_depth", default=0)

# 工程の開始・終了を呼び出し側へ知らせる(進捗の表示。#593)。引数は (工程名, "started" /
# "success" / "error", 経過秒)。通知の失敗は回答を止めない。
# 4 つ目は終わった工程の記録（``details``: 構造化した詳細、``llm_calls``: LLM の回数、``status``: 結果の説明）。
# 工程が終わったとき（成功・失敗）に渡し、処理の経過の段を実行中から開けるようにする（#2373）。開始は空。
StepListener = Callable[[str, str, float, Mapping[str, Any]], None]
_step_listener: ContextVar[StepListener | None] = ContextVar("answer_step_listener", default=None)


@contextmanager
def bind_step_listener(listener: StepListener | None) -> Iterator[None]:
    """この呼び出しの中の工程の開始・終了を ``listener`` へ知らせる。"""
    token = _step_listener.set(listener)
    try:
        yield
    finally:
        _step_listener.reset(token)


# 工程ごとに差分を記録する累積の計測値（DB の SQL の回数・行数など。#1490）。呼び出し側が渡す関数は
# その時点の累積値（数値）を返し、工程の記録には終了時と開始時の差を足す。失敗しても回答を止めない。
StepMetrics = Callable[[], Mapping[str, int | float]]
_step_metrics: ContextVar[StepMetrics | None] = ContextVar("answer_step_metrics", default=None)


@contextmanager
def bind_step_metrics(provider: StepMetrics | None) -> Iterator[None]:
    """この呼び出しの中の工程の記録に、``provider`` の累積値の差分を足す。"""
    token = _step_metrics.set(provider)
    try:
        yield
    finally:
        _step_metrics.reset(token)


def _step_metrics_snapshot() -> dict[str, int | float]:
    provider = _step_metrics.get()
    if provider is None:
        return {}
    try:
        values = provider()
    except Exception:  # noqa: BLE001 - 計測は補助。回答の処理を止めない。
        return {}
    return {key: value for key, value in values.items() if isinstance(value, int | float) and not isinstance(value, bool)}


def _step_metrics_delta(before: Mapping[str, int | float]) -> dict[str, int | float]:
    after = _step_metrics_snapshot()
    delta: dict[str, int | float] = {}
    for key, value in after.items():
        diff = value - before.get(key, 0)
        delta[key] = round(diff, 3) if isinstance(diff, float) else diff
    return delta


def _notify_step(name: str, outcome: str, elapsed: float, record: Mapping[str, Any] | None = None) -> None:
    listener = _step_listener.get()
    if listener is None:
        return
    # 進捗の通知は補助。回答の処理を止めない。
    with suppress(Exception):
        listener(name, outcome, elapsed, dict(record or {}))


class AnswerExecutionError(RuntimeError):
    """失敗時も完了済み工程と中断箇所を質問欄へ返すための例外。"""

    def __init__(self, message: str, question_display: str):
        """元のエラー本文と、保存前の実行記録を保持する。"""
        super().__init__(message)
        self.question_display = question_display


@contextmanager
def _execution_step(name: str, purpose: str = ""):
    """工程境界で記録し、例外時には終了状態を「エラーで中断」にする。

    purpose は利用者向けの「何のための処理か」。工程名だけでは分からない場合に付ける。
    回答が取り消された（呼び出し元の切断・停止。#2691）後は、次の工程を始めない（`DeadlineExceeded`）。
    """
    check_cancelled()
    steps = _structured_steps.get()
    index = len(steps) if steps is not None else 0
    start = monotonic()
    llm_before = llm_call_count.get()
    usage_before = usage_snapshot()
    metrics_before = _step_metrics_snapshot()
    depth = _step_depth.get()
    depth_token = _step_depth.set(depth + 1)
    if steps is not None:
        steps.append({"name": name, "status": "running", "depth": depth})
    lines = _execution_lines.get()
    step = _ExecutionStep(name)
    first_line = len(lines) if lines is not None else 0
    if lines is not None:
        lines.extend(["", f"{name} 開始"])
        if purpose:
            lines.append(f"  目的: {purpose}")
    if purpose:
        step._keep_field("目的", purpose)
    _notify_step(name, "started", 0.0)
    failed = False
    llm_calls = 0
    usage: dict[str, Any] = {}
    sends: dict[str, int] = {}

    def measure() -> None:
        nonlocal llm_calls, usage, sends
        llm_calls = llm_call_count.get() - llm_before
        usage = usage_delta(usage_before)
        # LLM の API への実際の送信の回数（未完了・停滞・暴走の送り直しを含む）。``llm_calls`` は呼び出しの回数で、
        # 送り直しを数えないので、送信が多いときだけ両方を示す（#2966）。
        llm_sends = int(usage.get("calls", 0) or 0)
        sends = {"llm_sends": llm_sends} if llm_sends > llm_calls else {}

    try:
        with observe_call("stage", name) as observation:
            # 工程の span の表示用のデータ（種類と工程名。#3226）。終わったときに LLM の回数・結果の説明・詳細を足す。
            observation.display("answer_step", {"name": name})
            try:
                yield step
            except Exception:
                failed = True
                step.status = "エラーで中断"
                raise
            finally:
                measure()
                observation.display(
                    "answer_step",
                    {"llm_calls": llm_calls, **sends, "status": step.status or "complete"},
                    step.structured_details() or None,
                )
    finally:
        _step_depth.reset(depth_token)
        elapsed = monotonic() - start
        # 工程の span を開けなかったときも計測値をそろえる（span の中で測った値と同じ）。
        measure()
        # 終わった工程の詳細・LLM の回数・結果の説明を、終わったときの通知で渡す（回答の完了を待たない。#2373）。
        _notify_step(
            name,
            "error" if failed else "success",
            elapsed,
            {"details": step.structured_details(), "llm_calls": llm_calls, **sends, "status": step.status or "complete"},
        )
        if steps is not None:
            steps[index] = {
                "name": name,
                "depth": depth,
                "status": step.status or "complete",
                "elapsed_seconds": elapsed,
                "llm_calls": llm_calls,
                **sends,
                **_step_metrics_delta(metrics_before),
            }
            # LLM の token の数（入力・出力・推論）と呼び出しの秒の和。遅い工程の理由を確かめる（#1516）。
            if usage:
                steps[index]["llm_usage"] = usage
            # この工程だけの詳細（処理の経過の段を開いて見る。#2294）。段は省いた工程も出るので、省いた工程も残す（#2341）。
            if details := step.structured_details():
                steps[index]["details"] = details
        if lines is not None:
            if step.hidden and not step.status:
                del lines[first_line:]
            else:
                suffix = f"（{step.status}）" if step.status else ""
                # 所要時間と LLM 呼出回数は終了行の末尾に付け、整形時に見出しへ移す（_END_METRICS）。
                lines.append(f"{name} 終了{suffix} @{elapsed:.1f}s/{llm_calls}")


def format_question_display(original_question: str, execution_lines: Sequence[str]) -> str:
    """原質問に工程境界・対応番号付きの記録を付加する。記録自体の順序は変更しない。"""
    if not execution_lines:
        return original_question
    return "\n".join(
        [
            original_question,
            "",
            QUESTION_DISPLAY_METADATA_SEPARATOR,
            *_format_execution_boundaries(execution_lines),
        ]
    ).strip()


_END_METRICS = re.compile(r" @([\d.]+)s/(\d+)$")


def _format_execution_boundaries(execution_lines: Sequence[str]) -> list[str]:
    """工程を番号付きの節（1. / 6.1 / 6.1.1）と字下げで表示する。字下げ済みの詳細文は工程として解釈しない。

    見出しは工程ごとに1回だけ出し、LLM を呼んだ工程・時間のかかった工程には見出しに
    「［LLM n 回 / x.x 秒］」を添える。正常に終わった工程に終了行は付けず、中止・失敗など
    利用者が知るべき状態だけを「状態:」として示す。詳細も状態もない工程は表示しない。
    末尾に LLM 呼出回数と所要時間の合計を出す（計測のある工程が 1 つもなければ出さない）。
    """
    root: dict[str, Any] = {"name": "", "items": [], "status": "", "elapsed": 0.0, "llm": 0}
    stack = [root]
    for line in execution_lines:
        if not line.strip():
            continue
        if not line[0].isspace() and line.endswith(" 開始"):
            node = {"name": line.removesuffix(" 開始"), "items": [], "status": "", "elapsed": None, "llm": 0}
            stack[-1]["items"].append(node)
            stack.append(node)
        elif len(stack) > 1 and line.startswith(stack[-1]["name"] + " 終了"):
            node = stack.pop()
            metrics = _END_METRICS.search(line)
            if metrics:
                node["elapsed"], node["llm"] = float(metrics.group(1)), int(metrics.group(2))
                line = line[: metrics.start()]
            status = re.search(r"終了（(.+)）$", line)
            node["status"] = status.group(1) if status else ""
        else:
            stack[-1]["items"].append((line[2:] if line.startswith("  ") else line).rstrip())
    # 中断時は終了行がない。どこで止まったかが分かるよう、開いたままの工程に状態を付ける。
    for node in stack[1:]:
        node["status"] = node["status"] or "中断"

    rendered: list[str] = []

    def metrics_label(node: dict[str, Any]) -> str:
        parts = []
        if node["llm"]:
            parts.append(f"LLM {node['llm']} 回")
        if node["elapsed"] is not None and node["elapsed"] >= 0.1:
            parts.append(f"{node['elapsed']:.1f} 秒")
        return f" ［{' / '.join(parts)}］" if parts else ""

    def render(node: dict[str, Any], number: str, depth: int) -> None:
        indent = "   " * depth
        heading = f"{number}." if depth == 0 else number  # 最上位は「1.」、入れ子は「1.2」「1.2.3」
        rendered.extend(["", f"{indent}{heading} {node['name']}{metrics_label(node)}"])
        body = indent + "   "
        children = 0
        for item in node["items"]:
            if isinstance(item, str):
                rendered.append(body + item)
            elif item["items"] or item["status"]:
                children += 1
                render(item, f"{number}.{children}", depth + 1)
        if node["status"]:
            rendered.append(f"{body}状態: {node['status']}")

    count = 0
    for item in root["items"]:
        if isinstance(item, str):
            rendered.append(item)
        elif item["items"] or item["status"]:
            count += 1
            render(item, str(count), 0)
    measured = [item for item in root["items"] if isinstance(item, dict) and item["elapsed"] is not None]
    if measured:
        rendered.extend(["", f"合計: LLM {sum(item['llm'] for item in measured)} 回 / {sum(item['elapsed'] for item in measured):.1f} 秒"])
    return rendered


def _record_answer_execution(function):
    """結果に実行順の記録を付け、例外でも記録を保持して呼び出し元へ伝える。"""

    @wraps(function)
    def wrapped(question, *args, **kwargs):
        lines: list[str] = []
        steps: list[dict[str, Any]] = []
        step_token = _structured_steps.set(steps)
        token = _execution_lines.set(lines)
        llm_token = llm_call_count.set(0)
        usage_token = _llm_usage.set(LlmUsage())
        try:
            result = function(question, *args, **kwargs)
            return replace(result, question_display=format_question_display(result.original_question, lines), execution_steps=tuple(steps))
        except Exception as exc:
            if not lines:
                raise
            display = format_question_display(extract_original_question(question), lines)
            error = AnswerExecutionError(str(exc), display)
            error.execution_steps = tuple(steps)
            raise error from exc
        finally:
            _llm_usage.reset(usage_token)
            llm_call_count.reset(llm_token)
            _execution_lines.reset(token)
            _structured_steps.reset(step_token)

    return wrapped
