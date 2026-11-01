from __future__ import annotations

import importlib
import io
import json
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from app.features.nl2sql.models import (
    AnnotationApplyItem,
    AnnotationApplyRequest,
    ClassifierPredictRequest,
    ClassifierTrainingExample,
    ClassifierTrainRequest,
    CommentSuggestionRequest,
    DbAdminExecuteRequest,
    ExecuteRequest,
    LegacyLearningMaterialData,
    Nl2SqlEngine,
    Nl2SqlProfile,
    PreviewRequest,
    ProfileRecommendationRequest,
    QueryResults,
    ReverseSqlRequest,
    RewriteRequest,
    SampleDataMutationRequest,
    SampleDataStep,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.main import app


class FakeEnterpriseAiClient:
    def __init__(self, *responses: str | Exception, configured: bool = True) -> None:
        self.responses = list(responses)
        self.configured = configured
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return self.configured

    def model_id(self) -> str:
        return "fake-enterprise-ai"

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        response_format: object | None = None,
    ) -> str:
        del timeout_seconds, max_retries
        self.calls.append({"prompt": prompt, "context": context, "system_prompt": system_prompt})
        if not self.responses:
            return ""
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeEmbeddingClient:
    def is_configured(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * 1024
            vector[1 if "入金" in text or "支払" in text else 2] = 1.0
            vectors.append(vector)
        return vectors


class FakeDbAdminSelectAdapter:
    def __init__(self) -> None:
        self.select_calls: list[tuple[str, int]] = []

    def execute_select(self, sql: str, row_limit: int) -> QueryResults:
        self.select_calls.append((sql, row_limit))
        return QueryResults(
            columns=["CUSTOMER_NAME"],
            rows=[{"CUSTOMER_NAME": "架空商事"}],
            total=1,
        )


def _workbook_bytes(workbook: Any) -> bytes:
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _single_sheet_workbook_bytes(title: str, rows: list[list[Any]]) -> bytes:
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    return _workbook_bytes(workbook)


def _pickle_marker(path: str) -> None:
    Path(path).write_text("pickle executed", encoding="utf-8")


class _MaliciousClassifierArtifact:
    def __init__(self, marker_path: Path) -> None:
        self.marker_path = marker_path

    def __reduce__(self) -> tuple[Any, tuple[str]]:
        return (_pickle_marker, (str(self.marker_path),))


def _classifier_model_artifact_bytes(
    *categories: str,
    embedding_model: str = "deterministic-hash-1024",
    feature_dim: int = 1024,
    include_intercept: bool = True,
    include_embedding_model: bool = True,
) -> bytes:
    row_count = 1 if len(categories) == 2 else len(categories)
    coef: list[list[float]] = []
    for index in range(row_count):
        vector = [0.0] * feature_dim
        if feature_dim:
            vector[min(index + 1, feature_dim - 1)] = 1.0
        coef.append(vector)
    payload: dict[str, Any] = {
        "classes": list(categories),
        "coef": coef,
        "feature_dim": feature_dim,
    }
    if include_embedding_model:
        payload["embedding_model"] = embedding_model
    if include_intercept:
        payload["intercept"] = [0.0] * row_count
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _import_sample(service: Nl2SqlService) -> None:
    service.import_sample_data(
        SampleDataMutationRequest(
            step=SampleDataStep.ALL,
            confirmation="ADMIN_EXECUTE",
        )
    )


def test_classifier_training_predicts_and_drives_profile_recommendation() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._embedding_client = FakeEmbeddingClient()
    service.create_profile(
        Nl2SqlProfile(
            id="payment",
            name="入金管理",
            description="入金遅延と支払状況を扱う profile。",
            allowed_tables=["PAYMENTS", "INVOICES"],
            glossary={"入金": "PAYMENTS.PAID_AT"},
        )
    )
    imported = service.import_classifier_training_data(
        filename="training_data.xlsx",
        content=_single_sheet_workbook_bytes(
            "training_data",
            [
                ["CATEGORY", "TEXT"],
                ["標準業務プロファイル", "請求金額が大きい取引先を見たい"],
                ["標準業務プロファイル", "売上合計を顧客別に確認したい"],
                ["入金管理", "入金が遅れている請求を確認したい"],
                ["入金管理", "未入金の支払状況を見たい"],
            ],
        ),
        replace=True,
    )
    assert imported.imported_count == 4
    training_data = service.classifier_training_data()
    assert training_data.total_examples == 4
    assert training_data.categories == ["入金管理", "標準業務プロファイル"]
    assert [(item.category, item.text) for item in training_data.examples[:2]] == [
        ("標準業務プロファイル", "請求金額が大きい取引先を見たい"),
        ("標準業務プロファイル", "売上合計を顧客別に確認したい"),
    ]

    first_status = service.train_classifier(ClassifierTrainRequest())
    assert first_status.ready
    assert first_status.example_count == 4
    assert first_status.category_count == 2

    status = service.train_classifier(ClassifierTrainRequest())
    assert status.ready
    assert status.classifier_version != first_status.classifier_version

    prediction = service.predict_classifier(
        ClassifierPredictRequest(question="未入金の請求を確認したい")
    )
    assert prediction.recommendation_source == "classifier"
    assert prediction.classifier_version == status.classifier_version
    assert prediction.candidates

    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="未入金の請求を確認したい")
    )
    assert recommendation.recommendation_source == "classifier"
    assert recommendation.classifier_version == status.classifier_version
    assert recommendation.category_scores


def test_classifier_model_import_replaces_the_single_model_and_preserves_it_on_failure() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    first = service.import_classifier_model_artifact(
        filename="first.json",
        content=_classifier_model_artifact_bytes("入金管理", "標準業務プロファイル"),
    )
    assert first.imported is True
    assert first.model is not None
    assert first.model.active is True
    assert first.model.source == "json:first.json"
    assert service.classifier_status().classifier_version == first.active_version

    second = service.import_classifier_model_artifact(
        filename="second.json",
        content=_classifier_model_artifact_bytes("監査", "標準業務プロファイル"),
    )
    assert second.imported is True
    assert second.active_version != first.active_version
    assert second.model is not None
    assert second.model.active is True
    assert service.classifier_status().classifier_version == second.active_version

    with pytest.raises(ValueError, match="JSON artifact"):
        service.import_classifier_model_artifact(
            filename="broken.txt",
            content=b"not-a-model",
        )
    assert service.classifier_status().classifier_version == second.active_version


