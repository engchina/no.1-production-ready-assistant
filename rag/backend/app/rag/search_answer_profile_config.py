"""検索・回答プロファイル(Search Answer Profile)の設定解決。

検索・回答プロファイルは「利用者(回答する側)視点」のエンティティで、**複数の KB を束ねた
参照集合**と、**1 つの一貫した回答の設定**を持つ。KB(加工する側視点)とは関心事が
異なるため別レイヤーとして扱う。

設計:

* 参照 KB は多対多。1 つの KB を複数の検索・回答プロファイルから共有できる。逆も可。
* query 上書き(回答の検索と生成・安全チェック)は KB の :class:`KnowledgeBaseQueryConfig` を
  再利用する。複数 KB の query 設定は競合するため、検索時はこの**検索・回答プロファイル 1 枚から**
  解決する。KB 個別の query legacy 値は使わない。
* persona(system prompt / 既定言語)は旧 standard の回答エンジンだけが使っていたため #595 で
  削除した。保存済みの値は ``extra=ignore`` で読み込み時に捨て、次回保存で消える。
* 取込系(Preprocess / Parser / Chunking / Vector Index build)は KB の物理索引方法なので
  検索・回答プロファイルでは触らない。
* 永続化は ``rag_search_answer_profiles.profile_config`` JSON カラムに一括格納する(DDL 最小)。
* 解決順は 検索・回答プロファイル > グローバル既定。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Literal

from pr_system_settings.answer_notice import (
    ANSWER_NOTICE_CONTACT_MAX_CHARS,
    normalize_notice_contact,
)
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from rag_parser_core.language import FIELD_SPECS, LanguagePackError, builtin_pack, resolve

from app.config import Settings, enterprise_ai_model_catalog
from app.rag.kb_adapter_config import (
    KnowledgeBaseQueryConfig,
    compose_query_settings,
)

logger = logging.getLogger(__name__)

SEARCH_ANSWER_PROFILE_CONFIG_VERSION = 1
MAX_SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES = 200
# 窓口の上書きの長さの上限（システム設定の窓口と同じ。#2400）。
CONTACT_OVERRIDE_MAX_CHARS = ANSWER_NOTICE_CONTACT_MAX_CHARS


# 検索方法の全体設定だけで決め、プロファイルへは保存・適用しない。
PROFILE_GLOBAL_QUERY_FIELDS = frozenset(
    (
        "query_strategy",
        "answer_flow",
        "neighbor_child_count",
        "rerank_enabled",
        "auto_field_filter_enabled",
        "graph_expansion_enabled",
        "graph_expansion_max_chunks",
        "screen_linking_enabled",
        "request_coverage_retrieval_enabled",
        "guide_step_retrieval_enabled",
        "guide_step_max_queries",
        "guide_step_deadline_seconds",
        "entity_expansion_enabled",
        "entity_expansion_max_chunks",
        "section_expansion_enabled",
        "section_expansion_max_levels",
        "section_expansion_input_token_percent",
    )
)


class ProfileAnswerModelUnavailableError(ValueError):
    """回答設定が指すモデルが現在の登録にない。"""


class SearchAnswerProfileConfig(BaseModel):
    """検索・回答プロファイルの設定一式(参照 KB + query 上書き)。"""

    model_config = ConfigDict(extra="ignore")

    version: int = SEARCH_ANSWER_PROFILE_CONFIG_VERSION
    answer_model_id: str | None = Field(
        default=None,
        max_length=256,
        description="回答に使う登録済みモデル。null は全体の既定を継承する。",
    )

    @field_validator("answer_model_id")
    @classmethod
    def _normalize_answer_model(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    knowledge_base_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES,
        description="束ねる参照 KB の ID 群(多対多)。検索時にこの集合を検索対象へ展開する。",
    )
    query: KnowledgeBaseQueryConfig = Field(default_factory=KnowledgeBaseQueryConfig)

    @field_serializer("query")
    def _serialize_query(self, value: KnowledgeBaseQueryConfig) -> dict[str, object]:
        return value.model_dump(mode="json", exclude=set(PROFILE_GLOBAL_QUERY_FIELDS))

    # 回答前の確認（#1839）の方針。confirm = 曖昧な質問を確認してから回答する（ルールごとの確認・
    # 業務ガイドの条件の確認を出す）/ assume = 確認せずに前提を示して回答する
    # （チャットに確認を出さない）。
    # 1 つの質問で聞き返すのは 1 回だけ（#717）なので、確認の上限（往復）は持たない。
    clarification_policy: Literal["confirm", "assume"] = "confirm"
    # 言語資源の上書き（#2181。全体の言語資源に重ねる「節 → 項目 → 値」）。検索・回答で使う項目
    # （ingest=False）だけを持てる。取込の項目は文書の分割と食い違うので持たない。
    language_pack: dict[str, dict[str, object]] = Field(default_factory=dict)
    # 回答の注意書きの窓口の上書き（#2400。業務ごとに窓口が違うとき。窓口の名前・連絡の方法の
    # 1 行）。空ならシステム設定の窓口。業務ガイドの人への引き継ぎの窓口（handoff.contact）が優先。
    contact_override: str = Field(default="", max_length=CONTACT_OVERRIDE_MAX_CHARS * 2)

    @field_validator("contact_override")
    @classmethod
    def _validate_contact_override(cls, value: str) -> str:
        normalized = normalize_notice_contact(value or "")
        if len(normalized) > CONTACT_OVERRIDE_MAX_CHARS:
            raise ValueError(f"窓口は {CONTACT_OVERRIDE_MAX_CHARS} 字以内で入力してください。")
        return normalized

    @field_validator("language_pack")
    @classmethod
    def _validate_language_pack(
        cls, value: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        specs = {(spec.section, spec.key): spec for spec in FIELD_SPECS}
        cleaned: dict[str, dict[str, object]] = {}
        for section, fields in (value or {}).items():
            for key, item in (fields or {}).items():
                spec = specs.get((section, key))
                if spec is None:
                    raise ValueError(f"{section}.{key}: 知らない項目です。")
                if spec.ingest:
                    raise ValueError(
                        f"{section}.{key}: 取込の項目はプロファイルで上書きできません。"
                    )
                cleaned.setdefault(section, {})[key] = item
        if cleaned:
            try:
                resolve(builtin_pack(), cleaned)
            except LanguagePackError as exc:
                raise ValueError(str(exc)) from exc
        return cleaned

    def normalized_knowledge_base_ids(self) -> list[str]:
        """参照 KB ID の前後空白・重複を取り除く。"""
        return _unique_clean_ids(self.knowledge_base_ids)


def parse_search_answer_profile_config(
    raw: Mapping[str, object] | None,
) -> SearchAnswerProfileConfig:
    """``profile_config`` JSON から検索・回答プロファイル設定を寛容に復元する。"""
    if not raw:
        return SearchAnswerProfileConfig()
    try:
        return SearchAnswerProfileConfig.model_validate(dict(raw))
    except Exception:  # noqa: BLE001 - 壊れた永続値は空設定へ縮退して検索を止めない
        logger.warning(
            "検索・回答プロファイル設定の復元に失敗したため空へ縮退します。", exc_info=True
        )
        return SearchAnswerProfileConfig()


def dump_search_answer_profile_config(config: SearchAnswerProfileConfig) -> dict[str, object]:
    """検索・回答プロファイル設定を ``profile_config`` カラムへ保存する dict へ変換する。"""
    return config.model_dump(mode="json")


def validate_profile_answer_model(settings: Settings, config: SearchAnswerProfileConfig) -> None:
    """登録済みのモデルだけを参照し、登録削除後の別モデルへの切り替えを防ぐ。"""
    if config.answer_model_id and not any(
        model.model_id == config.answer_model_id for model in enterprise_ai_model_catalog(settings)
    ):
        raise ProfileAnswerModelUnavailableError(
            "回答設定で選んだモデルが登録されていません。検索・回答プロファイルの回答モデルを選び直すか、全体の既定を継承してください。"
        )


def resolve_search_answer_profile_settings(
    global_settings: Settings,
    config: SearchAnswerProfileConfig,
    *,
    apply_answer_model: bool = True,
) -> tuple[Settings, bool]:
    """グローバルへ検索・回答プロファイルの query 上書きを重ねた Settings。

    KB はナレッジ構築設定だけを持つため、KB に残る query legacy 値はここでは扱わない。
    戻り値 2 番目は上書きが実際に効いたかどうか。
    """
    if apply_answer_model:
        validate_profile_answer_model(global_settings, config)
    overlays = [config.query.model_copy(update={key: None for key in PROFILE_GLOBAL_QUERY_FIELDS})]
    settings, applied = compose_query_settings(global_settings, overlays)
    # 使用の意思を持たない既存プロファイルは、全体の値にかかわらず OFF。
    guide_enabled = config.query.support_guide_enabled is True
    if settings.rag_support_guide_enabled != guide_enabled:
        settings = settings.model_copy(update={"rag_support_guide_enabled": guide_enabled})
        applied = True
    if apply_answer_model and config.answer_model_id:
        settings = settings.model_copy(
            update={"oci_enterprise_ai_default_text_model": config.answer_model_id}
        )
        applied = True
    if settings.rag_language_pack_overrides != config.language_pack:
        settings = settings.model_copy(update={"rag_language_pack_overrides": config.language_pack})
    if config.language_pack:
        applied = True
    if config.contact_override:
        # 回答の注意書きの窓口（#2400）。検索・回答の挙動は変えないので applied にしない。
        settings = settings.model_copy(
            update={
                "rag_answer_contact_override": config.contact_override,
            }
        )
    return settings, applied


def _unique_clean_ids(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned
