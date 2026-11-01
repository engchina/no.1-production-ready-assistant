"""検索 API の Search Answer Profile 解決テスト。

Search Answer Profile 指定時に参照 KB 群を検索対象へ展開し、その回答の設定を
検索 runtime / diagnostics へ反映することを検証する。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from app.api.routes import search as search_route
from app.config import Settings, get_settings
from app.main import app
from app.rag import extraction_field_adapter as fields_mod
from app.rag.diagnostics import build_search_diagnostics
from app.rag.extraction_field_adapter import FieldDefinition
from app.rag.kb_adapter_config import KnowledgeBaseAdapterConfig, KnowledgeBaseQueryConfig
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.schemas.knowledge_base import KnowledgeBaseDetail, KnowledgeBaseStatus
from app.schemas.search import SearchRequest, SearchResponse
from app.schemas.search_answer_profile import SearchAnswerProfileDetail, SearchAnswerProfileStatus
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


class RecordingPipeline:
    """構築時 settings と実行時 request を捕捉するテスト用 pipeline。"""

    captured_settings: Settings | None = None
    captured_request: SearchRequest | None = None

    def __init__(self, *, settings: Settings | None = None, **_kwargs: object) -> None:
        RecordingPipeline.captured_settings = settings
        self._settings = settings

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: object | None = None,
        token_callback: object | None = None,
        trace: object | None = None,
    ) -> SearchResponse:
        _ = progress_callback, token_callback
        RecordingPipeline.captured_request = request
        settings = self._settings
        assert trace_id
        return SearchResponse(
            answer="ok",
            citations=[],
            trace_id=trace_id,
            elapsed_ms=1.0,
            diagnostics=build_search_diagnostics(
                request, settings=settings, retrieval_strategy_adapter="grounded"
            ),
        )


class FakeViewOracle:
    """検索・回答プロファイルを返すテスト用 Oracle。"""

    def __init__(self, views: dict[str, SearchAnswerProfileConfig]) -> None:
        self._views = views

    async def get_search_answer_profile_knowledge(
        self, search_answer_profile_id: str, kind: str
    ) -> dict[str, object] | None:
        return None

    async def get_search_answer_profile(
        self, search_answer_profile_id: str
    ) -> SearchAnswerProfileDetail | None:
        config = self._views.get(search_answer_profile_id)
        if config is None:
            return None
        return SearchAnswerProfileDetail(
            id=search_answer_profile_id,
            name=f"view {search_answer_profile_id}",
            status=SearchAnswerProfileStatus.ACTIVE,
            config=config,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def get_knowledge_base(self, knowledge_base_id: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset() -> None:
    RecordingPipeline.captured_settings = None
    RecordingPipeline.captured_request = None


def _install(monkeypatch: MonkeyPatch, views: dict[str, SearchAnswerProfileConfig]) -> None:
    monkeypatch.setattr(search_route, "RagPipeline", RecordingPipeline)
    monkeypatch.setattr(
        search_route,
        "OracleClient",
        lambda *_args, **_kwargs: FakeViewOracle(views),
    )


def test_search_answer_profile_expands_kbs_and_applies_query_config(
    monkeypatch: MonkeyPatch,
) -> None:
    """参照 KB 群が検索対象へ展開され、query 設定が pipeline と diagnostics に効く。"""
    config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1", "kb-2"],
        query=KnowledgeBaseQueryConfig(
            query_strategy="rag_fusion",
            answer_flow="standard_rag",
        ),
    )
    _install(monkeypatch, {"bv-1": config})

    response = client.post(
        "/api/search",
        json={"query": "経費精算の上限", "search_answer_profile_id": "bv-1"},
    )

    assert response.status_code == 200
    diagnostics = response.json()["data"]["diagnostics"]
    assert RecordingPipeline.captured_settings is not None
    assert (
        RecordingPipeline.captured_settings.rag_query_strategy == get_settings().rag_query_strategy
    )
    assert RecordingPipeline.captured_settings.rag_answer_flow == get_settings().rag_answer_flow
    assert diagnostics["search_answer_profile_applied"] == "bv-1"
    # 参照 KB が検索対象へ展開されている。
    assert RecordingPipeline.captured_request is not None
    assert RecordingPipeline.captured_request.knowledge_base_ids == ["kb-1", "kb-2"]
    assert RecordingPipeline.captured_request.filters["knowledge_base_id"] == "kb-1,kb-2"


@pytest.mark.parametrize("path", ["/api/search", "/api/search/stream"])
def test_search_answer_profile_ids_is_rejected(monkeypatch: MonkeyPatch, path: str) -> None:
    """検索・回答プロファイルは 1 つだけ。削除した search_answ
    er_profile_ids は読み捨てず 422 にする（#635）。"""
    _install(monkeypatch, {"bv-1": SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"])})

    response = client.post(
        path, json={"query": "経費精算の上限", "search_answer_profile_ids": ["bv-1"]}
    )

    assert response.status_code == 422
    assert RecordingPipeline.captured_request is None


def test_saved_standard_options_of_search_answer_profile_are_ignored(
    monkeypatch: MonkeyPatch,
) -> None:
    """保存済みの旧 standard の値(回答スタイル・persona など。#595 で削除)は読み捨てて検索する。"""
    config = SearchAnswerProfileConfig.model_validate(
        {
            "knowledge_base_ids": ["kb-1"],
            "system_prompt": "あなたは経理規程アシスタントです。",
            "default_language": "en",
            "query": {
                "retrieval_strategy": "keyword",
                "generation_profile": "structured_json",
                "post_retrieval_pipeline": "lean",
                "rerank_enabled": False,
            },
        }
    )
    _install(monkeypatch, {"bv-1": config})

    response = client.post(
        "/api/search",
        json={"query": "上限額", "search_answer_profile_id": "bv-1"},
    )

    assert response.status_code == 200
    settings = RecordingPipeline.captured_settings
    assert settings is not None
    assert settings.rag_rerank_enabled == get_settings().rag_rerank_enabled
    for removed in ("rag_generation_profile", "rag_generation_system_prompt_override"):
        assert not hasattr(settings, removed)


def test_search_answer_profile_guardrail_policy_reaches_pipeline_settings(
    monkeypatch: MonkeyPatch,
) -> None:
    """検索・回答プロファイルの regulated 設定が実 pipeline 構築に使う Settings へ届く。"""
    config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1"],
        query=KnowledgeBaseQueryConfig(guardrail_policy="regulated"),
    )
    _install(monkeypatch, {"bv-1": config})

    response = client.post(
        "/api/search",
        json={"query": "上限額", "search_answer_profile_id": "bv-1"},
    )

    assert response.status_code == 200
    assert RecordingPipeline.captured_settings is not None
    assert not hasattr(RecordingPipeline.captured_settings, "rag_guardrails_input_enabled")


def test_request_kb_ids_take_precedence_over_view(monkeypatch: MonkeyPatch) -> None:
    """request 明示の KB は検索・回答プロファイルの参照 KB より優先する。"""
    config = SearchAnswerProfileConfig(knowledge_base_ids=["kb-1", "kb-2"])
    _install(monkeypatch, {"bv-1": config})

    response = client.post(
        "/api/search",
        json={
            "query": "上限額",
            "search_answer_profile_id": "bv-1",
            "knowledge_base_ids": ["kb-9"],
        },
    )

    assert response.status_code == 200
    assert RecordingPipeline.captured_request is not None
    assert RecordingPipeline.captured_request.knowledge_base_ids == ["kb-9"]


@pytest.mark.parametrize("path", ["/api/search", "/api/search/stream"])
def test_search_answer_profile_without_knowledge_bases_is_rejected(
    monkeypatch: MonkeyPatch, path: str
) -> None:
    """参照 KB が 0 件の検索・回答プロファイルでは利用者の
    全 KB を検索せず、理由を 409 で返す（#304）。"""
    _install(monkeypatch, {"bv-empty": SearchAnswerProfileConfig()})

    response = client.post(path, json={"query": "上限額", "search_answer_profile_id": "bv-empty"})

    assert response.status_code == 409
    assert response.json()["error_messages"] == [
        search_route.SEARCH_ANSWER_PROFILE_NO_KNOWLEDGE_BASES_MESSAGE
    ]
    assert RecordingPipeline.captured_request is None


def test_missing_search_answer_profile_is_rejected(monkeypatch: MonkeyPatch) -> None:
    """明示した検索・回答プロファイルが無い場合は別 scope へ縮退せず 404 にする。"""
    _install(monkeypatch, {})

    response = client.post(
        "/api/search",
        json={"query": "上限額", "search_answer_profile_id": "missing"},
    )

    assert response.status_code == 404
    assert response.json()["error_messages"] == [
        "指定した検索・回答プロファイルが見つかりません: missing"
    ]


def test_archived_search_answer_profile_is_rejected(monkeypatch: MonkeyPatch) -> None:
    class ArchivedOracle(FakeViewOracle):
        async def get_search_answer_profile(
            self, search_answer_profile_id: str
        ) -> SearchAnswerProfileDetail | None:
            detail = await super().get_search_answer_profile(search_answer_profile_id)
            return (
                detail.model_copy(update={"status": SearchAnswerProfileStatus.ARCHIVED})
                if detail
                else None
            )

    monkeypatch.setattr(search_route, "RagPipeline", RecordingPipeline)
    monkeypatch.setattr(
        search_route,
        "OracleClient",
        lambda *_args, **_kwargs: ArchivedOracle({"bv-1": SearchAnswerProfileConfig()}),
    )

    response = client.post(
        "/api/search",
        json={"query": "上限額", "search_answer_profile_id": "bv-1"},
    )

    assert response.status_code == 409
    assert "アーカイブ済み" in response.json()["error_messages"][0]


def test_search_answer_profile_flows_to_settings_and_diagnostics(
    monkeypatch: MonkeyPatch,
) -> None:
    """検索・回答プロファイルの設定が pipeline settings と診断へ流れる。"""
    config = SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"])
    _install(monkeypatch, {"bv-1": config})

    response = client.post(
        "/api/search",
        json={"query": "上限額", "search_answer_profile_id": "bv-1"},
    )

    assert response.status_code == 200
    settings = RecordingPipeline.captured_settings
    assert settings is not None
    diagnostics = response.json()["data"]["diagnostics"]
    assert diagnostics["search_answer_profile_applied"] == "bv-1"


class FakeViewAndKbOracle:
    """Search Answer Profile と、その参照 KB(legacy query 付き)を返すテスト用 Oracle。"""

    def __init__(
        self,
        views: dict[str, SearchAnswerProfileConfig],
        kb_configs: dict[str, KnowledgeBaseAdapterConfig],
    ) -> None:
        self._views = views
        self._kb_configs = kb_configs

    async def get_search_answer_profile_knowledge(
        self, search_answer_profile_id: str, kind: str
    ) -> dict[str, object] | None:
        return None

    async def get_search_answer_profile(
        self, search_answer_profile_id: str
    ) -> SearchAnswerProfileDetail | None:
        config = self._views.get(search_answer_profile_id)
        if config is None:
            return None
        return SearchAnswerProfileDetail(
            id=search_answer_profile_id,
            name=f"view {search_answer_profile_id}",
            status=SearchAnswerProfileStatus.ACTIVE,
            config=config,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseDetail | None:
        config = self._kb_configs.get(knowledge_base_id)
        if config is None:
            return None
        return KnowledgeBaseDetail(
            id=knowledge_base_id,
            name=f"KB {knowledge_base_id}",
            status=KnowledgeBaseStatus.ACTIVE,
            adapter_config=config,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_search_answer_profile_ignores_single_kb_legacy_query(monkeypatch: MonkeyPatch) -> None:
    """Search Answer Profile は単一 KB に解決しても KB legacy query を下層に重ねない。"""
    kb_config = KnowledgeBaseAdapterConfig.model_validate(
        {"query": {"vector_index_profile": "fast", "neighbor_child_count": 9}}
    )
    profile_config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1"],
        query=KnowledgeBaseQueryConfig(answer_flow="standard_rag"),
    )
    monkeypatch.setattr(search_route, "RagPipeline", RecordingPipeline)
    monkeypatch.setattr(
        search_route,
        "OracleClient",
        lambda *_args, **_kwargs: FakeViewAndKbOracle(
            {"bv-1": profile_config},
            {"kb-1": kb_config},
        ),
    )

    response = client.post(
        "/api/search",
        json={"query": "上限額", "search_answer_profile_id": "bv-1"},
    )

    assert response.status_code == 200
    settings = RecordingPipeline.captured_settings
    assert settings is not None
    # Search Answer Profile が設定した回答の設定は Search Answer Profile 値が効く。
    assert settings.rag_answer_flow == "standard_rag"
    # Search Answer Profile が触れていない項目は KB legacy 値ではなく global 既定。
    assert settings.rag_neighbor_child_count == 3
    assert settings.rag_vector_index_profile == "accurate"
    diagnostics = response.json()["data"]["diagnostics"]
    assert diagnostics["search_answer_profile_applied"] == "bv-1"
    assert diagnostics["kb_adapter_config_applied"] is None


class FakeFieldSetOracle(FakeViewOracle):
    """検索・回答プロファイルと、KB ごとの項目抽出の定義(#549)を返すテスト用 Oracle。"""

    def __init__(
        self,
        views: dict[str, SearchAnswerProfileConfig],
        field_sets: dict[str, list[FieldDefinition] | None],
    ) -> None:
        super().__init__(views)
        self._field_sets = field_sets
        self.requested_kb_ids: list[str] = []

    async def list_knowledge_base_extraction_field_sets(
        self, knowledge_base_ids: list[str]
    ) -> list[list[FieldDefinition] | None]:
        self.requested_kb_ids = list(knowledge_base_ids)
        return [
            self._field_sets[kb_id] for kb_id in knowledge_base_ids if kb_id in self._field_sets
        ]


def test_search_extraction_fields_unions_search_answer_profile_knowledge_bases(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """検索の絞り込みの項目は、選んだ検索・回答プロファイル
    の KB の定義(無ければ既定)の和集合(#549)。"""
    monkeypatch.setenv(fields_mod.FIELD_SCHEMA_FILE_ENV, str(tmp_path / "fields.json"))
    fields_mod.save_field_schema([FieldDefinition(name="請求書番号")])
    oracle = FakeFieldSetOracle(
        {"bv-1": SearchAnswerProfileConfig(knowledge_base_ids=["kb-contract", "kb-default"])},
        {
            "kb-contract": [
                FieldDefinition(name="契約日", value_type="date"),
                FieldDefinition(name="金額", value_type="number"),
            ],
            "kb-default": None,
        },
    )
    monkeypatch.setattr(search_route, "OracleClient", lambda *_args, **_kwargs: oracle)

    response = client.get("/api/search/extraction-fields?search_answer_profile_id=bv-1")

    assert response.status_code == 200
    assert oracle.requested_kb_ids == ["kb-contract", "kb-default"]
    assert [
        (field["name"], field["value_type"]) for field in response.json()["data"]["fields"]
    ] == [("契約日", "date"), ("金額", "number"), ("請求書番号", "string")]


def test_search_extraction_fields_without_knowledge_bases_and_missing_view(
    monkeypatch: MonkeyPatch,
) -> None:
    oracle = FakeFieldSetOracle({"bv-empty": SearchAnswerProfileConfig()}, {})
    monkeypatch.setattr(search_route, "OracleClient", lambda *_args, **_kwargs: oracle)

    empty = client.get("/api/search/extraction-fields?search_answer_profile_id=bv-empty")
    assert empty.status_code == 200
    assert empty.json()["data"]["fields"] == []
    missing = client.get("/api/search/extraction-fields?search_answer_profile_id=bv-missing")
    assert missing.status_code == 404


class ArchivedFieldSetOracle(FakeFieldSetOracle):
    """検索・回答プロファイルをアーカイブ済みとして返す。"""

    async def get_search_answer_profile(
        self, search_answer_profile_id: str
    ) -> SearchAnswerProfileDetail | None:
        detail = await super().get_search_answer_profile(search_answer_profile_id)
        if detail is None:
            return None
        return detail.model_copy(update={"status": SearchAnswerProfileStatus.ARCHIVED})


def test_search_extraction_fields_rejects_archived_search_answer_profile(
    monkeypatch: MonkeyPatch,
) -> None:
    """アーカイブ済みの検索・回答プロファイルは、検索と同じく項目も返さない（409。#961）。"""
    oracle = ArchivedFieldSetOracle(
        {"bv-old": SearchAnswerProfileConfig(knowledge_base_ids=["kb-contract"])},
        {"kb-contract": [FieldDefinition(name="契約日", value_type="date")]},
    )
    monkeypatch.setattr(search_route, "OracleClient", lambda *_args, **_kwargs: oracle)

    response = client.get("/api/search/extraction-fields?search_answer_profile_id=bv-old")

    assert response.status_code == 409
    assert (
        "アーカイブ済みの検索・回答プロファイルは検索に使用できません"
        in (response.json()["error_messages"][0])
    )
    assert oracle.requested_kb_ids == []
