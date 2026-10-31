"""多段の質問の評価セット（#1335・#1352）の原稿と評価セットを、実体のデータから作る。

`rag/evaluation/multi-hop/` の原稿（`sources/*.html`・`sources/*.workbook.json`）と評価セット
（`multi-hop.json`）は、このファイルの実体（部署・システム・保守枠など）から決定的に作る。原稿の文と、
評価セットの正解（期待する語）・必要な根拠（`required_evidence`）は同じデータから作るため、資料を
増やしても正解と根拠の文がずれない。資料はすべて架空の「サンプル社」（と子会社の「サンプル物流社」）の
内容で、実在の顧客・製品の内容は入れない。

- #1335 の 33 問は ID・質問・期待する語をそのまま残し（`ORIGINAL_CASES`）、根拠の文は原稿と同じ
  データから引く。
- #1352 で足した問は、実体のデータから正解と根拠を組み立てる（`_new_cases`）。
- #1438 で足した問は、複数ページの章節を読み切らないと答えられない問（`section_read`。
  `_section_cases`）で、資料はサーバー室の定期点検要領（`_inspection`。章節を改ページで
  2〜3 ページに分ける）。根拠の文ごとに、置いたページ（`Document.pages`）を持つ。
- #1503 で足した問は、件数・章の範囲を示して網羅させる問（`sr-inspection-stop-six` と、文書
  全体の複数の章に散らばる記載を集める `document_read`。`_document_read_cases`）。点検表の欄の
  表の値も根拠にする。
- 資料を増やしても #1335 の問の正解が変わらないことは `_check_constraints` で確かめる（例: 重要度 A
  かつ極秘のシステムは今も 2 つだけ、情報システム部が担当するシステムは 1 つだけ）。

PDF / xlsx への変換は `generate_evaluation_corpus.py` が行う（このファイルの `write_sources` を先に
呼ぶ）。原稿だけを作り直すときは、リポジトリ直下から::

    uv run --project rag/backend python rag/scripts/generate_evaluation_corpus.py \\
        rag/evaluation/multi-hop --sources-only
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCES_DIRNAME = "sources"
GOLDEN_SET_NAME = "multi-hop.json"
# 評価セットの検索の件数（RAG の既定の top_k）。資料はこの件数で全部が文脈に入らない大きさにする。
TOP_K = 20
EXPECTED_OUTCOMES = ("answered", "conditional")

LEDGER = "system-ledger.xlsx"
ORGANIZATION = "organization-rules.pdf"
APPROVAL = "approval-rules.pdf"
CHANGE = "change-procedure.pdf"
MAINTENANCE = "maintenance-plan.pdf"
INCIDENT = "incident-contact-rules.pdf"
RETENTION = "data-retention-rules.pdf"
# #1352 で足した資料（紛らわしい規程・旧版・別の会社・新しい段の資料）。
APPROVAL_2023 = "approval-rules-2023.pdf"
MAINTENANCE_PREVIOUS = "maintenance-plan-2025.pdf"
PURCHASE = "purchase-approval-rules.pdf"
ACCESS = "access-request-procedure.pdf"
BACKUP = "backup-rules.pdf"
GUIDE = "system-operation-guide.pdf"
LOGISTICS_ORGANIZATION = "logistics-organization-rules.pdf"
LOGISTICS_LEDGER = "logistics-system-ledger.xlsx"
# 複数ページの章節を持つ資料（#1438）。
INSPECTION = "server-room-inspection.pdf"
# 文書の版（#1366）: 旧版 → 置き換えた新しい版。取り込み（`evaluation_corpus_cli`）が旧版を
# 「新しい版に置き換えた文書」として登録し、既定の検索から外す（製品で旧版を扱うときと同じ）。
DOCUMENT_VERSIONS: tuple[tuple[str, str], ...] = (
    (APPROVAL_2023, APPROVAL),
    (MAINTENANCE_PREVIOUS, MAINTENANCE),
)

_STYLE = (
    'body{font-family:"Noto Sans CJK JP","Noto Sans JP",sans-serif;font-size:11pt;line-height:1.6}'
    "h1{font-size:18pt}h2{font-size:14pt;margin-top:1.4em}"
)


# ---- 実体 ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Department:
    """部署（略号は漢字 1 文字）。`division` は所属の本部（None は本部に属さない）。"""

    code: str
    slug: str
    name: str
    division: str | None
    approver: str
    approver_note: str = ""
    nickname: str | None = None


@dataclass(frozen=True)
class System:
    """システム台帳の 1 行。"""

    number: int
    name: str
    alias: str
    dept: str
    severity: str
    confidentiality: str
    prefix: str = "SYS"

    @property
    def sys_id(self) -> str:
        return f"{self.prefix}-{self.number}"


DEPARTMENTS: tuple[Department, ...] = (
    # #1335 の部署（略号・名前・承認者は変えない）。
    Department("情", "jo", "情報システム部", "管理本部", "情報システム部長", nickname="情シス"),
    Department(
        "経",
        "kei",
        "経理部",
        "管理本部",
        "管理本部長",
        "会計の記録に関わるため、部長ではなく本部長が承認します。",
    ),
    Department("人", "jin", "人事部", "管理本部", "人事部長"),
    Department("総", "so", "総務部", "管理本部", "総務部長"),
    Department("営", "ei", "営業企画部", "営業本部", "営業本部長"),
    # #1352 で足した部署（名前の似た部署・略号の紛らわしい部署を含む）。
    Department("財", "zai", "財務部", "管理本部", "財務部長"),
    Department("労", "ro", "労務部", "管理本部", "労務部長"),
    Department(
        "安", "an", "情報セキュリティ部", "管理本部", "情報セキュリティ部長", nickname="情セキ"
    ),
    Department("企", "ki", "経営企画部", "管理本部", "経営企画部長"),
    Department("法", "hou", "法務部", "管理本部", "法務部長"),
    Department("広", "koho", "広報部", "管理本部", "広報部長"),
    Department("業", "gyo", "営業推進部", "営業本部", "営業推進部長"),
    Department("顧", "kokyaku", "顧客サポート部", "営業本部", "顧客サポート部長"),
    Department(
        "製",
        "sei",
        "製造部",
        "生産本部",
        "生産本部長",
        "製造の計画に関わるため、部長ではなく本部長が承認します。",
    ),
    Department("品", "hin", "品質保証部", "生産本部", "品質保証部長"),
    Department("物", "butsu", "物流部", "生産本部", "物流部長"),
    Department("購", "kobai", "購買部", "生産本部", "購買部長"),
    Department("研", "ken", "研究開発部", "生産本部", "研究開発部長"),
    Department(
        "監",
        "kan",
        "内部監査室",
        None,
        "担当役員",
        "監査の独立を保つため、本部長ではなく担当役員が承認します。",
    ),
)
DEPARTMENT_BY_CODE = {item.code: item for item in DEPARTMENTS}

# システム台帳（80 行）。SYS-101〜108 は #1335 の行で、値を変えない。
SYSTEMS: tuple[System, ...] = tuple(
    System(number, name, alias, dept, severity, confidentiality)
    for number, name, alias, dept, severity, confidentiality in (
        (101, "経費精算ポータル", "経費 Portal", "経", "B", "社外秘"),
        (102, "勤怠管理システム", "勤怠", "人", "B", "社外秘"),
        (103, "人事評価システム", "HRM", "人", "A", "極秘"),
        (104, "受発注管理システム", "OMS、受発注", "営", "A", "社外秘"),
        (105, "ドキュメントポータル", "Document Portal", "総", "B", "社内限り"),
        (106, "会議室予約システム", "会議室予約", "総", "C", "社内限り"),
        (107, "資産管理システム", "AMS", "情", "B", "社外秘"),
        (108, "予算管理システム", "BMS", "経", "A", "極秘"),
        (109, "交通費精算ポータル", "交通費 Portal", "経", "B", "社外秘"),
        (110, "勤怠集計システム", "勤怠集計", "労", "C", "社外秘"),
        (111, "人事給与システム", "HRP", "労", "A", "社外秘"),
        (112, "人材評価分析システム", "HRA", "人", "B", "極秘"),
        (113, "受注分析システム", "受注分析", "業", "B", "社外秘"),
        (114, "発注管理システム", "POMS", "購", "A", "社外秘"),
        (115, "ドキュメント管理システム", "DMS", "総", "B", "社内限り"),
        (116, "会議室案内システム", "会議室案内", "総", "C", "社内限り"),
        (117, "資産台帳システム", "資産台帳", "財", "B", "社外秘"),
        (118, "予算実績システム", "予実", "企", "B", "極秘"),
        (119, "購買管理システム", "PMS", "購", "A", "社外秘"),
        (120, "案件管理システム", "PMS", "業", "B", "社外秘"),
        (121, "倉庫管理システム", "WMS", "物", "A", "社外秘"),
        (122, "配送管理システム", "TMS", "物", "B", "社内限り"),
        (123, "生産計画システム", "生産計画", "製", "A", "社外秘"),
        (124, "製造実行システム", "MES", "製", "A", "社外秘"),
        (125, "品質記録システム", "QMS", "品", "A", "社外秘"),
        (126, "検査成績書システム", "検査成績", "品", "B", "社外秘"),
        (127, "サプライヤーポータル", "Supplier Portal", "購", "B", "社外秘"),
        (128, "カスタマーポータル", "Customer Portal", "顧", "A", "社外秘"),
        (129, "問い合わせ管理システム", "CRM", "顧", "B", "極秘"),
        (130, "顧客台帳システム", "顧客台帳", "業", "B", "極秘"),
        (131, "契約書管理システム", "CLM", "法", "B", "極秘"),
        (132, "法令データベース", "法令DB", "法", "C", "社内限り"),
        (133, "研究データ管理システム", "RDM", "研", "B", "極秘"),
        (134, "試作管理システム", "試作管理", "研", "C", "社外秘"),
        (135, "広報素材ライブラリ", "素材ライブラリ", "広", "C", "社内限り"),
        (136, "社内報配信システム", "社内報", "広", "C", "社内限り"),
        (137, "監査記録システム", "監査記録", "監", "B", "極秘"),
        (138, "内部通報窓口システム", "通報窓口", "監", "B", "極秘"),
        (139, "ID 管理システム", "IDM", "安", "A", "社外秘"),
        (140, "ログ監視システム", "SIEM", "安", "A", "社外秘"),
        (141, "脆弱性管理システム", "脆弱性管理", "安", "B", "社外秘"),
        (142, "資金管理システム", "TMS", "財", "A", "社外秘"),
        (143, "固定資産システム", "FAS", "財", "B", "社外秘"),
        (144, "経営ダッシュボード", "経営DB", "企", "B", "極秘"),
        (145, "中期計画管理システム", "中計", "企", "C", "極秘"),
        (146, "採用管理システム", "ATS", "人", "B", "極秘"),
        (147, "研修管理システム", "LMS", "人", "C", "社内限り"),
        (148, "健康管理システム", "健康管理", "労", "B", "極秘"),
        (149, "社宅管理システム", "社宅", "総", "C", "社外秘"),
        (150, "文書保管庫システム", "文書保管庫", "総", "B", "社外秘"),
        (151, "名刺管理システム", "名刺", "業", "C", "社外秘"),
        (152, "見積管理システム", "見積", "業", "B", "社外秘"),
        (153, "販売管理システム", "SMS", "営", "A", "社外秘"),
        (154, "販売分析システム", "販売分析", "営", "C", "社外秘"),
        (155, "請求書発行システム", "請求書", "経", "A", "社外秘"),
        (156, "支払管理システム", "支払", "経", "B", "社外秘"),
        (157, "会計システム", "GL", "経", "A", "社外秘"),
        (158, "連結会計システム", "連結", "財", "B", "極秘"),
        (159, "在庫管理システム", "IMS", "物", "A", "社外秘"),
        (160, "入出荷管理システム", "入出荷", "物", "B", "社内限り"),
        (161, "設備保全システム", "設備保全", "製", "B", "社内限り"),
        (162, "工程管理システム", "工程管理", "製", "B", "社外秘"),
        (163, "図面管理システム", "PDM", "研", "B", "極秘"),
        (164, "特許管理システム", "特許", "法", "C", "極秘"),
        (165, "取引先審査システム", "与信", "財", "B", "極秘"),
        (166, "電子契約システム", "電子契約", "法", "B", "社外秘"),
        (167, "受付管理システム", "受付", "総", "C", "社内限り"),
        (168, "備品予約システム", "備品予約", "総", "C", "社内限り"),
        (169, "アンケート集計システム", "アンケート", "広", "C", "社内限り"),
        (170, "ウェブサイト管理システム", "CMS", "広", "B", "社内限り"),
        (171, "コールセンターシステム", "CTI", "顧", "A", "社外秘"),
        (172, "FAQ 管理システム", "FAQ", "顧", "C", "社内限り"),
        (173, "返品管理システム", "返品", "顧", "B", "社外秘"),
        (174, "輸出管理システム", "輸出管理", "法", "B", "社外秘"),
        (175, "原価管理システム", "原価", "経", "B", "極秘"),
        (176, "経費分析システム", "経費分析", "企", "C", "社外秘"),
        (177, "稟議システム", "稟議", "総", "A", "社外秘"),
        (178, "監査調書システム", "調書", "監", "C", "極秘"),
        (179, "端末管理システム", "MDM", "安", "B", "社外秘"),
        (180, "勤務シフト管理システム", "シフト", "労", "C", "社内限り"),
    )
)
SYSTEM_BY_NUMBER = {item.number: item for item in SYSTEMS}

# 別の会社（サンプル物流社。サンプル社の子会社という設定の架空の会社）の部署とシステム。略号と
# 略称がサンプル社のものと衝突する（「経」「総」「情」、OMS・BMS・DMS）。
LOGISTICS_DEPARTMENTS: tuple[Department, ...] = (
    Department("経", "kei", "経理課", "管理部", "経理課長"),
    Department("総", "so", "総務課", "管理部", "総務課長"),
    Department("情", "jo", "情報システム課", "管理部", "管理部長"),
    Department("運", "un", "運行管理課", "輸送部", "運行管理課長"),
    Department("倉", "so-ko", "倉庫課", "輸送部", "倉庫課長"),
)
LOGISTICS_SYSTEMS: tuple[System, ...] = tuple(
    System(number, name, alias, dept, severity, confidentiality, prefix="SL")
    for number, name, alias, dept, severity, confidentiality in (
        (201, "運行管理システム", "OMS", "運", "A", "社外秘"),
        (202, "経費精算システム", "経費精算", "経", "B", "社外秘"),
        (203, "配車計画システム", "配車", "運", "A", "社外秘"),
        (204, "庫内作業管理システム", "WES", "倉", "B", "社外秘"),
        (205, "車両管理システム", "車両", "運", "B", "社内限り"),
        (206, "勤怠打刻システム", "打刻", "総", "C", "社外秘"),
        (207, "ドライバー教育システム", "教育", "総", "C", "社内限り"),
        (208, "燃料管理システム", "燃料", "運", "C", "社内限り"),
        (209, "荷主ポータル", "Shipper Portal", "情", "B", "社外秘"),
        (210, "請求管理システム", "BMS", "経", "B", "社外秘"),
        (211, "安全運転記録システム", "安全記録", "運", "B", "極秘"),
        (212, "事故報告システム", "事故報告", "総", "B", "極秘"),
        (213, "輸送実績システム", "実績", "運", "C", "社外秘"),
        (214, "ハンディ端末管理システム", "HTM", "情", "C", "社内限り"),
        (215, "入出庫記録システム", "入出庫", "倉", "B", "社外秘"),
        (216, "棚卸システム", "棚卸", "倉", "C", "社内限り"),
        (217, "文書管理システム", "DMS", "総", "C", "社内限り"),
        (218, "給与計算システム", "給与", "経", "A", "社外秘"),
        (219, "協力会社管理システム", "協力会社", "運", "B", "社外秘"),
        (220, "運賃計算システム", "運賃", "経", "B", "社外秘"),
    )
)

# 役職 → 承認の期限（営業日）と根拠の ID（承認規程 第 2 章）。
ROLE_DEADLINE_DAYS = {"部長": 3, "本部長": 5, "担当役員": 7}
ROLE_EVIDENCE = {"部長": "ap-bucho", "本部長": "ap-honbucho", "担当役員": "ap-yakuin"}
# 機密区分 → 保管の年数（データ保管規程）と、利用権限の承認者（利用権限の申請手順）。
RETENTION_YEARS = {"極秘": 10, "社外秘": 7, "社内限り": 3}
RETENTION_EVIDENCE = {"極秘": "dr-gokuhi", "社外秘": "dr-shagaihi", "社内限り": "dr-shanai"}
ACCESS_EVIDENCE = {"極秘": "ac-gokuhi", "社外秘": "ac-shagaihi", "社内限り": "ac-shanai"}
# 重要度 → 障害の最初の連絡・復旧の目標（障害連絡規程）とバックアップ（バックアップ規程）。
FIRST_CONTACT = {
    "A": ("in-a", "検知から 30 分以内に、担当部署の承認者と情シス部長に電話で連絡します", "30分"),
    "B": ("in-b", "検知から 2 時間以内に、担当部署の承認者に連絡します", "2時間"),
    "C": ("in-c", "翌営業日までに、担当部署に報告します", "翌営業日"),
}
RECOVERY = {"A": "4 時間以内", "B": "1 営業日以内", "C": "3 営業日以内"}
RECOVERY_EVIDENCE = {"A": "in-recover-a", "B": "in-recover-b", "C": "in-recover-c"}
BACKUP_RULES = {
    "A": ("毎日 2 回（0 時と 12 時）バックアップを取り、35 世代を保管します", "2回"),
    "B": ("毎日 1 回（2 時）バックアップを取り、14 世代を保管します", "1回"),
    "C": ("毎週日曜日に 1 回バックアップを取り、4 世代を保管します", "毎週日曜"),
}


@dataclass(frozen=True)
class MaintenanceWindow:
    """定期保守計画の個別の保守枠（`label` は計画に書く表記。全角・英字の表記ゆれを含む）。"""

    slug: str
    label: str
    system: int
    window: str


MAINTENANCE_2026: tuple[MaintenanceWindow, ...] = (
    MaintenanceWindow("hrm", "ＨＲＭ", 103, "毎月第 2 土曜日の 22:00〜翌 2:00"),
    MaintenanceWindow("oms", "ＯＭＳ", 104, "毎月第 4 日曜日の 1:00〜5:00"),
    MaintenanceWindow("doc", "Document Portal", 105, "毎週水曜日の 20:00〜22:00"),
    MaintenanceWindow("wms", "ＷＭＳ", 121, "毎月第 1 火曜日の 21:00〜23:00"),
    MaintenanceWindow("mes", "ＭＥＳ", 124, "毎月第 3 日曜日の 2:00〜6:00"),
    MaintenanceWindow("customer", "Customer Portal", 128, "毎月第 1 日曜日の 0:00〜4:00"),
    MaintenanceWindow("pms", "ＰＭＳ（購買）", 119, "毎月第 3 木曜日の 20:00〜23:00"),
    MaintenanceWindow("gl", "ＧＬ", 157, "毎月第 1 土曜日の 20:00〜24:00"),
)
MAINTENANCE_COMMON_2026 = "毎月最終金曜日の 20:00〜24:00"
# 旧版（2025 年度。2026 年 3 月 31 日で終了）。今の計画と同じシステムで、別の時間帯。
MAINTENANCE_2025: tuple[MaintenanceWindow, ...] = (
    MaintenanceWindow("hrm", "ＨＲＭ", 103, "毎月第 1 土曜日の 22:00〜翌 2:00"),
    MaintenanceWindow("oms", "ＯＭＳ", 104, "毎月第 4 土曜日の 1:00〜5:00"),
    MaintenanceWindow("doc", "Document Portal", 105, "毎週木曜日の 20:00〜22:00"),
    MaintenanceWindow("wms", "ＷＭＳ", 121, "毎月第 2 火曜日の 21:00〜23:00"),
)
MAINTENANCE_COMMON_2025 = "毎月最終土曜日の 20:00〜24:00"

# 障害連絡規程で、本部長にも連絡するシステム（システム ID は全角で書く）。
ESCALATIONS = (
    ("in-oms", 104, "取引先に影響するため", "営業本部長"),
    ("in-ims", 159, "出荷が止まるため", "生産本部長"),
)

# システム運用要領の連携（連携先 → 連携元・受け取るデータ）。
LINKS: Mapping[int, tuple[int, str]] = {
    121: (104, "受注データ"),
    124: (123, "生産計画"),
    159: (121, "入出庫の実績"),
    155: (153, "売上データ"),
    157: (156, "支払データ"),
    171: (129, "問い合わせの履歴"),
    163: (133, "試験データ"),
    114: (119, "購買の依頼"),
}
_GUIDE_HOURS = ("24 時間（毎日）", "平日の 8:00〜22:00", "毎日 6:00〜24:00", "平日の 9:00〜18:00")
_GUIDE_NOTES = (
    "月末は利用が多いため、大きな変更を避けます。",
    "利用者の登録は、申請の翌営業日に反映します。",
    "操作の手引きは社内ポータルに載せています。",
    "帳票は夜間の処理で作ります。",
    "画面の表示が遅いときは、まず担当部署に知らせます。",
)


# ---- 文字の扱い ----------------------------------------------------------------------------


def fullwidth(text: str) -> str:
    """ASCII の英数字・記号を全角にする（空白はそのまま。表記ゆれの原稿に使う）。"""
    return "".join(chr(ord(char) + 0xFEE0) if 0x21 <= ord(char) <= 0x7E else char for char in text)


def normalize(text: str) -> str:
    """照合用の形（NFKC・大小文字・空白を無視。評価の `contains_normalized` と同じ考え方）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def _is_ascii(text: str) -> bool:
    return all(ord(char) < 0x80 for char in text)


