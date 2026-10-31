"""Structured Outputs の出力の上限と、未完了の送り直し・組への分割（#1719）。

推論のあるモデルは推論の token も出力の上限に数える。2026-10-13 の実測（コメント管理の SQL 生成と
同じ要求、5 表・32 列）で、openai.gpt-oss-120b（推論 medium）は上限 1200 では output 1200 のうち
reasoning 1198 で `status=incomplete`・`incomplete_details.reason=max_output_tokens` になった。
ここでは同じ振る舞いを HTTP のスタブ（推論の token + 本文の token が上限を超えたら未完了）で
再現する。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pr_system_settings.model import ModelSettingsStore

from app.features.nl2sql import enterprise_ai_client as module
from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError
from app.features.nl2sql.models import MetadataSqlGenerateRequest
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.features.nl2sql.structured_outputs import SqlOutput, response_format
from app.settings import EnterpriseAiConfiguredModel, Settings

_API_KEY = "synthetic-secret-api-key-1719"
_MODEL = "xai.grok-4.3"
_REASONING_TOKENS = 1198
# 推論の量を low に下げた送り直し（#2023）で使う推論の token（スタブの値）。
_LOW_REASONING_TOKENS = 300
_ENDPOINT = "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1"
_PROJECT = "ocid1.generativeaiproject.oc1.us-chicago-1.synthetic"


def _settings(**model: Any) -> Settings:
    return Settings(
        oci_enterprise_ai_endpoint=_ENDPOINT,
        oci_enterprise_ai_api_key=_API_KEY,
        oci_enterprise_ai_project_ocid=_PROJECT,
        oci_enterprise_ai_default_text_model=_MODEL,
        oci_enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id=_MODEL, reasoning_effort="medium", **model)
        ],
    )


def _targets_in(payload: dict[str, Any]) -> list[str]:
    text = str(payload["input"][0]["content"])
    match = re.search(r"^targets: (.*)$", text, flags=re.M)
    assert match is not None
    return [item.split(":", 1)[1] for item in match.group(1).split(", ")]


class _ReasoningModel:
    """推論のあるモデルのスタブ: 推論の token + 本文の token が上限を超えたら未完了を返す。"""

    def __init__(
        self,
        *,
        tokens_per_target: int = 300,
        sql_for: Callable[[list[str]], str] | None = None,
        fail_targets: frozenset[str] = frozenset(),
    ) -> None:
        self.tokens_per_target = tokens_per_target
        self.sql_for = sql_for or (
            lambda targets: "\n".join(
                f"COMMENT ON TABLE {name} IS 'AI のコメント';" for name in targets
            )
        )
        self.fail_targets = fail_targets
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.payloads.append(payload)
        budget = int(payload["max_output_tokens"])
        targets = _targets_in(payload)
        reasoning = (
            _LOW_REASONING_TOKENS
            if payload.get("reasoning") == {"effort": "low"}
            else _REASONING_TOKENS
        )
        needed = reasoning + self.tokens_per_target * len(targets)
        if budget < needed or self.fail_targets.intersection(targets):
            return httpx.Response(
                200,
                json={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [
                        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "…"}]}
                    ],
                    "usage": {
                        "input_tokens": 934,
                        "output_tokens": budget,
                        "output_tokens_details": {"reasoning_tokens": min(budget, reasoning)},
                    },
                },
            )
        text = json.dumps({"sql": self.sql_for(targets), "explanation": ""}, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "…"}]},
                    {"type": "message", "content": [{"type": "output_text", "text": text}]},
                ],
                "usage": {
                    "input_tokens": 934,
                    "output_tokens": needed,
                    "output_tokens_details": {"reasoning_tokens": reasoning},
                },
            },
        )


_REAL_HTTPX_CLIENT = httpx.Client


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: _REAL_HTTPX_CLIENT(transport=transport, **kwargs)
    )


def _service(settings: Settings) -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = module.OciEnterpriseAiDirectClient(settings)
    return service


def _request(service: Nl2SqlService, count: int, columns: int = 10) -> MetadataSqlGenerateRequest:
    names = [f"T{index:02d}" for index in range(1, count + 1)]
    blocks = [
        "\n".join(
            [f"OBJECT: {name}", "TYPE: table", "COMMENT: -", "COLUMNS:"]
            + [f"- C{column}: NUMBER NULLABLE=Y COMMENT=-" for column in range(1, columns + 1)]
        )
        for name in names
    ]
    return MetadataSqlGenerateRequest(
        targets=[{"object_name": name, "object_type": "table"} for name in names],
        structure_text="\n\n".join(blocks),
        primary_key_text="\n\n".join(f"OBJECT: {name}\n{name}_PK P(C1)" for name in names),
    )


def test_old_common_limit_1200_is_dropped_and_model_default_32768_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OCI の配備の状態（v5 の JSON に共通の上限 1200）を読んでも、モデルの既定 32768 で送る。"""
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "version": 5,
                "enterprise_ai": {
                    "connections": [
                        {
                            "connection_id": "primary",
                            "endpoint": _ENDPOINT,
                            "project_ocid": _PROJECT,
                        }
                    ],
                    "models": [{"model_id": _MODEL, "reasoning_effort": "medium"}],
                    "default_text_model_id": _MODEL,
                    "default_vision_model_id": "",
                    "llm_max_output_tokens": 1200,
                    "vlm_max_output_tokens": 65536,
                },
            }
        ),
        encoding="utf-8",
    )
    settings = _settings()
    ModelSettingsStore(
        resolve_path=lambda _settings: tmp_path / "model-settings.json",
        env_file=lambda _settings: tmp_path / ".env",
    ).load(settings)
    assert settings.oci_enterprise_ai_models[0].max_output_tokens == 32_768
    model = _ReasoningModel()
    _mock_http(monkeypatch, model)
    service = _service(settings)

    result = service.generate_comment_sql(_request(service, 5))

    assert result.source == "oci_enterprise_ai"
    assert result.fallback is False
    assert [payload["max_output_tokens"] for payload in model.payloads] == [32_768]
    assert model.payloads[0]["reasoning"] == {"effort": "medium"}
    assert result.sql.count("COMMENT ON TABLE") == 5


def test_incomplete_is_retried_once_with_the_model_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """未完了（max_output_tokens）は、モデルの出力の上限まで広げて 1 回だけ送り直す。"""
    model = _ReasoningModel(tokens_per_target=8_000)
    _mock_http(monkeypatch, model)
    service = _service(_settings(max_input_tokens=1_000_000, max_output_tokens=131_072))

    result = service.generate_comment_sql(_request(service, 5))

    assert result.source == "oci_enterprise_ai"
    assert result.fallback is False
    assert [payload["max_output_tokens"] for payload in model.payloads] == [32_768, 131_072]


def test_many_tables_are_split_into_batches_and_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限の小さいモデルでも、表を組に分けると 1 回の出力が上限に収まる。"""
    model = _ReasoningModel(tokens_per_target=280)
    _mock_http(monkeypatch, model)
    # 12 表を 1 回で呼ぶと 1198 + 280 × 12 = 4558 > 4096 で未完了、10 表 + 2 表なら収まる。
    service = _service(_settings(max_output_tokens=4_096))

    result = service.generate_comment_sql(_request(service, 12))

    assert result.source == "oci_enterprise_ai"
    assert result.fallback is False
    assert sorted(len(_targets_in(payload)) for payload in model.payloads) == [2, 10]
    assert all(payload["max_output_tokens"] == 4_096 for payload in model.payloads)
    assert result.sql.count("COMMENT ON TABLE") == 12
    # 組ごとの入力には、ほかの組の表の塊を入れない。
    for payload in model.payloads:
        content = str(payload["input"][0]["content"])
        targets = _targets_in(payload)
        assert content.count("OBJECT: ") == 2 * len(targets)


def test_annotation_and_domain_are_split_too(monkeypatch: pytest.MonkeyPatch) -> None:
    annotation_model = _ReasoningModel(
        tokens_per_target=280,
        sql_for=lambda targets: "\n".join(
            f"ALTER TABLE {name} ANNOTATIONS (ADD OR REPLACE \"DESCRIPTION\" 'AI の説明');"
            for name in targets
        ),
    )
    _mock_http(monkeypatch, annotation_model)
    service = _service(_settings(max_output_tokens=4_096))
    annotations = service.generate_annotation_sql(_request(service, 12))
    assert annotations.source == "oci_enterprise_ai"
    assert annotations.fallback is False
    assert annotations.sql.count("ANNOTATIONS") == 12

    domain_model = _ReasoningModel(
        tokens_per_target=280,
        sql_for=lambda targets: f"CREATE DOMAIN IF NOT EXISTS D_{len(targets)} AS NUMBER;",
    )
    _mock_http(monkeypatch, domain_model)
    domains = service.generate_domain_sql(_request(service, 12))
    assert domains.source == "oci_enterprise_ai"
    assert domains.fallback is False
    assert [len(_targets_in(payload)) for payload in domain_model.payloads] == [10, 2]
    # ドメインは順に呼び、前の組で作るドメインを次の組に渡す（同じ意味の列で再利用させる）。
    second = str(domain_model.payloads[1]["input"][0]["content"])
    assert "この生成の前の組で作るドメイン" in second
    assert "D_10" in second


def test_failure_shows_reason_limit_and_usage_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """広げられない上限で未完了なら推論の量を下げて 1 回だけ送り直す。

    それでも未完了なら fallback にし、理由・上限・使った token を詳細に出す。
    """
    model = _ReasoningModel(tokens_per_target=200)
    _mock_http(monkeypatch, model)
    service = _service(_settings(max_output_tokens=1_200))

    result = service.generate_comment_sql(_request(service, 5))

    # モデルの上限（1200）より広げられないので、推論の量を下げて（medium → low）送り直す（#2023）。
    assert [payload["max_output_tokens"] for payload in model.payloads] == [1_200, 1_200]
    assert [payload["reasoning"] for payload in model.payloads] == [
        {"effort": "medium"},
        {"effort": "low"},
    ]
    assert result.source == "deterministic"
    assert result.fallback is True
    assert result.fallback_objects == []
    assert "出力の上限" in result.fallback_reason
    assert "理由=max_output_tokens" in result.fallback_detail
    assert "出力の上限=1200 tokens" in result.fallback_detail
    assert "使った出力=1200 tokens（うち推論 300）" in result.fallback_detail
    # 規則ベースの SQL の理由は warnings に重ねない。secret は出さない。
    assert not any("fallback" in warning for warning in result.warnings)
    assert _API_KEY not in result.model_dump_json()


def test_partial_failure_marks_only_the_failed_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _ReasoningModel(tokens_per_target=280, fail_targets=frozenset())
    service = _service(_settings(max_output_tokens=4_096))
    failing = service._db_admin_object_identity("T11").qualified_name
    model.fail_targets = frozenset({failing})
    _mock_http(monkeypatch, model)

    result = service.generate_comment_sql(_request(service, 12))

    assert result.source == "oci_enterprise_ai"
    assert result.fallback is True
    assert result.fallback_objects == [
        service._db_admin_object_identity(name).qualified_name for name in ("T11", "T12")
    ]
    assert result.sql.count("'AI のコメント'") == 10
    assert "T11" in result.sql and "T12" in result.sql
    assert result.fallback_detail.startswith(failing)


def test_client_error_message_after_retry_has_reason_limit_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _ReasoningModel(tokens_per_target=200_000)
    _mock_http(monkeypatch, model)
    client = module.OciEnterpriseAiDirectClient(
        _settings(max_input_tokens=1_000_000, max_output_tokens=131_072)
    )

    with pytest.raises(EnterpriseAiDirectError) as raised:
        client.generate(
            prompt="COMMENT",
            context="targets: table:APP.T1",
            system_prompt="SQL",
            max_output_tokens=4_000,
            response_format=response_format(SqlOutput),
        )

    assert raised.value.code == "incomplete"
    assert raised.value.incomplete is not None
    message = str(raised.value)
    assert "出力の上限を広げ、推論の量を下げて送り直しても" in message
    assert "出力の上限=131072 tokens" in message
    # 最後に送った low より下げられないので、直し方に推論の量を入れない。
    assert "「推論の量」を下げる" not in message
    assert _API_KEY not in message
    assert [payload["max_output_tokens"] for payload in model.payloads] == [
        32_768,
        131_072,
        131_072,
    ]
    assert model.payloads[-1]["reasoning"] == {"effort": "low"}


def test_incomplete_at_the_model_limit_succeeds_with_lower_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上限を広げられないとき、推論の量を下げた送り直しで回答が出る（#2023）。"""
    # medium: 1198 + 200 > 1200 で未完了、low: 300 + 200 <= 1200 で完了。
    model = _ReasoningModel(tokens_per_target=200)
    _mock_http(monkeypatch, model)
    client = module.OciEnterpriseAiDirectClient(_settings(max_output_tokens=1_200))

    text = client.generate(
        prompt="COMMENT",
        context="targets: table:APP.T1",
        system_prompt="SQL",
        response_format=response_format(SqlOutput),
    )

    assert "COMMENT ON TABLE" in text
    assert [payload["reasoning"] for payload in model.payloads] == [
        {"effort": "medium"},
        {"effort": "low"},
    ]


def test_budget_is_capped_by_context_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """出力の上限が入力の上限と同じモデルでも、prompt の分を引いた残りで送る。

    prompt が入らずに失敗するのを防ぐ。
    """
    model = _ReasoningModel(tokens_per_target=1)
    _mock_http(monkeypatch, model)
    client = module.OciEnterpriseAiDirectClient(
        _settings(max_input_tokens=8_192, max_output_tokens=8_192)
    )
    context = "targets: table:APP.T1\n" + "あ" * 3_000

    client.generate(
        prompt="COMMENT",
        context=context,
        system_prompt="SQL",
        response_format=response_format(SqlOutput),
    )

    sent = model.payloads[0]["max_output_tokens"]
    assert 8_192 - 3_100 < sent < 8_192 - 3_000
