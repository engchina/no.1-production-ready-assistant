"""製品横断の評価の CLI（`suite-evaluation`。#3240・#3417）。

    export SUITE_EVALUATION_API_KEY=prak_...   # 使える製品に rag と agent を含む API キー
    # A: RAG（ナレッジベースを指定）・C: RAG（検索・回答プロファイルを指定）・D: 業務 Agent
    uv run suite-evaluation run <評価セット> --document-map <対応表> --target rag-mcp \\
        --mcp-url http://127.0.0.1:8000/api/mcp --knowledge-base-id <ID> --output a.json
    uv run suite-evaluation run <評価セット> --document-map <対応表> --target rag-mcp \\
        --mcp-url http://127.0.0.1:8000/api/mcp --search-answer-profile-id <ID> --output c.json
    uv run suite-evaluation run <評価セット> --document-map <対応表> --target agent-mcp \\
        --mcp-url http://127.0.0.1:8020/api/mcp --agent-id <公開済みの業務 Agent> --output d.json
    uv run suite-evaluation score d.json --dataset <評価セット> --document-map <対応表> \\
        --output d.rescored.json
    uv run suite-evaluation compare A=a.json C=c.json D=d.json

- `run`: 評価セットを Target（RAG の公開の MCP の `rag_search`、業務 Agent の公開の MCP）に流し、
  観測を保存して採点する。どの Target も同じ Evaluator・Report で採点する。
- `score`: 保存した観測を、モデル・製品を呼ばずに採点し直す（規則・期待を直したとき）。
- `compare`: suite の結果を比べる表を出す（同じ評価セットの結果どうしだけ）。
- `judge`: 保存した観測の回答を LLM で判定し、判定を結果に足す（製品は呼ばない。判定のモデルは呼ぶ。
  接続とモデルは `SUITE_EVALUATION_JUDGE_*`。`--calibration` の校正に通った観点だけ合否を出す）。
- `calibrate`: 人手の判定（ラベル）と LLM の判定の一致（κ・混同行列・誤合格 / 誤不合格）を集計し、
  校正の合否を出す（`--judgements` に前の校正の結果を渡すと、モデルを呼ばずに集計だけやり直す。
  `--repeats` で同じ項目を複数回判定し、判定の分散を記録する）。
- `label-template`: 保存した評価の結果の回答から、人手の判定の記入用のブック（.xlsx）と項目の
  ファイル（`*.items.json`）を書く（`--perturb` で故意に壊した回答の合成の対照を足す）。
- `label-import`: 記入者のブック（2 つ以上）の一致（κ）と食い違いを出し、裁定用のブックを書くか、
  裁定（か全員の一致）から `calibrate` が読む人手の判定（LabelSet の JSON）を書く。

終了コード: 0 成功 / 2 入力・接続の失敗（ケースの失敗は結果に記録し、終了コードにしない）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from pr_evaluation_scoring.perturbation import parse_kinds
from pydantic import ValidationError

from pr_suite_evaluation.dataset import (
    DatasetError,
    LoadedDataset,
    condition_labels,
    load_dataset,
    load_document_map,
)
from pr_suite_evaluation.experiment import (
    ExperimentRecord,
    ScorerConfig,
    Target,
    rescore,
    run_experiment,
)
from pr_suite_evaluation.judge.calibration import (
    CalibrationReport,
    CalibrationThresholds,
    calibrate,
    judge_labels,
    load_labels,
)
from pr_suite_evaluation.judge.client import (
    EnterpriseAiJudgeClient,
    JudgeConfigError,
    JudgeModelConfig,
)
from pr_suite_evaluation.judge.judging import Judgement, JudgeSettings
from pr_suite_evaluation.judge.labeling import (
    DEFAULT_HOLDOUT_RATIO,
    DEFAULT_PERTURB_MAX,
    DEFAULT_SPLIT_SALT,
    Annotation,
    LabelingItems,
    build_label_set,
    build_labeling_items,
    inter_annotator,
    items_path_for,
    load_items,
    read_annotation,
    write_adjudication,
    write_json,
    write_template,
)
from pr_suite_evaluation.judge.rubric import load_rubric
from pr_suite_evaluation.judge.run import judge_record
from pr_suite_evaluation.judge.workbook import WorkbookError
from pr_suite_evaluation.mcp_client import McpClient, TargetError
from pr_suite_evaluation.report import summarize_result, summary_markdown
from pr_suite_evaluation.targets import agent_mcp, rag_mcp

TARGETS = {"agent-mcp": agent_mcp.TARGET_KIND, "rag-mcp": rag_mcp.TARGET_KIND}
DEFAULT_MCP_URLS = {
    agent_mcp.TARGET_KIND: "http://localhost:8020/api/mcp",
    rag_mcp.TARGET_KIND: "http://localhost:8000/api/mcp",
}
# API キーを読む環境変数（キーを引数に書かない。シェルの履歴・プロセスの一覧に残さない）。
DEFAULT_API_KEY_ENV = "SUITE_EVALUATION_API_KEY"


class CliError(RuntimeError):
    """利用者へ返す失敗（exit code 2）。"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suite-evaluation", description="製品横断の評価（Dataset / Target / Report）"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="評価セットを製品の公開の MCP に流して採点する")
    run.add_argument("dataset", type=Path, help="評価セット（JSON）")
    run.add_argument(
        "--document-map",
        type=Path,
        help=(
            "評価セットの file:<ファイル名> を配備先の文書の ID にする対応表"
            '（{"documents": {...}}）'
        ),
    )
    run.add_argument("--target", choices=sorted(TARGETS), default="agent-mcp", help="評価の対象")
    run.add_argument("--mcp-url", help="対象の MCP（POST /api/mcp）。既定は対象ごとの localhost")
    run.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"MCP を呼ぶ API キーを読む環境変数（既定 {DEFAULT_API_KEY_ENV}）",
    )
    run.add_argument("--case", action="append", default=[], help="流すケースの id（複数可）")
    run.add_argument("--split", choices=["dev", "holdout"], help="流す区分")
    run.add_argument("--output", type=Path, required=True, help="結果（JSON）の出力先")
    agent = run.add_argument_group("agent-mcp（業務 Agent）")
    agent.add_argument("--agent-id", help="使う業務 Agent（公開済み。agent_list の id）")
    agent.add_argument("--guides", type=Path, help="既知の条件の名前に使う業務ガイド（JSON）")
    agent.add_argument("--run-timeout", type=float, default=agent_mcp.DEFAULT_RUN_TIMEOUT_SECONDS)
    agent.add_argument(
        "--infer-missing-outcome",
        action="store_true",
        help="outcome の無い完了の Run を回答の文から推定する（旧形式の互換。既定は評価不能）",
    )
    rag = run.add_argument_group("rag-mcp（RAG の検索・回答）")
    rag.add_argument(
        "--knowledge-base-id",
        action="append",
        default=[],
        help="検索するナレッジベース（A。複数可）",
    )
    rag.add_argument("--search-answer-profile-id", help="検索・回答プロファイル（C）")
    rag.add_argument("--top-k", type=int, help="検索する件数（省略時はプロファイル・既定）")
    rag.add_argument(
        "--evidence-limit",
        type=int,
        default=rag_mcp.DEFAULT_EVIDENCE_LIMIT,
        help=f"返す根拠の上限（既定 {rag_mcp.DEFAULT_EVIDENCE_LIMIT}。契約の上限）",
    )
    rag.add_argument(
        "--request-timeout",
        type=float,
        default=rag_mcp.DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="rag_search の 1 回の待ちの上限（秒）",
    )

    score = commands.add_parser("score", help="保存した観測を採点し直す（製品を呼ばない）")
    score.add_argument("record", type=Path, help="run の結果（JSON）")
    score.add_argument("--dataset", type=Path, required=True, help="採点に使う評価セット")
    score.add_argument("--document-map", type=Path, help="文書の対応表")
    score.add_argument("--case", action="append", default=[], help="採点するケースの id")
    score.add_argument("--infer-missing-outcome", action="store_true")
    score.add_argument("--output", type=Path, required=True, help="採点し直した結果の出力先")

    judge = commands.add_parser("judge", help="保存した観測の回答を LLM で判定する")
    judge.add_argument("record", type=Path, help="run の結果（JSON）")
    judge.add_argument("--dataset", type=Path, required=True, help="評価セット（標準回答を読む）")
    judge.add_argument("--document-map", type=Path, help="文書の対応表")
    judge.add_argument("--rubric", type=Path, help="判定の基準（省略時は既定の rubric）")
    judge.add_argument(
        "--calibration", type=Path, help="calibrate の結果（合否に使える観点を決める）"
    )
    judge.add_argument("--max-attempts", type=int, default=JudgeSettings().max_attempts)
    judge.add_argument("--max-evidence-chars", type=int, default=JudgeSettings().max_evidence_chars)
    judge.add_argument("--output", type=Path, required=True, help="判定を足した結果の出力先")

    calibration = commands.add_parser("calibrate", help="LLM の判定を人手の判定で校正する")
    calibration.add_argument("labels", type=Path, help="人手の判定（JSON）")
    calibration.add_argument("--rubric", type=Path, help="判定の基準（省略時は既定の rubric）")
    calibration.add_argument(
        "--judgements", type=Path, help="前の calibrate の結果（モデルを呼ばずに集計だけやり直す）"
    )
    defaults = CalibrationThresholds()
    calibration.add_argument("--min-items", type=int, default=defaults.min_items)
    calibration.add_argument("--min-kappa", type=float, default=defaults.min_kappa)
    calibration.add_argument(
        "--max-false-pass-rate", type=float, default=defaults.max_false_pass_rate
    )
    calibration.add_argument("--min-coverage", type=float, default=defaults.min_coverage)
    calibration.add_argument("--max-attempts", type=int, default=JudgeSettings().max_attempts)
    calibration.add_argument(
        "--max-evidence-chars", type=int, default=JudgeSettings().max_evidence_chars
    )
    calibration.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="同じ項目を判定する回数（2 以上で判定の分散を記録する。合否には最初の回を使う）",
    )
    calibration.add_argument("--output", type=Path, required=True, help="校正の結果の出力先")

    template = commands.add_parser(
        "label-template", help="人手の判定の記入用のブック（.xlsx）と項目のファイルを書く"
    )
    template.add_argument(
        "records", nargs="+", help="run / score / judge の結果（[<名前>=]<結果の JSON>。複数可）"
    )
    template.add_argument("--dataset", type=Path, required=True, help="評価セット（標準回答）")
    template.add_argument("--document-map", type=Path, help="文書の対応表")
    template.add_argument("--rubric", type=Path, help="判定の基準（省略時は既定の rubric）")
    template.add_argument(
        "--holdout-ratio",
        type=float,
        default=DEFAULT_HOLDOUT_RATIO,
        help=f"holdout にするケースの割合（既定 {DEFAULT_HOLDOUT_RATIO}）",
    )
    template.add_argument(
        "--split-salt", default=DEFAULT_SPLIT_SALT, help="区分を決める hash の salt"
    )
    template.add_argument(
        "--perturb",
        help="故意に壊した回答を足す（all か swap_values,drop_condition,"
        "add_unsupported_claim,reverse_steps のカンマ区切り）",
    )
    template.add_argument(
        "--perturb-max",
        type=int,
        default=DEFAULT_PERTURB_MAX,
        help=f"壊し方ごとの合成の項目の上限（既定 {DEFAULT_PERTURB_MAX}）",
    )
    template.add_argument("--output", type=Path, required=True, help="記入用のブック（.xlsx）")
    template.add_argument(
        "--items-output", type=Path, help="項目のファイル（既定 <ブック>.items.json）"
    )

    importer = commands.add_parser(
        "label-import", help="記入したブックから人手の判定（LabelSet の JSON）を作る"
    )
    importer.add_argument("--items", type=Path, required=True, help="項目のファイル")
    importer.add_argument(
        "--annotations",
        nargs="+",
        required=True,
        help="記入者のブック（[<記入者の ID>=]<ブック>。2 つ以上）",
    )
    importer.add_argument("--rubric", type=Path, help="判定の基準（省略時は既定の rubric）")
    importer.add_argument("--adjudicated", help="裁定したブック（[<裁定者の ID>=]<ブック>）")
    importer.add_argument(
        "--adjudication-output",
        type=Path,
        help="裁定用のブック（食い違いの行を空けたもの）の出力先",
    )
    importer.add_argument("--report", type=Path, help="記入者の間の一致と食い違い（JSON）の出力先")
    importer.add_argument("--output", type=Path, help="人手の判定（LabelSet の JSON）の出力先")

    compare = commands.add_parser("compare", help="suite の結果を比べる表を出す")
    compare.add_argument("results", nargs="+", help="<名前>=<結果の JSON>（例: A=a.json）")
    compare.add_argument("--json", action="store_true", help="表ではなく JSON で出す")
    return parser


