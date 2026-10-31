"""業務ガイド（SupportGuide。#1237）の内容の検証と API（保存はメモリの偽物）。

内容は架空の「サンプル業務ポータル」の例だけを使う。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pr_system_settings.auth.errors import ROUTE_FORBIDDEN_CODE

from app.api.routes import search as search_route
from app.api.routes import support_guides as route
from app.clients.support_guide_store import SupportGuideConflictError, SupportGuideNotFoundError
from app.config import get_settings
from app.main import app
from app.rag import support_guide_query
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.rag.support_guide import content_sha256, diff_contents, has_errors, validate_content
from app.rag.support_guide_runtime import (
    GUIDE_PREVIEW_KEY,
    GUIDE_PREVIEW_TRACE_PREFIX,
    GuidePreview,
)
from app.schemas.document import FileStatus
from app.schemas.search import SearchDiagnostics, SearchRequest, SearchResponse
from app.schemas.support_guide import (
    SupportGuideContent,
    SupportGuideRevision,
    SupportGuideRevisionSummary,
    SupportGuideSummary,
)
from tests.security_support import enable_production_auth, login
from tests.support import AsgiTestClient

client = AsgiTestClient(app)
BASE = "/api/search-answer-profiles/bv-1/support-guides"


def guide(**overrides: Any) -> dict[str, Any]:
    """検証用アカウントの登録と権限の付与の業務ガイド（架空）。"""
    payload: dict[str, Any] = {
        "title": "検証用アカウントに権限を付ける",
        "goal": {
            "expected_result": "検証用アカウントが対象の機能を使える",
            "intent_examples": ["検証用アカウントに権限を付けたい"],
            "match_terms": ["検証用アカウント", "権限"],
        },
        "conditions": [
            {
                "id": "target",
                "label": "付与先",
                "type": "enum",
                "allowed_values": ["個別", "グループ"],
                "unknown_handling": "branch",
                "question": "権限は個別の利用者とグループのどちらに付けますか？",
            }
        ],
        "steps": [
            {"id": "register", "title": "アカウントを登録する", "allowed_tools": ["rag_search"]},
            {"id": "grant", "title": "権限を付与する", "depends_on": ["register"]},
            {"id": "ask", "title": "付与先を確かめる"},
        ],
        "branches": [
            {
                "id": "b1",
                "when": {"condition_id": "target", "values": ["個別"]},
                "goto_step": "grant",
            },
            {
                "id": "b2",
                "when": {"condition_id": "target", "operator": "unknown"},
                "goto_step": "ask",
            },
        ],
        "references": [{"document_id": "doc-1", "title": "運用手順書"}],
        "impact": {"scope": "group", "approval_required": True},
    }
    payload.update(overrides)
    return payload


def content(**overrides: Any) -> SupportGuideContent:
    return SupportGuideContent.model_validate(guide(**overrides))


# ---- 内容の検証 ----------------------------------------------------------------------------


def test_valid_guide_has_only_the_uncovered_branch_warning() -> None:
    issues = validate_content(content())
    assert not has_errors(issues)
    assert [issue.code for issue in issues] == ["branch_not_covered"]
    assert "グループ" in issues[0].message


def test_structural_errors_are_reported_with_paths() -> None:
    broken = content(
        goal={"expected_result": "結果"},
        steps=[
            {"id": "a", "title": "A", "depends_on": ["b", "missing"], "allowed_tools": ["shell"]},
            {"id": "b", "title": "B", "depends_on": ["a"]},
            {"id": "c", "title": "C"},
            {"id": "c", "title": "C2"},
        ],
        branches=[
            {
                "id": "x",
                "when": {"condition_id": "target", "values": ["全員"]},
                "goto_step": "nowhere",
            },
            {"id": "y", "when": {"condition_id": "nope", "values": ["1"]}, "goto_step": "a"},
        ],
        impact={"scope": "all", "steps": ["a", "gone"]},
    )
    issues = validate_content(broken)
    codes = {issue.code for issue in issues}
    assert {
        "goal_unmatchable",
        "duplicate_id",
        "unknown_dependency",
        "unknown_tool",
        "dependency_cycle",
        "unknown_goto",
        "unknown_value",
        "unknown_condition",
        "unknown_branch_missing",
        "wide_impact_without_approval",
        "unknown_impact_step",
    } <= codes
    # 影響範囲・承認が係る手順は、手順の id で決める（#1320）。
    impact = next(issue for issue in issues if issue.code == "unknown_impact_step")
    assert (impact.path, impact.severity) == ("impact.steps", "error")
    assert "gone" in impact.message and "「a" not in impact.message


def test_content_model_rejects_unsafe_or_inconsistent_values() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):  # 選択肢が 1 つの enum
        content(conditions=[{"id": "c", "label": "c", "allowed_values": ["a"], "question": "?"}])
    with pytest.raises(ValidationError):  # 確かめる問いの無い ask
        content(conditions=[{"id": "c", "label": "c", "type": "text"}])
    with pytest.raises(ValidationError):  # 宣言にない項目（式・script は受け付けない）
        content(
            branches=[
                {"id": "x", "when": {"condition_id": "target", "expr": "1"}, "goto_step": "a"}
            ]
        )
    with pytest.raises(ValidationError):  # id の形
        content(steps=[{"id": "1 step", "title": "x"}])
    with pytest.raises(ValidationError):  # 有効期間の逆転
        content(applicability={"effective_from": "2026-05-01", "effective_to": "2026-04-01"})
    # 指紋は内容が同じなら同じ。
    assert content_sha256(content()) == content_sha256(content())
    assert content_sha256(content()) != content_sha256(content(title="別"))


# ---- API ---------------------------------------------------------------------------------


class FakeStore:
    """SupportGuideStore のメモリの偽物（楽観ロック・公開の版・アーカイブ）。"""

    def __init__(self) -> None:
        self.heads: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, list[SupportGuideRevision]] = {}

    def _summary(self, guide_id: str) -> SupportGuideSummary:
        head = self.heads[guide_id]
        published = head["published_revision"]
        published_sha = (
            self.revisions[guide_id][published - 1].content_sha256 if published else None
        )
        return SupportGuideSummary(
            guide_id=guide_id,
            search_answer_profile_id=head["profile"],
            status=head["status"],
            title=head["draft"].title,
            draft_revision=head["draft_revision"],
            published_revision=published,
            has_unpublished_changes=published_sha != content_sha256(head["draft"]),
            updated_at=datetime(2026, 10, 7, tzinfo=UTC),
            updated_by=head["user"],
        )

    def _head(self, profile: str, guide_id: str) -> dict[str, Any]:
        head = self.heads.get(guide_id)
        if head is None or head["profile"] != profile:
            raise SupportGuideNotFoundError(guide_id)
        return head

    async def list_guides(
        self, profile: str, *, include_archived: bool = False
    ) -> list[SupportGuideSummary]:
        return [
            self._summary(guide_id)
            for guide_id, head in self.heads.items()
            if head["profile"] == profile and (include_archived or head["status"] == "active")
        ]

    async def get_guide(
        self, profile: str, guide_id: str
    ) -> tuple[SupportGuideSummary, SupportGuideContent]:
        head = self._head(profile, guide_id)
        return self._summary(guide_id), head["draft"]

    async def list_revisions(self, guide_id: str) -> list[SupportGuideRevisionSummary]:
        return [
            SupportGuideRevisionSummary(**revision.model_dump(exclude={"content"}))
            for revision in reversed(self.revisions.get(guide_id, []))
        ]

    async def get_revision(self, guide_id: str, revision: int) -> SupportGuideRevision:
        items = self.revisions.get(guide_id, [])
        if not 1 <= revision <= len(items):
            raise SupportGuideNotFoundError(guide_id)
        return items[revision - 1]

    async def published_contents(self, profile: str) -> list[tuple[str, int, SupportGuideContent]]:
        return [
            (
                guide_id,
                head["published_revision"],
                self.revisions[guide_id][head["published_revision"] - 1].content,
            )
            for guide_id, head in self.heads.items()
            if head["profile"] == profile
            and head["status"] == "active"
            and head["published_revision"]
        ]

    async def create_guide(
        self, profile: str, draft: SupportGuideContent, *, user: str | None
    ) -> str:
        guide_id = f"g{len(self.heads) + 1}"
        self.heads[guide_id] = {
            "profile": profile,
            "status": "active",
            "draft": draft,
            "draft_revision": 1,
            "published_revision": None,
            "user": user,
        }
        return guide_id

    async def save_draft(
        self,
        profile: str,
        guide_id: str,
        draft: SupportGuideContent,
        *,
        base_revision: int,
        user: str | None,
    ) -> int:
        head = self._head(profile, guide_id)
        if head["draft_revision"] != base_revision:
            raise SupportGuideConflictError(head["draft_revision"])
        head.update(draft=draft, draft_revision=head["draft_revision"] + 1, user=user)
        return int(head["draft_revision"])

    async def publish(
        self,
        profile: str,
        guide_id: str,
        draft: SupportGuideContent,
        *,
        base_revision: int,
        user: str | None,
        rollback_from: int | None = None,
    ) -> int:
        head = self._head(profile, guide_id)
        if head["draft_revision"] != base_revision:
            raise SupportGuideConflictError(head["draft_revision"])
        items = self.revisions.setdefault(guide_id, [])
        revision = len(items) + 1
        items.append(
            SupportGuideRevision(
                revision=revision,
                title=draft.title,
                content_sha256=content_sha256(draft),
                published_at=datetime(2026, 10, 7, tzinfo=UTC),
                published_by=user,
                rollback_from=rollback_from,
                content=draft,
            )
        )
        head["published_revision"] = revision
        if rollback_from is not None:
            head.update(draft=draft, draft_revision=head["draft_revision"] + 1)
        return revision

    async def set_status(
        self, profile: str, guide_id: str, status: str, *, user: str | None
    ) -> None:
        self._head(profile, guide_id)["status"] = status


class FakeOracle:
    def __init__(self) -> None:
        self.documents = {"doc-1": (FileStatus.INDEXED, ["kb-1"])}

    async def get_search_answer_profile(self, profile_id: str) -> Any:
        if profile_id != "bv-1":
            return None
        return SimpleNamespace(config=SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"]))

    async def get_document(self, document_id: str) -> Any:
        found = self.documents.get(document_id)
        return SimpleNamespace(status=found[0]) if found else None

    async def list_document_knowledge_bases(self, document_id: str) -> list[Any]:
        return [SimpleNamespace(id=kb) for kb in self.documents.get(document_id, (None, []))[1]]


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeStore, FakeOracle]:
    store, oracle = FakeStore(), FakeOracle()
    monkeypatch.setattr(route, "OracleClient", lambda: oracle)
    monkeypatch.setattr(route, "SupportGuideStore", lambda _oracle=None: store)
    return store, oracle


def test_publish_stores_intent_embeddings_only_when_semantic_match_is_on(
    fakes: tuple[FakeStore, FakeOracle], monkeypatch: pytest.MonkeyPatch
) -> None:
    """意味の照合が ON なら、公開・戻すときに質問の例の埋め込みを作って版ごとに持つ（#2398）。"""
    saved: list[tuple[str, int, int]] = []
    embedded: list[list[str]] = []

    class _Store:
        async def save_intent_embeddings(
            self, guide_id: str, revision: int, *, vectors: list[list[float]], **_: Any
        ) -> None:
            saved.append((guide_id, revision, len(vectors)))

    class _Embedder:
        async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
            embedded.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(support_guide_query, "SupportGuideStore", lambda _oracle: _Store())
    monkeypatch.setattr(support_guide_query, "guide_embedder", lambda _settings: _Embedder())
    guide_id = client.post(BASE, json={"draft": guide()}).json()["data"]["guide_id"]
    assert client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1}).status_code == 200
    assert (saved, embedded) == ([], [])

    settings = get_settings().model_copy(update={"rag_support_guide_semantic_match_enabled": True})
    monkeypatch.setattr(route, "get_settings", lambda: settings)
    assert client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1}).status_code == 200
    assert saved == [(guide_id, 2, 1)]
    assert embedded == [["検証用アカウントに権限を付けたい"]]

    # 埋め込みに失敗しても公開は取り消さない（照合のときに作り直す）。
    class _Broken:
        async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
            raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(support_guide_query, "guide_embedder", lambda _settings: _Broken())
    rolled = client.post(f"{BASE}/{guide_id}/rollback", json={"revision": 1, "base_revision": 1})
    assert rolled.status_code == 200
    assert rolled.json()["data"]["published_revision"] == 3


