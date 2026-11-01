"""モデルの API の接続（プライマリ / セカンダリ / ターシャリ）を持ち、モデルごとに選ぶ。

#533 / #542 / #786。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from test_model import FakeSettings, make_client, make_store

from pr_system_settings import model as shared_model
from pr_system_settings.model import (
    ENTERPRISE_AI_API_KEY_ENV,
    ENTERPRISE_AI_SECONDARY_API_KEY_ENV,
    ENTERPRISE_AI_TERTIARY_API_KEY_ENV,
    MAX_ENTERPRISE_AI_CONNECTIONS,
    EnterpriseAiConfiguredModel,
    EnterpriseAiModelNotRegisteredError,
    EnterpriseAiModelSettings,
    ModelSettingsTestRequest,
    connection_label,
    enterprise_ai_connection_for_model,
    enterprise_ai_connections,
    validate_enterprise_ai_connections,
)

PRIMARY_ENDPOINT = "https://primary.invalid/openai/v1"
SECONDARY_ENDPOINT = "https://secondary.invalid/openai/v1"
TERTIARY_ENDPOINT = "https://api.openai.com/v1"


@pytest.fixture(autouse=True)
def _no_process_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENTERPRISE_AI_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ENTERPRISE_AI_SECONDARY_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ENTERPRISE_AI_TERTIARY_API_KEY_ENV, raising=False)


def two_connection_payload(**secondary: Any) -> dict[str, Any]:
    return {
        "enterprise_ai": {
            "connections": [
                {
                    "connection_id": "primary",
                    "endpoint": PRIMARY_ENDPOINT,
                    "project_id": "ocid1.project.primary",
                    "api_key": "sk-primary",
                },
                {
                    "connection_id": "secondary",
                    "endpoint": SECONDARY_ENDPOINT,
                    "project_id": "ocid1.project.secondary",
                    "api_key": "sk-secondary",
                    **secondary,
                },
            ],
            "models": [
                {"model_id": "llm-a", "display_name": "A", "vision_enabled": False},
                {
                    "model_id": "vlm-b",
                    "display_name": "B",
                    "vision_enabled": True,
                    "connection_id": "secondary",
                },
            ],
            "default_text_model_id": "llm-a",
            "default_vision_model_id": "vlm-b",
            "default_embedding_model_id": "cohere.embed-v4.0",
            "default_rerank_model_id": "rr",
        },
    }


def test_max_connections_is_three() -> None:
    assert MAX_ENTERPRISE_AI_CONNECTIONS == 3
    with pytest.raises(ValueError):
        EnterpriseAiModelSettings.model_validate(
            {"connections": [{"connection_id": "primary"}] * 4}
        )


def test_connections_must_start_with_primary_without_duplicates() -> None:
    for connections in (
        [{"connection_id": "secondary"}],
        [{"connection_id": "tertiary"}],
        [{"connection_id": "primary"}, {"connection_id": "primary"}],
        [
            {"connection_id": "primary"},
            {"connection_id": "tertiary"},
            {"connection_id": "secondary"},
        ],
        [{"connection_id": "primary"}, {"connection_id": "quaternary"}],
    ):
        with pytest.raises(ValueError):
            EnterpriseAiModelSettings.model_validate({"connections": connections})


@pytest.mark.parametrize(
    "ids",
    [
        ["primary"],
        ["primary", "secondary"],
        ["primary", "tertiary"],
        ["primary", "secondary", "tertiary"],
    ],
    ids=["primary", "primary-secondary", "primary-tertiary", "all"],
)
def test_connections_allow_any_subset_with_primary_in_order(ids: list[str]) -> None:
    """セカンダリ接続を置かずにターシャリ接続だけを置いてもよい（#786）。"""
    settings = EnterpriseAiModelSettings.model_validate(
        {"connections": [{"connection_id": connection_id} for connection_id in ids]}
    )
    assert [c.connection_id for c in settings.connections] == ids


def test_legacy_payload_is_read_as_primary_connection() -> None:
    settings = EnterpriseAiModelSettings.model_validate(
        {"endpoint": PRIMARY_ENDPOINT, "project_id": "ocid1", "api_key": "sk", "models": []}
    )
    assert [c.connection_id for c in settings.connections] == ["primary"]
    assert settings.primary_connection.endpoint == PRIMARY_ENDPOINT
    assert settings.primary_connection.api_key == "sk"