def approver_role(approver: str) -> str:
    """承認者の役職（承認規程の期限を決める）。"""
    if approver == "担当役員":
        return "担当役員"
    if approver.endswith("本部長"):
        return "本部長"
    if approver.endswith("部長"):
        return "部長"
    raise ValueError(f"役職が分かりません: {approver}")


def window_keyword(window: str) -> str:
    """保守枠の文から期待する語（「毎月第 2 土曜日…」→「第2土曜」、「毎週水曜日…」→「水曜」）。"""
    if match := re.search(r"第 (\d) (.)曜日", window):
        return f"第{match.group(1)}{match.group(2)}曜"
    if match := re.search(r"最終(.)曜日", window):
        return f"最終{match.group(1)}曜"
    if match := re.search(r"毎週(.)曜日", window):
        return f"{match.group(1)}曜"
    raise ValueError(f"保守枠の曜日が分かりません: {window}")


def _mention(system: System) -> str:
    """ほかの資料でのシステムの書き方（英字の略称は全角、衝突する略称は正式名を添える）。"""
    alias = system.alias.split("、")[0]
    collides = sum(alias in other.alias.split("、") for other in SYSTEMS) > 1
    if _is_ascii(alias) and " " not in alias and not collides:
        return fullwidth(alias)
    if _is_ascii(alias) and collides:
        return f"{system.name}（{fullwidth(alias)}）"
    return system.name


# ---- 資料の原稿 ----------------------------------------------------------------------------


@dataclass
class Document:
    """1 つの資料の原稿と、その中の根拠の文（ID → 文）。"""

    file_name: str
    source_name: str
    content: str
    evidence: dict[str, str] = field(default_factory=dict)
    # 根拠の文を置いたページ（改ページで分けた資料だけ。#1438）。
    pages: dict[str, int] = field(default_factory=dict)


def _page(title: str, heading: str, meta: str, chapters: Sequence[tuple[str, str]]) -> str:
    parts = [
        "<!doctype html>",
        f'<html lang="ja"><head><meta charset="utf-8"><title>{html.escape(title)}</title>',
        f"<style>{_STYLE}</style></head>",
        "<body>",
        f"<h1>{html.escape(heading)}</h1>",
        f"<p>{meta}</p>",
    ]
    for number, (chapter, body) in enumerate(chapters, start=1):
        parts.extend(["", f"<h2>第 {number} 章 {html.escape(chapter)}</h2>", body])
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def _ul(items: Iterable[str]) -> str:
    return "<ul>\n" + "".join(f"<li>{item}</li>\n" for item in items) + "</ul>"


def _code_text(dept: Department) -> str:
    if dept.division is None:
        return f"略号「{dept.code}」: {dept.name}。どの本部にも属さず、社長の直属です"
    return f"略号「{dept.code}」: {dept.name}。{dept.division}に属します"


def _approver_text(dept: Department) -> str:
    return f"{dept.name}の承認者は{dept.approver}です"


def _nickname_text(dept: Department) -> str:
    return f"通称「{dept.nickname}」と呼びます"


def _organization() -> Document:
    evidence: dict[str, str] = {}
    codes = []
    approvers = []
    for dept in DEPARTMENTS:
        evidence[f"org-code-{dept.slug}"] = _code_text(dept)
        line = _code_text(dept) + "。"
        if dept.nickname:
            evidence[f"org-nick-{dept.slug}"] = _nickname_text(dept)
            line += _nickname_text(dept) + "。"
        codes.append(line)
        evidence[f"org-appr-{dept.slug}"] = _approver_text(dept)
        approvers.append(_approver_text(dept) + "。" + dept.approver_note)
    evidence.update(
        {
            "org-proxy-ref": "承認者が不在のときの代理は第 4 章を参照してください",
            "org-proxy": "その部署が属する本部の本部長が代理で承認します",
            "org-proxy-exec": "本部長が不在のときは、担当役員が代理で承認します",
            "org-proxy-direct": "承認者が担当役員のときは、代表取締役が代理で承認します",
        }
    )
    content = _page(
        "サンプル社 組織規程（システムの担当と承認者）",
        "組織規程（システムの担当と承認者）",
        "発行: 2026年4月1日　対象: サンプル社（架空の会社。評価用の合成資料）",
        [
            (
                "目的",
                "<p>この規程は、社内のシステムを担当する部署と、その部署の承認者を定めます。"
                "どのシステムをどの部署が担当するかは、システム台帳の「担当部署」の列に、部署の略号"
                "（漢字 1 文字）で書きます。</p>",
            ),
            ("部署と略号", _ul(codes)),
            (
                "部署の承認者",
                "<p>システムの変更・停止・データの消去の申請は、そのシステムを担当する部署の承認者が"
                "承認します。承認者は部署ごとに次のとおりです。</p>\n"
                + _ul(approvers)
                + f"\n<p>{evidence['org-proxy-ref']}。</p>",
            ),
            (
                "承認者の代理",
                "<p>承認者が 3 営業日以上不在のときは、その部署が属する本部の本部長が"
                "代理で承認します。"
                f"{evidence['org-proxy-exec']}。本部に属さない部署の承認者が不在のときと、"
                f"{evidence['org-proxy-direct']}。</p>",
            ),
        ],
    )
    return Document(ORGANIZATION, "organization-rules", content, evidence)


def _approval() -> Document:
    deadlines = {
        role: f"{role}が承認する申請は、受付から {days} 営業日以内に承認します"
        for role, days in ROLE_DEADLINE_DAYS.items()
    }
    evidence = {ROLE_EVIDENCE[role]: text for role, text in deadlines.items()}
    evidence.update(
        {
            "ap-overdue-ref": "期限までに承認されないときの扱いは第 4 章を参照してください",
            "ap-emergency-ref": "緊急の変更は第 3 章の手順で進めます",
            "ap-emergency": "作業の後 24 時間以内に承認者へ事後の承認を申請します",
            "ap-overdue": "申請者は承認者の 1 つ上の役職に催促を依頼します",
            "ap-record": "承認の記録は、承認した日から 5 年間保管します",
        }
    )
    content = _page(
        "サンプル社 承認規程",
        "承認規程",
        "発行: 2026年4月1日　対象: サンプル社（架空の会社。評価用の合成資料）",
        [
            (
                "目的",
                "<p>この規程は、申請を承認する期限と、期限を過ぎたときの扱いを定めます。"
                "誰が承認するかは組織規程で決まります。</p>",
            ),
            (
                "承認の期限",
                "<p>承認の期限は、承認者の役職で決まります。期限は、申請を受け付けた日の翌営業日から"
                "数えます。</p>\n"
                + _ul(text + "。" for text in deadlines.values())
                + f"\n<p>{evidence['ap-overdue-ref']}。{evidence['ap-emergency-ref']}。</p>",
            ),
            (
                "緊急の変更",
                "<p>障害の復旧のためにすぐに必要な変更は、承認の前に作業してかまいません。ただし、"
                f"{evidence['ap-emergency']}。</p>",
            ),
            (
                "期限を過ぎたとき",
                f"<p>期限を過ぎても承認されないときは、{evidence['ap-overdue']}（部長の 1 つ上は"
                "本部長、本部長の 1 つ上は担当役員です）。催促を依頼した日から 2 営業日"
                "以内に承認され"
                "なければ、申請は取り下げとして扱います。</p>",
            ),
            ("承認の記録", f"<p>{evidence['ap-record']}。</p>"),
        ],
    )
    return Document(APPROVAL, "approval-rules", content, evidence)