def test_classifier_model_import_rejects_pickle_without_executing(
    tmp_path: Path,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    marker_path = tmp_path / "pickle-marker.txt"
    payload = pickle.dumps(_MaliciousClassifierArtifact(marker_path))

    with pytest.raises(ValueError, match="pickle/joblib artifact"):
        service.import_classifier_model_artifact(
            filename="malicious.joblib",
            content=payload,
        )

    assert not marker_path.exists()
    assert service.classifier_status().ready is False


def test_classifier_model_import_rejects_invalid_model_shapes() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    with pytest.raises(ValueError, match="feature_dim"):
        service.import_classifier_model_artifact(
            filename="wrong-dimension.json",
            content=_classifier_model_artifact_bytes(
                "入金管理",
                "標準業務プロファイル",
                feature_dim=768,
            ),
        )
    with pytest.raises(ValueError, match="intercept"):
        service.import_classifier_model_artifact(
            filename="missing-intercept.json",
            content=_classifier_model_artifact_bytes(
                "入金管理",
                "標準業務プロファイル",
                include_intercept=False,
            ),
        )

    assert service.classifier_status().ready is False


def test_classifier_model_import_rejects_invalid_metrics_without_replacing_artifact() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    active = service.import_classifier_model_artifact(
        filename="active.json",
        content=_classifier_model_artifact_bytes("入金管理", "標準業務プロファイル"),
    )
    broken = json.loads(
        _classifier_model_artifact_bytes("監査", "標準業務プロファイル").decode("utf-8")
    )
    broken["metrics"] = {
        "nested": {"a": 1},
        "training_examples": "abc",
    }

    with pytest.raises(ValueError, match="metrics.training_examples"):
        service.import_classifier_model_artifact(
            filename="broken-metrics.json",
            content=json.dumps(broken, ensure_ascii=False).encode("utf-8"),
        )

    status = service.classifier_status()
    assert status.ready is True
    assert status.classifier_version == active.active_version


def test_classifier_status_treats_corrupt_metrics_as_missing() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.import_classifier_model_artifact(
        filename="active.json",
        content=_classifier_model_artifact_bytes("入金管理", "標準業務プロファイル"),
    )
    with cast(Any, service)._lock:
        cast(Any, service)._classifier_artifact["metrics"] = {
            "training_examples": "abc",
            "source_example_count": None,
            "nested": {"a": 1},
        }

    status = service.classifier_status()

    assert status.ready is True
    assert status.trained_example_count == 0
    assert "training_examples" not in status.metrics
    assert "source_example_count" not in status.metrics
    assert "nested" not in status.metrics
    assert any("metrics.training_examples" in warning for warning in status.warnings)


def test_classifier_model_import_defaults_to_runtime_embedding_model_when_missing() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="payment",
            name="入金管理",
            allowed_tables=["PAYMENTS", "INVOICES"],
            glossary={"入金": "PAYMENTS.PAID_AT"},
        )
    )

    imported = service.import_classifier_model_artifact(
        filename="classifier.json",
        content=_classifier_model_artifact_bytes(
            "標準業務プロファイル",
            "入金管理",
            include_embedding_model=False,
        ),
    )
    prediction = service.predict_classifier(
        ClassifierPredictRequest(question="未入金の請求を確認したい")
    )

    assert imported.model is not None
    assert imported.model.embedding_model == "deterministic-hash-1024"
    assert any("deterministic fallback" in warning for warning in imported.warnings)
    assert prediction.recommendation_source == "classifier"
    assert prediction.classifier_version == imported.active_version


def test_classifier_prediction_falls_back_when_embedding_model_differs() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._embedding_client = FakeEmbeddingClient()
    service.create_profile(
        Nl2SqlProfile(
            id="payment",
            name="入金管理",
            allowed_tables=["PAYMENTS", "INVOICES"],
            glossary={"入金": "PAYMENTS.PAID_AT"},
        )
    )
    imported = service.import_classifier_model_artifact(
        filename="classifier.json",
        content=_classifier_model_artifact_bytes(
            "標準業務プロファイル",
            "入金管理",
            embedding_model="old-embedding-model",
        ),
    )
    assert imported.imported is True
    assert service.classifier_status().ready is True

    prediction = service.predict_classifier(
        ClassifierPredictRequest(question="未入金の請求を確認したい")
    )
    assert prediction.recommendation_source == "deterministic"
    assert any("embedding model" in warning for warning in prediction.warnings)

    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="未入金の請求を確認したい")
    )
    assert recommendation.recommendation_source == "deterministic"
    assert any("embedding model" in warning for warning in recommendation.warnings)


@pytest.mark.asyncio
async def test_classifier_model_api_exposes_only_single_model_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.features.nl2sql import router as nl2sql_router

    service = Nl2SqlService(store=MemoryNl2SqlStore())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    raw_model = _classifier_model_artifact_bytes("入金管理", "標準業務プロファイル")
    marker_path = tmp_path / "api-pickle-marker.txt"
    unsafe_model = pickle.dumps(_MaliciousClassifierArtifact(marker_path))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        imported = await client.post(
            "/api/nl2sql/classifier/model/import",
            files={"file": ("single.json", raw_model, "application/json")},
        )
        legacy_imported = await client.post(
            "/api/nl2sql/classifier/models/import",
            data={"activate": "true"},
            files={"file": ("legacy.json", raw_model, "application/json")},
        )
        unsafe_imported = await client.post(
            "/api/nl2sql/classifier/model/import",
            files={"file": ("unsafe.joblib", unsafe_model, "application/octet-stream")},
        )
        invalid_imported = await client.post(
            "/api/nl2sql/classifier/model/import",
            files={
                "file": (
                    "wrong-dimension.json",
                    _classifier_model_artifact_bytes(
                        "入金管理",
                        "標準業務プロファイル",
                        feature_dim=768,
                    ),
                    "application/json",
                )
            },
        )
        inactive = await client.post(
            "/api/nl2sql/classifier/models/import",
            data={"activate": "false"},
            files={"file": ("inactive.json", raw_model, "application/json")},
        )
        listed = await client.get("/api/nl2sql/classifier/models")
        activated = await client.post(
            "/api/nl2sql/classifier/models/legacy/activate",
            json={},
        )
        deleted = await client.delete("/api/nl2sql/classifier/models/legacy")

    assert imported.status_code == 200
    assert imported.json()["data"]["model"]["active"] is True
    assert legacy_imported.status_code == 200
    assert legacy_imported.json()["data"]["model"]["active"] is True
    assert unsafe_imported.status_code == 422
    assert "pickle/joblib artifact" in unsafe_imported.text
    assert not marker_path.exists()
    assert invalid_imported.status_code == 422
    assert "feature_dim" in invalid_imported.text
    assert inactive.status_code == 422
    assert "activate=false" in inactive.text
    assert listed.status_code == 404
    assert activated.status_code == 404
    assert deleted.status_code == 404


def test_db_admin_executor_requires_confirmation_for_non_select() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    selected = service.execute_db_admin_sql(
        DbAdminExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=3)
    )
    assert selected.executed is True
    assert selected.select_result is not None
    assert selected.statements[0].statement_type == "SELECT"

    confirmation_required = service.execute_db_admin_sql(
        DbAdminExecuteRequest(sql="COMMENT ON TABLE \"INVOICES\" IS '請求';")
    )
    assert confirmation_required.executed is False
    assert confirmation_required.statements[0].status == "confirmation_required"


def test_sql_execute_row_limit_is_bounded_1_to_100000() -> None:
    assert ExecuteRequest(sql="SELECT * FROM INVOICES").row_limit is None
    assert (
        ExecuteRequest.model_validate(
            {"sql": "SELECT * FROM INVOICES", "row_limit": None}
        ).row_limit
        is None
    )
    assert ExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=1).row_limit == 1
    assert ExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=100000).row_limit == 100000
    # 0(無制限指定)は許可しない。未指定/null だけが「total maximum なし」を表す。
    with pytest.raises(ValidationError):
        ExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=0)
    with pytest.raises(ValidationError):
        ExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=100001)
    with pytest.raises(ValidationError):
        ExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=-1)


@pytest.mark.parametrize("limit", [0, -1, 1.5, 100001])
def test_db_admin_execute_row_limit_rejects_out_of_range(limit: float) -> None:
    with pytest.raises(ValidationError):
        DbAdminExecuteRequest.model_validate({"sql": "SELECT * FROM INVOICES", "row_limit": limit})