def _api_key(name: str) -> str:
    key = os.environ.get(name, "").strip()
    if not key:
        raise CliError(
            f"環境変数 {name} に MCP の API キー（使える製品に対象の製品を含むキー）を"
            "設定してください。"
        )
    return key


def _dataset(path: Path, document_map: Path | None) -> LoadedDataset:
    documents = load_document_map(document_map) if document_map else None
    return load_dataset(path, documents=documents)


def _write(path: Path, record: ExperimentRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)


def _selected(loaded: LoadedDataset, case_ids: Sequence[str], split: str | None) -> LoadedDataset:
    if not case_ids and split is None:
        return loaded
    dataset = loaded.dataset.select(case_ids, split)
    return LoadedDataset(dataset=dataset, name=loaded.name, sha256=loaded.sha256)


def _agent_target(
    args: argparse.Namespace, client: httpx.Client, url: str, api_key: str
) -> tuple[Target, dict[str, Any], ScorerConfig]:
    if not args.agent_id:
        raise CliError("--target agent-mcp には --agent-id を渡してください。")
    labels = (
        condition_labels(json.loads(args.guides.read_text(encoding="utf-8"))) if args.guides else {}
    )
    config = agent_mcp.AgentTargetConfig(
        mcp_url=url, agent_id=args.agent_id, run_timeout_seconds=args.run_timeout
    )
    mcp = McpClient(
        client, url, api_key=api_key, label="Agent", error_class=agent_mcp.AgentEvaluationError
    )
    target = agent_mcp.AgentMcpTarget(agent_mcp.AgentMcp(mcp), config, labels=labels)
    scorer = ScorerConfig(
        target_kind=agent_mcp.TARGET_KIND,
        infer_missing_outcome=args.infer_missing_outcome,
        run_timeout_seconds=args.run_timeout,
    )
    return target, config.provenance(), scorer


