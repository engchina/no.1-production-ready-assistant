from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_backend_core.llm_request_params import note_dropped_parameters
from pydantic import Field

from pr_system_settings import model as shared_model
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS
from pr_system_settings.model import (
    ENTERPRISE_AI_API_KEY_ENV,
    ENTERPRISE_AI_SECONDARY_API_KEY_ENV,
    ENTERPRISE_AI_TERTIARY_API_KEY_ENV,
    EnterpriseAiConfiguredModel,
    ModelSecretStateMixin,
    ModelSettingsStore,
    ModelSettingsTestRequest,
    build_model_router,
)


class FakeSettings(ModelSecretStateMixin):
    """製品の Settings のうちモデル設定 API が読む属性だけを持つ。"""

    model_settings_file: str = "model-settings.json"
    oci_enterprise_ai_endpoint: str = ""
    oci_enterprise_ai_project_ocid: str = ""
    oci_enterprise_ai_api_key: str = ""
    oci_enterprise_ai_secondary_endpoint: str = ""
    oci_enterprise_ai_secondary_project_ocid: str = ""
    oci_enterprise_ai_secondary_api_key: str = ""
    oci_enterprise_ai_tertiary_endpoint: str = ""
    oci_enterprise_ai_tertiary_project_ocid: str = ""
    oci_enterprise_ai_tertiary_api_key: str = ""
    oci_enterprise_ai_models: list[EnterpriseAiConfiguredModel] = Field(default_factory=list)
    oci_enterprise_ai_default_text_model: str = ""
    oci_enterprise_ai_default_vision_model: str = ""
    oci_enterprise_ai_default_judge_model: str = ""
    oci_enterprise_ai_llm_path: str = "/responses"
    oci_enterprise_ai_vlm_path: str = "/responses"
    oci_enterprise_ai_vlm_input_mode: str = "auto"
    oci_enterprise_ai_llm_payload_template: str = ""
    oci_enterprise_ai_vlm_payload_template: str = ""
    oci_enterprise_ai_llm_response_path: str = ""
    oci_enterprise_ai_vlm_response_path: str = ""
    oci_enterprise_ai_timeout_seconds: float = 600.0
    oci_enterprise_ai_max_retries: int = 3
    oci_genai_embedding_model: str = "cohere.embed-v4.0"
    oci_genai_embedding_dim: int = EMBEDDING_DIMENSIONS
    oci_genai_rerank_model: str = "cohere.rerank-v4.0-fast"


PAYLOAD: dict[str, Any] = {
    "enterprise_ai": {
        "endpoint": "https://example.invalid/openai/v1",
        "project_ocid": "ocid1.generativeaiproject.oc1..aaaa",
        "api_key": "sk-new",
        "models": [
            {"model_id": "llm-a", "display_name": "A", "vision_enabled": False},
            {"model_id": "vlm-b", "display_name": "B", "vision_enabled": True},
        ],
        "default_text_model_id": "llm-a",
        "default_vision_model_id": "vlm-b",
    },
    "generative_ai": {"embedding_model": "cohere.embed-v4.0", "rerank_model": "rr"},
}


@pytest.fixture(autouse=True)
def _no_process_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENTERPRISE_AI_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ENTERPRISE_AI_SECONDARY_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ENTERPRISE_AI_TERTIARY_API_KEY_ENV, raising=False)


def make_store(tmp_path: Path) -> ModelSettingsStore:
    return ModelSettingsStore(
        resolve_path=lambda settings: tmp_path / settings.model_settings_file,
        env_file=lambda settings: tmp_path / ".env",
    )


def make_client(settings: FakeSettings, store: ModelSettingsStore, **kwargs: Any) -> TestClient:
    async def run_test(candidate: Any, request: ModelSettingsTestRequest) -> dict[str, Any]:
        return {"key": candidate.oci_enterprise_ai_api_key, "model": request.model_id}

    def get_settings() -> FakeSettings:
        store.reload_if_changed(settings)
        return settings

    app = FastAPI()
    app.include_router(
        build_model_router(
            get_settings=get_settings,
            store=store,
            run_model_test=kwargs.get("run_model_test", run_test),
        ),
        prefix="/api/settings",
    )
    return TestClient(app)