@pytest.mark.parametrize("limit", [1, 100, 100000])
def test_db_admin_execute_row_limit_accepts_boundaries(limit: int) -> None:
    assert DbAdminExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=limit).row_limit == limit


def test_db_admin_executor_with_dml_requires_confirmation() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    confirmation_required = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql=(
                "WITH pending AS (SELECT INVOICE_ID FROM INVOICES WHERE STATUS = 'PENDING') "
                "UPDATE INVOICES SET STATUS = 'REVIEWED' "
                "WHERE INVOICE_ID IN (SELECT INVOICE_ID FROM pending)"
            )
        )
    )

    assert confirmation_required.executed is False
    assert confirmation_required.statements[0].status == "confirmation_required"
    assert any("ADMIN_EXECUTE" in warning for warning in confirmation_required.warnings)


def test_db_admin_executor_blocks_mixed_select_and_update_batch() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    blocked = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql=(
                "SELECT INVOICE_ID FROM INVOICES; "
                "UPDATE INVOICES SET STATUS = 'REVIEWED' WHERE INVOICE_ID = 1"
            ),
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert blocked.executed is False
    assert {statement.status for statement in blocked.statements} == {"blocked"}
    assert any("SELECT は含められません" in warning for warning in blocked.warnings)


def test_db_admin_executor_select_uses_oracle_select_data_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = FakeDbAdminSelectAdapter()
    monkeypatch.setattr(service, "_oracle_adapter", adapter)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)

    selected = service.execute_db_admin_sql(
        DbAdminExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=3)
    )

    assert selected.executed is True
    assert selected.runtime == "oracle"
    assert selected.select_result is not None
    assert selected.committed is False
    assert selected.statements[0].statement_type == "SELECT"
    assert selected.statements[0].status == "executed"
    assert adapter.select_calls == [("SELECT * FROM INVOICES", 3)]


def test_db_admin_executor_max_row_limit_reaches_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = FakeDbAdminSelectAdapter()
    monkeypatch.setattr(service, "_oracle_adapter", adapter)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)

    selected = service.execute_db_admin_sql(
        DbAdminExecuteRequest(sql="SELECT * FROM INVOICES", row_limit=100000)
    )

    assert selected.executed is True
    assert adapter.select_calls == [("SELECT * FROM INVOICES", 100000)]


def test_profile_upsert_preserves_allowed_views() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    created = service.create_profile(
        Nl2SqlProfile(
            id="finance",
            name="財務プロファイル",
            description="表とビューを併用する profile。",
            allowed_tables=["INVOICES"],
            allowed_views=["V_INVOICE_SUMMARY"],
            sql_rules=["金額は円単位で表示する。"],
        )
    )

    assert created.allowed_views == ["APP.V_INVOICE_SUMMARY"]
    assert created.sql_rules == ["金額は円単位で表示する。"]

    updated = service.update_profile(
        "finance",
        lambda current: current.model_copy(update={"allowed_views": ["V_CUSTOMER_BALANCE"]}),
    )

    assert updated.allowed_views == ["APP.V_CUSTOMER_BALANCE"]
    assert updated.sql_rules == ["金額は円単位で表示する。"]


def test_profile_legacy_snapshot_defaults_allowed_views_and_folds_legacy_rules() -> None:
    profile = Nl2SqlProfile.model_validate(
        {
            "id": "legacy",
            "name": "旧 snapshot",
            "description": "allowed_views が無く、全体ルールを旧設定に持つ旧データ。",
            "allowed_tables": ["INVOICES"],
            "sql_rules": ["既存ルール"],
            # 以前の版は全体ルールを削除した生成方式の設定の additional_instructions に保存した。
            "select_ai_config": {"additional_instructions": "既存ルール\n金額は円単位で表示する。"},
            "default_engine": "select_ai",
        }
    )

    assert profile.allowed_views == []
    assert profile.sql_rules == ["既存ルール", "金額は円単位で表示する。"]
    assert "select_ai_config" not in profile.model_dump()
    assert "default_engine" not in profile.model_dump()


def test_classifier_training_data_xlsx_accepts_legacy_headers_and_blanks() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(Nl2SqlProfile(id="payment", name="入金管理"))
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "training_data"
    sheet.append([])
    sheet.append([None, None])
    sheet.append(["category", "QUESTION"])
    sheet.append(["標準業務プロファイル", "請求金額を確認したい"])
    sheet.append(["入金管理", "未入金の請求を確認したい"])
    sheet.append(["入金管理", "未入金の請求を確認したい"])
    sheet.append(["", "質問だけの空行"])
    sheet.append(["標準業務プロファイル", ""])

    imported = service.import_classifier_training_data(
        filename="training_data.xlsx",
        content=_workbook_bytes(workbook),
        replace=True,
    )

    assert imported.imported_count == 2
    assert imported.skipped_count == 3
    assert imported.categories == ["入金管理", "標準業務プロファイル"]
    listed = service.classifier_training_data()
    assert listed.total_examples == 2
    assert [item.text for item in listed.examples] == [
        "請求金額を確認したい",
        "未入金の請求を確認したい",
    ]

    service.create_profile(Nl2SqlProfile(id="audit", name="監査"))
    replaced = service.import_classifier_training_data(
        filename="replacement.xlsx",
        content=_single_sheet_workbook_bytes(
            "training_data",
            [["CATEGORY", "TEXT"], ["監査", "監査ログを確認したい"]],
        ),
        replace=True,
    )
    assert replaced.imported_count == 1
    replaced_listing = service.classifier_training_data()
    assert replaced_listing.total_examples == 1
    assert replaced_listing.categories == ["監査"]
    assert replaced_listing.examples[0].text == "監査ログを確認したい"


def test_classifier_training_data_replace_rejects_invalid_header_without_deleting() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    imported = service.import_classifier_training_data(
        filename="training_data.xlsx",
        content=_single_sheet_workbook_bytes(
            "training_data",
            [["CATEGORY", "TEXT"], ["標準業務プロファイル", "請求金額を確認したい"]],
        ),
        replace=True,
    )
    assert imported.imported_count == 1

    with pytest.raises(ValueError, match="CATEGORY または PROFILE_ID"):
        service.import_classifier_training_data(
            filename="broken.xlsx",
            content=_single_sheet_workbook_bytes(
                "training_data",
                [["FOO", "BAR"], ["監査", "監査ログを確認したい"]],
            ),
            replace=True,
        )

    listed = service.classifier_training_data()
    assert listed.total_examples == 1
    assert listed.examples[0].text == "請求金額を確認したい"


