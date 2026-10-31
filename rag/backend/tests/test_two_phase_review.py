"""段階レビュー可能なファイル処理(PREPROCESS/EXTRACT → CHUNK → INDEX)の実 Oracle 統合テスト。

取込 job はすべて文書のレシピの job として実行する。文書単位の投入 API
(``POST /api/documents/{id}/ingestion-jobs``)は既定レシピの job を作り、確認待ちの工程は
レシピの承認 API(``POST /api/documents/{id}/recipes/{recipe_id}/approve``)で 1 工程ずつ
進める。レビュー修正などの API 契約は ``tests/test_document_workspace.py`` が fake の Oracle で
確かめる。
"""

import asyncio
from typing import Any, cast

import pytest
from pytest import MonkeyPatch

from app.api.routes import documents as documents_route
from app.clients.oracle import OracleClient
from app.config import get_settings
from app.main import app
from app.rag import ingestion as ingestion_module
from app.rag.audit import record_rag_ingestion_audit
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

# 実 Oracle AI Database + OCI を用いる統合テスト（DB 未到達環境では自動 skip）。
pytestmark = pytest.mark.usefixtures("oracle_db")


def _enable_review_gate(monkeypatch: MonkeyPatch) -> None:
    """段階レビューを有効にし、抽出後(REVIEW)と Chunk 作成後(CHUNKED)で自動進行を止める。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_review_gate_enabled", True)
    monkeypatch.setattr(settings, "rag_auto_chunk_after_extract_enabled", False)
    monkeypatch.setattr(settings, "rag_auto_index_after_chunk_enabled", False)


def _upload_sample(text: str = "社内規程: 経費申請\n部門長の承認後、経理部が確認します。") -> str:
    upload_resp = client.post(
        "/api/documents/upload",
        files={"file": ("two-phase-policy.txt", text.encode(), "text/plain")},
    )
    assert upload_resp.status_code == 200
    return cast(str, upload_resp.json()["data"]["id"])


def _run_job(job_id: str) -> None:
    asyncio.run(documents_route._run_ingestion_job(job_id))


def _job(job_id: str) -> dict[str, Any]:
    response = client.get(f"/api/documents/ingestion-jobs/{job_id}")
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["data"])


def _queued_jobs(document_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/documents/{document_id}/ingestion-jobs")
    assert response.status_code == 200
    jobs = cast(list[dict[str, Any]], response.json()["data"])
    return [job for job in jobs if job["status"] == "QUEUED"]


def _enqueue_ingestion(document_id: str) -> dict[str, Any]:
    """文書単位の投入 API で既定レシピの最初の工程(ファイル準備から)を投入する。"""
    response = client.post(f"/api/documents/{document_id}/ingestion-jobs")
    assert response.status_code == 200
    job = cast(dict[str, Any], response.json()["data"])
    assert job["phase"] == "PREPROCESS"
    assert job["recipe_id"] == _first_recipe_id(document_id)
    return job


def _enqueue_phase(document_id: str, phase: str) -> Any:
    """文書単位の投入 API で次の工程(CHUNK / INDEX)を投入する。"""
    return client.post(f"/api/documents/{document_id}/ingestion-jobs", params={"phase": phase})


def _document_chunks(document_id: str) -> list[Any]:
    return asyncio.run(OracleClient().list_document_chunks(document_id))


def _document_chunk_sets(document_id: str) -> list[dict[str, object]]:
    return asyncio.run(OracleClient().list_document_chunk_sets(document_id))


def _first_recipe(document_id: str) -> dict[str, Any]:
    response = client.get(f"/api/documents/{document_id}/recipes")
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["data"][0])


def _first_recipe_id(document_id: str) -> str:
    return cast(str, _first_recipe(document_id)["recipe_id"])


def _extract_to_review(document_id: str) -> None:
    """ファイル準備と抽出を実行し、既定レシピを REVIEW(抽出の確認待ち)で止める。"""
    job = _enqueue_ingestion(document_id)
    _run_job(cast(str, job["id"]))
    assert _job(cast(str, job["id"]))["status"] == "SUCCEEDED"
    assert _first_recipe(document_id)["status"] == "REVIEW"


def _approve(document_id: str, expected_phase: str) -> dict[str, Any]:
    """既定レシピの確認待ちの工程を承認し、投入された次の工程の job を実行する。"""
    recipe_id = _first_recipe_id(document_id)
    response = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/approve")
    assert response.status_code == 200
    job = cast(dict[str, Any], response.json()["data"])
    assert job["phase"] == expected_phase
    assert job["status"] == "QUEUED"
    _run_job(cast(str, job["id"]))
    return _job(cast(str, job["id"]))


def _approve_to_chunked(document_id: str) -> dict[str, Any]:
    """抽出を承認し、CHUNK 工程だけを実行して CHUNKED で止める。"""
    job = _approve(document_id, "CHUNK")
    assert job["status"] == "SUCCEEDED"
    assert _first_recipe(document_id)["status"] == "CHUNKED"
    return job


def _approve_chunks_to_indexed(document_id: str) -> dict[str, Any]:
    """Chunk を承認し、INDEX 工程を実行して INDEXED(検索対象)にする。"""
    job = _approve(document_id, "INDEX")
    assert job["status"] == "SUCCEEDED"
    assert _first_recipe(document_id)["status"] == "INDEXED"
    assert _get_document(document_id)["status"] == "INDEXED"
    return job


def _approve_all(document_id: str) -> None:
    """REVIEW から CHUNKED を経て INDEXED まで進める。"""
    _approve_to_chunked(document_id)
    _approve_chunks_to_indexed(document_id)


def _index_in_single_job(document_id: str) -> None:
    """確認待ちのゲートなし: 最初の job 1 本で索引まで進み、後続の job を作らない。"""
    job = _enqueue_ingestion(document_id)
    _run_job(cast(str, job["id"]))
    assert _job(cast(str, job["id"]))["status"] == "SUCCEEDED"
    assert _queued_jobs(document_id) == []
    assert _first_recipe(document_id)["status"] == "INDEXED"
    assert _get_document(document_id)["status"] == "INDEXED"


def _get_document(document_id: str) -> dict[str, Any]:
    response = client.get(f"/api/documents/{document_id}")
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["data"])


def _search(query: str) -> dict[str, Any]:
    response = client.post(
        "/api/search",
        json={"query": query, "top_k": 5},
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["data"])


def _add_to_new_knowledge_base(document_id: str, name: str) -> str:
    """新しい KB を作り、文書を所属させる(KB は所属だけを持ち、処理設定は持たない)。"""
    kb_resp = client.post(
        "/api/knowledge-bases", json={"name": name, "description": f"{name} の検証用"}
    )
    assert kb_resp.status_code == 200
    knowledge_base_id = cast(str, kb_resp.json()["data"]["id"])
    assign_resp = client.post(
        f"/api/knowledge-bases/{knowledge_base_id}/documents",
        json={"document_ids": [document_id]},
    )
    assert assign_resp.status_code == 200
    return knowledge_base_id


def test_review_gate_stops_at_review_and_excludes_from_search(monkeypatch: MonkeyPatch) -> None:
    """抽出後は REVIEW で停止し、抽出は保持されるが chunk はなく検索対象外。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()

    _extract_to_review(document_id)

    recipe = _first_recipe(document_id)
    assert recipe["searchable"] is False
    assert _queued_jobs(document_id) == []
    # 抽出本文はプレビュー用にレシピの抽出成果物として保持される。
    extraction = asyncio.run(
        OracleClient().get_document_extraction_artifact(
            document_id=document_id,
            extraction_recipe_id=cast(str, recipe["active_extraction_recipe_id"]),
        )
    )
    assert extraction is not None
    assert "部門長の承認" in cast(dict[str, Any], extraction["extraction_json"])["raw_text"]
    # まだ分割・索引していないので chunk は無い。
    assert _document_chunks(document_id) == []
    # REVIEW 文書は検索対象に入らない。
    search = _search("経費申請の承認者は？")
    assert all(citation["document_id"] != document_id for citation in search["citations"])