def test_draft_publish_conflict_and_history(fakes: tuple[FakeStore, FakeOracle]) -> None:
    created = client.post(BASE, json={"draft": guide()})
    assert created.status_code == 201
    detail = created.json()["data"]
    guide_id = detail["guide_id"]
    assert (detail["draft_revision"], detail["published_revision"]) == (1, None)
    assert detail["has_unpublished_changes"] is True
    assert [issue["code"] for issue in detail["issues"]] == ["branch_not_covered"]

    # 下書きの保存は版を照合する。
    saved = client.put(
        f"{BASE}/{guide_id}", json={"draft": guide(title="改訂"), "base_revision": 1}
    )
    assert saved.json()["data"]["draft_revision"] == 2
    stale = client.put(f"{BASE}/{guide_id}", json={"draft": guide(), "base_revision": 1})
    assert stale.status_code == 409
    assert "版 2" in stale.json()["error_messages"][0]

    assert client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1}).status_code == 409
    published = client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 2}).json()["data"]
    assert published["published_revision"] == 1
    assert published["published"]["title"] == "改訂"
    assert published["has_unpublished_changes"] is False
    # 公開の履歴に残す版の数（RAG_SUPPORT_GUIDE_MAX_PUBLISHED_REVISIONS。画面の案内に出す。#2356）。
    assert published["max_published_revisions"] == 5

    client.put(f"{BASE}/{guide_id}", json={"draft": guide(title="三訂"), "base_revision": 2})
    second = client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 3}).json()["data"]
    assert [item["revision"] for item in second["revisions"]] == [2, 1]
    assert client.get(f"{BASE}/{guide_id}/revisions/1").json()["data"]["content"]["title"] == "改訂"

    # 古い版を新しい版として公開し直す（下書きもその内容になる）。下書きを置き換えるので、
    # 読み込んだ下書きの版を照合する（ほかの人の保存した下書きを黙って失わない。#1278）。
    client.put(
        f"{BASE}/{guide_id}", json={"draft": guide(title="別の人の下書き"), "base_revision": 3}
    )
    stale_rollback = client.post(
        f"{BASE}/{guide_id}/rollback", json={"revision": 1, "base_revision": 3}
    )
    assert stale_rollback.status_code == 409
    assert "版 4" in stale_rollback.json()["error_messages"][0]
    assert "読み込み直してから戻してください" in stale_rollback.json()["error_messages"][0]
    kept = client.get(f"{BASE}/{guide_id}").json()["data"]
    assert (kept["draft"]["title"], kept["published_revision"]) == ("別の人の下書き", 2)
    # 照合の版は必須（省略は 422）。
    assert client.post(f"{BASE}/{guide_id}/rollback", json={"revision": 1}).status_code == 422
    rolled = client.post(
        f"{BASE}/{guide_id}/rollback", json={"revision": 1, "base_revision": 4}
    ).json()["data"]
    assert rolled["published_revision"] == 3
    assert rolled["draft_revision"] == 5
    assert rolled["revisions"][0]["rollback_from"] == 1
    assert (rolled["draft"]["title"], rolled["published"]["title"]) == ("改訂", "改訂")
    assert client.get(f"{BASE}/{guide_id}/revisions/9").status_code == 404