@pytest.mark.asyncio
async def test_classifier_training_data_replace_api_returns_422_without_deleting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as nl2sql_router

    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.import_classifier_training_data(
        filename="training_data.xlsx",
        content=_single_sheet_workbook_bytes(
            "training_data",
            [["CATEGORY", "TEXT"], ["標準業務プロファイル", "請求金額を確認したい"]],
        ),
        replace=True,
    )
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/nl2sql/classifier/training-data/import",
            data={"replace": "true"},
            files={
                "file": (
                    "broken.xlsx",
                    _single_sheet_workbook_bytes(
                        "training_data",
                        [["FOO", "BAR"], ["監査", "監査ログを確認したい"]],
                    ),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert response.status_code == 422
    assert "CATEGORY または PROFILE_ID" in response.text
    assert service.classifier_training_data().total_examples == 1


def test_classifier_training_data_import_serializes_duplicate_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    content = _single_sheet_workbook_bytes(
        "training_data",
        [["CATEGORY", "TEXT"], ["標準業務プロファイル", "請求金額を確認したい"]],
    )
    original_parse = service._parse_classifier_training_file  # noqa: SLF001
    active_parse_count = 0
    max_active_parse_count = 0
    active_parse_lock = threading.Lock()
    first_parse_can_finish = threading.Event()

    def slow_parse(
        filename: str, content: bytes, warnings: list[str]
    ) -> tuple[list[tuple[str, str, str]], int]:
        nonlocal active_parse_count, max_active_parse_count
        with active_parse_lock:
            active_parse_count += 1
            max_active_parse_count = max(max_active_parse_count, active_parse_count)
        try:
            first_parse_can_finish.wait(timeout=0.2)
            return original_parse(filename, content, warnings)
        finally:
            with active_parse_lock:
                active_parse_count -= 1
            first_parse_can_finish.set()

    monkeypatch.setattr(service, "_parse_classifier_training_file", slow_parse)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: service.import_classifier_training_data(
                    filename="training_data.xlsx",
                    content=content,
                ),
                range(2),
            )
        )

    assert sum(result.imported_count for result in results) == 1
    assert sum(result.skipped_count for result in results) == 1
    assert max_active_parse_count == 1
    assert service.classifier_training_data().total_examples == 1


def test_classifier_training_data_xlsx_keeps_zero_text_value() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    imported = service.import_classifier_training_data(
        filename="zero_text.xlsx",
        content=_single_sheet_workbook_bytes(
            "training_data",
            [
                [],
                ["CATEGORY", "TEXT"],
                ["標準業務プロファイル", 0],
            ],
        ),
        replace=True,
    )

    assert imported.imported_count == 1
    assert service.classifier_training_data().examples[0].text == "0"


def test_classifier_training_data_xlsx_export_contains_profile_and_source_metadata() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.import_classifier_training_data(
        filename="training_data.xlsx",
        content=_single_sheet_workbook_bytes(
            "training_data",
            [
                ["CATEGORY", "TEXT"],
                ["標準業務プロファイル", "請求金額を確認したい"],
                ["入金管理", "未入金の請求を確認したい"],
            ],
        ),
        replace=True,
        profile_id="default",
    )

    filename, content = service.export_classifier_training_data_xlsx()
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows = list(workbook["training_data"].iter_rows(values_only=True))

    assert filename == "nl2sql_classifier_training_data.xlsx"
    assert rows == [
        (
            "CATEGORY",
            "TEXT",
            "PROFILE_ID",
            "SOURCE",
            "SOURCE_TYPE",
            "SOURCE_HISTORY_ID",
        ),
        (
            "標準業務プロファイル",
            "請求金額を確認したい",
            "default",
            "training_data.xlsx",
            "file",
            None,
        ),
        (
            "入金管理",
            "未入金の請求を確認したい",
            "default",
            "training_data.xlsx",
            "file",
            None,
        ),
    ]


def test_classifier_training_data_xlsx_export_import_round_trips_safe_text() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    with service._lock:  # noqa: SLF001 - export fixture
        service._classifier_examples = [  # noqa: SLF001
            ClassifierTrainingExample(
                id="formula-text",
                category="標準業務プロファイル",
                text="=SUM(A1:A2)\x01",
                profile_id="default",
                profile_name="標準プロファイル",
                source="edge.xlsx",
                source_type="file",
                created_at="2026-07-19T00:00:00+00:00",
                updated_at="2026-07-19T00:00:00+00:00",
            )
        ]

    filename, content = service.export_classifier_training_data_xlsx()
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=False, data_only=False)
    exported_text_cell = workbook["training_data"]["B2"]
    assert exported_text_cell.value == "=SUM(A1:A2)"
    assert exported_text_cell.data_type == "s"
    workbook.close()

    round_tripped = Nl2SqlService(store=MemoryNl2SqlStore())
    imported = round_tripped.import_classifier_training_data(
        filename=filename,
        content=content,
        replace=True,
    )

    assert imported.imported_count == 1
    assert round_tripped.classifier_training_data().examples[0].text == "=SUM(A1:A2)"


def test_classifier_training_data_jsonl_export_route_is_not_registered() -> None:
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/nl2sql/classifier/training-data/export.jsonl" not in paths


def test_annotations_require_confirmation_without_oracle() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    _import_sample(service)

    suggestions = service.suggest_annotations()
    assert suggestions.suggestions
    first = suggestions.suggestions[0]

    applied = service.apply_annotations(
        AnnotationApplyRequest(
            items=[
                AnnotationApplyItem(
                    object_name=first.object_name,
                    object_type=first.object_type,
                    annotation_name=first.annotation_name,
                    annotation_value=first.annotation_value,
                )
            ],
        )
    )
    assert not applied.executed
    assert applied.statements
    assert applied.statements[0].status == "confirmation_required"
    assert "ANNOTATIONS" in applied.statements[0].sql


def test_profile_learning_material_imports_xlsx_and_exports_xlsx() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    imported = service.import_profile_learning_material(
        profile_id="default",
        filename="terms.xlsx",
        content=_single_sheet_workbook_bytes(
            "terms",
            [["TERM", "DEFINITION"], ["粗利", "INVOICES.PROFIT"]],
        ),
        mode="merge",
    )
    assert imported.imported_terms == 1
    assert imported.profile.glossary["粗利"] == "INVOICES.PROFIT"

    imported_rules = service.import_profile_learning_material(
        profile_id="default",
        filename="rules.xlsx",
        content=_single_sheet_workbook_bytes(
            "rules",
            [["CATEGORY", "RULE"], ["共通", "日付条件は TRUNC を使う"]],
        ),
        mode="merge",
    )
    assert imported_rules.imported_rules == 1
    assert imported_rules.profile.sql_rules == ["日付条件は TRUNC を使う"]

    filename, workbook_bytes = service.export_profile_learning_material_xlsx("default")
    assert filename.endswith("_learning_material.xlsx")
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(workbook_bytes), read_only=True)
    assert "terms" in workbook.sheetnames
    assert "few_shot" not in workbook.sheetnames
    assert "rules" in workbook.sheetnames
    assert workbook["rules"]["A1"].value == "RULE"
    assert workbook["rules"]["A2"].value == "日付条件は TRUNC を使う"


def test_profile_learning_material_rejects_invalid_mode_without_merge_fallback() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    with pytest.raises(ValueError, match="未対応の import mode"):
        service.import_profile_learning_material(
            profile_id="default",
            filename="terms.xlsx",
            content=_single_sheet_workbook_bytes(
                "terms",
                [["TERM", "DEFINITION"], ["粗利", "INVOICES.PROFIT"]],
            ),
            mode="append",
        )