def _rag_target(
    args: argparse.Namespace, client: httpx.Client, url: str, api_key: str
) -> tuple[Target, dict[str, Any], ScorerConfig]:
    try:
        config = rag_mcp.RagTargetConfig(
            mcp_url=url,
            knowledge_base_ids=tuple(args.knowledge_base_id),
            search_answer_profile_id=args.search_answer_profile_id,
            top_k=args.top_k,
            evidence_limit=args.evidence_limit,
        )
    except ValueError as error:
        raise CliError(str(error)) from error
    mcp = McpClient(
        client, url, api_key=api_key, label="RAG", error_class=rag_mcp.RagEvaluationError
    )
    return (
        rag_mcp.RagMcpTarget(mcp, config),
        config.provenance(),
        ScorerConfig(target_kind=rag_mcp.TARGET_KIND),
    )


def _run_command(args: argparse.Namespace) -> int:
    loaded = _selected(_dataset(args.dataset, args.document_map), args.case, args.split)
    kind = TARGETS[args.target]
    url = args.mcp_url or DEFAULT_MCP_URLS[kind]
    api_key = _api_key(args.api_key_env)
    # Agent の Run の待ちは MCP の wait_seconds（最大 300 秒）、rag_search は回答まで同期で
    # 返すので、HTTP の timeout はそれより長くする。
    timeout = (
        agent_mcp.MAX_WAIT_SECONDS + agent_mcp.REQUEST_TIMEOUT_SECONDS
        if kind == agent_mcp.TARGET_KIND
        else args.request_timeout
    )
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        build = _agent_target if kind == agent_mcp.TARGET_KIND else _rag_target
        target, target_config, scorer = build(args, client, url, api_key)
        record = run_experiment(
            loaded,
            target,
            target_config=target_config,
            scorer=scorer,
            log=lambda line: print(line, file=sys.stderr),
        )
    _write(args.output, record)
    return 0


