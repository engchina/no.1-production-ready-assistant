"""業務プロファイル推薦の信頼度公式（相対信頼度）の回帰テスト。

旧実装は `confidence = score / 6` を分類器経路・決定論経路で共有し、
- 学習済みモデルの predict_proba(0..1) が proba/6(≈0.167) に潰れる
- 実マッチ 0 でも +0.5/+0.2 の見かけ倒し加点で非ゼロ信頼度が出る
という欠陥があった。ここでは新しい `_relative_confidence` と経路別の信頼度を固定する。
"""

from __future__ import annotations

import importlib
import io
import json
from typing import Any, cast

import pytest

from app.features.nl2sql.models import (
    ClassifierPredictRequest,
    ClassifierTrainRequest,
    Nl2SqlProfile,
    ProfileRecommendationRequest,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore


def _single_sheet_workbook_bytes(title: str, rows: list[list[Any]]) -> bytes:
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _classifier_artifact_bytes(*classes: str) -> bytes:
    vector = [0.0] * 1536
    payload = {
        "classes": list(classes),
        "coef": [vector],
        "intercept": [5.0],
        "feature_dim": 1536,
        "embedding_model": "deterministic-hash-1536",
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class _FakeEmbeddingClient:
    """入金/支払を含む文だけ別次元に立てる決定論 embedding。"""

    def is_configured(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * 1536
            vector[1 if ("入金" in text or "支払" in text) else 2] = 1.0
            vectors.append(vector)
        return vectors


def _employee_project_catalog() -> SchemaCatalog:
    return SchemaCatalog(
        refreshed_at="2026-09-06T00:00:00.000Z",
        current_owner="ADMIN",
        tables=[
            SchemaTable(
                owner="ADMIN",
                table_name="EMPLOYEE",
                logical_name="従業員情報",
                comment="従業員情報",
                columns=[
                    SchemaColumn(
                        column_name="EMPLOYEE_NAME",
                        logical_name="従業員名",
                        data_type="VARCHAR2",
                        comment="従業員名",
                    )
                ],
            ),
            SchemaTable(
                owner="ADMIN",
                table_name="DEPARTMENT",
                logical_name="部署情報",
                comment="部署情報",
            ),
            SchemaTable(
                owner="ADMIN",
                table_name="PROJECT",
                logical_name="プロジェクト情報",
                comment="プロジェクト情報",
            ),
        ],
    )


def test_relative_confidence_math() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    rc = service._relative_confidence
    # 実マッチ皆無 → 0.0（「8%」を撲滅）
    assert rc([]) == 0.0
    assert rc([0.0, 0.0]) == 0.0
    # 独走: dominance=1.0, strength=3/(3+3)=0.5 → 0.5
    assert rc([3.0, 0.0]) == 0.5
    # 拮抗（曖昧）: dominance=0.5, strength=2/(2+3)=0.4 → 0.2（0.3 ゲート未満）
    assert rc([2.0, 2.0]) == 0.2
    # 強い独走はさらに高い
    assert rc([6.0, 0.0]) > 0.5


def test_deterministic_recommend_zero_confidence_for_irrelevant_question() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="hr",
            name="人事プロファイル",
            allowed_tables=["EMPLOYEE"],
            glossary={"社員": "EMPLOYEE.FULL_NAME"},
        )
    )
    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="今日の天気はどうですか")
    )
    # 何にも一致しない → 信頼度 0、候補スコアも全て 0（見かけ倒し加点が混入しない）
    assert recommendation.confidence == 0.0
    assert all(0.0 <= c.score <= 1.0 for c in recommendation.candidates)
    assert all(c.score == 0.0 for c in recommendation.candidates)


def test_deterministic_recommend_confident_for_matching_question() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="hr",
            name="人事プロファイル",
            allowed_tables=["EMPLOYEE"],
            glossary={"社員": "EMPLOYEE.FULL_NAME"},
        )
    )
    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="社員の一覧を見たい")
    )
    assert recommendation.recommended_profile_id == "hr"
    # 実マッチがある独走ケース → ヒント表示閾値(0.3)以上、上限 1.0 以内
    assert 0.3 <= recommendation.confidence <= 1.0
    # 候補スコアは 0..1 の相対シェア（「スコア X%」が 0-100% に収まる）
    assert all(0.0 <= c.score <= 1.0 for c in recommendation.candidates)
    assert recommendation.recommendation_source == "deterministic"


