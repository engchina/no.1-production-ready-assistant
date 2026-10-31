"""版の再構成は冪等、異なる内容は衝突として扱う。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.features.nl2sql.ontology_models import (
    OntologyNode,
    OntologyNodeKind,
    OntologyProvenance,
    OntologyRevision,
    OntologySourceKind,
)
from app.features.nl2sql.ontology_service import (
    OntologyQuerySessionService,
    OntologyVersionConflictError,
)


def test_concurrent_identical_revision_registration_is_idempotent() -> None:
    service = OntologyQuerySessionService()

    def register(_index: int) -> str:
        return service.register_revision(OntologyRevision(id="synthetic", version=1)).etag

    with ThreadPoolExecutor(max_workers=4) as executor:
        etags = list(executor.map(register, range(16)))
    assert len(set(etags)) == 1


def test_same_revision_id_with_different_content_is_rejected() -> None:
    service = OntologyQuerySessionService()
    service.register_revision(OntologyRevision(id="synthetic", version=1))
    with pytest.raises(OntologyVersionConflictError):
        service.register_revision(OntologyRevision(id="synthetic", version=2))


def test_observation_time_is_ignored_but_metadata_is_immutable() -> None:
    service = OntologyQuerySessionService()
    revision = OntologyRevision(id="synthetic", version=1)
    node = OntologyNode(
        id="node-a",
        revision_id=revision.id,
        kind=OntologyNodeKind.TABLE,
        business_name_ja="表 A",
        provenance=OntologyProvenance(source_kind=OntologySourceKind.INTROSPECTED),
        metadata={"created_at": "value-a"},
    )
    service.register_revision(revision, nodes=[node])
    refreshed = node.model_copy(deep=True)
    refreshed.provenance.observed_at = refreshed.provenance.observed_at.replace(year=2025)
    service.register_revision(revision, nodes=[refreshed])
    refreshed.metadata["created_at"] = "value-b"
    with pytest.raises(OntologyVersionConflictError):
        service.register_revision(revision, nodes=[refreshed])