def _load_record(path: Path) -> ExperimentRecord:
    try:
        return ExperimentRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise CliError(
            f"suite の結果のファイルの形ではありません（{error.error_count()} 件）: {path.name}"
        ) from error


def _score_command(args: argparse.Namespace) -> int:
    record = _load_record(args.record)
    loaded = _dataset(args.dataset, args.document_map)
    if args.case:
        record = record.model_copy(
            update={
                "observations": [item for item in record.observations if item.case_id in args.case]
            }
        )
    target = record.provenance.get("target") or {}
    target = target if isinstance(target, Mapping) else {}
    timeout = target.get("run_timeout_seconds")
    try:
        scorer = ScorerConfig(
            target_kind=str(target.get("kind") or agent_mcp.TARGET_KIND),
            infer_missing_outcome=args.infer_missing_outcome,
            run_timeout_seconds=float(timeout)
            if isinstance(timeout, int | float)
            else agent_mcp.DEFAULT_RUN_TIMEOUT_SECONDS,
        )
        rescored = rescore(record, loaded, scorer)
    except ValueError as error:
        raise CliError(str(error)) from error
    _write(args.output, rescored)
    return 0


def _judge_settings(args: argparse.Namespace) -> JudgeSettings:
    try:
        return JudgeSettings(
            max_attempts=args.max_attempts, max_evidence_chars=args.max_evidence_chars
        )
    except ValueError as error:
        raise CliError(str(error)) from error