def test_publish_is_refused_when_validation_or_references_fail(
    fakes: tuple[FakeStore, FakeOracle],
) -> None:
    _, oracle = fakes
    broken = guide(steps=[{"id": "a", "title": "A", "depends_on": ["a"]}], branches=[])
    guide_id = client.post(BASE, json={"draft": broken}).json()["data"]["guide_id"]
    refused = client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1})
    assert refused.status_code == 422
    messages = refused.json()["error_messages"]
    assert messages[0] == "検証で問題が見つかったため公開できません。"
    assert any("自分自身に依存" in message for message in messages[1:])

    oracle.documents["doc-1"] = (FileStatus.REVIEW, ["kb-other"])
    ok_id = client.post(BASE, json={"draft": guide()}).json()["data"]["guide_id"]
    validation = client.post(f"{BASE}/{ok_id}/validate").json()["data"]
    assert validation["valid"] is False
    messages = [issue["message"] for issue in validation["issues"] if issue["severity"] == "error"]
    assert messages == ["資料「運用手順書」: このプロファイルのナレッジベースに含まれていません。"]
    assert client.post(f"{BASE}/{ok_id}/publish", json={"base_revision": 1}).status_code == 422


def test_archive_restore_list_and_export(fakes: tuple[FakeStore, FakeOracle]) -> None:
    guide_id = client.post(BASE, json={"draft": guide()}).json()["data"]["guide_id"]
    client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1})
    assert [item["guide_id"] for item in client.get(BASE).json()["data"]] == [guide_id]
    assert len(client.get(f"{BASE}/export").json()["data"]["guides"]) == 1

    archived = client.post(f"{BASE}/{guide_id}/archive").json()["data"]
    assert archived["status"] == "archived"
    assert client.get(BASE).json()["data"] == []
    assert client.get(f"{BASE}?include_archived=true").json()["data"][0]["status"] == "archived"
    assert client.get(f"{BASE}/export").json()["data"]["guides"] == []
    assert client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1}).status_code == 409
    assert client.post(f"{BASE}/{guide_id}/restore").json()["data"]["status"] == "active"
    assert client.get("/api/search-answer-profiles/missing/support-guides").status_code == 404
    assert client.get(f"{BASE}/unknown").status_code == 404