def _approval_2023() -> Document:
    """旧版の承認規程（廃止済み。期限・記録の保管が今の版と違う）。"""
    evidence = {
        "ap2023-bucho": "部長の承認は、申請を受け付けてから 5 営業日以内に行います",
        "ap2023-honbucho": "本部長の承認は、申請を受け付けてから 7 営業日以内に行います",
        "ap2023-yakuin": "担当役員の承認は、申請を受け付けてから 10 営業日以内に行います",
        "ap2023-overdue": "期限を過ぎたときは、総務部が承認者に催促します",
        "ap2023-record": "承認の記録は、承認した日から 3 年間保管します",
    }
    content = _page(
        "サンプル社 承認規程（2023年度版）",
        "承認規程（2023年度版）",
        "発行: 2023年4月1日　廃止: 2026年3月31日（2026年4月1日発行の承認規程に置き換えました）"
        "　対象: サンプル社（架空の会社。評価用の合成資料）",
        [
            (
                "目的",
                "<p>この規程は、申請の承認の期限を定めていました。この版は廃止しました。今の期限は"
                "2026年4月1日発行の承認規程で確かめます。</p>",
            ),
            (
                "承認の期限",
                _ul(
                    evidence[key] + "。"
                    for key in ("ap2023-bucho", "ap2023-honbucho", "ap2023-yakuin")
                ),
            ),
            ("期限を過ぎたとき", f"<p>{evidence['ap2023-overdue']}。</p>"),
            ("承認の記録", f"<p>{evidence['ap2023-record']}。</p>"),
        ],
    )
    return Document(APPROVAL_2023, "approval-rules-2023", content, evidence)


def _purchase() -> Document:
    """似た承認の規程（購買の申請。システムの変更の申請とは別の承認者・期限）。"""
    evidence = {
        "pu-small": "50 万円未満の購買は、購買部長が承認します",
        "pu-mid": "50 万円以上 500 万円未満の購買は、生産本部長が承認します",
        "pu-exec": "500 万円以上の購買は、担当役員が承認します",
        "pu-deadline": "購買の申請は、承認者の役職にかかわらず、受付から 2 営業日以内に承認します",
        "pu-overdue-ref": "期限を過ぎたときの扱いは第 4 章を参照してください",
        "pu-overdue": "申請者は購買部に連絡し、購買部が承認者に催促します",
    }
    content = _page(
        "サンプル社 購買承認規程",
        "購買承認規程",
        "発行: 2026年4月1日　対象: サンプル社の購買（物品・サービスの購入）の申請"
        "（架空の会社。評価用の合成資料）",
        [
            (
                "目的",
                "<p>この規程は、購買の申請の承認者と承認の期限を定めます。システムの変更の申請は"
                "承認規程で扱い、この規程の対象ではありません。</p>",
            ),
            (
                "購買の承認者",
                "<p>購買の承認者は、購買の金額で決まります。</p>\n"
                + _ul(evidence[key] + "。" for key in ("pu-small", "pu-mid", "pu-exec")),
            ),
            (
                "承認の期限",
                f"<p>{evidence['pu-deadline']}。{evidence['pu-overdue-ref']}。</p>",
            ),
            (
                "期限を過ぎたとき",
                f"<p>期限を過ぎても承認されないときは、{evidence['pu-overdue']}。</p>",
            ),
        ],
    )
    return Document(PURCHASE, "purchase-approval-rules", content, evidence)


def _change() -> Document:
    evidence = {
        "ch-items-ref": "申請書に書く項目は第 3 章を参照してください",
        "ch-item-rollback": "切り戻しの手順",
        "ch-window-a": "重要度 A のシステムの変更は、定期保守の時間帯にだけ作業します",
        "ch-window-bc": "重要度 B と C のシステムの変更は、平日の 19 時から 22 時の間に作業します",
    }
    content = _page(
        "サンプル社 システム変更手順書",
        "システム変更手順書",
        "発行: 2026年4月1日　対象: サンプル社の本番環境のシステム（架空の会社。評価用の合成資料）",
        [
            (
                "対象",
                "<p>この手順書は、本番環境のシステムの設定の変更とリリースに使います。対象のシステムは、"
                "システム台帳に載っているものに限ります。</p>",
            ),
            (
                "申請",
                "<p>変更の申請は、変更するシステムを担当する部署の承認者に出します。担当部署はシステム"
                f"台帳で、承認者は組織規程で確かめます。{evidence['ch-items-ref']}。</p>",
            ),
            (
                "申請書の項目",
                "<ol>\n<li>変更の内容と理由</li>\n<li>影響を受けるシステムと利用者</li>\n"
                f"<li>{evidence['ch-item-rollback']}</li>\n<li>作業の日時</li>\n</ol>\n"
                "<p>経費 Portal の変更では、上の 4 項目に加えて「月次の締め処理への影響」"
                "を書きます。</p>",
            ),
            (
                "作業の時間帯",
                f"<p>{evidence['ch-window-a']}。時間帯は定期保守計画で確かめます。"
                f"{evidence['ch-window-bc']}。</p>",
            ),
            (
                "利用者への告知",
                "<p>システムの停止を伴う変更は、作業の 5 営業日前までに社内ポータルの"
                "お知らせで利用者に"
                "告知します。ただし、重要度 A のシステムは 10 営業日前までに告知します。</p>",
            ),
        ],
    )
    return Document(CHANGE, "change-procedure", content, evidence)


def _maintenance_line(item: MaintenanceWindow) -> str:
    return f"{item.label}: {item.window}"


def _maintenance(
    *,
    file_name: str,
    source_name: str,
    year: str,
    prefix: str,
    windows: Sequence[MaintenanceWindow],
    common: str,
    meta: str,
) -> Document:
    evidence = {f"{prefix}-{item.slug}": _maintenance_line(item) for item in windows}
    evidence[f"{prefix}-common-ref"] = "この章に無いシステムは、第 2 章の共通の保守枠で保守します"
    evidence[f"{prefix}-common"] = f"共通の保守枠は、{common} です"
    content = _page(
        f"サンプル社 定期保守計画 {year}年度",
        f"定期保守計画 {year}年度",
        meta,
        [
            (
                "個別の保守枠",
                "<p>次のシステムは、システムごとの保守枠で保守します。</p>\n"
                + _ul(_maintenance_line(item) for item in windows)
                + f"\n<p>{evidence[f'{prefix}-common-ref']}。</p>",
            ),
            ("共通の保守枠", f"<p>{evidence[f'{prefix}-common']}。</p>"),
            (
                "保守枠の変更",
                "<p>保守枠を変えたいときは、変えたい月の 1 か月前までに情シスへ申し出ます。</p>",
            ),
        ],
    )
    return Document(file_name, source_name, content, evidence)


def _incident() -> Document:
    evidence = {key: text for key, text, _keyword in FIRST_CONTACT.values()}
    # #1335 の根拠の文のまま（重要度 C だけは「重要度 C:」から書いている）。
    evidence["in-c"] = f"重要度 C: {FIRST_CONTACT['C'][1]}"
    evidence.update(
        {
            RECOVERY_EVIDENCE[severity]: f"重要度 {severity} のシステムは {target}に復旧します"
            for severity, target in RECOVERY.items()
        }
    )
    escalations = []
    for key, number, reason, contact in ESCALATIONS:
        evidence[key] = (
            f"{fullwidth(SYSTEM_BY_NUMBER[number].sys_id)} の障害は、{reason}、"
            f"{contact}にも連絡します"
        )
        escalations.append(evidence[key] + "。")
    evidence.update(
        {
            "in-means-ref": "連絡の手段は第 3 章を参照してください",
            "in-means": (
                "社内チャットの障害チャンネル（#incident）に書き込み、15 分ごとに電話をかけ直します"
            ),
        }
    )
    content = _page(
        "サンプル社 障害連絡規程",
        "障害連絡規程",
        "発行: 2026年4月1日　対象: サンプル社のシステム（架空の会社。評価用の合成資料）",
        [
            (
                "最初の連絡",
                "<p>障害を見つけた人は、システムの重要度に応じて次のとおり連絡します。システムの重要度は、"
                "システム台帳の「重要度」の列で確かめます。</p>\n"
                + _ul(
                    f"重要度 {severity}: {text}。"
                    for severity, (_k, text, _w) in FIRST_CONTACT.items()
                )
                + "\n<p>"
                + "".join(escalations)
                + f"{evidence['in-means-ref']}。</p>",
            ),
            (
                "復旧の目標",
                _ul(evidence[RECOVERY_EVIDENCE[severity]] + "。" for severity in RECOVERY),
            ),
            (
                "連絡の手段",
                f"<p>連絡は電話で行います。電話がつながらないときは、{evidence['in-means']}。</p>",
            ),
        ],
    )
    return Document(INCIDENT, "incident-contact-rules", content, evidence)


def _retention() -> Document:
    lines = {
        "極秘": "極秘のデータは 10 年間保管します。社外への持ち出しを禁止します。",
        "社外秘": "社外秘のデータは 7 年間保管します。",
        "社内限り": "社内限りのデータは 3 年間保管します。",
    }
    evidence = {
        RETENTION_EVIDENCE[name]: f"{name}のデータは {years} 年間保管します"
        for name, years in RETENTION_YEARS.items()
    }
    evidence.update(
        {
            "dr-erase-ref": "保管期間を過ぎたデータの消し方は第 3 章を参照してください",
            "dr-erase": "システムを担当する部署の承認者の承認を得てから消去します",
            "dr-erase-report": "消去の記録は総務部に提出します",
        }
    )
    content = _page(
        "サンプル社 データ保管規程",
        "データ保管規程",
        "発行: 2026年4月1日　対象: サンプル社のシステムのデータ（架空の会社。評価用の合成資料）",
        [
            (
                "機密区分と保管期間",
                "<p>システムのデータは、そのシステムの機密区分に応じて保管します。システムの機密区分は、"
                "システム台帳の「機密区分」の列で確かめます。</p>\n"
                + _ul(lines[name] for name in RETENTION_YEARS)
                + f"\n<p>{evidence['dr-erase-ref']}。</p>",
            ),
            (
                "閲覧の記録",
                "<p>極秘のデータを扱うシステムは、閲覧の記録（アクセスログ）を 3 年間"
                "保管します。</p>",
            ),
            (
                "データの消去",
                f"<p>保管期間を過ぎたデータは、{evidence['dr-erase']}。"
                f"{evidence['dr-erase-report']}。</p>",
            ),
        ],
    )
    return Document(RETENTION, "data-retention-rules", content, evidence)


def _access() -> Document:
    """利用権限の申請手順（機密区分 → 権限の承認者。情報セキュリティ部は通称で書く）。"""
    evidence = {
        "ac-gokuhi": "極秘のシステムの権限は、担当部署の承認者と情セキ部長の両方が承認します",
        "ac-shagaihi": "社外秘のシステムの権限は、担当部署の承認者が承認します",
        "ac-shanai": "社内限りのシステムの権限は、申請者の上長が承認します",
        "ac-review-ref": "権限の見直しは第 4 章を参照してください",
        "ac-review-gokuhi": "極秘のシステムの権限は、半年ごとに見直します",
    }
    content = _page(
        "サンプル社 利用権限の申請手順",
        "利用権限の申請手順",
        "発行: 2026年4月1日　対象: サンプル社のシステムの利用権限（架空の会社。評価用の合成資料）",
        [
            (
                "対象",
                "<p>この手順は、システムの利用権限の追加・変更の申請に使います。権限の承認者は、"
                "システムの機密区分で決まります。機密区分はシステム台帳で確かめます。</p>",
            ),
            (
                "権限の承認者",
                _ul(evidence[key] + "。" for key in ("ac-gokuhi", "ac-shagaihi", "ac-shanai")),
            ),
            (
                "申請の期限",
                "<p>権限は、使い始める日の 3 営業日前までに申請します。"
                f"{evidence['ac-review-ref']}。</p>",
            ),
            (
                "権限の見直し",
                f"<p>{evidence['ac-review-gokuhi']}。それ以外のシステムの権限は、1 年ごとに"
                "見直します。</p>",
            ),
        ],
    )
    return Document(ACCESS, "access-request-procedure", content, evidence)


def _backup() -> Document:
    evidence = {
        f"bk-{severity.lower()}": f"重要度 {severity} のシステムは、{text}"
        for severity, (text, _keyword) in BACKUP_RULES.items()
    }
    evidence.update(
        {
            "bk-restore-ref": "復元の手順は第 3 章を参照してください",
            "bk-restore": "復元は、担当部署の承認者の承認を得てから情シスに依頼します",
        }
    )
    content = _page(
        "サンプル社 バックアップ規程",
        "バックアップ規程",
        "発行: 2026年4月1日　対象: サンプル社のシステム（架空の会社。評価用の合成資料）",
        [
            (
                "目的",
                "<p>この規程は、システムのバックアップの頻度と、保管する世代を定めます。どちらも"
                "システムの重要度で決まります。重要度はシステム台帳の「重要度」の列で確かめます。</p>",
            ),
            (
                "頻度と世代",
                _ul(evidence[f"bk-{severity.lower()}"] + "。" for severity in BACKUP_RULES)
                + f"\n<p>{evidence['bk-restore-ref']}。</p>",
            ),
            (
                "復元",
                f"<p>{evidence['bk-restore']}。復元した後は、担当部署が内容を確かめます。</p>",
            ),
            (
                "保管の場所",
                "<p>バックアップは本番とは別の拠点に保管します。極秘のデータのバックアップは暗号化"
                "します。</p>",
            ),
        ],
    )
    return Document(BACKUP, "backup-rules", content, evidence)


def _guide_name(system: System) -> str:
    """運用要領の見出しの書き方（名前の英字は全角）。"""
    return fullwidth(system.name)


def _link_text(target: int) -> str:
    source, data = LINKS[target]
    return f"{_mention(SYSTEM_BY_NUMBER[source])}から{data}を受け取ります"


def _guide() -> Document:
    """システム運用要領（システムごとの章。利用時間・連携・運用の注意）。"""
    evidence: dict[str, str] = {}
    chapters = [
        (
            "この要領の使い方",
            "<p>この要領は、システムごとの利用時間・連携・運用の注意をまとめます。担当部署・重要度・"
            "機密区分はシステム台帳で、保守の時間帯は定期保守計画で確かめます。</p>",
        )
    ]
    for system in SYSTEMS:
        sentences = [
            f"{_guide_name(system)}（{system.sys_id}）の利用時間は、"
            f"{_GUIDE_HOURS[system.number % len(_GUIDE_HOURS)]}です。"
        ]
        if system.number in LINKS:
            evidence[f"guide-{system.number}-link"] = _link_text(system.number)
            sentences.append(_link_text(system.number) + "。")
        sentences.append(_GUIDE_NOTES[system.number % len(_GUIDE_NOTES)])
        chapters.append((_guide_name(system), "<p>" + "".join(sentences) + "</p>"))
    content = _page(
        "サンプル社 システム運用要領",
        "システム運用要領",
        "発行: 2026年4月1日　作成: 情シス　対象: サンプル社のシステム台帳のシステム"
        "（架空の会社。評価用の合成資料）",
        chapters,
    )
    return Document(GUIDE, "system-operation-guide", content, evidence)


