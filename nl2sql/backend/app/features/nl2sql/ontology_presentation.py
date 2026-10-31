"""表示専用の版情報。保存済み artifact/ETag/hash は変更しない。"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from .ontology_definitions import ProfileOntologyBundle
from .ontology_service import OntologyNotFoundError


def bundle_view(store: Any, bundle: ProfileOntologyBundle | dict[str, Any]) -> dict[str, Any]:
    value = (
        bundle.model_dump(mode="json")
        if isinstance(bundle, ProfileOntologyBundle)
        else dict(bundle)
    )
    source = value.get("source_revision_id")
    record = store.get_document("revisions", {"revision_id": source}) if source else None
    # store の version は更新回数。利用者向けの版は OntologyRevision payload にある。
    revision = record.get("payload") if record else None
    revision = revision if isinstance(revision, dict) else None
    version = revision.get("version") if revision else None
    # 旧共有 revision は許容するが別 Profile の版番号を表示しない。
    visible = revision and revision.get("profile_id", "") in ("", value.get("profile_id"))
    value["display_version"] = version if visible and type(version) is int and version > 0 else None
    return value


def release_view(service: Any, profile_id: str, release_id: str = "") -> dict[str, Any] | None:
    release = service.release(profile_id, release_id)
    if release is None:
        return None
    bundle = bundle_view(service.store, release["bundle"])
    return {**release, "bundle": bundle, "display_version": bundle["display_version"]}


def execution_view(service: Any, profile_id: str, value: dict[str, Any]) -> dict[str, Any]:
    release_id = value.get("release_id")
    release = None
    if release_id:
        with suppress(OntologyNotFoundError):
            release = release_view(service, profile_id, release_id)
    return {**value, "display_version": release["display_version"] if release else None}
