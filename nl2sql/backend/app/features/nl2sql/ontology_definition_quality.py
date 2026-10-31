"""型付き候補の根拠・不足情報検査。SQL や外部操作は実行しない。"""

from __future__ import annotations

import re
from typing import Literal

from .object_identity import object_match_key, split_match_key
from .ontology_definitions import (
    BusinessDefinition,
    DefinitionEvidence,
    DefinitionFinding,
    DefinitionSource,
    PropertyDefinition,
)
from .ontology_terms import plain_label

# 物理の表・列が業務プロファイルの範囲にあれば、DB の schema を根拠にした evidence は照合済みにする
# （LLM が書く資料の検証値・原文の引用は、schema の JSON とは一致しない。#1720）。
_DATABASE_SOURCE_IDS = frozenset({"schema", "database", "db"})
_LOCATOR_SEPARATOR = re.compile(r"[,、\s=()]+")


def physical_references(locator: str) -> list[tuple[str, str]]:
    """evidence の位置（`OWNER.OBJECT[.COLUMN]` の並び）を (object の照合キー, 列) にする。"""

    refs: list[tuple[str, str]] = []
    for token in _LOCATOR_SEPARATOR.split(locator.strip()):
        # `OWNER.OBJECT.*` は表全体（列なし）を指す。
        token = token.removesuffix(".*")
        if "." not in token or token.upper() in {"AND", "OR"}:
            continue  # 結合条件の演算子・語（`A.X = B.Y AND …`）は物理名ではない
        parts = split_match_key(object_match_key(token))
        if len(parts) == 2:
            refs.append((".".join(parts), ""))
        elif len(parts) == 3:
            refs.append((".".join(parts[:2]), parts[2]))
        else:
            return []
    return refs


def _database_evidence_verified(
    evidence: DefinitionEvidence, physical: dict[str, set[str]] | None
) -> bool:
    if physical is None or (
        evidence.source_kind != "database" and evidence.source_id not in _DATABASE_SOURCE_IDS
    ):
        return False
    refs = physical_references(evidence.locator)
    return bool(refs) and all(
        key in physical and (not column or column in physical[key]) for key, column in refs
    )