def test_import_preview_and_import_create_drafts_only(fakes: tuple[FakeStore, FakeOracle]) -> None:
    payload = {"guides": [guide(), {"title": "壊れた"}, guide(goal={"expected_result": "x"})]}
    preview = client.post(f"{BASE}/import/preview", json=payload).json()["data"]
    assert [item["valid"] for item in preview["items"]] == [True, False, False]
    assert preview["importable_count"] == 1
    assert client.post(f"{BASE}/import", json=payload).status_code == 422
    assert client.get(BASE).json()["data"] == []

    imported = client.post(f"{BASE}/import", json={"guides": [guide()]}).json()["data"]
    assert imported["created"][0]["published_revision"] is None


def test_condition_value_aliases_are_validated() -> None:
    from pydantic import ValidationError

    ok = content(
        conditions=[
            {
                "id": "target",
                "label": "付与先",
                "allowed_values": ["個別", "グループ"],
                "question": "どちらですか？",
                "value_aliases": {"個別": [" 検証用アカウント ", ""], "グループ": []},
            }
        ]
    )
    assert ok.conditions[0].value_aliases == {"個別": ["検証用アカウント"]}
    with pytest.raises(ValidationError):  # 選択肢に無い値の言い換え
        content(
            conditions=[
                {
                    "id": "target",
                    "label": "付与先",
                    "allowed_values": ["個別", "グループ"],
                    "question": "どちらですか？",
                    "value_aliases": {"全員": ["みんな"]},
                }
            ]
        )


