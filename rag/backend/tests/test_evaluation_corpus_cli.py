"""評価の合成の資料の取込 CLI（#1231）。API は httpx の MockTransport で置き換える。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.rag.evaluation_corpus_cli import (
    CorpusError,
    CorpusLoader,
    document_versions,
    guide_referenced_files,
    guided_golden_set,
    main,
    referenced_files,
    resolve_golden_set,
    resolve_guides,
    with_entity_expansion,
)

GOLDEN_SET: dict[str, Any] = {
    "cases": [
        {"id": "a", "query": "q", "relevant_document_ids": ["file:manual.pdf", "doc-fixed"]},
        {"id": "b", "query": "q", "relevant_document_ids": ["file:params.xlsx", "file:manual.pdf"]},
        {"id": "c", "query": "q", "answerable": False},
    ]
}


def test_referenced_files_and_resolution() -> None:
    assert referenced_files(GOLDEN_SET) == ["manual.pdf", "params.xlsx"]
    resolved = resolve_golden_set(GOLDEN_SET, {"manual.pdf": "d1", "params.xlsx": "d2"}, "kb-1")
    assert resolved["cases"][0]["relevant_document_ids"] == ["d1", "doc-fixed"]
    assert resolved["cases"][1]["relevant_document_ids"] == ["d2", "d1"]
    assert resolved["knowledge_base_ids"] == ["kb-1"]
    # 元の評価セットは変えない。
    assert GOLDEN_SET["cases"][0]["relevant_document_ids"][0] == "file:manual.pdf"
    with pytest.raises(CorpusError):
        resolve_golden_set(GOLDEN_SET, {"manual.pdf": "d1"}, "kb-1")


class FakeApi:
    """取込の API。レシピは呼ばれるたびに次の状態へ進む（REVIEW では承認を待つ）。"""

    def __init__(self, statuses: dict[str, list[str]]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, str, Any]] = []
        self.documents: dict[str, str] = {}
        # 前の評価で同じ内容を取り込んだファイル → 重複の元の文書 ID（#1381）。
        self.duplicates: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body: Any = None
        if request.method in {"POST", "PUT"} and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            body = json.loads(request.content or b"{}")
        self.calls.append((request.method, path, body))
        if path == "/knowledge-bases":
            return httpx.Response(200, json={"data": {"id": "kb-new"}})
        if path == "/documents/upload":
            name = "params.xlsx" if b'filename="params.xlsx"' in request.content else "manual.pdf"
            document_id = f"doc-{name}"
            self.documents[document_id] = name
            data = {"document_id": document_id}
            if name in self.duplicates:
                data["duplicate_of_document_id"] = self.duplicates[name]
            return httpx.Response(200, json={"data": data})
        document_id = path.split("/")[2]
        name = self.documents[document_id]
        if path.endswith("/recipes") and request.method == "GET":
            queue = self.statuses[name]
            status = queue[0] if len(queue) == 1 else queue.pop(0)
            return httpx.Response(
                200, json={"data": [{"recipe_id": "r1", "status": status, "error_message": None}]}
            )
        return httpx.Response(200, json={"data": {}})


def _loader(api: FakeApi) -> CorpusLoader:
    client = httpx.Client(transport=httpx.MockTransport(api), base_url="http://test")
    return CorpusLoader(
        client, "http://test", poll_interval_seconds=0, sleep=lambda _: None, log=lambda _: None
    )


def test_loader_uploads_sets_excel_recipe_and_approves_gates(tmp_path: Path) -> None:
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "params.xlsx").write_bytes(b"PK")
    api = FakeApi(
        {
            # アップロード直後の取得 → 待ちの間に REVIEW（承認）→ INDEXED。
            "manual.pdf": ["UPLOADED", "REVIEW", "REVIEW", "INDEXED"],
            "params.xlsx": ["UPLOADED", "INGESTING", "INDEXED"],
        }
    )
    loader = _loader(api)
    knowledge_base_id = loader.create_knowledge_base("評価")
    documents = {
        name: loader.ingest(tmp_path / name, knowledge_base_id)
        for name in ("manual.pdf", "params.xlsx")
    }
    loader.wait_indexed(documents)

    assert documents == {"manual.pdf": "doc-manual.pdf", "params.xlsx": "doc-params.xlsx"}
    puts = [call for call in api.calls if call[0] == "PUT"]
    # Excel だけ前処理 excel_to_jsonl のレシピにする。
    assert puts == [
        ("PUT", "/documents/doc-params.xlsx/recipes/r1", {"preprocess_profile": "excel_to_jsonl"})
    ]
    jobs = [call[1] for call in api.calls if call[1].endswith("/ingestion-jobs")]
    assert len(jobs) == 2
    approvals = [call[1] for call in api.calls if call[1].endswith("/approve")]
    # 同じゲートは 1 回だけ承認する。
    assert approvals == ["/documents/doc-manual.pdf/recipes/r1/approve"]


def test_loader_ingests_duplicate_content_as_its_own_document(tmp_path: Path) -> None:
    """前の評価と同じ内容のファイルも取込を進め、この KB の文書 ID を使う(#1381)。"""
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.4")
    api = FakeApi({"manual.pdf": ["UPLOADED", "INDEXED"]})
    api.duplicates["manual.pdf"] = "doc-earlier-kb"
    lines: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(api), base_url="http://test")
    loader = CorpusLoader(
        client, "http://test", poll_interval_seconds=0, sleep=lambda _: None, log=lines.append
    )

    document_id = loader.ingest(tmp_path / "manual.pdf", "kb-new")
    loader.wait_indexed({"manual.pdf": document_id})

    assert document_id == "doc-manual.pdf"
    jobs = [call[1] for call in api.calls if call[1].endswith("/ingestion-jobs")]
    assert jobs == ["/documents/doc-manual.pdf/recipes/r1/ingestion-jobs"]
    assert any("duplicate manual.pdf" in line and "doc-earlier-kb" in line for line in lines)
    resolved = resolve_golden_set(
        GOLDEN_SET, {"manual.pdf": document_id, "params.xlsx": "d2"}, "kb"
    )
    assert resolved["cases"][0]["relevant_document_ids"] == ["doc-manual.pdf", "doc-fixed"]


