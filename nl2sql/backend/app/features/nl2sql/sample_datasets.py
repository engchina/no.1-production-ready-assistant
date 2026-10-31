"""業務別サンプルの固定レジストリ。SQL のパスや対象名は入力から組み立てない。"""

from dataclasses import dataclass

from .models import SampleDataset

# サンプルデータの取り込み・削除は、他の管理 SQL 操作と同じ確認語で実行する。
SAMPLE_DATA_CONFIRMATION = "ADMIN_EXECUTE"


@dataclass(frozen=True)
class SampleDatasetDefinition:
    directory: str
    tables: tuple[str, ...]
    views: tuple[str, ...]
    # 旧バージョンが作成した名前の接頭辞。検出と削除だけに使い、新規作成はしない。
    legacy_prefix: str = ""

    @property
    def objects(self) -> tuple[str, ...]:
        return self.tables + self.views

    @property
    def legacy_names(self) -> dict[str, str]:
        """旧名 → 現行名。"""
        if not self.legacy_prefix:
            return {}
        return {f"{self.legacy_prefix}{name}": name for name in self.objects}


SAMPLE_DATASETS = {
    SampleDataset.HR: SampleDatasetDefinition(
        directory="sql_assist_sample",
        tables=("DEPARTMENT", "EMPLOYEE", "PROJECT"),
        views=("V_EMP_DEPT", "V_DEPT_PROJECT"),
    ),
    SampleDataset.SALES: SampleDatasetDefinition(
        directory="sales",
        tables=("SALES_CUSTOMER", "SALES_PRODUCT", "SALES_ORDER"),
        views=("V_SALES_DETAIL",),
        legacy_prefix="SAMPLE_NL2SQL_",
    ),
    SampleDataset.INQUIRIES: SampleDatasetDefinition(
        directory="inquiries",
        tables=("INQUIRY_CUSTOMER", "INQUIRY_CATEGORY", "INQUIRY_TICKET"),
        views=("V_INQUIRY_DETAIL",),
        legacy_prefix="SAMPLE_NL2SQL_",
    ),
}