def _judge_command(args: argparse.Namespace) -> int:
    record = _load_record(args.record)
    loaded = _dataset(args.dataset, args.document_map)
    rubric = load_rubric(args.rubric)
    settings = _judge_settings(args)
    calibration = (
        json.loads(args.calibration.read_text(encoding="utf-8")) if args.calibration else None
    )
    config = JudgeModelConfig.from_env()
    target = record.provenance.get("target") or {}
    target = target if isinstance(target, Mapping) else {}
    scorer = ScorerConfig(target_kind=str(target.get("kind") or agent_mcp.TARGET_KIND))
    with httpx.Client(trust_env=False) as client:
        judged = judge_record(
            record,
            loaded,
            scorer=scorer,
            rubric=rubric,
            client=EnterpriseAiJudgeClient(client, config),
            judge_provenance=config.provenance(),
            settings=settings,
            calibration=calibration,
        )
    _write(args.output, judged)
    return 0


def _calibrate_command(args: argparse.Namespace) -> int:
    labels = load_labels(args.labels)
    rubric = load_rubric(args.rubric)
    settings = _judge_settings(args)
    thresholds = CalibrationThresholds(
        min_items=args.min_items,
        min_kappa=args.min_kappa,
        max_false_pass_rate=args.max_false_pass_rate,
        min_coverage=args.min_coverage,
    )
    if args.repeats < 1:
        raise CliError("--repeats は 1 以上にしてください。")
    if args.judgements:
        previous = CalibrationReport.model_validate_json(
            args.judgements.read_text(encoding="utf-8")
        )
        if (previous.provenance.get("rubric") or {}).get("sha256") != rubric.sha256:
            raise CliError("前の校正の結果の rubric が違います（判定をやり直してください）。")
        judgements = previous.judgements
        repeats = previous.repeat_judgements
        judge_provenance = previous.provenance.get("judge") or {}
    else:
        config = JudgeModelConfig.from_env()
        runs: list[list[Judgement]] = []
        with httpx.Client(trust_env=False) as client:
            judge = EnterpriseAiJudgeClient(client, config)
            for _ in range(args.repeats):
                runs.append(judge_labels(labels, rubric, judge, settings))
        judgements, repeats = runs[0], runs[1:]
        judge_provenance = config.provenance()
    try:
        report = calibrate(
            labels,
            rubric,
            judgements=judgements,
            judge_provenance=judge_provenance,
            settings=settings,
            thresholds=thresholds,
            repeat_judgements=repeats,
        )
    except ValueError as error:
        raise CliError(str(error)) from error
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for aspect_id, item in report.aspects.items():
        holdout = item.splits.get("holdout")
        synthetic = item.splits.get("synthetic")
        variance = report.variance.get(aspect_id)
        extra = ""
        if synthetic is not None:
            extra += f" synthetic_false_pass={synthetic.false_pass_rate}"
        if variance is not None:
            extra += f" unstable_rate={variance.unstable_rate}"
        print(
            f"{aspect_id}: passed={item.passed} kappa={holdout.kappa if holdout else None} "
            f"items={holdout.total if holdout else 0}{extra} {'; '.join(item.failures)}",
            file=sys.stderr,
        )
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