def test_deterministic_recommend_prefers_narrow_scope_over_all_scope_for_table_name() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _employee_project_catalog()  # noqa: SLF001 - recommendation fixture
    wide_scope = ["ADMIN.EMPLOYEE", "ADMIN.DEPARTMENT", "ADMIN.PROJECT"]
    service.create_profile(
        Nl2SqlProfile(
            id="all",
            name="ALL",
            category="ALL",
            allowed_tables=wide_scope,
            object_scope_version=2,
        )
    )
    service.create_profile(
        Nl2SqlProfile(
            id="default",
            name="既定プロファイル",
            category="既定",
            allowed_tables=wide_scope,
            object_scope_version=2,
        )
    )
    service.create_profile(
        Nl2SqlProfile(
            id="hr",
            name="人事プロファイル",
            category="人事",
            allowed_tables=["ADMIN.EMPLOYEE"],
            object_scope_version=2,
        )
    )

    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(
            question='対象テーブル："従業員情報"\n抽出項目：\n抽出条件：',
            current_profile_id="all",
        )
    )

    assert recommendation.recommended_profile_id == "hr"
    assert recommendation.confidence >= 0.3
    scores = {candidate.profile_id: candidate.score for candidate in recommendation.candidates}
    assert scores["hr"] > scores["all"]
    assert scores["hr"] > scores["default"]


def test_current_profile_bias_does_not_affect_deterministic_confidence() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="default",
            name="既定プロファイル",
            glossary={"社員": "EMPLOYEE.FULL_NAME"},
        )
    )
    service.create_profile(
        Nl2SqlProfile(
            id="hr",
            name="人事プロファイル",
            glossary={"社員": "EMPLOYEE.FULL_NAME"},
        )
    )

    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(
            question="社員を確認したい",
            current_profile_id="default",
        )
    )

    assert recommendation.recommended_profile_id == "default"
    assert recommendation.confidence == 0.2
    scores = {candidate.profile_id: candidate.score for candidate in recommendation.candidates}
    assert scores["default"] == 0.5
    assert scores["hr"] == 0.5


def test_few_shot_partial_overlap_contributes_to_deterministic_score() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="payment",
            name="入金管理",
            few_shot_examples=[
                {
                    "question": "未入金の請求を確認したい",
                    "sql": "SELECT * FROM INVOICES WHERE PAID_AT IS NULL",
                }
            ],
        )
    )

    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="未入金の請求一覧")
    )

    assert recommendation.recommended_profile_id == "payment"
    assert recommendation.confidence > 0.0
    payment = next(
        candidate for candidate in recommendation.candidates if candidate.profile_id == "payment"
    )
    assert payment.score == 1.0
    assert {"入金", "未入"} & set(payment.matched_terms)


def test_template_labels_only_do_not_create_few_shot_overlap_score() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="template",
            name="テンプレ学習",
            few_shot_examples=[
                {
                    "question": "対象テーブル：\n抽出項目：\n抽出条件：",
                    "sql": "SELECT * FROM CUSTOMERS",
                }
            ],
        )
    )

    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="対象テーブル：\n抽出項目：\n抽出条件：")
    )

    assert recommendation.confidence == 0.0
    template = next(
        candidate for candidate in recommendation.candidates if candidate.profile_id == "template"
    )
    assert template.score == 0.0
    assert template.matched_terms == []


def _sales_and_name_catalog() -> SchemaCatalog:
    def column(name: str, logical_name: str) -> SchemaColumn:
        return SchemaColumn(column_name=name, logical_name=logical_name, data_type="VARCHAR2")

    return SchemaCatalog(
        refreshed_at="2026-10-15T00:00:00.000Z",
        current_owner="ADMIN",
        tables=[
            SchemaTable(
                owner="ADMIN",
                table_name="SALES_PRODUCT",
                logical_name="売上サンプルの商品",
                comment="売上サンプルの商品",
                columns=[column("PRODUCT_NAME", "商品名"), column("CATEGORY", "商品分類")],
            ),
            SchemaTable(
                owner="ADMIN",
                table_name="SALES_ORDER",
                logical_name="売上サンプルの受注（1受注につき1商品）",
                comment="売上サンプルの受注（1受注につき1商品）",
                columns=[
                    column("STATUS", "受注状況（受注・出荷済・キャンセル）"),
                    column("SALES_AMOUNT", "売上金額（円）"),
                ],
            ),
            SchemaTable(
                owner="ADMIN",
                table_name="TEST_UEDA",
                logical_name="テスト用ユーザー情報テーブル",
                comment="テスト用ユーザー情報テーブル",
                columns=[column("NAME", "名前"), column("AGE", "年齢")],
            ),
        ],
    )


