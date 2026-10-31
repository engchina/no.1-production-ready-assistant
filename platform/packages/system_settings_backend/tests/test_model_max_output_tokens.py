"""登録モデルごとの出力の上限 `max_output_tokens`（tokens。#1719）と、呼び出しの出力の予算。

既定は 32768（値の無いモデル。入力の上限がそれより小さければ入力の上限）、範囲は 1〜2000000 で、
入力の上限（prompt と応答の合計）以下。共通の `llm_max_output_tokens` / `vlm_max_output_tokens` は
持たず、#1719 より前の JSON のそれらは読み捨てる。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_model import PAYLOAD, FakeSettings, make_client, make_store

from pr_system_settings.enterprise_ai_errors import (
    EnterpriseAiIncomplete,
    enterprise_ai_incomplete_from_response,
    enterprise_ai_incomplete_message,
)
from pr_system_settings.model import (
    DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS,
    ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX,
    ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN,
    EnterpriseAiConfiguredModel,
    enterprise_ai_max_output_tokens,
    enterprise_ai_model_catalog,
    enterprise_ai_output_token_budget,
    enterprise_ai_retry_output_token_budget,
    max_output_tokens_to_use,
    output_token_budget,
)


def _payload_with(**limits: object) -> dict[str, Any]:
    payload = copy.deepcopy(PAYLOAD)
    payload["enterprise_ai"]["models"][0].update(limits)
    return payload


def test_default_and_range() -> None:
    assert DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS == 32_768
    assert (ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN, ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX) == (
        1,
        2_000_000,
    )
    assert EnterpriseAiConfiguredModel(model_id="m").max_output_tokens == 32_768
    unset = EnterpriseAiConfiguredModel.model_validate({"model_id": "m", "max_output_tokens": None})
    assert unset.max_output_tokens == 32_768
    # 入力の上限が既定より小さいモデルは、値が無ければ入力の上限。
    small = EnterpriseAiConfiguredModel.model_validate({"model_id": "m", "max_input_tokens": 8_192})
    assert small.max_output_tokens == 8_192
    # 128k 以上を入れられる（入力の上限の中なら）。
    large = EnterpriseAiConfiguredModel.model_validate(
        {"model_id": "m", "max_input_tokens": 1_000_000, "max_output_tokens": 131_072}
    )
    assert large.max_output_tokens == 131_072


@pytest.mark.parametrize(
    "limits",
    [
        {"max_output_tokens": 0},
        {"max_output_tokens": 2_000_001, "max_input_tokens": 2_000_000},
        {"max_output_tokens": True},
        {"max_output_tokens": "abc"},
        {"max_output_tokens": 128_001, "max_input_tokens": 128_000},
    ],
    ids=["below-min", "above-max", "bool", "text", "above-input"],
)
def test_invalid_is_rejected(limits: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EnterpriseAiConfiguredModel.model_validate({"model_id": "m", **limits})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 32_768),
        (65_536, 65_536),
        (" 4096 ", 4_096),
        (0, 32_768),
        ("x", 32_768),
        (True, 32_768),
    ],
    ids=["none", "value", "text", "below", "invalid", "bool"],
)
def test_value_to_use(value: object, expected: int) -> None:
    assert max_output_tokens_to_use(value) == expected


def test_budget_takes_min_of_model_limit_and_context_room() -> None:
    # 要求が無ければモデルの出力の上限。
    assert output_token_budget(max_input_tokens=128_000, max_output_tokens=65_536) == 65_536
    # 機能の要る量が小さくても、既定（32768）を下回らない。
    assert (
        output_token_budget(max_input_tokens=128_000, max_output_tokens=65_536, requested=4_000)
        == 32_768
    )
    # 機能の要る量が既定より大きければその量（モデルの上限まで）。
    assert (
        output_token_budget(max_input_tokens=128_000, max_output_tokens=65_536, requested=50_000)
        == 50_000
    )
    # モデルの上限が既定より小さければモデルの上限。
    assert (
        output_token_budget(max_input_tokens=128_000, max_output_tokens=16_000, requested=4_000)
        == 16_000
    )
    # 入力の上限から prompt を引いた残りを超えない
    # （gpt-oss-120b の context 128k に出力 128k を指定した場合）。
    assert (
        output_token_budget(
            max_input_tokens=128_000, max_output_tokens=128_000, prompt_tokens=100_000
        )
        == 28_000
    )
    assert (
        output_token_budget(max_input_tokens=4_096, max_output_tokens=4_096, prompt_tokens=9_999)
        == 1
    )


def test_model_budget_and_retry_widen_up_to_model_limit() -> None:
    settings = FakeSettings()
    settings.oci_enterprise_ai_models = [
        EnterpriseAiConfiguredModel(
            model_id="big", max_input_tokens=1_000_000, max_output_tokens=131_072
        ),
        EnterpriseAiConfiguredModel(model_id="default"),
    ]
    assert enterprise_ai_max_output_tokens(settings, "big") == 131_072
    assert enterprise_ai_max_output_tokens(settings, "default") == 32_768
    assert enterprise_ai_max_output_tokens(settings, "unknown") == 32_768
    first = enterprise_ai_output_token_budget(settings, "big", requested=6_000)
    assert first == 32_768
    assert enterprise_ai_retry_output_token_budget(settings, "big", current=first) == 131_072
    # 既にモデルの上限で送ったなら、広げられないので送り直さない。
    assert enterprise_ai_retry_output_token_budget(settings, "default", current=32_768) is None


def test_limit_is_saved_per_model_and_old_common_keys_are_dropped(tmp_path: Path) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)
    payload = _payload_with(max_output_tokens=65_536)
    # #1719 より前の共通の項目は、送られても知らない key として無視する。
    payload["enterprise_ai"]["llm_max_output_tokens"] = 1200

    response = make_client(settings, store).patch("/api/settings/model", json=payload)

    assert response.status_code == 200
    enterprise = response.json()["data"]["settings"]["enterprise_ai"]
    assert [model["max_output_tokens"] for model in enterprise["models"]] == [65_536, 32_768]
    assert "llm_max_output_tokens" not in enterprise
    assert "vlm_max_output_tokens" not in enterprise
    document = json.loads((tmp_path / "model-settings.json").read_text(encoding="utf-8"))
    assert document["version"] == 6
    assert "llm_max_output_tokens" not in document["enterprise_ai"]
    assert [model["max_output_tokens"] for model in document["enterprise_ai"]["models"]] == [
        65_536,
        32_768,
    ]
    other = FakeSettings()
    make_store(tmp_path).load(other)
    assert enterprise_ai_max_output_tokens(other, "llm-a") == 65_536


@pytest.mark.parametrize(
    "limits",
    [{"max_output_tokens": 0}, {"max_output_tokens": 200_000, "max_input_tokens": 128_000}],
    ids=["below-min", "above-input"],
)
def test_invalid_save_is_422(tmp_path: Path, limits: dict[str, object]) -> None:
    settings = FakeSettings()
    store = make_store(tmp_path)
    store.load(settings)

    response = make_client(settings, store).patch(
        "/api/settings/model", json=_payload_with(**limits)
    )

    assert response.status_code == 422
    assert not (tmp_path / "model-settings.json").exists()


def test_v5_document_with_common_limits_is_read_per_model_default(tmp_path: Path) -> None:
    """#1719 より前の JSON（version 5、共通の 1200）は読め、各モデルは既定の 32768 になる。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 5,
                "enterprise_ai": {
                    "models": [
                        {"model_id": "llm-a", "max_input_tokens": 128_000},
                        {"model_id": "small", "max_input_tokens": 8_192},
                    ],
                    "default_text_model_id": "llm-a",
                    "llm_max_output_tokens": 1200,
                    "vlm_max_output_tokens": 65536,
                },
            }
        ),
        encoding="utf-8",
    )
    settings = FakeSettings()
    make_store(tmp_path).load(settings)

    assert [model.max_output_tokens for model in enterprise_ai_model_catalog(settings)] == [
        32_768,
        8_192,
    ]