def _logistics_organization() -> Document:
    """別の会社（サンプル物流社）の組織規程。略号はサンプル社と衝突する。"""
    evidence: dict[str, str] = {}
    for dept in LOGISTICS_DEPARTMENTS:
        evidence[f"lg-org-code-{dept.slug}"] = _code_text(dept)
        evidence[f"lg-org-appr-{dept.slug}"] = _approver_text(dept)
    content = _page(
        "サンプル物流社 組織規程（システムの担当と承認者）",
        "サンプル物流社 組織規程（システムの担当と承認者）",
        "発行: 2026年4月1日　対象: サンプル物流社（サンプル社の子会社。架空の会社。評価用の"
        "合成資料）",
        [
            (
                "対象",
                "<p>この規程はサンプル物流社だけに適用します。部署の略号は、サンプル社の略号とは別に"
                "決めています。システムの担当部署は、サンプル物流社のシステム台帳で確かめます。</p>",
            ),
            (
                "部署と略号",
                _ul(evidence[f"lg-org-code-{dept.slug}"] + "。" for dept in LOGISTICS_DEPARTMENTS),
            ),
            (
                "部署の承認者",
                _ul(evidence[f"lg-org-appr-{dept.slug}"] + "。" for dept in LOGISTICS_DEPARTMENTS)
                + "\n<p>サンプル物流社では、課長が承認する申請は受付から 2 営業日以内に、部長が"
                "承認する申請は受付から 4 営業日以内に承認します。</p>",
            ),
        ],
    )
    return Document(LOGISTICS_ORGANIZATION, "logistics-organization-rules", content, evidence)


def _ledger_sheet(title: str, preamble: Sequence[str], systems: Sequence[System]) -> dict[str, Any]:
    return {
        "title": title,
        "preamble": list(preamble),
        "header": ["システムID", "正式名", "略称・別表記", "担当部署", "重要度", "機密区分"],
        "rows": [
            [s.sys_id, s.name, s.alias, s.dept, s.severity, s.confidentiality] for s in systems
        ],
        "text_columns": ["A", "B", "C", "D", "E", "F"],
    }


# 重要度の順位（#1406）。重要度を比べる問（`cmp-pms-severity`）の結論の根拠。検証は記号の
# 一般的な順位を資料に無い推論として通さないため、台帳の前書きに書く（前書きの 1 つのセルで、
# 根拠はセルの全文）。
LEDGER_SEVERITY_ORDER = "重要度は A・B・C の 3 段階です。A が最も高く、B、C の順に低くなります。"


def _ledger() -> Document:
    sheet = _ledger_sheet(
        "システム台帳",
        [
            "サンプル社のシステム台帳（架空の会社。評価用の合成資料）",
            "担当部署は部署の略号（漢字 1 文字）で書きます。略号と部署の正式名は組織規程の第 2 章を"
            "参照してください。",
            LEDGER_SEVERITY_ORDER,
        ],
        SYSTEMS,
    )
    evidence = {f"ledger-{s.number}": s.sys_id for s in SYSTEMS}
    evidence["ledger-severity-order"] = LEDGER_SEVERITY_ORDER
    return Document(LEDGER, "system-ledger", _workbook_json([sheet]), evidence)


def _logistics_ledger() -> Document:
    sheet = _ledger_sheet(
        "システム台帳",
        [
            "サンプル物流社のシステム台帳（サンプル社の子会社。架空の会社。評価用の合成資料）",
            "担当部署はサンプル物流社の部署の略号です。サンプル物流社の組織規程を参照してください。",
        ],
        LOGISTICS_SYSTEMS,
    )
    evidence = {f"lg-ledger-{s.number}": s.sys_id for s in LOGISTICS_SYSTEMS}
    return Document(LOGISTICS_LEDGER, "logistics-system-ledger", _workbook_json([sheet]), evidence)


def _workbook_json(sheets: Sequence[Mapping[str, Any]]) -> str:
    """表の原稿の JSON（行は 1 行に 1 つ。読みやすさのため）。"""

    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = ["{", '  "sheets": [']
    for index, sheet in enumerate(sheets):
        lines.append("    {")
        lines.append(f'      "title": {dump(sheet["title"])},')
        lines.append('      "preamble": [')
        lines.append(",\n".join(f"        {dump(line)}" for line in sheet["preamble"]))
        lines.append("      ],")
        lines.append(f'      "header": {dump(sheet["header"])},')
        lines.append('      "rows": [')
        lines.append(",\n".join(f"        {dump(row)}" for row in sheet["rows"]))
        lines.append("      ],")
        lines.append(f'      "text_columns": {dump(sheet["text_columns"])}')
        lines.append("    }" + ("," if index < len(sheets) - 1 else ""))
    lines.extend(["  ]", "}"])
    return "\n".join(lines) + "\n"


# ---- 複数ページの章節を読み切る資料（#1438） ----------------------------------------------------

# 改ページ（LibreOffice の HTML の変換は、インラインの style の改ページだけを守る）。
PAGE_BREAK = ' style="page-break-before: always"'

# サーバー室の定期点検要領の章節。章節（h2 / h3）の中を改ページで複数ページに分け、1 ページに
# 項目を 2〜3 つ置く。項目の文（根拠）の後ろに補足の文を続け、ページの終わりに点検表の欄の表を
# 置く（表が本文の子 chunk を区切り、1 つの chunk・1 つの抜粋に章節の項目がそろわない）。
# 各ページは (根拠の ID, 項目の文, 補足) の並び。
_INSPECTION_PAGES: dict[str, tuple[tuple[tuple[str, str, str], ...], ...]] = {
    "prep": (
        (
            (
                "sv-prep-reserve",
                "点検の 5 営業日前までに、総務部にサーバー室の入室の予約をします",
                "予約では、点検の日時・作業者の氏名・持ち込む測定器を伝えます。予約の無い入室は"
                "警備室で断られるため、日程を変えたときも予約を取り直します。入室の予約は点検の"
                "責任者が行い、作業者に予約の番号を知らせます。",
            ),
            (
                "sv-prep-staff",
                "点検は 2 人以上で行い、そのうち 1 人は情報システム部の担当者にします",
                "電源設備に触れる作業があるため、1 人での点検はしません。作業者は前の年度の点検"
                "の記録を読み、設備の場所と点検の順を確かめておきます。外部の業者に頼む場合も、"
                "社内の作業者が 1 人は立ち会います。",
            ),
            (
                "sv-prep-sheet",
                "前回の点検の記録を見て、今回の点検表を作ります",
                "点検表には、前回に直すことになった箇所と、その後に直したかどうかを書いておきます。"
                "点検表は第 5 章の記録の様式と同じ欄を使い、点検の当日に記入します。",
            ),
        ),
        (
            (
                "sv-prep-monitor",
                "点検の前日までに、監視の担当者に監視の一時停止を依頼します",
                "電源設備と空調設備の点検では、測定のために警告の値を一時的に超えることがあります。"
                "一時停止する監視の項目と時間を伝え、停止した時刻を点検表に書きます。停止の依頼は"
                "メールで送り、監視の担当者の返信を受け取ってから点検を始めます。",
            ),
            (
                "sv-prep-notice",
                "点検の 3 営業日前までに、点検の日時をサーバー室を使う部署に知らせます",
                "サーバー室に機器を置いている部署は、点検の時間に機器の作業を入れないようにします。"
                "知らせる文には、点検の責任者の連絡先と、点検で止める設備が無いことを書きます。",
            ),
            (
                "sv-prep-key",
                "点検の当日は、警備室でサーバー室の鍵を受け取ります",
                "鍵は点検の責任者が受け取り、受け取った時刻を警備室の台帳に書きます。鍵は作業者の"
                "あいだで手渡さず、点検が終わるまで責任者が持ちます。",
            ),
        ),
        (
            (
                "sv-prep-parts",
                "電源装置のバッテリーと空調のフィルターの予備部品が倉庫にあることを確かめます",
                "予備部品が無いと、点検で見つけた不具合をその日に直せません。数が足りないときは、"
                "点検の 10 営業日前までに購買の担当者に発注を頼みます。倉庫の在庫の表には、最後に"
                "数えた日付を書きます。",
            ),
            (
                "sv-prep-meter",
                "温度計と電圧計の校正の期限が切れていないことを確かめます",
                "校正の期限が切れた測定器で測った値は、点検の記録に使えません。期限は測定器に"
                "貼った札で確かめ、切れていれば情報システム部の予備の測定器を借ります。",
            ),
            (
                "sv-prep-safety",
                "絶縁手袋と懐中電灯を用意し、作業者の人数分あることを確かめます",
                "懐中電灯は、点検の途中で照明が消えたときのために使います。絶縁手袋は破れが無いか"
                "を確かめ、破れていれば新しいものに替えます。",
            ),
        ),
    ),
    "power": (
        (
            (
                "sv-power-range",
                "無停電電源装置の出力の電圧が 200 V から 210 V の間にあることを確かめます",
                "電圧は電源装置の表示と電圧計の両方で測り、差が 2 V を超えたら表示の不具合として"
                "記録します。測った値は点検表の電源設備の欄に書きます。",
            ),
            (
                "sv-power-battery",
                "バッテリーの交換の時期の表示が出ていないことを確かめます",
                "交換の時期の表示が出ていたら、予備のバッテリーに替えます。替えたバッテリーの番号"
                "は点検の記録に書きます。電源の盤の扉は、点検が終わったら必ず閉めます。分電盤の"
                "ブレーカーには触れず、表示の灯の色だけを確かめます。",
            ),
        ),
    ),
    "air": (
        (
            (
                "sv-air-range",
                "サーバー室の室温が 18 度から 27 度の間にあることを確かめます",
                "室温は、ラックの列ごとに前面の吸気の側で測ります。列のあいだで 5 度以上の差が"
                "あれば、空調の吹き出し口がふさがれていないかを見ます。湿度も同じ場所で測り、"
                "点検表に書きます。",
            ),
            (
                "sv-air-filter",
                "空調のフィルターに目に見えるほこりが付いていないことを確かめます",
                "ほこりが付いていたら、予備のフィルターに替えます。外したフィルターは袋に入れて"
                "サーバー室の外へ出します。空調の室外機の点検は、この要領の対象ではありません。",
            ),
        ),
    ),
    "fire": (
        (
            (
                "sv-fire-range",
                "消火ガスのボンベの圧力計の針が緑の範囲にあることを確かめます",
                "針が緑の範囲の外にあれば、消火設備の業者に連絡します。ボンベの弁には触れません。"
                "圧力計の値は点検表の消火設備の欄に書きます。",
            ),
            (
                "sv-fire-detector",
                "煙の感知器の表示の灯が緑に点いていることを確かめます",
                "表示の灯が消えている・赤に点いている感知器は、場所の番号を点検表に書きます。"
                "感知器の試験のボタンは押しません（警報が鳴り、ビル全体に知らせが出るため）。"
                "消火設備の年に 1 回の点検は、業者が別の日に行います。",
            ),
        ),
    ),
    "stop": (
        (
            (
                "sv-stop-leak",
                "床や天井に漏水を見つけたときは、点検を中止します",
                "漏水を見つけたら、水に近い機器に触れずにサーバー室の外へ出て、総務部と情報"
                "システム部に知らせます。漏水の場所と見つけた時刻を点検表に書きます。",
            ),
            (
                "sv-stop-smell",
                "焦げたにおいがするときは、点検を中止します",
                "においのもとを探して機器の扉を開けることはしません。においに気づいた時刻と、"
                "においの強かった場所を点検表に書き、警備室にも知らせます。",
            ),
            (
                "sv-stop-heat",
                "室温が 32 度を超えたときは、点検を中止します",
                "室温が高いまま作業を続けると、作業者の体調を崩すおそれがあります。空調の不具合"
                "として情報システム部に知らせ、室温が下がるまで入室しません。",
            ),
        ),
        (
            (
                "sv-stop-alarm",
                "火災の警報が鳴ったときは、点検を中止します",
                "警報が鳴ったら、作業の途中でも道具をそのままにしてサーバー室の外へ出ます。消火"
                "ガスが出るおそれがあるため、扉を閉めて戻りません。警報が止まった後に入室するとき"
                "は、警備室の許可を得ます。",
            ),
            (
                "sv-stop-battery",
                "電源装置がバッテリーで動いているときは、点検を中止します",
                "バッテリーで動いているのは、建物の電源が止まっているためです。電源設備の点検を"
                "続けず、情報システム部に知らせて指示を待ちます。",
            ),
            (
                "sv-stop-alone",
                "作業者が 1 人だけになったときは、点検を中止します",
                "体調不良などで作業者が抜けたら、残った作業者は点検を続けずに外へ出ます。点検は"
                "別の日に、2 人以上でやり直します。",
            ),
        ),
    ),
    "record": (
        (
            (
                "sv-record-name",
                "点検の記録には、点検の日時と点検者の氏名を書きます",
                "点検者の氏名は、立ち会った外部の業者の名前も書きます。日時は、入室した時刻と"
                "退室した時刻の両方を書きます。記録は点検表の様式を使い、手書きでもかまいません。",
            ),
            (
                "sv-record-values",
                "点検の記録には、設備ごとに測った値を書きます",
                "値は第 3 章の設備ごとの欄に書き、範囲の外の値には印を付けます。測れなかった値は"
                "空けずに「測れなかった」と理由を書きます。",
            ),
        ),
        (
            (
                "sv-record-parts",
                "点検の記録には、交換した部品とその数を書きます",
                "交換した部品は、部品の名前・番号・数を書き、倉庫の在庫の表からも減らします。"
                "交換しなかったときは「交換なし」と書きます。",
            ),
            (
                "sv-record-todo",
                "点検の記録には、次の点検までに直すことを書きます",
                "直すことには、担当する人と期限を書きます。前回の点検で直すことになった箇所を直した"
                "かどうかも、この欄に書きます。",
            ),
        ),
    ),
    "after": (
        (
            (
                "sv-after-monitor",
                "点検が終わったら、監視の担当者に監視の再開を依頼します",
                "再開の依頼の前に、一時停止した監視の項目を点検表で確かめます。監視の担当者から再開"
                "したと返信が来たら、その時刻を点検表に書きます。",
            ),
            (
                "sv-after-key",
                "サーバー室の鍵は、点検が終わったその日のうちに警備室へ返します",
                "鍵を返した時刻を警備室の台帳に書きます。点検が翌日に続くときも、鍵はその日の"
                "うちに返し、翌日に受け取り直します。",
            ),
        ),
        (
            (
                "sv-after-report",
                "点検の記録は、点検の日から 3 営業日以内に情報システム部長へ提出します",
                "提出するのは点検の責任者です。範囲の外の値や中止があったときは、記録と一緒に"
                "経緯を書いた文を添えます。",
            ),
            (
                "sv-after-refill",
                "使った予備部品は、点検の日から 10 営業日以内に補充します",
                "補充は購買の担当者に頼み、届いたら倉庫の在庫の表に書きます。補充が間に合わない"
                "ときは、次の点検の準備で確かめる部品の欄に書いておきます。",
            ),
        ),
    ),
}