def test_model_without_connection_uses_primary() -> None:
    assert EnterpriseAiConfiguredModel(model_id="m").connection_id == "primary"
    assert EnterpriseAiConfiguredModel(model_id="m", connection_id=" ").connection_id == "primary"


def test_save_two_connections_keeps_keys_only_in_env(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    response = make_client(settings, store).patch(
        "/api/settings/model", json=two_connection_payload()
    )

    assert response.status_code == 200, response.text
    enterprise = response.json()["data"]["settings"]["enterprise_ai"]
    assert [c["connection_id"] for c in enterprise["connections"]] == ["primary", "secondary"]
    # 接続の表示名は持たない（#542）
    assert all("display_name" not in c for c in enterprise["connections"])
    assert all(c["api_key"] == "" and c["has_api_key"] for c in enterprise["connections"])
    assert [m["connection_id"] for m in enterprise["models"]] == ["primary", "secondary"]
    assert "sk-primary" not in response.text and "sk-secondary" not in response.text

    raw = (tmp_path / "model-settings.json").read_text(encoding="utf-8")
    assert "sk-primary" not in raw and "sk-secondary" not in raw
    document = json.loads(raw)
    assert document["enterprise_ai"]["connections"] == [
        {
            "connection_id": "primary",
            "endpoint": PRIMARY_ENDPOINT,
            "project_id": "ocid1.project.primary",
        },
        {
            "connection_id": "secondary",
            "endpoint": SECONDARY_ENDPOINT,
            "project_id": "ocid1.project.secondary",
        },
    ]
    assert "endpoint" not in document["enterprise_ai"]
    assert document["enterprise_ai"]["models"][1]["connection_id"] == "secondary"
    env = dotenv_values(tmp_path / ".env")
    assert env[ENTERPRISE_AI_API_KEY_ENV] == "sk-primary"
    assert env[ENTERPRISE_AI_SECONDARY_API_KEY_ENV] == "sk-secondary"
    assert settings.enterprise_ai_secondary_endpoint == SECONDARY_ENDPOINT
    assert settings.enterprise_ai_secondary_api_key == "sk-secondary"


def test_resolver_returns_connection_of_each_model(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    make_client(settings, store).patch("/api/settings/model", json=two_connection_payload())

    text = enterprise_ai_connection_for_model(settings, "llm-a")
    vision = enterprise_ai_connection_for_model(settings, "vlm-b")
    assert (text.connection_id, text.endpoint, text.api_key, text.project_id) == (
        "primary",
        PRIMARY_ENDPOINT,
        "sk-primary",
        "ocid1.project.primary",
    )
    assert (vision.connection_id, vision.endpoint, vision.api_key, vision.project_id) == (
        "secondary",
        SECONDARY_ENDPOINT,
        "sk-secondary",
        "ocid1.project.secondary",
    )
    # 登録モデルにない ID はプライマリ接続へ切り替えず、明示の失敗にする（#3240）。
    with pytest.raises(EnterpriseAiModelNotRegisteredError) as raised:
        enterprise_ai_connection_for_model(settings, "unknown")
    assert raised.value.model_id == "unknown"
    assert raised.value.code == "enterprise_ai_model_not_registered"
    assert "unknown" in str(raised.value)
    assert "sk-" not in str(raised.value)
    # API key は repr に出さない
    assert "sk-secondary" not in repr(vision)


def test_resolver_uses_the_default_model_for_an_empty_id(tmp_path: Path) -> None:
    """空の ID は既定のテキストモデルの接続（既定がセカンダリ接続ならセカンダリ。#3240）。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    payload = two_connection_payload()
    payload["enterprise_ai"]["default_text_model_id"] = "vlm-b"
    assert (
        make_client(settings, store).patch("/api/settings/model", json=payload).status_code == 200
    )

    assert enterprise_ai_connection_for_model(settings, "").connection_id == "secondary"
    assert enterprise_ai_connection_for_model(settings, "  ").connection_id == "secondary"


def test_resolver_keeps_env_only_default_models_on_the_primary_connection() -> None:
    """`.env` だけで決めた既定のモデル（登録モデルの一覧に無い）は、プライマリ接続で呼ぶ。"""
    settings = FakeSettings(
        enterprise_ai_endpoint=PRIMARY_ENDPOINT,
        enterprise_ai_api_key="sk-primary",
        enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id="listed")],
        enterprise_ai_default_text_model="env-text",
        enterprise_ai_default_vision_model="env-vision",
    )
    for model_id in ("env-text", "env-vision", "", "listed"):
        assert enterprise_ai_connection_for_model(settings, model_id).endpoint == PRIMARY_ENDPOINT
    with pytest.raises(EnterpriseAiModelNotRegisteredError):
        enterprise_ai_connection_for_model(settings, "removed-model")


def test_resolver_with_no_models_falls_back_only_for_empty_ids() -> None:
    """モデルを 1 つも設定していなければ、空の ID はプライマリ（未設定の判定は呼び出し側）。"""
    settings = FakeSettings()
    assert enterprise_ai_connection_for_model(settings, "").connection_id == "primary"
    with pytest.raises(EnterpriseAiModelNotRegisteredError):
        enterprise_ai_connection_for_model(settings, "any-model")


def test_resolver_falls_back_to_primary_when_connection_is_gone() -> None:
    settings = FakeSettings(
        enterprise_ai_endpoint=PRIMARY_ENDPOINT,
        enterprise_ai_api_key="sk-primary",
        enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id="m", connection_id="secondary")],
    )
    assert [c.connection_id for c in enterprise_ai_connections(settings)] == ["primary"]
    assert enterprise_ai_connection_for_model(settings, "m").endpoint == PRIMARY_ENDPOINT


def test_secondary_connection_from_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env だけでセカンダリ接続を設定した環境（JSON なし）も、セカンダリ接続を使える。"""
    monkeypatch.setenv(ENTERPRISE_AI_SECONDARY_API_KEY_ENV, "sk-env-secondary")
    settings = FakeSettings(
        enterprise_ai_secondary_endpoint=SECONDARY_ENDPOINT,
        enterprise_ai_secondary_api_key="sk-env-secondary",
    )
    make_store(tmp_path).load(settings)
    connections = enterprise_ai_connections(settings)
    assert [c.connection_id for c in connections] == ["primary", "secondary"]
    assert connections[1].api_key == "sk-env-secondary"


def test_old_json_is_read_as_single_connection(tmp_path: Path) -> None:
    """#533 より前の JSON（接続 1 組）はプライマリ接続として読み、モデルはプライマリ接続を使う。"""
    old = {
        "version": 3,
        "enterprise_ai": {
            "endpoint": PRIMARY_ENDPOINT,
            # #51 より前の JSON の key。
            "project_ocid": "ocid1.project.primary",
            "models": [{"model_id": "vlm", "display_name": "V", "vision_enabled": True}],
            "default_text_model_id": "",
            "default_vision_model_id": "vlm",
        },
    }
    (tmp_path / "model-settings.json").write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / ".env").write_text(f"{ENTERPRISE_AI_API_KEY_ENV}=sk-old\n", encoding="utf-8")
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    enterprise = (
        make_client(settings, store)
        .get("/api/settings/model")
        .json()["data"]["settings"]["enterprise_ai"]
    )
    assert enterprise["connections"] == [
        {
            "connection_id": "primary",
            "endpoint": PRIMARY_ENDPOINT,
            "project_id": "ocid1.project.primary",
            "api_key": "",
            "has_api_key": True,
            "clear_api_key": False,
        }
    ]
    assert enterprise["models"][0]["connection_id"] == "primary"
    connection = enterprise_ai_connection_for_model(settings, "vlm")
    assert (connection.endpoint, connection.api_key) == (PRIMARY_ENDPOINT, "sk-old")


def test_saved_json_with_old_project_key_is_read(tmp_path: Path) -> None:
    """#51 より前の JSON の接続の key `project_ocid` を `project_id` として読む。"""
    document = {
        "version": 6,
        "enterprise_ai": {
            "connections": [
                {
                    "connection_id": "primary",
                    "endpoint": PRIMARY_ENDPOINT,
                    "project_ocid": "proj-a",
                },
                {
                    "connection_id": "secondary",
                    "endpoint": SECONDARY_ENDPOINT,
                    "project_ocid": "proj-b",
                },
            ],
            "models": [],
        },
    }
    path = tmp_path / "model-settings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    settings = FakeSettings(enterprise_ai_secondary_api_key="sk-secondary")
    store = make_store(tmp_path)
    store.load(settings)
    connections = enterprise_ai_connections(settings)
    assert [(c.connection_id, c.project_id) for c in connections] == [
        ("primary", "proj-a"),
        ("secondary", "proj-b"),
    ]

    client = make_client(settings, store)
    payload = client.get("/api/settings/model").json()["data"]["settings"]
    assert client.patch("/api/settings/model", json=payload).status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))["enterprise_ai"]["connections"]
    assert [item.get("project_id") for item in saved] == ["proj-a", "proj-b"]
    assert all("project_ocid" not in item for item in saved)


