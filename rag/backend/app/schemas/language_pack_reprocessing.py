"""公開版とレシピ改訂を確認してから行う再処理の要求。"""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.document import BULK_INGESTION_MAX_DOCUMENTS, IngestionJobPhase
from app.schemas.language_pack_release import LanguagePackIndexRelation, LanguagePackRelease


class ReprocessingSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_ids: list[str] = Field(min_length=1, max_length=BULK_INGESTION_MAX_DOCUMENTS)
    expected_release_id: str = Field(min_length=1, max_length=64)


class ReprocessingTarget(BaseModel):
    document_id: str
    file_name: str
    recipe_id: str
    slot_no: int
    config_revision: int
    used_release_id: str | None = None
    used_ingest_hash: str | None = None
    relation: LanguagePackIndexRelation = LanguagePackIndexRelation.UNRECORDED


class ReprocessingTargets(BaseModel):
    current_release: LanguagePackRelease
    targets: list[ReprocessingTarget]
    unavailable_count: int


class ReprocessingJobTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1, max_length=64)
    recipe_id: str = Field(min_length=1, max_length=64)
    phase: IngestionJobPhase
    expected_config_revision: int = Field(ge=1)


class ReprocessingJobsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_release_id: str = Field(min_length=1, max_length=64)
    targets: list[ReprocessingJobTarget] = Field(
        min_length=1, max_length=BULK_INGESTION_MAX_DOCUMENTS * 3
    )


class ReprocessingJobResult(BaseModel):
    document_id: str
    recipe_id: str
    outcome: str
    job_id: str | None = None
    reason: str | None = None


class ReprocessingJobsResult(BaseModel):
    items: list[ReprocessingJobResult]
    accepted_count: int
    rejected_count: int
