"""サービスカタログ。

前処理(`services/preprocess/*`)と Parser(`services/parsers/*`)の各マイクロサービスを
1 つの静的レジストリに統合する。`service_id` は systemd の unit 名
(``production-ready-rag-<service_id>.service``)の元になり、起動/停止(systemd 制御)と
稼働状態プローブ(``systemctl show`` + /health)の双方の正本にする(#286)。

各サービスはサービスごとの uv の venv で動くネイティブのプロセスで、``127.0.0.1:<port>`` で
listen する(本番は ``rag/init_script.sh``、開発は ``rag/scripts/rag-services.sh`` が unit を作る)。

URL 設定名は既存実装(`parser_adapter_readiness._SERVICE_URL_FIELDS` /
`preprocess_strategy.PREPROCESS_SERVICE_URL_ATTRS`)と一致させる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.config import Settings

# 工程の並びはサイドナビ(ファイル準備 → 文書解析)と同じ。画面の工程の並び・名前はフロントの
# nav-config.ts が正本(#638)。検索インデックス・文書構造索引の構築・評価の基準の段は
# backend の中で動き、サービスを持たない(#3218)。
ServiceCategory = Literal[
    "preprocess",
    "parser",
]
# cpu/gpu はローカル ML 依存の重さで分ける(OCI のクラウドサービスを呼ぶ oci は #11 で削除した)。
ServiceProfile = Literal["cpu", "gpu"]
ServiceExecutionPolicy = Literal["selected_adapter", "default_adapter"]

# systemd の unit 名の接頭辞。init_script.sh / scripts/rag-services.sh が作る unit と同じ。
SYSTEMD_UNIT_PREFIX = "production-ready-rag-"
# 画面から操作してよい unit 名の形(allowlist と併せて検証する。shell のメタ文字・パスを通さない)。
SYSTEMD_UNIT_NAME_RE = re.compile(r"^production-ready-rag-[a-z0-9]+(?:-[a-z0-9]+)*\.service$")


@dataclass(frozen=True)
class ServiceCatalogEntry:
    """1 マイクロサービスのメタデータ。

    - ``service_id``: サービスの識別子(allowlist の鍵)。systemd の unit 名の元になる。
    - ``url_field``: base URL を持つ Settings フィールド名(/health 問い合わせ・取込の委譲先)。
    - ``label_key``: フロントの i18n キー(表示名)。
    - ``working_dir``: リポジトリ root からのサービス実装の相対パス(uv の venv を置く場所)。
    - ``port``: ``127.0.0.1`` で listen するポート(本番・開発で共通)。URL 設定の既定値と一致させる。
    - ``execution_policy``: 停止時・未使用時の runtime 契約。UI/API で fallback 境界を明示する。
    - ``model_cache_path``: モデル DL を行うサービスのキャッシュ親(実行ユーザーの ``~/.cache``)。
      HF・docling など tool 別キャッシュをまとめて含めるため ``huggingface`` ではなく
      ``.cache`` 親を指す。None のサービスはモデル DL なし。
    """

    service_id: str
    category: ServiceCategory
    profile: ServiceProfile
    url_field: str
    label_key: str
    working_dir: str
    port: int
    execution_policy: ServiceExecutionPolicy = "selected_adapter"
    model_cache_path: str | None = None

    @property
    def systemd_unit(self) -> str:
        """このサービスの systemd の unit 名。"""
        return f"{SYSTEMD_UNIT_PREFIX}{self.service_id}.service"

    @property
    def default_url(self) -> str:
        """URL 設定の既定値(ネイティブ配備で listen する ``127.0.0.1:<port>``)。"""
        return f"http://127.0.0.1:{self.port}"


# 工程の順に並べる(ファイル準備 → 文書解析 CPU / GPU → 文書分割以降の工程)。
SERVICE_CATALOG: tuple[ServiceCatalogEntry, ...] = (
    ServiceCatalogEntry(
        service_id="preprocess-office-to-pdf",
        category="preprocess",
        profile="cpu",
        url_field="rag_preprocess_office_to_pdf_service_url",
        label_key="settings.services.item.preprocessOfficeToPdf",
        working_dir="services/preprocess/office_to_pdf",
        port=18010,
    ),
    ServiceCatalogEntry(
        service_id="preprocess-csv-to-jsonl",
        category="preprocess",
        profile="cpu",
        url_field="rag_preprocess_csv_to_jsonl_service_url",
        label_key="settings.services.item.preprocessCsvToJsonl",
        working_dir="services/preprocess/csv_to_jsonl",
        port=18012,
    ),
    ServiceCatalogEntry(
        service_id="preprocess-excel-to-jsonl",
        category="preprocess",
        profile="cpu",
        url_field="rag_preprocess_excel_to_jsonl_service_url",
        label_key="settings.services.item.preprocessExcelToJsonl",
        working_dir="services/preprocess/excel_to_jsonl",
        port=18013,
    ),
    ServiceCatalogEntry(
        service_id="preprocess-word-to-jsonl",
        category="preprocess",
        profile="cpu",
        url_field="rag_preprocess_word_to_jsonl_service_url",
        label_key="settings.services.item.preprocessWordToJsonl",
        working_dir="services/preprocess/word_to_jsonl",
        port=18017,
    ),
    ServiceCatalogEntry(
        service_id="parser-docling",
        category="parser",
        profile="cpu",
        url_field="rag_parser_docling_service_url",
        label_key="settings.services.item.parserDocling",
        working_dir="services/parsers/docling",
        port=18020,
        model_cache_path="~/.cache",
    ),
    ServiceCatalogEntry(
        service_id="parser-unstructured",
        category="parser",
        profile="cpu",
        url_field="rag_parser_unstructured_service_url",
        label_key="settings.services.item.parserUnstructured",
        working_dir="services/parsers/unstructured",
        port=18022,
    ),
)

_CATALOG_BY_ID: dict[str, ServiceCatalogEntry] = {
    entry.service_id: entry for entry in SERVICE_CATALOG
}
# 画面から操作してよい systemd の unit(カタログのサービスだけ)。
ALLOWED_SYSTEMD_UNITS: frozenset[str] = frozenset(entry.systemd_unit for entry in SERVICE_CATALOG)


def get_catalog_entry(service_id: str) -> ServiceCatalogEntry | None:
    """service_id に対応するカタログエントリを返す(allowlist 照合)。未知なら None。"""
    return _CATALOG_BY_ID.get(service_id)


def is_allowed_systemd_unit(unit: str) -> bool:
    """unit 名が allowlist にあり、決まった形であるかを返す(任意の unit・文字列を拒否する)。"""
    return unit in ALLOWED_SYSTEMD_UNITS and SYSTEMD_UNIT_NAME_RE.fullmatch(unit) is not None


def is_dev_mode(settings: Settings) -> bool:
    """ローカル環境だけでサービス制御を自動的に有効にする。"""
    return settings.environment == "local"


def resolve_service_base_url(settings: Settings, url_field: str) -> str:
    """設定 ``url_field`` のサービス base URL を解決する(末尾スラッシュ除去)。

    既定値はネイティブ配備の ``http://127.0.0.1:<port>``(#286)。設定値をそのまま使い、空欄(=未設定)は
    空文字を返す。以前の Docker Compose の service 名(例 ``http://parser-docling:8000``)の読み替えは
    #356 で削除した(``backend/.env`` の値を ``127.0.0.1:<port>`` に直す。rag/docs/deployment.md)。

    稼働プローブ(/health)と取込の HTTP 委譲(/parse・/convert)で **同じ解決**を使い、
    「画面では到達できるのに取込では失敗」という不整合を防ぐ。
    """
    return str(getattr(settings, url_field, "") or "").strip().rstrip("/")


def service_health_url(settings: Settings, entry: ServiceCatalogEntry) -> str:
    """エントリの /health base URL を返す(``resolve_service_base_url`` と同じ解決)。"""
    return resolve_service_base_url(settings, entry.url_field)


def effective_execution_policy(
    settings: Settings, entry: ServiceCatalogEntry
) -> ServiceExecutionPolicy:
    """文書解析の全体設定で選んだサービスは、未指定のレシピが使う既定として示す。"""
    default_parser_id = "parser-" + settings.rag_parser_adapter_backend.replace("_", "-")
    if entry.category == "parser" and entry.service_id == default_parser_id:
        return "default_adapter"
    return entry.execution_policy
