"""RAG search API の簡易 load / p95 gate CLI。

`python -m app.rag.search_load_cli scenario.json` として使う。
query / answer / context 原文は出力 artifact に残さず、case id と aggregate latency だけを残す。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
from pr_backend_core.internal_http import http_client_options
from pr_backend_core.logging import configure_cli_logging
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.clients.http_retry import HttpRetryConfig, async_request_with_retry
from app.schemas.search import SEARCH_TOP_K_MAX, SearchRequest
from app.search_defaults import search_top_k_default, search_top_k_schema

DEFAULT_SEARCH_LOAD_API_URL = "http://localhost:8100/api/search"
DEFAULT_SEARCH_LOAD_API_BASE_URL = "http://localhost:8100"
DEFAULT_TIMEOUT_SECONDS = 60.0
logger = logging.getLogger(__name__)


class SearchLoadCliError(RuntimeError):
    """load test CLI が利用者へ返す安全なエラー。"""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class SearchLoadRunError(RuntimeError):
    """単一 search request の失敗を低機密に表す。"""

    def __init__(
        self,
        error_type: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.status_code = status_code


class SearchLoadThresholds(BaseModel):
    """load gate の閾値。query 原文は持たない。"""

    client_p95_ms: float | None = Field(default=None, gt=0.0)
    server_p95_ms: float | None = Field(default=None, gt=0.0)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    stage_p95_ms: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_stage_thresholds(self) -> SearchLoadThresholds:
        invalid = [stage for stage, value in self.stage_p95_ms.items() if value <= 0.0]
        if invalid:
            raise ValueError("stage_p95_ms は正の値にしてください。")
        return self


class SearchLoadCase(BaseModel):
    """SearchRequest に case id を付けた load scenario case。"""

    id: str = Field(..., min_length=1, max_length=128)
    query: str = Field(..., min_length=1)
    top_k: int = Field(
        default_factory=search_top_k_default,
        ge=1,
        le=SEARCH_TOP_K_MAX,
        json_schema_extra=search_top_k_schema,
    )
    # 旧 standard の指定(rerank_top_n / mode / strategy)は #595 で削除した。scenario に
    # 残っていても読み捨てる(pydantic の既定 extra="ignore")。
    filters: dict[str, str] = Field(default_factory=dict)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=200)
    # 検索・回答プロファイル(#1226)。実際の検索・回答と同じ設定・参照 KB で測る。
    search_answer_profile_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_search_request(self) -> SearchLoadCase:
        self.to_search_request()
        return self

    def to_search_request(self) -> SearchRequest:
        payload: dict[str, Any] = {
            "query": self.query,
            "top_k": self.top_k,
            "filters": self.filters,
            "knowledge_base_ids": self.knowledge_base_ids,
        }
        if self.search_answer_profile_id:
            payload["search_answer_profile_id"] = self.search_answer_profile_id
        return SearchRequest.model_validate(payload)


class SearchLoadScenario(BaseModel):
    """load CLI 入力 JSON schema。"""

    cases: list[SearchLoadCase] = Field(..., min_length=1, max_length=1000)
    repeat: int = Field(default=1, ge=1, le=1000)
    concurrency: int = Field(default=1, ge=1, le=200)
    thresholds: SearchLoadThresholds = Field(default_factory=SearchLoadThresholds)


@dataclass(frozen=True)
class SearchLoadRun:
    """単一 request の低機密結果。"""

    case_id: str
    ok: bool
    client_latency_ms: float
    server_latency_ms: float | None = None
    stage_timings: Mapping[str, float] | None = None
    # 回答フローの工程ごとのモデル(LLM)の呼び出しの回数(#1226)。
    stage_llm_calls: Mapping[str, int] | None = None
    error_type: str | None = None
    status_code: int | None = None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""
    configure_cli_logging("rag")
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        scenario = _load_scenario(args.scenario)
        result = asyncio.run(
            _run_load(
                scenario,
                api_url=_resolve_api_url(args.api_url, args.api_base_url),
                timeout=args.timeout,
                headers=_request_headers(args.tenant_id, args.user_id),
            )
        )
        _write_json({"data": result}, args.output)
        if args.trend_output is not None:
            _write_json(_trend_payload(result), args.trend_output)
    except SearchLoadCliError as exc:
        print(f"検索 load gate エラー: {exc}", file=sys.stderr)
        return exc.exit_code

    if not result["passed"]:
        print(_load_summary(result, passed=False), file=sys.stderr)
        return 1
    print(_load_summary(result, passed=True), file=sys.stderr)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag-search-load",
        description="RAG search API に並列 request を投げ、p50/p95 と gate 終了コードを返します。",
    )
    parser.add_argument(
        "scenario",
        type=Path,
        help="SearchLoadScenario 形式の JSON ファイル。",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help=f"検索 API URL。未指定時は {DEFAULT_SEARCH_LOAD_API_URL}",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("RAG_SEARCH_LOAD_API_BASE_URL"),
        help="staging host の base URL。/api/search を付与します。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_env_float("RAG_SEARCH_LOAD_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
        help=f"1 request の timeout 秒。既定値: {DEFAULT_TIMEOUT_SECONDS}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="load 結果 JSON の保存先。未指定なら stdout に出力します。",
    )
    parser.add_argument(
        "--trend-output",
        type=Path,
        help="trend 用の非機密サマリ JSON 保存先。",
    )
    parser.add_argument(
        "--tenant-id",
        default=os.getenv("RAG_SEARCH_LOAD_TENANT_ID"),
        help="検索対象 tenant の X-Tenant-ID。値は CLI 出力へ表示しません。",
    )
    parser.add_argument(
        "--user-id",
        default=os.getenv("RAG_SEARCH_LOAD_USER_ID"),
        help="検索実行者の X-User-ID。値は CLI 出力へ表示しません。",
    )
    return parser


def _load_scenario(path: Path) -> SearchLoadScenario:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SearchLoadCliError(f"load scenario が見つかりません: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SearchLoadCliError(
            f"load scenario が JSON として読めません: line={exc.lineno}, column={exc.colno}"
        ) from exc
    try:
        return SearchLoadScenario.model_validate(raw)
    except ValidationError as exc:
        raise SearchLoadCliError(
            "load scenario の形式が不正です: " + _safe_validation_error_summary(exc)
        ) from exc


def _resolve_api_url(api_url: str | None, api_base_url: str | None) -> str:
    if api_url:
        return api_url
    if env_url := os.getenv("RAG_SEARCH_LOAD_API_URL"):
        return env_url
    if api_base_url:
        return f"{api_base_url.rstrip('/')}/api/search"
    return DEFAULT_SEARCH_LOAD_API_URL


async def _run_load(
    scenario: SearchLoadScenario,
    *,
    api_url: str,
    timeout: float,
    headers: Mapping[str, str],
) -> dict[str, Any]:
    if timeout <= 0:
        raise SearchLoadCliError("timeout は 0 より大きい値にしてください。")
    semaphore = asyncio.Semaphore(scenario.concurrency)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, **http_client_options(api_url)
    ) as client:
        tasks = [
            _run_one(
                client,
                semaphore,
                api_url=api_url,
                case=case,
                iteration=iteration,
                headers=headers,
            )
            for case in scenario.cases
            for iteration in range(scenario.repeat)
        ]
        runs = await asyncio.gather(*tasks)
    return _summarize_runs(scenario, runs)


async def _run_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    *,
    api_url: str,
    case: SearchLoadCase,
    iteration: int,
    headers: Mapping[str, str],
) -> SearchLoadRun:
    _ = iteration
    async with semaphore:
        payload = case.to_search_request().model_dump(mode="json")
        started_at = perf_counter()
        try:
            response_payload = await _post_search_request(
                client,
                api_url=api_url,
                payload=payload,
                headers=headers,
            )
            client_latency_ms = round((perf_counter() - started_at) * 1000, 3)
            data = _api_response_data(response_payload)
            return SearchLoadRun(
                case_id=case.id,
                ok=True,
                client_latency_ms=client_latency_ms,
                server_latency_ms=_optional_float(data.get("elapsed_ms")),
                stage_timings=_stage_timings(data),
                stage_llm_calls=_stage_llm_calls(data),
            )
        except SearchLoadRunError as exc:
            return SearchLoadRun(
                case_id=case.id,
                ok=False,
                client_latency_ms=round((perf_counter() - started_at) * 1000, 3),
                error_type=exc.error_type,
                status_code=exc.status_code,
            )


async def _post_search_request(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json", **headers}
    try:
        response = await async_request_with_retry(
            client,
            "POST",
            api_url,
            retry=HttpRetryConfig(),
            logger=logger,
            log_extra={"api_url": api_url},
            json=payload,
            headers=request_headers,
        )
    except httpx.InvalidURL as exc:
        raise SearchLoadRunError(type(exc).__name__) from exc
    except httpx.TimeoutException as exc:
        raise SearchLoadRunError(type(exc).__name__) from exc
    except httpx.RequestError as exc:
        raise SearchLoadRunError(type(exc).__name__) from exc
    if response.status_code >= 400:
        raise SearchLoadRunError("http_status", status_code=response.status_code)
    return _decode_json_response(response.content)


def _decode_json_response(raw_body: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SearchLoadRunError("invalid_json") from exc
    if not isinstance(decoded, dict):
        raise SearchLoadRunError("invalid_json_root")
    return decoded


def _api_response_data(response_payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = response_payload.get("data", response_payload)
    if not isinstance(data, Mapping):
        raise SearchLoadRunError("missing_data")
    return data


def _execution_steps(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    diagnostics = data.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return []
    answer = diagnostics.get("answer")
    if not isinstance(answer, Mapping):
        return []
    steps = answer.get("execution_steps")
    if not isinstance(steps, Sequence) or isinstance(steps, str):
        return []
    return [step for step in steps if isinstance(step, Mapping)]


def _stage_llm_calls(data: Mapping[str, Any]) -> dict[str, int]:
    """回答フローの工程ごとのモデル(LLM)の呼び出しの回数(#1226)。同じ名前の工程は合計する。

    ``execution_steps`` の ``llm_calls``。入れ子の工程(回答生成フローとその中の工程)は、
    それぞれの工程の回数をそのまま持つ(親の工程の回数は子の回数を含む)。
    """
    calls: dict[str, int] = {}
    for step in _execution_steps(data):
        name = str(step.get("name") or "").strip()
        value = step.get("llm_calls")
        if not name or isinstance(value, bool) or not isinstance(value, int):
            continue
        calls[name] = calls.get(name, 0) + value
    return calls


def _stage_timings(data: Mapping[str, Any]) -> dict[str, float]:
    """回答フローの工程ごとの時間(ms)。``diagnostics.answer.execution_steps`` から読む。

    同じ名前の工程が複数回あれば合計する(補正検索の 2 回目など)。
    """
    diagnostics = data.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return {}
    answer = diagnostics.get("answer")
    if not isinstance(answer, Mapping):
        return {}
    steps = answer.get("execution_steps")
    if not isinstance(steps, Sequence) or isinstance(steps, str):
        return {}
    timings: dict[str, float] = {}
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        name = str(step.get("name") or "").strip()
        seconds = _optional_float(step.get("elapsed_seconds"))
        if not name or seconds is None:
            continue
        timings[name] = round(timings.get(name, 0.0) + seconds * 1000, 3)
    return timings


def _summarize_runs(
    scenario: SearchLoadScenario,
    runs: Sequence[SearchLoadRun],
) -> dict[str, Any]:
    success_runs = [run for run in runs if run.ok]
    failed_runs = [run for run in runs if not run.ok]
    client_values = [run.client_latency_ms for run in success_runs]
    server_values = [
        run.server_latency_ms for run in success_runs if run.server_latency_ms is not None
    ]
    stage_values = _stage_values(success_runs)
    error_count = len(failed_runs)
    request_count = len(runs)
    error_rate = round(error_count / request_count, 6) if request_count else 0.0
    summary: dict[str, Any] = {
        "kind": "search_load",
        "passed": True,
        "request_count": request_count,
        "success_count": len(success_runs),
        "error_count": error_count,
        "error_rate": error_rate,
        "repeat": scenario.repeat,
        "concurrency": scenario.concurrency,
        "case_count": len(scenario.cases),
        "client_latency_ms": _latency_summary(client_values),
        "server_latency_ms": _latency_summary(server_values),
        "stage_latency_ms": {
            stage: _latency_summary(values) for stage, values in sorted(stage_values.items())
        },
        # 工程ごとのモデルの呼び出しの回数の平均と最大(#1226。遅い工程と回数を合わせて見る)。
        "stage_llm_calls": _llm_call_summary(success_runs),
        "cases": _case_summaries(runs),
        "error_type_counts": _error_type_counts(failed_runs),
        "threshold_failures": [],
    }
    failures = _threshold_failures(summary, scenario.thresholds)
    summary["threshold_failures"] = failures
    summary["passed"] = not failures
    return summary


def _llm_call_summary(runs: Sequence[SearchLoadRun]) -> dict[str, dict[str, float]]:
    values: dict[str, list[int]] = defaultdict(list)
    for run in runs:
        for stage, count in (run.stage_llm_calls or {}).items():
            values[stage].append(count)
    return {
        stage: {"mean": round(sum(counts) / len(counts), 3), "max": float(max(counts))}
        for stage, counts in sorted(values.items())
    }


def _stage_values(runs: Sequence[SearchLoadRun]) -> dict[str, list[float]]:
    values: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        for stage, elapsed_ms in (run.stage_timings or {}).items():
            values[stage].append(elapsed_ms)
    return values


def _case_summaries(runs: Sequence[SearchLoadRun]) -> list[dict[str, Any]]:
    grouped: dict[str, list[SearchLoadRun]] = defaultdict(list)
    for run in runs:
        grouped[run.case_id].append(run)
    summaries: list[dict[str, Any]] = []
    for case_id, case_runs in sorted(grouped.items()):
        success_runs = [run for run in case_runs if run.ok]
        summaries.append(
            {
                "id": case_id,
                "request_count": len(case_runs),
                "success_count": len(success_runs),
                "error_count": len(case_runs) - len(success_runs),
                "client_latency_ms": _latency_summary(
                    [run.client_latency_ms for run in success_runs]
                ),
                "server_latency_ms": _latency_summary(
                    [
                        run.server_latency_ms
                        for run in success_runs
                        if run.server_latency_ms is not None
                    ]
                ),
            }
        )
    return summaries


def _error_type_counts(runs: Sequence[SearchLoadRun]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for run in runs:
        key = run.error_type or "unknown"
        if run.status_code is not None:
            key = f"{key}:{run.status_code}"
        counts[key] += 1
    return dict(sorted(counts.items()))


def _latency_summary(values: Sequence[float | None]) -> dict[str, float | int | None]:
    filtered = sorted(float(value) for value in values if value is not None)
    if not filtered:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {
        "count": len(filtered),
        "p50": _percentile(filtered, 50),
        "p95": _percentile(filtered, 95),
        "max": round(max(filtered), 3),
    }


def _percentile(sorted_values: Sequence[float], percentile: int) -> float:
    if not sorted_values:
        return 0.0
    rank = max(
        0,
        min(
            len(sorted_values) - 1,
            math.ceil((percentile / 100) * len(sorted_values)) - 1,
        ),
    )
    return round(sorted_values[rank], 3)


def _threshold_failures(
    summary: Mapping[str, Any],
    thresholds: SearchLoadThresholds,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    error_rate = _optional_float(summary.get("error_rate")) or 0.0
    if error_rate > thresholds.error_rate:
        failures.append(
            {
                "metric": "error_rate",
                "actual": error_rate,
                "threshold": thresholds.error_rate,
            }
        )
    _append_latency_failure(
        failures,
        metric="client_p95_ms",
        actual=_summary_p95(summary.get("client_latency_ms")),
        threshold=thresholds.client_p95_ms,
    )
    _append_latency_failure(
        failures,
        metric="server_p95_ms",
        actual=_summary_p95(summary.get("server_latency_ms")),
        threshold=thresholds.server_p95_ms,
    )
    stage_summary = summary.get("stage_latency_ms")
    if isinstance(stage_summary, Mapping):
        for stage, threshold in sorted(thresholds.stage_p95_ms.items()):
            actual = _summary_p95(stage_summary.get(stage))
            _append_latency_failure(
                failures,
                metric=f"stage_p95_ms.{stage}",
                actual=actual,
                threshold=threshold,
            )
    return failures


def _append_latency_failure(
    failures: list[dict[str, Any]],
    *,
    metric: str,
    actual: float | None,
    threshold: float | None,
) -> None:
    if threshold is None or actual is None:
        return
    if actual > threshold:
        failures.append({"metric": metric, "actual": actual, "threshold": threshold})


def _summary_p95(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    return _optional_float(value.get("p95"))


def _trend_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    return {
        "data": {
            **dict(result),
            "result_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        }
    }


def _write_json(payload: Mapping[str, Any], output_path: Path | None) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output_path is None:
        print(serialized, end="")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized, encoding="utf-8")


def _load_summary(result: Mapping[str, Any], *, passed: bool) -> str:
    status = "passed" if passed else "failed"
    client_p95 = _summary_p95(result.get("client_latency_ms"))
    server_p95 = _summary_p95(result.get("server_latency_ms"))
    return (
        f"検索 load gate {status}: requests={result['request_count']}, "
        f"errors={result['error_count']}, error_rate={result['error_rate']}, "
        f"client_p95_ms={client_p95}, server_p95_ms={server_p95}, "
        f"threshold_failures={len(result['threshold_failures'])}"
    )


def _request_headers(tenant_id: str | None, user_id: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id
    if user_id:
        headers["X-User-ID"] = user_id
    return headers


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return round(float(value), 3)
    return None


def _safe_validation_error_summary(error: ValidationError) -> str:
    """入力値を出さず、path と error type だけを利用者へ返す。"""
    summaries: list[str] = []
    for item in error.errors():
        loc = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        error_type = item.get("type", "validation_error")
        summaries.append(f"{loc}:{error_type}")
    return ", ".join(summaries)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


if __name__ == "__main__":
    raise SystemExit(main())
