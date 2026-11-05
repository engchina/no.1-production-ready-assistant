"""検索・回答プロファイルのスキーマ。

KB は文書 membership の範囲、文書レシピは加工設定を持つ。
プロファイルは参照 KB・検索/回答の上書き・用途専用の知識をまとめる。
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.rag.search_answer_profile_config import (
    SearchAnswerProfileConfig,
)
from app.schemas.knowledge_base import (
    DESCRIPTION_REQUIRED_MESSAGE,
    KnowledgeBaseRef,
    KnowledgeBaseStatus,
)

DEFAULT_SEARCH_ANSWER_PROFILE_NAME = "DEFAULT"
# DEFAULT は改名できないため、説明は既定の文言を補う（作成・読み込み時と migration。#521）。
DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION = (
    "DEFAULT ナレッジベースを検索・回答に使う、既定の検索・回答プロファイルです。"
)


class SearchAnswerProfileStatus(StrEnum):
    """検索・回答プロファイルの運用状態。"""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class SearchAnswerProfileRef(BaseModel):
    """他スキーマへ埋め込む軽量な検索・回答プロファイル参照。"""

    id: str
    name: str


class SearchAnswerProfileKnowledgeBaseRef(KnowledgeBaseRef):
    """検索・回答プロファイルが参照する KB の {id, name, status}(アーカイブ済みを含む)。"""

    status: KnowledgeBaseStatus


class SearchAnswerProfileTarget(SearchAnswerProfileRef):
    """質問から検索・回答プロファイルを推薦するときの候補(#1850)。

    `knowledge_bases` は検索できる参照 KB だけ(ACTIVE で、利用者が使える KB)。
    """

    description: str | None = None
    knowledge_bases: list[KnowledgeBaseRef] = Field(default_factory=list)


class SearchAnswerProfileSummary(SearchAnswerProfileRef):
    """一覧表示用の検索・回答プロファイル要約。"""

    description: str | None = None
    status: SearchAnswerProfileStatus
    knowledge_base_count: int = 0
    archived_knowledge_base_count: int = Field(
        default=0,
        description="参照 KB のうちアーカイブ済みの件数(検索対象にならない。#302)。",
    )
    missing_knowledge_base_count: int = Field(
        default=0,
        description="参照 KB のうち tenant に存在しない件数(検索対象にならない。#302)。",
    )
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class SearchAnswerProfileDetail(SearchAnswerProfileSummary):
    """詳細表示用の検索・回答プロファイル情報。"""

    config: SearchAnswerProfileConfig = Field(default_factory=SearchAnswerProfileConfig)
    knowledge_bases: list[SearchAnswerProfileKnowledgeBaseRef] = Field(
        default_factory=list,
        description=(
            "参照 KB の解決済み一覧(存在する KB のみ。アーカイブ済みを含み、status で見分ける)。"
        ),
    )
    missing_knowledge_base_ids: list[str] = Field(
        default_factory=list,
        description="参照 KB のうち tenant に存在しない ID(#302)。",
    )


class SearchAnswerProfileCreateRequest(BaseModel):
    """検索・回答プロファイル作成 request。"""

    name: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., max_length=2000)
    config: SearchAnswerProfileConfig = Field(default_factory=SearchAnswerProfileConfig)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _search_answer_profile_name(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        """前後空白を取り、空の説明を拒否する（#521）。"""
        return _required_clean_text(value, DESCRIPTION_REQUIRED_MESSAGE)


class SearchAnswerProfileUpdateRequest(BaseModel):
    """検索・回答プロファイル更新 request。指定フィールドのみ更新する。"""

    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    config: SearchAnswerProfileConfig | None = Field(
        default=None,
        description="指定時は設定一式を置換する。",
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _search_answer_profile_name(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str:
        """指定したときは空・空白だけ・null を拒否する。省略すれば変更しない（#521）。"""
        return _required_clean_text(value or "", DESCRIPTION_REQUIRED_MESSAGE)


def _required_clean_text(value: str, message: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(message)
    return cleaned


def _search_answer_profile_name(value: str) -> str:
    cleaned = _required_clean_text(value, "名前を入力してください。")
    if cleaned.casefold() == DEFAULT_SEARCH_ANSWER_PROFILE_NAME.casefold():
        raise ValueError("DEFAULT は予約名のため使用できません。")
    return cleaned


class InheritedAnswerContactData(BaseModel):
    """検索・回答プロファイルが継承する、システム設定の窓口（読み取りだけ。#2683）。

    回答の注意書き（運用設定。#2405）の窓口を、回答に使うのと同じ値で返す。画面はプロファイルの
    「窓口の上書き」が空のときに継承する窓口と、有効な窓口の出どころを示す。
    """

    enabled: bool = Field(description="回答の注意書きが有効か（無効なら窓口の案内を出さない）。")
    contact: str = Field(default="", description="システム設定の窓口（無効なら空）。")
