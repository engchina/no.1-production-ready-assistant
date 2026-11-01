"""検索・回答プロファイルの窓口の上書きと固定した窓口の案内。

回答の注意書き（#2405）の窓口の案内の 1 行を、プロファイルの上書き・業務ガイドの引き継ぎで決める。
同じ窓口の案内を 2 度出さない。外部 I/O はスタブ（`tests.test_answer_surfaces` と同じ）。
"""

from __future__ import annotations

from typing import Any

import pytest
from pr_system_settings.answer_notice import (
    DEFAULT_ANSWER_NOTICE_BODY,
    DEFAULT_ANSWER_NOTICE_PARTS,
    DEFAULT_ANSWER_NOTICE_TEXT,
    AnswerNoticeParts,
)
from pydantic import ValidationError
from pytest import MonkeyPatch

from app.config import Settings
from app.rag.answer_notice import (
    GUIDE_HANDOFF_CONTACT_KEY,
    compose_rag_answer_notice,
)
from app.rag.search_answer_profile_config import (
    CONTACT_OVERRIDE_MAX_CHARS,
    SearchAnswerProfileConfig,
    resolve_search_answer_profile_settings,
)
from tests.test_answer_engine import _fake_llm
from tests.test_answer_notice import _install, _local_service, client

PARTS = AnswerNoticeParts(body="本文の 1 行目", contact="システムの窓口")
SYSTEM_LINE = "解決しない場合は、システムの窓口へお問い合わせください。"


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch: MonkeyPatch) -> None:
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)


def test_without_override_uses_the_system_notice() -> None:
    """プロファイルに上書きが無ければ #2405 の注意書きのまま。"""
    assert (
        compose_rag_answer_notice(PARTS, {"outcome": "answered"}) == f"本文の 1 行目\n{SYSTEM_LINE}"
    )
    assert (
        compose_rag_answer_notice(DEFAULT_ANSWER_NOTICE_PARTS, {"outcome": "insufficient_evidence"})
        == DEFAULT_ANSWER_NOTICE_TEXT
    )


def test_override_replaces_the_contact_line_once() -> None:
    notice = compose_rag_answer_notice(
        PARTS, {"outcome": "insufficient_evidence"}, contact_override="窓口 B（内線 2）"
    )
    assert notice == "本文の 1 行目\n解決しない場合は、窓口 B（内線 2）へお問い合わせください。"
    # システムの窓口の案内を重ねない。
    assert notice is not None and "システムの窓口" not in notice
    assert notice.count("お問い合わせください") == 1


@pytest.mark.parametrize(
    ("guide_contact", "expected_line"),
    [
        ("窓口 C", None),
        ("", "解決しない場合は、窓口 Bへお問い合わせください。"),
    ],
    ids=["guide-contact", "guide-without-contact"],
)
def test_guide_handoff_contact_takes_priority(
    guide_contact: str, expected_line: str | None
) -> None:
    """業務ガイドの引き継ぎの窓口があれば、回答の本文がそれを案内するので注意書きに重ねない。"""
    guide = {"decision": "handoff", GUIDE_HANDOFF_CONTACT_KEY: guide_contact}
    notice = compose_rag_answer_notice(
        PARTS, {"outcome": "needs_human"}, contact_override="窓口 B", guide=guide
    )
    assert notice == (
        "本文の 1 行目" if expected_line is None else f"本文の 1 行目\n{expected_line}"
    )


def test_guide_contact_is_ignored_when_the_answer_is_not_the_handoff() -> None:
    """引き継ぎでない回答（モデルの回答）では、ガイドの窓口を案内しないので窓口の行を出す。"""
    guide = {"decision": "proceed", GUIDE_HANDOFF_CONTACT_KEY: "窓口 C"}
    notice = compose_rag_answer_notice(PARTS, {"outcome": "answered"}, guide=guide)
    assert notice == f"本文の 1 行目\n{SYSTEM_LINE}"


@pytest.mark.parametrize(
    "outcome",
    ["answered", "conditional", "insufficient_evidence", "needs_environment_data", "needs_human"],
)
def test_contact_form_is_fixed_for_all_answer_outcomes(outcome: str) -> None:
    notice = compose_rag_answer_notice(PARTS, {"outcome": outcome}, contact_override="窓口 B")
    assert notice is not None
    assert notice.splitlines()[-1] == "解決しない場合は、窓口 Bへお問い合わせください。"
    assert notice.count("お問い合わせください") == 1


@pytest.mark.parametrize("legacy_value", [True, False, None])
def test_removed_contact_policy_is_ignored_and_not_saved(legacy_value: bool | None) -> None:
    config = SearchAnswerProfileConfig.model_validate({"spec_then_contact": legacy_value})
    assert "spec_then_contact" not in config.model_dump()
    settings, _ = resolve_search_answer_profile_settings(Settings(), config)
    assert not hasattr(settings, "rag_answer_spec_then_contact")
    notice = compose_rag_answer_notice(PARTS, {"outcome": "answered"})
    assert notice == f"本文の 1 行目\n{SYSTEM_LINE}"


def test_no_notice_for_clarification_or_disabled() -> None:
    assert compose_rag_answer_notice(PARTS, {"outcome": "needs_clarification"}) is None
    assert (
        compose_rag_answer_notice(None, {"outcome": "answered"}, contact_override="窓口 B") is None
    )


def test_profile_config_normalizes_and_limits_the_contact_override() -> None:
    config = SearchAnswerProfileConfig(contact_override="  窓口 B\n（内線 2）  ")
    assert config.contact_override == "窓口 B （内線 2）"
    with pytest.raises(ValidationError):
        SearchAnswerProfileConfig(contact_override="あ" * (CONTACT_OVERRIDE_MAX_CHARS + 1))


def test_profile_settings_carry_the_contact_override() -> None:
    settings, _ = resolve_search_answer_profile_settings(
        Settings(), SearchAnswerProfileConfig(contact_override="窓口 B")
    )
    assert settings.rag_answer_contact_override == "窓口 B"
    plain, _ = resolve_search_answer_profile_settings(Settings(), SearchAnswerProfileConfig())
    assert plain.rag_answer_contact_override == ""


def test_search_api_uses_the_profile_contact_override(monkeypatch: MonkeyPatch) -> None:
    oracle = _install(monkeypatch)
    oracle._views["bv-1"] = SearchAnswerProfileConfig.model_validate(  # noqa: SLF001 - テストのスタブ
        {
            "knowledge_base_ids": ["kb-1"],
            "contact_override": "窓口 B（内線 2）",
            "spec_then_contact": True,
        }
    )
    _local_service()
    response = client.post(
        "/api/search", json={"query": "受注の登録方法は？", "search_answer_profile_id": "bv-1"}
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    assert data["notice"].startswith(DEFAULT_ANSWER_NOTICE_BODY)
    assert data["notice"].endswith("解決しない場合は、窓口 B（内線 2）へお問い合わせください。")
    assert "サポート窓口" not in data["notice"]
    assert oracle.saved[-1]["answer_notice"] == data["notice"]
