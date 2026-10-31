"""品質評価の判定の校正（記入の集合・blind の記入・一致・裁定・校正の job・LabelSet。#3578）。

記入の項目の元（品質評価の job と回答の記録）は合成の台本、評価器は決定的なスタブ。判定は
テストの中だけの合成の記入（人の判定ではない）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pr_evaluation_scoring.labeling import (
    DEFAULT_SPLIT_SALT,
    LABEL_SET_FIELDS,
    LABEL_SET_ITEM_FIELDS,
    assign_splits,
)

from app.clients.evaluation_calibration_store import (
    CalibrationRecord,
    MemoryEvaluationCalibrationStore,
    oracle_evaluation_calibration_schema_sql,
)
from app.main import app
from app.rag import evaluation_calibration
from app.rag.answer_judge_calibration import LabeledAnswer
from app.rag.evaluation_calibration import (
    JudgeIdentity,
    LabelSetDocument,
    LabelSetItem,
    judge_status,
    set_calibration_store,
    set_source_reader,
)
from app.rag.evaluation_jobs import (
    EvaluationJobService,
    MemoryEvaluationJobStore,
    get_evaluation_job_service,
    set_evaluation_job_service,
)
from app.security.permissions import (
    EVALUATION_CALIBRATION_MANAGE,
    MENU_EVALUATION,
    PERMISSION_BY_CODE,
    permission_for_route,
)
from tests.support import TEST_REQUEST_HEADERS

BASE = "/api/evaluation/calibration"
CATEGORIES = ("rule", "procedure")
JOB_ID = "job-a" + "0" * 27


def _number(index: int) -> str:
    return str(index).zfill(2)


def _case(index: int) -> dict[str, Any]:
    return dict(
        case_id="c" + _number(index),
        trace_id="trace-" + _number(index),
        status="success",
        category=CATEGORIES[index % 2],
    )


class FakeReader:
    """合成の品質評価の job 1 件（10 ケース）と、その回答の記録。"""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = dict()
        for index in range(10):
            standard = (
                None if index == 9 else "合成の標準回答。期限は " + str(index + 3) + " 日です。"
            )
            self.records["trace-" + _number(index)] = dict(
                question="合成の質問 " + str(index),
                answer="合成の回答。期限は "
                + str(index + 3)
                + " 日、上限は "
                + str(index + 10)
                + " 件です。",
                evaluation_input_json=dict(
                    evidence_items=[dict(id="chunk-" + str(index), text="合成の根拠")]
                ),
                evaluation_json=dict(standard_answer=standard),
            )
        # 回答の記録が消えたケース（項目にしない）。
        self.records.pop("trace-08")

    async def list_finished_jobs(self) -> list[dict[str, Any]]:
        return [
            dict(
                job_id=JOB_ID,
                created_at=datetime(2026, 10, 1, tzinfo=UTC),
                finished_at=datetime(2026, 10, 1, 1, tzinfo=UTC),
                total_cases=10,
                case_count=10,
                compared=0,
            )
        ]

    async def job_result(self, job_id: str) -> Mapping[str, Any] | None:
        if job_id != JOB_ID:
            return None
        return dict(case_results=[_case(index) for index in range(10)])

    async def answer_record(self, trace_id: str) -> Mapping[str, Any] | None:
        return self.records.get(trace_id)


@pytest.fixture
def store() -> Iterator[MemoryEvaluationCalibrationStore]:
    memory = MemoryEvaluationCalibrationStore()
    set_calibration_store(memory)
    set_source_reader(FakeReader())
    set_evaluation_job_service(
        EvaluationJobService(MemoryEvaluationJobStore(), heartbeat_seconds=0.01)
    )
    yield memory
    set_calibration_store(None)
    set_source_reader(None)
    set_evaluation_job_service(None)


def _headers(user: str) -> dict[str, str]:
    headers = dict(TEST_REQUEST_HEADERS)
    headers["X-User-ID"] = user
    return headers


@pytest.fixture
async def clients(
    store: MemoryEvaluationCalibrationStore,
) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers=_headers("annotator-a")
        ) as first,
        httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers=_headers("annotator-b")
        ) as second,
    ):
        yield first, second


def _set(label_set_id: str, suffix: str = "") -> str:
    return BASE + "/label-sets/" + label_set_id + suffix


async def _create(client: httpx.AsyncClient, *, synthetic: bool = False) -> str:
    response = await client.post(
        BASE + "/label-sets",
        json=dict(name="校正の集合 A", job_ids=[JOB_ID], include_synthetic=synthetic),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["label_set_id"])


async def _rows(client: httpx.AsyncClient, label_set_id: str) -> list[dict[str, Any]]:
    response = await client.get(_set(label_set_id, "/rows"), params=dict(limit=100))
    assert response.status_code == 200, response.text
    return list(response.json()["data"]["items"])


async def _label(
    client: httpx.AsyncClient, label_set_id: str, item_id: str, aspect: str, label: str | None
) -> httpx.Response:
    return await client.put(
        _set(label_set_id, "/labels"),
        json=dict(item_id=item_id, aspect=aspect, label=label, note="合成のメモ"),
    )


async def test_create_label_set_splits_like_label_template(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
    store: MemoryEvaluationCalibrationStore,
) -> None:
    """job の回答を項目にし、区分は suite の `label-template` と同じ（層ごとの sha256 の順）。"""
    first, _ = clients
    label_set_id = await _create(first, synthetic=True)
    detail = (await first.get(_set(label_set_id))).json()["data"]
    assert detail["summary"]["human_item_count"] == 9
    assert detail["summary"]["synthetic_item_count"] > 0
    assert detail["skipped"] == dict(record_missing=1)
    assert detail["can_manage"] is True
    # 標準回答の無い項目は claim_support だけ。
    assert detail["summary"]["row_count"] == 8 * 3 + 1
    assert [aspect["aspect"] for aspect in detail["aspects"]] == [
        "requirement_coverage",
        "answer_correctness",
        "claim_support",
    ]
    items = await store.list_items(label_set_id)
    human = [item for item in items if item.origin == "human"]
    strata = dict((item.payload["case_id"], item.payload["category"]) for item in human)
    expected = assign_splits(strata, holdout_ratio=0.7, salt=DEFAULT_SPLIT_SALT)
    assert all(item.split == expected[item.payload["case_id"]] for item in human)
    synthetic = (await first.get(_set(label_set_id, "/synthetic"))).json()["data"]
    assert synthetic["items"] and all(item["fail_aspects"] for item in synthetic["items"])
    target = synthetic["items"][0]["item_id"]
    excluded = await first.put(_set(label_set_id, "/synthetic/" + target), json=dict(excluded=True))
    assert excluded.json()["data"]["excluded"] is True


async def test_annotation_is_blind_between_annotators(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """記入者は自分の判定だけを見る（他の記入者の判定・評価器の判定を返さない）。"""
    first, second = clients
    label_set_id = await _create(first)
    row = (await _rows(first, label_set_id))[0]
    assert set(row) == set(
        (
            "item_id",
            "aspect",
            "split",
            "question",
            "answer",
            "standard_answer",
            "evidence",
            "label",
            "error_type",
            "note",
        )
    )
    saved = await _label(first, label_set_id, row["item_id"], row["aspect"], "fail")
    assert saved.status_code == 200, saved.text
    mine = await _rows(first, label_set_id)
    assert mine[0]["label"] == "fail"
    theirs = await _rows(second, label_set_id)
    assert all(item["label"] is None for item in theirs)
    unlabeled = await second.get(
        _set(label_set_id, "/rows"), params=dict(status="unlabeled", limit=1)
    )
    assert unlabeled.json()["data"]["unlabeled"] == len(theirs)
    cleared = await _label(first, label_set_id, row["item_id"], row["aspect"], None)
    assert cleared.status_code == 200
    assert (await _rows(first, label_set_id))[0]["label"] is None
    bad = await first.put(
        _set(label_set_id, "/labels"),
        json=dict(item_id=row["item_id"], aspect=row["aspect"], label="pass", error_type="x"),
    )
    assert bad.status_code == 422
    missing = await _label(first, label_set_id, "unknown", "claim_support", "pass")
    assert missing.status_code == 404


async def _label_all(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient], label_set_id: str, *, disagree: int
) -> list[dict[str, Any]]:
    """2 人で全行を記入する（先頭の ``disagree`` 行だけ食い違わせる）。"""
    first, second = clients
    rows = await _rows(first, label_set_id)
    for index, row in enumerate(rows):
        label = "pass" if index % 3 else "fail"
        await _label(first, label_set_id, row["item_id"], row["aspect"], label)
        other = ("fail" if label == "pass" else "pass") if index < disagree else label
        await _label(second, label_set_id, row["item_id"], row["aspect"], other)
    return rows


async def test_agreement_and_adjudication_make_final_labels(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """記入者の間の κ と食い違いを出し、裁定した行と全員一致の行が最終の判定になる。"""
    first, _ = clients
    label_set_id = await _create(first)
    rows = await _label_all(clients, label_set_id, disagree=2)
    agreement = (await first.get(_set(label_set_id, "/agreement"))).json()["data"]
    assert agreement["annotators"] == ["a1", "a2"]
    assert agreement["total"] == 2
    assert sum(aspect["disagreed"] for aspect in agreement["aspects"]) == 2
    assert all(aspect["pairs"][0]["annotators"] == ["a1", "a2"] for aspect in agreement["aspects"])
    blocked = await first.get(_set(label_set_id, "/export"))
    assert blocked.status_code == 409
    for row in agreement["rows"]:
        assert set(item["code"] for item in row["annotations"]) == set(("a1", "a2"))
        response = await first.put(
            _set(label_set_id, "/adjudications"),
            json=dict(item_id=row["item_id"], aspect=row["aspect"], label="fail"),
        )
        assert response.status_code == 200
    after = (await first.get(_set(label_set_id, "/agreement"))).json()["data"]
    assert after["total"] == 0
    assert sum(aspect["adjudicated"] for aspect in after["aspects"]) == 2
    exported = await first.get(_set(label_set_id, "/export"))
    assert exported.status_code == 200
    document = json.loads(exported.text)
    assert tuple(document) == LABEL_SET_FIELDS
    assert all(tuple(item) == LABEL_SET_ITEM_FIELDS for item in document["items"])
    by_id = dict((item["id"], item) for item in document["items"])
    first_row = rows[0]
    assert by_id[first_row["item_id"]]["labels"][first_row["aspect"]] == "fail"
    assert by_id[first_row["item_id"]]["labeler"] == "adj1"
    assert set(by_id[first_row["item_id"]]["annotator_labels"]) == set(("a1", "a2"))
    assert document["provenance"]["annotators"] == ["a1", "a2"]
    detail = (await first.get(_set(label_set_id))).json()["data"]
    final = detail["final_progress"]["aspects"]
    assert sum(aspect["holdout_rows"] for aspect in final) == sum(
        aspect["holdout_pass"] + aspect["holdout_fail"] for aspect in final
    )


async def test_label_set_round_trips_through_json(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """書き出した LabelSet を取り込むと、同じ最終の判定・記入者の判定の LabelSet に戻る。"""
    first, _ = clients
    label_set_id = await _create(first, synthetic=True)
    await _label_all(clients, label_set_id, disagree=1)
    agreement = (await first.get(_set(label_set_id, "/agreement"))).json()["data"]
    row = agreement["rows"][0]
    await first.put(
        _set(label_set_id, "/adjudications"),
        json=dict(item_id=row["item_id"], aspect=row["aspect"], label="pass"),
    )
    exported = json.loads((await first.get(_set(label_set_id, "/export"))).text)
    content = json.dumps(exported).encode("utf-8")
    imported = await first.post(
        BASE + "/label-sets/import",
        files=dict(file=("labels.json", content, "application/json")),
        data=dict(name="取り込んだ集合"),
    )
    assert imported.status_code == 201, imported.text
    new_id = imported.json()["data"]["label_set"]["label_set_id"]
    again = json.loads((await first.get(_set(new_id, "/export"))).text)

    def comparable(document: dict[str, Any]) -> dict[str, Any]:
        return dict(
            (item["id"], (item["split"], item["labels"], item["annotator_labels"], item["origin"]))
            for item in document["items"]
        )

    assert comparable(again) == comparable(exported)
    wrong = await first.post(
        BASE + "/label-sets/import",
        files=dict(file=("bad.json", b"[]", "application/json")),
    )
    assert wrong.status_code == 422


def test_label_set_document_matches_shared_contract() -> None:
    assert tuple(LabelSetDocument.model_fields) == LABEL_SET_FIELDS
    assert tuple(LabelSetItem.model_fields) == LABEL_SET_ITEM_FIELDS


METRICS = dict(
    requirement_coverage="requirement_coverage",
    answer_correctness="answer_correctness",
    claim_support="claim_support_rate",
)


def _perfect_evaluate(item: LabeledAnswer) -> Mapping[str, Any]:
    """人手の最終の判定どおりに指標を返す評価器（決定的なスタブ）。"""
    result: dict[str, Any] = dict(status="completed", rubric_version=11)
    for aspect, label in item.labels.items():
        result[METRICS[aspect]] = 1.0 if label == "pass" else 0.0
    return result


async def test_calibration_job_records_results(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校正は品質評価の job で動き、観点 × 区分の一致・判定のモデル・指示の sha256 を記録する。"""
    first, _ = clients
    label_set_id = await _create(first)
    await _label_all(clients, label_set_id, disagree=0)
    provenance = dict(provider="oci_enterprise_ai", model="judge-model-a")
    monkeypatch.setattr(
        evaluation_calibration,
        "backend_evaluator",
        lambda settings: (_perfect_evaluate, provenance),
    )
    monkeypatch.setattr(evaluation_calibration, "judge_provenance", lambda settings: provenance)
    response = await first.post(_set(label_set_id, "/calibrations"), json=dict(repeats=2))
    assert response.status_code == 202, response.text
    started = response.json()["data"]
    assert started["job"]["kind"] == "calibration"
    job_id = started["job"]["job_id"]
    await get_evaluation_job_service().wait_for(job_id)
    job = (await first.get("/api/evaluation/jobs/" + job_id)).json()["data"]
    assert job["status"] == "SUCCEEDED", job
    assert job["run_result"] is None
    assert job["completed_cases"] == job["total_cases"]
    listed = (await first.get(_set(label_set_id, "/calibrations"))).json()["data"]
    calibration = listed["items"][0]
    assert calibration["status"] == "SUCCEEDED"
    assert calibration["judge_model_id"] == "judge-model-a"
    assert calibration["judge_rubric_version"] == "11"
    assert calibration["prompt_sha256"]
    assert calibration["current"] is True
    aspects = dict((item["aspect"], item) for item in calibration["aspects"])
    holdout = next(
        split for split in aspects["claim_support"]["splits"] if split["split"] == "holdout"
    )
    assert holdout["accuracy"] == 1.0
    # 件数が 50 に届かないので合否は不合格（閾値は suite と同じ）。
    assert aspects["claim_support"]["passed"] is False
    assert any("holdout の件数" in failure for failure in aspects["claim_support"]["failures"])
    assert aspects["claim_support"]["variance"]["runs"] == 2
    assert aspects["claim_support"]["variance"]["unstable"] == 0
    status = (await first.get(BASE + "/status")).json()["data"]
    assert [item["calibrated"] for item in status["aspects"]] == [False, False, False]
    assert status["judge_model_id"] == "judge-model-a"


