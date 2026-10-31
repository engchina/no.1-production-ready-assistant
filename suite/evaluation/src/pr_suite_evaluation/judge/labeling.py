"""人手の判定を集める: 記入用のブックの書き出し（label-template）と取り込み（label-import）。

#3418。

1. `build_labeling_items`: 保存した評価の結果（`run`・`score`・`judge` の結果のファイル）の回答を、
   判定と同じ入力（`judging.case_input`）で項目にする。区分（dev / holdout）は、ケースごとに
   評価セットの区分の種類（`category`）で層に分け、層の中で sha256 の順に決める（決定的。同じ
   ケースの A・C・D の回答と、その合成の項目は同じ区分にする）。故意に壊した回答
   （`perturbation`）を足せる。
2. `write_template`: 記入用のブック（1 行 = 項目 × 観点）と項目のファイル（機械の正本）を書く。
3. `inter_annotator`: 2 人以上の記入者のブックの一致（観点ごとの Cohen の κ）と食い違いの一覧。
4. `write_adjudication`: 食い違いだけを空けた裁定用のブック（一致した行は判定を埋める）。
5. `build_label_set`: 裁定（か全員の一致）から `LabelSet`（`calibrate` が読む JSON）を作る。

人の判定はコードで作らない。合成の項目の fail は壊し方の仮定で、`origin=synthetic` として人手の判定
と分ける（校正の合否に入れない）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# 区分の決め方・誤りの型と目標・記入者の間の一致は platform の共有の部品（RAG の判定の校正の画面と
# 同じ。#3578）。CLI・テストが使う名前はここからも読めるようにする。
from pr_evaluation_scoring.labeling import (
    ANNOTATOR_ID,
    CANNOT_JUDGE,
    FAIL,
    NO_CATEGORY,
    PASS,
    item_order_key,
    pairwise_agreement,
)
from pr_evaluation_scoring.labeling import ASPECT_HOLDOUT_TARGET as ASPECT_HOLDOUT_TARGET
from pr_evaluation_scoring.labeling import ASPECT_LABEL_TARGET as ASPECT_LABEL_TARGET
from pr_evaluation_scoring.labeling import DEFAULT_HOLDOUT_RATIO as DEFAULT_HOLDOUT_RATIO
from pr_evaluation_scoring.labeling import DEFAULT_PERTURB_MAX as DEFAULT_PERTURB_MAX
from pr_evaluation_scoring.labeling import DEFAULT_SPLIT_SALT as DEFAULT_SPLIT_SALT
from pr_evaluation_scoring.labeling import ERROR_TYPE_IDS as ERROR_TYPE_IDS
from pr_evaluation_scoring.labeling import ERROR_TYPES as ERROR_TYPES
from pr_evaluation_scoring.labeling import ErrorType as ErrorType
from pr_evaluation_scoring.labeling import agreed_label as shared_agreed_label
from pr_evaluation_scoring.labeling import assign_splits as assign_splits
from pr_evaluation_scoring.perturbation import PerturbationSource, perturb_items
from pr_evaluation_scoring.rubric import applicable_aspects, aspect_criteria
from pydantic import BaseModel, ConfigDict, Field

from pr_suite_evaluation.dataset import LoadedDataset, canonical_sha256
from pr_suite_evaluation.experiment import ExperimentRecord, ScorerConfig
from pr_suite_evaluation.judge.calibration import (
    LabeledEvidence,
    LabeledItem,
    LabelSet,
    PerturbationInfo,
)
from pr_suite_evaluation.judge.judging import case_input
from pr_suite_evaluation.judge.rubric import Rubric
from pr_suite_evaluation.judge.workbook import (
    LABEL_CHOICES,
    TARGET_SHEET,
    WORKBOOK_SCHEMA_VERSION,
    WorkbookContent,
    WorkbookError,
    read_workbook,
    write_workbook,
)
from pr_suite_evaluation.targets import agent_mcp

ITEMS_SCHEMA_VERSION = 1
# ブックの根拠の抜粋の合計の上限（セルの上限の内）。
MAX_EVIDENCE_EXCERPT_CHARS = 30_000


class LabelingItem(BaseModel):
    """記入の 1 項目（人が判定する項目か、合成の項目）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=200)
    split: Literal["dev", "holdout"]
    origin: Literal["human", "synthetic"] = "human"
    source: str
    case_id: str
    category: str | None = None
    question: str
    answer: str
    standard_answer: str | None = None
    evidence: list[LabeledEvidence] = Field(default_factory=list)
    # 判定する観点（人の項目）か、fail にした観点（合成の項目）。
    aspects: list[str] = Field(..., min_length=1)
    perturbation: PerturbationInfo | None = None
    synthetic_labels: dict[str, Literal["fail"]] = Field(default_factory=dict)