def inspect_definition_quality(
    definitions: list[BusinessDefinition],
    sources: list[DefinitionSource],
    physical: dict[str, set[str]] | None = None,
) -> list[DefinitionFinding]:
    """graph を作れるが補うと精度が上がる項目を info、SQL の意味が欠ける項目を warning にする。

    graph（物理のマッピング・参照・式）の破損は `validate_definitions` が error にする。ここでは
    公開を止めない（#1720。情報が足りない項目は best effort で空のまま公開できる）。
    """

    findings: list[DefinitionFinding] = []
    database_sha = next((s.sha256 for s in sources if s.kind == "database"), "")
    for item in definitions:

        def finding(
            code: str,
            field: str,
            message: str,
            *,
            severity: Literal["info", "warning", "error"] = "info",
            definition_id: str = item.id or item.api_name,
        ) -> None:
            findings.append(
                DefinitionFinding(
                    code=code,
                    definition_id=definition_id,
                    field=field,
                    severity=severity,
                    message_ja=message,
                )
            )

        for evidence in item.evidence:
            evidence.verified = False
            if _database_evidence_verified(evidence, physical):
                evidence.verified = True
                evidence.source_kind = "database"
                evidence.source_sha256 = database_sha
                continue
            matches = [
                source
                for source in sources
                if source.source_id == evidence.source_id and source.locator == evidence.locator
            ]
            if (
                len(matches) == 1
                and evidence.excerpt_ja.strip()
                and evidence.excerpt_ja in matches[0].text
            ):
                source = matches[0]
                if not evidence.source_sha256 or evidence.source_sha256 == source.sha256:
                    evidence.verified = True
                    evidence.source_sha256 = source.sha256
                    evidence.source_kind = source.kind
        # 照合できない根拠は定義ごとに 1 件だけにする（evidence ごとに重ねない）。
        if not any(e.verified for e in item.evidence):
            finding(
                "INFERENCE_WITHOUT_EVIDENCE",
                "evidence",
                "資料やスキーマで確かめられる根拠がありません。"
                "業務の資料・Q/A を構築に加えると、根拠を確かめられます"
                "（何もしなくても公開できます）。",
            )
        for missing in item.missing_information_ja:
            finding("INFORMATION_REQUIRED", "missing_information_ja", missing)
        if item.kind == "object_type":
            # view など制約の無いオブジェクトは識別子を決められないことがある。graph は作れるため
            # 公開を止めない（#1720）。
            if not item.primary_key:
                finding(
                    "OBJECT_IDENTITY_REQUIRED",
                    "primary_key",
                    "主キー（1 行を特定する列）が分かりません。"
                    "分かれば Markdown のオブジェクト型に「主キー」を書くと、"
                    "件数・重複の判定が正確になります（何もしなくても公開できます）。",
                )
            if not item.grain_ja:
                finding(
                    "OBJECT_GRAIN_REQUIRED",
                    "grain_ja",
                    "粒度（1 行が何を表すか）が書かれていません。"
                    "分かれば Markdown のオブジェクト型に「粒度」を書くと、集計が正確になります"
                    "（何もしなくても公開できます）。",
                )
        elif isinstance(item, PropertyDefinition):
            if not item.data_type:
                finding(
                    "PROPERTY_TYPE_REQUIRED",
                    "data_type",
                    "業務の型（文字列・数値・日付など）が分かりません。"
                    "分かれば Markdown のプロパティに「業務の型」を書いてください"
                    "（何もしなくても公開できます）。",
                )
        elif item.kind == "link_type" and item.cardinality == "unknown":
            finding(
                "CARDINALITY_REQUIRED",
                "cardinality",
                "多重度（1 対多など）が分かりません。分かれば Markdown のリンク型に「多重度」を"
                "書くと、件数の数え方が正確になります（何もしなくても公開できます）。",
            )
        elif item.kind == "metric":
            required = (
                "expression_sql",
                "aggregation",
                "grain",
                "null_policy_ja",
                "unit",
                "time_policy_ja",
            )
            for field in required:
                if not getattr(item, field):
                    # 式・集約が無い指標は SQL に使えないため warning、ほかは補足（info）。
                    finding(
                        "METRIC_SEMANTICS_REQUIRED",
                        field,
                        f"指標の「{plain_label(field)}」が書かれていません。"
                        + (
                            "Markdown の指標に書くまで、この指標は SQL に使えません。"
                            if field in {"expression_sql", "aggregation"}
                            else "分かれば Markdown の指標に書いてください"
                            "（何もしなくても公開できます）。"
                        ),
                        severity=(
                            "warning" if field in {"expression_sql", "aggregation"} else "info"
                        ),
                    )
            if item.additivity == "unknown":
                finding(
                    "METRIC_ADDITIVITY_REQUIRED",
                    "additivity",
                    "指標の加法性（足し合わせてよいか）が書かれていません。"
                    "分かれば Markdown の指標に書いてください（何もしなくても公開できます）。",
                )
        elif item.kind == "function":
            if not item.return_type or not (item.expression_sql or item.implementation_key):
                finding(
                    "CONFIGURATION_REQUIRED",
                    "implementation_key",
                    "関数の戻り値の型と計算の方法（式）が書かれていません。"
                    "使う場合は Markdown の関数に書いてください。",
                )
        elif item.kind == "action_type" and (
            not item.permission_requirement_ja
            or not item.failure_policy_ja
            or not (item.assignments or item.implementation_key)
        ):
            finding(
                "CONFIGURATION_REQUIRED",
                "implementation_key",
                "アクション型の更新する項目・必要な権限・失敗時の扱いが書かれていません。"
                "使う場合は Markdown のアクション型に書いてください。",
            )
    return findings