def test_chunk_preview_reuses_review_extraction_without_state_change(
    monkeypatch: MonkeyPatch,
) -> None:
    """一時分割は抽出を再利用し、chunk 行も REVIEW 状態も変更しない。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()
    recipe_id = _first_recipe_id(document_id)
    job_response = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/ingestion-jobs")
    assert job_response.status_code == 200
    job = job_response.json()["data"]
    assert job["phase"] == "PREPROCESS"
    _run_job(cast(str, job["id"]))
    recipe_before = client.get(f"/api/documents/{document_id}/recipes").json()["data"][0]
    assert recipe_before["status"] == "REVIEW"

    response = client.post(
        f"/api/documents/{document_id}/recipes/{recipe_id}/chunk-preview",
        json={
            "chunking_strategy": "recursive_character",
            "chunk_size": 200,
            "chunk_overlap": 20,
            "chunk_min_chars": 0,
            "chunk_context_header_enabled": True,
        },
    )

    assert response.status_code == 200
    preview = response.json()["data"]
    assert preview["chunks"]
    assert preview["stats"]["chunk_count"] == len(preview["chunks"])
    assert preview["stats"]["min_chars"] > 0
    assert preview["chunks"][0]["metadata"]["context_header"].startswith("two-phase-policy.txt")
    recipe_after = client.get(f"/api/documents/{document_id}/recipes").json()["data"][0]
    assert recipe_after["status"] == "REVIEW"
    assert recipe_after["config_revision"] == recipe_before["config_revision"]
    assert (
        client.get(f"/api/documents/{document_id}/recipes/{recipe_id}/chunks").json()["data"] == []
    )


def test_chunk_preview_small_to_big_builds_parents_without_docling_layout(
    monkeypatch: MonkeyPatch,
) -> None:
    """Docling 以外の解析結果でも、親子階層（small-to-big）のプレビューは親子で分割する(#3377)。

    以前は構造認識へ縮退していた(#300)。範囲外の値は 422 のまま。
    """
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()
    recipe_id = _first_recipe_id(document_id)
    job_response = client.post(f"/api/documents/{document_id}/recipes/{recipe_id}/ingestion-jobs")
    assert job_response.status_code == 200
    _run_job(cast(str, job_response.json()["data"]["id"]))

    out_of_range = client.post(
        f"/api/documents/{document_id}/recipes/{recipe_id}/chunk-preview",
        json={"chunking_strategy": "small_to_big", "chunk_child_target_chars": 2000},
    )
    response = client.post(
        f"/api/documents/{document_id}/recipes/{recipe_id}/chunk-preview",
        json={"chunking_strategy": "small_to_big", "chunk_child_target_chars": 600},
    )

    assert out_of_range.status_code == 422
    assert response.status_code == 200
    chunks = response.json()["data"]["chunks"]
    assert chunks
    for chunk in chunks:
        assert chunk["metadata"]["chunk_strategy"] == "small_to_big"
        assert chunk["metadata"]["chunk_group_kind"] == "small_to_big_parent"
        assert chunk["metadata"]["parent_text"]
        assert "chunk_strategy_fallback_reason" not in chunk["metadata"]


def test_chunk_preview_rejects_recipe_without_review_artifact(
    monkeypatch: MonkeyPatch,
) -> None:
    """UPLOADED のレシピは dry-run 対象外。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()
    recipe_id = _first_recipe_id(document_id)

    response = client.post(
        f"/api/documents/{document_id}/recipes/{recipe_id}/chunk-preview",
        json={},
    )

    assert response.status_code == 409


def test_approve_chunks_then_second_approve_indexes_and_makes_searchable(
    monkeypatch: MonkeyPatch,
) -> None:
    """抽出の承認では CHUNKED で止まり、chunk の承認後に INDEXED・検索可能になる。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()
    _extract_to_review(document_id)

    _approve_to_chunked(document_id)

    assert _document_chunks(document_id)
    assert _first_recipe(document_id)["searchable"] is False
    search = _search("経費申請の承認者は？")
    assert all(citation["document_id"] != document_id for citation in search["citations"])

    _approve_chunks_to_indexed(document_id)
    assert _first_recipe(document_id)["searchable"] is True
    search = _search("経費申請の承認者は？")
    assert any(citation["document_id"] == document_id for citation in search["citations"])


def test_approve_records_chunk_set_and_activates_it(monkeypatch: MonkeyPatch) -> None:
    """索引後はレシピの chunk_set を記録し、レシピの active(検索対象)にする。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()
    _extract_to_review(document_id)

    _approve_all(document_id)

    chunk_sets = _document_chunk_sets(document_id)
    # 1 レシピ 1 materialization なので chunk_set は 1 つ。
    assert len(chunk_sets) == 1
    chunk_set_id = str(chunk_sets[0]["chunk_set_id"])
    recipe = _first_recipe(document_id)
    assert recipe["active_chunk_set_id"] == chunk_set_id
    assert recipe["chunk_count"] > 0

    chunk_set = asyncio.run(OracleClient().get_chunk_set(chunk_set_id))
    assert chunk_set is not None
    assert chunk_set["status"] == "INDEXED"
    assert chunk_set["recipe_id"] == recipe["recipe_id"]
    # chunk がタグ付け・計数されている。
    assert chunk_set["chunk_count"]
    assert chunk_set["vector_count"] == chunk_set["chunk_count"]
    assert int(str(chunk_set["is_active"])) == 1


def test_activation_failure_does_not_make_recipe_searchable(monkeypatch: MonkeyPatch) -> None:
    """chunk/vector の保存後でも、active の切り替えに失敗したら INDEXED 成功扱いにしない。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()
    _extract_to_review(document_id)
    _approve_to_chunked(document_id)

    async def _fail_activation(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("activation failed: INV-SECRET")

    monkeypatch.setattr(OracleClient, "activate_recipe_chunk_set", _fail_activation)

    job = _approve(document_id, "INDEX")

    assert job["status"] == "FAILED"
    assert job["error_message"] == "取込処理に失敗しました。"
    recipe = _first_recipe(document_id)
    assert recipe["status"] == "ERROR"
    assert recipe["failed_phase"] == "INDEX"
    assert recipe["searchable"] is False
    assert recipe["active_chunk_set_id"] is None
    assert "INV-SECRET" not in str(recipe)
    assert _get_document(document_id)["status"] != "INDEXED"
    search = _search("経費申請の承認者は？")
    assert all(citation["document_id"] != document_id for citation in search["citations"])


def test_kb_scoped_search_finds_active_chunk_set(monkeypatch: MonkeyPatch) -> None:
    """KB スコープ検索でも、レシピの active な chunk_set はフィルタで除外されない。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()
    _extract_to_review(document_id)
    _approve_all(document_id)

    detail = _get_document(document_id)
    knowledge_base_id = detail["knowledge_bases"][0]["id"]

    response = client.post(
        "/api/search",
        json={
            "query": "経費申請の承認者は？",
            "knowledge_base_ids": [knowledge_base_id],
            "top_k": 5,
        },
    )
    assert response.status_code == 200
    citations = response.json()["data"]["citations"]
    assert any(citation["document_id"] == document_id for citation in citations)


def test_document_in_two_knowledge_bases_shares_single_chunk_set(
    monkeypatch: MonkeyPatch,
) -> None:
    """3 層モデル: 2 つの KB に所属しても、文書のレシピの chunk_set は 1 つを共有する。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()
    _extract_to_review(document_id)
    kb_b = _add_to_new_knowledge_base(document_id, "共有KB")

    _approve_all(document_id)

    chunk_sets = _document_chunk_sets(document_id)
    assert len(chunk_sets) == 1
    knowledge_bases = asyncio.run(OracleClient().list_document_knowledge_bases(document_id))
    assert len(knowledge_bases) == 2
    assert kb_b in cast(list[str], chunk_sets[0]["knowledge_base_ids"])
    # 追加した KB に絞った検索でも同じ chunk_set が見つかる。
    response = client.post(
        "/api/search",
        json={
            "query": "経費申請の承認者は？",
            "knowledge_base_ids": [kb_b],
            "top_k": 5,
        },
    )
    assert response.status_code == 200
    citations = response.json()["data"]["citations"]
    assert any(citation["document_id"] == document_id for citation in citations)


def test_chunk_phase_requires_review_status(monkeypatch: MonkeyPatch) -> None:
    """抽出を確認していない(REVIEW でない)文書の CHUNK 工程は 409。"""
    _enable_review_gate(monkeypatch)
    document_id = _upload_sample()

    # まだ UPLOADED。
    approve_resp = _enqueue_phase(document_id, "CHUNK")
    assert approve_resp.status_code == 409


def test_gate_disabled_keeps_single_pass_indexing(monkeypatch: MonkeyPatch) -> None:
    """確認待ちのゲートなしでは、1 本の job で INDEXED まで進む。"""
    monkeypatch.setattr(get_settings(), "rag_review_gate_enabled", False)
    document_id = _upload_sample()

    _index_in_single_job(document_id)

    assert len(_document_chunk_sets(document_id)) == 1
    search = _search("経費申請の承認者は？")
    assert any(citation["document_id"] == document_id for citation in search["citations"])


def test_indexing_records_single_success_audit(monkeypatch: MonkeyPatch) -> None:
    """取込は文書につき成功 audit を 1 回だけ記録する(1 文書 1 論理取込に集約)。"""
    monkeypatch.setattr(get_settings(), "rag_review_gate_enabled", False)

    success_audit_docs: list[str] = []

    def _spy(**kwargs: Any) -> None:
        if kwargs.get("outcome") == "success":
            success_audit_docs.append(cast(str, kwargs.get("document_id")))
        record_rag_ingestion_audit(**kwargs)

    monkeypatch.setattr(ingestion_module, "record_rag_ingestion_audit", _spy)

    document_id = _upload_sample()
    _add_to_new_knowledge_base(document_id, "監査KB")

    _index_in_single_job(document_id)

    # 2 KB に所属しても単一 chunk_set。
    assert len(_document_chunk_sets(document_id)) == 1
    # 成功 audit はこの文書につき 1 回だけ。
    assert success_audit_docs.count(document_id) == 1


def test_document_chunk_sets_endpoint_lists_single_variant(monkeypatch: MonkeyPatch) -> None:
    """/chunk-sets が文書の単一 chunk_set を状態・件数・所属 KB・抽出状態つきで返す。"""
    monkeypatch.setattr(get_settings(), "rag_review_gate_enabled", False)
    document_id = _upload_sample()
    kb_b = _add_to_new_knowledge_base(document_id, "一覧KB")

    _index_in_single_job(document_id)

    resp = client.get(f"/api/documents/{document_id}/chunk-sets")
    assert resp.status_code == 200
    chunk_sets = cast(list[dict[str, Any]], resp.json()["data"])
    assert len(chunk_sets) == 1
    chunk_set = chunk_sets[0]
    assert chunk_set["chunk_set_id"] == _first_recipe(document_id)["active_chunk_set_id"]
    assert chunk_set["status"] == "INDEXED"
    assert chunk_set["extraction_recipe_id"].startswith("er_")
    assert chunk_set["extraction_status"] == "materialized"
    assert chunk_set["chunk_count"] > 0
    assert chunk_set["vector_count"] == chunk_set["chunk_count"]
    assert chunk_set["created_at"]
    # 所属 KB(既定 KB と追加した KB)の和集合を返す。
    assert kb_b in chunk_set["knowledge_base_ids"]
    assert len(chunk_set["knowledge_base_ids"]) == 2


def test_document_chunk_sets_endpoint_reports_layer_statuses(monkeypatch: MonkeyPatch) -> None:
    """/chunk-sets は global で有効にしたレイヤー(graph / field / navigation)の状態を返す。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_review_gate_enabled", False)
    # レイヤー軸は KB 上書きではなく global で有効化する(3 層モデル)。
    monkeypatch.setattr(settings, "rag_graph_profile", "entities")
    monkeypatch.setattr(settings, "rag_field_extraction_enabled", True)
    monkeypatch.setattr(settings, "rag_navigation_summary_enabled", True)
    document_id = _upload_sample(
        "# 第1章 概要\n\n"
        "社内規程の概要を説明します。\n\n"
        "## 1.1 経費申請\n\n"
        "部門長の承認後、経理部が確認します。\n"
    )

    _index_in_single_job(document_id)

    resp = client.get(f"/api/documents/{document_id}/chunk-sets")
    assert resp.status_code == 200
    chunk_sets = cast(list[dict[str, Any]], resp.json()["data"])
    assert len(chunk_sets) == 1
    assert set(chunk_sets[0]["layer_statuses"]) == {"metadata", "graph", "navigation"}
    planned_layers = [
        (name, status)
        for name, status in chunk_sets[0]["layer_statuses"].items()
        if status["requested"]
    ]
    assert {name for name, _status in planned_layers} == {"metadata", "graph", "navigation"}
    assert all(status["layer_id"] for _name, status in planned_layers)
    assert any(
        name == "navigation" and status["status"] == "materialized"
        for name, status in planned_layers
    )