def test_patch_saves_key_only_in_env(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    response = make_client(settings, store).patch("/api/settings/model", json=PAYLOAD)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["settings"]["enterprise_ai"]["connections"][0]["api_key"] == ""
    assert data["settings"]["enterprise_ai"]["connections"][0]["has_api_key"] is True
    assert data["secret_source"] == "environment"
    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert document["version"] == 6
    assert "api_key" not in document["enterprise_ai"]
    assert "sk-new" not in json.dumps(document)
    # 製品固有の設定は model-settings.json に書かない（#1701）。
    assert set(document) == {"version", "enterprise_ai", "generative_ai"}
    assert stat.S_IMODE((tmp_path / "model-settings.json").stat().st_mode) == 0o600
    assert dotenv_values(tmp_path / ".env")[ENTERPRISE_AI_API_KEY_ENV] == "sk-new"
    assert settings.oci_enterprise_ai_api_key == "sk-new"
    assert settings.oci_enterprise_ai_default_vision_model == "vlm-b"
    assert document["enterprise_ai"]["default_text_model_id"] == "llm-a"
    assert document["enterprise_ai"]["default_vision_model_id"] == "vlm-b"
    assert "default_model_id" not in document["enterprise_ai"]


def test_blank_key_keeps_current_and_clear_removes_it(tmp_path: Path) -> None:
    settings = FakeSettings(oci_enterprise_ai_api_key="sk-old")
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    keep = {**PAYLOAD, "enterprise_ai": {**PAYLOAD["enterprise_ai"], "api_key": ""}}

    assert client.patch("/api/settings/model", json=keep).status_code == 200
    assert settings.oci_enterprise_ai_api_key == "sk-old"

    clear = {**PAYLOAD, "enterprise_ai": {**keep["enterprise_ai"], "clear_api_key": True}}
    data = client.patch("/api/settings/model", json=clear).json()["data"]
    assert data["settings"]["enterprise_ai"]["connections"][0]["has_api_key"] is False
    assert data["secret_source"] == "missing"
    assert ENTERPRISE_AI_API_KEY_ENV not in dotenv_values(tmp_path / ".env")


def test_legacy_json_key_is_used_until_saved_to_env(tmp_path: Path) -> None:
    legacy = {"version": 2, "enterprise_ai": {"api_key": "sk-legacy", "models": []}}
    (tmp_path / "model-settings.json").write_text(json.dumps(legacy), encoding="utf-8")
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    assert settings.oci_enterprise_ai_api_key == "sk-legacy"
    assert settings.model_secret_source == "legacy_json"
    assert settings.legacy_model_secret_detected is True

    keep = {**PAYLOAD, "enterprise_ai": {**PAYLOAD["enterprise_ai"], "api_key": ""}}
    data = make_client(settings, store).patch("/api/settings/model", json=keep).json()["data"]

    assert data["secret_source"] == "environment"
    assert data["legacy_secret_detected"] is False
    assert dotenv_values(tmp_path / ".env")[ENTERPRISE_AI_API_KEY_ENV] == "sk-legacy"
    assert "sk-legacy" not in (tmp_path / "model-settings.json").read_text(encoding="utf-8")


def test_environment_key_wins_over_legacy_json(tmp_path: Path) -> None:
    legacy = {"version": 1, "enterprise_ai": {"api_key": "sk-legacy"}}
    (tmp_path / "model-settings.json").write_text(json.dumps(legacy), encoding="utf-8")
    settings = FakeSettings(oci_enterprise_ai_api_key="sk-env")
    make_store(tmp_path).load(settings)
    assert settings.oci_enterprise_ai_api_key == "sk-env"
    assert settings.model_secret_source == "environment"
    assert settings.legacy_model_secret_detected is True


def test_json_failure_restores_env_and_keeps_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(f"{ENTERPRISE_AI_API_KEY_ENV}=sk-old\n", encoding="utf-8")
    settings = FakeSettings(oci_enterprise_ai_api_key="sk-old")
    store = make_store(tmp_path)
    store.load(settings)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_document", fail)
    response = make_client(settings, store).patch("/api/settings/model", json=PAYLOAD)

    assert response.status_code == 500
    assert dotenv_values(tmp_path / ".env")[ENTERPRISE_AI_API_KEY_ENV] == "sk-old"
    assert settings.oci_enterprise_ai_api_key == "sk-old"
    assert settings.oci_enterprise_ai_endpoint == ""


def test_other_worker_picks_up_saved_key_and_settings(tmp_path: Path) -> None:
    store_a, store_b = make_store(tmp_path), make_store(tmp_path)
    worker_a, worker_b = FakeSettings(), FakeSettings()
    store_a.load(worker_a)
    store_b.load(worker_b)

    make_client(worker_a, store_a).patch("/api/settings/model", json=PAYLOAD)
    data = make_client(worker_b, store_b).get("/api/settings/model").json()["data"]

    assert worker_b.oci_enterprise_ai_api_key == "sk-new"
    assert data["settings"]["enterprise_ai"]["default_text_model_id"] == "llm-a"
    assert data["settings"]["enterprise_ai"]["default_vision_model_id"] == "vlm-b"


def test_get_without_models_returns_one_blank_row(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    models = (
        make_client(settings, store)
        .get("/api/settings/model")
        .json()["data"]["settings"]["enterprise_ai"]["models"]
    )
    assert models == [
        {
            "model_id": "",
            "display_name": "",
            "vision_enabled": False,
            "connection_id": "primary",
            "reasoning_effort": "low",
            "max_input_tokens": 128000,
            "max_output_tokens": 32768,
        }
    ]


def test_model_test_uses_unsaved_payload_and_masks_secret(tmp_path: Path) -> None:
    settings = FakeSettings(oci_enterprise_ai_api_key="sk-saved")
    store = make_store(tmp_path)
    store.load(settings)
    request = {"settings": PAYLOAD, "target_type": "enterprise_vision", "model_id": "llm-a"}

    ok = make_client(settings, store).post("/api/settings/model/test", json=request).json()["data"]
    assert ok["status"] == "success"
    assert ok["details"] == {"key": "sk-new", "model": "llm-a"}

    async def fail(candidate: Any, _request: ModelSettingsTestRequest) -> dict[str, Any]:
        assert candidate.oci_enterprise_ai_default_vision_model == "llm-a"
        assert shared_model.enterprise_ai_vision_model_id(candidate) == "llm-a"
        raise RuntimeError(f"401 Unauthorized for {candidate.oci_enterprise_ai_api_key}")

    failed = (
        make_client(settings, store, run_model_test=fail)
        .post("/api/settings/model/test", json=request)
        .json()["data"]
    )
    assert failed["status"] == "failed"
    assert "sk-new" not in failed["raw_error"]
    assert "<secret>" in failed["raw_error"]
    assert any("認証エラー" in tip for tip in failed["troubleshooting"])
    assert settings.oci_enterprise_ai_endpoint == ""


def test_model_test_requires_model_id(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    request = {"settings": PAYLOAD, "target_type": "rerank", "model_id": " "}
    data = make_client(settings, store).post("/api/settings/model/test", json=request).json()
    assert data["data"]["status"] == "failed"
    assert data["data"]["raw_error"] == "テストするモデル ID を入力してください。"


def test_model_test_reports_dropped_parameters(tmp_path: Path) -> None:
    """受け付けない引数を外して送ったら、詳細と成功の文に出す（#3539。worker thread も）。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    request = {"settings": PAYLOAD, "target_type": "enterprise_text", "model_id": "llm-a"}

    async def dropped(_candidate: Any, _request: ModelSettingsTestRequest) -> dict[str, Any]:
        await asyncio.to_thread(note_dropped_parameters, ("temperature",))
        note_dropped_parameters(("top_p",))
        return {"response_chars": 2, "surface": "llm"}

    data = (
        make_client(settings, store, run_model_test=dropped)
        .post("/api/settings/model/test", json=request)
        .json()["data"]
    )
    assert data["status"] == "success"
    assert data["details"] == {
        "response_chars": 2,
        "surface": "llm",
        "dropped_parameters": "temperature, top_p",
    }
    assert "受け付けない引数（temperature, top_p）は外して送りました" in data["message"]

    plain = make_client(settings, store).post("/api/settings/model/test", json=request).json()
    assert "dropped_parameters" not in plain["data"]["details"]
    assert "外して送りました" not in plain["data"]["message"]


def test_saved_key_overrides_process_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """画面で保存した key はプロセスの環境変数より優先し、削除すると環境変数へ戻る。"""
    monkeypatch.setenv(ENTERPRISE_AI_API_KEY_ENV, "sk-process")
    settings = FakeSettings(oci_enterprise_ai_api_key="sk-process")
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)

    client.patch("/api/settings/model", json=PAYLOAD)
    data = client.get("/api/settings/model").json()["data"]
    assert settings.oci_enterprise_ai_api_key == "sk-new"
    assert data["settings"]["enterprise_ai"]["connections"][0]["has_api_key"] is True

    clear = {**PAYLOAD, "enterprise_ai": {**PAYLOAD["enterprise_ai"], "clear_api_key": True}}
    cleared = client.patch("/api/settings/model", json=clear).json()["data"]
    assert cleared["settings"]["enterprise_ai"]["connections"][0]["has_api_key"] is True
    client.get("/api/settings/model")
    assert settings.oci_enterprise_ai_api_key == "sk-process"


def test_seed_is_not_kept(tmp_path: Path) -> None:
    """seed は持たない（#3529）。保存済みの JSON・送られた payload の `seed` は読み捨てる。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 6,
                "enterprise_ai": {
                    "models": [{"model_id": "llm-a", "display_name": "A"}],
                    "default_text_model_id": "llm-a",
                    "seed": 42,
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)

    assert (
        "seed" not in client.get("/api/settings/model").json()["data"]["settings"]["enterprise_ai"]
    )
    payload = {**PAYLOAD, "enterprise_ai": {**PAYLOAD["enterprise_ai"], "seed": 7}}
    saved = client.patch("/api/settings/model", json=payload)

    assert saved.status_code == 200
    assert "seed" not in saved.json()["data"]["settings"]["enterprise_ai"]
    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert "seed" not in document["enterprise_ai"]


def test_broken_json_is_reported(tmp_path: Path) -> None:
    (tmp_path / "model-settings.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="モデル設定ファイルを読み込めません"):
        make_store(tmp_path).load(FakeSettings())


def test_catalog_falls_back_to_default_text_and_vision_ids() -> None:
    settings = FakeSettings(
        oci_enterprise_ai_default_text_model="llm", oci_enterprise_ai_default_vision_model="vlm"
    )
    assert [
        (m.model_id, m.vision_enabled) for m in shared_model.enterprise_ai_model_catalog(settings)
    ] == [("llm", False), ("vlm", True)]
    assert shared_model.enterprise_ai_default_model_id(settings) == "llm"
    assert shared_model.enterprise_ai_vision_model_id(settings) == "vlm"


# --------------------------------------------------------------------------- #499 既定のモデル 2 つ

MODELS = [
    EnterpriseAiConfiguredModel(model_id="llm-a", display_name="A", vision_enabled=False),
    EnterpriseAiConfiguredModel(model_id="vlm-b", display_name="B", vision_enabled=True),
]


def test_resolution_uses_text_model_and_falls_back_to_vision_model() -> None:
    settings = FakeSettings(
        oci_enterprise_ai_models=MODELS,
        oci_enterprise_ai_default_text_model="llm-a",
        oci_enterprise_ai_default_vision_model="vlm-b",
    )
    assert shared_model.enterprise_ai_default_model_id(settings) == "llm-a"
    assert shared_model.enterprise_ai_vision_model_id(settings) == "vlm-b"

    settings.oci_enterprise_ai_default_text_model = ""
    # 画面・API では必須だが（#566）、未設定の既存環境では画像を扱わない呼び出しも
    # 既定の画像対応モデルを使う（安全策）。
    assert shared_model.enterprise_ai_default_model_id(settings) == "vlm-b"
    assert shared_model.enterprise_ai_vision_model_id(settings) == "vlm-b"


def test_vision_model_is_derived_when_not_set_explicitly() -> None:
    """`.env` で登録モデルだけを書いた環境は、従来どおり Vision 対応のモデルを使う。"""
    settings = FakeSettings(oci_enterprise_ai_models=MODELS)
    assert shared_model.enterprise_ai_vision_model_id(settings) == "vlm-b"
    assert shared_model.enterprise_ai_default_model_id(settings) == "vlm-b"


@pytest.mark.parametrize(
    ("update", "field", "message"),
    [
        (
            {"models": [{"model_id": "llm-a", "vision_enabled": False}]},
            "default_vision_model_id",
            "画像入力に対応したモデルがありません",
        ),
        ({"default_vision_model_id": ""}, "default_vision_model_id", "選択してください"),
        ({"default_vision_model_id": "gone"}, "default_vision_model_id", "登録モデルにありません"),
        (
            {"default_vision_model_id": "llm-a"},
            "default_vision_model_id",
            "画像入力に対応していません",
        ),
        ({"default_text_model_id": ""}, "default_text_model_id", "既定のテキストモデルを選択"),
        ({"default_text_model_id": "gone"}, "default_text_model_id", "登録モデルにありません"),
    ],
    ids=[
        "no-vision-model",
        "vision-empty",
        "vision-removed",
        "vision-not-capable",
        "text-empty",
        "text-removed",
    ],
)
def test_patch_rejects_invalid_default_models(
    tmp_path: Path, update: dict[str, Any], field: str, message: str
) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    enterprise = {**PAYLOAD["enterprise_ai"], **update}
    errors = shared_model.validate_default_models(
        shared_model.EnterpriseAiModelSettings.model_validate(enterprise)
    )
    assert [error.field for error in errors] == [field]

    response = make_client(settings, store).patch(
        "/api/settings/model", json={**PAYLOAD, "enterprise_ai": enterprise}
    )

    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert not (tmp_path / "model-settings.json").exists()


def test_default_model_errors_follow_screen_order() -> None:
    """2 つとも空なら、画面の並び順（テキスト → Vision）でエラーを返す（#566）。"""
    enterprise = {
        **PAYLOAD["enterprise_ai"],
        "default_text_model_id": "",
        "default_vision_model_id": "",
    }
    errors = shared_model.validate_default_models(
        shared_model.EnterpriseAiModelSettings.model_validate(enterprise)
    )
    assert [error.field for error in errors] == ["default_text_model_id", "default_vision_model_id"]


# --------------------------------------------------------------------------- #3574 既定の判定モデル


@pytest.mark.parametrize(
    ("judge", "answer_model", "expected"),
    [
        ("", "llm-c", "llm-a"),
        (shared_model.ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER, "llm-c", "llm-c"),
        (shared_model.ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER, None, "llm-a"),
        (shared_model.ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER, " ", "llm-a"),
        ("vlm-b", "llm-c", "vlm-b"),
    ],
    ids=["default-text", "same-as-answer", "same-without-answer", "same-blank-answer", "fixed"],
)
def test_judge_model_resolution(judge: str, answer_model: str | None, expected: str) -> None:
    """空（既定）は既定のテキストモデル、「回答と同じモデル」は回答のモデル、ほかはそのモデル。"""
    settings = FakeSettings(
        oci_enterprise_ai_models=MODELS,
        oci_enterprise_ai_default_text_model="llm-a",
        oci_enterprise_ai_default_vision_model="vlm-b",
        oci_enterprise_ai_default_judge_model=judge,
    )
    assert shared_model.enterprise_ai_judge_model_id(settings, answer_model) == expected


@pytest.mark.parametrize(
    "judge", ["", shared_model.ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER, "llm-a", "vlm-b"]
)
def test_judge_model_accepts_default_same_as_answer_and_registered(judge: str) -> None:
    enterprise = {**PAYLOAD["enterprise_ai"], "default_judge_model_id": judge}
    assert (
        shared_model.validate_default_models(
            shared_model.EnterpriseAiModelSettings.model_validate(enterprise)
        )
        == []
    )


def test_unregistered_judge_model_is_rejected_after_other_defaults(tmp_path: Path) -> None:
    """登録モデルに無い判定モデルはエラー。並びは画面と同じ（テキスト → Vision → 判定）。"""
    enterprise = {
        **PAYLOAD["enterprise_ai"],
        "default_text_model_id": "",
        "default_judge_model_id": "gone",
    }
    errors = shared_model.validate_default_models(
        shared_model.EnterpriseAiModelSettings.model_validate(enterprise)
    )
    assert [error.field for error in errors] == ["default_text_model_id", "default_judge_model_id"]

    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    response = make_client(settings, store).patch(
        "/api/settings/model",
        json={
            **PAYLOAD,
            "enterprise_ai": {**PAYLOAD["enterprise_ai"], "default_judge_model_id": "gone"},
        },
    )
    assert response.status_code == 422
    assert "既定の判定モデル「gone」は登録モデルにありません" in response.json()["detail"]


def test_judge_model_is_saved_and_reloaded(tmp_path: Path) -> None:
    """既定の判定モデルは model-settings.json に保存し、読み直しても同じ値になる。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    same = shared_model.ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER

    response = client.patch(
        "/api/settings/model",
        json={
            **PAYLOAD,
            "enterprise_ai": {**PAYLOAD["enterprise_ai"], "default_judge_model_id": same},
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["settings"]["enterprise_ai"]["default_judge_model_id"] == same
    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert document["enterprise_ai"]["default_judge_model_id"] == same
    reloaded = FakeSettings()
    store.load(reloaded)
    assert reloaded.oci_enterprise_ai_default_judge_model == same


def test_document_without_judge_model_defaults_to_default_text_model(tmp_path: Path) -> None:
    """既定の判定モデルの無い JSON は空（既定のテキストモデルで判定）として読む。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps({"version": 6, "enterprise_ai": PAYLOAD["enterprise_ai"]}), encoding="utf-8"
    )
    settings = FakeSettings(oci_enterprise_ai_default_judge_model="llm-a")
    make_store(tmp_path).load(settings)
    assert settings.oci_enterprise_ai_default_judge_model == ""
    assert shared_model.enterprise_ai_judge_model_id(settings, "vlm-b") == "llm-a"


def test_patch_accepts_vision_capable_text_model(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)

    vision_text = {**PAYLOAD["enterprise_ai"], "default_text_model_id": "vlm-b"}
    response = client.patch("/api/settings/model", json={**PAYLOAD, "enterprise_ai": vision_text})
    assert response.status_code == 200
    assert shared_model.enterprise_ai_default_model_id(settings) == "vlm-b"


def test_patch_without_models_needs_no_vision_model(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    enterprise = {
        **PAYLOAD["enterprise_ai"],
        "models": [],
        "default_text_model_id": "",
        "default_vision_model_id": "",
    }
    response = make_client(settings, store).patch(
        "/api/settings/model", json={**PAYLOAD, "enterprise_ai": enterprise}
    )
    assert response.status_code == 200


def test_patch_rejects_duplicate_model_ids(tmp_path: Path) -> None:
    """同じモデル ID の 2 行目は使われない（接続もテストも先頭の行）ので保存しない（#1035）。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    models = [
        *PAYLOAD["enterprise_ai"]["models"],
        {"model_id": " llm-a ", "display_name": "A2", "vision_enabled": False},
    ]
    enterprise = {**PAYLOAD["enterprise_ai"], "models": models}
    errors = shared_model.validate_model_ids(
        shared_model.EnterpriseAiModelSettings.model_validate(enterprise)
    )
    assert [error.field for error in errors] == ["models.2.model_id"]

    response = make_client(settings, store).patch(
        "/api/settings/model", json={**PAYLOAD, "enterprise_ai": enterprise}
    )

    assert response.status_code == 422
    assert "モデル ID「llm-a」はすでに登録されています。" in response.json()["detail"]
    assert not (tmp_path / "model-settings.json").exists()


def test_saving_other_sections_is_not_blocked_by_saved_duplicate_model_ids(
    tmp_path: Path,
) -> None:
    """保存済みの登録モデルに重複があっても、登録モデルを変えない保存は止めない（#1035）。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 3,
                "enterprise_ai": {
                    "models": [
                        {"model_id": "vlm-b", "vision_enabled": True},
                        {"model_id": "vlm-b", "vision_enabled": True},
                    ],
                    "default_text_model_id": "vlm-b",
                    "default_vision_model_id": "vlm-b",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    current = client.get("/api/settings/model").json()["data"]["settings"]

    current["generative_ai"]["rerank_model"] = "rr-changed"
    response = client.patch("/api/settings/model", json=current)

    assert response.status_code == 200
    assert settings.oci_genai_rerank_model == "rr-changed"


def test_saving_other_sections_is_not_blocked_by_saved_invalid_defaults(tmp_path: Path) -> None:
    """保存済みの状態に Vision 対応のモデルがなくても、接続情報だけの保存は止めない。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 3,
                "enterprise_ai": {
                    "models": [{"model_id": "llm-a", "vision_enabled": False}],
                    "default_model_id": "llm-a",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    current = client.get("/api/settings/model").json()["data"]["settings"]
    assert current["enterprise_ai"]["default_vision_model_id"] == ""

    current["enterprise_ai"]["connections"][0]["endpoint"] = "https://changed.invalid/openai/v1"
    response = client.patch("/api/settings/model", json=current)

    assert response.status_code == 200
    assert settings.oci_enterprise_ai_endpoint == "https://changed.invalid/openai/v1"


@pytest.mark.parametrize(
    ("enterprise", "expected"),
    [
        # 既定モデルが Vision 対応ならそれを Vision にも使う（従来の導出）。
        (
            {
                "models": [
                    {"model_id": "llm-a", "vision_enabled": False},
                    {"model_id": "vlm-b", "vision_enabled": True},
                    {"model_id": "vlm-c", "vision_enabled": True},
                ],
                "default_model_id": "vlm-c",
            },
            ("vlm-c", "vlm-c"),
        ),
        # 既定モデルが Vision 非対応なら、一覧で最初の Vision 対応モデル。
        (
            {
                "models": [
                    {"model_id": "llm-a", "vision_enabled": False},
                    {"model_id": "vlm-b", "vision_enabled": True},
                ],
                "default_model_id": "llm-a",
            },
            ("llm-a", "vlm-b"),
        ),
        # 既定モデルが空なら先頭の登録モデル。Vision 対応がなければ Vision は空（失敗させない）。
        (
            {"models": [{"model_id": "llm-a", "vision_enabled": False}], "default_model_id": ""},
            ("llm-a", ""),
        ),
    ],
    ids=["default-is-vision", "first-vision", "no-vision"],
)
def test_legacy_json_default_model_id_is_backfilled(
    tmp_path: Path, enterprise: dict[str, Any], expected: tuple[str, str]
) -> None:
    (tmp_path / "model-settings.json").write_text(
        json.dumps({"version": 3, "enterprise_ai": enterprise}), encoding="utf-8"
    )
    settings = FakeSettings()
    make_store(tmp_path).load(settings)

    assert (
        settings.oci_enterprise_ai_default_text_model,
        settings.oci_enterprise_ai_default_vision_model,
    ) == expected
    assert shared_model.enterprise_ai_default_model_id(settings) == expected[0]
    assert shared_model.enterprise_ai_vision_model_id(settings) == expected[1]


def test_saving_legacy_json_writes_new_keys(tmp_path: Path) -> None:
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 3,
                "enterprise_ai": {
                    "models": [
                        {"model_id": "llm-a", "vision_enabled": False},
                        {"model_id": "vlm-b", "vision_enabled": True},
                    ],
                    "default_model_id": "llm-a",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    current = client.get("/api/settings/model").json()["data"]["settings"]

    assert client.patch("/api/settings/model", json=current).status_code == 200

    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert document["enterprise_ai"]["default_text_model_id"] == "llm-a"
    assert document["enterprise_ai"]["default_vision_model_id"] == "vlm-b"
    assert "default_model_id" not in document["enterprise_ai"]


def test_saved_vision_model_that_is_not_vision_capable_is_replaced_on_load(
    tmp_path: Path,
) -> None:
    """手で編集した JSON の不整合でも読み込みは失敗させず、Vision 対応のモデルへ置き換える。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 3,
                "enterprise_ai": {
                    "models": [
                        {"model_id": "llm-a", "vision_enabled": False},
                        {"model_id": "vlm-b", "vision_enabled": True},
                    ],
                    "default_text_model_id": "",
                    "default_vision_model_id": "llm-a",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    make_store(tmp_path).load(settings)

    assert settings.oci_enterprise_ai_default_text_model == ""
    assert settings.oci_enterprise_ai_default_vision_model == "vlm-b"


def test_key_saved_in_env_file_is_used_on_startup(tmp_path: Path) -> None:
    """コンテナで `.env` を volume に置く場合も、起動時に key を読む。"""
    (tmp_path / ".env").write_text(f"{ENTERPRISE_AI_API_KEY_ENV}=sk-volume\n", encoding="utf-8")
    settings = FakeSettings()
    make_store(tmp_path).load(settings)
    assert settings.oci_enterprise_ai_api_key == "sk-volume"
    assert settings.model_secret_source == "environment"


def test_shared_document_keeps_unknown_keys_and_loads_without_product_sections(
    tmp_path: Path,
) -> None:
    """model-settings.json は3製品で共有する（#211）。

    モデルの設定だけを読み書きし、知らない key は消さず、無くても読み込みを失敗させない（#1701）。
    """
    (tmp_path / "model-settings.json").write_text(
        json.dumps({"version": 4, "enterprise_ai": {"models": []}, "other": {"keep": True}}),
        encoding="utf-8",
    )
    store = ModelSettingsStore(
        resolve_path=lambda settings: tmp_path / settings.model_settings_file,
        env_file=lambda settings: tmp_path / "platform.env",
    )
    settings = FakeSettings()
    store.load(settings)

    response = make_client(settings, store).patch("/api/settings/model", json=PAYLOAD)

    assert response.status_code == 200
    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert document["other"] == {"keep": True}
    platform_env = (tmp_path / "platform.env").read_text(encoding="utf-8")
    assert f"{ENTERPRISE_AI_API_KEY_ENV}=sk-new" in platform_env


def test_saved_embedding_dim_is_ignored_and_code_constant_is_used(tmp_path: Path) -> None:
    """#46 より前に保存した次元（1536）があっても読み込みを失敗させず、コードの次元を使う。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 5,
                "enterprise_ai": {"models": []},
                "generative_ai": {"embedding_model": "text-embedding-v4", "embedding_dim": 1536},
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    make_store(tmp_path).load(settings)

    assert settings.oci_genai_embedding_model == "text-embedding-v4"
    assert settings.oci_genai_embedding_dim == EMBEDDING_DIMENSIONS


def test_embedding_dim_accepts_only_the_code_constant() -> None:
    """次元は固定で、画面・API から別の値を保存させない（Oracle の列が固定の次元のため。#46）。"""
    assert shared_model.GenerativeAiModelSettings().embedding_dim == EMBEDDING_DIMENSIONS
    with pytest.raises(ValueError):
        shared_model.GenerativeAiModelSettings(embedding_dim=EMBEDDING_DIMENSIONS + 512)