# 各項目の補足の続き（1 ページの本文を子 chunk の目標の 1000 文字に近くするため）。
_INSPECTION_DETAILS: dict[str, str] = {
    "sv-prep-reserve": (
        "サーバー室は月に 1 回、ほかの部署の工事や機器の入れ替えにも使われるため、予約が重な"
        "ったときは先に予約した作業を優先します。重なりが分かったら、総務部から点検の責任者に"
        "連絡が来ます。その場合は、総務部と相談して点検の日を前後の週に移します。"
    ),
    "sv-prep-staff": (
        "作業者のうち 1 人は、前の年度に点検をしたことのある人にします。初めて点検をする人だ"
        "けで組むと、設備の場所を探すのに時間がかかり、決めた時間の中で点検が終わらないことが"
        "あるためです。作業者の名前は、入室の予約のときに総務部へ伝えた名前と同じにします。"
    ),
    "sv-prep-sheet": (
        "点検表は紙でも表計算のファイルでもかまいませんが、当日に書き込めるよう、紙なら人数分"
        "を印刷し、ファイルなら持ち込む端末に入れておきます。前回の点検表は、点検の責任者が情"
        "報システム部の共有のフォルダから取り出します。"
    ),
    "sv-prep-monitor": (
        "依頼の文には、一時停止の開始と終了の予定の時刻、止める監視の項目、点検の責任者の連絡"
        "先を書きます。監視を止めているあいだに本当の不具合が起きても気づけないため、止める時"
        "間はできるだけ短くし、点検の順も止める項目がまとまるように組みます。"
    ),
    "sv-prep-notice": (
        "知らせを受けた部署から、点検の時間に機器の作業をどうしても入れたいと返事があったとき"
        "は、点検の責任者が作業の時刻をずらしてもらうよう頼みます。ずらせないときは、その部署"
        "の作業者の入室の時刻を点検表に書いておきます。"
    ),
    "sv-prep-key": (
        "警備室では、入室の予約の番号と作業者の名前を確かめてから鍵を渡します。予約の番号を忘"
        "れると鍵を受け取れないため、点検の責任者は番号を控えて持っていきます。鍵を受け取った"
        "後は、作業者全員でサーバー室の前に集まってから入室します。"
    ),
    "sv-prep-parts": (
        "倉庫の鍵は総務部が管理しているため、予備部品を確かめるときは総務部に立ち会いを頼みま"
        "す。バッテリーには製造の年月の札が付いているので、製造から 4 年を過ぎたものは予備に"
        "数えず、入れ替えの対象として在庫の表に印を付けます。"
    ),
    "sv-prep-meter": (
        "測定器は、点検の前の週に一度電源を入れ、電池が残っていることも確かめます。予備の測定"
        "器を借りたときは、借りた測定器の番号を点検表に書き、点検の後に情報システム部へ返しま"
        "す。測定器の校正は年に 1 回、情報システム部がまとめて業者に頼みます。"
    ),
    "sv-prep-safety": (
        "安全具は、サーバー室の入口の棚に置いてあるものを使います。棚に足りないときは、総務部"
        "の備品の倉庫から借ります。点検の後は、使った安全具を元の棚に戻し、足りなくなったもの"
        "があれば総務部に知らせます。"
    ),
    "sv-power-range": (
        "電圧を測るときは、電源装置の正面の測定の端子を使い、盤の中の配線には触れません。範囲"
        "の外の値が続けて 2 回出たときは、電源装置の業者に連絡して、点検の日のうちに見てもら"
        "うよう頼みます。業者が来るまでのあいだ、電源装置の表示の写真を撮っておきます。測定の"
        "前には、電圧計の針が 0 を指していることも確かめます。"
    ),
    "sv-power-battery": (
        "バッテリーを替えるときは、電源装置の取扱説明書の手順に従い、1 つずつ替えます。一度に"
        "複数のバッテリーを外すと、電源装置が止まるおそれがあるためです。替えた後は、電源装置"
        "の表示で、新しいバッテリーが使える状態になったことを確かめます。古いバッテリーは倉庫"
        "の回収の箱に入れます。"
    ),
    "sv-air-range": (
        "測る場所は、ラックの列の端と真ん中の 3 か所です。値は小数点の下 1 桁まで書きます。範"
        "囲の外の値があったときは、10 分おいてから同じ場所で測り直し、2 回とも範囲の外なら空"
        "調の不具合として情報システム部に知らせます。測り直した値も点検表に書きます。吸気の側"
        "の床の通風口に物が置かれていないことも、このときに見ます。"
    ),
    "sv-air-filter": (
        "フィルターを替えるときは、空調の運転を止めずに、1 台ずつ替えます。替える前と後で、吹"
        "き出し口の風の強さに違いが無いかを手で確かめます。替えたフィルターの数は、第 5 章の"
        "記録の交換した部品の欄に書きます。空調の運転の表示に警告が出ていないことも確かめます"
        "。"
    ),
    "sv-fire-range": (
        "圧力計は、ボンベの上の丸い計器です。針が緑の範囲の端にあるときも、範囲の中として扱い"
        "、点検表には「端」と書き添えます。ボンベの周りに物が置かれているときは、ボンベから離"
        "れた場所に移します。ボンベの数は 4 本で、すべての圧力計を確かめます。"
    ),
    "sv-fire-detector": (
        "感知器は天井に 12 個あり、場所の番号は天井の図に書いてあります。図はサーバー室の入口"
        "に貼ってあります。表示の灯が見えにくいときは、懐中電灯で照らして確かめます。脚立に乗"
        "るときは、もう 1 人の作業者が脚立を支えます。"
    ),
    "sv-stop-leak": (
        "漏水が機器の上から落ちているときは、自分で止めようとせず、総務部の担当者が来るのを待"
        "ちます。総務部の担当者が来たら、漏水の場所を案内し、点検表を見せて点検の途中で中止し"
        "たことを伝えます。"
    ),
    "sv-stop-smell": (
        "においが弱く、すぐに消えたときも、点検は中止します。焦げたにおいは、機器の中の部品の"
        "故障の始まりであることが多く、点検を続けて機器に触れると故障を広げるおそれがあるため"
        "です。"
    ),
    "sv-stop-heat": (
        "室温は、第 3 章の空調設備の点検で測った値で判断します。32 度を超えていなくても、作業"
        "者が暑さで気分が悪くなったときは、同じように点検を中止します。"
    ),
    "sv-stop-alarm": (
        "警報が誤りだと分かっても、その日の点検は再開しません。警報の原因が分かってから、別の"
        "日に最初からやり直します。警報が鳴った時刻と、そのときに点検していた設備を点検表に書"
        "きます。"
    ),
    "sv-stop-battery": (
        "電源が戻っても、その日の点検は再開しません。電源が止まっていたあいだの電源装置の記録"
        "は、情報システム部が業者と一緒に確かめます。"
    ),
    "sv-stop-alone": (
        "作業者が戻ってくるまで待つことはしません。1 人で設備に触れているときに事故が起きると"
        "、助けを呼べないためです。中止した時刻と理由を点検表に書きます。"
    ),
    "sv-record-name": (
        "外部の業者が立ち会ったときは、業者の会社名と立ち会った人の名前を書きます。点検の責任"
        "者は、記録の最後に自分の名前を書き、記録の内容を確かめたことを示します。記録は点検の"
        "日のうちに書き終えます。点検を途中で中止したときも、中止した時刻と理由を書いた記録を"
        "同じ様式で残します。"
    ),
    "sv-record-values": (
        "値は、点検表に書いた値をそのまま写します。電源設備の電圧、空調設備の室温と湿度、消火"
        "設備の圧力計の針の位置と感知器の表示の灯の色が、記録に書く値です。前回の値と比べて大"
        "きく変わった値には、その旨を書き添えます。"
    ),
    "sv-record-parts": (
        "部品の番号は、部品の箱か部品に貼ってある札の番号を写します。電源装置のバッテリーと空"
        "調のフィルターのほか、点検の途中で替えた電球やケーブルも書きます。交換した部品の写真"
        "を撮ったときは、写真のファイルの名前も書きます。"
    ),
    "sv-record-todo": (
        "直すことには、直す内容と、直す前の様子を書きます。業者に頼むものは、頼んだ日と業者の"
        "会社名も書きます。次の点検の責任者は、この欄を見て、点検表の前回の直すことの欄を作り"
        "ます。期限を過ぎても直っていないものは、次の記録にも続けて書きます。"
    ),
    "sv-after-monitor": (
        "監視の再開の依頼は、サーバー室を出る前に行います。再開の返信が来るまで、作業者のうち"
        " 1 人はサーバー室の前に残り、監視の担当者からの問い合わせに答えます。再開した監視で"
        "警告が出たときは、点検で触れた設備を確かめ直します。"
    ),
    "sv-after-key": (
        "鍵を返すときは、作業者全員がサーバー室を出たことと、扉が閉まっていることを確かめます"
        "。警備室では、返した鍵と台帳の番号が合うかを確かめてもらいます。"
    ),
    "sv-after-report": (
        "提出は、情報システム部の共有のフォルダに記録のファイルを置き、情報システム部長にメー"
        "ルで知らせます。紙の記録は、読み取った写しのファイルを置き、紙は情報システム部の棚に"
        "保管します。提出の後に誤りが分かったときは、直した記録を同じフォルダに置き直します。"
    ),
    "sv-after-refill": (
        "補充した部品は、倉庫の決まった棚に置き、製造の年月の札を確かめてから在庫の表に書きま"
        "す。予備部品の数は、バッテリーが 4 個、空調のフィルターが 6 枚を下回らないようにしま"
        "す。補充の費用は情報システム部の予算から出します。"
    ),
}

# 各ページの終わりの「点検表の欄」の表（そのページの項目と、点検表に書くこと）。表は本文の
# 子 chunk を区切るので、ページごとの本文が別々の chunk になる（1 つの抜粋に章節の項目が
# そろわない）。
_INSPECTION_SHEET: dict[str, tuple[str, str]] = {
    "sv-prep-reserve": ("入室の予約", "予約の番号"),
    "sv-prep-staff": ("作業者", "作業者の氏名と所属"),
    "sv-prep-sheet": ("点検表", "前回に直すことになった箇所"),
    "sv-prep-monitor": ("監視の一時停止", "停止した項目と時刻"),
    "sv-prep-notice": ("部署への知らせ", "知らせた日"),
    "sv-prep-key": ("鍵", "受け取った時刻"),
    "sv-prep-parts": ("予備部品", "倉庫にある数"),
    "sv-prep-meter": ("測定器", "校正の期限"),
    "sv-prep-safety": ("安全具", "用意した数"),
    "sv-power-range": ("出力の電圧", "測った値（V）"),
    "sv-power-battery": ("バッテリー", "交換の時期の表示の有無"),
    "sv-air-range": ("室温", "測った値（度）"),
    "sv-air-filter": ("フィルター", "ほこりの有無"),
    "sv-fire-range": ("ボンベの圧力計", "針の位置"),
    "sv-fire-detector": ("煙の感知器", "表示の灯の色"),
    "sv-stop-leak": ("漏水", "見つけた場所と時刻"),
    "sv-stop-smell": ("におい", "気づいた場所と時刻"),
    "sv-stop-heat": ("室温の上昇", "測った値と時刻"),
    "sv-stop-alarm": ("警報", "鳴った時刻"),
    "sv-stop-battery": ("電源の停止", "気づいた時刻"),
    "sv-stop-alone": ("作業者の不足", "中止した時刻と理由"),
    "sv-record-name": ("日時と点検者", "入室・退室の時刻と氏名"),
    "sv-record-values": ("測った値", "設備ごとの値"),
    "sv-record-parts": ("交換した部品", "部品の名前・番号・数"),
    "sv-record-todo": ("直すこと", "担当する人と期限"),
    "sv-after-monitor": ("監視の再開", "再開した時刻"),
    "sv-after-key": ("鍵の返却", "返した時刻"),
    "sv-after-report": ("記録の提出", "提出した日"),
    "sv-after-refill": ("予備部品の補充", "補充を頼んだ日"),
}

# 点検表の欄の表の根拠（#1503。「章ごとに点検表の欄に書くことをすべて」の問）。表の「書くこと」
# の値のうち、資料の中で表にしか現れない値を、そのページの表を引用で取れたかの根拠にする（本文
# にも現れる値は、本文の chunk の引用でも一致するので使わない。p.7・p.9・p.11 の表は該当の値が
# 無い）。ID は ``sheet-`` + 項目の ID。
_INSPECTION_SHEET_EVIDENCE = (
    "sv-prep-staff",
    "sv-prep-monitor",
    "sv-prep-parts",
    "sv-power-range",
    "sv-air-range",
    "sv-stop-leak",
    "sv-record-name",
    "sv-after-monitor",
    "sv-after-report",
)


def _inspection_block(
    items: Sequence[tuple[str, str, str]], evidence: dict[str, str], *, page_break: bool
) -> str:
    paragraphs = []
    for index, (key, sentence, detail) in enumerate(items):
        evidence[key] = sentence
        if key in _INSPECTION_SHEET_EVIDENCE:
            evidence[f"sheet-{key}"] = _INSPECTION_SHEET[key][1]
        style = PAGE_BREAK if page_break and index == 0 else ""
        more = _INSPECTION_DETAILS.get(key, "")
        paragraphs.append(f"<p{style}>{html.escape(sentence)}。{html.escape(detail + more)}</p>")
    rows = "".join(
        "<tr><td>{}</td><td>{}</td></tr>".format(*map(html.escape, _INSPECTION_SHEET[key]))
        for key, _sentence, _detail in items
    )
    paragraphs.append(
        '<table border="1"><tr><th>点検表の欄</th><th>書くこと</th></tr>' + rows + "</table>"
    )
    return "\n".join(paragraphs)


def _inspection() -> Document:
    """サーバー室の定期点検要領（章節が複数ページにまたがる。#1438）。

    手順・一覧・条件の章節を改ページで 2〜3 ページに分け、章節を読み切らないと全部の項目を答え
    られない問（``section_read``）の資料にする。章（h2）は新しいページから始め、第 3 章は設備ごとの
    節（h3）を 1 ページずつに置く（節を 1 つ読んでも足りず、親の章を読む問）。
    """
    evidence: dict[str, str] = {}
    pages: dict[str, int] = {}
    page = 1
    parts = [
        "<!doctype html>",
        '<html lang="ja"><head><meta charset="utf-8">'
        "<title>サンプル社 サーバー室の定期点検要領</title>",
        f"<style>{_STYLE}</style></head>",
        "<body>",
        "<h1>サーバー室の定期点検要領</h1>",
        "<p>発行: 2026年4月1日　作成: 情シス　対象: サンプル社の本社のサーバー室（架空の会社。"
        "評価用の合成資料）</p>",
        "",
        "<h2>第 1 章 この要領の使い方</h2>",
        "<p>この要領は、本社のサーバー室の電源設備・空調設備・消火設備を毎月 1 回点検するときの"
        "準備・手順・中止の条件・記録・点検の後の作業を定めます。システムの変更の作業は、"
        "この要領の対象ではありません。</p>",
    ]

    def add_chapter(number: int, title: str, key: str) -> None:
        nonlocal page
        for index, items in enumerate(_INSPECTION_PAGES[key]):
            page += 1
            if index == 0:
                parts.extend(["", f"<h2{PAGE_BREAK}>第 {number} 章 {html.escape(title)}</h2>"])
            parts.append(_inspection_block(items, evidence, page_break=index > 0))
            pages.update(
                {
                    key: page
                    for item in items
                    for key in (item[0], f"sheet-{item[0]}")
                    if key in evidence
                }
            )

    add_chapter(2, "点検の前の準備", "prep")
    page += 1
    parts.extend(
        [
            "",
            f"<h2{PAGE_BREAK}>第 3 章 設備ごとの点検</h2>",
            "<p>設備ごとに次の節の値を確かめ、点検表に書きます。</p>",
        ]
    )
    for index, (number, title, key) in enumerate(
        (("3.1", "電源設備", "power"), ("3.2", "空調設備", "air"), ("3.3", "消火設備", "fire"))
    ):
        if index > 0:
            page += 1
        style = PAGE_BREAK if index > 0 else ""
        parts.append(f"<h3{style}>{number} {html.escape(title)}</h3>")
        [items] = _INSPECTION_PAGES[key]
        parts.append(_inspection_block(items, evidence, page_break=False))
        pages.update(
            {
                key: page
                for item in items
                for key in (item[0], f"sheet-{item[0]}")
                if key in evidence
            }
        )
    add_chapter(4, "点検を中止する条件", "stop")
    add_chapter(5, "点検の記録", "record")
    add_chapter(6, "点検の後の作業", "after")
    parts.append("</body></html>")
    content = "\n".join(parts) + "\n"
    return Document(INSPECTION, "server-room-inspection", content, evidence, pages)


