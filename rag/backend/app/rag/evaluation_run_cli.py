"""評価を backend の process の中で流し、外部の AI の応答を記録・再生する CLI（#59）。

評価の job と同じ処理（``_execute_run``。検索・回答プロファイルの解決・回答・判定・集計・実行条件の
manifest）を、この process の中で直接呼ぶ。Oracle は設定どおりに接続する（検索の結果は固定しない）。

- ``run --record FILE``: 実の API を呼び、生成・判定・質問の拡張・埋め込み・再ランクの応答を
  FILE に残す（実データの応答を含む。明示した保存先だけに置き、公開の場所・GitHub に置かない）。
- ``run --replay FILE``: 実の API を呼ばずに FILE の応答で同じ評価を流す（API キー・外部の
  ネットワーク不要）。記録の条件（manifest）・入力・呼び出しの数が違えば終了コード 2 で失敗し、
  実の API には接続しない。再生は制御の回帰の確認で、モデルの能力・実サービスの速さの証明ではない。
- ``compare BASE TARGET``: 同じケースの対応のある差・区間・改善 / 悪化 / 同値の件数（#60）。
- ``diff A B``: 2 つの評価の結果（この CLI・``evaluation_cli`` の出力）の実行条件の差を出す。

記録・再生は呼び出しの順を揃えるため、ケースを 1 件ずつ流す（``RAG_EVALUATION_CONCURRENCY`` = 1）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from typing import Any

from pr_backend_core.logging import configure_cli_logging
from pydantic import ValidationError
from rag_engine.resources.external_calls import (
    ExternalCallMismatch,
    ExternalCallSession,
    use_external_calls,
)

from app.config import Settings, fixed_today, get_settings
from app.rag.evaluation_cli import (
    EvaluationGateError,
    _gate_failed,
    _gate_summary,
    _load_evaluation_request,
    _write_json,
)
from app.rag.evaluation_jobs import evaluation_answer_runs, evaluation_job_time_limit_seconds
from app.rag.evaluation_manifest import comparison_differences, flatten_manifest
from app.rag.evaluation_paired import (
    DEFAULT_MIN_CASES,
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    compare_paired,
)
from app.rag.request_context import with_system_access
from app.schemas.evaluation import (
    EvaluationMetrics,
    EvaluationPairedMetric,
    EvaluationRunRequest,
)

# 再生で、空の接続の設定に入れる値（実の API には接続しない。設定の検査だけを通す）。
REPLAY_PLACEHOLDER_KEY = "replay-without-key"  # nosec B105 - 秘密ではない固定の印
REPLAY_PLACEHOLDER_ENDPOINT = "https://replay.invalid/v1"
REPLAY_FAILED_EXIT_CODE = 2
_SHOWN_VALUE_CHARS = 80


@with_system_access
def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""
    configure_cli_logging("rag")
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "diff":
            return _diff(args.left, args.right)
        if args.command == "compare":
            return _compare(args)
        return _run(args)
    except EvaluationGateError as exc:
        print(f"評価エラー: {exc}", file=sys.stderr)
        return exc.exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag-evaluation-run",
        description="評価を process の中で流し、外部の AI の応答を記録・再生します（#59）。",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="評価を流す（--record / --replay で記録・再生）。")
    run.add_argument("golden_set", type=Path, help="EvaluationRunRequest 形式の JSON。")
    run.add_argument("--split", choices=["dev", "holdout"], help="この区分のケースだけを流す。")
    run.add_argument("--output", type=Path, help="結果の JSON の保存先（未指定は stdout）。")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--record", type=Path, help="外部の AI の応答を残す先（実データを含む）。")
    mode.add_argument("--replay", type=Path, help="この記録の応答で、実の API を呼ばずに流す。")
    compare = commands.add_parser(
        "compare", help="2 つの評価の結果を、同じケースの対応のある差と区間で比べる（#60）。"
    )
    compare.add_argument("base", type=Path, help="比べる元の結果（変更の前）。")
    compare.add_argument("target", type=Path, help="比べる先の結果（変更の後）。")
    compare.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap の乱数の seed。")
    compare.add_argument(
        "--resamples", type=int, default=DEFAULT_RESAMPLES, help="bootstrap の反復数。"
    )
    compare.add_argument(
        "--min-cases",
        type=int,
        default=DEFAULT_MIN_CASES,
        help="これより対が少なければ小標本として判定しない。",
    )
    compare.add_argument("--output", type=Path, help="比較の JSON の保存先。")
    diff = commands.add_parser("diff", help="2 つの評価の結果の実行条件の差を出す。")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    return parser


def _run(args: argparse.Namespace) -> int:
    from app.api.routes.evaluation import _execute_run

    request = EvaluationRunRequest.model_validate(
        _load_evaluation_request(args.golden_set, split=args.split)
    )
    settings = get_settings()
    session: ExternalCallSession | None = None
    recorded_manifest: Mapping[str, Any] = {}
    recording_sha256: str | None = None
    if args.replay is not None:
        raw = _read_bytes(args.replay)
        recording_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            recording = json.loads(raw)
            session = ExternalCallSession.from_recording(recording)
        except ValueError as exc:
            raise EvaluationGateError(f"記録を読めません: {exc}") from exc
        recorded_manifest = recording.get("manifest") or {}
        fill_replay_connections(settings)
    elif args.record is not None:
        session = ExternalCallSession("record")
    if session is not None:
        # 呼び出しの順（埋め込み・再ランクの cache の当たり方も）を記録と再生で揃える。
        settings.rag_evaluation_concurrency = 1
    passes = 2 if request.compare_search_answer_profile_id else 1
    time_limit = evaluation_job_time_limit_seconds(
        settings, answer_runs=evaluation_answer_runs(request.cases) * passes
    )
    # 再生は記録の日を「今日」にする（基準日・業務ガイドの期間・LLM に渡す今日を記録と揃える）。
    recorded_as_of = recorded_manifest.get("as_of")
    today = fixed_today(date.fromisoformat(recorded_as_of)) if recorded_as_of else nullcontext()
    try:
        with use_external_calls(session), today:
            metrics, evaluation_run_id = asyncio.run(
                _execute_run(request, time_budget_seconds=time_limit)
            )
    except ExternalCallMismatch as exc:
        # ふつうはケースの失敗として結果に残る。評価の外（条件の読み取りなど）で起きたら止める。
        raise EvaluationGateError(str(exc), exit_code=REPLAY_FAILED_EXIT_CODE) from exc
    manifest = dict(metrics.run_manifest or {})
    external: dict[str, Any] = {"mode": session.mode if session is not None else "live"}
    problems: list[str] = []
    if session is not None and session.mode == "record":
        recording_sha256 = _write_recording(args.record, session.to_recording(manifest))
        external.update(recording_sha256=recording_sha256, calls=len(session.entries))
        print(
            f"外部の AI の応答を {len(session.entries)} 件記録しました: {args.record}",
            file=sys.stderr,
        )
    elif session is not None:
        differences = comparison_differences(recorded_manifest, manifest)
        unused = session.unused()
        problems = [
            *(f"条件が記録と違います: {path}" for path in differences),
            *session.mismatches,
            *(
                f"再生の不一致: 記録の呼び出しが使われませんでした（{entry['kind']} "
                f"{entry.get('label', '')}、入力の hash {str(entry['key'])[:12]}）"
                for entry in unused
            ),
        ]
        external.update(
            recording_sha256=recording_sha256,
            calls=len(session.entries) - len(unused),
            mismatches=len(session.mismatches),
            unused=len(unused),
            condition_differences=differences,
        )
    manifest["external_calls"] = external
    metrics = metrics.model_copy(update={"run_manifest": manifest})
    _write_json(
        {"data": metrics.model_dump(mode="json"), "evaluation_run_id": evaluation_run_id},
        args.output,
    )
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print("再生は記録と一致しませんでした（実の API には接続していません）。", file=sys.stderr)
        return REPLAY_FAILED_EXIT_CODE
    print(f"[{external['mode']}] " + _gate_summary(metrics, passed=not _gate_failed(metrics)))
    return 1 if _gate_failed(metrics) else 0


def _diff(left_path: Path, right_path: Path) -> int:
    """2 つの結果の実行条件の差を出す（差・実行中の変化が無ければ 0、あれば 1）。"""
    left = _result_manifest(left_path)
    right = _result_manifest(right_path)
    flat_left = flatten_manifest(left)
    flat_right = flatten_manifest(right)
    differences = comparison_differences(left, right)
    for path in differences:
        print(f"{path}: {_shown(flat_left.get(path))} -> {_shown(flat_right.get(path))}")
    unstable = [
        f"{label}: 実行中に条件が変わりました（{', '.join(manifest.get('changed_during_run') or [])}）"  # noqa: E501
        for label, manifest in (("left", left), ("right", right))
        if manifest.get("comparable") is False
    ]
    for line in unstable:
        print(line)
    modes = {str((manifest.get("external_calls") or {}).get("mode")) for manifest in (left, right)}
    if differences or unstable:
        print(f"比較できる条件ではありません（差 {len(differences)} 件）。", file=sys.stderr)
        return 1
    print(f"実行条件は同じです（外部の AI の応答: {' / '.join(sorted(modes))}）。", file=sys.stderr)
    return 0


def fill_replay_connections(settings: Settings) -> None:
    """再生では API キー・Endpoint が無くても動くよう、空の接続の設定に印の値を入れる。"""
    for slot in ("", "secondary_", "tertiary_"):
        if not getattr(settings, f"enterprise_ai_{slot}api_key"):
            setattr(settings, f"enterprise_ai_{slot}api_key", REPLAY_PLACEHOLDER_KEY)
        if not getattr(settings, f"enterprise_ai_{slot}endpoint"):
            setattr(settings, f"enterprise_ai_{slot}endpoint", REPLAY_PLACEHOLDER_ENDPOINT)


def _compare(args: argparse.Namespace) -> int:
    """同じケースの対応のある比較（比較不能・target の gate の不合格・確かな悪化があれば 1）。"""
    paired = compare_paired(
        _result_metrics(args.base),
        _result_metrics(args.target),
        seed=args.seed,
        resamples=args.resamples,
        min_cases=args.min_cases,
    )
    if args.output is not None:
        _write_json(paired.model_dump(mode="json"), args.output)
    print(
        f"対応のある比較: {paired.status}、対 {paired.paired_case_count} 件、seed {paired.seed}、"
        f"bootstrap {paired.resamples} 回、信頼度 {paired.confidence:g}、"
        f"最小の対 {paired.min_cases}"
    )
    for label, values in (
        ("比較不能の理由", paired.reasons),
        ("注意", paired.warnings),
        ("base だけのケース", paired.base_only_case_ids),
        ("target だけのケース", paired.target_only_case_ids),
        ("条件の差", paired.condition_differences),
    ):
        if values:
            print(f"{label}: {', '.join(values)}")
    if paired.metrics:
        print(_PAIRED_HEADER)
    for item in paired.metrics:
        print(_paired_row(item))
    print(
        "gate（既存の閾値・失敗したケース）: "
        f"base {'不合格' if paired.base_gate_failed else '合格'}、"
        f"target {'不合格' if paired.target_gate_failed else '合格'}"
    )
    regressed = [item.metric for item in paired.metrics if item.verdict == "regressed"]
    return 1 if paired.status != "compared" or paired.target_gate_failed or regressed else 0


_PAIRED_HEADER = (
    f"{'指標':<28}{'対':>4}{'欠測':>5}{'誤り':>5}{'改善':>5}{'悪化':>5}{'同値':>5}"
    f"{'base':>8}{'target':>8}{'差':>8}  {'区間':<17}{'p':>7}  判定"
)


def _paired_row(item: EvaluationPairedMetric) -> str:
    def number(value: float | None) -> str:
        return "-" if value is None else f"{value:.3f}"

    interval = (
        "-" if item.interval is None else f"[{item.interval[0]:+.3f}, {item.interval[1]:+.3f}]"
    )
    p_value = "-" if item.p_value is None else f"{item.p_value:.3f}"
    return (
        f"{item.metric:<28}{item.paired_cases:>4}{item.missing_cases:>5}{item.error_cases:>5}"
        f"{item.improved:>5}{item.regressed:>5}{item.unchanged:>5}{number(item.base_mean):>8}"
        f"{number(item.target_mean):>8}{number(item.mean_difference):>8}  {interval:<17}"
        f"{p_value:>7}  {item.verdict}"
    )


def _result_metrics(path: Path) -> EvaluationMetrics:
    """この CLI・``evaluation_cli`` の出力（``{"data": 結果}``）か、結果そのものを読む。"""
    try:
        payload = json.loads(_read_bytes(path))
    except json.JSONDecodeError as exc:
        raise EvaluationGateError(f"結果を JSON として読めません: {path}") from exc
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    try:
        return EvaluationMetrics.model_validate(data)
    except ValidationError as exc:
        raise EvaluationGateError(f"評価の結果の形式ではありません: {path}") from exc


def _result_manifest(path: Path) -> dict[str, Any]:
    metrics = _result_metrics(path)
    if metrics.run_manifest is None:
        raise EvaluationGateError(
            f"実行条件の記録がありません（#59 より前の結果）: {path}", exit_code=1
        )
    return metrics.run_manifest


def _write_recording(path: Path, recording: Mapping[str, Any]) -> str:
    """記録を所有者だけが読める権限（0600）で書き、内容の sha256 を返す。"""
    encoded = (json.dumps(recording, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
    path.chmod(0o600)
    return hashlib.sha256(encoded).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise EvaluationGateError(f"ファイルが見つかりません: {path}") from exc


def _shown(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _SHOWN_VALUE_CHARS else text[: _SHOWN_VALUE_CHARS - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
