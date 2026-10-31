"""rag_poc のプロンプトの編集と反映。

rag_poc はプロンプトをファイル(`prompts/<key>.txt`)へ保存したが、rag では DB
(`rag_answer_prompts`)に保存し、rag_engine の runtime の ``prompt_overrides`` で渡す。
回答生成・文書解析・文書分割の編集可能な指示は、それぞれの設定画面で管理する。
検索・回答・監査・評価の自然言語の指示も編集でき、応答の型はコードで管理する。
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from rag_engine.knowledge.prompt_files import (
    IMAGE_RETRIEVAL_PROMPT_KEY,
    PROMPT_FILES,
    VLM_ANSWER_PROMPT_KEY,
)
from rag_engine.parsing.vision_prompt_rules import split_image_retrieval_prompt

INGESTION_PROMPT_REFS = {
    "heading_levels": "app.rag.ingestion_prompts:HEADING_LEVEL_PROMPT",
    "field_extraction": "app.rag.ingestion_prompts:FIELD_EXTRACTION_PROMPT",
    "navigation_summary": "app.rag.ingestion_prompts:NAVIGATION_SUMMARY_PROMPT",
    "raptor_summary_system": "app.rag.ingestion_prompts:RAPTOR_SUMMARY_SYSTEM_PROMPT",
}
EDITABLE_PROMPT_KEYS = (VLM_ANSWER_PROMPT_KEY, IMAGE_RETRIEVAL_PROMPT_KEY, *INGESTION_PROMPT_REFS)
ANSWER_PROMPT_MAX_CHARS = 50_000


def default_prompt(key: str) -> str:
    if key in INGESTION_PROMPT_REFS:
        return prompt_constant(INGESTION_PROMPT_REFS[key])
    if key in STAGE_PROMPT_REFS:
        return prompt_constant(STAGE_PROMPT_REFS[key])
    return PROMPT_FILES[key].default_content or ""


def required_placeholders(key: str) -> tuple[str, ...]:
    if key in STAGE_PROMPT_REFS:
        return tuple(
            dict.fromkeys(re.findall(r"{{([A-Za-z_][A-Za-z_0-9]*)}}", default_prompt(key)))
        )
    return () if key in INGESTION_PROMPT_REFS else PROMPT_FILES[key].required_placeholders


def validate_prompt(key: str, content: str) -> None:
    """rag_poc の save_prompt_file と同じ検証(空と必須 placeholder の欠落は保存しない)。"""
    if key not in EDITABLE_PROMPT_KEYS:
        raise KeyError(key)
    if not content.strip():
        raise ValueError("内容が空です。既定の内容を使う場合は「既定に戻す」を使ってください。")
    missing = [
        f"{{{{{name}}}}}" for name in required_placeholders(key) if f"{{{{{name}}}}}" not in content
    ]
    missing.extend(token for token in required_tokens(key) if token not in content)
    if missing:
        raise ValueError(f"必須の placeholder がありません: {', '.join(missing)}")


# 設定の概要の順を土台に、独立した知識の準備と、検索・回答の実行順を区別する。
PROMPT_PHASES = (
    "ingestion",
    "indexing",
    "knowledge",
    "question",
    "retrieval",
    "grounding",
    "generation",
    "audit",
    "evaluation",
)


@dataclass(frozen=True)
class PromptStage:
    """コードで管理する 1 段のプロンプト。

    ``parts`` は (部分の id, ``module:定数名``)。定数は実行の経路が使うものそのものを指す
    (画面に出す内容と実行で渡す内容をずらさない)。使われる条件の文は画面の i18n
    (``settings.answerPrompts.stage.<id>.condition``)に置く。
    """

    id: str
    phase: str
    parts: tuple[tuple[str, str], ...]


# 実行で使う固定のプロンプトをすべて、処理の順に並べる(#2054)。足したプロンプトをここに
# 載せ忘れると tests/test_answer_prompt_stages.py が失敗する。編集できる指示
# (EDITABLE_PROMPT_KEYS)は編集欄で示す。
PROMPT_STAGES: tuple[PromptStage, ...] = (
    PromptStage(
        "picture_description",
        "ingestion",
        (
            ("system", "rag_engine.adapters.oci:VISION_SYSTEM_PROMPT"),
            ("rules", "rag_engine.parsing.vision_prompt_rules:VISION_EXTRACTION_INSTRUCTION"),
            ("picture", "rag_engine.parsing.vision_prompt_rules:PICTURE_EXTRACTION_INSTRUCTION"),
            ("table", "rag_engine.parsing.vision_prompt_rules:TABLE_EXTRACTION_INSTRUCTION"),
        ),
    ),
    PromptStage(
        "heading_levels",
        "ingestion",
        (("instruction", "app.rag.ingestion_prompts:HEADING_LEVEL_PROMPT"),),
    ),
    PromptStage(
        "field_extraction",
        "ingestion",
        (("instruction", "app.rag.ingestion_prompts:FIELD_EXTRACTION_PROMPT"),),
    ),
    PromptStage(
        "navigation_summary",
        "ingestion",
        (("instruction", "app.rag.ingestion_prompts:NAVIGATION_SUMMARY_PROMPT"),),
    ),
    PromptStage(
        "raptor_summary",
        "indexing",
        (("system", "app.rag.ingestion_prompts:RAPTOR_SUMMARY_SYSTEM_PROMPT"),),
    ),
    # 回答の過程の表から業務ガイドの下書きを作る(#2397。検索・回答プロファイルの知識の作成)。
    PromptStage(
        "support_guide_draft",
        "knowledge",
        (
            ("system", "app.rag.support_guide_process_import:GUIDE_DRAFT_SYSTEM_PROMPT"),
            ("instruction", "app.rag.support_guide_process_import:GUIDE_DRAFT_PROMPT"),
        ),
    ),
    PromptStage(
        "support_guide_conditions",
        "question",
        (
            ("system", "app.rag.support_guide_conditions:CONDITION_SYSTEM_PROMPT"),
            # 確認に自由入力で答えた選択式の条件の選択肢を決める（#2726）。
            ("answerChoice", "app.rag.support_guide_conditions:ANSWER_SYSTEM_PROMPT"),
        ),
    ),
    PromptStage(
        "rule_selection",
        "question",
        (
            ("system", "app.rag.rule_selection:RULE_SELECTION_SYSTEM_PROMPT"),
            ("instruction", "app.rag.rule_selection:RULE_SELECTION_INPUT_PROMPT"),
        ),
    ),
    PromptStage(
        "history_rewrite",
        "question",
        (("system", "app.rag.history_question:HISTORY_REWRITE_SYSTEM_PROMPT"),),
    ),
    PromptStage(
        "field_filter",
        "question",
        (("system", "app.rag.field_filter_reader:FIELD_FILTER_SYSTEM_PROMPT"),),
    ),
    PromptStage(
        "routing",
        "question",
        (
            ("system", "rag_engine.generation.answering:QUERY_ROUTING_SYSTEM_PROMPT"),
            ("template", "rag_engine.generation.query_prompts:QUERY_ROUTING_PROMPT_TEMPLATE"),
        ),
    ),
    PromptStage(
        "expansion",
        "question",
        (
            ("system", "rag_engine.generation.answering:QUERY_EXPANSION_SYSTEM_PROMPT"),
            ("template", "rag_engine.generation.query_prompts:QUERY_EXPANSION_PROMPT_TEMPLATE"),
        ),
    ),
    PromptStage(
        "screen_linking",
        "retrieval",
        (("system", "rag_engine.retrieval.screen_catalog:LINK_SYSTEM_PROMPT"),),
    ),
    PromptStage(
        "article_catalog",
        "retrieval",
        (
            (
                "documentSystem",
                "rag_engine.retrieval.article_catalog:ARTICLE_DOCUMENT_SELECTION_SYSTEM_PROMPT",
            ),
            ("system", "rag_engine.retrieval.article_catalog:ARTICLE_SELECTION_SYSTEM_PROMPT"),
            ("refineSystem", "rag_engine.retrieval.article_catalog:ARTICLE_REFINE_SYSTEM_PROMPT"),
        ),
    ),
    PromptStage(
        "crag",
        "grounding",
        (
            ("system", "rag_engine.generation.answering:CRAG_GRADER_SYSTEM_PROMPT"),
            ("template", "rag_engine.generation.crag_support:CRAG_GRADE_PROMPT_TEMPLATE"),
        ),
    ),
    PromptStage(
        "section_extraction",
        "grounding",
        (("system", "rag_engine.generation.section_extraction:EXTRACTION_SYSTEM_PROMPT"),),
    ),
    PromptStage(
        "faq_answer",
        "generation",
        (("system", "rag_engine.generation.faq_answer:SYSTEM_PROMPT"),),
    ),
    PromptStage(
        "generation",
        "generation",
        (
            ("instruction", "rag_engine.generation.grounded:SCOPE_PLAN_SYSTEM_PROMPT"),
            ("system", "rag_engine.generation.grounded:GENERATE_SYSTEM_PROMPT"),
            ("plannedSystem", "rag_engine.generation.grounded:PLANNED_GENERATE_SYSTEM_PROMPT"),
            ("assumptionSystem", "rag_engine.generation.grounded:ASSUMPTION_SYSTEM_PROMPT"),
            (
                "citationRepair",
                "rag_engine.generation.citation_range_repair:CITATION_RANGE_REPAIR_SYSTEM_PROMPT",
            ),
            # 目録で選んだ条を生成が引かなかったときだけ、支える説明を検討する（#3526）。
            ("catalogReview", "rag_engine.generation.grounded:CATALOG_REVIEW_SYSTEM_PROMPT"),
        ),
    ),
    PromptStage(
        "audit",
        "audit",
        (
            ("detailedSystem", "rag_engine.generation.grounded:AUDIT_SYSTEM_PROMPT"),
            # 標準の監査。未使用の根拠は列挙せず、主張・要求・要件を判定する。
            ("factSystem", "rag_engine.generation.grounded:FACT_AUDIT_SYSTEM_PROMPT"),
            ("compactRule", "rag_engine.generation.grounded:AUDIT_COMPACT_REASON_RULE"),
        ),
    ),
    PromptStage(
        "application",
        "audit",
        (
            ("system", "rag_engine.generation.applications:APPLICATION_SYSTEM_PROMPT"),
            ("auditSystem", "rag_engine.generation.applications:APPLICATION_AUDIT_SYSTEM_PROMPT"),
        ),
    ),
    PromptStage(
        "answer_validation",
        "audit",
        (
            ("system", "rag_engine.evaluation.answer_validation:VALIDATION_SYSTEM_PROMPT"),
            # 表の対応と取り違えた段落を、表の正しい値に直す（#3005。取り違えがあるときだけ）。
            ("tableRevision", "rag_engine.generation.table_revision:REVISION_SYSTEM_PROMPT"),
            # 確認の答えがあるときだけ、勧める内容が答えの条件・制約と両立するかを比べる（#2726）。
            (
                "premiseConsistency",
                "rag_engine.evaluation.answer_validation:PREMISE_CONSISTENCY_SYSTEM_PROMPT",
            ),
        ),
    ),
    PromptStage(
        "evaluation",
        "evaluation",
        (
            ("scope", "rag_engine.evaluation.answer_eval:SCOPE_PROMPT"),
            # 網羅と、段落ごとの主張の裏付け（その段落の出典の根拠だけ）は別の呼び出し（#3576）。
            ("coverage", "rag_engine.evaluation.answer_eval:COVERAGE_PROMPT"),
            ("claim", "rag_engine.evaluation.answer_eval:CLAIM_PROMPT"),
            ("correctness", "rag_engine.evaluation.answer_eval:CORRECTNESS_PROMPT"),
        ),
    ),
)


# 実行の段に対応する自然言語の指示。既存の解析設定の key は維持する。
STAGE_PROMPT_REFS = {
    f"{stage.id}.{part_id}": ref
    for stage in PROMPT_STAGES
    if stage.phase not in {"ingestion", "indexing"} or stage.id == "picture_description"
    for part_id, ref in stage.parts
}
EDITABLE_PROMPT_KEYS = (*EDITABLE_PROMPT_KEYS, *STAGE_PROMPT_REFS)


def required_tokens(key: str) -> tuple[str, ...]:
    """入力テンプレートの単一括弧の差し込み口を保持する。"""
    if key not in STAGE_PROMPT_REFS:
        return ()
    return tuple(
        dict.fromkeys(re.findall(r"(?<!{){[A-Za-z_][A-Za-z_0-9]*}(?!})", default_prompt(key)))
    )


def stage_prompt_permission(key: str) -> str | None:
    """指示の配置先の設定権限。条件の認識は回答プロンプトに置く。"""
    for stage in PROMPT_STAGES:
        if key not in {f"{stage.id}.{part}" for part, _ref in stage.parts}:
            continue
        if stage.id == "picture_description":
            return "menu.settings_parser_adapters"
        if stage.phase == "evaluation":
            return "menu.settings_evaluation"
        if stage.id != "support_guide_conditions" and stage.phase in {
            "question",
            "retrieval",
            "grounding",
        }:
            return "menu.settings_retrieval"
        return "menu.settings_prompts"
    return None


def prompt_constant(ref: str) -> str:
    """``module:定数名`` の定数の内容。"""
    module_name, _, name = ref.partition(":")
    return str(getattr(import_module(module_name), name))


def separate_vision_prompt_rows(
    saved: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """旧一体型の保存内容を、読取時だけ独立した編集欄へ投影する。"""
    result = {key: dict(row) for key, row in saved.items()}
    row = result.get(IMAGE_RETRIEVAL_PROMPT_KEY)
    if row is not None:
        body, legacy = split_image_retrieval_prompt(str(row["content"]))
        row["content"] = body
        if legacy is not None and "picture_description.rules" not in result:
            result["picture_description.rules"] = {**row, "content": legacy}
    return result


def readonly_prompt_stages(
    *, image_prompt: str | None = None, overrides: Mapping[str, str] | None = None
) -> list[dict[str, object]]:
    """処理順の固定指示と、図の読み取りで実際に使うテンプレートを示す。"""
    stages: list[dict[str, object]] = []
    saved = dict(overrides or {})
    if image_prompt is not None:
        _body, legacy = split_image_retrieval_prompt(image_prompt)
        if legacy is not None:
            saved.setdefault("picture_description.rules", legacy)
    by_ref = {ref: saved[key] for key, ref in INGESTION_PROMPT_REFS.items() if key in saved}
    for stage in PROMPT_STAGES:
        parts = [
            {
                "id": part_id,
                "content": saved.get(
                    f"{stage.id}.{part_id}", by_ref.get(ref, prompt_constant(ref))
                ),
                "prompt_key": f"{stage.id}.{part_id}"
                if f"{stage.id}.{part_id}" in STAGE_PROMPT_REFS
                else None,
            }
            for part_id, ref in stage.parts
        ]
        if stage.id == "picture_description":
            # 解析と同じ正規化を使い、保存済みのテンプレートに足す共通規則も含める。
            parts.insert(
                1,
                {
                    "id": "template",
                    "content": split_image_retrieval_prompt(
                        image_prompt
                        if image_prompt is not None
                        else default_prompt(IMAGE_RETRIEVAL_PROMPT_KEY)
                    )[0],
                },
            )
            from rag_engine.adapters.oci import _picture_output_contract

            parts.append(
                {"id": "contract", "content": _picture_output_contract(), "prompt_key": None}
            )
        stages.append({"id": stage.id, "phase": stage.phase, "prompts": parts})
    return stages


@contextmanager
def prompt_overrides(overrides: Mapping[str, str]) -> Iterator[None]:
    """rag_engine の read_prompt が overrides を返すように、同期処理の間だけ runtime を有効にする。

    現在の指示の上書きを引き継ぎ、このスコープで指定した内容を優先する。
    保存先(paths.prompts)を持たないので、read_prompt はファイルを読まず上書きか既定値を返す。
    """
    if not overrides:
        yield
        return
    from rag_engine.resources.runtime import ResourcePaths, Runtime, current_runtime

    current = current_runtime()
    merged = {**dict(current.prompt_overrides if current else ()), **dict(overrides)}
    with tempfile.TemporaryDirectory(prefix="rag-engine-prompts-") as work:
        runtime = Runtime(
            ResourcePaths(workspace=Path(work), output=Path(work)),
            prompt_overrides=tuple(merged.items()),
        )
        with runtime.activate():
            yield
