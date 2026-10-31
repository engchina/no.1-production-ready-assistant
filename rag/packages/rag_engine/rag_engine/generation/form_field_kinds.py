"""根拠の画面の図の項目の種類を、回答の材料と業務ガイドの条件に使う（#2404）。

取込の Vision は画面の図の項目（``form_fields``）に種類（``kind``: select / text / checkbox / radio / date /
button / unknown）を付け、chunk の metadata（``image_evidence[].visual_structure.form_fields``）に残す。
回答では、質問に名前が出た項目だけを根拠から拾い、

- 項目名と種類を引用できる画像由来の根拠にし、対応する根拠の注記を生成の材料へ渡す（表示の語は言語資源）。
- 業務ガイドの分からなかった条件（backend が ``evidence_conditions`` として渡す）のうち、選択肢が項目の種類に
  対応する条件（``condition_kind_options``）を、拾った種類が 1 つに決まるときだけ既知にする。種類が割れる・
  拾えないときは決めない（今までどおり）。
- 条件が決まったら、backend が決まり方ごとに前もって作った業務ガイドの手順の計画（``evidence_plans``。#2689）に
  切り替える: 外れた分岐の手順を回答の手順から外し、決まった分岐の手順を必須にし、業務ガイドのルールの本文を
  決まった条件の版にする（``with_resolved_plan``）。分岐の判断は backend の業務ガイドの照合が正本で、ここでは
  決まった条件の組で計画を選ぶだけ。

照合は決定的（LLM を呼ばない）。項目名は文字の形（NFKC・大小・記号・空白）をそろえて、質問の中に含まれるかで比べる。
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from rag_engine.generation.answer_payload import _metadata_image_evidence
from rag_engine.models.llm import form_field_kind
from rag_engine.parsing.content_labels import content_labels

# 質問の中に名前を探す項目名の最短の長さ（そろえた後の文字数）。1 字の項目名は質問の別の語に当たりやすい。
MIN_FIELD_NAME_CHARS = 2
# 生成の材料に出す観測の上限（同じ項目・種類・根拠は 1 つにまとめた後）。
MAX_NOTE_OBSERVATIONS = 12

# 業務ガイドの条件の形（backend が照合の結果から渡す）: (条件の id, 名前, ((選択肢, (言い換え, ...)), ...))。
EvidenceCondition = tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]]
# 条件が決まったときの業務ガイドの手順の計画（backend が渡す。#2689）:
# (決まった条件の (id, 値) の列, 残る手順の (id, 必須か) の列, 業務ガイドのルールの id, そのルールの本文)。
EvidencePlan = tuple[tuple[tuple[str, str], ...], tuple[tuple[str, bool], ...], str, str]


@dataclass(frozen=True)
class FieldKindObservation:
    """根拠（parent の source_id）の画面の図で、質問に名前が出た項目とその種類。"""

    source_id: str
    name: str
    kind: str
    page: int | None = None


@dataclass(frozen=True)
class ResolvedCondition:
    """根拠の画面の図の項目の種類で値が決まった業務ガイドの条件。"""

    condition_id: str
    label: str
    value: str
    kind: str


def normalized_name(text: str) -> str:
    """照合に使う文字の形（NFKC・小文字・文字と数字だけ）。"""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in folded if unicodedata.category(char)[0] in {"L", "N"})


def _record_form_fields_with_page(record: Any) -> list[tuple[str, str, int | None]]:
    """record の metadata の画面の図の項目（名前, 種類）。種類が unknown の項目と名前の無い項目は除く。"""
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, dict):
        return []
    fields: list[tuple[str, str, int | None]] = []
    for image in _metadata_image_evidence(metadata):
        structure = image.get("visual_structure")
        items = structure.get("form_fields") if isinstance(structure, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            kind = form_field_kind(item.get("kind"))
            if name and kind != "unknown":
                page = image.get("page")
                if not isinstance(page, int) or isinstance(page, bool) or page <= 0:
                    start = getattr(record, "page", None)
                    end = getattr(record, "page_end", None) or start
                    page = start if isinstance(start, int) and not isinstance(start, bool) and start > 0 and start == end else None
                fields.append((name, kind, page))
    return fields


def record_form_fields(record: Any) -> list[tuple[str, str]]:
    """構造化解析の項目名と種類（所在は内部の観測へ引き継ぐ）。"""
    return [(name, kind) for name, kind, _page in _record_form_fields_with_page(record)]


def _source_id(record: Any) -> str:
    return str(getattr(record, "chunk_uid", "") or getattr(record, "id", "") or "")


def question_field_kinds(question: str, context: Any) -> list[FieldKindObservation]:
    """回答の context の根拠から、質問に名前が出た画面の図の項目と種類を、根拠の順に返す。

    child の metadata の項目は、生成に渡す根拠（parent）の source_id に帰属させる。
    """
    target = normalized_name(question)
    if not target:
        return []
    tree = tuple(getattr(context, "evidence_tree", ()) or ())
    groups: list[tuple[Any, list[Any]]] = (
        [(parent.record, [parent.record, *(child.record for child in parent.children)]) for parent in tree]
        if tree
        else [(record, [record]) for record in getattr(context, "records", ()) or ()]
    )
    seen: set[tuple[str, str, str, int | None]] = set()
    observations: list[FieldKindObservation] = []
    for owner, members in groups:
        source_id = _source_id(owner)
        for member in members:
            for name, kind, page in _record_form_fields_with_page(member):
                key = normalized_name(name)
                if len(key) < MIN_FIELD_NAME_CHARS or key not in target or (source_id, key, kind, page) in seen:
                    continue
                seen.add((source_id, key, kind, page))
                observations.append(FieldKindObservation(source_id, name, kind, page))
    return observations


def condition_kind_options(options: Iterable[tuple[str, Sequence[str]]]) -> dict[str, str]:
    """条件の選択肢のうち、項目の種類に対応する選択肢（種類 → 選択肢）。

    選択肢の語（選択肢そのものと言い換え）が言語資源の項目の種類の語（content_labels.form_field_kinds）と
    文字の形をそろえて一致すれば、その種類の選択肢とする。2 つの種類に当たる選択肢・1 つの種類に 2 つの選択肢が
    当たる種類は使わない。項目の種類の条件とみなすのは、2 つ以上の種類に選択肢が対応するときだけ（それ未満は空）。
    """
    words_by_kind = {
        kind: {normalized_name(word) for word in words} - {""} for kind, words in content_labels().form_field_kind_words.items()
    }
    by_kind: dict[str, list[str]] = {}
    for option, words in options:
        keys = {normalized_name(word) for word in (option, *words)} - {""}
        kinds = [kind for kind, kind_words in words_by_kind.items() if keys & kind_words]
        if len(kinds) == 1:
            by_kind.setdefault(kinds[0], []).append(option)
    mapping = {kind: values[0] for kind, values in by_kind.items() if len(values) == 1}
    return mapping if len(mapping) >= 2 else {}


def _condition_observations(observations: Sequence[FieldKindObservation], label: str, *, allow_single: bool) -> list[FieldKindObservation]:
    """条件の対象名を特定する。重なる名前は具体的な方、特定できない複数項目は未確定にする。"""
    names = {normalized_name(item.name) for item in observations} - {""}
    target = normalized_name(label)
    matched = {name for name in names if name in target}
    if not matched and not allow_single:
        return []
    selected = {name for name in matched if not any(name != other and name in other for other in matched)} if matched else names
    if len(selected) != 1:
        return []
    return [item for item in observations if normalized_name(item.name) in selected]


def resolve_conditions(observations: Sequence[FieldKindObservation], conditions: Iterable[EvidenceCondition]) -> list[ResolvedCondition]:
    """条件が指す項目の種類だけで選択肢を決める。同じ項目の競合・対象不明は決めない。"""
    conditions = list(conditions)
    resolved: list[ResolvedCondition] = []
    for condition_id, label, options in conditions:
        kinds = {item.kind for item in _condition_observations(observations, label, allow_single=len(conditions) == 1)}
        if len(kinds) != 1:
            continue
        (kind,) = kinds
        value = condition_kind_options(options).get(kind)
        if value:
            resolved.append(ResolvedCondition(condition_id, label, value, kind))
    return resolved


def resolved_plan(resolved: Sequence[ResolvedCondition], plans: Iterable[EvidencePlan]) -> EvidencePlan | None:
    """決まった条件の組（id と値）がちょうど一致する計画。条件が決まらない・一致する計画が無ければ None。"""
    if not resolved:
        return None
    wanted = sorted((item.condition_id, item.value) for item in resolved)
    return next((plan for plan in plans if sorted(plan[0]) == wanted), None)


def with_resolved_plan(runtime_knowledge: Any, resolved: Sequence[ResolvedCondition], plans: Iterable[EvidencePlan]) -> Any:
    """決まった条件の計画に切り替えた runtime knowledge（#2689）。計画が無ければそのまま返す。

    手順ごとの根拠・検索の手がかりは計画に残る手順だけにし、必須に数えない手順は計画の必須かで決め直し、
    業務ガイドのルール（rule の id が一致するもの）の本文を計画の本文にする。
    """
    plan = resolved_plan(resolved, plans) if runtime_knowledge is not None else None
    if plan is None:
        return runtime_knowledge
    _conditions, steps, rule_id, rule_content = plan
    kept = {step_id for step_id, _required in steps}
    rules = tuple(
        replace(rule, content=rule_content) if rule_content and rule.rule_id == rule_id else rule
        for rule in runtime_knowledge.matched_rules
    )
    from rag_engine.generation.guide_acquisition import acquisition_notices

    acquisitions = tuple(
        {**row, "step_ids": [step_id for step_id in row.get("step_ids", ()) if step_id in kept]}
        for row in runtime_knowledge.guide_acquisition
        if any(step_id in kept for step_id in row.get("step_ids", ()))
    )
    return replace(
        runtime_knowledge,
        guide_acquisition=acquisitions,
        guide_acquisition_notices=acquisition_notices(acquisitions),
        matched_rules=rules,
        guide_steps=tuple(item for item in runtime_knowledge.guide_steps if item[0] in kept),
        guide_optional_steps=tuple(step_id for step_id, required in steps if not required),
        guide_step_terms=tuple(item for item in runtime_knowledge.guide_step_terms if item[0] in kept),
        guide_step_contracts=tuple(item for item in runtime_knowledge.guide_step_contracts if item["step_id"] in kept),
        guide_unsearched_steps=tuple(step_id for step_id in runtime_knowledge.guide_unsearched_steps if step_id in kept),
    )


def with_field_kind_evidence(observations: Sequence[FieldKindObservation], spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """取得済みの画像の構造化項目を、同じ出典・頁の独立した根拠にする。"""
    result = [dict(span) for span in spans]
    names = content_labels()
    seen: set[tuple[str, str, str, int | None]] = set()
    for item in observations:
        key = (item.source_id, item.name, item.kind, item.page)
        if key in seen:
            continue
        candidates = [span for span in spans if item.source_id in (span.get("source_id"), *(span.get("source_aliases") or ()))]
        if item.page is not None:
            candidates = [span for span in candidates if span.get("page") == item.page]
        # 図の頁が分からない複数頁の parent から所在を補わない。
        pages = {span.get("page") for span in candidates}
        word = names.form_field_kind(item.kind)
        if not candidates or len(pages) != 1 or not next(iter(pages)) or not word:
            continue
        source = next((span for span in candidates if normalized_name(item.name) in normalized_name(span.get("text", ""))), candidates[0])
        text = f"{item.name} = {word}"
        identity = hashlib.sha256("\0".join(map(str, key)).encode()).hexdigest()[:20]
        evidence = {
            key: source[key]
            for key in (
                "source_id",
                "source",
                "document_scope",
                "business_scope",
                "page",
                "page_end",
                "location",
                "section_path",
                "section_path_sources",
                "source_aliases",
                "value_context",
            )
            if key in source
        }
        evidence.update(
            evidence_id=f"Efield{identity}",
            text=text,
            origin="image_extraction",
            field_kind={"source_id": item.source_id, "name": item.name, "kind": item.kind, "page": item.page},
            excerpt_only=False,
        )
        result.append(evidence)
        seen.add(key)
        if len(seen) >= MAX_NOTE_OBSERVATIONS:
            break
    return result


def prompt_note(
    observations: Sequence[FieldKindObservation],
    resolved: Sequence[ResolvedCondition],
    spans: Sequence[Mapping[str, Any]],
) -> str:
    """生成の材料に入れる根拠の注記。生成に渡す根拠（spans）に無い出典の観測は出さない。"""
    labels: dict[str, str] = {}
    for span in spans:
        span_label = str(span.get("label") or "")
        for source_id in (span.get("source_id"), *(span.get("source_aliases") or ())):
            if source_id and span_label:
                labels.setdefault(str(source_id), span_label)
    field_labels = {
        (
            str(field.get("source_id") or span.get("source_id") or ""),
            str(field.get("name") or ""),
            str(field.get("kind") or ""),
            field.get("page"),
        ): str(span.get("label") or "")
        for span in spans
        if isinstance(field := span.get("field_kind"), Mapping)
    }
    names = content_labels()
    lines = []
    for item in observations:
        label = field_labels.get((item.source_id, item.name, item.kind, item.page)) or labels.get(item.source_id)
        word = names.form_field_kind(item.kind)
        if label and word:
            lines.append(f"- {label}: {item.name} = {word}")
    lines = list(dict.fromkeys(lines))[:MAX_NOTE_OBSERVATIONS]
    if not lines:
        return ""
    parts = ["根拠の画面の図の項目の種類（質問に名前が出た項目）:\n" + "\n".join(lines)]
    if resolved:
        parts.append(
            "業務ガイドの条件のうち、根拠の画面の図の項目の種類で分かった値（この値に当たる分岐の手順で答える）: "
            + "、".join(f"{item.label}={item.value}" for item in resolved)
        )
    return "\n".join(parts)


def trace_payload(
    observations: Sequence[FieldKindObservation], resolved: Sequence[ResolvedCondition], plans: Iterable[EvidencePlan] = ()
) -> dict[str, Any]:
    """回答の記録に残す観測と、決まった条件と、切り替えた業務ガイドの手順の計画（手順の id と必須か）。"""
    plan = resolved_plan(resolved, plans)
    return {
        "observations": [
            {"source_id": item.source_id, "name": item.name, "kind": item.kind, **({"page": item.page} if item.page is not None else {})}
            for item in observations
        ],
        "resolved_conditions": [
            {"id": item.condition_id, "label": item.label, "value": item.value, "kind": item.kind} for item in resolved
        ],
        **({"guide_plan": [{"step_id": step_id, "required": required} for step_id, required in plan[1]]} if plan else {}),
    }
