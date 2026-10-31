"""登録モデルごとの推論（思考）の量 `reasoning_effort`（#1425）。

選択肢は none / low / medium / high、既定は low、none は `reasoning` を送らない。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_model import PAYLOAD, FakeSettings, make_client, make_store

from pr_system_settings.model import (
    DEFAULT_ENTERPRISE_AI_REASONING_EFFORT,
    ENTERPRISE_AI_REASONING_EFFORTS,
    MODEL_SETTINGS_DOCUMENT_VERSION,
    EnterpriseAiConfiguredModel,
    ModelSettingsTestRequest,
    enterprise_ai_model_catalog,
    enterprise_ai_reasoning,
    enterprise_ai_reasoning_effort,
    enterprise_ai_reasoning_effort_to_send,
    lower_reasoning_effort,
    reasoning_effort_to_send,
    reasoning_payload,
)


def _payload_with_effort(effort: str) -> dict[str, Any]:
    payload = copy.deepcopy(PAYLOAD)
    payload["enterprise_ai"]["models"][0]["reasoning_effort"] = effort
    return payload


def test_choices_and_default() -> None:
    assert ENTERPRISE_AI_REASONING_EFFORTS == ("none", "low", "medium", "high")
    assert DEFAULT_ENTERPRISE_AI_REASONING_EFFORT == "low"
    assert EnterpriseAiConfiguredModel(model_id="m").reasoning_effort == "low"


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("none", ""),
        ("low", "low"),
        ("medium", "medium"),
        (" High ", "high"),
    ],
    ids=["none", "low", "medium", "high"],
)
def test_none_is_not_sent(effort: str, expected: str) -> None:
    """値の解釈はここだけ: none（と空）は送らない、それ以外はその値を送る。"""
    assert reasoning_effort_to_send(effort) == expected
    assert reasoning_payload(effort) == ({"effort": expected} if expected else None)


def test_effort_is_saved_per_model_and_reloaded(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    payload = _payload_with_effort("low")
    payload["enterprise_ai"]["models"][1]["reasoning_effort"] = "none"

    response = make_client(settings, store).patch("/api/settings/model", json=payload)

    assert response.status_code == 200
    models = response.json()["data"]["settings"]["enterprise_ai"]["models"]
    assert [model["reasoning_effort"] for model in models] == ["low", "none"]
    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert document["version"] == MODEL_SETTINGS_DOCUMENT_VERSION == 6
    assert [model["reasoning_effort"] for model in document["enterprise_ai"]["models"]] == [
        "low",
        "none",
    ]
    # 別の worker も同じ値を読む。
    other = FakeSettings()
    make_store(tmp_path).load(other)
    assert enterprise_ai_reasoning_effort(other, "llm-a") == "low"
    assert enterprise_ai_reasoning(other, "llm-a") == {"effort": "low"}
    assert enterprise_ai_reasoning_effort(other, "vlm-b") == "none"
    assert enterprise_ai_reasoning(other, "vlm-b") is None
    assert enterprise_ai_reasoning_effort_to_send(other, "vlm-b") == ""


def test_v3_document_without_effort_is_read_as_default_low(tmp_path: Path) -> None:
    """#1425 より前の JSON（version 3、項目なし）はそのまま読め、既定の low を送る。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 3,
                "enterprise_ai": {
                    "models": [{"model_id": "llm-a", "display_name": "A"}],
                    "default_text_model_id": "llm-a",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    make_store(tmp_path).load(settings)

    assert [model.reasoning_effort for model in enterprise_ai_model_catalog(settings)] == ["low"]
    assert enterprise_ai_reasoning(settings, "llm-a") == {"effort": "low"}


@pytest.mark.parametrize("effort", ["minimal", "xhigh", ""])
def test_document_with_effort_outside_choices_is_rejected(tmp_path: Path, effort: str) -> None:
    """選択肢（none / low / medium / high）以外の値は、読み込みでも検証エラーにする（寄せない）。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 4,
                "enterprise_ai": {
                    "models": [{"model_id": "llm-a", "reasoning_effort": effort}],
                    "default_text_model_id": "llm-a",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="モデル設定ファイルを読み込めません"):
        make_store(tmp_path).load(FakeSettings())


@pytest.mark.parametrize("effort", ["minimal", "xhigh", "max", "ultra", ""])
def test_api_rejects_values_outside_choices(tmp_path: Path, effort: str) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    response = make_client(settings, store).patch(
        "/api/settings/model", json=_payload_with_effort(effort)
    )

    assert response.status_code == 422
    assert not (tmp_path / "model-settings.json").exists()


@pytest.mark.parametrize("effort", ENTERPRISE_AI_REASONING_EFFORTS)
def test_model_accepts_every_choice(effort: str) -> None:
    model = EnterpriseAiConfiguredModel.model_validate(
        {"model_id": "m", "reasoning_effort": effort}
    )
    assert model.reasoning_effort == effort


def test_model_rejects_values_outside_choices() -> None:
    with pytest.raises(ValidationError):
        EnterpriseAiConfiguredModel.model_validate({"model_id": "m", "reasoning_effort": "xhigh"})


def test_unregistered_model_uses_default_low() -> None:
    settings = FakeSettings(
        oci_enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="llm-a", reasoning_effort="none")
        ]
    )
    assert enterprise_ai_reasoning(settings, " llm-a ") is None
    # env だけで設定したモデルなど、登録モデルにない ID も既定の low。
    assert enterprise_ai_reasoning(settings, "env-only-model") == {"effort": "low"}


def test_model_test_uses_unsaved_effort(tmp_path: Path) -> None:
    """「テスト」は保存前に選んだ推論の量で呼ぶ（受け付けない値を保存の前に確かめられる）。"""
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    seen: dict[str, Any] = {}

    async def run_test(candidate: Any, request: ModelSettingsTestRequest) -> dict[str, Any]:
        seen["reasoning"] = enterprise_ai_reasoning(candidate, request.model_id)
        return {}

    request = {
        "settings": _payload_with_effort("high"),
        "target_type": "enterprise_text",
        "model_id": "llm-a",
    }
    data = (
        make_client(settings, store, run_model_test=run_test)
        .post("/api/settings/model/test", json=request)
        .json()["data"]
    )

    assert data["status"] == "success"
    assert seen["reasoning"] == {"effort": "high"}
    # 保存済みの設定（モデル未登録 = 既定）は変えない。
    assert enterprise_ai_reasoning(settings, "llm-a") == {"effort": "low"}


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("high", "medium"),
        ("medium", "low"),
        ("low", ""),
        ("none", ""),
        ("", ""),
        ("HIGH", "medium"),
    ],
    ids=["high", "medium", "low-is-the-floor", "none", "empty", "case-insensitive"],
)
def test_lower_reasoning_effort_steps_down_to_low(effort: str, expected: str) -> None:
    """出力の上限で未完了の送り直しは推論の量を 1 段下げる（low より下にはしない。#2023）。"""
    assert lower_reasoning_effort(effort) == expected