class LabelingItems(BaseModel):
    """項目のファイル（`*.items.json`。ブックと対になる機械の正本）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = ITEMS_SCHEMA_VERSION
    created_at: str
    rubric: dict[str, Any]
    split_policy: dict[str, Any]
    sources: list[dict[str, Any]] = Field(default_factory=list)
    skipped: dict[str, int] = Field(default_factory=dict)
    items: list[LabelingItem] = Field(..., min_length=1)

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            {
                "rubric": self.rubric,
                "items": [item.model_dump(mode="json") for item in self.items],
            }
        )

    @property
    def human_items(self) -> list[LabelingItem]:
        return [item for item in self.items if item.origin == "human"]

    @property
    def synthetic_items(self) -> list[LabelingItem]:
        return [item for item in self.items if item.origin == "synthetic"]

    def check_rubric(self, rubric: Rubric) -> None:
        if self.rubric.get("sha256") != rubric.sha256:
            raise ValueError(
                "項目のファイルの rubric（sha256）と、渡した rubric が違います（同じ rubric で"
                "作り直してください）。"
            )


def load_items(path: Path) -> LabelingItems:
    return LabelingItems.model_validate_json(path.read_text(encoding="utf-8"))


def _applicable(rubric: Rubric, standard_answer: str | None) -> list[str]:
    return applicable_aspects(
        rubric.model_dump(mode="json"), has_standard_answer=bool(standard_answer)
    )


def build_labeling_items(
    records: Sequence[tuple[str, ExperimentRecord]],
    dataset: LoadedDataset,
    rubric: Rubric,
    *,
    holdout_ratio: float = DEFAULT_HOLDOUT_RATIO,
    salt: str = DEFAULT_SPLIT_SALT,
    perturb: Sequence[str] = (),
    perturb_max: int = DEFAULT_PERTURB_MAX,
) -> LabelingItems:
    """保存した評価の結果の回答を、記入の項目にする（判定と同じ入力。モデル・製品は呼ばない）。"""
    by_id = {case.id: case for case in dataset.dataset.cases}
    drafts: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    names = [name for name, _ in records]
    if len(set(names)) != len(names):
        raise ValueError(f"結果の名前が重複しています: {', '.join(names)}")
    for name, record in records:
        target = record.provenance.get("target") or {}
        kind = (
            str(target.get("kind") or agent_mcp.TARGET_KIND) if isinstance(target, Mapping) else ""
        )
        scorer = ScorerConfig(target_kind=kind or agent_mcp.TARGET_KIND)
        dataset_provenance = record.provenance.get("dataset") or {}
        sources.append(
            {
                "name": name,
                "target_kind": scorer.target_kind,
                "dataset_sha256": dataset_provenance.get("sha256")
                if isinstance(dataset_provenance, Mapping)
                else None,
                "observations": len(record.observations),
            }
        )
        for observation in record.observations:
            case = by_id.get(observation.case_id)
            if case is None:
                raise ValueError(f"評価セットに無いケースの観測です: {observation.case_id}")
            view = scorer.view(observation)
            if view.failure is not None or not view.turns:
                skipped["case_failed"] = skipped.get("case_failed", 0) + 1
                continue
            judge_input = case_input(case, view)
            if not judge_input.answer.strip():
                skipped["no_answer"] = skipped.get("no_answer", 0) + 1
                continue
            aspects = _applicable(rubric, judge_input.standard_answer)
            if not aspects:
                skipped["no_aspect"] = skipped.get("no_aspect", 0) + 1
                continue
            drafts.append(
                {
                    "id": f"{name}:{case.id}",
                    "source": name,
                    "case_id": case.id,
                    "category": case.category,
                    "question": judge_input.question,
                    "answer": judge_input.answer,
                    "standard_answer": judge_input.standard_answer,
                    "evidence": [
                        LabeledEvidence(id=evidence_id, text=text)
                        for evidence_id, text in judge_input.evidence
                    ],
                    "aspects": aspects,
                }
            )
    if not drafts:
        raise ValueError("記入できる回答がありません（失敗したケース・空の回答だけです）。")
    strata = {draft["case_id"]: draft["category"] or NO_CATEGORY for draft in drafts}
    splits = assign_splits(strata, holdout_ratio=holdout_ratio, salt=salt)
    items = [LabelingItem(split=splits[draft["case_id"]], **draft) for draft in drafts]
    items.extend(_perturbed(items, perturb, perturb_max=perturb_max, salt=salt))
    return LabelingItems(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        rubric=rubric.provenance(),
        split_policy={
            "unit": "case_id",
            "strata": "category",
            "holdout_ratio": holdout_ratio,
            "salt": salt,
            "perturb": list(perturb),
            "perturb_max": perturb_max,
        },
        sources=sources,
        skipped=skipped,
        items=items,
    )


def _perturbed(
    items: Sequence[LabelingItem], kinds: Sequence[str], *, perturb_max: int, salt: str
) -> list[LabelingItem]:
    by_id = {item.id: item for item in items}
    sources = [
        PerturbationSource(
            id=item.id,
            answer=item.answer,
            standard_answer=item.standard_answer,
            aspects=tuple(item.aspects),
        )
        for item in items
    ]
    return [
        by_id[result.base_item_id].model_copy(
            update={
                "id": result.id,
                "origin": "synthetic",
                "answer": result.answer,
                "aspects": list(result.labels),
                "perturbation": PerturbationInfo(
                    kind=result.kind,
                    base_item_id=result.base_item_id,
                    description=result.description,
                ),
                "synthetic_labels": result.labels,
            }
        )
        for result in perturb_items(sources, kinds, perturb_max=perturb_max, salt=salt)
    ]


# ---- ブックの書き出し ---------------------------------------------------------------------


def aspect_text(rubric: Rubric, aspect_id: str) -> str:
    """rubric の指示の中の、観点の基準の行（`- <観点>: …`）。無ければ指示の全体。"""
    return aspect_criteria(rubric.instructions, aspect_id)


def evidence_excerpt(evidence: Sequence[LabeledEvidence]) -> str:
    """ブックに書く根拠の抜粋（上限を超えた根拠は件数だけ書く）。"""
    parts: list[str] = []
    used = 0
    for index, item in enumerate(evidence):
        block = f"[{item.id}]\n{item.text}"
        if used + len(block) > MAX_EVIDENCE_EXCERPT_CHARS:
            parts.append(f"…（以下 {len(evidence) - index} 件の根拠を省略。全文は項目のファイル）")
            break
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts) if parts else "（根拠なし）"


GUIDANCE = (
    "1 行は「項目 × 観点」。観点ごとに独立に、判定の列で pass / fail / cannot_judge を選ぶ。",
    "判断は、この行の質問・回答の本文・根拠の抜粋・標準回答だけにもとづく。一般の知識で補わない。",
    "LLM の判定・ほかの記入者の判定を見ずに付ける（記入者どうしで相談しない。食い違いは裁定で"
    "決める）。",
    "入力が足りず決められないときだけ cannot_judge にする（校正に使わない）。迷ったら pass に"
    "しない。",
    f"観点ごとに holdout の行を {ASPECT_HOLDOUT_TARGET} 以上（pass・fail をそれぞれ "
    f"{ASPECT_LABEL_TARGET} 以上）集める。件数は「{TARGET_SHEET}」の sheet で確かめる。",
    "誤りの型・対照の列は、当てはまるものがあれば選ぶ（任意。目標の件数を確かめるために使う）。",
    "メモには判断の理由を短く書く（裁定と、ラベルの見直しに使う）。",
    "記入者の列には自分の匿名の ID（英数字）を書く。実名・メールは書かない。",
    "区分（dev / holdout）・項目の ID・観点の列は変えない。行を足したり消したりしない。",
    "このブックの内容（質問・回答・根拠）を Issue・チャット・repo に書き写さない。",
)


def _rubric_rows(rubric: Rubric) -> list[list[str]]:
    rows: list[list[str]] = [
        ["rubric", f"{rubric.id}（版 {rubric.version}）"],
        ["rubric の sha256", rubric.sha256],
        [],
        ["付け方", ""],
        *[["", line] for line in GUIDANCE],
        [],
        [
            "ラベル",
            "pass: 観点の基準を満たす / fail: 満たさない / cannot_judge: 入力から決められない",
        ],
        [],
        ["観点", "基準"],
    ]
    for aspect in rubric.aspects:
        needs = "（標準回答のある項目だけ）" if "standard_answer" in aspect.requires else ""
        rows.append([aspect.id, f"{aspect_text(rubric, aspect.id)}{needs}"])
    rows.extend([[], ["判定の指示の全文", rubric.instructions]])
    return rows


def _target_rows() -> list[list[object]]:
    return [
        [item.group, item.id, item.title, item.aspects, item.holdout_target] for item in ERROR_TYPES
    ]


def _item_order(items: Sequence[LabelingItem], salt: str) -> list[LabelingItem]:
    # 同じケースの A・C・D の回答が続かないよう、sha256 の順に並べる（決定的）。
    return sorted(items, key=lambda item: item_order_key(salt, item.id))


def _label_rows(
    items: LabelingItems, rubric: Rubric, prefill: Mapping[tuple[str, str], Mapping[str, str]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in _item_order(items.human_items, str(items.split_policy.get("salt", ""))):
        excerpt = evidence_excerpt(item.evidence)
        for aspect_id in item.aspects:
            rows.append(
                {
                    "item_id": item.id,
                    "aspect": aspect_id,
                    "split": item.split,
                    "question": item.question,
                    "answer": item.answer,
                    "evidence": excerpt,
                    "standard_answer": item.standard_answer or "（なし）",
                    "rubric": aspect_text(rubric, aspect_id),
                    **prefill.get((item.id, aspect_id), {}),
                }
            )
    return rows


def _synthetic_rows(items: LabelingItems) -> list[dict[str, str]]:
    return [
        {
            "item_id": item.id,
            "base_item_id": item.perturbation.base_item_id if item.perturbation else "",
            "kind": item.perturbation.kind if item.perturbation else "",
            "fail_aspects": "・".join(item.synthetic_labels),
            "description": item.perturbation.description if item.perturbation else "",
            "answer": item.answer,
        }
        for item in items.synthetic_items
    ]


def _meta(items: LabelingItems, kind: str, annotators: Sequence[str] = ()) -> dict[str, str]:
    return {
        "schema_version": str(WORKBOOK_SCHEMA_VERSION),
        "kind": kind,
        "items_sha256": items.sha256,
        "rubric_sha256": str(items.rubric.get("sha256", "")),
        "annotators": ",".join(annotators),
    }


def write_template(path: Path, items: LabelingItems, rubric: Rubric) -> None:
    """記入用のブックを書く（全員に同じブックを配り、各自が別のファイルに記入する）。"""
    items.check_rubric(rubric)
    write_workbook(
        path,
        meta=_meta(items, "annotation"),
        label_rows=_label_rows(items, rubric, {}),
        target_rows=_target_rows(),
        aspect_ids=rubric.aspect_ids,
        rubric_rows=_rubric_rows(rubric),
        synthetic_rows=_synthetic_rows(items),
        error_type_ids=ERROR_TYPE_IDS,
    )


# ---- 記入したブックの読み込み ---------------------------------------------------------------


@dataclass
class Annotation:
    """1 人の記入（か裁定）。キーは（項目の ID, 観点）。"""

    annotator: str
    source: str
    labels: dict[tuple[str, str], str] = field(default_factory=dict)
    notes: dict[tuple[str, str], str] = field(default_factory=dict)
    error_types: dict[tuple[str, str], str] = field(default_factory=dict)
    excluded_synthetic: set[str] = field(default_factory=set)


def _annotator_id(content: WorkbookContent, given: str | None, path: Path) -> str:
    written = sorted({row.get("annotator", "") for row in content.rows} - {""})
    if given:
        if written and written != [given]:
            raise WorkbookError(
                f"{path.name} の記入者の列（{', '.join(written)}）と指定（{given}）が違います。"
            )
        annotator = given
    elif len(written) == 1:
        annotator = written[0]
    else:
        raise WorkbookError(
            f"{path.name} の記入者を決められません（記入者の列に 1 つの ID を書くか、"
            "<ID>=<ブック> で渡してください）。"
        )
    if not ANNOTATOR_ID.match(annotator):
        raise WorkbookError(
            f"記入者の ID は英数字と _ . - の 40 文字までにしてください: {path.name}"
        )
    return annotator


def read_annotation(
    path: Path,
    items: LabelingItems,
    *,
    annotator: str | None = None,
    kind: str = "annotation",
) -> Annotation:
    """記入したブックを読み、項目のファイルと照らす（行の過不足・ラベルの語彙）。"""
    content = read_workbook(path)
    if content.meta.get("items_sha256") != items.sha256:
        raise WorkbookError(f"{path.name} は、渡した項目のファイルのブックではありません。")
    if content.meta.get("kind") != kind:
        raise WorkbookError(
            f"{path.name} は {content.meta.get('kind')} のブックです"
            f"（{kind} のブックを渡してください）。"
        )
    expected = {(item.id, aspect) for item in items.human_items for aspect in item.aspects}
    result = Annotation(
        annotator=_annotator_id(content, annotator, path),
        source=path.name,
        excluded_synthetic=content.excluded_synthetic & {item.id for item in items.synthetic_items},
    )
    seen: set[tuple[str, str]] = set()
    problems: list[str] = []
    for number, row in enumerate(content.rows, start=2):
        key = (row.get("item_id", ""), row.get("aspect", ""))
        if key not in expected:
            problems.append(f"{number} 行目: 知らない項目・観点です（{key[0]} / {key[1]}）")
            continue
        if key in seen:
            problems.append(f"{number} 行目: 項目・観点が重複しています（{key[0]} / {key[1]}）")
            continue
        seen.add(key)
        label = row.get("label", "").lower()
        if label:
            if label not in LABEL_CHOICES:
                problems.append(f"{number} 行目: 判定は {' / '.join(LABEL_CHOICES)} のどれかです")
            else:
                result.labels[key] = label
        error_type = row.get("error_type", "")
        if error_type:
            if error_type not in ERROR_TYPE_IDS:
                problems.append(f"{number} 行目: 知らない誤りの型・対照です（{error_type}）")
            else:
                result.error_types[key] = error_type
        if row.get("note"):
            result.notes[key] = row["note"]
    missing = expected - seen
    if missing:
        problems.append(f"行が足りません（{len(missing)} 行。例: {sorted(missing)[0]}）")
    if problems:
        raise WorkbookError(f"{path.name}: " + "; ".join(problems[:10]))
    return result


# ---- 記入者の間の一致と食い違い -------------------------------------------------------------


class PairAgreement(BaseModel):
    annotators: list[str]
    # 両方が pass / fail を付けた行の数（cannot_judge・未記入は数えない）。
    compared: int
    agreement: float | None
    kappa: float | None


class AspectInterAnnotator(BaseModel):
    rows: int
    agreed: int
    disagreements: int
    pairs: list[PairAgreement] = Field(default_factory=list)
    cannot_judge: dict[str, int] = Field(default_factory=dict)
    blank: dict[str, int] = Field(default_factory=dict)


class Disagreement(BaseModel):
    item_id: str
    aspect: str
    split: str
    labels: dict[str, str | None]


class InterAnnotatorReport(BaseModel):
    """記入者の間の一致（観点ごと・記入者の組ごとの Cohen の κ）と、裁定が要る行。"""

    created_at: str
    items_sha256: str
    annotators: list[str]
    aspects: dict[str, AspectInterAnnotator] = Field(default_factory=dict)
    disagreements: list[Disagreement] = Field(default_factory=list)


def agreed_label(annotations: Sequence[Annotation], key: tuple[str, str]) -> str | None:
    """全員が同じラベルを付けた行のラベル（未記入・食い違いは None）。"""
    return shared_agreed_label([item.labels.get(key) for item in annotations])


def _check_annotators(annotations: Sequence[Annotation]) -> None:
    if len(annotations) < 2:
        raise ValueError("記入者のブックを 2 つ以上渡してください（記入者の間の一致を測る）。")
    names = [item.annotator for item in annotations]
    if len(set(names)) != len(names):
        raise ValueError(f"記入者の ID が重複しています: {', '.join(names)}")


def inter_annotator(
    items: LabelingItems, rubric: Rubric, annotations: Sequence[Annotation]
) -> InterAnnotatorReport:
    _check_annotators(annotations)
    split_of = {item.id: item.split for item in items.human_items}
    report = InterAnnotatorReport(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        items_sha256=items.sha256,
        annotators=[item.annotator for item in annotations],
    )
    for aspect_id in rubric.aspect_ids:
        keys = [(item.id, aspect_id) for item in items.human_items if aspect_id in item.aspects]
        if not keys:
            continue
        pairs = [
            PairAgreement(
                annotators=list(result.annotators),
                compared=result.compared,
                agreement=result.agreement,
                kappa=result.kappa,
            )
            for result in pairwise_agreement(
                keys, {item.annotator: item.labels for item in annotations}
            )
        ]
        disagreed = [key for key in keys if agreed_label(annotations, key) is None]
        report.aspects[aspect_id] = AspectInterAnnotator(
            rows=len(keys),
            agreed=len(keys) - len(disagreed),
            disagreements=len(disagreed),
            pairs=pairs,
            cannot_judge={
                item.annotator: sum(1 for key in keys if item.labels.get(key) == CANNOT_JUDGE)
                for item in annotations
            },
            blank={
                item.annotator: sum(1 for key in keys if key not in item.labels)
                for item in annotations
            },
        )
        report.disagreements.extend(
            Disagreement(
                item_id=key[0],
                aspect=key[1],
                split=split_of[key[0]],
                labels={item.annotator: item.labels.get(key) for item in annotations},
            )
            for key in disagreed
        )
    return report


def write_adjudication(
    path: Path, items: LabelingItems, rubric: Rubric, annotations: Sequence[Annotation]
) -> int:
    """裁定用のブックを書く（一致した行は判定を埋め、食い違った行だけ空ける）。空けた行の数を返す。"""
    _check_annotators(annotations)
    items.check_rubric(rubric)
    prefill: dict[tuple[str, str], dict[str, str]] = {}
    extra: list[str] = ["状態"]
    for annotation in annotations:
        extra.extend([f"判定: {annotation.annotator}", f"メモ: {annotation.annotator}"])
    open_rows = 0
    for item in items.human_items:
        for aspect_id in item.aspects:
            key = (item.id, aspect_id)
            agreed = agreed_label(annotations, key)
            types = {a.error_types[key] for a in annotations if key in a.error_types}
            row = {
                "extra:状態": "一致" if agreed else "要裁定",
                "label": agreed or "",
                "error_type": types.pop() if len(types) == 1 else "",
            }
            for annotation in annotations:
                row[f"extra:判定: {annotation.annotator}"] = annotation.labels.get(key, "")
                row[f"extra:メモ: {annotation.annotator}"] = annotation.notes.get(key, "")
            open_rows += agreed is None
            prefill[key] = row
    excluded = set().union(*(a.excluded_synthetic for a in annotations))
    synthetic_rows = [
        {**row, "exclude": "x" if row["item_id"] in excluded else ""}
        for row in _synthetic_rows(items)
    ]
    write_workbook(
        path,
        meta=_meta(items, "adjudication", [a.annotator for a in annotations]),
        label_rows=_label_rows(items, rubric, prefill),
        extra_columns=extra,
        target_rows=_target_rows(),
        aspect_ids=rubric.aspect_ids,
        rubric_rows=_rubric_rows(rubric),
        synthetic_rows=synthetic_rows,
        error_type_ids=ERROR_TYPE_IDS,
    )
    return open_rows


# ---- LabelSet にする ----------------------------------------------------------------------


@dataclass
class ImportResult:
    labels: LabelSet
    dropped_items: list[str]


def build_label_set(
    items: LabelingItems,
    rubric: Rubric,
    annotations: Sequence[Annotation],
    *,
    adjudicated: Annotation | None = None,
    report: InterAnnotatorReport | None = None,
) -> ImportResult:
    """裁定（無ければ全員の一致）から LabelSet を作る。決まらない行が残れば ValueError。"""
    _check_annotators(annotations)
    items.check_rubric(rubric)
    unresolved: list[tuple[str, str]] = []
    labeled: list[LabeledItem] = []
    dropped: list[str] = []
    for item in items.human_items:
        finals: dict[str, str] = {}
        for aspect_id in item.aspects:
            key = (item.id, aspect_id)
            final = adjudicated.labels.get(key) if adjudicated else agreed_label(annotations, key)
            if final is None:
                unresolved.append(key)
            else:
                finals[aspect_id] = final
        labels = {aspect: label for aspect, label in finals.items() if label in (PASS, FAIL)}
        if not labels:
            dropped.append(item.id)
            continue
        keys = [(item.id, aspect_id) for aspect_id in item.aspects]
        sources = [*annotations, *([adjudicated] if adjudicated else [])]
        notes = [
            f"{source.annotator}: {source.notes[key]}"
            for source in sources
            for key in keys
            if key in source.notes
        ]
        labeled.append(
            LabeledItem(
                id=item.id,
                split=item.split,
                question=item.question,
                answer=item.answer,
                standard_answer=item.standard_answer,
                evidence=item.evidence,
                labels=labels,
                labeler=adjudicated.annotator
                if adjudicated
                else "+".join(a.annotator for a in annotations),
                note="\n".join(notes) or None,
                error_types=sorted(
                    {
                        source.error_types[key]
                        for source in sources
                        for key in keys
                        if key in source.error_types
                    }
                ),
                annotator_labels={
                    annotation.annotator: {
                        key[1]: annotation.labels[key] for key in keys if key in annotation.labels
                    }
                    for annotation in annotations
                },
            )
        )
    if unresolved:
        hint = (
            "裁定のブックの判定を埋めてください"
            if adjudicated
            else "--adjudication-output で裁定用のブックを作り、裁定して --adjudicated で"
            "渡してください"
        )
        raise ValueError(
            f"判定の決まらない行が {len(unresolved)} 行あります（例: {unresolved[0][0]} / "
            f"{unresolved[0][1]}）。{hint}。"
        )
    excluded = set().union(
        *(a.excluded_synthetic for a in annotations),
        adjudicated.excluded_synthetic if adjudicated else set(),
    )
    for item in items.synthetic_items:
        if item.id in excluded:
            continue
        labeled.append(
            LabeledItem(
                id=item.id,
                split=item.split,
                question=item.question,
                answer=item.answer,
                standard_answer=item.standard_answer,
                evidence=item.evidence,
                labels=dict(item.synthetic_labels),
                labeler="synthetic",
                origin="synthetic",
                perturbation=item.perturbation,
            )
        )
    if not labeled:
        raise ValueError("pass / fail の判定のある項目がありません。")
    provenance: dict[str, Any] = {
        "items_sha256": items.sha256,
        "rubric": items.rubric,
        "split_policy": items.split_policy,
        "sources": items.sources,
        "annotators": [a.annotator for a in annotations],
        "adjudicator": adjudicated.annotator if adjudicated else None,
        "dropped_cannot_judge_items": len(dropped),
        "synthetic": {
            "items": len(items.synthetic_items),
            "excluded": sorted(excluded),
        },
    }
    if report is not None:
        provenance["inter_annotator"] = {
            aspect_id: {
                "rows": item.rows,
                "disagreements": item.disagreements,
                "kappa": [pair.kappa for pair in item.pairs],
            }
            for aspect_id, item in report.aspects.items()
        }
    return ImportResult(
        labels=LabelSet(
            rubric_id=rubric.id,
            description="記入用のブックから label-import で作った人手の判定（#3418）。",
            provenance=provenance,
            items=labeled,
        ),
        dropped_items=dropped,
    )


def write_json(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")


def items_path_for(workbook: Path) -> Path:
    """ブックと対になる項目のファイルの既定の名前（`<ブック>.items.json`）。"""
    return workbook.with_suffix(".items.json")
