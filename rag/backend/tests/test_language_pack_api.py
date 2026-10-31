"""言語資源の下書き・不変な公開版・CAS・検証・試行の API 契約。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rag_parser_core.language import builtin_pack, current_pack

from app.api.routes import language_pack
from app.clients.oracle import OracleClient
from app.main import app
from tests.language_release_api_support import FakeReleaseStore
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


@pytest.fixture(autouse=True)
def release_store(monkeypatch: pytest.MonkeyPatch) -> FakeReleaseStore:
    store = FakeReleaseStore()

    async def initialized() -> FakeReleaseStore:
        return store

    async def count(_self: OracleClient, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(language_pack, "release_store", initialized)
    monkeypatch.setattr(OracleClient, "count_documents", count)
    return store


def test_get_returns_complete_published_snapshot() -> None:
    data = client.get("/api/settings/language-pack").json()["data"]
    assert data["effective"] == data["builtin"]
    assert data["hash"] == builtin_pack().hash
    assert data["current_release"]["effective"] == builtin_pack().to_dict()
    assert data["current_release"]["version"] == 1


def test_draft_comparison_is_read_only_and_bound_to_both_revisions() -> None:
    saved = client.put(
        "/api/settings/language-pack/draft",
        json={
            "overrides": {"entity_cues": {"alias_column_words": ["合成の語"]}},
            "expected_revision": 1,
        },
    )
    assert saved.status_code == 200
    url = "/api/settings/language-pack/draft/comparison"
    query = {"expected_revision": 2, "expected_current_id": "synthetic-1"}
    comparison = client.get(url, params=query)
    assert comparison.status_code == 200
    assert comparison.json()["data"]["affected_stages"] == ["INDEX"]
    assert comparison.json()["data"]["after_id"] == "draft:2"
    assert (
        client.get("/api/settings/language-pack").json()["data"]["current_release"]["version"] == 1
    )
    for wrong in ({**query, "expected_revision": 1}, {**query, "expected_current_id": "previous"}):
        assert client.get(url, params=wrong).status_code == 409


def test_draft_publish_restore_cas_and_paginated_immutable_releases() -> None:
    before = client.get("/api/settings/language-pack").json()["data"]
    saved = client.put(
        "/api/settings/language-pack/draft",
        json={
            "overrides": {"numbers": {"article_units": ["条", " 款 ", "款"]}},
            "expected_revision": 1,
        },
    )
    assert saved.status_code == 200
    state = saved.json()["data"]
    assert state["draft_revision"] == 2
    assert state["draft_effective"]["numbers"]["article_units"] == ["条", "款"]
    assert client.get("/api/settings/language-pack").json()["data"] == before
    assert (
        client.put(
            "/api/settings/language-pack/draft", json={"overrides": {}, "expected_revision": 1}
        ).status_code
        == 409
    )
    payload = {"expected_revision": 2, "expected_current_id": "synthetic-1"}
    published = client.post("/api/settings/language-pack/publish", json=payload)
    assert published.status_code == 200
    assert client.post("/api/settings/language-pack/publish", json=payload).status_code == 409
    assert (
        client.get("/api/settings/language-pack/releases/synthetic-1").json()["data"]
        == before["current_release"]
    )
    page = client.get("/api/settings/language-pack/releases?limit=1&offset=1").json()["data"]
    assert page["total"] == 2 and page["releases"][0]["id"] == "synthetic-1"
    restored = client.post(
        "/api/settings/language-pack/releases/synthetic-1/restore",
        json={"expected_revision": 3, "expected_current_id": "synthetic-2"},
    )
    assert restored.status_code == 200
    now = restored.json()["data"]["current"]
    assert now["id"] == "synthetic-3" and now["restored_from"] == "synthetic-1"
    assert now["effective"] == before["current_release"]["effective"]
    assert (
        client.post(
            "/api/settings/language-pack/releases/missing/restore",
            json={"expected_revision": 4, "expected_current_id": "synthetic-3"},
        ).status_code
        == 404
    )


def test_preview_compares_current_and_draft_without_publishing() -> None:
    response = client.post(
        "/api/settings/language-pack/preview",
        json={
            "text": "第二款 見出し\n第二款を参照",
            "overrides": {"numbers": {"article_units": ["条", "款"]}},
        },
    )
    assert response.status_code == 200
    result = response.json()["data"]
    assert "article:2" in result["draft"]["heading_keys"]
    assert "article:2" not in result["saved"]["heading_keys"]
    assert client.get("/api/settings/language-pack/draft").json()["data"]["draft_revision"] == 1


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("symbols", "bracket_pairs", ["("]),
        ("english", "en_section_words", ["part=volume"]),
        ("symbols", "normalization", "NFKD"),
        ("numbers", "kanji_digits", ["一=いち"]),
        ("numbers", "kanji_multipliers", ["十十=10"]),
        ("symbols", "name_chars", ["あいう"]),
        ("references", "separate_part_patterns", ["改正"]),
        ("references", "abbreviation_targets", ["法"]),
        ("numbers", "unknown_key", ["x"]),
    ],
    ids=[
        "bracket-not-pair",
        "en-kind-unknown",
        "normalization-unknown",
        "kanji-not-number",
        "multiplier-not-one-char",
        "name-char-not-range",
        "pattern-without-number",
        "map-without-value",
        "unknown-field",
    ],
)
def test_put_rejects_malformed_values(section: str, key: str, value: object) -> None:
    resp = client.put(
        "/api/settings/language-pack/draft",
        json={"overrides": {section: {key: value}}, "expected_revision": 1},
    )
    assert resp.status_code == 422
    assert f"{section}.{key}" in json.dumps(resp.json(), ensure_ascii=False)
    assert current_pack().hash == builtin_pack().hash


def test_every_field_has_screen_label_and_help() -> None:
    """言語資源の項目は、画面の名前と説明(frontend の i18n)を持つ(#2181)。"""
    import re

    from rag_parser_core.language import FIELD_SPECS, SECTIONS

    i18n = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "i18n.ts").read_text(
        encoding="utf-8"
    )
    keys = set(re.findall(r'^\s*"(settings\.language\.[^"]+)":', i18n, re.MULTILINE))
    missing = [
        key
        for spec in FIELD_SPECS
        for key in (
            f"settings.language.field.{spec.key}",
            f"settings.language.field.{spec.key}.help",
        )
        if key not in keys
    ]
    missing += [
        key
        for section in SECTIONS
        for key in (
            f"settings.language.section.{section}",
            f"settings.language.section.{section}.help",
        )
        if key not in keys
    ]
    assert missing == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("roman_digits", ["i=1", "v=0"]),
        ("kanji_multipliers", ["十=0"]),
        ("kanji_digits", ["一=²"]),
        ("kanji_digits", ["一=１"]),
    ],
)
def test_numeric_mapping_rejection_preserves_saved_pack(key: str, value: list[str]) -> None:
    accepted = client.put(
        "/api/settings/language-pack/draft",
        json={"overrides": {"numbers": {"article_units": ["款"]}}, "expected_revision": 1},
    )
    assert accepted.status_code == 200
    before = client.get("/api/settings/language-pack/draft").json()["data"]
    response = client.put(
        "/api/settings/language-pack/draft",
        json={"overrides": {"numbers": {key: value}}, "expected_revision": 2},
    )
    assert response.status_code == 422
    assert f"numbers.{key}" in json.dumps(response.json())
    assert client.get("/api/settings/language-pack/draft").json()["data"] == before


def test_numeric_mapping_metadata_exposes_minimum() -> None:
    fields = client.get("/api/settings/language-pack").json()["data"]["fields"]
    minimums = {f["key"]: f["min_value"] for f in fields if f["section"] == "numbers"}
    assert minimums["kanji_digits"] == 0
    assert minimums["roman_digits"] == minimums["kanji_multipliers"] == 1