def build_documents() -> list[Document]:
    """資料の原稿（評価セットが参照する順）。"""
    return [
        _approval(),
        _retention(),
        _incident(),
        _ledger(),
        _maintenance(
            file_name=MAINTENANCE,
            source_name="maintenance-plan",
            year="2026",
            prefix="mt",
            windows=MAINTENANCE_2026,
            common=MAINTENANCE_COMMON_2026,
            meta="発行: 2026年4月1日　作成: 情シス　対象: サンプル社のシステム（架空の会社。"
            "評価用の合成資料）",
        ),
        _organization(),
        _change(),
        _approval_2023(),
        _maintenance(
            file_name=MAINTENANCE_PREVIOUS,
            source_name="maintenance-plan-2025",
            year="2025",
            prefix="mt2025",
            windows=MAINTENANCE_2025,
            common=MAINTENANCE_COMMON_2025,
            meta="発行: 2025年4月1日　終了: 2026年3月31日（2026年度は「定期保守計画 2026年度」を"
            "見てください）　作成: 情シス　対象: サンプル社のシステム（架空の会社。評価用の"
            "合成資料）",
        ),
        _purchase(),
        _access(),
        _backup(),
        _guide(),
        _logistics_organization(),
        _logistics_ledger(),
        _inspection(),
    ]


# ---- 評価セット ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseSpec:
    """評価セットの 1 問（根拠は資料の根拠の ID で指す）。"""

    id: str
    split: str
    reasoning_type: str
    hops: int
    query: str
    keywords: tuple[str, ...]
    evidence: tuple[str, ...]


def _case(
    case_id: str,
    split: str,
    reasoning_type: str,
    hops: int,
    query: str,
    keywords: Sequence[str],
    evidence: Sequence[str],
) -> CaseSpec:
    return CaseSpec(case_id, split, reasoning_type, hops, query, tuple(keywords), tuple(evidence))


# #1335 の 33 問（ID・質問・期待する語は変えない。根拠の文は資料の原稿から引く）。
ORIGINAL_CASES: tuple[CaseSpec, ...] = (
    _case("sh-approval-record-retention", "dev", "single_hop", 1,
          "承認の記録は何年間保管しますか？", ["5年"], ["ap-record"]),
    _case("sh-confidential-retention", "dev", "single_hop", 1,
          "社外秘のデータは何年間保管しますか？", ["7年"], ["dr-shagaihi"]),
    _case("sh-severity-c-report", "holdout", "single_hop", 1,
          "重要度 C のシステムで障害が起きたら、いつまでにどこへ報告しますか？",
          ["翌営業日", "担当部署"], ["in-c"]),
    _case("sh-ledger-sys107", "holdout", "single_hop", 1,
          "システム ID が SYS-107 のシステムの略称と担当部署の略号を教えてください。",
          ["AMS", "情"], ["ledger-107"]),
    _case("tl-second-saturday-confidentiality", "dev", "table_lookup", 2,
          "定期保守計画で毎月第 2 土曜日に保守するシステムの機密区分は何ですか？",
          ["極秘"], ["mt-hrm", "ledger-103"]),
    _case("tl-wednesday-department", "dev", "table_lookup", 2,
          "毎週水曜日に定期保守をするシステムは、どの部署の略号で台帳に載っていますか？",
          ["総"], ["mt-doc", "ledger-105"]),
    _case("tl-severity-a-top-secret", "dev", "table_lookup", 1,
          "重要度が A で、機密区分が極秘のシステムをすべて挙げてください。",
          ["人事評価システム", "予算管理システム"], ["ledger-103", "ledger-108"]),
    _case("tl-sales-escalation-system", "holdout", "table_lookup", 2,
          "障害連絡規程で、営業本部長にも連絡するとされているシステムの正式名は何ですか？",
          ["受発注管理システム"], ["in-oms", "ledger-104"]),
    _case("tl-it-department-system", "holdout", "table_lookup", 2,
          "情報システム部が担当するシステムの正式名と機密区分を教えてください。",
          ["資産管理システム", "社外秘"], ["org-code-jo", "ledger-107"]),
    _case("br-expense-approver-deadline", "dev", "bridge", 4,
          "経費精算ポータルの変更の申請は、誰が承認し、受付から何営業日以内に承認されますか？",
          ["管理本部長", "5営業日"],
          ["ledger-101", "org-code-kei", "org-appr-kei", "ap-honbucho"]),
    _case("br-attendance-deadline", "dev", "bridge", 4,
          "勤怠管理システムの変更を申請したら、受付から何営業日以内に承認されますか？",
          ["3営業日"], ["ledger-102", "org-code-jin", "org-appr-jin", "ap-bucho"]),
    _case("br-order-approver-deadline", "holdout", "bridge", 4,
          "受発注管理システムの変更の承認者と、承認の期限を教えてください。",
          ["営業本部長", "5営業日"],
          ["ledger-104", "org-code-ei", "org-appr-ei", "ap-honbucho"]),
    _case("br-room-booking-department", "dev", "bridge", 2,
          "会議室予約システムを担当する部署の正式名は何ですか？",
          ["総務部"], ["ledger-106", "org-code-so"]),
    _case("br-hr-evaluation-retention", "dev", "bridge", 2,
          "人事評価システムのデータは何年間保管しますか？", ["10年"], ["ledger-103", "dr-gokuhi"]),
    _case("br-document-portal-retention", "holdout", "bridge", 2,
          "ドキュメントポータルのデータの保管期間は何年ですか？",
          ["3年"], ["ledger-105", "dr-shanai"]),
    _case("br-budget-incident-contact", "dev", "bridge", 4,
          "予算管理システムで障害が起きたら、何分以内に誰へ連絡しますか？",
          ["30分", "管理本部長"], ["ledger-108", "in-a", "org-code-kei", "org-appr-kei"]),
    _case("br-asset-recovery-target", "holdout", "bridge", 2,
          "資産管理システムで障害が起きたときの復旧の目標はどれくらいですか？",
          ["1営業日"], ["ledger-107", "in-recover-b"]),
    _case("br-hr-evaluation-change-window", "dev", "bridge", 3,
          "人事評価システムの本番の変更は、いつ作業できますか？",
          ["第2土曜"], ["ledger-103", "ch-window-a", "mt-hrm"]),
    _case("br-budget-change-window", "dev", "bridge", 4,
          "予算管理システムの本番の変更は、いつ作業できますか？",
          ["最終金曜"], ["ledger-108", "ch-window-a", "mt-common-ref", "mt-common"]),
    _case("br-order-change-window", "holdout", "bridge", 3,
          "受発注管理システムの変更作業ができる時間帯はいつですか？",
          ["第4日曜"], ["ledger-104", "ch-window-a", "mt-oms"]),
    _case("br-room-booking-proxy", "holdout", "bridge", 3,
          "会議室予約システムの変更の承認者が 3 営業日以上不在のとき、だれが代理で承認しますか？",
          ["管理本部長"], ["ledger-106", "org-code-so", "org-proxy"]),
    _case("br-expense-approver-proxy", "dev", "bridge", 5,
          "経費精算ポータルの変更を承認する人が不在のときは、だれが代理で承認しますか？",
          ["担当役員"],
          ["ledger-101", "org-code-kei", "org-appr-kei", "org-proxy-ref", "org-proxy-exec"]),
    _case("ir-overdue-approval", "dev", "intra_document_reference", 2,
          "申請が承認の期限までに承認されないとき、申請者は何をすればよいですか？",
          ["催促"], ["ap-overdue-ref", "ap-overdue"]),
    _case("ir-room-booking-maintenance", "dev", "intra_document_reference", 2,
          "会議室予約システムの定期保守はいつ行いますか？",
          ["最終金曜"], ["mt-common-ref", "mt-common"]),
    _case("ir-change-application-items", "dev", "intra_document_reference", 2,
          "本番環境の設定を変えるときの申請には、何を書けばよいですか？",
          ["切り戻し"], ["ch-items-ref", "ch-item-rollback"]),
    _case("ir-incident-phone-unreachable", "dev", "intra_document_reference", 2,
          "障害の最初の連絡で、電話がつながらないときはどうしますか？",
          ["#incident"], ["in-means-ref", "in-means"]),
    _case("ir-emergency-change", "holdout", "intra_document_reference", 2,
          "緊急の変更を承認の前に作業したときは、そのあと何をしますか？",
          ["24時間", "事後"], ["ap-emergency-ref", "ap-emergency"]),
    _case("ir-expired-data-erasure", "holdout", "intra_document_reference", 2,
          "保管期間を過ぎたデータは、どのように消せばよいですか？",
          ["承認", "総務部"], ["dr-erase-ref", "dr-erase", "dr-erase-report"]),
    _case("cmp-expense-vs-attendance-deadline", "dev", "comparison", 4,
          "経費精算ポータルと勤怠管理システムでは、変更の承認の期限はどちらが長いですか？",
          ["経費精算ポータル"],
          ["ledger-101", "ledger-102", "org-code-kei", "org-code-jin", "org-appr-kei",
           "org-appr-jin", "ap-honbucho", "ap-bucho"]),
    _case("cmp-hr-vs-attendance-retention", "dev", "comparison", 2,
          "人事評価システムと勤怠管理システムでは、どちらのデータを長く保管しますか？",
          ["人事評価システム", "10年"], ["ledger-103", "ledger-102", "dr-gokuhi", "dr-shagaihi"]),
    _case("cmp-hrm-vs-oms-maintenance", "dev", "comparison", 1,
          "HRM と OMS では、どちらの定期保守が月の早い時期にありますか？",
          ["HRM"], ["mt-hrm", "mt-oms"]),
    _case("cmp-hr-vs-asset-first-contact", "holdout", "comparison", 2,
          "人事評価システムと資産管理システムでは、障害のときの最初の連絡の期限はどちらが短いですか？",
          ["人事評価システム", "30分"], ["ledger-103", "ledger-107", "in-a", "in-b"]),
    _case("cmp-document-vs-room-recovery", "holdout", "comparison", 2,
          "ドキュメントポータルと会議室予約システムでは、障害からの復旧の目標はどちらが短いですか？",
          ["ドキュメントポータル", "1営業日"],
          ["ledger-105", "ledger-106", "in-recover-b", "in-recover-c"]),
)  # fmt: skip


# ---- #1352 の問（実体のデータから正解と根拠を組み立てる） ------------------------------------


def _system(number: int) -> System:
    return SYSTEM_BY_NUMBER[number]


def _dept(system: System) -> Department:
    return DEPARTMENT_BY_CODE[system.dept]


def _approval_chain(system: System) -> tuple[list[str], str, int]:
    """システム → 担当部署 → 承認者 → 期限 の根拠、承認者、期限（営業日）。"""
    dept = _dept(system)
    role = approver_role(dept.approver)
    evidence = [
        f"ledger-{system.number}",
        f"org-code-{dept.slug}",
        f"org-appr-{dept.slug}",
        ROLE_EVIDENCE[role],
    ]
    return evidence, dept.approver, ROLE_DEADLINE_DAYS[role]


def _approver_deadline_case(case_id: str, split: str, number: int, query: str) -> CaseSpec:
    evidence, approver, days = _approval_chain(_system(number))
    return _case(case_id, split, "bridge", 4, query, [approver, f"{days}営業日"], evidence)


def _window_chain(system: System) -> tuple[list[str], str]:
    """重要度 A のシステム → 定期保守の時間帯（個別の枠か、共通の枠）の根拠と期待する語。"""
    if system.severity != "A":
        raise ValueError(f"{system.sys_id} は重要度 A ではありません。")
    for item in MAINTENANCE_2026:
        if item.system == system.number:
            return [f"ledger-{system.number}", "ch-window-a", f"mt-{item.slug}"], window_keyword(
                item.window
            )
    return [
        f"ledger-{system.number}",
        "ch-window-a",
        "mt-common-ref",
        "mt-common",
    ], window_keyword(MAINTENANCE_COMMON_2026)


def _change_window_case(case_id: str, split: str, number: int, query: str) -> CaseSpec:
    evidence, keyword = _window_chain(_system(number))
    return _case(case_id, split, "bridge", len(evidence), query, [keyword], evidence)


def _retention_case(case_id: str, split: str, number: int, query: str) -> CaseSpec:
    system = _system(number)
    return _case(
        case_id,
        split,
        "bridge",
        2,
        query,
        [f"{RETENTION_YEARS[system.confidentiality]}年"],
        [f"ledger-{number}", RETENTION_EVIDENCE[system.confidentiality]],
    )


def _backup_case(case_id: str, split: str, number: int, query: str) -> CaseSpec:
    system = _system(number)
    return _case(
        case_id,
        split,
        "bridge",
        2,
        query,
        [BACKUP_RULES[system.severity][1]],
        [f"ledger-{number}", f"bk-{system.severity.lower()}"],
    )


def _proxy_case(case_id: str, split: str, number: int, query: str) -> CaseSpec:
    """承認者が部長の部署 → 所属の本部の本部長、承認者が担当役員の部署 → 代表取締役。"""
    system = _system(number)
    dept = _dept(system)
    role = approver_role(dept.approver)
    ledger = f"ledger-{number}"
    if role == "部長" and dept.division:
        return _case(
            case_id,
            split,
            "bridge",
            3,
            query,
            [f"{dept.division}長"],
            [ledger, f"org-code-{dept.slug}", "org-proxy"],
        )
    if role == "担当役員":
        evidence = [
            ledger,
            f"org-code-{dept.slug}",
            f"org-appr-{dept.slug}",
            "org-proxy-ref",
            "org-proxy-direct",
        ]
        return _case(case_id, split, "bridge", 5, query, ["代表取締役"], evidence)
    raise ValueError(f"{system.sys_id} の代理の問は作れません。")