def test_saved_json_with_unknown_connections_is_read_leniently(tmp_path: Path) -> None:
    document = {
        "version": 3,
        "enterprise_ai": {
            "connections": [
                {"connection_id": "quaternary", "endpoint": "https://x.invalid"},
                {"connection_id": "secondary", "endpoint": SECONDARY_ENDPOINT},
            ],
            "models": [],
        },
    }
    (tmp_path / "model-settings.json").write_text(json.dumps(document), encoding="utf-8")
    settings = FakeSettings(enterprise_ai_endpoint="https://env.invalid")
    make_store(tmp_path).load(settings)
    assert [c.connection_id for c in enterprise_ai_connections(settings)] == [
        "primary",
        "secondary",
    ]
    assert settings.enterprise_ai_endpoint == ""


def test_model_pointing_to_missing_connection_is_a_field_error(tmp_path: Path) -> None:
    payload = two_connection_payload()
    payload["enterprise_ai"]["connections"].pop()
    enterprise = EnterpriseAiModelSettings.model_validate(payload["enterprise_ai"])
    errors = validate_enterprise_ai_connections(enterprise)
    assert [error.field for error in errors] == ["models.1.connection_id"]

    settings = FakeSettings()
    store = make_store(tmp_path)
    response = make_client(settings, store).patch("/api/settings/model", json=payload)
    assert response.status_code == 422
    assert "「B」の接続がありません" in response.json()["detail"]
    assert not (tmp_path / "model-settings.json").exists()