def _named(value: str) -> tuple[str | None, Path]:
    """`[<名前>=]<path>` を名前と path にする（名前を省くと None）。"""
    name, separator, path = value.partition("=")
    if separator and name and path and "/" not in name:
        return name, Path(path)
    return None, Path(value)


def _label_template_command(args: argparse.Namespace) -> int:
    loaded = _dataset(args.dataset, args.document_map)
    rubric = load_rubric(args.rubric)
    records = []
    for value in args.records:
        name, path = _named(value)
        records.append((name or path.stem, _load_record(path)))
    try:
        items = build_labeling_items(
            records,
            loaded,
            rubric,
            holdout_ratio=args.holdout_ratio,
            salt=args.split_salt,
            perturb=parse_kinds(args.perturb),
            perturb_max=args.perturb_max,
        )
    except ValueError as error:
        raise CliError(str(error)) from error
    items_path = args.items_output or items_path_for(args.output)
    write_json(items_path, items)
    write_template(args.output, items, rubric)
    human = items.human_items
    rows = sum(len(item.aspects) for item in human)
    holdout = sum(1 for item in human if item.split == "holdout")
    print(
        f"items={len(human)} (holdout={holdout}) rows={rows} "
        f"synthetic={len(items.synthetic_items)} skipped={items.skipped}",
        file=sys.stderr,
    )
    print(f"wrote {args.output}", file=sys.stderr)
    print(f"wrote {items_path}", file=sys.stderr)
    return 0


def _read_annotation(value: str, items: LabelingItems, kind: str = "annotation") -> Annotation:
    name, path = _named(value)
    return read_annotation(path, items, annotator=name, kind=kind)