@pytest.mark.parametrize(
    "question",
    [
        "サンプル工業が注文した商品の名前をすべて挙げてください。",
        "キャンセルになった受注は何件で、金額はいくらですか？",
    ],
    ids=["customer-products", "cancelled"],
)
def test_deterministic_recommend_uses_description_and_logical_name_tokens(question: str) -> None:
    """名前・用語集に一致しない質問でも、説明と表・列の論理名の語の重なりで候補の先頭にする（#1959）。

    「商品の名前」は列の論理名「名前」（別の業務プロファイルの表）に全体で一致するが、売上の業務
    プロファイルは説明（「顧客・商品・受注」）と論理名（「商品名」「受注状況」）の語で上回る。
    """
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _sales_and_name_catalog()  # noqa: SLF001 - recommendation fixture
    service.create_profile(
        Nl2SqlProfile(
            id="sales",
            name="MCP_TEST_SALES",
            category="MCP テスト_営業分析",
            description="営業サンプル（顧客・商品・受注）の売上・受注件数を集計する。",
            allowed_tables=["ADMIN.SALES_PRODUCT", "ADMIN.SALES_ORDER"],
            object_scope_version=2,
        )
    )
    service.create_profile(
        Nl2SqlProfile(
            id="ueda",
            name="UEDA",
            category="UEDA",
            allowed_tables=["ADMIN.TEST_UEDA"],
            object_scope_version=2,
        )
    )

    recommendation = service.recommend_profile(ProfileRecommendationRequest(question=question))

    assert recommendation.candidates[0].profile_id == "sales"
    scores = {candidate.profile_id: candidate.score for candidate in recommendation.candidates}
    assert scores["sales"] > scores.get("ueda", 0.0)


def test_empty_v2_scope_profile_recommendation_keeps_allowed_objects_empty() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _employee_project_catalog()  # noqa: SLF001 - recommendation fixture
    service.create_profile(
        Nl2SqlProfile(
            id="empty",
            name="空スコープ",
            category="検証",
            allowed_tables=[],
            allowed_views=[],
            glossary={"空スコープ": "EMPTY_SCOPE"},
            object_scope_version=2,
        )
    )

    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="空スコープの profile を確認したい")
    )

    assert recommendation.recommended_profile_id == "empty"
    assert recommendation.recommended_allowed_objects.table_names == []
    assert recommendation.recommended_allowed_objects.columns == {}


def test_classifier_confidence_uses_probability_not_divided_by_six() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._embedding_client = _FakeEmbeddingClient()
    service.create_profile(
        Nl2SqlProfile(
            id="payment",
            name="入金管理",
            allowed_tables=["PAYMENTS", "INVOICES"],
            glossary={"入金": "PAYMENTS.PAID_AT"},
        )
    )
    service.import_classifier_training_data(
        filename="training_data.xlsx",
        content=_single_sheet_workbook_bytes(
            "training_data",
            [
                ["CATEGORY", "TEXT"],
                ["標準業務プロファイル", "請求金額が大きい取引先を見たい"],
                ["標準業務プロファイル", "売上合計を顧客別に確認したい"],
                ["入金管理", "入金が遅れている請求を確認したい"],
                ["入金管理", "未入金の支払状況を見たい"],
            ],
        ),
        replace=True,
    )
    status = service.train_classifier(ClassifierTrainRequest())
    assert status.ready

    prediction = service.predict_classifier(
        ClassifierPredictRequest(question="未入金の請求を確認したい")
    )
    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="未入金の請求を確認したい")
    )
    assert recommendation.recommendation_source == "classifier"
    best_proba = prediction.candidates[0].score
    # 信頼度は predict_proba そのもの（旧 proba/6 の潰れが無い）
    assert recommendation.confidence == round(best_proba, 3)
    assert recommendation.confidence > 0.2  # proba/6 なら最大 ≈0.167 に潰れていた


def test_classifier_category_scores_keep_same_category_profiles_separate() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.create_profile(
        Nl2SqlProfile(
            id="sales-east",
            name="東日本営業",
            category="営業",
            allowed_tables=["SALES_EAST"],
        )
    )
    service.create_profile(
        Nl2SqlProfile(
            id="sales-west",
            name="西日本営業",
            category="営業",
            allowed_tables=["SALES_WEST"],
        )
    )
    service.import_classifier_model_artifact(
        filename="classifier.json",
        content=_classifier_artifact_bytes("sales-east", "sales-west"),
    )

    recommendation = service.recommend_profile(
        ProfileRecommendationRequest(question="営業の売上を確認したい")
    )

    assert recommendation.recommendation_source == "classifier"
    assert {candidate.profile_id for candidate in recommendation.candidates} == {
        "sales-east",
        "sales-west",
    }
    assert {candidate.category for candidate in recommendation.candidates} == {"営業"}
    assert set(recommendation.category_scores) == {"sales-east", "sales-west"}