# ---- 取込の差分（#1288） ------------------------------------------------------------------


def test_diff_contents_lists_added_removed_and_changed_rows() -> None:
    before = content()
    after = content(
        description="説明を足した",
        conditions=[
            {
                "id": "target",
                "label": "付与先",
                "type": "enum",
                "allowed_values": ["個別", "グループ", "全員"],
                "unknown_handling": "branch",
                "question": "権限は個別の利用者とグループのどちらに付けますか？",
            }
        ],
        steps=[
            {"id": "register", "title": "アカウントを登録する", "allowed_tools": ["rag_search"]},
            {"id": "grant", "title": "権限を付与する（承認の後）", "depends_on": ["register"]},
            {"id": "notify", "title": "利用者に知らせる"},
        ],
        branches=[
            {
                "id": "b1",
                "when": {"condition_id": "target", "values": ["個別"]},
                "goto_step": "grant",
            }
        ],
        references=[{"document_id": "doc-2", "title": "新しい手順書"}],
        impact={"scope": "all", "approval_required": True},
    )
    changes = [
        (change.section, change.kind, change.key, change.fields)
        for change in diff_contents(before, after)
    ]
    assert changes == [
        ("basic", "changed", "", ["description"]),
        ("conditions", "changed", "target", ["allowed_values"]),
        ("steps", "changed", "grant", ["title"]),
        ("steps", "added", "notify", []),
        ("steps", "removed", "ask", []),
        ("branches", "removed", "b2", []),
        ("references", "added", "doc-2", []),
        ("references", "removed", "doc-1", []),
        ("impact", "changed", "", ["scope"]),
    ]
    labels = {(change.section, change.key): change.label for change in diff_contents(before, after)}
    assert labels[("steps", "notify")] == "利用者に知らせる"
    assert labels[("references", "doc-1")] == "運用手順書"
    assert diff_contents(before, content()) == []


def test_import_preview_compares_with_the_existing_guide(
    fakes: tuple[FakeStore, FakeOracle],
) -> None:
    store, _ = fakes
    guide_id = client.post(BASE, json={"draft": guide()}).json()["data"]["guide_id"]
    client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1})
    # 公開の後に下書きを変えても、比べるのは公開の版。
    client.put(
        f"{BASE}/{guide_id}", json={"draft": guide(description="下書き"), "base_revision": 1}
    )
    draft_only = client.post(BASE, json={"draft": guide(title="未公開のガイド")}).json()["data"]

    changed = guide(steps=[*guide()["steps"], {"id": "notify", "title": "知らせる"}])
    renamed = {**guide(title="名前を変えた"), "guide_id": guide_id}
    payload = {
        "guides": [
            changed,
            renamed,
            guide(title="新しいガイド"),
            guide(title="未公開のガイド"),
            {"title": "壊れた"},
        ]
    }
    items = client.post(f"{BASE}/import/preview", json=payload).json()["data"]["items"]

    by_title = items[0]["existing"]
    assert (by_title["guide_id"], by_title["matched_by"]) == (guide_id, "title")
    assert (by_title["base"], by_title["revision"], by_title["status"]) == (
        "published",
        1,
        "active",
    )
    assert by_title["changes"] == [
        {"section": "steps", "kind": "added", "key": "notify", "label": "知らせる", "fields": []}
    ]
    by_id = items[1]["existing"]
    assert (by_id["guide_id"], by_id["matched_by"]) == (guide_id, "id")
    assert by_id["changes"][0]["fields"] == ["title"]
    assert items[2]["existing"] is None
    unpublished = items[3]["existing"]
    assert (unpublished["guide_id"], unpublished["base"], unpublished["revision"]) == (
        draft_only["guide_id"],
        "draft",
        1,
    )
    assert unpublished["changes"] == []
    assert items[4]["existing"] is None and items[4]["valid"] is False
    # 差分を出すだけで、既存のガイドは変えない。取り込みは別の下書きとして作る。
    assert store.heads[guide_id]["draft"].description == "下書き"
    imported = client.post(f"{BASE}/import", json={"guides": [renamed]}).json()["data"]
    assert imported["created"][0]["guide_id"] not in {guide_id, draft_only["guide_id"]}
    assert imported["created"][0]["title"] == "名前を変えた"


