"""検索・回答プロファイルの言語資源の上書き(検索・回答の項目だけ。#2181 段 6)。"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from rag_parser_core.language import current_pack

from app.config import get_settings
from app.rag import pipeline as pipeline_module
from app.rag.search_answer_profile_config import (
    SearchAnswerProfileConfig,
    resolve_search_answer_profile_settings,
)
from app.schemas.search import SearchRequest, SearchResponse


def test_profile_accepts_only_search_and_answer_fields() -> None:
    config = SearchAnswerProfileConfig(language_pack={"question_cues": {"cause_cues": ["理屈"]}})
    assert config.language_pack == {"question_cues": {"cause_cues": ["理屈"]}}
    with pytest.raises(ValidationError, match="取込の項目"):
        SearchAnswerProfileConfig(language_pack={"numbers": {"article_units": ["条"]}})
    with pytest.raises(ValidationError, match="知らない項目"):
        SearchAnswerProfileConfig(language_pack={"question_cues": {"nothing": ["x"]}})
    with pytest.raises(ValidationError):
        SearchAnswerProfileConfig(language_pack={"answer_labels": {"citation_prefix": ""}})


def test_profile_overrides_reach_settings_and_are_bound_during_the_request() -> None:
    config = SearchAnswerProfileConfig(language_pack={"question_cues": {"cause_cues": ["理屈"]}})
    settings, applied = resolve_search_answer_profile_settings(get_settings(), config)
    assert applied
    assert settings.rag_language_pack_overrides == {"question_cues": {"cause_cues": ["理屈"]}}

    seen: list[tuple[str, ...]] = []

    class _Probe(pipeline_module.RagPipeline):
        async def _run(self, *args: object, **kwargs: object) -> SearchResponse:
            seen.append(current_pack().words("question_cues", "cause_cues"))
            return SearchResponse(answer="", trace_id="synthetic", elapsed_ms=0)

    probe = _Probe.__new__(_Probe)
    probe._settings = settings
    probe._run_recorder = None
    asyncio.run(probe.run(SearchRequest(query="合成の質問")))
    assert seen == [("理屈",)]
    assert current_pack().words("question_cues", "cause_cues") != ("理屈",)
