"""公開した言語資源と、索引が使用した版の追跡契約。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LanguagePackRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = Field(ge=1)
    effective: dict[str, Any]
    hash: str
    ingest_hash: str
    published_at: datetime
    published_by: str | None = None
    previous_id: str | None = None
    restored_from: str | None = None
    change_note: str = ""


class LanguagePackReleaseState(BaseModel):
    draft_revision: int = Field(ge=1)
    draft_effective: dict[str, Any]
    current: LanguagePackRelease


class LanguagePackDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    overrides: dict[str, dict[str, Any]]


class LanguagePackPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    expected_current_id: str = Field(min_length=1, max_length=64)
    change_note: str = Field(default="", max_length=2000)


class LanguagePackRestoreRequest(LanguagePackPublishRequest):
    """以前の完全な版を新しい公開操作として有効にする。"""


class LanguagePackIndexRelation(StrEnum):
    CURRENT = "current"
    EQUIVALENT = "equivalent"
    DIFFERENT = "different"
    UNRECORDED = "unrecorded"


class LanguagePackFieldDifference(BaseModel):
    section: str
    key: str
    before: Any = None
    after: Any = None
    ingest: bool


class LanguagePackReleaseComparison(BaseModel):
    before_id: str
    after_id: str
    differences: list[LanguagePackFieldDifference]
    ingest_changed: bool
    # 依存を特定できない場合は工程を推定せず、確認が必要なことを返す。
    impact_requires_review: bool = False
    affected_stages: list[str] = Field(default_factory=list)


class LanguagePackReleaseList(BaseModel):
    releases: list[LanguagePackRelease]
    total: int = Field(ge=0)


class LanguagePackRecipeUse(BaseModel):
    recipe_id: str | None = None
    chunk_set_id: str | None = None
    release_id: str | None = None
    version: int | None = None
    ingest_hash: str | None = None
    relation: LanguagePackIndexRelation
    built_at: datetime | None = None
    job_id: str | None = None
    target_release_id: str | None = None


class LanguagePackDocumentUse(BaseModel):
    current_release_id: str
    current_version: int
    recipes: list[LanguagePackRecipeUse] = Field(default_factory=list)
