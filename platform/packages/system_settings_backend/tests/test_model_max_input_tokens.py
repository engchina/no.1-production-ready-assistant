"""登録モデルごとの入力の上限 `max_input_tokens`（tokens。#1437）。

既定は 128000（値の無いモデル）、範囲は 4096〜2000000。値の解釈・入力の予算・token の見積もりは
`pr_system_settings.model` の 1 か所（`max_input_tokens_to_use` / `input_token_budget` /
`estimate_input_tokens`）で、3 製品はこれを使う。
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
    DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS,
    ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX,
    ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN,
    MODEL_SETTINGS_DOCUMENT_VERSION,
    EnterpriseAiConfiguredModel,
    enterprise_ai_max_input_tokens,
    enterprise_ai_model_catalog,
    estimate_input_tokens,
    input_token_budget,
    max_input_tokens_to_use,
)


def _payload_with_limit(value: object) -> dict[str, Any]:
    payload = copy.deepcopy(PAYLOAD)
    payload["enterprise_ai"]["models"][0]["max_input_tokens"] = value
    return payload


def test_default_and_range() -> None:
    assert DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS == 128_000
    assert (ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN, ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX) == (
        4_096,
        2_000_000,
    )
    assert EnterpriseAiConfiguredModel(model_id="m").max_input_tokens == 128_000
    unset = EnterpriseAiConfiguredModel.model_validate({"model_id": "m", "max_input_tokens": None})
    assert unset.max_input_tokens == 128_000


@pytest.mark.parametrize(
    "value",
    [4_095, 2_000_001, True, "abc"],
    ids=["below-min", "above-max", "bool", "text"],
)
def test_out_of_range_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        EnterpriseAiConfiguredModel.model_validate({"model_id": "m", "max_input_tokens": value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 128_000),
        (200_000, 200_000),
        (" 32768 ", 32_768),
        (4_096, 4_096),
        (2_000_000, 2_000_000),
        (1_000, 128_000),
        (3_000_000, 128_000),
        ("x", 128_000),
        (True, 128_000),
    ],
    ids=["none", "value", "text", "min", "max", "below", "above", "invalid", "bool"],
)
def test_value_to_use(value: object, expected: int) -> None:
    """値の解釈はここだけ: 範囲の中の整数はその値、それ以外は既定の 128000。"""
    assert max_input_tokens_to_use(value) == expected


@pytest.mark.parametrize(
    ("limit", "percent", "expected"),
    [
        (128_000, 50, 64_000),
        (128_000, 60, 76_800),
        (200_000, 70, 140_000),
        (None, 50, 64_000),
        (128_000, 0, 1_280),
        (128_000, 150, 128_000),
        (128_000, "x", 64_000),
    ],
    ids=["half", "part", "large", "default-limit", "min-share", "max-share", "invalid-share"],
)
def test_input_token_budget(limit: object, percent: object, expected: int) -> None:
    assert input_token_budget(limit, percent) == expected


def test_estimate_input_tokens() -> None:
    """ASCII は 4 字で 1 token（切り上げ）、それ以外は 1 字で 1 token。文字数を超えない。"""
    assert estimate_input_tokens("") == 0
    assert estimate_input_tokens("点検") == 2
    assert estimate_input_tokens("abcd") == 1
    assert estimate_input_tokens("abcde") == 2
    assert estimate_input_tokens("UPS の点検") == 1 + 3
    text = "点検の手順 step 1: 電源を切る。" * 50
    assert estimate_input_tokens(text) <= len(text)


def test_limit_is_saved_per_model_and_reloaded(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    payload = _payload_with_limit(200_000)

    response = make_client(settings, store).patch("/api/settings/model", json=payload)

    assert response.status_code == 200
    models = response.json()["data"]["settings"]["enterprise_ai"]["models"]
    # 値を渡さなかった 2 つ目のモデルは既定。
    assert [model["max_input_tokens"] for model in models] == [200_000, 128_000]
    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert document["version"] == MODEL_SETTINGS_DOCUMENT_VERSION == 6
    assert [model["max_input_tokens"] for model in document["enterprise_ai"]["models"]] == [
        200_000,
        128_000,
    ]
    other = FakeSettings()
    make_store(tmp_path).load(other)
    assert enterprise_ai_max_input_tokens(other, "llm-a") == 200_000
    assert enterprise_ai_max_input_tokens(other, " vlm-b ") == 128_000
    # 登録モデルに無い ID は既定。
    assert enterprise_ai_max_input_tokens(other, "unknown") == 128_000


@pytest.mark.parametrize("value", [1_000, 3_000_000], ids=["below-min", "above-max"])
def test_out_of_range_save_is_422(tmp_path: Path, value: int) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    response = make_client(settings, store).patch(
        "/api/settings/model", json=_payload_with_limit(value)
    )

    assert response.status_code == 422
    assert not (tmp_path / "model-settings.json").exists()


def test_v4_document_without_limit_is_read_as_default(tmp_path: Path) -> None:
    """#1437 より前の JSON（version 4、項目なし）はそのまま読め、既定の 128000 になる。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 4,
                "enterprise_ai": {
                    "models": [{"model_id": "llm-a", "reasoning_effort": "low"}],
                    "default_text_model_id": "llm-a",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    make_store(tmp_path).load(settings)

    assert [model.max_input_tokens for model in enterprise_ai_model_catalog(settings)] == [128_000]
    assert enterprise_ai_max_input_tokens(settings, "llm-a") == 128_000
