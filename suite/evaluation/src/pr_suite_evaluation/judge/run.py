"""保存した観測の回答を LLM で判定し、結果のファイルに判定を足す（#3418）。

Target（製品）は呼び直さない。判定するのは採点に成功したケースの最後の回答だけ（失敗したケースは
決定的な採点と同じく分母に入れない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pr_suite_evaluation.dataset import LoadedDataset, canonical_sha256
from pr_suite_evaluation.experiment import ExperimentRecord, ScorerConfig
from pr_suite_evaluation.judge.client import JudgeClient
from pr_suite_evaluation.judge.judging import (
    JudgeRun,
    JudgeSettings,
    case_input,
    judge_item,
    prompt_sha256,
    summarize_judgements,
)
from pr_suite_evaluation.judge.rubric import Rubric


def judge_record(
    record: ExperimentRecord,
    dataset: LoadedDataset,
    *,
    scorer: ScorerConfig,
    rubric: Rubric,
    client: JudgeClient,
    judge_provenance: Mapping[str, Any],
    settings: JudgeSettings,
    calibration: Mapping[str, Any] | None = None,
) -> ExperimentRecord:
    by_id = {case.id: case for case in dataset.dataset.cases}
    judgements = []
    for observation in record.observations:
        case = by_id.get(observation.case_id)
        if case is None:
            raise ValueError(f"評価セットに無いケースの観測です: {observation.case_id}")
        view = scorer.view(observation)
        if view.failure is not None or not view.turns:
            continue
        judgements.append(judge_item(case_input(case, view), rubric, client, settings))
    summary = summarize_judgements(
        judgements,
        rubric,
        thresholds=dataset.dataset.thresholds,
        calibration=calibration,
        model=str(judge_provenance.get("model") or ""),
    )
    calibration_provenance = None
    if calibration:
        calibration_provenance = {
            "sha256": canonical_sha256(dict(calibration)),
            "rubric_sha256": ((calibration.get("provenance") or {}).get("rubric") or {}).get(
                "sha256"
            ),
            "passed": calibration.get("passed"),
        }
    run = JudgeRun(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provenance={
            "rubric": rubric.provenance(),
            "prompt_sha256": prompt_sha256(rubric),
            "judge": dict(judge_provenance),
            "settings": settings.provenance(),
            "dataset": dataset.provenance(),
            "calibration": calibration_provenance,
        },
        judgements=judgements,
        summary=summary,
    )
    return record.model_copy(update={"judge": run})
