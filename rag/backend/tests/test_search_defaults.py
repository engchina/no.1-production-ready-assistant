"""省略した件数は現在の環境設定、明示した件数は利用者の指定から解決する。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app import config
from app.config import Settings
from app.mcp.tools import EVIDENCE_LIMIT_MAX, RetrieveEvidenceInput, SearchInput
from app.schemas.chat import ChatMessageRequest
from app.schemas.search import SearchRequest


@pytest.mark.parametrize("value", [1, 37, 50, 100])
def test_omitted_top_k_and_public_schema_use_current_setting(
    value: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None, rag_search_top_k_default=value)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    requests: list[SearchRequest | ChatMessageRequest | SearchInput] = [
        SearchRequest(query="q"),
        ChatMessageRequest(content="q"),
        SearchInput(query="q"),
        RetrieveEvidenceInput(query="q"),
    ]
    assert [request.top_k for request in requests] == [value] * len(requests)
    assert RetrieveEvidenceInput(query="q").evidence_limit == min(value, EVIDENCE_LIMIT_MAX)
    for model in (SearchRequest, ChatMessageRequest, SearchInput, RetrieveEvidenceInput):
        assert model.model_json_schema()["properties"]["top_k"]["default"] == value
    assert SearchRequest(query="q", top_k=12).top_k == 12
    assert ChatMessageRequest(content="q", top_k=12).top_k == 12
    assert SearchInput(query="q", top_k=12).top_k == 12
    assert RetrieveEvidenceInput(query="q", top_k=12, evidence_limit=4).evidence_limit == 4


@pytest.mark.parametrize("value", ["0", "101", "invalid"])
def test_invalid_environment_value_is_rejected(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_SEARCH_TOP_K_DEFAULT", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", [0, 101, "invalid"])
def test_invalid_explicit_value_is_rejected(value: int | str) -> None:
    for model, field in (
        (SearchRequest, "query"),
        (ChatMessageRequest, "content"),
        (SearchInput, "query"),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({field: "q", "top_k": value})


def test_default_is_not_frozen_at_import_or_after_file_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.env"
    monkeypatch.delenv("RAG_SEARCH_TOP_K_DEFAULT", raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=path))
    path.write_text("RAG_SEARCH_TOP_K_DEFAULT=31\n")
    assert SearchRequest(query="q").top_k == 31
    path.write_text("RAG_SEARCH_TOP_K_DEFAULT=17\n")
    assert ChatMessageRequest(content="q").top_k == 17
    assert SearchInput(query="q").top_k == 17
    path.write_text("")
    assert SearchRequest(query="q").top_k == 50