def test_profile_learning_material_xlsx_handles_multi_sheet_dedupe_and_replace() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="compat",
            name="互換性検証",
            glossary={"既存": "OLD.VALUE"},
            sql_rules=["既存ルール"],
            few_shot_examples=[{"question": "既存質問", "sql": "SELECT 1 FROM DUAL"}],
        )
    )
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.Workbook()
    terms = workbook.active
    terms.title = "Terms"
    terms.append(["term", "definition"])
    terms.append(["粗利", "INVOICES.PROFIT"])
    terms.append(["粗利", "INVOICES.PROFIT"])
    terms.append(["空定義", ""])
    rules = workbook.create_sheet("Rules")
    rules.append(["Category", "Text"])
    rules.append(["共通", "日付条件は TRUNC を使う"])
    rules.append(["共通", "日付条件は TRUNC を使う"])
    examples = workbook.create_sheet("few_shot")
    examples.append(["question", "expected_sql"])
    examples.append(["粗利を見たい", "SELECT PROFIT FROM INVOICES"])
    examples.append(["粗利を見たい", "SELECT PROFIT FROM INVOICES"])

    merged = service.import_profile_learning_material(
        profile_id="compat",
        filename="legacy_learning.xlsx",
        content=_workbook_bytes(workbook),
        mode="merge",
    )

    assert merged.imported_terms == 1
    assert merged.imported_rules == 1
    assert merged.imported_examples == 1
    assert merged.skipped_count == 1
    assert merged.profile.glossary["既存"] == "OLD.VALUE"
    assert merged.profile.glossary["粗利"] == "INVOICES.PROFIT"
    assert merged.profile.sql_rules == ["既存ルール", "日付条件は TRUNC を使う"]
    assert merged.profile.few_shot_examples == [
        {"question": "既存質問", "sql": "SELECT 1 FROM DUAL"},
        {"question": "粗利を見たい", "sql": "SELECT PROFIT FROM INVOICES"},
    ]

    replaced = service.import_profile_learning_material(
        profile_id="compat",
        filename="terms.xlsx",
        content=_single_sheet_workbook_bytes(
            "terms",
            [["term", "definition"], ["売上", "INVOICES.TOTAL_AMOUNT"]],
        ),
        mode="replace",
    )

    assert replaced.profile.glossary == {"売上": "INVOICES.TOTAL_AMOUNT"}
    # 全体ルールは replace でも消さない（以前の版と同じ。ルールのシートが無い import のため）。
    assert replaced.profile.sql_rules == ["既存ルール", "日付条件は TRUNC を使う"]
    assert replaced.profile.few_shot_examples == []


def test_global_learning_material_imports_exports_and_applies_all_rules() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="osaka",
            name="大阪プロファイル",
            category="OSAKA",
            glossary={"売上": "PROFILE.SALES"},
            sql_rules=["profile 固有ルール"],
        )
    )
    openpyxl = importlib.import_module("openpyxl")
    terms_book = openpyxl.Workbook()
    terms = terms_book.active
    terms.title = "terms"
    terms.append(["TERM", "DEFINITION"])
    terms.append(["粗利", "INVOICES.PROFIT"])
    terms.append(["売上", "LEGACY.SALES"])
    rules_book = openpyxl.Workbook()
    rules = rules_book.active
    rules.title = "rules"
    rules.append(["CATEGORY", "RULE"])
    rules.append(["共通", "共通ルール"])
    rules.append(["OSAKA", "大阪ルール"])
    rules.append(["TOKYO", "東京ルール"])

    material = service.import_legacy_terms(
        filename="terms.xlsx",
        content=_workbook_bytes(terms_book),
    )
    assert material.glossary["粗利"] == "INVOICES.PROFIT"
    material = service.import_legacy_rules(
        filename="rules.xlsx",
        content=_workbook_bytes(rules_book),
    )
    assert material.rules == ["共通ルール", "大阪ルール", "東京ルール"]

    profile = service.get_profile("osaka")
    assert cast(Any, service)._effective_glossary(profile)["売上"] == "PROFILE.SALES"
    # グローバルルールは _legacy_learning_material.rules として保存され、全 profile の
    # SQL 生成へ注入される。profile 固有の sql_rules はその後ろに続く。
    assert cast(Any, service)._effective_sql_rules(profile) == [
        "共通ルール",
        "大阪ルール",
        "東京ルール",
        "profile 固有ルール",
    ]
    assert "東京ルール" in cast(Any, service)._append_rules_to_question("質問", profile)
    assert profile.sql_rules == ["profile 固有ルール"]

    terms_filename, terms_bytes = service.export_legacy_terms_xlsx()
    rules_filename, rules_bytes = service.export_legacy_rules_xlsx()
    assert terms_filename == "terms.xlsx"
    assert rules_filename == "rules.xlsx"
    terms_export = openpyxl.load_workbook(io.BytesIO(terms_bytes), read_only=True)
    rules_export = openpyxl.load_workbook(io.BytesIO(rules_bytes), read_only=True)
    assert terms_export.active["A1"].value == "TERM"
    assert rules_export.active["A1"].value == "RULE"
    assert rules_export.active["B1"].value is None


def test_learning_material_header_matching_keeps_japanese_and_empty_columns_distinct() -> None:
    term_cases: list[tuple[list[list[Any]], dict[str, str]]] = [
        (
            [["No.", "用語", "定義"], [1, "売上", "INVOICES.TOTAL_AMOUNT"]],
            {"売上": "INVOICES.TOTAL_AMOUNT"},
        ),
        (
            [["備考", "TERM", "DEFINITION"], ["メモ1", "粗利", "INVOICES.PROFIT"]],
            {"粗利": "INVOICES.PROFIT"},
        ),
        (
            [["", "TERM", "DEFINITION"], ["", "税額", "INVOICES.TAX_AMOUNT"]],
            {"税額": "INVOICES.TAX_AMOUNT"},
        ),
    ]
    for rows, expected in term_cases:
        service = Nl2SqlService(store=MemoryNl2SqlStore())
        material = service.import_legacy_terms(
            filename="terms.xlsx",
            content=_single_sheet_workbook_bytes("terms", rows),
        )
        assert material.glossary == expected

    service = Nl2SqlService(store=MemoryNl2SqlStore())
    rules = service.import_legacy_rules(
        filename="rules.xlsx",
        content=_single_sheet_workbook_bytes(
            "rules",
            [["カテゴリ", "RULE"], ["共通", "SELECT のみ"]],
        ),
    )
    assert rules.rules == ["SELECT のみ"]

    profile_import = service.import_profile_learning_material(
        profile_id="default",
        filename="learning_material.xlsx",
        content=_single_sheet_workbook_bytes(
            "terms",
            [["No.", "用語", "定義"], [1, "売上", "INVOICES.TOTAL_AMOUNT"]],
        ),
    )
    assert profile_import.imported_terms == 1
    assert profile_import.profile.glossary == {"売上": "INVOICES.TOTAL_AMOUNT"}


def test_legacy_learning_material_rejects_unrecognized_empty_import_without_clearing() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.import_legacy_terms(
        filename="terms.xlsx",
        content=_single_sheet_workbook_bytes(
            "terms",
            [["TERM", "DEFINITION"], ["既存", "APP.EXISTING_COLUMN"]],
        ),
    )
    service.import_legacy_rules(
        filename="rules.xlsx",
        content=_single_sheet_workbook_bytes("rules", [["RULE"], ["既存ルール"]]),
    )

    with pytest.raises(ValueError, match="TERM/DEFINITION"):
        service.import_legacy_terms(
            filename="terms.xlsx",
            content=_single_sheet_workbook_bytes("terms", [["備考"], ["メモ"]]),
        )
    with pytest.raises(ValueError, match="RULE"):
        service.import_legacy_rules(
            filename="rules.xlsx",
            content=_single_sheet_workbook_bytes("rules", [["カテゴリ"], ["共通"]]),
        )

    material = service.get_legacy_learning_material()
    assert material.glossary == {"既存": "APP.EXISTING_COLUMN"}
    assert material.rules == ["既存ルール"]