def test_incomplete_detail_has_reason_limit_and_usage() -> None:
    response = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {
            "input_tokens": 934,
            "output_tokens": 1200,
            "output_tokens_details": {"reasoning_tokens": 1198},
        },
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "secret-ish"}]}],
    }
    incomplete = enterprise_ai_incomplete_from_response(response, max_output_tokens=1200)
    assert incomplete == EnterpriseAiIncomplete(
        status="incomplete",
        reason="max_output_tokens",
        max_output_tokens=1200,
        output_tokens=1200,
        reasoning_tokens=1198,
    )
    assert incomplete.output_limit_reached
    message = enterprise_ai_incomplete_message(incomplete, model_id="m", retried=True)
    assert "max_output_tokens" in message
    assert "出力の上限=1200 tokens" in message
    assert "使った出力=1200 tokens（うち推論 1198）" in message
    assert "送り直しても" in message
    # 応答の本文は出さない。
    assert "secret-ish" not in message
    assert enterprise_ai_incomplete_from_response({"status": "completed"}) is None


def test_incomplete_message_names_the_retries_and_the_fixes() -> None:
    """送り直しの句と、まだ下げられるときだけ推論の量を直し方に入れる（#2023）。"""
    incomplete = EnterpriseAiIncomplete(
        status="incomplete", reason="max_output_tokens", max_output_tokens=1200
    )
    both = enterprise_ai_incomplete_message(
        incomplete, model_id="m", retried=True, lowered_effort=True, reasoning_effort="low"
    )
    assert "出力の上限を広げ、推論の量を下げて送り直しても" in both
    assert "「推論の量」を下げる" not in both
    medium = enterprise_ai_incomplete_message(incomplete, model_id="m", reasoning_effort="medium")
    assert "送り直しても" not in medium
    assert "「推論の量」を下げる" in medium
    filtered = enterprise_ai_incomplete_message(
        EnterpriseAiIncomplete(status="incomplete", reason="content_filter"), model_id="m"
    )
    assert "安全の判定" in filtered
