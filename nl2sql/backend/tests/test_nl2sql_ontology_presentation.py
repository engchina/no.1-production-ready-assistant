"""表示版の投影と保存済み artifact/hash の不変条件。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_workspace import model, no_auth  # noqa: F401

from app.features.nl2sql.ontology_definition_workspace import ProfileOntologyWorkspaceService
from app.features.nl2sql.ontology_presentation import bundle_view, execution_view, release_view
from app.features.nl2sql.ontology_service import OntologyNotFoundError


def test_display_version_uses_revision_and_preserves_release_snapshots() -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    releases = []
    for version in (7, 13):
        revision_id = f"opaque-{version * 11}"
        revision = rt.store.save_document(
            "revisions",
            {
                "revision_id": revision_id,
                "payload": {"id": revision_id, "profile_id": "sales", "version": version},
            },
        )
        # 更新回数と業務版を故意に異ならせる。
        rt.store.save_document("revisions", revision, expected_etag=revision["etag"])
        bundle = svc.save_build(
            profile_id="sales",
            job_id=str(version),
            definitions=model(),
            schema_fingerprint="schema",
            source_revision_id=revision_id,
        )
        bundle = svc.review(
            "sales", bundle.id, bundle.etag, [d.id for d in bundle.definitions], None
        )
        bundle = svc.validate("sales", bundle.id, bundle.etag, None)
        head = svc.head("sales")["release_id"]
        release = svc.publish("sales", bundle.id, bundle.etag, head, str(version), None)
        saved = deepcopy(release)
        assert bundle_view(rt.store, svc.get("sales", bundle.id))["display_version"] == version
        assert (release_view(svc, "sales", release["id"]) or {})["display_version"] == version
        assert svc.release("sales", release["id"]) == saved
        assert "display_version" not in saved["bundle"]
        releases.append(release)
    historical = {"release_id": releases[0]["id"], "status": "succeeded", "result": 42}
    assert execution_view(svc, "sales", historical)["display_version"] == 7
    assert "display_version" not in historical
    assert (release_view(svc, "sales") or {})["display_version"] == 13
    head = svc.head("sales")
    svc.rollback(
        "sales",
        releases[0]["id"],
        head["release_id"],
        None,
        expected_etag=head["etag"],
        key="rollback",
    )
    assert (release_view(svc, "sales") or {})["display_version"] == 7
    assert execution_view(svc, "sales", {"release_id": releases[1]["id"]})["display_version"] == 13
    with pytest.raises(OntologyNotFoundError):
        release_view(svc, "finance", releases[0]["id"])


@pytest.mark.parametrize(
    "source,profile,version,expected",
    [
        ("missing", "sales", None, None),
        ("known", "sales", 4, 4),
        ("known", "finance", 4, None),
        ("known", "sales", True, None),
        ("known", "sales", 0, None),
    ],
)
def test_unresolvable_versions_never_guess_from_identity(
    source: str, profile: str, version: int | None, expected: int | None
) -> None:
    rt, _ = runtime()
    if source != "missing":
        rt.store.save_document(
            "revisions", {"revision_id": source, "profile_id": profile, "version": version}
        )
    value = {"id": "v999", "profile_id": "sales", "source_revision_id": source, "etag": "fixed"}
    original = deepcopy(value)
    store = SimpleNamespace(
        get_document=lambda *_: (
            None
            if source == "missing"
            else {"version": 99, "payload": {"profile_id": profile, "version": version}}
        )
    )
    assert bundle_view(store, value)["display_version"] == expected
    assert value == original
    assert (
        execution_view(ProfileOntologyWorkspaceService(rt), "sales", {"release_id": "missing"})[
            "display_version"
        ]
        is None
    )


@pytest.mark.asyncio
async def test_read_dtos_project_versions_without_rewriting_artifacts() -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.features.nl2sql.ontology_capability_router import create_capability_router
    from app.features.nl2sql.ontology_router import _raise_domain_error
    from app.features.nl2sql.ontology_workspace_router import create_workspace_router

    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    rt.store.save_document(
        "revisions",
        {"revision_id": "source", "payload": {"id": "source", "profile_id": "sales", "version": 1}},
    )
    b = svc.save_build(
        profile_id="sales",
        job_id="api",
        definitions=model(),
        schema_fingerprint="schema",
        source_revision_id="source",
    )
    b = svc.review("sales", b.id, b.etag, [d.id for d in b.definitions], None)
    b = svc.validate("sales", b.id, b.etag, None)
    release = svc.publish("sales", b.id, b.etag, "", "api", None)
    saved = deepcopy(rt.store.list_artifacts(svc._session("sales")))
    app = FastAPI()
    app.include_router(create_workspace_router(lambda: rt, _raise_domain_error))
    app.include_router(create_capability_router(lambda: rt, _raise_domain_error))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/profiles/sales/ontology-results/{b.id}/workspace")
        assert response.status_code == 200
        view = response.json()["data"]
        assert view["bundle"]["display_version"] == 1
        assert view["head"]["display_version"] == 1
        assert view["releases"][0]["display_version"] == 1
        catalog = (await client.get("/profiles/sales/ontology-capabilities")).json()["data"]
        assert catalog["display_version"] == 1
        assert catalog["release_id"] == release["id"]
    assert rt.store.list_artifacts(svc._session("sales")) == saved