def test_profile_learning_material_rejects_empty_parse_without_replacing_profile() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="billing-learning",
            name="請求学習",
            glossary={"既存": "APP.EXISTING_COLUMN"},
            few_shot_examples=[{"question": "既存質問", "sql": "SELECT 1 FROM DUAL"}],
        )
    )

    with pytest.raises(ValueError, match="TERM/DEFINITION"):
        service.import_profile_learning_material(
            profile_id="billing-learning",
            filename="learning_material.xlsx",
            content=_single_sheet_workbook_bytes("terms", [["備考"], ["メモ"]]),
            mode="replace",
        )

    profile = service.get_profile("billing-learning")
    assert profile.glossary == {"既存": "APP.EXISTING_COLUMN"}
    assert profile.few_shot_examples == [{"question": "既存質問", "sql": "SELECT 1 FROM DUAL"}]


def test_learning_material_exports_formula_like_and_control_character_values_as_text() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._legacy_learning_material = LegacyLearningMaterialData(
        glossary={"=SUM(1,1)": "A\x01B", "粗利": "=INVOICES.PROFIT"},
        rules=["=DELETE()", "A\x02B"],
    )
    service.create_profile(
        Nl2SqlProfile(
            id="formula-profile",
            name="式検証",
            glossary={"=TERM": "C\x03D"},
            few_shot_examples=[{"question": "=Q", "sql": "SELECT '\x04' FROM DUAL"}],
        )
    )

    openpyxl = importlib.import_module("openpyxl")
    _, legacy_terms_bytes = service.export_legacy_terms_xlsx()
    legacy_terms = openpyxl.load_workbook(io.BytesIO(legacy_terms_bytes), data_only=False)
    assert legacy_terms.active["A2"].value == "=SUM(1,1)"
    assert legacy_terms.active["A2"].data_type == "s"
    assert legacy_terms.active["B2"].value == "AB"
    assert legacy_terms.active["B2"].data_type == "s"
    assert legacy_terms.active["B3"].value == "=INVOICES.PROFIT"
    assert legacy_terms.active["B3"].data_type == "s"

    _, legacy_rules_bytes = service.export_legacy_rules_xlsx()
    legacy_rules = openpyxl.load_workbook(io.BytesIO(legacy_rules_bytes), data_only=False)
    assert legacy_rules.active["A2"].value == "=DELETE()"
    assert legacy_rules.active["A2"].data_type == "s"
    assert legacy_rules.active["A3"].value == "AB"

    _, profile_bytes = service.export_profile_learning_material_xlsx("formula-profile")
    profile_book = openpyxl.load_workbook(io.BytesIO(profile_bytes), data_only=False)
    assert profile_book["terms"]["A2"].value == "=TERM"
    assert profile_book["terms"]["A2"].data_type == "s"
    assert profile_book["terms"]["B2"].value == "CD"
    assert profile_book["few_shot"]["A2"].value == "=Q"
    assert profile_book["few_shot"]["A2"].data_type == "s"


@pytest.mark.parametrize("suffix", [".csv", ".xls", ".xlsm", ".txt", ".tsv"])
@pytest.mark.parametrize(
    "template_import",
    ["legacy_terms", "legacy_rules", "classifier_training", "profile_learning"],
)
def test_excel_template_imports_reject_non_xlsx_extensions(
    suffix: str,
    template_import: str,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    filename = f"template{suffix}"

    with pytest.raises(ValueError, match="xlsx テンプレート"):
        if template_import == "legacy_terms":
            service.import_legacy_terms(filename=filename, content=b"")
        elif template_import == "legacy_rules":
            service.import_legacy_rules(filename=filename, content=b"")
        elif template_import == "classifier_training":
            service.import_classifier_training_data(filename=filename, content=b"")
        else:
            service.import_profile_learning_material(
                profile_id="default",
                filename=filename,
                content=b"",
            )


def test_global_rule_xlsx_preserves_newlines_blank_lines_and_indentation() -> None:
    store = MemoryNl2SqlStore()
    service = Nl2SqlService(store=store)
    multiline_rule = (
        "SELECT customer_name\n\n    FROM customers\n    WHERE customer_status = 'ACTIVE'"
    )
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "rules"
    sheet.append(["RULE"])
    sheet.append([multiline_rule])

    imported = service.import_legacy_rules(
        filename="rules.xlsx",
        content=_workbook_bytes(workbook),
    )

    assert imported.rules == [multiline_rule]
    assert Nl2SqlService(store=store).get_legacy_learning_material().rules == [multiline_rule]

    _, exported_bytes = service.export_legacy_rules_xlsx()
    exported = openpyxl.load_workbook(io.BytesIO(exported_bytes), read_only=True)
    assert exported.active["A2"].value == multiline_rule


def test_legacy_rule_entries_snapshot_migrates_to_global_rules() -> None:
    store = MemoryNl2SqlStore()
    store.save_snapshot(
        {
            "legacy_learning_material": {
                "glossary": {"売上": "INVOICES.TOTAL_AMOUNT"},
                "rule_entries": [
                    {"category": "OSAKA", "rule": "日付条件は TRUNC を使う"},
                    {"category": "TOKYO", "rule": "日付条件は TRUNC を使う"},
                    {"category": "共通", "rule": "SELECT/WITH のみ"},
                ],
            }
        }
    )

    service = Nl2SqlService(store=store)

    assert service.get_legacy_learning_material().rules == [
        "日付条件は TRUNC を使う",
        "SELECT/WITH のみ",
    ]
    persisted = store.load_snapshot()
    assert persisted is not None
    material = cast(dict[str, Any], persisted["legacy_learning_material"])
    assert material["rules"] == ["日付条件は TRUNC を使う", "SELECT/WITH のみ"]
    assert "rule_entries" not in material


def test_enterprise_ai_direct_uses_global_and_profile_learning_material() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="INVOICES",
                logical_name="請求",
                columns=[
                    SchemaColumn(
                        column_name="TOTAL_AMOUNT",
                        logical_name="売上",
                        data_type="NUMBER",
                    ),
                    SchemaColumn(
                        column_name="TAX_AMOUNT",
                        logical_name="請求金額",
                        data_type="NUMBER",
                    ),
                ],
            )
        ],
    )
    service.import_legacy_terms(
        filename="terms.xlsx",
        content=_single_sheet_workbook_bytes(
            "terms",
            [["TERM", "DEFINITION"], ["売上", "INVOICES.TOTAL_AMOUNT"]],
        ),
    )
    service.create_profile(
        Nl2SqlProfile(
            id="billing-direct",
            name="請求管理",
            allowed_tables=["INVOICES"],
            glossary={"請求金額": "INVOICES.TAX_AMOUNT"},
            sql_rules=["プロファイル固有ルール"],
        )
    )
    service.import_legacy_rules(
        filename="rules.xlsx",
        content=_single_sheet_workbook_bytes(
            "rules",
            [["RULE"], ["グローバルルール"]],
        ),
    )
    fake = FakeEnterpriseAiClient(
        '{"sql":"SELECT TOTAL_AMOUNT FROM INVOICES","explanation":"売上を取得します。"}'
    )
    cast(Any, service)._enterprise_ai_client = fake

    service.preview(
        PreviewRequest(
            question="売上と請求金額を確認したい",
            engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
            profile_id="billing-direct",
            use_glossary=True,
        )
    )

    assert fake.calls
    assert "売上=INVOICES.TOTAL_AMOUNT" in fake.calls[0]["prompt"]
    assert "請求金額=INVOICES.TAX_AMOUNT" in fake.calls[0]["prompt"]
    assert "- 売上: INVOICES.TOTAL_AMOUNT" in fake.calls[0]["context"]
    assert "- 請求金額: INVOICES.TAX_AMOUNT" in fake.calls[0]["context"]
    assert "sql_rules:" in fake.calls[0]["context"]
    assert "グローバルルール" in fake.calls[0]["context"]
    assert "プロファイル固有ルール" in fake.calls[0]["context"]