def test_connection_labels_are_tab_names() -> None:
    """画面のタブ・登録モデルの選択肢・メッセージの名前（#542）。"""
    assert connection_label("primary") == "プライマリ接続"
    assert connection_label("secondary") == "セカンダリ接続"
    assert connection_label("tertiary") == "ターシャリ接続"
    assert connection_label("unknown") == "プライマリ接続"


@pytest.mark.parametrize(("field", "label"), [("endpoint", "Endpoint URL")])
def test_secondary_connection_requires_each_field(field: str, label: str) -> None:
    payload = two_connection_payload(**{field: ""})
    enterprise = EnterpriseAiModelSettings.model_validate(payload["enterprise_ai"])
    errors = validate_enterprise_ai_connections(enterprise)
    assert [(e.field, e.message) for e in errors] == [
        (f"connections.1.{field}", f"セカンダリ接続の {label} を入力してください。")
    ]


def test_secondary_connection_does_not_require_project_id() -> None:
    """Project ID はどの接続でも任意（#9）。project を求めない OpenAI 互換 API では空で足りる。"""
    enterprise = EnterpriseAiModelSettings.model_validate(
        two_connection_payload(project_id="")["enterprise_ai"]
    )
    keys = {"primary": "sk-primary", "secondary": "sk-secondary"}
    assert validate_enterprise_ai_connections(enterprise, api_keys=keys) == []


def test_primary_connection_fields_stay_optional() -> None:
    """プライマリ接続は使うときだけ必須（画面は「利用時必須」）。保存は止めない。"""
    payload = two_connection_payload()
    payload["enterprise_ai"]["connections"][0].update(endpoint="", project_id="", api_key="")
    enterprise = EnterpriseAiModelSettings.model_validate(payload["enterprise_ai"])
    keys = {"primary": "", "secondary": "sk-secondary"}
    assert validate_enterprise_ai_connections(enterprise, api_keys=keys) == []