async def test_calibration_needs_final_labels(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    first, _ = clients
    label_set_id = await _create(first)
    response = await first.post(_set(label_set_id, "/calibrations"), json=dict(repeats=1))
    assert response.status_code == 409
    assert "最終の判定の決まらない行" in response.text


def test_judge_status_uses_only_the_current_judge() -> None:
    """合格した校正があり、判定のモデルと指示が今と同じ観点だけ「校正済み」にする。"""
    summary = dict(
        aspects=[
            dict(aspect="claim_support", passed=True, splits=[dict(split="holdout", kappa=0.72)]),
            dict(aspect="answer_correctness", passed=False, splits=[]),
        ]
    )
    current = CalibrationRecord(
        calibration_id="c1",
        label_set_id="s1",
        status="SUCCEEDED",
        judge_model_id="judge-model-a",
        prompt_sha256="sha-a",
        summary=summary,
    )
    other_model = CalibrationRecord(
        calibration_id="c2",
        label_set_id="s1",
        status="SUCCEEDED",
        judge_model_id="judge-model-b",
        prompt_sha256="sha-a",
        summary=summary,
    )
    identity = JudgeIdentity(model_id="judge-model-a", prompt_sha256="sha-a")
    status = dict((item["aspect"], item) for item in judge_status([other_model, current], identity))
    assert status["claim_support"]["calibrated"] is True
    assert status["claim_support"]["kappa"] == 0.72
    assert status["claim_support"]["calibration_id"] == "c1"
    assert status["answer_correctness"]["calibrated"] is False
    changed = JudgeIdentity(model_id="judge-model-a", prompt_sha256="sha-changed")
    assert not any(item["calibrated"] for item in judge_status([current], changed))


def test_calibration_permissions_are_registered() -> None:
    """記入は品質評価の権限、全員の判定・裁定・校正・入出力は判定の校正の管理の権限。"""
    prefix = "/evaluation/calibration"
    annotate = (
        ("GET", "/status"),
        ("GET", "/label-sets"),
        ("GET", "/label-sets/{label_set_id}"),
        ("GET", "/label-sets/{label_set_id}/rows"),
        ("PUT", "/label-sets/{label_set_id}/labels"),
    )
    manage = (
        ("GET", "/sources"),
        ("POST", "/label-sets"),
        ("POST", "/label-sets/import"),
        ("DELETE", "/label-sets/{label_set_id}"),
        ("GET", "/label-sets/{label_set_id}/agreement"),
        ("PUT", "/label-sets/{label_set_id}/adjudications"),
        ("GET", "/label-sets/{label_set_id}/synthetic"),
        ("PUT", "/label-sets/{label_set_id}/synthetic/{item_id}"),
        ("GET", "/label-sets/{label_set_id}/calibrations"),
        ("POST", "/label-sets/{label_set_id}/calibrations"),
        ("GET", "/label-sets/{label_set_id}/export"),
    )
    for method, path in annotate:
        assert permission_for_route(method, prefix + path) == frozenset((MENU_EVALUATION,))
    for method, path in manage:
        assert permission_for_route(method, prefix + path) == frozenset(
            (EVALUATION_CALIBRATION_MANAGE,)
        )
    assert PERMISSION_BY_CODE[EVALUATION_CALIBRATION_MANAGE].implies == (MENU_EVALUATION,)


async def test_annotator_without_manage_sees_no_final_or_calibration(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理の権限が無い記入者には、最終の判定の進み具合と校正の結果を返さない。"""
    from app.api.routes import evaluation_calibration as routes

    first, second = clients
    label_set_id = await _create(first)
    monkeypatch.setattr(routes, "_can_manage", lambda request: False)
    listed = (await second.get(BASE + "/label-sets")).json()["data"]
    assert listed["can_manage"] is False
    assert listed["items"][0]["latest_calibration"] is None
    detail = (await second.get(_set(label_set_id))).json()["data"]
    assert detail["final_progress"] is None
    assert detail["can_manage"] is False


async def test_sources_and_delete(
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    first, _ = clients
    sources = (await first.get(BASE + "/sources")).json()["data"]
    assert sources["jobs"][0]["case_count"] == 10
    missing = await first.post(
        BASE + "/label-sets", json=dict(name="集合 B", job_ids=["job-missing"])
    )
    assert missing.status_code == 422
    label_set_id = await _create(first)
    assert (await first.delete(_set(label_set_id))).status_code == 200
    assert (await first.get(_set(label_set_id))).status_code == 404


def test_schema_ddl_names_follow_rag_prefix() -> None:
    sql = oracle_evaluation_calibration_schema_sql()
    for table in (
        "rag_evaluation_label_sets",
        "rag_evaluation_label_items",
        "rag_evaluation_labels",
        "rag_evaluation_calibrations",
    ):
        assert "CREATE TABLE " + table + " (" in sql
    assert "ON DELETE CASCADE" in sql
    assert "CHECK (label IN ('pass', 'fail', 'cannot_judge'))" in sql


class _RecordingOracle:
    """OracleClient の代役（transaction の中の SQL を記録する）。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.fetched: list[tuple[str, dict[str, object]]] = []

    async def _run_transaction(self, operation: Any, **_: Any) -> Any:
        return operation(None)

    async def _fetch_all(self, statement: str, binds: dict[str, object]) -> list[dict[str, object]]:
        self.fetched.append((statement, dict(binds)))
        return self.rows

    async def _fetch_one(
        self, statement: str, binds: dict[str, object]
    ) -> dict[str, object] | None:
        self.fetched.append((statement, dict(binds)))
        return self.rows[0] if self.rows else None


async def test_oracle_store_writes_label_set_items_and_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle の保存先は集合・項目・判定を 1 つの transaction で書き、判定は更新か追加にする。"""
    from app.clients import evaluation_calibration_store as module

    executed: list[tuple[str, dict[str, object]]] = []

    def execute(_connection: object, statement: str, binds: Any, **_: Any) -> None:
        executed.append((" ".join(statement.split()), dict(binds)))

    def execute_count(_connection: object, statement: str, binds: Any) -> int:
        executed.append((" ".join(statement.split()), dict(binds)))
        return 0

    monkeypatch.setattr(module, "_execute", execute)
    monkeypatch.setattr(module, "_execute_count", execute_count)
    oracle = _RecordingOracle()
    store = module.OracleEvaluationCalibrationStore(oracle)  # type: ignore[arg-type]
    record = module.LabelSetRecord(
        label_set_id="s1",
        name="集合",
        origin="jobs",
        rubric_id="answer_quality",
        items_sha256="h",
        item_count=1,
    )
    item = module.LabelItemRecord(
        item_id="i1", item_order=0, split="holdout", origin="human", payload=dict(id="i1")
    )
    label = module.LabelRecord(
        item_id="i1", aspect="claim_support", role="annotation", annotator_code="a1", label="pass"
    )
    await store.create_label_set(record, [item], [label])
    targets = [statement.split(" (", 1)[0] for statement, _ in executed]
    assert targets == [
        "INSERT INTO rag_evaluation_label_sets",
        "INSERT INTO rag_evaluation_label_items",
        "INSERT INTO rag_evaluation_labels",
    ]
    executed.clear()
    await store.save_label("s1", label)
    assert executed[0][0].startswith("UPDATE rag_evaluation_labels")
    assert executed[1][0].startswith("INSERT INTO rag_evaluation_labels")
    await store.list_labels("s1", role="annotation", annotator_hash="u1")
    statement, binds = oracle.fetched[-1]
    assert "annotator_hash = :annotator_hash" in statement
    assert binds == dict(label_set_id="s1", role="annotation", annotator_hash="u1")
    await store.list_calibrations(label_set_id="s1", status="SUCCEEDED")
    statement, _ = oracle.fetched[-1]
    assert "LEFT JOIN rag_evaluation_jobs j ON j.job_id = c.calibration_id" in statement