def test_comment_llm_fallback_without_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    monkeypatch.setattr(
        cast(Any, service),
        "_enterprise_ai_client",
        FakeEnterpriseAiClient(configured=False),
    )
    _import_sample(service)

    comments = service.suggest_comments(CommentSuggestionRequest(use_llm=True))
    assert comments.suggestions
    assert comments.source == "deterministic"
    assert comments.warnings


def test_comment_llm_generation_uses_enterprise_ai_and_falls_back_on_bad_json() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    _import_sample(service)
    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient(
        '{"suggestions":[{"object_name":"EMPLOYEE","object_type":"table",'
        '"suggested_comment":"社員情報のヘッダ"}]}'
    )

    generated = service.suggest_comments(CommentSuggestionRequest(use_llm=True, max_items=2))

    assert generated.source == "enterprise_ai"
    assert generated.suggestions[0].object_name == "APP.EMPLOYEE"
    assert generated.suggestions[0].suggested_comment == "社員情報のヘッダ"

    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient(
        '{"suggestions":[{"object_name":"EMPLOYEE","object_type":"invalid",'
        '"suggested_comment":"壊れた候補"}]}'
    )

    fallback = service.suggest_comments(CommentSuggestionRequest(use_llm=True, max_items=2))

    assert fallback.source == "deterministic"
    assert fallback.warnings
    assert "fallback" in fallback.warnings[0]


def test_rewrite_never_calls_enterprise_ai_and_only_applies_glossary() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    fake = FakeEnterpriseAiClient("請求金額を税込金額として一覧したい")
    cast(Any, service)._enterprise_ai_client = fake
    service.update_profile(
        "default",
        lambda current: current.model_copy(update={"glossary": {"請求金額": "TOTAL_AMOUNT"}}),
    )

    rewritten = service.rewrite(
        RewriteRequest(question="請求金額を一覧で見たい", profile_id="default")
    )

    # LLM による自由な書き換えはしない(用語・同義語の置換だけ)。
    assert fake.calls == []
    assert rewritten.source == "deterministic"
    assert rewritten.model == ""
    assert rewritten.rewritten_question == "請求金額を一覧で見たい（請求金額=TOTAL_AMOUNT）"
    assert rewritten.warnings == []
    assert RewriteRequest(question="売上を確認").use_glossary is True
    assert "use_schema" not in RewriteRequest.model_json_schema()["properties"]


def test_rewrite_without_glossary_flag_returns_question_unchanged() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    fake = FakeEnterpriseAiClient("勝手に書き換えた質問")
    cast(Any, service)._enterprise_ai_client = fake
    service.update_profile(
        "default",
        lambda current: current.model_copy(update={"glossary": {"請求金額": "TOTAL_AMOUNT"}}),
    )

    rewritten = service.rewrite(
        RewriteRequest(question="請求金額を一覧で見たい", profile_id="default", use_glossary=False)
    )

    assert fake.calls == []
    assert rewritten.rewritten_question == "請求金額を一覧で見たい"


def test_rewrite_does_not_turn_sql_row_limit_into_question_text() -> None:
    """SQL を貼っても「先頭100件」のような件数表現を質問へ勝手に足さない。"""

    service = Nl2SqlService(store=MemoryNl2SqlStore())
    fake = FakeEnterpriseAiClient("従業員IDと所属部署IDを先頭100件取得したい")
    cast(Any, service)._enterprise_ai_client = fake
    sql = (
        'SELECT "e"."EMPLOYEE_ID","e"."DEPARTMENT_ID" FROM "ADMIN"."EMPLOYEE" "e" '
        "FETCH FIRST 100 ROWS ONLY"
    )

    rewritten = service.rewrite(RewriteRequest(question=sql, profile_id="default"))

    assert fake.calls == []
    assert rewritten.rewritten_question == sql
    assert "先頭" not in rewritten.rewritten_question
    assert "100件" not in rewritten.rewritten_question


def test_rewrite_preserves_empty_filter_template_and_skips_enterprise_ai() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    fake = FakeEnterpriseAiClient("部署情報を管理するテーブルから、管理部門の情報を検索する。")
    cast(Any, service)._enterprise_ai_client = fake
    question = '対象テーブル："部署情報を管理するテーブル"\n抽出項目：\n抽出条件：'

    rewritten = service.rewrite(RewriteRequest(question=question, profile_id="default"))

    assert rewritten.source == "deterministic"
    assert rewritten.rewritten_question == question
    assert "管理部門" not in rewritten.rewritten_question
    assert rewritten.warnings == ["抽出条件が空欄のため条件追加を抑止しました。"]
    assert fake.calls == []


def test_rewrite_applies_glossary_when_filter_slot_has_value() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    fake = FakeEnterpriseAiClient("部署名が管理部門の部署を検索する。")
    cast(Any, service)._enterprise_ai_client = fake
    service.update_profile(
        "default",
        lambda current: current.model_copy(update={"glossary": {"部署": "DEPARTMENT"}}),
    )
    question = "対象テーブル：部署\n抽出項目：\n抽出条件：部署名 = '管理部門'"

    rewritten = service.rewrite(RewriteRequest(question=question, profile_id="default"))

    assert fake.calls == []
    assert rewritten.source == "deterministic"
    assert rewritten.rewritten_question == f"{question}（部署=DEPARTMENT）"


def test_reverse_deep_uses_enterprise_ai_and_falls_back_on_invalid_json() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    request = ReverseSqlRequest(sql="SELECT TOTAL_AMOUNT FROM INVOICES")
    fake = FakeEnterpriseAiClient(
        '{"question":"請求金額を確認したい",'
        '"explanation":"INVOICES から TOTAL_AMOUNT を取得します。",'
        '"logical_structure":"SQL 論理構造",'
        '"logical_steps":["INVOICES を参照","TOTAL_AMOUNT を選択"]}'
    )
    fake.responses = fake.responses * 3
    cast(Any, service)._enterprise_ai_client = fake

    reversed_sql = service.reverse_sql_deep(request)

    assert reversed_sql.source == "enterprise_ai"
    assert reversed_sql.question == "請求金額を確認したい"
    assert reversed_sql.logical_structure == "SQL 論理構造"
    assert reversed_sql.logical_steps == ["INVOICES を参照", "TOTAL_AMOUNT を選択"]
    assert fake.calls
    assert len(fake.calls) == 3
    assert "100% of SQL information" in fake.calls[0]["system_prompt"]
    assert "情報の完全性を最優先" in fake.calls[2]["system_prompt"]

    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient("not json")

    fallback = service.reverse_sql_deep(request)

    assert fallback.source == "deterministic"
    assert fallback.warnings


