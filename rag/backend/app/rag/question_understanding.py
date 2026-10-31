"""質問理解のプロファイル範囲と、全体の言語資源からの解決。"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from rag_parser_core.language import FIELD_SPECS, LanguagePack, bind_pack, default_pack, resolve

from app.config import Settings
from app.rag.language_pack_releases import active_release, pack_from_release

# 取込で使う項目・回答の表示や検査の項目は、この編集面から変更しない。
QUESTION_UNDERSTANDING_SECTIONS = frozenset(
    {"question_cues", "inquiry_cues", "enumeration_cues", "definition_cues"}
)
QUESTION_UNDERSTANDING_FIELDS = tuple(
    spec
    for spec in FIELD_SPECS
    if not spec.ingest and spec.section in QUESTION_UNDERSTANDING_SECTIONS
)
QUESTION_UNDERSTANDING_KEYS = frozenset(
    (spec.section, spec.key) for spec in QUESTION_UNDERSTANDING_FIELDS
)


def resolved_profile_language(settings: Settings) -> LanguagePack:
    """全体の既定へ、この要求で指定されたプロファイルの上書きだけを重ねる。"""
    release = active_release()
    base = pack_from_release(release) if release is not None else default_pack()
    return resolve(base, settings.rag_language_pack_overrides)


@contextmanager
def profile_language_scope(settings: Settings) -> Iterator[LanguagePack]:
    """await と thread へ伝播し、終了・失敗時に必ず元の要求へ戻す。"""
    pack = resolved_profile_language(settings)
    with bind_pack(pack):
        yield pack


def question_understanding_overrides(
    overrides: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """質問理解面が所有する項目だけを選ぶ。保存済みの他用途の項目は残す。"""
    return {
        section: {
            key: value
            for key, value in fields.items()
            if (section, key) in QUESTION_UNDERSTANDING_KEYS
        }
        for section, fields in overrides.items()
        if any((section, key) in QUESTION_UNDERSTANDING_KEYS for key in fields)
    }


def replace_question_understanding(
    existing: Mapping[str, Mapping[str, object]], replacement: Mapping[str, Mapping[str, object]]
) -> dict[str, dict[str, object]]:
    result = {
        section: {
            key: value
            for key, value in fields.items()
            if (section, key) not in QUESTION_UNDERSTANDING_KEYS
        }
        for section, fields in existing.items()
    }
    for section, fields in replacement.items():
        result.setdefault(section, {}).update(fields)
    return {section: fields for section, fields in result.items() if fields}
