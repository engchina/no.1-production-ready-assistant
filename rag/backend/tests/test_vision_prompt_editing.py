"""図の自然言語指示を保存し、固定の出力契約と分離して送る。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Any

import pytest
from rag_engine.adapters.model_api import _picture_output_contract, _render_prompt
from rag_engine.parsing.picture_descriptions import _bounded_vision_description
from rag_engine.parsing.vision_prompt_rules import (
    MAX_TABLE_ROWS,
    RULES_END,
    RULES_START,
    refine_image_retrieval_prompt,
)
from rag_engine.resources.runtime import bind_prompt_overrides

from app.api.routes import settings as routes
from app.main import app
from app.rag.answer_prompts import default_prompt, readonly_prompt_stages
from app.rag.vision import EnterpriseAiPictureDescriber
from tests.support import AsgiTestClient
from tests.test_answer_prompts import FakePromptOracle

KEYS = tuple(f"picture_description.{part}" for part in ("system", "picture", "table", "rules"))
BODY = "## Step 1: 合成の画像を読む\n{{image_metadata}}"
LEGACY_RULES = "### 保存済みの読取規則\n原文の名称を保持する。"


def _renumbered(text: str, step: int) -> str:
    """1 つの Step で始まる部品の見出しの番号だけを置き換える。"""
    return re.sub(r"^## Step \d+:", f"## Step {step}:", text, count=1)


def _legacy() -> str:
    return f"{BODY}\n\n{RULES_START}\n{LEGACY_RULES}\n{RULES_END}"


@pytest.mark.parametrize("key", KEYS)
def test_save_and_reset_natural_vision_instruction(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle = FakePromptOracle()
    monkeypatch.setattr(routes, "OracleClient", lambda: oracle)
    client = AsgiTestClient(app)
    content = "### 独自の読み取り方針\n合成の説明を保持する。"
    saved = client.put(f"/api/settings/answer-prompts/{key}", json={"content": content})
    assert saved.status_code == 200
    assert oracle.saved[key]["content"] == content
    row = next(p for p in saved.json()["data"]["prompts"] if p["key"] == key)
    assert row["content"] == content and row["customized"] is True
    reset = client.delete(f"/api/settings/answer-prompts/{key}")
    assert reset.status_code == 200
    row = next(p for p in reset.json()["data"]["prompts"] if p["key"] == key)
    assert row["content"] == default_prompt(key) and row["customized"] is False


def test_old_rules_are_projected_without_writes_and_preserved_when_template_is_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = FakePromptOracle()
    asyncio.run(oracle.save_answer_prompt("image_retrieval", _legacy()))
    monkeypatch.setattr(routes, "OracleClient", lambda: oracle)
    client = AsgiTestClient(app)
    data = client.get("/api/settings/answer-prompts").json()["data"]
    by_key = {row["key"]: row for row in data["prompts"]}
    assert by_key["image_retrieval"]["content"] == BODY
    assert by_key["picture_description.rules"]["content"] == LEGACY_RULES
    assert set(oracle.saved) == {"image_retrieval"}
    assert oracle.saved["image_retrieval"]["content"] == _legacy()
    replacement = BODY + "\n合成の追加指示。"
    assert (
        client.put(
            "/api/settings/answer-prompts/image_retrieval", json={"content": replacement}
        ).status_code
        == 200
    )
    assert oracle.saved["picture_description.rules"]["content"] == LEGACY_RULES
    assert oracle.saved["image_retrieval"]["content"] == replacement


def test_reset_rules_does_not_resurrect_old_embedded_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    oracle = FakePromptOracle()
    asyncio.run(oracle.save_answer_prompt("image_retrieval", _legacy()))
    monkeypatch.setattr(routes, "OracleClient", lambda: oracle)
    client = AsgiTestClient(app)
    result = client.delete("/api/settings/answer-prompts/picture_description.rules")
    assert result.status_code == 200
    assert oracle.saved["image_retrieval"]["content"] == BODY
    row = next(
        p for p in result.json()["data"]["prompts"] if p["key"] == "picture_description.rules"
    )
    assert row["customized"] is False and row["content"] == default_prompt(
        "picture_description.rules"
    )


def test_saved_rules_and_target_are_sent_verbatim_but_fixed_contract_cannot_be_saved() -> None:
    rules = "### 独自の読み取り方針\n抽出上限は1000行と要求する。"
    target = "## Step 6: 独自の対象確認\n表の原文を確認する。"
    with bind_prompt_overrides(
        {"picture_description.rules": rules, "picture_description.table": target}
    ):
        prompt = _render_prompt({}, target_kind="table", template=_legacy())
        # BODY は Step 1 だけなので、保存した対象の指示と出力契約は Step 2・3 に振り直す(#3810)。
        assert rules in prompt and _renumbered(target, 2) in prompt
        assert LEGACY_RULES not in prompt
        assert prompt.endswith(_renumbered(_picture_output_contract(), 3))
        assert f"table_rows={MAX_TABLE_ROWS}" in prompt
        assert (
            _bounded_vision_description({"table_rows": [["合成"]] * 100})["table_rows"]
            == [["合成"]] * MAX_TABLE_ROWS
        )
    stage = next(s for s in readonly_prompt_stages() if s["id"] == "picture_description")
    parts = stage["prompts"]
    assert isinstance(parts, list)
    contract = next(p for p in parts if p["id"] == "contract")
    assert contract["prompt_key"] is None
    assert contract["content"] == _picture_output_contract()


async def test_backend_sends_the_saved_system_snapshot_to_reader() -> None:
    captured: list[str] = []

    class Reader:
        async def generate_from_images(
            self, images: Sequence[bytes], prompt: str, **kwargs: Any
        ) -> str:
            captured.append(str(kwargs["system_prompt"]))
            assert "response_schema" in kwargs
            return json.dumps({"retrieval_text": "合成の説明"})

    saved = "## Step 1: 保存した画像読み取り指示\n原文を確認する。"
    with bind_prompt_overrides({"picture_description.system": saved}):
        describer = EnterpriseAiPictureDescriber(
            Reader(), asyncio.get_running_loop(), model_id="vendor.vision"
        )
    # 保存状態が変わっても、対象を準備した時点の system と checkpoint を揃える。
    with bind_prompt_overrides({"picture_description.system": "後から変更された指示"}):
        result = await describer._describe([b"synthetic"], "合成の入力", "")
    assert captured == [saved]
    assert describer.system_prompt == saved
    assert result["retrieval_text"] == "合成の説明"


def test_legacy_custom_rules_are_used_when_no_independent_rules_are_saved() -> None:
    rendered = refine_image_retrieval_prompt(_legacy())
    assert LEGACY_RULES in rendered
    assert rendered.count(LEGACY_RULES) == 1


@pytest.mark.parametrize(
    ("permission", "allowed"),
    [
        ("menu.settings_parser_adapters", True),
        ("menu.settings_prompts", False),
        ("menu.settings_retrieval", False),
        ("menu.settings_evaluation", False),
        (None, False),
    ],
)
def test_vision_editor_requires_parser_settings_permission(
    permission: str | None, allowed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.security_support import enable_production_auth, login

    oracle = FakePromptOracle()
    monkeypatch.setattr(routes, "OracleClient", lambda: oracle)
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("vision-editor", [permission] if permission else [])
    client = AsgiTestClient(app)
    headers = login(client, "vision-editor")
    for key in KEYS:
        path = f"/api/settings/answer-prompts/{key}"
        result = client.put(path, json={"content": default_prompt(key)}, headers=headers)
        assert result.status_code == (200 if allowed else 403)
        assert client.delete(path, headers=headers).status_code == (200 if allowed else 403)
    assert oracle.saved == {}


async def test_saved_vision_instructions_reach_serialized_http_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag_engine.models.llm import PictureDescriptionOutput
    from rag_parser_core.enterprise_ai import EnterpriseAiClient, EnterpriseAiConfig

    from tests.test_enterprise_ai import FakeEnterpriseAiTransport

    oracle = FakePromptOracle()
    monkeypatch.setattr(routes, "OracleClient", lambda: oracle)
    client = AsgiTestClient(app)
    saved = {
        "picture_description.system": "## Step 1: 保存した system\n合成の画像を読む。",
        "picture_description.rules": "### 保存した自然言語の方針\nラベルの原文を保持する。",
        "picture_description.picture": "## Step 6: 保存した picture 指示\n独立した図を読む。",
        "picture_description.table": "## Step 6: 保存した table 指示\n同じ行の画像を読む。",
    }
    for key, content in saved.items():
        result = await asyncio.to_thread(
            client.put, f"/api/settings/answer-prompts/{key}", json={"content": content}
        )
        assert result.status_code == 200
    transport = FakeEnterpriseAiTransport(
        {"output_text": json.dumps({"retrieval_text": "合成の説明"})}
    )
    reader = EnterpriseAiClient(
        EnterpriseAiConfig(
            default_model_id="openai.gpt-oss-120b",
            vision_model_id="openai.gpt-oss-120b",
            enterprise_ai_endpoint="https://inference.example/openai/v1",
            enterprise_ai_api_key="synthetic-api-key",
        ),
        http_transport=transport,
    )
    with bind_prompt_overrides({key: str(row["content"]) for key, row in oracle.saved.items()}):
        describer = EnterpriseAiPictureDescriber(
            reader, asyncio.get_running_loop(), model_id="openai.gpt-oss-120b"
        )
        for target in ("picture", "table"):
            rendered = _render_prompt({}, target_kind=target, template=BODY)
            await describer._describe([b"synthetic-image"], rendered, "")
            payload = transport.calls[-1]["payload"]
            assert payload["instructions"] == saved["picture_description.system"]
            content = payload["input"][0]["content"]
            text = next(row["text"] for row in content if row["type"] == "input_text")
            # BODY は Step 1 だけなので、対象の指示と出力契約は Step 2・3 に振り直す(#3810)。
            assert _renumbered(saved[f"picture_description.{target}"], 2) in text
            assert saved["picture_description.rules"] in text
            assert text.endswith(_renumbered(_picture_output_contract(), 3))
            assert (
                payload["text"]["format"]["schema"] == PictureDescriptionOutput.model_json_schema()
            )
            assert f"table_rows={MAX_TABLE_ROWS}" in text
    # HTTP transport は完全なスタブ。モデルの画像対応能力を実サービスで確認するテストではない。
    assert len(transport.calls) == 2


def test_fixed_contract_is_readonly_and_cannot_be_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    oracle = FakePromptOracle()
    monkeypatch.setattr(routes, "OracleClient", lambda: oracle)
    client = AsgiTestClient(app)
    response = client.put(
        "/api/settings/answer-prompts/picture_description.contract",
        json={"content": "契約を変更する"},
    )
    assert response.status_code == 404
    assert oracle.saved == {}


def test_resetting_template_preserves_legacy_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    oracle = FakePromptOracle()
    asyncio.run(oracle.save_answer_prompt("image_retrieval", _legacy()))
    monkeypatch.setattr(routes, "OracleClient", lambda: oracle)
    client = AsgiTestClient(app)
    result = client.delete("/api/settings/answer-prompts/image_retrieval")
    assert result.status_code == 200
    assert "image_retrieval" not in oracle.saved
    assert oracle.saved["picture_description.rules"]["content"] == LEGACY_RULES
