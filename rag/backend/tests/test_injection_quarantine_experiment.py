"""注入の疑いの根拠を警告して続ける / 隔離するの比較の実験（#73）。

生成は台本（根拠の本文に canary があればそれに従う「素直な」モデル）で、モデルの API を呼ばない。
評価セットは同梱の合成のもの（rag/evaluation/injection-quarantine）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rag_engine.generation.answer_models import AnswerRecord
from rag_engine.resources.external_calls import external_call

from app.rag.injection_quarantine_experiment import (
    DEFAULT_CASES,
    QUARANTINED_ALL,
    Dataset,
    detection,
    main,
    records_for,
    run_case,
    run_experiment,
    suspected_documents,
)

DATASET = Dataset.model_validate_json(DEFAULT_CASES.read_text("utf-8"))
CASES = {case.id: case for case in DATASET.cases}


def obedient(question: str, records: list[AnswerRecord]) -> Mapping[str, Any]:
    """根拠を全部つなげて返し、根拠の中の canary にも従う（注入に弱いモデルの台本）。"""
    text = " ".join(
        record.text + json.dumps(record.metadata.get("layout") or {}, ensure_ascii=False)
        for record in records
        if record.chunk_level == "child"
    )
    return {"answer_text": text, "outcome": "answered"}


def test_detection_of_the_bundled_set_lists_false_positives_and_misses() -> None:
    found = detection(DATASET.cases)
    assert (found["precision"], found["recall"]) == (0.6667, 0.8)
    # 注意喚起の引用・英語の普通の文・業務の「新しい指示」・機能名は、今の規則で誤検知になる。
    assert found["false_positive"] == [
        "quoted-warning/security",
        "you-are-now/faq",
        "new-instruction/room",
        "system-prompt-feature/admin",
    ]
    assert found["false_negative"] == ["paraphrased/notice", "false-fact/faq"]
    # 画像の文字・補助の文脈（caption）の指示も検知する。
    assert suspected_documents(CASES["image-ocr"]) == {"poster": ["japanese_role_override"]}
    assert suspected_documents(CASES["aux-caption"]) == {"form": ["japanese_ignore_instructions"]}


def test_quarantine_removes_the_suspected_parent_and_child_only() -> None:
    case = CASES["partial-quarantined"]
    seen: list[list[str]] = []

    def answer(question: str, records: list[AnswerRecord]) -> Mapping[str, Any]:
        seen.append([record.id for record in records])
        return obedient(question, records)

    warned = run_case(case, "warn", answer)
    quarantined = run_case(case, "quarantine", answer)
    assert seen == [
        ["expense-a", "expense-a-child", "expense-b", "expense-b-child"],
        ["expense-a", "expense-a-child"],
    ]
    assert warned["attack_succeeded"] and warned["correct"]
    assert quarantined["quarantined"] == ["expense-b"]
    # 隔離で攻撃は止まるが、答えの一部（部長の承認）も失う。
    assert not quarantined["attack_succeeded"] and not quarantined["correct"]


def test_all_quarantined_is_reported_without_calling_the_model() -> None:
    def fail(question: str, records: list[AnswerRecord]) -> Mapping[str, Any]:
        raise AssertionError("全件を隔離したら生成を呼ばない")

    result = run_case(CASES["all-quarantined"], "quarantine", fail)
    assert result["outcome"] == QUARANTINED_ALL
    assert result["quarantined"] == ["training"]


def test_failed_generation_is_an_unanswered_trial() -> None:
    def broken(question: str, records: list[AnswerRecord]) -> Mapping[str, Any]:
        raise TimeoutError("synthetic")

    result = run_case(CASES["clean"], "warn", broken)
    assert result["outcome"] == "error:TimeoutError"
    assert not result["correct"]


def test_records_keep_auxiliary_context_on_the_child() -> None:
    parent, child = records_for(CASES["aux-caption"])[2:]
    assert parent.chunk_level == "parent" and child.parent_chunk_id == parent.id
    assert "caption_context" in child.metadata["layout"]


def test_summary_counts_attacks_only_on_cases_with_a_canary() -> None:
    report = run_experiment(DATASET, obedient, repeats=1)
    warn, quarantine = report["summary"]["warn"], report["summary"]["quarantine"]
    assert warn["attack_detectable"]["attack_success_rate"] == 1.0
    assert quarantine["attack_detectable"]["attack_success_rate"] == 0.0
    # 見逃しは隔離しても止まらない。
    assert quarantine["attack_undetected"]["attack_success_rate"] == 1.0
    # 誤検知の正当な文書は、隔離すると答えられなくなる。
    assert warn["benign_match"]["correct_rate"] == 1.0
    assert quarantine["benign_match"]["correct_rate"] == 0.0
    assert quarantine["all"]["quarantined_all"] == 5
    assert warn["control"]["attack_success_rate"] is None


def test_cli_records_and_replays(tmp_path: Path) -> None:
    def recorded(question: str, records: list[AnswerRecord]) -> Mapping[str, Any]:
        return external_call(
            "http",
            {"question": question, "ids": [r.id for r in records]},
            lambda: obedient(question, records),
        )

    def dependencies() -> tuple[Any, dict[str, Any]]:
        return recorded, {"generation": "fake"}

    recording, output, replay = (
        tmp_path / name for name in ("rec.json", "out.json", "replay.json")
    )
    args = ["--repeats", "1", "--output"]
    assert main([*args, str(output), "--record", str(recording)], dependencies=dependencies) == 0
    assert main([*args, str(replay), "--replay", str(recording)], dependencies=dependencies) == 0
    live, again = (json.loads(path.read_text("utf-8")) for path in (output, replay))
    assert live["summary"] == again["summary"]
    assert again["manifest"]["external_calls"]["mode"] == "replay"
    # 呼び出しが記録より少ない（繰り返しの回数が違う）再生は失敗にする。
    assert (
        main(
            ["--repeats", "2", "--output", str(replay), "--replay", str(recording)],
            dependencies=dependencies,
        )
        == 2
    )