def test_secondary_connection_requires_api_key(tmp_path: Path) -> None:
    """セカンダリ接続の API key は、保存後の値（空欄は保存済みの値）で確かめる（#542）。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)

    response = client.patch("/api/settings/model", json=two_connection_payload(api_key=""))
    assert response.status_code == 422
    assert response.json()["detail"] == "セカンダリ接続の API key を入力してください。"
    assert not (tmp_path / "model-settings.json").exists()

    # 保存済みの key があれば、空欄のままで保存できる
    assert client.patch("/api/settings/model", json=two_connection_payload()).status_code == 200
    assert (
        client.patch("/api/settings/model", json=two_connection_payload(api_key="")).status_code
        == 200
    )
    assert settings.enterprise_ai_secondary_api_key == "sk-secondary"


def test_saved_json_and_payload_with_connection_names_are_accepted(tmp_path: Path) -> None:
    """#533 の表示名（`display_name`）が残った JSON・payload も失敗させない（#542 で廃止）。"""
    document = {
        "version": 3,
        "enterprise_ai": {
            "connections": [
                {"connection_id": "primary", "display_name": "大阪", "endpoint": PRIMARY_ENDPOINT},
                {
                    "connection_id": "secondary",
                    "display_name": "シカゴ",
                    "endpoint": SECONDARY_ENDPOINT,
                    "project_id": "ocid1.project.secondary",
                },
            ],
            "models": [],
        },
    }
    (tmp_path / "model-settings.json").write_text(json.dumps(document), encoding="utf-8")
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    enterprise = client.get("/api/settings/model").json()["data"]["settings"]["enterprise_ai"]
    assert [c["connection_id"] for c in enterprise["connections"]] == ["primary", "secondary"]
    assert all("display_name" not in c for c in enterprise["connections"])

    payload = two_connection_payload()
    payload["enterprise_ai"]["connections"][1]["display_name"] = "シカゴ"
    assert client.patch("/api/settings/model", json=payload).status_code == 200
    saved = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert all("display_name" not in c for c in saved["enterprise_ai"]["connections"])