def test_reverse_logical_steps_include_group_order_and_limit() -> None:
    """決定論の処理手順は 条件/結合/集計 に加え グループ化/並び替え/件数制限 も含む。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    reversed_sql = service.reverse_sql(
        ReverseSqlRequest(
            sql=(
                "SELECT DEPARTMENT_ID, COUNT(*) FROM EMPLOYEE "
                "WHERE SALARY > 1000 GROUP BY DEPARTMENT_ID "
                "ORDER BY DEPARTMENT_ID DESC FETCH FIRST 2 ROWS ONLY"
            )
        )
    )

    steps = reversed_sql.logical_steps or []
    assert any(step.startswith("条件: ") for step in steps)
    assert any(step.startswith("集計: COUNT") for step in steps)
    assert any(step.startswith("グループ化: ") for step in steps)
    assert any(step.startswith("並び替え: ") for step in steps)
    assert "件数制限: 上位2件" in steps


def test_reverse_logical_step_details_pair_business_and_technical() -> None:
    """処理手順は業務者向け(business)と技術者向け(technical)を併記する。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    reversed_sql = service.reverse_sql(
        ReverseSqlRequest(
            sql=(
                "SELECT DEPARTMENT_ID, COUNT(*) FROM EMPLOYEE "
                "WHERE SALARY > 1000 GROUP BY DEPARTMENT_ID "
                "ORDER BY DEPARTMENT_ID DESC FETCH FIRST 2 ROWS ONLY"
            )
        )
    )

    details = reversed_sql.logical_step_details
    assert [detail.kind for detail in details] == [
        "summary",
        "filter",
        "aggregation",
        "group_by",
        "order_by",
        "limit",
    ]
    # technical は従来の logical_steps 文字列そのまま(技術者向けの情報量を落とさない)。
    assert [detail.technical for detail in details] == reversed_sql.logical_steps
    business = {detail.kind: detail.business for detail in details}
    assert business["filter"] == "SALARYが1000より大きい行に絞り込みます"
    assert business["aggregation"] == "件数を数えます"
    assert business["group_by"] == "DEPARTMENT_IDごとに集計します"
    assert business["order_by"] == "DEPARTMENT_IDの降順で並べ替えます"
    assert business["limit"] == "先頭 2 件だけ取り出します"
    # 業務者向け文には SQL 記法(引用符・句キーワード)を残さない。
    for detail in details:
        assert '"' not in detail.business
        assert "GROUP BY" not in detail.business
    # 説明文も「業務者向け + SQL 構造」の併記にする。
    assert reversed_sql.explanation.startswith("EMPLOYEEを対象に、")
    assert "SQL 構造: SELECT, GROUP BY, ORDER BY" in reversed_sql.explanation


def test_reverse_logical_structure_items_pair_business_and_technical() -> None:
    """SQL 論理構造も業務者向け/技術者向けの併記項目で返す。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    reversed_sql = service.reverse_sql(
        ReverseSqlRequest(sql="SELECT TOTAL_AMOUNT FROM INVOICES WHERE STATUS = 'OPEN'")
    )

    items = {item.kind: item for item in reversed_sql.logical_structure_items}
    assert items["statement"].business == "データを取り出すだけの参照 SQL です"
    assert items["statement"].technical == "SELECT"
    assert items["operations"].business == "一覧の取得"
    assert items["filters"].business == "STATUSがOPENと一致する行に絞り込みます"
    assert items["filters"].technical == "STATUS = 'OPEN'"
    # 従来の平文 logical_structure も後方互換のため残す。
    assert reversed_sql.logical_structure.startswith("SQL 論理構造")


def test_reverse_logical_step_details_apply_glossary_to_business_text() -> None:
    """用語・同義語 ON のときは業務者向け文にも用語を反映する。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="billing-steps",
            name="請求管理",
            description="請求テーブルを扱う profile。",
            allowed_tables=["INVOICES"],
            glossary={"請求金額": "TOTAL_AMOUNT"},
        )
    )

    reversed_sql = service.reverse_sql(
        ReverseSqlRequest(
            sql="SELECT TOTAL_AMOUNT FROM INVOICES WHERE TOTAL_AMOUNT >= 1000",
            profile_id="billing-steps",
            use_glossary=True,
        )
    )

    filters = [detail for detail in reversed_sql.logical_step_details if detail.kind == "filter"]
    assert filters
    assert filters[0].business == "請求金額が1000以上の行に絞り込みます"
    assert "請求金額" in filters[0].technical


def test_reverse_glossary_replaces_sql_identifiers_without_corrupting_larger_tokens() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="profit-glossary",
            name="利益検証",
            glossary={"利益": "INVOICES.PROFIT"},
        )
    )
    profile = service.get_profile("profit-glossary")

    rewritten = cast(Any, service)._apply_reverse_glossary(
        "NET_PROFIT_RATE, GROSS_PROFIT, INVOICES.PROFIT, PROFIT",
        profile=profile,
        enabled=True,
    )

    assert rewritten == "NET_PROFIT_RATE, GROSS_PROFIT, 利益, 利益"


def test_rewrite_question_uses_original_question_for_glossary_annotations() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="rewrite-chain",
            name="注釈検証",
            glossary={"売上": "SALES_AMOUNT 列", "列": "COLUMN"},
        )
    )

    rewritten = service.rewrite_question("売上を表示", service.get_profile("rewrite-chain"))

    assert rewritten == "売上を表示（売上=SALES_AMOUNT 列）"


def test_reverse_deep_uses_profile_context_and_glossary() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.import_legacy_terms(
        filename="terms.xlsx",
        content=_single_sheet_workbook_bytes(
            "terms",
            [["TERM", "DEFINITION"], ["請求表", "INVOICES"]],
        ),
    )
    service.create_profile(
        Nl2SqlProfile(
            id="billing",
            name="請求管理",
            description="請求テーブルを扱う profile。",
            allowed_tables=["INVOICES"],
            glossary={"請求金額": "INVOICES.TOTAL_AMOUNT"},
        )
    )
    service.import_legacy_rules(
        filename="rules.xlsx",
        content=_single_sheet_workbook_bytes(
            "rules",
            [["RULE"], ["金額列には業務名を使う"]],
        ),
    )
    fake = FakeEnterpriseAiClient(
        '{"question":"請求金額を一覧で確認したい",'
        '"explanation":"請求管理 profile の文脈で逆生成しました。",'
        '"logical_structure":"SQL 論理構造\\n- SELECT: 請求金額",'
        '"logical_steps":["請求金額を選択"]}'
    )
    cast(Any, service)._enterprise_ai_client = fake

    fake.responses = fake.responses * 3
    reversed_sql = service.reverse_sql_deep(
        ReverseSqlRequest(
            sql="SELECT TOTAL_AMOUNT FROM INVOICES",
            profile_id="billing",
            use_glossary=True,
        )
    )

    assert reversed_sql.question == "請求金額を一覧で確認したい"
    assert "請求金額" in reversed_sql.logical_structure
    assert fake.calls
    assert "profile: 請求管理" in fake.calls[0]["context"]
    assert "- 請求表: INVOICES" in fake.calls[0]["context"]
    assert "- 請求金額: INVOICES.TOTAL_AMOUNT" in fake.calls[0]["context"]
    # import_legacy_rules されたルールはグローバル保存に入り sql_rules: へ注入される。
    assert "sql_rules:" in fake.calls[0]["context"]
    assert "金額列には業務名を使う" in fake.calls[0]["context"]
    assert "logical_structure" in fake.calls[0]["system_prompt"]

    deterministic = service.reverse_sql(
        ReverseSqlRequest(
            sql="SELECT TOTAL_AMOUNT FROM INVOICES", profile_id="billing", use_glossary=True
        )
    )
    assert "請求表" in deterministic.question
    assert "請求金額" in deterministic.question
    assert "INVOICES" not in deterministic.question