def _incident_contact_case(case_id: str, split: str, number: int, query: str) -> CaseSpec:
    system = _system(number)
    dept = _dept(system)
    key, _text, keyword = FIRST_CONTACT[system.severity]
    evidence = [f"ledger-{number}", key, f"org-code-{dept.slug}", f"org-appr-{dept.slug}"]
    return _case(case_id, split, "bridge", 4, query, [keyword, dept.approver], evidence)


def _department_case(case_id: str, split: str, number: int, query: str) -> CaseSpec:
    system = _system(number)
    dept = _dept(system)
    return _case(
        case_id,
        split,
        "bridge",
        2,
        query,
        [dept.name],
        [f"ledger-{number}", f"org-code-{dept.slug}"],
    )


def _link_department_case(case_id: str, split: str, target: int, query: str) -> CaseSpec:
    """運用要領の連携元 → 台帳の担当部署 → 部署の正式名。"""
    source = _system(LINKS[target][0])
    dept = _dept(source)
    evidence = [f"guide-{target}-link", f"ledger-{source.number}", f"org-code-{dept.slug}"]
    return _case(case_id, split, "bridge", 3, query, [dept.name], evidence)


def _access_approver_case(case_id: str, split: str, number: int, query: str) -> CaseSpec:
    """機密区分 → 権限の承認者（極秘は担当部署の承認者と情セキ部長 = 情報セキュリティ部長）。"""
    system = _system(number)
    dept = _dept(system)
    evidence = [
        f"ledger-{number}",
        ACCESS_EVIDENCE[system.confidentiality],
        f"org-code-{dept.slug}",
        f"org-appr-{dept.slug}",
    ]
    keywords = [dept.approver]
    if system.confidentiality == "極秘":
        security = next(item for item in DEPARTMENTS if item.nickname == "情セキ")
        evidence.append(f"org-nick-{security.slug}")
        keywords.append(security.approver)
    elif system.confidentiality != "社外秘":
        raise ValueError(f"{system.sys_id} の権限の承認者は担当部署で決まりません。")
    return _case(case_id, split, "bridge", len(evidence), query, keywords, evidence)


def _compare_deadline_case(case_id: str, split: str, numbers: tuple[int, int]) -> CaseSpec:
    chains = [_approval_chain(_system(number)) for number in numbers]
    if chains[0][2] == chains[1][2]:
        raise ValueError(f"{case_id}: 期限が同じです。")
    winner = _system(numbers[0] if chains[0][2] > chains[1][2] else numbers[1])
    evidence = [chains[0][0][0], chains[1][0][0]]
    for position in (1, 2, 3):
        for chain in chains:
            if chain[0][position] not in evidence:
                evidence.append(chain[0][position])
    names = "と".join(_system(number).name for number in numbers)
    query = f"{names}では、変更の承認の期限はどちらが長いですか？"
    keywords = [winner.name, f"{max(chain[2] for chain in chains)}営業日"]
    return _case(case_id, split, "comparison", 4, query, keywords, evidence)


def _compare_retention_case(case_id: str, split: str, numbers: tuple[int, int]) -> CaseSpec:
    systems = [_system(number) for number in numbers]
    years = [RETENTION_YEARS[item.confidentiality] for item in systems]
    if years[0] == years[1]:
        raise ValueError(f"{case_id}: 保管の年数が同じです。")
    winner = systems[0] if years[0] > years[1] else systems[1]
    query = f"{systems[0].name}と{systems[1].name}では、どちらのデータを長く保管しますか？"
    evidence = [f"ledger-{item.number}" for item in systems] + [
        RETENTION_EVIDENCE[item.confidentiality] for item in systems
    ]
    keywords = [winner.name, f"{max(years)}年"]
    return _case(case_id, split, "comparison", 2, query, keywords, evidence)


def _systems_with_alias(alias: str) -> list[System]:
    return [item for item in SYSTEMS if alias in item.alias.split("、")]


def _compare_alias_recovery_case(case_id: str, split: str, alias: str) -> CaseSpec:
    """略称が衝突する 2 つのシステムの復旧の目標を比べる。"""
    systems = _systems_with_alias(alias)
    order = "ABC"
    if len(systems) != 2 or systems[0].severity == systems[1].severity:
        raise ValueError(f"{case_id}: 略称 {alias} の 2 つのシステムを比べられません。")
    winner = min(systems, key=lambda item: order.index(item.severity))
    query = f"略称が {alias} のシステム 2 つでは、障害からの復旧の目標はどちらが短いですか？"
    evidence = [f"ledger-{item.number}" for item in systems] + [
        RECOVERY_EVIDENCE[item.severity] for item in systems
    ]
    keyword = normalize(RECOVERY[winner.severity]).removesuffix("以内")
    return _case(case_id, split, "comparison", 2, query, [winner.name, keyword], evidence)


def _compare_alias_severity_case(case_id: str, split: str, alias: str) -> CaseSpec:
    systems = _systems_with_alias(alias)
    order = "ABC"
    if len(systems) != 2 or systems[0].severity == systems[1].severity:
        raise ValueError(f"{case_id}: 略称 {alias} の 2 つのシステムを比べられません。")
    winner = min(systems, key=lambda item: order.index(item.severity))
    query = f"略称が {alias} のシステムは 2 つあります。重要度が高いのはどちらですか？"
    # 2 つの行と、どちらが高いかを決める重要度の順位（台帳の前書き。#1406）。
    evidence = [f"ledger-{item.number}" for item in systems] + ["ledger-severity-order"]
    return _case(case_id, split, "comparison", 1, query, [winner.name], evidence)


def _window_lookup_case(case_id: str, split: str, window_prefix: str, query: str) -> CaseSpec:
    """2026 年度の保守枠の曜日 → 台帳の行 → 担当部署の正式名（正式名も答えに入れる）。"""
    matches = [item for item in MAINTENANCE_2026 if item.window.startswith(window_prefix)]
    if len(matches) != 1:
        raise ValueError(f"{case_id}: 保守枠「{window_prefix}」のシステムが 1 つではありません。")
    item = matches[0]
    system = _system(item.system)
    dept = _dept(system)
    evidence = [f"mt-{item.slug}", f"ledger-{system.number}", f"org-code-{dept.slug}"]
    return _case(case_id, split, "table_lookup", 3, query, [system.name, dept.name], evidence)


def _escalation_case(case_id: str, split: str, contact: str) -> CaseSpec:
    matches = [item for item in ESCALATIONS if item[3] == contact]
    if len(matches) != 1:
        raise ValueError(f"{case_id}: {contact}にも連絡するシステムが 1 つではありません。")
    key, number, _reason, _contact = matches[0]
    query = f"障害連絡規程で、{contact}にも連絡するとされているシステムの正式名は何ですか？"
    return _case(
        case_id, split, "table_lookup", 2, query, [_system(number).name], [key, f"ledger-{number}"]
    )


def _department_filter_case(case_id: str, split: str, code: str, severity: str) -> CaseSpec:
    dept = DEPARTMENT_BY_CODE[code]
    systems = [item for item in SYSTEMS if item.dept == code and item.severity == severity]
    if not systems:
        raise ValueError(f"{case_id}: 該当するシステムがありません。")
    query = (
        f"{dept.name}が担当するシステムのうち、重要度が {severity} のものをすべて挙げてください。"
    )
    evidence = [f"org-code-{dept.slug}"] + [f"ledger-{item.number}" for item in systems]
    return _case(
        case_id, split, "table_lookup", 2, query, [item.name for item in systems], evidence
    )


def _logistics_alias_case(case_id: str, split: str, alias: str) -> CaseSpec:
    matches = [item for item in LOGISTICS_SYSTEMS if alias in item.alias.split("、")]
    if len(matches) != 1:
        raise ValueError(f"{case_id}: サンプル物流社の略称 {alias} が 1 つではありません。")
    system = matches[0]
    query = f"サンプル物流社のシステム台帳で、略称が {alias} のシステムの正式名は何ですか？"
    return _case(
        case_id, split, "table_lookup", 1, query, [system.name], [f"lg-ledger-{system.number}"]
    )


def _logistics_approver_case(case_id: str, split: str, number: int) -> CaseSpec:
    system = next(item for item in LOGISTICS_SYSTEMS if item.number == number)
    dept = next(item for item in LOGISTICS_DEPARTMENTS if item.code == system.dept)
    query = f"サンプル物流社の{system.name}の変更は、だれが承認しますか？"
    evidence = [f"lg-ledger-{number}", f"lg-org-code-{dept.slug}", f"lg-org-appr-{dept.slug}"]
    return _case(case_id, split, "bridge", 3, query, [dept.approver], evidence)


def _version_maintenance_case(case_id: str, split: str, slug: str) -> CaseSpec:
    now = next(item for item in MAINTENANCE_2026 if item.slug == slug)
    old = next(item for item in MAINTENANCE_2025 if item.slug == slug)
    query = f"定期保守計画の 2025 年度と 2026 年度では、{now.label} の保守枠はどう変わりましたか？"
    keywords = [window_keyword(old.window), window_keyword(now.window)]
    return _case(case_id, split, "comparison", 1, query, keywords, [f"mt-{slug}", f"mt2025-{slug}"])


def _new_cases() -> list[CaseSpec]:
    """#1352 で足した問（名前の似たシステム・部署、略称の衝突、表記ゆれ、旧版・別の会社）。"""
    return [
        # -- dev ---------------------------------------------------------------------------
        _approver_deadline_case(
            "br-mes-approver-deadline",
            "dev",
            124,
            "製造実行システムの変更の申請は、誰が承認し、受付から何営業日以内に承認されますか？",
        ),
        _approver_deadline_case(
            "br-audit-record-approver-deadline",
            "dev",
            137,
            "監査記録システムの変更の承認者と、承認の期限を教えてください。",
        ),
        _department_case(
            "br-supplier-portal-department",
            "dev",
            127,
            "Supplier Portal を担当する部署の正式名は何ですか？",
        ),
        _change_window_case(
            "br-wms-change-window",
            "dev",
            121,
            "倉庫管理システムの本番の変更は、いつ作業できますか？",
        ),
        _change_window_case(
            "br-purchase-pms-change-window",
            "dev",
            119,
            "購買管理システムの本番の変更は、いつ作業できますか？",
        ),
        _retention_case(
            "br-crm-retention", "dev", 129, "問い合わせ管理システムのデータは何年間保管しますか？"
        ),
        _incident_contact_case(
            "br-idm-incident-contact",
            "dev",
            139,
            "ＩＤ管理システムで障害が起きたら、何分以内に誰へ連絡しますか？",
        ),
        _proxy_case(
            "br-audit-record-proxy",
            "dev",
            137,
            "監査記録システムの変更を承認する人が不在のときは、だれが代理で承認しますか？",
        ),
        _link_department_case(
            "br-wms-link-department",
            "dev",
            121,
            "倉庫管理システムに受注データを送る連携元のシステムを担当する部署の正式名は何ですか？",
        ),
        _backup_case(
            "br-siem-backup", "dev", 140, "ログ監視システムのバックアップは、1 日に何回取りますか？"
        ),
        _access_approver_case(
            "br-contract-access-approver",
            "dev",
            131,
            "契約書管理システムの利用権限の申請は、だれが承認しますか？",
        ),
        _compare_alias_severity_case("cmp-pms-severity", "dev", "PMS"),
        _compare_deadline_case("cmp-mes-vs-qms-deadline", "dev", (124, 125)),
        _case(
            "cmp-approval-deadline-versions",
            "dev",
            "comparison",
            1,
            "今の承認規程と 2023 年度版の承認規程では、本部長が承認する申請の期限は"
            "どう変わりましたか？",
            [f"{ROLE_DEADLINE_DAYS['本部長']}営業日", "7営業日"],
            ["ap-honbucho", "ap2023-honbucho"],
        ),
        _window_lookup_case(
            "tl-first-tuesday-department",
            "dev",
            "毎月第 1 火曜日",
            "2026 年度の定期保守計画で毎月第 1 火曜日に保守するシステムの正式名と、担当する部署の"
            "正式名を教えてください。",
        ),
        _logistics_alias_case("tl-logistics-oms", "dev", "OMS"),
        _escalation_case("tl-production-escalation-system", "dev", "生産本部長"),
        _case(
            "ir-backup-restore",
            "dev",
            "intra_document_reference",
            2,
            "システムのデータをバックアップから戻したいときは、どうすればよいですか？",
            ["情シス"],
            ["bk-restore-ref", "bk-restore"],
        ),
        _case(
            "ir-access-review",
            "dev",
            "intra_document_reference",
            2,
            "極秘のシステムの利用権限は、どれくらいの間隔で見直しますか？",
            ["半年"],
            ["ac-review-ref", "ac-review-gokuhi"],
        ),
        _case(
            "sh-purchase-large-approver",
            "dev",
            "single_hop",
            1,
            "金額が 500 万円以上の購買の申請は、だれが承認しますか？",
            ["担当役員"],
            ["pu-exec"],
        ),
        # -- holdout -----------------------------------------------------------------------
        _approver_deadline_case(
            "br-gl-approver-deadline",
            "holdout",
            157,
            "会計システムの変更の申請は、誰が承認し、受付から何営業日以内に承認されますか？",
        ),
        _approver_deadline_case(
            "br-payroll-approver-deadline",
            "holdout",
            111,
            "人事給与システムの変更の承認者と、承認の期限を教えてください。",
        ),
        _change_window_case(
            "br-customer-portal-change-window",
            "holdout",
            128,
            "Customer Portal の本番の変更は、いつ作業できますか？",
        ),
        _change_window_case(
            "br-sales-change-window",
            "holdout",
            153,
            "販売管理システムの変更作業ができる時間帯はいつですか？",
        ),
        _retention_case(
            "br-delivery-retention",
            "holdout",
            122,
            "配送管理システムのデータの保管期間は何年ですか？",
        ),
        _backup_case(
            "br-shipping-backup",
            "holdout",
            160,
            "入出荷管理システムのバックアップは、どれくらいの頻度で取りますか？",
        ),
        _proxy_case(
            "br-quality-record-proxy",
            "holdout",
            125,
            "品質記録システムの変更の承認者が 3 営業日以上不在のとき、だれが代理で承認しますか？",
        ),
        _link_department_case(
            "br-mes-link-department",
            "holdout",
            124,
            "製造実行システムに生産計画を送る連携元のシステムを担当する部署の正式名は何ですか？",
        ),
        _logistics_approver_case("br-logistics-oms-approver", "holdout", 201),
        _compare_retention_case("cmp-crm-vs-faq-retention", "holdout", (129, 172)),
        _compare_alias_recovery_case("cmp-tms-recovery", "holdout", "TMS"),
        _version_maintenance_case("cmp-hrm-maintenance-versions", "holdout", "hrm"),
        _department_filter_case("tl-security-severity-a", "holdout", "安", "A"),
        _window_lookup_case(
            "tl-third-sunday-system",
            "holdout",
            "毎月第 3 日曜日",
            "2026 年度の定期保守計画で毎月第 3 日曜日に保守するシステムの正式名と、担当する部署の"
            "正式名を教えてください。",
        ),
        _case(
            "ir-purchase-overdue",
            "holdout",
            "intra_document_reference",
            2,
            "購買の申請が期限までに承認されないときは、どうなりますか？",
            ["購買部"],
            ["pu-overdue-ref", "pu-overdue"],
        ),
        _case(
            "sh-backup-c-frequency",
            "holdout",
            "single_hop",
            1,
            "重要度 C のシステムのバックアップは、どれくらいの頻度で取りますか？",
            [BACKUP_RULES["C"][1]],
            ["bk-c"],
        ),
    ]