def test_removing_secondary_clears_its_settings_and_key(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    client.patch("/api/settings/model", json=two_connection_payload())

    payload = two_connection_payload()
    payload["enterprise_ai"]["connections"].pop()
    payload["enterprise_ai"]["models"][1]["connection_id"] = "primary"
    payload["enterprise_ai"]["connections"][0]["api_key"] = ""
    response = client.patch("/api/settings/model", json=payload)

    assert response.status_code == 200, response.text
    env = dotenv_values(tmp_path / ".env")
    assert env[ENTERPRISE_AI_API_KEY_ENV] == "sk-primary"
    assert ENTERPRISE_AI_SECONDARY_API_KEY_ENV not in env
    assert settings.enterprise_ai_secondary_endpoint == ""
    assert settings.enterprise_ai_secondary_api_key == ""
    assert [c.connection_id for c in enterprise_ai_connections(settings)] == ["primary"]


def test_blank_secondary_key_keeps_current_and_clear_is_rejected(tmp_path: Path) -> None:
    """空欄は保存済みの key を保持する。key だけの削除は、必須の欄を空にするので止める（#542）。

    セカンダリ接続の key を消すときは、セカンダリ接続ごと削除する。
    """
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    client.patch("/api/settings/model", json=two_connection_payload())

    client.patch("/api/settings/model", json=two_connection_payload(api_key=""))
    assert settings.enterprise_ai_secondary_api_key == "sk-secondary"

    response = client.patch(
        "/api/settings/model", json=two_connection_payload(api_key="", clear_api_key=True)
    )
    assert response.status_code == 422
    assert "セカンダリ接続の API key を入力してください。" in response.json()["detail"]
    assert settings.enterprise_ai_secondary_api_key == "sk-secondary"
    assert dotenv_values(tmp_path / ".env")[ENTERPRISE_AI_SECONDARY_API_KEY_ENV] == "sk-secondary"
    # プライマリ接続の key は変わらない
    assert settings.enterprise_ai_api_key == "sk-primary"


def test_other_worker_picks_up_secondary_key(tmp_path: Path) -> None:
    store_a, store_b = make_store(tmp_path), make_store(tmp_path)
    worker_a, worker_b = FakeSettings(), FakeSettings()
    store_a.load(worker_a)
    store_b.load(worker_b)
    make_client(worker_a, store_a).patch("/api/settings/model", json=two_connection_payload())
    make_client(worker_b, store_b).get("/api/settings/model")
    assert worker_b.enterprise_ai_secondary_api_key == "sk-secondary"
    assert enterprise_ai_connection_for_model(worker_b, "vlm-b").endpoint == SECONDARY_ENDPOINT


def test_model_test_uses_connection_of_tested_model(tmp_path: Path) -> None:
    """接続テストは、テストするモデルの接続（保存前の入力値）で呼ぶ。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    seen: list[tuple[str, str, str]] = []

    async def run_test(candidate: Any, request: ModelSettingsTestRequest) -> dict[str, Any]:
        connection = enterprise_ai_connection_for_model(candidate, request.model_id)
        seen.append((request.model_id, connection.endpoint, connection.api_key))
        raise RuntimeError(f"401 for {connection.api_key}")

    client = make_client(settings, store, run_model_test=run_test)
    for model_id, target in (("llm-a", "enterprise_text"), ("vlm-b", "enterprise_vision")):
        data = client.post(
            "/api/settings/model/test",
            json={
                "settings": two_connection_payload(),
                "target_type": target,
                "model_id": model_id,
            },
        ).json()["data"]
        assert "sk-" not in data["raw_error"]
    assert seen == [
        ("llm-a", PRIMARY_ENDPOINT, "sk-primary"),
        ("vlm-b", SECONDARY_ENDPOINT, "sk-secondary"),
    ]
    # テストは保存しない
    assert settings.enterprise_ai_secondary_endpoint == ""


def test_model_payload_hides_keys_of_both_connections() -> None:
    settings = FakeSettings(
        enterprise_ai_api_key="sk-primary",
        enterprise_ai_secondary_endpoint=SECONDARY_ENDPOINT,
        enterprise_ai_secondary_api_key="sk-secondary",
    )
    dumped = shared_model.model_payload(settings).model_dump_json()
    assert "sk-primary" not in dumped and "sk-secondary" not in dumped


# --------------------------------------------------------------------------- ターシャリ接続（#786）


def three_connection_payload(**tertiary: Any) -> dict[str, Any]:
    """ターシャリ接続（OpenAI 向け。Project ID なし）を足した payload。"""
    payload = two_connection_payload()
    payload["enterprise_ai"]["connections"].append(
        {
            "connection_id": "tertiary",
            "endpoint": TERTIARY_ENDPOINT,
            "project_id": "",
            "api_key": "sk-tertiary",
            **tertiary,
        }
    )
    payload["enterprise_ai"]["models"].append(
        {"model_id": "gpt-c", "display_name": "C", "connection_id": "tertiary"}
    )
    return payload


def test_tertiary_connection_does_not_require_project_id() -> None:
    """ターシャリ接続は Endpoint URL と API key が必須で、Project ID は任意（#786 / #9）。"""
    enterprise = EnterpriseAiModelSettings.model_validate(
        three_connection_payload()["enterprise_ai"]
    )
    keys = {"primary": "sk-primary", "secondary": "sk-secondary", "tertiary": "sk-tertiary"}
    assert validate_enterprise_ai_connections(enterprise, api_keys=keys) == []

    blank = EnterpriseAiModelSettings.model_validate(
        three_connection_payload(endpoint="")["enterprise_ai"]
    )
    errors = validate_enterprise_ai_connections(blank, api_keys={**keys, "tertiary": ""})
    assert [(e.field, e.message) for e in errors] == [
        ("connections.2.endpoint", "ターシャリ接続の Endpoint URL を入力してください。"),
        ("connections.2.api_key", "ターシャリ接続の API key を入力してください。"),
    ]


def test_save_and_load_tertiary_connection(tmp_path: Path) -> None:
    """ターシャリ接続は JSON（Endpoint・Project ID）と `.env`（API key）に分けて保存する。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    response = make_client(settings, store).patch(
        "/api/settings/model", json=three_connection_payload()
    )

    assert response.status_code == 200, response.text
    enterprise = response.json()["data"]["settings"]["enterprise_ai"]
    assert [c["connection_id"] for c in enterprise["connections"]] == [
        "primary",
        "secondary",
        "tertiary",
    ]
    assert enterprise["connections"][2]["has_api_key"] is True
    assert enterprise["connections"][2]["project_id"] == ""
    assert [m["connection_id"] for m in enterprise["models"]] == [
        "primary",
        "secondary",
        "tertiary",
    ]
    assert "sk-tertiary" not in response.text
    raw = (tmp_path / "model-settings.json").read_text(encoding="utf-8")
    assert "sk-tertiary" not in raw
    assert json.loads(raw)["enterprise_ai"]["connections"][2] == {
        "connection_id": "tertiary",
        "endpoint": TERTIARY_ENDPOINT,
        "project_id": "",
    }
    assert dotenv_values(tmp_path / ".env")[ENTERPRISE_AI_TERTIARY_API_KEY_ENV] == "sk-tertiary"

    # 別 worker（JSON と `.env` から読む）も同じ接続で呼ぶ。
    worker_b = FakeSettings()
    make_store(tmp_path).load(worker_b)
    connection = enterprise_ai_connection_for_model(worker_b, "gpt-c")
    assert (
        connection.connection_id,
        connection.endpoint,
        connection.api_key,
        connection.project_id,
    ) == ("tertiary", TERTIARY_ENDPOINT, "sk-tertiary", "")
    assert connection.is_configured()


def test_tertiary_connection_without_secondary(tmp_path: Path) -> None:
    """セカンダリ接続を置かずに、プライマリ接続とターシャリ接続だけで保存できる（#786）。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    payload = three_connection_payload()
    del payload["enterprise_ai"]["connections"][1]
    payload["enterprise_ai"]["models"][1]["connection_id"] = "primary"

    response = make_client(settings, store).patch("/api/settings/model", json=payload)

    assert response.status_code == 200, response.text
    assert [c.connection_id for c in enterprise_ai_connections(settings)] == [
        "primary",
        "tertiary",
    ]
    env = dotenv_values(tmp_path / ".env")
    assert ENTERPRISE_AI_SECONDARY_API_KEY_ENV not in env
    assert env[ENTERPRISE_AI_TERTIARY_API_KEY_ENV] == "sk-tertiary"


def test_tertiary_connection_from_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env（`PLATFORM_ENTERPRISE_AI_TERTIARY_*`）だけで設定したターシャリ接続も使える。"""
    monkeypatch.setenv(ENTERPRISE_AI_TERTIARY_API_KEY_ENV, "sk-env-tertiary")
    settings = FakeSettings(
        enterprise_ai_tertiary_endpoint=TERTIARY_ENDPOINT,
        enterprise_ai_tertiary_api_key="sk-env-tertiary",
        enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="gpt-c", connection_id="tertiary")
        ],
    )
    make_store(tmp_path).load(settings)
    assert [c.connection_id for c in enterprise_ai_connections(settings)] == [
        "primary",
        "tertiary",
    ]
    connection = enterprise_ai_connection_for_model(settings, "gpt-c")
    assert (connection.endpoint, connection.api_key) == (TERTIARY_ENDPOINT, "sk-env-tertiary")


def test_old_json_keeps_tertiary_connection_from_environment(tmp_path: Path) -> None:
    """#533 より前の JSON（接続 1 組）でも、env のターシャリ接続はそのまま使う。"""
    old = {"version": 3, "enterprise_ai": {"endpoint": PRIMARY_ENDPOINT, "models": []}}
    (tmp_path / "model-settings.json").write_text(json.dumps(old), encoding="utf-8")
    settings = FakeSettings(
        enterprise_ai_tertiary_endpoint=TERTIARY_ENDPOINT,
        enterprise_ai_tertiary_api_key="sk-tertiary",
    )
    make_store(tmp_path).load(settings)
    assert [c.connection_id for c in enterprise_ai_connections(settings)] == [
        "primary",
        "tertiary",
    ]


def test_removing_tertiary_clears_its_settings_and_key(tmp_path: Path) -> None:
    """ターシャリ接続を削除すると、Endpoint などと `.env` の key を消す（モデルはプライマリへ）。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    client = make_client(settings, store)
    assert client.patch("/api/settings/model", json=three_connection_payload()).status_code == 200

    payload = three_connection_payload()
    payload["enterprise_ai"]["connections"].pop()
    payload["enterprise_ai"]["models"][2]["connection_id"] = "primary"
    response = client.patch("/api/settings/model", json=payload)

    assert response.status_code == 200, response.text
    env = dotenv_values(tmp_path / ".env")
    assert ENTERPRISE_AI_TERTIARY_API_KEY_ENV not in env
    assert env[ENTERPRISE_AI_SECONDARY_API_KEY_ENV] == "sk-secondary"
    assert settings.enterprise_ai_tertiary_endpoint == ""
    assert settings.enterprise_ai_tertiary_api_key == ""
    assert enterprise_ai_connection_for_model(settings, "gpt-c").connection_id == "primary"


def test_tertiary_connection_requires_api_key(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    response = make_client(settings, store).patch(
        "/api/settings/model", json=three_connection_payload(api_key="")
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "ターシャリ接続の API key を入力してください。"


# --------------------------------------------------------------------------- 保存の競合（#1037）


def test_stale_save_from_other_product_is_rejected_and_keeps_secondary_connection(
    tmp_path: Path,
) -> None:
    """画面を開いた後に別の製品が保存していたら、古い値での保存は 409 にして上書きしない。"""
    store_a, store_b = make_store(tmp_path), make_store(tmp_path)
    product_a, product_b = FakeSettings(), FakeSettings()
    store_a.load(product_a)
    store_b.load(product_b)
    client_a, client_b = make_client(product_a, store_a), make_client(product_b, store_b)

    # 製品 A の画面を開く（セカンダリ接続はまだない）。
    opened = client_a.get("/api/settings/model").json()["data"]
    assert opened["revision"]
    # 製品 B がセカンダリ接続を設定して保存する。
    assert client_b.patch("/api/settings/model", json=two_connection_payload()).status_code == 200

    # 製品 A の画面は、開いた時点の値に既定の再ランクモデルの変更を重ねて保存する。
    stale = opened["settings"]
    stale["enterprise_ai"]["default_rerank_model_id"] = "rr-a"
    response = client_a.patch(
        "/api/settings/model", json={**stale, "base_revision": opened["revision"]}
    )

    assert response.status_code == 409
    assert "ほかの画面" in response.json()["detail"]
    env = dotenv_values(tmp_path / ".env")
    assert env[ENTERPRISE_AI_SECONDARY_API_KEY_ENV] == "sk-secondary"
    assert [item.connection_id for item in enterprise_ai_connections(product_a)] == [
        "primary",
        "secondary",
    ]
    assert product_a.genai_rerank_model == "rr"

    # 最新の設定を読み直せば、その版で保存できる（セカンダリ接続は残る）。
    latest = client_a.get("/api/settings/model").json()["data"]
    assert latest["revision"] != opened["revision"]
    latest["settings"]["enterprise_ai"]["default_rerank_model_id"] = "rr-a"
    saved = client_a.patch(
        "/api/settings/model",
        json={**latest["settings"], "base_revision": latest["revision"]},
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["revision"] not in {opened["revision"], latest["revision"]}
    assert dotenv_values(tmp_path / ".env")[ENTERPRISE_AI_SECONDARY_API_KEY_ENV] == "sk-secondary"
    assert product_a.genai_rerank_model == "rr-a"


def test_revision_is_same_across_workers_and_save_without_base_revision_is_allowed(
    tmp_path: Path,
) -> None:
    """版は同じ状態ならどの worker でも同じ。`base_revision` を送らない保存は従来どおり通る。"""
    store_a, store_b = make_store(tmp_path), make_store(tmp_path)
    worker_a, worker_b = FakeSettings(), FakeSettings()
    store_a.load(worker_a)
    store_b.load(worker_b)
    client_a, client_b = make_client(worker_a, store_a), make_client(worker_b, store_b)

    saved = client_a.patch("/api/settings/model", json=two_connection_payload()).json()["data"]
    assert client_b.get("/api/settings/model").json()["data"]["revision"] == saved["revision"]
    # 同じ版なら、別の worker でも保存できる。
    again = client_b.patch(
        "/api/settings/model",
        json={**saved["settings"], "base_revision": saved["revision"]},
    )
    assert again.status_code == 200
    # API key の値は版にも応答にも入らない（有無だけ）。同じ内容の保存なら版は変わらない。
    assert again.json()["data"]["revision"] == saved["revision"]
    assert "sk-secondary" not in json.dumps(again.json())

    legacy = client_a.patch("/api/settings/model", json=saved["settings"])
    assert legacy.status_code == 200