def test_loader_reports_failed_ingestion(tmp_path: Path) -> None:
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.4")
    api = FakeApi({"manual.pdf": ["UPLOADED", "ERROR"]})
    loader = _loader(api)
    document_id = loader.ingest(tmp_path / "manual.pdf", "kb")
    with pytest.raises(CorpusError, match="取込に失敗しました"):
        loader.wait_indexed({"manual.pdf": document_id})


def test_main_rejects_missing_corpus_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    golden = tmp_path / "set.json"
    golden.write_text(json.dumps(GOLDEN_SET), encoding="utf-8")
    code = main([str(golden), "--output", str(tmp_path / "out.json")])
    assert code == 2
    assert "資料が見つかりません" in capsys.readouterr().err


def test_required_evidence_file_references_are_collected_and_resolved() -> None:
    """必要な根拠の文書（#1284）も `file:` で参照でき、取込の対象と置き換えの対象になる。"""
    golden_set = {
        "cases": [
            {
                "id": "a",
                "query": "q",
                "relevant_document_ids": ["file:manual.pdf"],
                "required_evidence": [
                    {"id": "e1", "document_id": "file:notes.pdf", "text": "token expired"},
                    {"id": "e2", "document_id": "doc-fixed", "text": "x"},
                    {"id": "e3", "text": "y"},
                ],
            }
        ]
    }
    assert referenced_files(golden_set) == ["manual.pdf", "notes.pdf"]
    resolved = resolve_golden_set(golden_set, {"manual.pdf": "d1", "notes.pdf": "d2"}, "kb-1")
    evidence = resolved["cases"][0]["required_evidence"]
    assert [item.get("document_id") for item in evidence] == ["d2", "doc-fixed", None]
    with pytest.raises(CorpusError):
        resolve_golden_set(golden_set, {"manual.pdf": "d1"}, "kb-1")


# ---- 業務ガイドの取込（C の再現。#1289） ------------------------------------------------

GUIDES: list[dict[str, Any]] = [
    {
        "title": "権限の付与",
        "references": [
            {"document_id": "file:manual.pdf", "title": "手順書"},
            {"document_id": "doc-fixed"},
        ],
    }
]