def _label_import_command(args: argparse.Namespace) -> int:
    items = load_items(args.items)
    rubric = load_rubric(args.rubric)
    try:
        items.check_rubric(rubric)
    except ValueError as error:
        raise CliError(str(error)) from error
    annotations = [_read_annotation(value, items) for value in args.annotations]
    try:
        report = inter_annotator(items, rubric, annotations)
    except ValueError as error:
        raise CliError(str(error)) from error
    for aspect_id, item in report.aspects.items():
        kappas = ", ".join(
            f"{'/'.join(pair.annotators)} κ={pair.kappa} n={pair.compared}" for pair in item.pairs
        )
        print(
            f"{aspect_id}: rows={item.rows} disagreements={item.disagreements} {kappas}",
            file=sys.stderr,
        )
    if args.report:
        write_json(args.report, report)
        print(f"wrote {args.report}", file=sys.stderr)
    if args.adjudication_output:
        open_rows = write_adjudication(args.adjudication_output, items, rubric, annotations)
        print(f"wrote {args.adjudication_output} (要裁定 {open_rows} 行)", file=sys.stderr)
    if args.output:
        adjudicated = (
            _read_annotation(args.adjudicated, items, kind="adjudication")
            if args.adjudicated
            else None
        )
        try:
            result = build_label_set(
                items, rubric, annotations, adjudicated=adjudicated, report=report
            )
        except ValueError as error:
            raise CliError(str(error)) from error
        write_json(args.output, result.labels)
        origins = [item.origin for item in result.labels.items]
        print(
            f"labels: human={origins.count('human')} synthetic={origins.count('synthetic')} "
            f"dropped(cannot_judge)={len(result.dropped_items)}",
            file=sys.stderr,
        )
        print(f"wrote {args.output}", file=sys.stderr)
    elif not args.adjudication_output and not args.report:
        raise CliError("--output・--adjudication-output・--report のどれかを渡してください。")
    return 0


def _compare_command(args: argparse.Namespace) -> int:
    summaries: dict[str, dict[str, Any]] = {}
    datasets: dict[str, str] = {}
    for item in args.results:
        label, separator, path = item.partition("=")
        if not separator or not label or not path:
            raise CliError(f"<名前>=<結果の JSON> の形で渡してください: {item}")
        record = _load_record(Path(path))
        dataset = record.provenance.get("dataset") or {}
        datasets[label] = str(dataset.get("sha256") if isinstance(dataset, Mapping) else "")
        target = record.provenance.get("target") or {}
        summaries[label] = {
            **summarize_result(record.report.model_dump(mode="json")),
            "target_kind": target.get("kind") if isinstance(target, Mapping) else None,
            "metrics_version": record.report.metrics_version,
        }
        if record.judge is not None:
            summaries[label]["judge"] = {
                aspect_id: {
                    "pass_rate": item.pass_rate,
                    "calibrated": item.calibrated,
                    "unevaluable": item.unevaluable,
                }
                for aspect_id, item in record.judge.summary.aspects.items()
            }
    if len(set(datasets.values())) > 1:
        # 違う評価セット（ケース・期待・文書の対応）の結果は、同じ採点器でも比べられない。
        raise CliError(
            "評価セットの内容（provenance.dataset.sha256）が違う結果は比べられません: "
            + ", ".join(f"{label}={value[:12]}" for label, value in datasets.items())
        )
    if args.json:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    else:
        print(summary_markdown(summaries), end="")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run_command(args)
        if args.command == "score":
            return _score_command(args)
        if args.command == "judge":
            return _judge_command(args)
        if args.command == "calibrate":
            return _calibrate_command(args)
        if args.command == "label-template":
            return _label_template_command(args)
        if args.command == "label-import":
            return _label_import_command(args)
        return _compare_command(args)
    except (
        CliError,
        DatasetError,
        WorkbookError,
        JudgeConfigError,
        ValidationError,
        TargetError,
        OSError,
        json.JSONDecodeError,
        httpx.HTTPError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
