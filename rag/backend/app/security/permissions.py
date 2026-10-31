"""RAG の権限カタログと API の権限 manifest（#214）。

- `PERMISSION_CATALOG`: ロールに付けられる権限（メニュー権限と capability）。
- `permission_for_route(method, path)`: API（method × route template）→ 必要な権限の集合
  （いずれか 1 つを持てばよい）。**既定は拒否**で、登録外の API は `UNCLASSIFIED_PERMISSION`
  を返し、認可で 403 にする。公開 API とログインだけで使える API は None を返す。

システム設定・ユーザーとロールの共通メニューは NL2SQL と同じコード・グループ名を使う
（3 製品共通の画面。#206）。権限管理は「セキュリティ設定」の製品固有メニュー
（セクション名は 3 製品で同じ。#658）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pr_system_settings.auth.dependencies import UNCLASSIFIED_PERMISSION as UNCLASSIFIED_PERMISSION

from app.rag.answer_prompts import STAGE_PROMPT_REFS, stage_prompt_permission

# 利用者の範囲（検索・回答プロファイル / ナレッジベース）外の対
# 象を指定したときの 403 の error_code（#224）。
#
# 経路の権限拒否（SECURITY_ROUTE_FORBIDDEN）と違い、frontend はその場で理由を表示する。
SCOPE_FORBIDDEN_CODE = "RAG_SCOPE_FORBIDDEN"


@dataclass(frozen=True, slots=True)
class PermissionDefinition:
    code: str
    group: str
    label: str
    description: str
    implies: tuple[str, ...] = ()


def _permission(
    code: str,
    group: str,
    label: str,
    description: str,
    *,
    implies: tuple[str, ...] = (),
) -> PermissionDefinition:
    return PermissionDefinition(code, group, label, description, implies)


def _menu_permission(code: str, group: str, label: str) -> PermissionDefinition:
    # 説明はナビの名前（label）と同じ用語で書く（#580）。
    return _permission(code, group, label, f"「{label}」の画面を表示し、関連操作を利用できます。")


# ---- メニュー権限 ----

MENU_SEARCH = "menu.search"
MENU_CHAT = "menu.chat"
# 実行履歴（チャット・RAG 検索の実行の一覧と詳細。#1890）。
MENU_HISTORY = "menu.history"
MENU_SEARCH_ANSWER_PROFILES = "menu.search_answer_profiles"
MENU_EVALUATION = "menu.evaluation"
MENU_FEEDBACK = "menu.feedback"
MENU_UPLOAD = "menu.upload"
MENU_FILE_LIST = "menu.file_list"
MENU_KNOWLEDGE_BASES = "menu.knowledge_bases"
# 文書の分類の一覧（大分類 → 中分類 → 小分類）の管理（#2131）。
MENU_DOCUMENT_CATEGORIES = "menu.document_categories"
MENU_SETTINGS_PIPELINE = "menu.settings_pipeline"
MENU_SETTINGS_PREPROCESS = "menu.settings_preprocess"
MENU_SETTINGS_PARSER_ADAPTERS = "menu.settings_parser_adapters"
MENU_SETTINGS_CHUNKING = "menu.settings_chunking"
# 言語資源(文書と質問を読むときの言語の書き方。#2181)。
MENU_SETTINGS_LANGUAGE_PACK = "menu.settings_language_pack"
MENU_SETTINGS_VECTOR_INDEX = "menu.settings_vector_index"
MENU_SETTINGS_RETRIEVAL = "menu.settings_retrieval"
MENU_SETTINGS_PROMPTS = "menu.settings_prompts"
MENU_SETTINGS_GUARDRAIL = "menu.settings_guardrail"
MENU_SETTINGS_EVALUATION = "menu.settings_evaluation"
MENU_SETTINGS_ENTITY_INDEX = "menu.settings_entity_index"
MENU_SETTINGS_GRAPH = "menu.settings_graph"
MENU_SETTINGS_SYSTEM_TABLES = "menu.settings_system_tables"
MENU_SETTINGS_HUGGINGFACE = "menu.settings_huggingface"
MENU_SETTINGS_SERVICES = "menu.settings_services"
MENU_SETTINGS_OCI = "menu.settings_oci"
MENU_SETTINGS_UPLOAD_STORAGE = "menu.settings_upload_storage"
MENU_SETTINGS_MODEL = "menu.settings_model"
MENU_SETTINGS_DATABASE = "menu.settings_database"
MENU_SETTINGS_APPEARANCE = "menu.settings_appearance"
# チャットの添付を使うか（設定は製品ごと。運用設定のシステムテーブルの下。#2517・#2546）。
MENU_SETTINGS_TRACE_RECORDING = "menu.settings_trace_recording"
MENU_SETTINGS_CHAT_ATTACHMENTS = "menu.settings_chat_attachments"
# 回答の注意書き（設定は製品ごと。運用設定のチャットの添付の下。#2405・#2449・#2546）。
MENU_SETTINGS_ANSWER_NOTICE = "menu.settings_answer_notice"
MENU_SECURITY_USERS = "menu.security_users"
MENU_SECURITY_ROLES = "menu.security_roles"
# 外部の MCP クライアント向けの API キー（RAG / NL2SQL 共通。#1805）。
MENU_SECURITY_API_KEYS = "menu.security_api_keys"
MENU_SECURITY_PERMISSIONS = "menu.security_permissions"

# ---- capability ----

SEARCH_ANSWER_PROFILES_MANAGE = "rag.search_answer_profiles.manage"
KNOWLEDGE_BASES_MANAGE = "rag.knowledge_bases.manage"
FEEDBACK_MANAGE = "rag.feedback.manage"
SYSTEM_TABLES_MANAGE = "rag.system_tables.manage"
# 品質評価の判定の校正の管理（全員の判定・一致・裁定・校正・LabelSet の入出力。#3578）。
EVALUATION_CALIBRATION_MANAGE = "rag.evaluation_calibration.manage"

# グループ・名前・並び順は左のナビ（frontend の nav-config.ts と、i18n の
# サイドナビの表示名）と同じにする（#567 / #580。一致は
# tests/test_permission_catalog_nav.py が確かめる）。ナビに無い capability は後ろに置く。
_GROUP_USE = "AI 活用"
_GROUP_INGESTION = "ナレッジ構築"
_GROUP_PIPELINE = "取込・検索・回答設定"
_GROUP_IMPROVE = "改善・運用"
_GROUP_SECURITY = "セキュリティ設定"
_GROUP_OPERATIONS = "運用設定"
_GROUP_USERS_ROLES = "ユーザーとロール"
_GROUP_SETTINGS = "システム設定"
_GROUP_MANAGE = "管理権限"


PERMISSION_CATALOG: tuple[PermissionDefinition, ...] = (
    _menu_permission(MENU_CHAT, _GROUP_USE, "チャット"),
    _menu_permission(MENU_SEARCH, _GROUP_USE, "RAG 検索"),
    _permission(
        MENU_HISTORY,
        _GROUP_USE,
        "実行履歴",
        "「実行履歴」の画面を表示し、チャットと RAG 検索の実行の一覧と詳細"
        "（回答・根拠・回答の検証・処理の経過）を見られます（自分の実行だけ。"
        "すべての利用者の実行を見られるのは SYSTEM_ADMIN だけ）。",
    ),
    _menu_permission(MENU_UPLOAD, _GROUP_INGESTION, "文書アップロード"),
    _menu_permission(MENU_FILE_LIST, _GROUP_INGESTION, "文書インデックス"),
    _menu_permission(MENU_DOCUMENT_CATEGORIES, _GROUP_INGESTION, "文書の分類"),
    _menu_permission(MENU_KNOWLEDGE_BASES, _GROUP_INGESTION, "ナレッジベース"),
    _menu_permission(MENU_SEARCH_ANSWER_PROFILES, _GROUP_INGESTION, "検索・回答プロファイル"),
    _menu_permission(MENU_SETTINGS_PIPELINE, _GROUP_PIPELINE, "設定の概要"),
    _menu_permission(MENU_SETTINGS_LANGUAGE_PACK, _GROUP_PIPELINE, "言語資源"),
    _menu_permission(MENU_SETTINGS_PREPROCESS, _GROUP_PIPELINE, "ファイル準備"),
    _menu_permission(MENU_SETTINGS_PARSER_ADAPTERS, _GROUP_PIPELINE, "文書解析"),
    _menu_permission(MENU_SETTINGS_CHUNKING, _GROUP_PIPELINE, "文書分割"),
    _menu_permission(MENU_SETTINGS_VECTOR_INDEX, _GROUP_PIPELINE, "検索インデックス"),
    _menu_permission(MENU_SETTINGS_ENTITY_INDEX, _GROUP_PIPELINE, "実体・別名索引"),
    _menu_permission(MENU_SETTINGS_GRAPH, _GROUP_PIPELINE, "文書構造索引"),
    _menu_permission(MENU_SETTINGS_RETRIEVAL, _GROUP_PIPELINE, "検索方法"),
    _menu_permission(MENU_SETTINGS_PROMPTS, _GROUP_PIPELINE, "回答プロンプト"),
    _menu_permission(MENU_SETTINGS_GUARDRAIL, _GROUP_PIPELINE, "安全チェック"),
    _menu_permission(MENU_SETTINGS_EVALUATION, _GROUP_PIPELINE, "評価の基準"),
    _menu_permission(MENU_EVALUATION, _GROUP_IMPROVE, "品質評価"),
    _permission(
        MENU_FEEDBACK,
        _GROUP_IMPROVE,
        "フィードバック",
        "「フィードバック」の画面を表示し、フィードバックの一覧・詳細の表示と評価ケースの作成が"
        "できます（自分が送った、利用できる検索・回答プロファイルのフィードバックだけ。すべての利用者の分を"
        "見られるのは SYSTEM_ADMIN だけ）。",
    ),
    # 権限管理は RAG 固有。ユーザー管理・ロール管理は 3 製品共通の画面（#206）。
    _menu_permission(MENU_SECURITY_PERMISSIONS, _GROUP_SECURITY, "権限管理"),
    # 並びはサイドナビと同じ
    # （セキュリティ設定 → ユーザーとロール → 運用設定 → システム設定。#658）。
    _menu_permission(MENU_SECURITY_USERS, _GROUP_USERS_ROLES, "ユーザー管理"),
    _menu_permission(MENU_SECURITY_ROLES, _GROUP_USERS_ROLES, "ロール管理"),
    # 全員のキーの一覧と失効（管理の画面）。自分のキーの発行・失効はログインだけで使える（#1805）。
    _permission(
        MENU_SECURITY_API_KEYS,
        _GROUP_USERS_ROLES,
        "API キー",
        "「API キー」の画面を表示し、全員の API キー（外部の MCP クライアント向け）の一覧の表示と"
        "失効ができます。自分のキーの発行・失効は、この権限が無くてもアカウントのメニューから"
        "できます。",
    ),
    _menu_permission(MENU_SETTINGS_SYSTEM_TABLES, _GROUP_OPERATIONS, "システムテーブル"),
    _menu_permission(MENU_SETTINGS_TRACE_RECORDING, _GROUP_OPERATIONS, "軌跡の記録"),
    _menu_permission(MENU_SETTINGS_CHAT_ATTACHMENTS, _GROUP_OPERATIONS, "チャットの添付"),
    _menu_permission(MENU_SETTINGS_ANSWER_NOTICE, _GROUP_OPERATIONS, "回答の注意書き"),
    _menu_permission(MENU_SETTINGS_HUGGINGFACE, _GROUP_OPERATIONS, "HuggingFace"),
    _menu_permission(MENU_SETTINGS_SERVICES, _GROUP_OPERATIONS, "サービス"),
    _menu_permission(MENU_SETTINGS_OCI, _GROUP_SETTINGS, "OCI 認証"),
    _menu_permission(MENU_SETTINGS_UPLOAD_STORAGE, _GROUP_SETTINGS, "アップロード保存先"),
    _menu_permission(MENU_SETTINGS_MODEL, _GROUP_SETTINGS, "モデル"),
    _menu_permission(MENU_SETTINGS_DATABASE, _GROUP_SETTINGS, "データベース"),
    _menu_permission(MENU_SETTINGS_APPEARANCE, _GROUP_SETTINGS, "外観と証明書"),
    _permission(
        SEARCH_ANSWER_PROFILES_MANAGE,
        _GROUP_MANAGE,
        "検索・回答プロファイル管理",
        "検索・回答プロファイルの作成・アーカイブと、すべての検索・回答プロファイルの利用ができます"
        "（検索・回答プロファイルの対象範囲の制限を受けません）。",
        implies=(MENU_SEARCH_ANSWER_PROFILES,),
    ),
    _permission(
        KNOWLEDGE_BASES_MANAGE,
        _GROUP_MANAGE,
        "ナレッジベース管理",
        "ナレッジベースの作成・アーカイブと、すべてのナレッジベースの利用ができます"
        "（ナレッジベースの対象範囲の制限を受けません）。",
        implies=(MENU_KNOWLEDGE_BASES,),
    ),
    _permission(
        FEEDBACK_MANAGE,
        _GROUP_MANAGE,
        "フィードバックの承認 FAQ 反映",
        "フィードバックの回答を検索・回答プロファイルの承認済み FAQ に登録できます"
        "（自分が送った、利用できる検索・回答プロファイルのフィードバックだけ。すべての利用者の分を"
        "扱えるのは SYSTEM_ADMIN だけ）。フィードバックの表示と、他の利用者の"
        "保存済みの回答の表示・評価・削除も含みます。",
        implies=(MENU_FEEDBACK,),
    ),
    _permission(
        EVALUATION_CALIBRATION_MANAGE,
        _GROUP_MANAGE,
        "判定の校正の管理",
        "「品質評価」の「判定の校正」で、記入の集合の作成・削除、すべての記入者の判定と記入者の間の"
        "一致の表示、裁定、校正の実行、人手の判定（LabelSet の JSON）の書き出し・取り込みができます"
        "（記入は「品質評価」の権限だけでできます。記入者は自分の判定だけを見ます）。",
        implies=(MENU_EVALUATION,),
    ),
    _permission(
        SYSTEM_TABLES_MANAGE,
        _GROUP_MANAGE,
        "システムテーブル管理",
        "「システムテーブル」の画面で、RAG のシステムテーブルの初期化・全再作成ができます。",
        implies=(MENU_SETTINGS_SYSTEM_TABLES,),
    ),
)

ALL_PERMISSION_CODES = frozenset(item.code for item in PERMISSION_CATALOG)
PERMISSION_BY_CODE = {item.code: item for item in PERMISSION_CATALOG}


def unknown_permission_codes(codes: Iterable[str]) -> set[str]:
    """カタログにない権限コード。"""
    return {
        code.strip() for code in codes if code.strip() and code.strip() not in PERMISSION_BY_CODE
    }


def normalize_permission_codes(codes: Iterable[str]) -> set[str]:
    """カタログにある権限コードだけを残す（空白除去）。"""
    return {code.strip() for code in codes if code.strip() in PERMISSION_BY_CODE}


def expand_permissions(codes: Iterable[str]) -> set[str]:
    """implies を閉包した実効権限。"""
    expanded = normalize_permission_codes(codes)
    pending = list(expanded)
    while pending:
        definition = PERMISSION_BY_CODE.get(pending.pop())
        if definition is None:
            continue
        for implied in definition.implies:
            if implied not in expanded:
                expanded.add(implied)
                pending.append(implied)
    return expanded


def grants_all_search_answer_profiles(codes: Iterable[str]) -> bool:
    """検索・回答プロファイル管理はすべての検索・回答プロファイルを利用できる（対象範囲の制限を受けない）。"""
    return SEARCH_ANSWER_PROFILES_MANAGE in expand_permissions(codes)


def grants_all_knowledge_bases(codes: Iterable[str]) -> bool:
    """ナレッジベース管理はすべてのナレッジベースを利用できる（対象範囲の制限を受けない）。"""
    return KNOWLEDGE_BASES_MANAGE in expand_permissions(codes)


# ---- API の権限 manifest ----

# `/figures/{token}`（#1311）と `/source-previews/{token}/…`（#2389）は Cookie を使わず、
# path の署名つきのトークンで route が認証し、トークンの利用者の今の権限・対象範囲で確かめ直す
# （`app.api.routes.figures`・`app.api.routes.source_previews`）。
SOURCE_PREVIEW_PAGE_PATH = "/source-previews/{token}/pages/{page_number}"
SOURCE_PREVIEW_CONTENT_PATH = "/source-previews/{token}/content"
PUBLIC_API_PATHS = frozenset(
    {
        "/health",
        "/ready",
        "/ready/database",
        "/auth/login",
        "/figures/{token}",
        SOURCE_PREVIEW_PAGE_PATH,
        SOURCE_PREVIEW_CONTENT_PATH,
    }
)
AUTHENTICATED_WITHOUT_PERMISSION = frozenset({"/auth/me", "/auth/logout", "/auth/password/change"})
# サービストークン（Agent から利用者として呼ぶ。#230 / #232）で認証する path。認証済みなら通し、
# 権限は MCP のツールごとに判定する（`app.mcp.tools`）。
SERVICE_TOKEN_API_PATHS = frozenset({"/mcp"})
# 外部の MCP クライアントの API キー（`Authorization: Bearer prak_…`。#1805）で認証する path。
# MCP の入口だけ。ほかの API にキーを付けたら 401（画面用の Cookie / CSRF の API を機械から
# 呼ばせない）。
API_KEY_API_PATHS = frozenset({"/mcp"})
# ログインだけで使える（メニュー権限を要しない）path。自分の API キーの発行・一覧・失効（#1805）。
# 公開 path ではないのでログインは要り、初回パスワード変更が済むまでは使えない。
LOGIN_ONLY_API_PATHS = frozenset({"/account/api-keys", "/account/api-keys/{key_id}"})
# 上の 4 つ（権限なしで通す path）の method × path。実行時の判定（platform の共通認証）は
# path 単位のため、同じ path に method を足したときに黙って公開されないよう、
# 完全性テストでこの一覧と照合する（#476）。
OPEN_API_OPERATIONS = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/ready/database"),
        ("POST", "/auth/login"),
        ("GET", "/auth/me"),
        ("POST", "/auth/logout"),
        ("POST", "/auth/password/change"),
        ("POST", "/mcp"),
        ("GET", "/figures/{token}"),
        ("GET", SOURCE_PREVIEW_PAGE_PATH),
        ("GET", SOURCE_PREVIEW_CONTENT_PATH),
        ("GET", "/account/api-keys"),
        ("POST", "/account/api-keys"),
        ("DELETE", "/account/api-keys/{key_id}"),
    }
)
SERVICE_TOKEN_AUDIENCE = "rag"  # nosec B105 - token の audience（呼び先の製品名）で秘密ではない


def _any(*codes: str) -> frozenset[str]:
    return frozenset(codes)


# 複数の画面から使う API の許可集合。
_DOCUMENT_WORKSPACE = _any(MENU_UPLOAD, MENU_FILE_LIST)
# 文書の要約・レシピ・原本表示は、文書ワークスペースと引用カードのプレビュー（検索・チャット・
# KB の検索テスト）が使う。文書の詳細の画面はワークスペース専用の API も使うため、frontend は
# `_DOCUMENT_WORKSPACE` の権限がある利用者にだけ詳細へのリンクを出す（#303）。
_DOCUMENT_VIEW = _any(MENU_UPLOAD, MENU_FILE_LIST, MENU_SEARCH, MENU_CHAT, MENU_KNOWLEDGE_BASES)
_KNOWLEDGE_BASE_READ = _any(
    MENU_UPLOAD,
    MENU_FILE_LIST,
    MENU_KNOWLEDGE_BASES,
    MENU_EVALUATION,
    MENU_SEARCH_ANSWER_PROFILES,
    MENU_SEARCH,
    MENU_CHAT,
)
_SEARCH_ANSWER_PROFILE_READ = _any(
    MENU_SEARCH, MENU_CHAT, MENU_FEEDBACK, MENU_SEARCH_ANSWER_PROFILES, MENU_EVALUATION
)
_ANSWER_USE = _any(MENU_SEARCH, MENU_CHAT)
_SECURITY_ROLE_READ = _any(MENU_SECURITY_USERS, MENU_SECURITY_ROLES, MENU_SECURITY_PERMISSIONS)

_D = "/documents/{document_id}"
_R = "/documents/{document_id}/recipes/{recipe_id}"
_KB = "/knowledge-bases/{knowledge_base_id}"
_BV = "/search-answer-profiles/{search_answer_profile_id}"

# (METHOD, route template) → 許可する権限（いずれか）。`/api` は付けない。
ROUTE_PERMISSIONS: dict[tuple[str, str], frozenset[str]] = {
    # ---- ナレッジ構築: 文書（アップロード・文書インデックス） ----
    ("POST", "/documents/upload"): _any(MENU_UPLOAD),
    ("POST", "/documents/batch-upload"): _any(MENU_UPLOAD),
    ("GET", "/documents"): _any(MENU_FILE_LIST, MENU_KNOWLEDGE_BASES),
    # 削除の確認で使う（削除と同じ権限。#303）。
    ("GET", "/documents/delete-impact"): _any(MENU_FILE_LIST),
    # 一覧の「条件に一致する文書をすべて選ぶ」の ID（一括の操作と同じ画面。#2674）。
    ("GET", "/documents/ids"): _any(MENU_FILE_LIST),
    ("POST", "/documents/language-pack/reprocessing-targets"): _any(MENU_FILE_LIST),
    ("POST", "/documents/language-pack/reprocessing-jobs"): _any(MENU_FILE_LIST),
    # 分類の入力の候補（分類の編集と同じ権限。#547）。
    # 分類の一覧は、管理の画面と、文書の詳細・RAG 検索の分類の選択肢が読む（#2131）。
    ("GET", "/document-categories"): _any(
        MENU_DOCUMENT_CATEGORIES, MENU_UPLOAD, MENU_FILE_LIST, MENU_SEARCH
    ),
    ("POST", "/document-categories"): _any(MENU_DOCUMENT_CATEGORIES),
    ("PATCH", "/document-categories/{category_id}"): _any(MENU_DOCUMENT_CATEGORIES),
    ("POST", "/document-categories/{category_id}/move"): _any(MENU_DOCUMENT_CATEGORIES),
    ("DELETE", "/document-categories/{category_id}"): _any(MENU_DOCUMENT_CATEGORIES),
    ("GET", "/documents/ingestion-jobs"): _DOCUMENT_WORKSPACE,
    ("POST", "/documents/ingestion-jobs/drain"): _any(MENU_UPLOAD),
    # 文書の一括の投入（1 件の投入と同じ権限。#2674）。
    ("POST", "/documents/ingestion-jobs/bulk"): _DOCUMENT_WORKSPACE,
    ("POST", "/documents/ingestion-jobs/{job_id}/retry"): _DOCUMENT_WORKSPACE,
    ("POST", "/documents/ingestion-jobs/{job_id}/cancel"): _DOCUMENT_WORKSPACE,
    ("GET", "/documents/ingestion-jobs/{job_id}"): _DOCUMENT_WORKSPACE,
    ("POST", f"{_D}/ingestion-jobs"): _DOCUMENT_WORKSPACE,
    ("GET", f"{_D}/ingestion-jobs"): _DOCUMENT_WORKSPACE,
    ("POST", f"{_D}/ingestion-segments/retry"): _DOCUMENT_WORKSPACE,
    ("GET", f"{_D}/recipes"): _DOCUMENT_VIEW,
    ("POST", f"{_D}/recipes"): _DOCUMENT_WORKSPACE,
    ("PUT", _R): _DOCUMENT_WORKSPACE,
    ("DELETE", _R): _DOCUMENT_WORKSPACE,
    ("POST", f"{_R}/ingestion-jobs"): _DOCUMENT_WORKSPACE,
    ("GET", f"{_R}/chunks"): _DOCUMENT_WORKSPACE,
    ("POST", f"{_R}/chunk-preview"): _DOCUMENT_WORKSPACE,
    ("GET", f"{_R}/content"): _DOCUMENT_VIEW,
    ("GET", f"{_R}/preview-pages"): _DOCUMENT_VIEW,
    ("GET", f"{_R}/preview-pages/{{page_number}}"): _DOCUMENT_VIEW,
    ("GET", f"{_R}/extraction-export"): _DOCUMENT_WORKSPACE,
    ("POST", f"{_R}/approve"): _DOCUMENT_WORKSPACE,
    ("PATCH", f"{_R}/review-edits"): _DOCUMENT_WORKSPACE,
    ("GET", f"{_D}/chunk-sets"): _DOCUMENT_WORKSPACE,
    ("GET", f"{_D}/sections"): _DOCUMENT_WORKSPACE,
    ("PUT", f"{_D}/sections"): _DOCUMENT_WORKSPACE,
    ("DELETE", f"{_D}/sections"): _DOCUMENT_WORKSPACE,
    ("GET", f"{_D}/ingestion-segments"): _DOCUMENT_WORKSPACE,
    ("GET", _D): _DOCUMENT_VIEW,
    ("GET", f"{_D}/chunks/{{chunk_id}}"): _DOCUMENT_VIEW,
    ("DELETE", _D): _any(MENU_FILE_LIST),
    ("GET", f"{_D}/knowledge-bases"): _DOCUMENT_WORKSPACE,
    ("PUT", f"{_D}/knowledge-bases"): _DOCUMENT_WORKSPACE,
    ("PUT", f"{_D}/classification"): _DOCUMENT_WORKSPACE,
    # 文書の版(置き換えた新しい版。#1248)。分類と同じく文書の編集の権限。
    ("PUT", f"{_D}/superseded-by"): _DOCUMENT_WORKSPACE,
    ("GET", f"{_D}/content"): _DOCUMENT_VIEW,
    ("GET", f"{_D}/crop"): _DOCUMENT_VIEW,
    # 根拠の図の画像（RAG の根拠のカード・サイドパネルの「図を開く」。#2428）。
    ("GET", "/figures"): _DOCUMENT_VIEW,
    ("GET", f"{_D}/preview-pages"): _DOCUMENT_VIEW,
    ("GET", f"{_D}/preview-pages/{{page_number}}"): _DOCUMENT_VIEW,
    # ---- ナレッジ構築: ナレッジベース ----
    ("GET", "/knowledge-bases"): _KNOWLEDGE_BASE_READ,
    ("POST", "/knowledge-bases"): _any(KNOWLEDGE_BASES_MANAGE),
    ("GET", _KB): _KNOWLEDGE_BASE_READ,
    ("PATCH", _KB): _any(MENU_KNOWLEDGE_BASES),
    ("GET", f"{_KB}/graph"): _any(MENU_KNOWLEDGE_BASES),
    # KB ごとの項目抽出の定義（#548）。
    ("GET", f"{_KB}/extraction-fields"): _any(MENU_KNOWLEDGE_BASES),
    ("PUT", f"{_KB}/extraction-fields"): _any(MENU_KNOWLEDGE_BASES),
    ("POST", f"{_KB}/archive"): _any(KNOWLEDGE_BASES_MANAGE),
    ("POST", f"{_KB}/documents"): _any(MENU_KNOWLEDGE_BASES),
    ("DELETE", f"{_KB}/documents/{{document_id}}"): _any(MENU_KNOWLEDGE_BASES),
    # ---- 検索・回答プロファイル ----
    ("GET", "/search-answer-profiles"): _SEARCH_ANSWER_PROFILE_READ,
    ("POST", "/search-answer-profiles"): _any(SEARCH_ANSWER_PROFILES_MANAGE),
    # 実体の索引を持つ文書の数（拡張の開閉の案内。#1388）。
    ("POST", "/search-answer-profiles/entity-index-coverage"): _any(MENU_SEARCH_ANSWER_PROFILES),
    # プロファイルとガイドが継承するシステム設定の窓口（読み取りだけ。#2683）。
    ("GET", "/search-answer-profiles/inherited-contact"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("GET", "/search-answer-profiles/answer-models"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("GET", _BV): _SEARCH_ANSWER_PROFILE_READ,
    ("PATCH", _BV): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/archive"): _any(SEARCH_ANSWER_PROFILES_MANAGE),
    ("GET", f"{_BV}/question-understanding"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("PUT", f"{_BV}/question-understanding"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/question-understanding/preview"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("GET", f"{_BV}/domain-keywords"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("PUT", f"{_BV}/domain-keywords"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/domain-keywords/suggest"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("GET", f"{_BV}/approved-faq"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/approved-faq"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/approved-faq/delete"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/domain-keywords/import/preview"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/domain-keywords/import"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/approved-faq/import/preview"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/approved-faq/import"): _any(MENU_SEARCH_ANSWER_PROFILES),
    # 回答前に類似の承認済み FAQ を提示する（読み取り）。
    ("POST", f"{_BV}/approved-faq/suggest"): _any(
        MENU_SEARCH, MENU_CHAT, MENU_SEARCH_ANSWER_PROFILES
    ),
    # 類似問の提示のオン / オフ（#684）。
    ("PUT", f"{_BV}/approved-faq/settings"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("GET", f"{_BV}/runtime-knowledge"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/runtime-knowledge/edit"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/runtime-knowledge/preview"): _any(MENU_SEARCH_ANSWER_PROFILES),
    # ルールの確認の質問(#717)。確認の判定はチャットが送信のたびに使う。
    ("PUT", f"{_BV}/runtime-knowledge/rules/{{rule_id}}/clarification"): _any(
        MENU_SEARCH_ANSWER_PROFILES
    ),
    ("POST", f"{_BV}/clarifications/suggest"): _any(MENU_CHAT, MENU_SEARCH_ANSWER_PROFILES),
    # 業務ガイド（#1237）。他の知識と同じく、プロファイルの管理の画面の権限で扱う。
    ("GET", f"{_BV}/support-guides"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/support-guides"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("GET", f"{_BV}/support-guides/export"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/support-guides/import/preview"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/support-guides/import"): _any(MENU_SEARCH_ANSWER_PROFILES),
    # 回答の過程の表から下書きを作る（#2397）。
    ("POST", f"{_BV}/support-guides/import/process/workbook"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/support-guides/import/process/preview"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("GET", f"{_BV}/support-guides/{{guide_id}}"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("PUT", f"{_BV}/support-guides/{{guide_id}}"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/support-guides/{{guide_id}}/validate"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/support-guides/{{guide_id}}/publish"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("GET", f"{_BV}/support-guides/{{guide_id}}/revisions/{{revision}}"): _any(
        MENU_SEARCH_ANSWER_PROFILES
    ),
    ("POST", f"{_BV}/support-guides/{{guide_id}}/rollback"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/support-guides/{{guide_id}}/archive"): _any(MENU_SEARCH_ANSWER_PROFILES),
    ("POST", f"{_BV}/support-guides/{{guide_id}}/restore"): _any(MENU_SEARCH_ANSWER_PROFILES),
    # 下書きで試す（#1288）。回答を作るが、業務ガイドの管理者だけ（検索の権限では使えない）。
    ("POST", f"{_BV}/support-guides/{{guide_id}}/try"): _any(MENU_SEARCH_ANSWER_PROFILES),
    # ---- 検索・回答プロファイル: チャット ----
    ("GET", "/chat/models"): _any(MENU_CHAT),
    ("GET", "/chat/conversations"): _any(MENU_CHAT),
    ("POST", "/chat/conversations"): _any(MENU_CHAT),
    ("GET", "/chat/conversations/{conversation_id}"): _any(MENU_CHAT),
    ("PATCH", "/chat/conversations/{conversation_id}"): _any(MENU_CHAT),
    ("DELETE", "/chat/conversations/{conversation_id}"): _any(MENU_CHAT),
    ("POST", "/chat/conversations/{conversation_id}/messages/stream"): _any(MENU_CHAT),
    # 作成中の回答の再購読と停止（#1175）。
    ("GET", "/chat/conversations/{conversation_id}/messages/{message_id}/stream"): _any(MENU_CHAT),
    ("POST", "/chat/conversations/{conversation_id}/messages/{message_id}/cancel"): _any(MENU_CHAT),
    # 回答の処理の経過（共通の span の表の snapshot。#3226）。
    ("GET", "/chat/conversations/{conversation_id}/messages/{assistant_message_id}/trace"): _any(
        MENU_CHAT
    ),
    # ---- 検索・回答プロファイル: 検索と回答履歴 ----
    # 文書ワークスペースのレシピ検索テストも同期検索を使う。
    ("POST", "/search"): _any(MENU_SEARCH, MENU_UPLOAD, MENU_FILE_LIST),
    # KB 詳細の検索テストもストリーム検索を使う。
    ("POST", "/search/stream"): _any(MENU_SEARCH, MENU_KNOWLEDGE_BASES),
    # ストリーム検索の実行の処理の経過（共通の span の表の snapshot。#3226）。
    ("GET", "/search/runs/{run_id}/trace"): _any(MENU_SEARCH, MENU_KNOWLEDGE_BASES),
    # 検索の絞り込みに使える項目（検索・回答プロファイルの KB の項目抽出の定義。#549）。
    ("GET", "/search/extraction-fields"): _any(MENU_SEARCH),
    # RAG 検索の回答に選べるモデル（既定のテキストモデルと Vision モデル。#675）。
    ("GET", "/search/models"): _any(MENU_SEARCH),
    # ---- 実行履歴（#1890）----
    ("GET", "/history/runs"): _any(MENU_HISTORY),
    ("GET", "/history/runs/{run_id}"): _any(MENU_HISTORY),
    ("GET", "/history/runs/{run_id}/trace"): _any(MENU_HISTORY),
    ("GET", "/search/answers"): _ANSWER_USE,
    ("GET", "/search/answers/{trace_id}"): _ANSWER_USE,
    ("GET", "/search/answers/{trace_id}/trace"): _ANSWER_USE,
    # ---- 検索・回答プロファイル: 品質評価 ----
    ("POST", "/evaluation/run"): _any(MENU_EVALUATION),
    # 評価リクエストの Excel のテンプレートと検証（#2065。保存しない）。
    ("GET", "/evaluation/cases/template"): _any(MENU_EVALUATION),
    ("POST", "/evaluation/cases/import"): _any(MENU_EVALUATION),
    ("POST", "/evaluation/cases/process-drafts"): _any(MENU_EVALUATION),
    # 品質評価の job（#390）。状態の取得・取り消しは、投入した利用者の job だけ（backend が判定）。
    ("POST", "/evaluation/jobs/run"): _any(MENU_EVALUATION),
    ("POST", "/evaluation/jobs/{job_id}/results.xlsx"): _any(MENU_EVALUATION),
    ("GET", "/evaluation/jobs/{job_id}"): _any(MENU_EVALUATION),
    ("POST", "/evaluation/jobs/{job_id}/cancel"): _any(MENU_EVALUATION),
    # 判定の校正（#3578）。記入（自分の判定だけ）は品質評価の権限、全員の判定・一致・裁定・校正・
    # 書き出し・取り込みは判定の校正の管理。校正の進捗・取り消しは上の job の API。
    ("GET", "/evaluation/calibration/status"): _any(MENU_EVALUATION),
    ("GET", "/evaluation/calibration/label-sets"): _any(MENU_EVALUATION),
    ("GET", "/evaluation/calibration/label-sets/{label_set_id}"): _any(MENU_EVALUATION),
    ("GET", "/evaluation/calibration/label-sets/{label_set_id}/rows"): _any(MENU_EVALUATION),
    ("PUT", "/evaluation/calibration/label-sets/{label_set_id}/labels"): _any(MENU_EVALUATION),
    ("GET", "/evaluation/calibration/sources"): _any(EVALUATION_CALIBRATION_MANAGE),
    ("POST", "/evaluation/calibration/label-sets"): _any(EVALUATION_CALIBRATION_MANAGE),
    ("POST", "/evaluation/calibration/label-sets/import"): _any(EVALUATION_CALIBRATION_MANAGE),
    ("DELETE", "/evaluation/calibration/label-sets/{label_set_id}"): _any(
        EVALUATION_CALIBRATION_MANAGE
    ),
    ("GET", "/evaluation/calibration/label-sets/{label_set_id}/agreement"): _any(
        EVALUATION_CALIBRATION_MANAGE
    ),
    ("PUT", "/evaluation/calibration/label-sets/{label_set_id}/adjudications"): _any(
        EVALUATION_CALIBRATION_MANAGE
    ),
    ("GET", "/evaluation/calibration/label-sets/{label_set_id}/synthetic"): _any(
        EVALUATION_CALIBRATION_MANAGE
    ),
    ("PUT", "/evaluation/calibration/label-sets/{label_set_id}/synthetic/{item_id}"): _any(
        EVALUATION_CALIBRATION_MANAGE
    ),
    ("GET", "/evaluation/calibration/label-sets/{label_set_id}/calibrations"): _any(
        EVALUATION_CALIBRATION_MANAGE
    ),
    ("POST", "/evaluation/calibration/label-sets/{label_set_id}/calibrations"): _any(
        EVALUATION_CALIBRATION_MANAGE
    ),
    ("GET", "/evaluation/calibration/label-sets/{label_set_id}/export"): _any(
        EVALUATION_CALIBRATION_MANAGE
    ),
    # ---- 検索・回答プロファイル: フィードバック ----
    ("POST", "/feedback"): _ANSWER_USE,
    ("GET", "/feedback/current"): _ANSWER_USE,
    # 一覧・詳細・評価ケースはフィードバック画面の権限（許可された検索・回答プロファイルで絞る）。
    ("GET", "/feedback"): _any(MENU_FEEDBACK),
    ("GET", "/feedback/{feedback_id}"): _any(MENU_FEEDBACK),
    ("GET", "/feedback/{feedback_id}/evaluation-case"): _any(MENU_FEEDBACK),
    # 承認済み FAQ への反映（回答の書き込み）だけは rag.feedback.manage。
    ("POST", "/feedback/{feedback_id}/approved-faq"): _any(FEEDBACK_MANAGE),
    # ---- システム設定（3 製品共通の画面） ----
    ("GET", "/settings/upload-storage"): _any(
        MENU_SETTINGS_UPLOAD_STORAGE, MENU_SETTINGS_OCI, MENU_UPLOAD
    ),
    ("PATCH", "/settings/upload-storage"): _any(MENU_SETTINGS_UPLOAD_STORAGE),
    # 確認は確認用の object を書くので、保存と同じ権限（#2101）。
    ("POST", "/settings/upload-storage/test"): _any(MENU_SETTINGS_UPLOAD_STORAGE),
    ("GET", "/settings/oci"): _any(MENU_SETTINGS_OCI),
    ("PATCH", "/settings/oci"): _any(MENU_SETTINGS_OCI),
    ("PATCH", "/settings/oci/object-storage"): _any(
        MENU_SETTINGS_OCI, MENU_SETTINGS_UPLOAD_STORAGE
    ),
    ("POST", "/settings/oci/config/read"): _any(MENU_SETTINGS_OCI),
    ("POST", "/settings/oci/config/test"): _any(MENU_SETTINGS_OCI),
    ("POST", "/settings/oci/object-storage/namespace"): _any(
        MENU_SETTINGS_OCI, MENU_SETTINGS_UPLOAD_STORAGE
    ),
    ("POST", "/settings/oci/key-file"): _any(MENU_SETTINGS_OCI),
    # 文書ワークスペースは VLM の設定有無を表示に使う。
    ("GET", "/settings/model"): _any(MENU_SETTINGS_MODEL, MENU_UPLOAD, MENU_FILE_LIST),
    ("PATCH", "/settings/model"): _any(MENU_SETTINGS_MODEL),
    ("POST", "/settings/model/test"): _any(MENU_SETTINGS_MODEL),
    ("GET", "/settings/database"): _any(MENU_SETTINGS_DATABASE),
    ("PATCH", "/settings/database"): _any(MENU_SETTINGS_DATABASE),
    ("POST", "/settings/database/wallet"): _any(MENU_SETTINGS_DATABASE),
    ("POST", "/settings/database/wallet/download"): _any(MENU_SETTINGS_DATABASE),
    ("POST", "/settings/database/test"): _any(MENU_SETTINGS_DATABASE),
    # 回答の注意書き（設定は製品ごと。#2449）。
    ("GET", "/settings/answer-notice"): _any(MENU_SETTINGS_ANSWER_NOTICE),
    ("PUT", "/settings/answer-notice"): _any(MENU_SETTINGS_ANSWER_NOTICE),
    # チャットの添付を使うか（設定は製品ごと。#2517）。
    ("GET", "/settings/trace-recording"): _any(MENU_SETTINGS_TRACE_RECORDING),
    ("PUT", "/settings/trace-recording"): _any(MENU_SETTINGS_TRACE_RECORDING),
    ("GET", "/settings/chat-attachments"): _any(MENU_SETTINGS_CHAT_ATTACHMENTS),
    ("PUT", "/settings/chat-attachments"): _any(MENU_SETTINGS_CHAT_ATTACHMENTS),
    # チャットの添付（#2517）。会話の権限（持ち主・system_admin）は API が判定する。
    ("POST", "/chat-attachments"): _any(MENU_CHAT),
    ("GET", "/chat-attachments"): _any(MENU_CHAT),
    ("DELETE", "/chat-attachments/{attachment_id}"): _any(MENU_CHAT),
    # システムテーブルは運用設定の専用の画面（#658）。
    ("GET", "/settings/database/system-tables"): _any(MENU_SETTINGS_SYSTEM_TABLES),
    ("POST", "/settings/database/system-tables/initialize"): _any(SYSTEM_TABLES_MANAGE),
    # 参照先のない行の削除（#511）は、作成・更新と同じ権限にする。
    ("POST", "/settings/database/system-tables/orphaned-rows/delete"): _any(SYSTEM_TABLES_MANAGE),
    # ---- 運用設定 ----
    ("GET", "/settings/huggingface"): _any(MENU_SETTINGS_HUGGINGFACE),
    ("PATCH", "/settings/huggingface"): _any(MENU_SETTINGS_HUGGINGFACE),
    ("GET", "/services/catalog"): _any(MENU_SETTINGS_SERVICES),
    # 文書解析の設定画面も parser サービスの状態を表示する。
    ("GET", "/services/{service_id}/status"): _any(
        MENU_SETTINGS_SERVICES, MENU_SETTINGS_PARSER_ADAPTERS
    ),
    ("GET", "/services/{service_id}/logs"): _any(MENU_SETTINGS_SERVICES),
    ("POST", "/services/{service_id}/start"): _any(MENU_SETTINGS_SERVICES),
    ("POST", "/services/{service_id}/stop"): _any(MENU_SETTINGS_SERVICES),
    ("POST", "/services/{service_id}/restart"): _any(MENU_SETTINGS_SERVICES),
    # ---- 取込・検索・回答設定 ----
    # 設定の概要: 工程の自動進行と、レシピ 11 項目の全体の既定（#528）。
    ("GET", "/settings/pipeline"): _any(MENU_SETTINGS_PIPELINE),
    ("PATCH", "/settings/pipeline"): _any(MENU_SETTINGS_PIPELINE),
    # 文書ワークスペースの処理設定パネルも文書解析の設定を読む。
    ("GET", "/settings/parser-adapters"): _any(
        MENU_SETTINGS_PARSER_ADAPTERS, MENU_UPLOAD, MENU_FILE_LIST
    ),
    ("PATCH", "/settings/parser-adapters"): _any(MENU_SETTINGS_PARSER_ADAPTERS),
    ("GET", "/settings/preprocess"): _any(MENU_SETTINGS_PREPROCESS),
    ("PATCH", "/settings/preprocess"): _any(MENU_SETTINGS_PREPROCESS),
    ("GET", "/settings/chunking"): _any(MENU_SETTINGS_CHUNKING),
    ("PATCH", "/settings/chunking"): _any(MENU_SETTINGS_CHUNKING),
    # 検索・回答プロファイルの言語資源の上書きの欄も、全体の値を読む（#2181）。
    ("GET", "/settings/language-pack"): _any(
        MENU_SETTINGS_LANGUAGE_PACK, SEARCH_ANSWER_PROFILES_MANAGE
    ),
    ("GET", "/settings/language-pack/draft"): _any(MENU_SETTINGS_LANGUAGE_PACK),
    ("GET", "/settings/language-pack/draft/comparison"): _any(MENU_SETTINGS_LANGUAGE_PACK),
    ("PUT", "/settings/language-pack/draft"): _any(MENU_SETTINGS_LANGUAGE_PACK),
    ("POST", "/settings/language-pack/preview"): _any(MENU_SETTINGS_LANGUAGE_PACK),
    ("POST", "/settings/language-pack/publish"): _any(MENU_SETTINGS_LANGUAGE_PACK),
    ("GET", "/settings/language-pack/releases"): _any(MENU_SETTINGS_LANGUAGE_PACK),
    ("GET", "/settings/language-pack/releases/compare"): _any(
        MENU_SETTINGS_LANGUAGE_PACK, MENU_FILE_LIST
    ),
    ("GET", "/settings/language-pack/releases/{release_id}"): _any(
        MENU_SETTINGS_LANGUAGE_PACK, MENU_FILE_LIST
    ),
    ("POST", "/settings/language-pack/releases/{release_id}/restore"): _any(
        MENU_SETTINGS_LANGUAGE_PACK
    ),
    # 回答履歴の保存設定は、検索・チャットの回答履歴表示も読む。
    # 回答の検索と生成の全体既定・回答の記録の保存期間・質問履歴は「検索方法」の画面にある(#593)。
    ("GET", "/settings/answering"): _any(MENU_SETTINGS_RETRIEVAL),
    ("PATCH", "/settings/answering"): _any(MENU_SETTINGS_RETRIEVAL),
    ("GET", "/settings/answer-records"): _any(MENU_SETTINGS_RETRIEVAL, MENU_SEARCH, MENU_CHAT),
    ("PATCH", "/settings/answer-records"): _any(MENU_SETTINGS_RETRIEVAL),
    # 編集できるプロンプトは回答プロンプト・文書解析・文書分割の
    # 画面で編集する。検索方法では検索関連の指示を読む。変更（PUT / DELETE）は、
    # route の中で `ANSWER_PROMPT_EDIT_PERMISSIONS` の key ごとの権限も確かめる。
    ("GET", "/settings/answer-prompts"): _any(
        MENU_SETTINGS_PROMPTS,
        MENU_SETTINGS_PARSER_ADAPTERS,
        MENU_SETTINGS_CHUNKING,
        MENU_SETTINGS_RETRIEVAL,
        MENU_SETTINGS_EVALUATION,
    ),
    ("PUT", "/settings/answer-prompts/{key}"): _any(
        MENU_SETTINGS_PROMPTS,
        MENU_SETTINGS_PARSER_ADAPTERS,
        MENU_SETTINGS_CHUNKING,
        MENU_SETTINGS_RETRIEVAL,
        MENU_SETTINGS_EVALUATION,
    ),
    ("DELETE", "/settings/answer-prompts/{key}"): _any(
        MENU_SETTINGS_PROMPTS,
        MENU_SETTINGS_PARSER_ADAPTERS,
        MENU_SETTINGS_CHUNKING,
        MENU_SETTINGS_RETRIEVAL,
        MENU_SETTINGS_EVALUATION,
    ),
    # 抽出項目は文書ワークスペース（処理設定）と文書解析の設定画面が読む。
    ("GET", "/settings/extraction-fields"): _any(
        MENU_FILE_LIST, MENU_UPLOAD, MENU_SETTINGS_PARSER_ADAPTERS
    ),
    # 抽出項目の定義は文書解析の「解析後の処理」で編集する（#528）。
    ("PATCH", "/settings/extraction-fields"): _any(MENU_SETTINGS_PARSER_ADAPTERS),
    # 「標準の項目に戻す」（#556）。
    ("DELETE", "/settings/extraction-fields"): _any(MENU_SETTINGS_PARSER_ADAPTERS),
    ("GET", "/settings/section-rules"): _any(
        MENU_SETTINGS_PARSER_ADAPTERS, MENU_UPLOAD, MENU_FILE_LIST
    ),
    ("PATCH", "/settings/section-rules"): _any(MENU_SETTINGS_PARSER_ADAPTERS),
    ("DELETE", "/settings/section-rules"): _any(MENU_SETTINGS_PARSER_ADAPTERS),
    ("GET", "/settings/guardrail"): _any(MENU_SETTINGS_GUARDRAIL),
    ("PATCH", "/settings/guardrail"): _any(MENU_SETTINGS_GUARDRAIL),
    ("GET", "/settings/vector-index"): _any(MENU_SETTINGS_VECTOR_INDEX),
    ("PATCH", "/settings/vector-index"): _any(MENU_SETTINGS_VECTOR_INDEX),
    # 品質評価の画面は評価設定（既定値）を読む。
    ("GET", "/settings/evaluation-suite"): _any(MENU_SETTINGS_EVALUATION, MENU_EVALUATION),
    ("PATCH", "/settings/evaluation-suite"): _any(MENU_SETTINGS_EVALUATION),
    ("GET", "/settings/entity-index"): _any(MENU_SETTINGS_ENTITY_INDEX),
    ("PATCH", "/settings/entity-index"): _any(MENU_SETTINGS_ENTITY_INDEX),
    ("GET", "/settings/graph"): _any(MENU_SETTINGS_GRAPH),
    ("PATCH", "/settings/graph"): _any(MENU_SETTINGS_GRAPH),
    # ---- ユーザーとロール（platform の共通 router。NL2SQL と同じ割り当て）----
    # 前方一致で割り当てず、route ごとに登録する（新しい route は登録するまで拒否。#476）。
    ("GET", "/security/users"): _any(MENU_SECURITY_USERS),
    ("POST", "/security/users"): _any(MENU_SECURITY_USERS),
    ("GET", "/security/users/{user_uuid}"): _any(MENU_SECURITY_USERS),
    ("PATCH", "/security/users/{user_uuid}"): _any(MENU_SECURITY_USERS),
    ("DELETE", "/security/users/{user_uuid}"): _any(MENU_SECURITY_USERS),
    ("POST", "/security/users/{user_uuid}/disable"): _any(MENU_SECURITY_USERS),
    ("POST", "/security/users/{user_uuid}/enable"): _any(MENU_SECURITY_USERS),
    ("POST", "/security/users/{user_uuid}/reset-password"): _any(MENU_SECURITY_USERS),
    ("POST", "/security/users/{user_uuid}/unlock"): _any(MENU_SECURITY_USERS),
    ("GET", "/security/roles"): _SECURITY_ROLE_READ,
    ("GET", "/security/roles/{role_id}"): _SECURITY_ROLE_READ,
    ("POST", "/security/roles"): _any(MENU_SECURITY_ROLES),
    ("PATCH", "/security/roles/{role_id}"): _any(MENU_SECURITY_ROLES),
    ("DELETE", "/security/roles/{role_id}"): _any(MENU_SECURITY_ROLES),
    ("POST", "/security/roles/{role_id}/archive"): _any(MENU_SECURITY_ROLES),
    ("POST", "/security/roles/{role_id}/restore"): _any(MENU_SECURITY_ROLES),
    # 外部の MCP クライアント向けの API キー（platform の共通 router。#1805）。
    ("GET", "/security/api-keys"): _any(MENU_SECURITY_API_KEYS),
    ("DELETE", "/security/api-keys/{key_id}"): _any(MENU_SECURITY_API_KEYS),
    # ---- セキュリティ設定: 権限管理 ----
    ("GET", "/security/permissions"): _any(MENU_SECURITY_PERMISSIONS),
    ("GET", "/security/access-targets/search-answer-profiles"): _any(MENU_SECURITY_PERMISSIONS),
    ("GET", "/security/access-targets/knowledge-bases"): _any(MENU_SECURITY_PERMISSIONS),
    ("PUT", "/security/roles/{role_id}/access"): _any(MENU_SECURITY_PERMISSIONS),
}


# 編集できるプロンプトの key → 変更に要る権限（#1010）。画面はそれぞれ 1 つのプロンプトだけを
# 編集するので、片方の画面の権限でもう片方のプロンプトを変えられないようにする。
ANSWER_PROMPT_EDIT_PERMISSIONS: dict[str, str] = {
    "vlm_answer": MENU_SETTINGS_PROMPTS,
    "image_retrieval": MENU_SETTINGS_PARSER_ADAPTERS,
    "heading_levels": MENU_SETTINGS_PARSER_ADAPTERS,
    "field_extraction": MENU_SETTINGS_PARSER_ADAPTERS,
    "navigation_summary": MENU_SETTINGS_PARSER_ADAPTERS,
    "raptor_summary_system": MENU_SETTINGS_PARSER_ADAPTERS,
}


# 配置先と保存先の対応は指示の一覧を正本にする。

ANSWER_PROMPT_EDIT_PERMISSIONS.update(
    {
        key: permission
        for key in STAGE_PROMPT_REFS
        if (permission := stage_prompt_permission(key)) is not None
    }
)


def permission_for_route(method: str, route_path: str) -> frozenset[str] | None:
    """method + route template（`/api` なし）→ 許可する権限の集合。

    None は公開 API・ログインだけで使える API・MCP（ツールごとに権限を判定）。
    登録外は `UNCLASSIFIED_PERMISSION`（拒否）。
    """
    method = method.upper()
    if (method, route_path) in OPEN_API_OPERATIONS:
        return None
    exact = ROUTE_PERMISSIONS.get((method, route_path))
    if exact is not None:
        return exact
    return _any(UNCLASSIFIED_PERMISSION)