def _section_cases() -> list[CaseSpec]:
    """#1438: 複数ページの章節を読み切らないと答えられない問（章節の読み切り）。

    必要な根拠は同じ章節の別々のページにあり、検索で当たった 1 つの chunk（抜粋）では全部の項目が
    そろわない。``sr-inspection-equipment-values`` は第 3 章の 3 つの節（1 ページずつ）にまたがり、
    節を 1 つ読んでも足りない（親の章を読む）。
    """
    return [
        _case(
            "sr-inspection-preparation",
            "dev",
            "section_read",
            2,
            "サーバー室の定期点検の前に行う準備をすべて挙げてください。",
            ["入室の予約", "監視の一時停止", "予備部品"],
            ["sv-prep-reserve", "sv-prep-monitor", "sv-prep-parts"],
        ),
        _case(
            "sr-inspection-equipment-values",
            "dev",
            "section_read",
            3,
            "サーバー室の定期点検で、設備ごとに確かめる値の範囲をすべて教えてください。",
            ["210", "27", "緑の範囲"],
            ["sv-power-range", "sv-air-range", "sv-fire-range"],
        ),
        _case(
            "sr-inspection-record-items",
            "dev",
            "section_read",
            2,
            "サーバー室の定期点検の記録には、何を書きますか？すべて挙げてください。",
            ["氏名", "交換した部品"],
            ["sv-record-name", "sv-record-parts"],
        ),
        _case(
            "sr-inspection-stop-conditions",
            "holdout",
            "section_read",
            2,
            "サーバー室の定期点検を途中で中止しなければならないのは、どのようなときですか？"
            "すべて挙げてください。",
            ["漏水", "警報"],
            ["sv-stop-leak", "sv-stop-alarm"],
        ),
        _case(
            "sr-inspection-after-tasks",
            "holdout",
            "section_read",
            2,
            "サーバー室の定期点検が終わった後に行う作業をすべて挙げてください。",
            ["監視の再開", "補充"],
            ["sv-after-monitor", "sv-after-refill"],
        ),
        # #1503: 件数を示した網羅の問。第 4 章の 2 ページに 3 つずつある中止の条件を 6 つそろえる。
        _case(
            "sr-inspection-stop-six",
            "dev",
            "section_read",
            2,
            "サーバー室の定期点検要領の第 4 章には、点検を中止する条件が 6 つ書かれています。"
            "6 つすべてと、それぞれ点検表に書くことを挙げてください。",
            ["漏水", "におい", "32 度", "警報", "バッテリー", "1 人"],
            [
                "sv-stop-leak",
                "sv-stop-smell",
                "sv-stop-heat",
                "sv-stop-alarm",
                "sv-stop-battery",
                "sv-stop-alone",
            ],
        ),
    ]


def _document_read_cases() -> list[CaseSpec]:
    """#1503: 文書全体（複数の章）に散らばる記載を、件数・章の範囲を示して網羅させる問。

    種類は ``document_read``。検索の上位の数件では項目がそろわず、CRAG の評価器も見えている数件で
    「足りる」と判定しやすい。件数・章の範囲の欠けをプログラムで数え、章節の拡張で文書全体へ広げる
    と取れる問。根拠は 2 つ以上の章にまたがる。
    """
    return [
        _case(
            "dr-inspection-business-day-five",
            "dev",
            "document_read",
            2,
            "サーバー室の定期点検要領の全体を通して、「営業日」で期限が決められている作業を "
            "5 つすべて挙げてください。",
            ["5 営業日", "3 営業日", "10 営業日"],
            [
                "sv-prep-reserve",
                "sv-prep-notice",
                "sv-prep-parts",
                "sv-after-report",
                "sv-after-refill",
            ],
        ),
        _case(
            "dr-inspection-sheet-columns",
            "holdout",
            "document_read",
            5,
            "サーバー室の定期点検要領の第 2 章から第 6 章までの各章で、点検表の欄に書くことを"
            "章ごとにすべて挙げてください。",
            [
                "予約の番号",
                "測った値",
                "見つけた場所と時刻",
                "入室・退室の時刻と氏名",
                "提出した日",
            ],
            [f"sheet-{key}" for key in _INSPECTION_SHEET_EVIDENCE],
        ),
    ]


def _check_section_cases(specs: Sequence[CaseSpec], documents: Sequence[Document]) -> None:
    """章節の読み切りの問は、根拠が同じ資料の 2 ページ以上にまたがる（#1438）。"""
    pages = {key: page for document in documents for key, page in document.pages.items()}
    for spec in specs:
        if spec.reasoning_type != "section_read":
            continue
        placed = [pages.get(key) for key in spec.evidence]
        if None in placed or len(set(placed)) < 2:
            raise ValueError(f"{spec.id}: 根拠が章節の 2 ページ以上にまたがっていません: {placed}")


def _check_document_read_cases(specs: Sequence[CaseSpec], documents: Sequence[Document]) -> None:
    """文書全体の網羅の問は、根拠が同じ資料の 2 つ以上の章にまたがる（#1503）。"""
    [inspection] = [document for document in documents if document.file_name == INSPECTION]
    text = _document_text(inspection)
    for spec in specs:
        if spec.reasoning_type != "document_read":
            continue
        pages = [inspection.pages.get(key) for key in spec.evidence]
        if None in pages:
            raise ValueError(f"{spec.id}: 根拠のページがありません: {spec.evidence}")
        chapters = {_chapter_of_page(inspection, page) for page in pages if page is not None}
        if len(chapters) < 2:
            raise ValueError(f"{spec.id}: 根拠が 2 つ以上の章にまたがっていません: {pages}")
        for key in spec.evidence:
            value = normalize(inspection.evidence[key])
            if key.startswith("sheet-") and normalize(text).count(value) != 1:
                raise ValueError(f"{spec.id}: 表の根拠の値が表の外にも現れます: {key}")


def _chapter_of_page(document: Document, page: int) -> int:
    """点検要領のページの章の番号（そのページに置いた項目の章）。"""
    chapters = {"prep": 2, "power": 3, "air": 3, "fire": 3, "stop": 4, "record": 5, "after": 6}
    for key, groups in _INSPECTION_PAGES.items():
        if any(document.pages.get(item[0]) == page for group in groups for item in group):
            return chapters[key]
    raise ValueError(f"{document.file_name}: p.{page} に項目がありません。")


def _check_constraints() -> None:
    """資料を増やしても #1335 の問の正解が変わらないことと、データの形を確かめる。"""
    names = [item.name for item in (*SYSTEMS, *LOGISTICS_SYSTEMS)]
    if len(names) != len(set(names)):
        raise ValueError("システムの正式名が重複しています。")
    if len({item.code for item in DEPARTMENTS}) != len(DEPARTMENTS):
        raise ValueError("部署の略号が重複しています。")
    for item in SYSTEMS:
        if item.dept not in DEPARTMENT_BY_CODE:
            raise ValueError(f"{item.sys_id} の担当部署の略号がありません。")
    # 重要度 A かつ極秘のシステム（両社）は SYS-103・108 だけ（tl-severity-a-top-secret）。
    top = [
        s.sys_id
        for s in (*SYSTEMS, *LOGISTICS_SYSTEMS)
        if (s.severity, s.confidentiality) == ("A", "極秘")
    ]
    if top != ["SYS-103", "SYS-108"]:
        raise ValueError(f"重要度 A かつ極秘のシステムが変わりました: {top}")
    # 情報システム部が担当するシステムは SYS-107 だけ（tl-it-department-system）。
    if [s.number for s in SYSTEMS if s.dept == "情"] != [107]:
        raise ValueError("情報システム部が担当するシステムが変わりました。")
    # 第 2 土曜日・毎週水曜日の保守枠は 1 つずつ（tl-second-saturday-* / tl-wednesday-*）、
    # 会議室予約・予算管理は共通の保守枠（ir-room-booking-maintenance / br-budget-change-window）。
    for plan in (MAINTENANCE_2026, MAINTENANCE_2025):
        for prefix in ("毎月第 2 土曜日", "毎週水曜日"):
            matches = [item.system for item in plan if item.window.startswith(prefix)]
            if matches not in ([], [103], [105]) or (plan is MAINTENANCE_2025 and matches):
                raise ValueError(f"保守枠「{prefix}」のシステムが変わりました: {matches}")
        if {106, 108} & {item.system for item in plan}:
            raise ValueError("会議室予約・予算管理に個別の保守枠を付けないでください。")
    # HRM の保守は今も旧版も OMS より月の早い時期（cmp-hrm-vs-oms-maintenance）。
    for plan in (MAINTENANCE_2026, MAINTENANCE_2025):
        windows = {item.slug: window_keyword(item.window) for item in plan}
        if not windows["hrm"] < windows["oms"]:
            raise ValueError("HRM と OMS の保守の順が変わりました。")
    for window in MAINTENANCE_2026:
        if _system(window.system).severity != "A" and window.slug != "doc":
            raise ValueError(f"{window.label} は重要度 A ではありません。")


def _case_payload(spec: CaseSpec, evidence: Mapping[str, tuple[str, str]]) -> dict[str, Any]:
    missing = [key for key in spec.evidence if key not in evidence]
    if missing:
        raise ValueError(f"{spec.id}: 根拠がありません: {missing}")
    documents: list[str] = []
    for key in spec.evidence:
        document = f"file:{evidence[key][0]}"
        if document not in documents:
            documents.append(document)
    payload: dict[str, Any] = {
        "id": spec.id,
        "category": "document_answerable",
        "split": spec.split,
        "reasoning_type": spec.reasoning_type,
        "hops": spec.hops,
        "query": spec.query,
        "relevant_document_ids": documents,
        "required_answer_points": list(spec.keywords),
        "expected_outcomes": list(EXPECTED_OUTCOMES),
        "required_evidence": [
            {"id": key, "document_id": f"file:{evidence[key][0]}", "text": evidence[key][1]}
            for key in spec.evidence
        ],
    }
    superseded = {f"file:{old}" for old, _ in DOCUMENT_VERSIONS}
    if superseded & set(documents):
        # 旧版の文を根拠にする問（版の比較）は、利用者が旧版を尋ねるときと同じく旧版も検索する。
        payload["include_superseded"] = True
    return payload


def _document_text(document: Document) -> str:
    if document.file_name.endswith(".xlsx"):
        spec = json.loads(document.content)
        return "\n".join(
            "\t".join(str(value) for value in row)
            for sheet in spec["sheets"]
            for row in [[line] for line in sheet["preamble"]] + [sheet["header"]] + sheet["rows"]
        )
    return html.unescape(re.sub(r"<[^>]+>", "", document.content))


def evidence_registry(documents: Sequence[Document]) -> dict[str, tuple[str, str]]:
    """根拠の ID → （資料のファイル名・文）。文が資料の原稿に無ければ失敗する。"""
    registry: dict[str, tuple[str, str]] = {}
    for document in documents:
        text = normalize(_document_text(document))
        for key, sentence in document.evidence.items():
            if key in registry:
                raise ValueError(f"根拠の ID が重複しています: {key}")
            if normalize(sentence) not in text:
                raise ValueError(f"{document.file_name}: 根拠の文がありません: {sentence}")
            registry[key] = (document.file_name, sentence)
    return registry


def build_golden_set(documents: Sequence[Document]) -> dict[str, Any]:
    _check_constraints()
    registry = evidence_registry(documents)
    specs = [*ORIGINAL_CASES, *_new_cases(), *_section_cases(), *_document_read_cases()]
    if len({spec.id for spec in specs}) != len(specs):
        raise ValueError("ケースの ID が重複しています。")
    _check_section_cases(specs, documents)
    _check_document_read_cases(specs, documents)
    cases = [_case_payload(spec, registry) for spec in specs]
    referenced = {name for case in cases for name in case["relevant_document_ids"]}
    unused = [doc.file_name for doc in documents if f"file:{doc.file_name}" not in referenced]
    if unused:
        # 評価の CLI は評価セットが参照するファイルだけを取り込む。紛らわしい資料も 1 問は参照する。
        raise ValueError(f"どの問も参照しない資料があります: {unused}")
    return {
        "top_k": TOP_K,
        "document_versions": [
            {"document_id": f"file:{old}", "superseded_by": f"file:{new}"}
            for old, new in DOCUMENT_VERSIONS
        ],
        "cases": cases,
    }


def source_path(corpus_dir: Path, document: Document) -> Path:
    suffix = ".workbook.json" if document.file_name.endswith(".xlsx") else ".html"
    return corpus_dir / SOURCES_DIRNAME / f"{document.source_name}{suffix}"


def write_sources(corpus_dir: Path) -> list[Path]:
    """原稿（sources/）と評価セット（multi-hop.json）を書き、書いたファイルを返す。"""
    documents = build_documents()
    golden_set = build_golden_set(documents)
    (corpus_dir / SOURCES_DIRNAME).mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for document in documents:
        path = source_path(corpus_dir, document)
        path.write_text(document.content, encoding="utf-8")
        written.append(path)
    golden = corpus_dir / GOLDEN_SET_NAME
    golden.write_text(json.dumps(golden_set, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written.append(golden)
    return written


def estimate_chunks(documents: Sequence[Document]) -> dict[str, int]:
    """資料ごとの子 chunk の数の見積もり（README と PR の根拠）。

    #1335 の評価（Docling の解析・親子階層の分割）では、PDF は前書きの段落と章（h2）ごとに 1 つ
    （章の本文は子の目標 1000 文字より短い）、Excel は表の行ごとに 1 つ（#1349 の 1 行 = 1 chunk）と
    前書きで 1 つになる。改ページで分けた資料（#1438）は、節（h3）と改ページの後のページも 1 つと
    数える（1 ページの本文を子の目標に近くしている）。
    """
    counts: dict[str, int] = {}
    for document in documents:
        if document.file_name.endswith(".xlsx"):
            spec = json.loads(document.content)
            counts[document.file_name] = sum(
                len(sheet["rows"]) + (1 if sheet["preamble"] else 0) for sheet in spec["sheets"]
            )
        else:
            counts[document.file_name] = (
                1
                + len(re.findall(r"<h[23][ >]", document.content))
                + document.content.count(f"<p{PAGE_BREAK}>")
                + document.content.count("<table")
            )
    return counts
