"""公開済み業務版を固定し、今回の Profile・列権限へ絞って生成に渡す。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import sqlglot

from .object_identity import object_match_key
from .ontology_definition_service import definition_fingerprint
from .ontology_definition_validation import (
    definition_expressions,
    mapping_column_key,
    mapping_object_key,
    validated_definition_expression,
)
from .ontology_definition_workspace import ProfileOntologyWorkspaceService
from .ontology_definitions import ProfileOntologyBundle
from .ontology_service import (
    BUSINESS_SCOPE_CHANGED_CODE,
    BUSINESS_SCOPE_CHANGED_MESSAGE_JA,
    OntologyGateBlockedError,
)


def require_current_scope(
    runtime: Any, profile_id: str, release: dict[str, Any]
) -> ProfileOntologyBundle:
    bundle = ProfileOntologyBundle.model_validate(release["bundle"])
    prepared = runtime.prepare_build_schema_context(profile_id)
    if (
        prepared.errors
        or bundle.requires_revalidation
        or bundle.profile_fingerprint
        != definition_fingerprint(runtime._strict_profile(profile_id).model_dump(mode="json"))
        or bundle.schema_context_fingerprint != definition_fingerprint(str(prepared.schema_context))
    ):
        raise OntologyGateBlockedError(
            BUSINESS_SCOPE_CHANGED_CODE, BUSINESS_SCOPE_CHANGED_MESSAGE_JA
        )
    return bundle


def legacy_markdown_in_scope(
    markdown: str,
    *,
    profile_objects: Iterable[str],
    allowed_tables: Iterable[str],
    allowed_columns: Mapping[str, Iterable[str]],
) -> str:
    """旧形式の公開 Markdown は、Profile 全体を許可した実行にだけ渡す。

    自由記述で概念単位に絞れないため、表や列を絞った実行に全文を渡すと
    許可外の業務記述・物理名が prompt に入る。
    """

    if any(list(columns) for columns in allowed_columns.values()):
        return ""
    requested = {object_match_key(name) for name in allowed_tables}
    required = {object_match_key(name) for name in profile_objects}
    if requested and not required <= requested:
        return ""
    return markdown


def _expressions_visible(
    definition: Any, allowed: dict[str, set[str]], definitions: dict[str, Any] | None = None
) -> bool:
    for field, sql, local_scope in definition_expressions(definition, definitions or {}, allowed):
        if not sql:
            continue
        try:
            validated_definition_expression(definition, field, sql, local_scope, allowed)
        except (ValueError, sqlglot.errors.SqlglotError):
            return False
    return True


def _allowed_scope(allowed_columns: Mapping[str, Iterable[str]]) -> dict[str, set[str]]:
    """許可された object → 列を `object_identity` の照合キーにそろえる。

    `schema_objects` と Profile の許可列はすでに照合キーのため値は変わらない。旧形式の小文字の
    キー（`app.orders`）は引用なしの識別子として大文字に解釈する。大文字化はしない（#573）。
    """

    return {
        object_match_key(name): {object_match_key(column) for column in columns}
        for name, columns in allowed_columns.items()
    }


def _mappings_visible(definition: Any, allowed: dict[str, set[str]]) -> bool:
    return all(
        mapping_object_key(m) in allowed
        and (not m.column_name or mapping_column_key(m) in allowed[mapping_object_key(m)])
        for m in definition.mappings
    )


def _visible_definitions(selected: dict[str, Any]) -> list[Any]:
    """オブジェクトの properties / 識別子 / 表示名を、渡す定義の中の参照だけにする。"""

    values = []
    for d in selected.values():
        if d.kind == "object_type":
            d = d.model_copy(
                update={
                    "properties": [p for p in d.properties if p in selected],
                    "primary_key": [p for p in d.primary_key if p in selected],
                    "title_property": d.title_property if d.title_property in selected else "",
                }
            )
        values.append(d)
    return values


def published_context(
    runtime: Any,
    profile_id: str,
    release_id: str,
    allowed_columns: dict[str, list[str]] | None = None,
) -> str:
    # 空 ID は旧 session。最新公開版で過去の問い合わせを再解釈しない。
    if not release_id:
        return ""
    from .ontology_markdown_workspace import SNAPSHOT, MarkdownOntologyWorkspace
    from .ontology_unified_model import DEFINITIONS, definition_references, render_llm_context

    record = runtime.store.get_artifact(release_id)
    if record and record.get("artifact_type") == SNAPSHOT:
        workspace = MarkdownOntologyWorkspace(runtime)
        # 種類の判定で読んだ行をそのまま使う（大きい snapshot を読み直さない。#1155）。
        snapshot = workspace.snapshot_from_record(profile_id, record)
        schema_context = workspace.scope_is_current(
            profile_id, snapshot["profile_hash"], snapshot["schema_hash"]
        )
        if not schema_context:
            raise OntologyGateBlockedError(
                BUSINESS_SCOPE_CHANGED_CODE, BUSINESS_SCOPE_CHANGED_MESSAGE_JA
            )
        from .ontology_definition_validation import schema_objects

        columns = schema_objects(json.loads(schema_context))
        allowed = _allowed_scope(allowed_columns if allowed_columns is not None else columns)
        definitions = {d.api_name: d for d in DEFINITIONS.validate_python(snapshot["definitions"])}
        selected = {
            name: d
            for name, d in definitions.items()
            if _expressions_visible(d, allowed, definitions) and _mappings_visible(d, allowed)
        }
        changed = True
        while changed:
            changed = False
            for name, d in list(selected.items()):
                refs = [ref for _, ref in definition_references(d)]
                if d.kind == "link_type":
                    refs.extend([d.source, d.target])
                if any(ref not in selected for ref in refs):
                    selected.pop(name)
                    changed = True
        # 自由記述に隠れた物理参照を漏らさないよう、scope を絞った場合は Q/A の節を渡さない。
        full_scope = all(
            name in allowed and set(cols) <= allowed[name] for name, cols in columns.items()
        )
        # LLM には公開した定義から作る業務の知識だけを渡す（下書きの Markdown の書き方・
        # 内部の情報・中身の無い節を渡さない。#1720）。
        from .ontology_build import qa_markdown_sections

        return render_llm_context(
            _visible_definitions(selected),
            qa_markdown_sections(snapshot["markdown"]) if full_scope else "",
        )
    svc = ProfileOntologyWorkspaceService(runtime)
    release = svc.release(profile_id, release_id)
    if release is None:
        raise OntologyGateBlockedError("BUSINESS_RELEASE_MISSING", "公開業務版が見つかりません。")
    bundle = require_current_scope(runtime, profile_id, release)
    if allowed_columns is None:
        from .ontology_definition_validation import schema_objects

        schema = schema_objects(
            json.loads(str(runtime.prepare_build_schema_context(profile_id).schema_context))
        )
        allowed_columns = {name: list(columns) for name, columns in schema.items()}
    allowed = _allowed_scope(allowed_columns)
    definitions = {d.api_name: d for d in bundle.definitions}
    selected = {}
    for name, d in definitions.items():
        if _expressions_visible(d, allowed, definitions) and _mappings_visible(d, allowed):
            selected[name] = d
    # 参照先が権限で落ちた定義は、間接経由でも prompt に混ぜない。
    changed = True
    while changed:
        changed = False
        for name, d in list(selected.items()):
            refs = [
                getattr(d, key, "")
                for key in ("object_type", "source", "target", "property", "time_property")
            ]
            for key in (
                "dependencies",
                "applies_to",
                "grain",
                "distinct_keys",
                "affected_properties",
            ):
                refs.extend(getattr(d, key, []))
            if any(ref and ref not in selected for ref in refs):
                selected.pop(name)
                changed = True
    from .ontology_unified_model import render_llm_context

    return render_llm_context(_visible_definitions(selected))