# ---- 下書きで試す（#1288） ----------------------------------------------------------------


def _preview_response(preview: GuidePreview, *, used: bool = True) -> SearchResponse:
    guide_summary = {
        "guide_id": preview.guide_id if used else "other",
        "revision": preview.draft_revision if used else 3,
        "title": preview.content.title,
        "decision": "branch",
        "known_conditions": [],
        "unknown_conditions": [{"id": "target", "label": "付与先", "state": "unknown"}],
        "applicability": {},
        **({"draft": True} if used else {}),
        # 回答の記録の業務ガイドの記録に移した手順ごとの充足と embedding の呼び出し（#2686）。
        "step_coverage": [
            {"step_id": "register", "status": "explained", "required": True},
            {"step_id": "grant", "status": "reference_only", "required": True},
            {"step_id": "ask", "status": "unused", "required": False},
            {"step_id": "removed", "status": "future_status", "required": True},
        ],
        "step_embedding": {"calls": 1, "texts": 12, "elapsed_ms": 250, "failed": False},
    }
    return SearchResponse(
        answer="権限は詳細画面の権限タブで付与します。",
        notice="変更前に管理窓口へ確認してください。",
        citations=[],
        trace_id=GUIDE_PREVIEW_TRACE_PREFIX + "0" * 32,
        elapsed_ms=12.0,
        diagnostics=SearchDiagnostics(
            answer={
                "outcome": "conditional",
                "guide": guide_summary,
                "envelope": {"clarifications": [{"condition_id": "target", "options": ["個別"]}]},
                GUIDE_PREVIEW_KEY: preview.marker(),
            }
        ),
    )


async def _async(value: Any) -> Any:
    return value


def test_try_draft_answers_with_the_draft_and_leaves_published_unchanged(
    fakes: tuple[FakeStore, FakeOracle], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[SearchRequest, GuidePreview]] = []

    async def fake_preview(request: SearchRequest, preview: GuidePreview) -> SearchResponse:
        calls.append((request, preview))
        return _preview_response(preview)

    monkeypatch.setattr(search_route, "run_guide_preview", fake_preview)
    guide_id = client.post(BASE, json={"draft": guide()}).json()["data"]["guide_id"]
    client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1})
    client.put(f"{BASE}/{guide_id}", json={"draft": guide(title="改訂"), "base_revision": 1})

    response = client.post(
        f"{BASE}/{guide_id}/try",
        json={"query": "検証用アカウントに権限を付けたい", "draft_revision": 2, "conditions": {}},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["guide_used"], data["draft_revision"], data["published_revision"]) == (
        True,
        2,
        1,
    )
    assert data["trace_id"].startswith(GUIDE_PREVIEW_TRACE_PREFIX)
    assert data["outcome"] == "conditional"
    assert data["notice"] == "変更前に管理窓口へ確認してください。"
    assert data["guide"]["decision"] == "branch"
    assert data["clarifications"][0]["condition_id"] == "target"
    # 手順ごとの充足に下書きの手順の題名を付けて返す（#2686。知らない状態の手順は返さない）。
    assert data["steps"] == [
        {
            "step_id": "register",
            "title": "アカウントを登録する",
            "status": "explained",
            "required": True,
        },
        {
            "step_id": "grant",
            "title": "権限を付与する",
            "status": "reference_only",
            "required": True,
        },
        {"step_id": "ask", "title": "付与先を確かめる", "status": "unused", "required": False},
    ]
    assert data["guide"]["step_embedding"]["calls"] == 1
    request, preview = calls[0]
    assert request.search_answer_profile_id == "bv-1"
    assert request.query == "検証用アカウントに権限を付けたい"
    assert (preview.guide_id, preview.draft_revision, preview.content.title) == (
        guide_id,
        2,
        "改訂",
    )
    assert preview.published_revision == 1

    # 公開の版・下書きの版は変わらない（試しは保存しない）。
    detail = client.get(f"{BASE}/{guide_id}").json()["data"]
    assert (detail["published_revision"], detail["draft_revision"]) == (1, 2)
    assert detail["published"]["title"] == "検証用アカウントに権限を付ける"
    assert [item["revision"] for item in detail["revisions"]] == [1]

    # 別のガイドで答えたときは「使われなかった」と返す。
    monkeypatch.setattr(
        search_route,
        "run_guide_preview",
        lambda request, preview: _async(_preview_response(preview, used=False)),
    )
    unused = client.post(
        f"{BASE}/{guide_id}/try", json={"query": "別の質問", "draft_revision": 2}
    ).json()["data"]
    assert unused["guide_used"] is False
    # 別のガイドの手順の充足は、この下書きの手順として返さない。
    assert unused["steps"] == []