def test_guide_references_are_collected_and_resolved() -> None:
    assert guide_referenced_files(GUIDES) == ["manual.pdf"]
    resolved = resolve_guides(GUIDES, {"manual.pdf": "d1"})
    assert [ref["document_id"] for ref in resolved[0]["references"]] == ["d1", "doc-fixed"]
    # 元のガイドは変えない。
    assert GUIDES[0]["references"][0]["document_id"] == "file:manual.pdf"
    with pytest.raises(CorpusError):
        resolve_guides(GUIDES, {})


def test_guided_golden_set_uses_profile_instead_of_knowledge_bases() -> None:
    resolved = resolve_golden_set(GOLDEN_SET, {"manual.pdf": "d1", "params.xlsx": "d2"}, "kb-1")
    guided = guided_golden_set(resolved, "sap-1")
    assert guided["search_answer_profile_id"] == "sap-1"
    assert "knowledge_base_ids" not in guided
    assert resolved["knowledge_base_ids"] == ["kb-1"]


def test_loader_creates_profile_imports_and_publishes_guides() -> None:
    calls: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content or b"{}")
        calls.append((request.method, path, body))
        if path == "/search-answer-profiles":
            return httpx.Response(200, json={"data": {"id": "sap-1"}})
        if path.endswith("/support-guides/import"):
            created = [
                {"guide_id": f"g{index}", "title": guide["title"], "draft_revision": 1}
                for index, guide in enumerate(body["guides"])
            ]
            return httpx.Response(200, json={"data": {"created": created}})
        return httpx.Response(200, json={"data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    loader = CorpusLoader(client, "http://test", log=lambda _: None)
    profile_id = loader.create_search_answer_profile("評価", "kb-1")
    published = loader.import_guides(profile_id, resolve_guides(GUIDES, {"manual.pdf": "d1"}))

    assert profile_id == "sap-1"
    assert published == ["g0"]
    assert calls[0][2]["config"] == {
        "knowledge_base_ids": ["kb-1"],
        "query": {"support_guide_enabled": True},
    }
    assert calls[1][2]["guides"][0]["references"][0]["document_id"] == "d1"
    assert calls[2] == (
        "POST",
        "/search-answer-profiles/sap-1/support-guides/g0/publish",
        {"base_revision": 1},
    )


def test_main_requires_guides_and_guided_output_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    golden = tmp_path / "set.json"
    golden.write_text(json.dumps(GOLDEN_SET), encoding="utf-8")
    guides = tmp_path / "guides.json"
    guides.write_text(json.dumps({"guides": GUIDES}), encoding="utf-8")
    code = main([str(golden), "--output", str(tmp_path / "out.json"), "--guides", str(guides)])
    assert code == 2
    assert "一緒に渡してください" in capsys.readouterr().err


def test_loader_selects_entity_index_recipe_for_every_document(tmp_path: Path) -> None:
    """``--entity-index``（#1362）はすべての文書のレシピで実体の抽出を選ぶ（Excel は前処理も）。

    実体の層の有り / 無しは、別のナレッジベースに取り込んで比べる。
    """
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "params.xlsx").write_bytes(b"PK")
    api = FakeApi({"manual.pdf": ["UPLOADED"], "params.xlsx": ["UPLOADED"]})
    client = httpx.Client(transport=httpx.MockTransport(api), base_url="http://test")
    loader = CorpusLoader(
        client,
        "http://test",
        poll_interval_seconds=0,
        sleep=lambda _: None,
        log=lambda _: None,
        entity_index=True,
    )

    for name in ("manual.pdf", "params.xlsx"):
        loader.ingest(tmp_path / name, "kb-1")

    puts = [call for call in api.calls if call[0] == "PUT"]
    assert puts == [
        ("PUT", "/documents/doc-manual.pdf/recipes/r1", {"entity_index_enabled": True}),
        (
            "PUT",
            "/documents/doc-params.xlsx/recipes/r1",
            {"preprocess_profile": "excel_to_jsonl", "entity_index_enabled": True},
        ),
    ]


def test_entity_index_selects_expansion_in_the_golden_set_and_the_profile() -> None:
    """評価の明示値は保持する。指南を使うプロファイルは使用だけ ON にし、拡張は全体を使う。"""
    resolved = resolve_golden_set(GOLDEN_SET, {"manual.pdf": "d1", "params.xlsx": "d2"}, "kb-1")
    selected = with_entity_expansion(resolved)
    assert selected["rag_overrides"] == {"entity_expansion_enabled": True}
    assert "rag_overrides" not in resolved
    explicit = with_entity_expansion(
        {**resolved, "rag_overrides": {"entity_expansion_enabled": False, "rerank_enabled": True}}
    )
    assert explicit["rag_overrides"] == {"entity_expansion_enabled": False, "rerank_enabled": True}

    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"data": {"id": "sap-1"}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    loader = CorpusLoader(client, "http://test", log=lambda _: None, entity_index=True)
    assert loader.create_search_answer_profile("評価", "kb-1") == "sap-1"
    assert bodies[0]["config"] == {
        "knowledge_base_ids": ["kb-1"],
        "query": {"support_guide_enabled": True},
    }


@pytest.mark.parametrize(
    ("entity_index", "expected"),
    [
        (None, {".pdf": None, ".xlsx": {"preprocess_profile": "excel_to_jsonl"}}),
        (False, {".pdf": {"entity_index_enabled": False}, ".xlsx": None}),
    ],
    ids=["default", "no-entity-index"],
)
def test_recipe_follows_the_global_default_or_disables_entity_index(
    entity_index: bool | None, expected: dict[str, Any]
) -> None:
    """実体の抽出は全体の既定で ON（#1388）。

    省略時はレシピに書かず、``--no-entity-index`` は無効にする。
    """
    from app.rag.evaluation_corpus_cli import recipe_for

    pdf = recipe_for(Path("manual.pdf"), entity_index=entity_index)
    xlsx = recipe_for(Path("params.xlsx"), entity_index=entity_index)
    assert pdf == (expected[".pdf"] or {})
    assert xlsx == (
        expected[".xlsx"]
        if expected[".xlsx"] is not None
        else {"preprocess_profile": "excel_to_jsonl", "entity_index_enabled": False}
    )


# ---- 文書の版（旧版の登録。#1366） ------------------------------------------------------

VERSIONED_SET: dict[str, Any] = {
    "document_versions": [
        {"document_id": "file:rules-2023.pdf", "superseded_by": "file:rules.pdf"},
    ],
    "cases": [{"id": "a", "query": "q", "relevant_document_ids": ["file:rules.pdf"]}],
}


def test_document_versions_are_read_and_resolved() -> None:
    assert document_versions(VERSIONED_SET) == [("rules-2023.pdf", "rules.pdf")]
    assert document_versions({"cases": []}) == []
    ids = {"rules.pdf": "d-new", "rules-2023.pdf": "d-old"}
    resolved = resolve_golden_set(VERSIONED_SET, ids, "kb-1")
    assert resolved["document_versions"] == [{"document_id": "d-old", "superseded_by": "d-new"}]
    # 登録しなかったとき（--keep-superseded-active）は、書き出す評価セットから外す。
    kept = resolve_golden_set(VERSIONED_SET, ids, "kb-1", versions_registered=False)
    assert "document_versions" not in kept
    # 元の評価セットは変えない。
    assert VERSIONED_SET["document_versions"][0]["document_id"] == "file:rules-2023.pdf"


@pytest.mark.parametrize(
    ("versions", "message"),
    [
        ({"document_id": "file:a.pdf"}, "superseded_by は file:"),
        ({"document_id": "a.pdf", "superseded_by": "file:b.pdf"}, "document_id は file:"),
        ({"document_id": "file:a.pdf", "superseded_by": "file:a.pdf"}, "自分自身"),
        ("file:a.pdf", "document_id は file:"),
    ],
    ids=["missing-new", "not-file-reference", "self", "not-object"],
)
def test_document_versions_reject_invalid_entries(versions: object, message: str) -> None:
    with pytest.raises(CorpusError, match=message):
        document_versions({"document_versions": [versions]})


def test_document_versions_reject_duplicate_old_versions() -> None:
    entry = {"document_id": "file:a.pdf", "superseded_by": "file:b.pdf"}
    with pytest.raises(CorpusError, match="重複"):
        document_versions({"document_versions": [entry, entry]})
    with pytest.raises(CorpusError, match="配列"):
        document_versions({"document_versions": entry})


class VersionApi:
    """main の取込の API（どのファイルもすぐ INDEXED になる）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body: Any = None
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"{}")
        self.calls.append((request.method, path, body))
        if path == "/knowledge-bases":
            return httpx.Response(200, json={"data": {"id": "kb-new"}})
        if path == "/documents/upload":
            match = re.search(rb'filename="([^"]+)"', request.content)
            assert match is not None
            return httpx.Response(
                200, json={"data": {"document_id": f"doc-{match.group(1).decode()}"}}
            )
        if path.endswith("/recipes") and request.method == "GET":
            return httpx.Response(200, json={"data": [{"recipe_id": "r1", "status": "INDEXED"}]})
        return httpx.Response(200, json={"data": {}})


def _run_main_with_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> tuple[int, VersionApi, dict[str, Any]]:
    for name in ("rules.pdf", "rules-2023.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")
    golden = tmp_path / "set.json"
    golden.write_text(json.dumps(VERSIONED_SET), encoding="utf-8")
    output = tmp_path / "out.json"
    api = VersionApi()
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(api), **kwargs)

    # main が作る client だけを差し替える（module の httpx.Client を置き換える）。
    monkeypatch.setattr("app.rag.evaluation_corpus_cli.httpx.Client", client)
    code = main([str(golden), "--api-base-url", "http://test", "--output", str(output), *extra])
    resolved = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    return code, api, resolved


def test_main_registers_old_versions_as_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧版は問が参照しなくても取り込み、索引の後に新しい版に置き換えた文書として登録する。"""
    code, api, resolved = _run_main_with_api(tmp_path, monkeypatch)

    assert code == 0
    uploads = [call for call in api.calls if call[1] == "/documents/upload"]
    assert len(uploads) == 2
    assert [call for call in api.calls if call[1].endswith("/superseded-by")] == [
        (
            "PUT",
            "/documents/doc-rules-2023.pdf/superseded-by",
            {"superseded_by_document_id": "doc-rules.pdf"},
        )
    ]
    assert resolved["document_versions"] == [
        {"document_id": "doc-rules-2023.pdf", "superseded_by": "doc-rules.pdf"}
    ]


def test_main_keeps_old_versions_active_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--keep-superseded-active は旧版を登録しない（旧版の紛らわしさを測る評価）。"""
    code, api, resolved = _run_main_with_api(tmp_path, monkeypatch, "--keep-superseded-active")

    assert code == 0
    assert not [call for call in api.calls if call[1].endswith("/superseded-by")]
    assert "document_versions" not in resolved


def test_main_writes_the_document_map_for_the_suite_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--document-map-output は取り込んだファイル名 → 文書 ID の対応表を書く（#3240）。"""
    document_map = tmp_path / "document-map.json"
    code, _api, _resolved = _run_main_with_api(
        tmp_path, monkeypatch, "--document-map-output", str(document_map)
    )

    assert code == 0
    assert json.loads(document_map.read_text(encoding="utf-8")) == {
        "documents": {"rules.pdf": "doc-rules.pdf", "rules-2023.pdf": "doc-rules-2023.pdf"}
    }


def test_loader_skips_approve_when_the_gate_already_moved(tmp_path: Path) -> None:
    """読んだ後に自動で次の工程へ進んだゲートの承認（HTTP 409）は失敗にせず、次の読み取りで進む。"""
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.4")
    api = FakeApi({"manual.pdf": ["UPLOADED", "PREPROCESSED", "INDEXED"]})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/approve"):
            api.calls.append((request.method, request.url.path.removeprefix("/api"), None))
            return httpx.Response(
                409, json={"error_messages": ["確認待ちのレシピのみ承認できます。"]}
            )
        return api(request)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    loader = CorpusLoader(
        client, "http://test", poll_interval_seconds=0, sleep=lambda _: None, log=lambda _: None
    )
    documents = {"manual.pdf": loader.ingest(tmp_path / "manual.pdf", "kb-1")}

    loader.wait_indexed(documents)

    approvals = [call[1] for call in api.calls if call[1].endswith("/approve")]
    assert approvals == ["/documents/doc-manual.pdf/recipes/r1/approve"]