def test_try_draft_refuses_stale_broken_or_archived_drafts(
    fakes: tuple[FakeStore, FakeOracle], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_preview(request: SearchRequest, preview: GuidePreview) -> SearchResponse:
        raise AssertionError("回答を作らない")

    monkeypatch.setattr(search_route, "run_guide_preview", fake_preview)
    guide_id = client.post(BASE, json={"draft": guide()}).json()["data"]["guide_id"]
    stale = client.post(f"{BASE}/{guide_id}/try", json={"query": "権限", "draft_revision": 9})
    assert stale.status_code == 409
    assert "読み込み直してから試してください" in stale.json()["error_messages"][0]
    empty = client.post(f"{BASE}/{guide_id}/try", json={"query": "", "draft_revision": 1})
    assert empty.status_code == 422

    broken = guide(steps=[{"id": "a", "title": "A", "depends_on": ["a"]}], branches=[])
    broken_id = client.post(BASE, json={"draft": broken}).json()["data"]["guide_id"]
    refused = client.post(f"{BASE}/{broken_id}/try", json={"query": "権限", "draft_revision": 1})
    assert refused.status_code == 422
    assert refused.json()["error_messages"][0] == (
        "検証で問題が見つかったため、この下書きでは試せません。"
    )

    client.post(f"{BASE}/{guide_id}/archive")
    archived = client.post(f"{BASE}/{guide_id}/try", json={"query": "権限", "draft_revision": 1})
    assert archived.status_code == 409
    missing = client.post(f"{BASE}/missing/try", json={"query": "権限", "draft_revision": 1})
    assert missing.status_code == 404


def test_try_draft_returns_the_ai_api_failure_reason_instead_of_500(
    fakes: tuple[FakeStore, FakeOracle], monkeypatch: pytest.MonkeyPatch
) -> None:
    """回答の作成で AI の割り当てを使い切ったら、定型の 500 でなく理由・目安・工程を返す（#2667）。

    呼び先の例外は合成の SDK の例外（HTTP status 無し、本文の JSON の文字列に割り当ての超過）。
    """
    import json

    import httpx
    import openai
    from rag_engine.generation.execution_record import AnswerExecutionError

    text = json.dumps(
        {"error": {"code": 429, "status": "resource-exhausted", "message": "synthetic quota"}}
    )

    class QuotaPipeline:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> SearchResponse:
            cause = openai.APIError(
                text,
                httpx.Request("POST", "https://ai.invalid/v1/responses"),
                body={"code": None, "message": text},
            )
            error = AnswerExecutionError(str(cause), "質問")
            error.execution_steps = (  # type: ignore[attr-defined]
                {"name": "回答の作成", "status": "エラーで中断"},
            )
            raise error from cause

    async def resolved(request: SearchRequest, settings: Any, **_kwargs: Any) -> tuple[Any, ...]:
        return request, settings, None, None

    monkeypatch.setattr(search_route, "_resolve_query_context", resolved)
    monkeypatch.setattr(search_route, "RagPipeline", QuotaPipeline)
    guide_id = client.post(BASE, json={"draft": guide()}).json()["data"]["guide_id"]

    response = client.post(f"{BASE}/{guide_id}/try", json={"query": "権限", "draft_revision": 1})

    assert response.status_code == 503
    [message] = response.json()["error_messages"]
    assert "割り当ての使い切り。中断した工程: 回答の作成" in message
    assert "割り当てが戻るまで時間をおいて再試行" in message
    assert "synthetic" not in response.text


def test_try_draft_requires_the_guide_management_permission(
    fakes: tuple[FakeStore, FakeOracle], monkeypatch: pytest.MonkeyPatch
) -> None:
    """検索・チャットの権限だけの利用者は試せない（403）。業務ガイドの管理者は試せる。"""
    store, _ = fakes

    async def fake_preview(request: SearchRequest, preview: GuidePreview) -> SearchResponse:
        return _preview_response(preview)

    monkeypatch.setattr(search_route, "run_guide_preview", fake_preview)
    guide_id = asyncio.run(store.create_guide("bv-1", content(), user=None))
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("searcher", ["menu.search", "menu.chat"])
    auth.user_with_permissions("guide-admin", ["menu.search_answer_profiles"])
    body = {"query": "検証用アカウントに権限を付けたい", "draft_revision": 1}

    denied = client.post(f"{BASE}/{guide_id}/try", json=body, headers=login(client, "searcher"))
    assert denied.status_code == 403
    assert denied.json()["error_code"] == ROUTE_FORBIDDEN_CODE
    admin = login(client, "guide-admin")
    allowed = client.post(f"{BASE}/{guide_id}/try", json=body, headers=admin)
    assert allowed.status_code == 200
    assert allowed.json()["data"]["guide_used"] is True


@pytest.mark.parametrize(
    "cell_range", ["A1048577", "A1:B1048577", "XFE1", "ZZZ1", "D10:B3", "D3:B10", "B10:D3"]
)
def test_sheet_cell_range_enforces_row_and_column_limits(cell_range: str) -> None:
    from pydantic import ValidationError

    from app.schemas.support_guide import SupportGuideSheetRange

    with pytest.raises(ValidationError):
        SupportGuideSheetRange(sheet_name="Sheet1", cell_range=cell_range)


def test_sheet_cell_range_accepts_last_excel_cell() -> None:
    from app.schemas.support_guide import SupportGuideSheetRange

    reference = SupportGuideSheetRange(sheet_name="Sheet1", cell_range="XFD1048576")
    assert reference.row_start == reference.row_end == 1_048_576


@pytest.mark.parametrize("value", ["invalid", "1", "yes please"])
def test_boolean_branch_refuses_values_that_cannot_match(value: str) -> None:
    draft = content(
        conditions=[
            {"id": "check", "label": "確認", "type": "boolean", "question": "確認済みですか？"}
        ],
        branches=[
            {"id": "b1", "when": {"condition_id": "check", "values": [value]}, "goto_step": "grant"}
        ],
    )
    assert any(issue.code == "unknown_value" for issue in validate_content(draft))


@pytest.mark.parametrize("value", ["はい", "いいえ", "TRUE", "false", "yes", "no"])
def test_boolean_branch_accepts_runtime_aliases(value: str) -> None:
    draft = content(
        conditions=[
            {"id": "check", "label": "確認", "type": "boolean", "question": "確認済みですか？"}
        ],
        branches=[
            {"id": "b1", "when": {"condition_id": "check", "values": [value]}, "goto_step": "grant"}
        ],
    )
    assert not has_errors(validate_content(draft))


def test_invalid_boolean_branch_cannot_be_published_or_imported(
    fakes: tuple[FakeStore, FakeOracle],
) -> None:
    draft = guide(
        conditions=[
            {"id": "check", "label": "確認", "type": "boolean", "question": "確認済みですか？"}
        ],
        branches=[
            {
                "id": "b1",
                "when": {"condition_id": "check", "values": ["invalid"]},
                "goto_step": "grant",
            }
        ],
    )
    created = client.post(BASE, json={"draft": draft})
    assert created.status_code == 201
    guide_id = created.json()["data"]["guide_id"]
    assert client.post(f"{BASE}/{guide_id}/publish", json={"base_revision": 1}).status_code == 422
    preview = client.post(f"{BASE}/import/preview", json={"guides": [draft]})
    assert preview.json()["data"]["items"][0]["valid"] is False
    assert client.post(f"{BASE}/import", json={"guides": [draft]}).status_code == 422


def test_diff_reports_relative_order_but_not_added_rows_as_reordering() -> None:
    before = content()
    reordered = before.model_copy(update={"steps": list(reversed(before.steps))})
    changes = diff_contents(before, reordered)
    assert changes
    assert all(change.section == "steps" and change.fields == ["order"] for change in changes)
    added = before.model_copy(
        update={"steps": [before.steps[0].model_copy(update={"id": "added"}), *before.steps]}
    )
    assert [
        (change.kind, change.key, change.fields) for change in diff_contents(before, added)
    ] == [("added", "added", [])]


@pytest.mark.parametrize(
    "status",
    ["explained", "partial", "unknown", "reference_only", "unused", "no_evidence", "not_searched"],
)
def test_preview_and_mcp_preserve_guide_coverage_status(status: str) -> None:
    from app.mcp.tools import ValidatedGuideStep
    from app.schemas.support_guide import SUPPORT_GUIDE_STEP_STATUSES

    draft = SupportGuideContent.model_validate(guide())
    values = route._try_steps(
        {"step_coverage": [{"step_id": "register", "status": status, "required": True}]}, draft
    )
    assert len(values) == 1 and values[0].status == status
    assert ValidatedGuideStep.model_validate(values[0].model_dump()).status == status
    assert (
        tuple(ValidatedGuideStep.model_json_schema()["properties"]["status"]["enum"])
        == SUPPORT_GUIDE_STEP_STATUSES
    )
