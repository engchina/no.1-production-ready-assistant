"""Permission catalog と API route manifest。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


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


def _menu_permission(
    code: str,
    group: str,
    label: str,
    *,
    implies: tuple[str, ...] = (),
) -> PermissionDefinition:
    # 説明はナビの名前（label）と同じ用語で書く（#580）。
    return _permission(
        code,
        group,
        label,
        f"「{label}」の画面を表示し、関連操作を利用できます。",
        implies=implies,
    )


PROFILE_READ_PERMISSION = "nl2sql.profiles.read"
PROFILE_MANAGE_PERMISSION = "nl2sql.profiles.manage"
SCHEMA_READ_PERMISSION = "nl2sql.schema.read"
SCHEMA_REFRESH_PERMISSION = "nl2sql.schema.refresh"
QUERY_GENERATE_PERMISSION = "nl2sql.query.generate"
SQL_EXECUTE_PERMISSION = "nl2sql.sql.execute"
FEEDBACK_WRITE_PERMISSION = "nl2sql.feedback.write"
FEEDBACK_MANAGE_PERMISSION = "nl2sql.feedback.manage"
SELECT_AI_ASSETS_READ_PERMISSION = "nl2sql.select_ai_assets.read"
SELECT_AI_ASSETS_REFRESH_PERMISSION = "nl2sql.select_ai_assets.refresh"
SELECT_AI_ASSETS_MANAGE_PERMISSION = "nl2sql.select_ai_assets.manage"
SAMPLE_DATA_MANAGE_PERMISSION = "nl2sql.sample_data.manage"
LEARNING_MATERIAL_MANAGE_PERMISSION = "nl2sql.learning_material.manage"
SYSTEM_STATUS_READ_PERMISSION = "nl2sql.system_status.read"
PERSISTENCE_RECOVER_PERMISSION = "nl2sql.persistence.recover"


# メニュー権限のグループ・名前・並び順は左のナビ（frontend の nav-config.ts と、
# i18n のサイドナビの表示名）と同じにする（#567 / #580。一致は
# tests/test_permission_catalog_nav.py が確かめる）。ナビに無い権限（Ontology・
# 参照権限・実行権限・管理権限の capability）はメニュー権限の後ろに置く。
PERMISSION_CATALOG: tuple[PermissionDefinition, ...] = (
    _menu_permission(
        "menu.chat",
        "AI 活用",
        "チャット",
        implies=(QUERY_GENERATE_PERMISSION, PROFILE_READ_PERMISSION, SCHEMA_READ_PERMISSION),
    ),
    _menu_permission(
        "menu.query",
        "AI 活用",
        "SQL 生成",
        implies=(
            QUERY_GENERATE_PERMISSION,
            SQL_EXECUTE_PERMISSION,
            FEEDBACK_WRITE_PERMISSION,
            PROFILE_READ_PERMISSION,
            SCHEMA_READ_PERMISSION,
        ),
    ),
    _menu_permission(
        "menu.direct_sql",
        "AI 活用",
        "SELECT SQL を実行",
        implies=(SQL_EXECUTE_PERMISSION, SCHEMA_READ_PERMISSION),
    ),
    _menu_permission(
        "menu.sql_to_question",
        "AI 活用",
        "SQL から質問を生成",
        implies=(PROFILE_READ_PERMISSION, SCHEMA_READ_PERMISSION),
    ),
    _menu_permission("menu.history", "AI 活用", "実行履歴"),
    # DB 管理チャット（#2264）。全種類の SQL を生成・実行できるので、既定はどのロールにも付けない
    # （system_admin だけが使える）。付与は「自分の持つ権限だけを付与できる」規則で越権を防ぐ。
    _menu_permission(
        "menu.db_admin_chat",
        "データ準備",
        "DB 管理チャット",
        implies=(SCHEMA_READ_PERMISSION, SCHEMA_REFRESH_PERMISSION),
    ),
    _menu_permission(
        "menu.admin_sql",
        "データ準備",
        "管理 SQL を実行",
        implies=(
            SCHEMA_READ_PERMISSION,
            SCHEMA_REFRESH_PERMISSION,
            SYSTEM_STATUS_READ_PERMISSION,
            PERSISTENCE_RECOVER_PERMISSION,
        ),
    ),
    _menu_permission(
        "menu.table_management",
        "データ準備",
        "テーブルの管理",
        implies=(SCHEMA_READ_PERMISSION, SCHEMA_REFRESH_PERMISSION),
    ),
    _menu_permission(
        "menu.view_management",
        "データ準備",
        "ビューの管理",
        implies=(SCHEMA_READ_PERMISSION, SCHEMA_REFRESH_PERMISSION),
    ),
    _menu_permission(
        "menu.data_management",
        "データ準備",
        "データの管理",
        implies=(
            SCHEMA_READ_PERMISSION,
            SCHEMA_REFRESH_PERMISSION,
            SELECT_AI_ASSETS_READ_PERMISSION,
            SELECT_AI_ASSETS_REFRESH_PERMISSION,
        ),
    ),
    # コメント・アノテーション・ドメインの管理も DDL を実行し、画面の「スキーマを更新」と実行後の
    # job の追跡を使うので、テーブルの管理と同じくスキーマの参照・更新を含める（#972）。
    _menu_permission(
        "menu.comment_management",
        "データ準備",
        "コメント管理",
        implies=(SCHEMA_READ_PERMISSION, SCHEMA_REFRESH_PERMISSION),
    ),
    _menu_permission(
        "menu.annotation_management",
        "データ準備",
        "アノテーション管理",
        implies=(SCHEMA_READ_PERMISSION, SCHEMA_REFRESH_PERMISSION),
    ),
    _menu_permission(
        "menu.domain_management",
        "データ準備",
        "ドメイン管理",
        implies=(SCHEMA_READ_PERMISSION, SCHEMA_REFRESH_PERMISSION),
    ),
    # 用語・同義語と共通ルールの画面は、それぞれ全体の用語集・共通ルールの取込・出力だけを使う。
    # 業務プロファイル管理（全業務プロファイルの利用を含む）と、もう一方の取込を許す
    # 学習素材管理は含めない（route は menu 権限で判定する。#1006）。
    _menu_permission(
        "menu.glossary_rules",
        "データ準備",
        "用語・同義語",
        implies=(SCHEMA_READ_PERMISSION,),
    ),
    _menu_permission(
        "menu.global_rules",
        "データ準備",
        "共通ルール",
        implies=(SCHEMA_READ_PERMISSION,),
    ),
    _menu_permission(
        "menu.sample_data",
        "データ準備",
        "サンプルデータ管理",
        implies=(SAMPLE_DATA_MANAGE_PERMISSION, SCHEMA_READ_PERMISSION),
    ),
    _menu_permission(
        "menu.profiles",
        "改善・運用",
        "業務プロファイル",
        implies=(
            PROFILE_MANAGE_PERMISSION,
            SCHEMA_READ_PERMISSION,
            SCHEMA_REFRESH_PERMISSION,
            SELECT_AI_ASSETS_READ_PERMISSION,
            SELECT_AI_ASSETS_REFRESH_PERMISSION,
            SELECT_AI_ASSETS_MANAGE_PERMISSION,
            LEARNING_MATERIAL_MANAGE_PERMISSION,
        ),
    ),
    _menu_permission(
        "menu.ontology_build",
        "改善・運用",
        "オントロジー構築",
        implies=(
            PROFILE_MANAGE_PERMISSION,
            SCHEMA_READ_PERMISSION,
            SCHEMA_REFRESH_PERMISSION,
            LEARNING_MATERIAL_MANAGE_PERMISSION,
        ),
    ),
    _menu_permission(
        "menu.feedback_management",
        "改善・運用",
        "フィードバック管理",
        implies=(
            PROFILE_READ_PERMISSION,
            FEEDBACK_WRITE_PERMISSION,
            FEEDBACK_MANAGE_PERMISSION,
            SELECT_AI_ASSETS_READ_PERMISSION,
            SELECT_AI_ASSETS_MANAGE_PERMISSION,
        ),
    ),
    _menu_permission(
        "menu.question_classifier_models",
        "改善・運用",
        "質問分類モデル管理",
        implies=(PROFILE_READ_PERMISSION,),
    ),
    _menu_permission(
        "menu.evaluation",
        "改善・運用",
        "SQL生成評価",
        implies=(PROFILE_READ_PERMISSION, QUERY_GENERATE_PERMISSION),
    ),
    # 権限管理と DeepSec は NL2SQL 固有。ユーザー管理・ロール管理は3製品共通の画面（#206）。
    # セクション名「セキュリティ設定」は 3 製品で同じ（#658）。
    _menu_permission("menu.security_permissions", "セキュリティ設定", "権限管理"),
    _menu_permission("menu.security_deepsec", "セキュリティ設定", "Deep Data Security"),
    # 並びはサイドナビと同じ
    # （セキュリティ設定 → ユーザーとロール → 運用設定 → システム設定。#658）。
    _menu_permission("menu.security_users", "ユーザーとロール", "ユーザー管理"),
    _menu_permission("menu.security_roles", "ユーザーとロール", "ロール管理"),
    # 全員のキーの一覧と失効（管理の画面）。自分のキーの発行・失効はログインだけで使える（#1805）。
    _permission(
        "menu.security_api_keys",
        "ユーザーとロール",
        "API キー",
        "「API キー」の画面を表示し、全員の API キー（外部の MCP クライアント向け）の一覧の表示と"
        "失効ができます。自分のキーの発行・失効は、この権限が無くてもアカウントのメニューから"
        "できます。",
    ),
    _menu_permission(
        "menu.settings_system_tables",
        "運用設定",
        "システムテーブル",
        implies=(
            SCHEMA_READ_PERMISSION,
            SCHEMA_REFRESH_PERMISSION,
            SYSTEM_STATUS_READ_PERMISSION,
            PERSISTENCE_RECOVER_PERMISSION,
        ),
    ),
    # チャットの添付を使うか（設定は製品ごと。運用設定のシステムテーブルの下。#2517・#2546）。
    _menu_permission("menu.settings_trace_recording", "運用設定", "軌跡の記録"),
    _menu_permission("menu.settings_chat_attachments", "運用設定", "チャットの添付"),
    # 回答の注意書き（設定は製品ごと。運用設定のチャットの添付の下。#2405・#2449・#2546）。
    _menu_permission("menu.settings_answer_notice", "運用設定", "回答の注意書き"),
    # データベース設定から分けた運用の操作（#658）。
    _menu_permission("menu.settings_select_ai_credential", "運用設定", "Select AI Credential"),
    _menu_permission("menu.settings_upload_storage", "システム設定", "アップロード保存先"),
    _menu_permission("menu.settings_model", "システム設定", "モデル"),
    _menu_permission(
        "menu.settings_database",
        "システム設定",
        "データベース",
        implies=(
            SCHEMA_READ_PERMISSION,
            SCHEMA_REFRESH_PERMISSION,
            SYSTEM_STATUS_READ_PERMISSION,
            PERSISTENCE_RECOVER_PERMISSION,
        ),
    ),
    _menu_permission("menu.settings_appearance", "システム設定", "外観と証明書"),
    # ここから下はナビに無い権限（画面の中の操作を許可する capability）。
    _permission(
        "nl2sql.ontology.capabilities.manage",
        "Ontology",
        "能力設定（Capability Binding）",
        "公開済みの関数・操作に実装を明示的に設定します。",
    ),
    _permission(
        "nl2sql.ontology.actions.execute",
        "Ontology",
        "操作実行（Action Execution）",
        "許可された Profile の操作をプレビューして確認後に実行します。",
    ),
    _permission(
        PROFILE_READ_PERMISSION,
        "参照権限",
        "業務プロファイル参照",
        "「SQL 生成」や「SQL から質問を生成」で、業務プロファイルの選択肢と利用コンテキストを"
        "参照できます。",
    ),
    _permission(
        PROFILE_MANAGE_PERMISSION,
        "管理権限",
        "業務プロファイル管理",
        "業務プロファイルの詳細表示、作成、更新、削除、Oracle 反映を実行できます。",
        implies=(PROFILE_READ_PERMISSION,),
    ),
    _permission(
        SCHEMA_READ_PERMISSION,
        "参照権限",
        "スキーマ参照",
        "SQL 生成や管理画面で、表・ビュー・列の参照情報を読み取れます。",
    ),
    _permission(
        SCHEMA_REFRESH_PERMISSION,
        "管理権限",
        "スキーマ更新",
        "Oracle から表・ビュー・列の最新情報を再取得できます。",
        implies=(SCHEMA_READ_PERMISSION,),
    ),
    _permission(
        QUERY_GENERATE_PERMISSION,
        "実行権限",
        "SQL 生成実行",
        "自然言語から SQL を生成し、推薦・書き換え・類似履歴を利用できます。",
    ),
    _permission(
        SQL_EXECUTE_PERMISSION,
        "実行権限",
        "SELECT SQL 実行",
        "SELECT/WITH SQL の安全確認と実行を利用できます。",
    ),
    _permission(
        FEEDBACK_WRITE_PERMISSION,
        "実行権限",
        "フィードバック登録",
        "自分の「実行履歴」（SQL 生成の履歴）へ利用者フィードバックを登録できます。",
    ),
    _permission(
        FEEDBACK_MANAGE_PERMISSION,
        "管理権限",
        "フィードバック管理",
        "全利用者のフィードバック一覧、管理者レビュー、学習 index 設定を管理できます。",
        implies=(FEEDBACK_WRITE_PERMISSION, PROFILE_READ_PERMISSION),
    ),
    _permission(
        SELECT_AI_ASSETS_READ_PERMISSION,
        "参照権限",
        "Select AI 資産参照",
        "Oracle Select AI / Agent の profile・資産状態を参照できます。",
    ),
    _permission(
        SELECT_AI_ASSETS_REFRESH_PERMISSION,
        "管理権限",
        "Select AI 資産更新",
        "Oracle Select AI / Agent の資産情報を再取得・反映できます。",
        implies=(SELECT_AI_ASSETS_READ_PERMISSION,),
    ),
    _permission(
        SELECT_AI_ASSETS_MANAGE_PERMISSION,
        "管理権限",
        "Select AI 資産管理",
        "Oracle Select AI / Agent の低レベル profile・feedback・資産を作成、更新、削除できます。",
        implies=(SELECT_AI_ASSETS_READ_PERMISSION, SELECT_AI_ASSETS_REFRESH_PERMISSION),
    ),
    _permission(
        SAMPLE_DATA_MANAGE_PERMISSION,
        "管理権限",
        "サンプルデータの投入・削除",
        "「サンプルデータ管理」で、サンプルデータの確認、投入、削除を実行できます。",
    ),
    _permission(
        LEARNING_MATERIAL_MANAGE_PERMISSION,
        "管理権限",
        "学習素材管理",
        "用語、ルール、few-shot などの学習素材を import/export できます。",
    ),
    _permission(
        SYSTEM_STATUS_READ_PERMISSION,
        "参照権限",
        "システム状態参照",
        "NL2SQL の永続化状態や診断情報を参照できます。",
    ),
    _permission(
        PERSISTENCE_RECOVER_PERMISSION,
        "管理権限",
        "永続化復旧",
        "DB 復旧後の永続化接続と migration 状態を再確認できます。",
        implies=(SYSTEM_STATUS_READ_PERMISSION,),
    ),
)

ALL_PERMISSION_CODES = frozenset(item.code for item in PERMISSION_CATALOG)
PERMISSION_BY_CODE = {item.code: item for item in PERMISSION_CATALOG}
UNCLASSIFIED_PERMISSION = "__unclassified__"

AI_USE_MENUS = frozenset(
    {
        "menu.query",
        "menu.direct_sql",
        "menu.sql_to_question",
        "menu.history",
    }
)
DATA_PREP_MENUS = frozenset(
    {
        "menu.admin_sql",
        "menu.table_management",
        "menu.view_management",
        "menu.data_management",
        "menu.comment_management",
        "menu.annotation_management",
        "menu.domain_management",
        "menu.glossary_rules",
        "menu.global_rules",
        "menu.sample_data",
    }
)
BUSINESS_MODEL_MENUS = frozenset(
    {
        "menu.profiles",
        "menu.ontology_build",
        "menu.glossary_rules",
        "menu.global_rules",
    }
)
SCHEMA_READ_MENUS = frozenset(
    {
        "menu.query",
        "menu.direct_sql",
        "menu.db_admin_chat",
        "menu.admin_sql",
        "menu.table_management",
        "menu.view_management",
        "menu.data_management",
        "menu.comment_management",
        "menu.annotation_management",
        "menu.domain_management",
        "menu.profiles",
        "menu.ontology_build",
        "menu.glossary_rules",
        "menu.global_rules",
        "menu.settings_database",
        "menu.settings_system_tables",
    }
)

LEGACY_PERMISSION_ALIASES: dict[str, tuple[str, ...]] = {
    "dashboard.view": ("menu.settings_appearance",),
    "documents.view": (
        "menu.table_management",
        "menu.view_management",
        "menu.data_management",
        "menu.comment_management",
        "menu.annotation_management",
        "menu.domain_management",
        "menu.sample_data",
    ),
    "documents.upload": ("menu.data_management", "menu.sample_data"),
    "documents.preview": (
        "menu.table_management",
        "menu.view_management",
        "menu.data_management",
    ),
    "documents.approve": ("menu.data_management",),
    "documents.ingest": ("menu.data_management",),
    "documents.delete": (
        "menu.table_management",
        "menu.view_management",
        "menu.data_management",
    ),
    "knowledge_bases.view": tuple(BUSINESS_MODEL_MENUS),
    "knowledge_bases.manage": tuple(BUSINESS_MODEL_MENUS),
    "business_views.view": ("menu.profiles",),
    "business_views.manage": ("menu.profiles",),
    "business_views.use": ("menu.query", "menu.profiles"),
    "search.view": tuple(AI_USE_MENUS),
    "search.execute": ("menu.query", "menu.direct_sql"),
    "search.export": ("menu.query", "menu.direct_sql", "menu.history"),
    "evaluation.view": (
        "menu.feedback_management",
        "menu.question_classifier_models",
        "menu.evaluation",
    ),
    "evaluation.run": ("menu.evaluation",),
    "evaluation.manage": (
        "menu.feedback_management",
        "menu.question_classifier_models",
        "menu.evaluation",
    ),
    "settings.object_storage.view": ("menu.settings_upload_storage",),
    "settings.object_storage.manage": ("menu.settings_upload_storage",),
    "settings.models.view": ("menu.settings_model",),
    "settings.models.manage": ("menu.settings_model",),
    "settings.database.view": (
        "menu.settings_database",
        "menu.settings_system_tables",
    ),
    "settings.database.manage": (
        "menu.settings_database",
        "menu.settings_select_ai_credential",
    ),
    "settings.database.sql_execute": (
        "menu.admin_sql",
        "menu.settings_system_tables",
    ),
    "settings.appearance.view": ("menu.settings_appearance",),
    "security.users.view": ("menu.security_users",),
    "security.users.manage": ("menu.security_users",),
    "security.roles.view": ("menu.security_roles",),
    "security.roles.manage": ("menu.security_roles", "menu.security_permissions"),
    "security.deepsec.view": ("menu.security_deepsec",),
    "security.deepsec.apply": ("menu.security_deepsec",),
    "security.deepsec.verify": ("menu.security_deepsec",),
}

for _adapter in (
    "preprocess",
    "parser",
    "chunking",
    "vector_index",
    "retrieval",
    "grounding",
    "generation",
    "guardrail",
    "evaluation",
    "graph",
    "agentic",
):
    LEGACY_PERMISSION_ALIASES[f"pipeline.{_adapter}.view"] = ("menu.settings_model",)
    LEGACY_PERMISSION_ALIASES[f"pipeline.{_adapter}.manage"] = ("menu.settings_model",)


def normalize_permission_codes(codes: Iterable[str]) -> set[str]:
    """保存済み legacy action 権限を現在の permission code へ正規化する。"""

    normalized: set[str] = set()
    pending = [code.strip() for code in codes if code and code.strip()]
    while pending:
        code = pending.pop()
        if code in ALL_PERMISSION_CODES:
            normalized.add(code)
            continue
        pending.extend(LEGACY_PERMISSION_ALIASES.get(code, ()))
    return normalized


def unknown_permission_codes(codes: Iterable[str]) -> set[str]:
    """catalog と legacy alias のどちらにも存在しない code を返す。"""

    return {
        code.strip()
        for code in codes
        if code.strip()
        and code.strip() not in ALL_PERMISSION_CODES
        and code.strip() not in LEGACY_PERMISSION_ALIASES
    }


def expand_permissions(codes: set[str]) -> set[str]:
    """旧 action permission と implied permission を含む実効権限へ閉包する。"""

    expanded = normalize_permission_codes(codes)
    pending = list(expanded)
    while pending:
        code = pending.pop()
        definition = PERMISSION_BY_CODE.get(code)
        if definition is None:
            continue
        for implied in definition.implies:
            for normalized in normalize_permission_codes((implied,)):
                if normalized in expanded:
                    continue
                expanded.add(normalized)
                pending.append(normalized)
    return expanded


def grants_all_profile_access(codes: Iterable[str]) -> bool:
    """業務プロファイル管理系の権限は個別 profile 制限を受けない。"""

    return PROFILE_MANAGE_PERMISSION in expand_permissions(set(codes))


PUBLIC_API_PATHS = frozenset({"/health", "/ready", "/ready/database", "/auth/login"})
AUTHENTICATED_WITHOUT_PERMISSION = frozenset({"/auth/me", "/auth/logout", "/auth/password/change"})
# サービストークン（Authorization: Bearer）で認証する path と audience（#230 / #231）。
# MCP は認証済みなら通し、ツールごとの権限はツール側（app.features.mcp.tools）で判定する。
MCP_API_PATH = "/mcp"
SERVICE_TOKEN_API_PATHS = frozenset({MCP_API_PATH})
# 外部の MCP クライアントの API キー（`Authorization: Bearer prak_…`。#1805）で認証する path。
# MCP の入口だけ。ほかの API にキーを付けたら 401（画面用の Cookie / CSRF の API を機械から
# 呼ばせない）。
API_KEY_API_PATHS = frozenset({MCP_API_PATH})
MCP_AUDIENCE = "nl2sql"
# 権限なしで通す操作（method × path）。公開・ログインだけ・MCP の path でも、ここにない method は
# 通常の認証と権限の判定にする（共通認証の `open_operations` にも渡す。#490）。
OPEN_API_OPERATIONS = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/ready/database"),
        ("POST", "/auth/login"),
        ("GET", "/auth/me"),
        ("POST", "/auth/logout"),
        ("POST", "/auth/password/change"),
        ("POST", MCP_API_PATH),
        # 自分の API キーの発行・一覧・失効（#1805）。公開 path ではないのでログインは要る。
        ("GET", "/account/api-keys"),
        ("POST", "/account/api-keys"),
        ("DELETE", "/account/api-keys/{key_id}"),
        # DB の状態の確認は、ログインした全員が使う（読み込みの失敗の画面）。
        # 公開 path ではないのでログインは要る。
        ("GET", "/nl2sql/persistence"),
    }
)


def _allowed(*codes: str) -> frozenset[str]:
    return frozenset(codes)


_SECURITY_USERS = _allowed("menu.security_users")
_SECURITY_ROLES = _allowed("menu.security_roles")
_SECURITY_ROLE_READ = _allowed(
    "menu.security_users", "menu.security_roles", "menu.security_permissions"
)
_SECURITY_PERMISSIONS = _allowed("menu.security_permissions")

# ユーザー管理・ロール管理（platform の共通 router）とロールへの権限付与の API。
# 前方一致で割り当てず、(METHOD, route template) ごとに登録する（`/api` は付けない）。
# `/security/users`・`/security/roles` 配下に登録のない route は拒否する（#503。RAG は #476）。
SECURITY_USER_ROLE_ROUTE_PERMISSIONS: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/security/users"): _SECURITY_USERS,
    ("POST", "/security/users"): _SECURITY_USERS,
    ("GET", "/security/users/{user_uuid}"): _SECURITY_USERS,
    ("PATCH", "/security/users/{user_uuid}"): _SECURITY_USERS,
    ("DELETE", "/security/users/{user_uuid}"): _SECURITY_USERS,
    ("POST", "/security/users/{user_uuid}/disable"): _SECURITY_USERS,
    ("POST", "/security/users/{user_uuid}/enable"): _SECURITY_USERS,
    ("POST", "/security/users/{user_uuid}/reset-password"): _SECURITY_USERS,
    ("POST", "/security/users/{user_uuid}/unlock"): _SECURITY_USERS,
    # ロールの一覧・詳細は、ユーザー管理（ロールの割り当て）と権限管理の画面も読む。
    ("GET", "/security/roles"): _SECURITY_ROLE_READ,
    ("GET", "/security/roles/{role_id}"): _SECURITY_ROLE_READ,
    ("POST", "/security/roles"): _SECURITY_ROLES,
    ("PATCH", "/security/roles/{role_id}"): _SECURITY_ROLES,
    ("DELETE", "/security/roles/{role_id}"): _SECURITY_ROLES,
    ("POST", "/security/roles/{role_id}/archive"): _SECURITY_ROLES,
    ("POST", "/security/roles/{role_id}/restore"): _SECURITY_ROLES,
    # 権限の付与はロール管理から分けた権限管理だけが行う（NL2SQL の router。#206）。
    ("PUT", "/security/roles/{role_id}/permissions"): _SECURITY_PERMISSIONS,
}

# ---- ROUTE_PERMISSIONS で使う権限の組（いずれかを持てば通す） ----
_ADMIN_SQL = _allowed("menu.admin_sql")
_DB_ADMIN_CHAT = _allowed("menu.db_admin_chat")
_ANNOTATION_MANAGEMENT = _allowed("menu.annotation_management")
_COMMENT_MANAGEMENT = _allowed("menu.comment_management")
_EVALUATION = _allowed("menu.evaluation")
_FEEDBACK_MANAGE = _allowed(FEEDBACK_MANAGE_PERMISSION)
_GLOSSARY_RULES = _allowed("menu.glossary_rules", LEARNING_MATERIAL_MANAGE_PERMISSION)
_GLOBAL_RULES = _allowed("menu.global_rules", LEARNING_MATERIAL_MANAGE_PERMISSION)
_LEGACY_LEARNING_MATERIAL_READ = _GLOSSARY_RULES | _GLOBAL_RULES
_ONTOLOGY_BUILD = _allowed("menu.ontology_build")
_PROFILE_MANAGE = _allowed(PROFILE_MANAGE_PERMISSION)
_QUERY_GENERATE = _allowed(QUERY_GENERATE_PERMISSION)
_QUESTION_CLASSIFIER_MODELS = _allowed("menu.question_classifier_models")
_SAMPLE_DATA_MANAGE = _allowed(SAMPLE_DATA_MANAGE_PERMISSION)
_SCHEMA_READ = _allowed(SCHEMA_READ_PERMISSION)
_SECURITY_DEEPSEC = _allowed("menu.security_deepsec")
_SELECT_AI_ASSETS_MANAGE = _allowed(SELECT_AI_ASSETS_MANAGE_PERMISSION)
_SELECT_AI_ASSETS_READ = _allowed(SELECT_AI_ASSETS_READ_PERMISSION)
_SELECT_AI_ASSETS_REFRESH = _allowed(SELECT_AI_ASSETS_REFRESH_PERMISSION)
_SETTINGS_DATABASE = _allowed("menu.settings_database")
_SETTINGS_SELECT_AI_CREDENTIAL = _allowed("menu.settings_select_ai_credential")
_SETTINGS_MODEL = _allowed("menu.settings_model")
_SQL_EXECUTE = _allowed(SQL_EXECUTE_PERMISSION)
_SQL_TO_QUESTION = _allowed("menu.sql_to_question")
_QUERY_HISTORY_READ = _allowed(
    "menu.history", QUERY_GENERATE_PERMISSION, FEEDBACK_MANAGE_PERMISSION
)
_FEEDBACK_WRITE = _allowed(FEEDBACK_WRITE_PERMISSION, FEEDBACK_MANAGE_PERMISSION)
_DB_ADMIN_TABLE_DETAIL = _allowed(
    "menu.table_management",
    "menu.comment_management",
    "menu.annotation_management",
    "menu.domain_management",
)
_DB_ADMIN_VIEW_DETAIL = _allowed(
    "menu.view_management",
    "menu.comment_management",
    "menu.annotation_management",
    "menu.domain_management",
)
_DB_ADMIN_TABLE_DATA = _allowed("menu.table_management", "menu.data_management")
_DB_ADMIN_PREVIEW = _allowed(
    "menu.table_management",
    "menu.view_management",
    "menu.data_management",
    "menu.comment_management",
    "menu.annotation_management",
    "menu.domain_management",
)
_SYNTHETIC_DATA = _allowed("menu.sample_data", "menu.data_management")
_PROFILE_LEARNING_MATERIAL = _allowed(
    PROFILE_MANAGE_PERMISSION, LEARNING_MATERIAL_MANAGE_PERMISSION
)
_ONTOLOGY_CAPABILITY_READ = _allowed(
    PROFILE_READ_PERMISSION,
    "menu.ontology_build",
    "nl2sql.ontology.actions.execute",
    SQL_EXECUTE_PERMISSION,
)
_ONTOLOGY_READ = _allowed("menu.ontology_build", PROFILE_READ_PERMISSION, QUERY_GENERATE_PERMISSION)
_ONTOLOGY_WRITE = _allowed("menu.ontology_build", PROFILE_MANAGE_PERMISSION)
_ORACLE_SYNC = _allowed(PROFILE_MANAGE_PERMISSION, SELECT_AI_ASSETS_REFRESH_PERMISSION)
_SELECT_AI_FEEDBACK = _allowed(FEEDBACK_MANAGE_PERMISSION, SELECT_AI_ASSETS_MANAGE_PERMISSION)
_SELECT_AI_CREDENTIAL_READINESS = (
    _ORACLE_SYNC | _QUERY_GENERATE | _SYNTHETIC_DATA | _EVALUATION | _SELECT_AI_ASSETS_MANAGE
)

# (METHOD, route template) → 許可する権限（いずれか）。`/api` は付けない。
# 前方一致では割り当てない。登録のない route は `UNCLASSIFIED_PERMISSION` になり、
# 起動時の manifest の検査（`app.main._assert_route_manifest`）で失敗し、実行時も拒否する
# （#510。RAG は #476）。
# 権限なしで通す操作は OPEN_API_OPERATIONS に置き、ここには登録しない。
ROUTE_PERMISSIONS: dict[tuple[str, str], frozenset[str]] = {
    # ---- ユーザーとロール（platform の共通 router。#503） ----
    **SECURITY_USER_ROLE_ROUTE_PERMISSIONS,
    # 全員の API キーの一覧と失効（管理の画面。platform の共通 router。#1805）。
    ("GET", "/security/api-keys"): _allowed("menu.security_api_keys"),
    ("DELETE", "/security/api-keys/{key_id}"): _allowed("menu.security_api_keys"),
    # ---- セキュリティ設定: 権限管理・業務プロファイル利用権限 ----
    ("GET", "/security/permissions"): _SECURITY_PERMISSIONS,
    ("GET", "/security/profile-access/profiles"): _SECURITY_PERMISSIONS,
    # ---- セキュリティ設定: Deep Data Security ----
    ("PATCH", "/security/deepsec/config"): _SECURITY_DEEPSEC,
    ("POST", "/security/deepsec/config/sync-password"): _SECURITY_DEEPSEC,
    ("GET", "/security/deepsec/data-entitlements"): _SECURITY_DEEPSEC,
    ("PATCH", "/security/deepsec/data-entitlements/{role_id}"): _SECURITY_DEEPSEC,
    ("POST", "/security/deepsec/data-entitlements/{role_id}/apply"): _SECURITY_DEEPSEC,
    ("POST", "/security/deepsec/data-entitlements/{role_id}/preview"): _SECURITY_DEEPSEC,
    ("GET", "/security/deepsec/plan"): _SECURITY_DEEPSEC,
    ("POST", "/security/deepsec/plan/{version}/reset"): _SECURITY_DEEPSEC,
    ("POST", "/security/deepsec/plan/{version}/steps/{step_no}/apply"): _SECURITY_DEEPSEC,
    ("GET", "/security/deepsec/relations"): _SECURITY_DEEPSEC,
    ("GET", "/security/deepsec/scope-profiles"): _SECURITY_DEEPSEC,
    ("GET", "/security/deepsec/status"): _SECURITY_DEEPSEC,
    ("GET", "/security/deepsec/target-objects"): _SECURITY_DEEPSEC,
    ("GET", "/security/deepsec/target-objects/{owner}/{object_name}"): _SECURITY_DEEPSEC,
    ("POST", "/security/deepsec/verify"): _SECURITY_DEEPSEC,
    # ---- システム設定: アップロード保存先 ----
    ("GET", "/settings/upload-storage"): _allowed("menu.settings_upload_storage"),
    ("PATCH", "/settings/upload-storage"): _allowed("menu.settings_upload_storage"),
    # ---- システム設定: モデル ----
    ("GET", "/settings/model"): _SETTINGS_MODEL,
    ("PATCH", "/settings/model"): _SETTINGS_MODEL,
    ("POST", "/settings/model/test"): _SETTINGS_MODEL,
    # ---- システム設定: システムテーブル ----
    ("GET", "/settings/database/system-tables"): _allowed("menu.settings_system_tables"),
    ("POST", "/settings/database/system-tables/initialize"): _allowed(
        "menu.settings_system_tables"
    ),
    # ---- システム設定: データベース ----
    ("GET", "/settings/database"): _SETTINGS_DATABASE,
    ("PATCH", "/settings/database"): _SETTINGS_DATABASE,
    # ---- 運用設定: 回答の注意書き（設定は製品ごと。#2449） ----
    ("GET", "/settings/answer-notice"): _allowed("menu.settings_answer_notice"),
    ("PUT", "/settings/answer-notice"): _allowed("menu.settings_answer_notice"),
    # ---- 運用設定: チャットの添付（設定は製品ごと。#2517） ----
    ("GET", "/settings/trace-recording"): _allowed("menu.settings_trace_recording"),
    ("PUT", "/settings/trace-recording"): _allowed("menu.settings_trace_recording"),
    ("GET", "/settings/chat-attachments"): _allowed("menu.settings_chat_attachments"),
    ("PUT", "/settings/chat-attachments"): _allowed("menu.settings_chat_attachments"),
    ("POST", "/settings/database/password/reveal"): _SETTINGS_DATABASE,
    # Select AI Credential は運用設定の専用の画面（#658）。
    ("GET", "/settings/database/select-ai-credential"): _SETTINGS_SELECT_AI_CREDENTIAL,
    ("POST", "/settings/database/select-ai-credential"): _SETTINGS_SELECT_AI_CREDENTIAL,
    ("POST", "/settings/database/test"): _SETTINGS_DATABASE,
    ("POST", "/settings/database/wallet"): _SETTINGS_DATABASE,
    # ---- DB 構造（schema catalog） ----
    ("GET", "/schema/catalog"): _SCHEMA_READ,
    ("GET", "/schema/catalog/head"): _SCHEMA_READ,
    ("GET", "/schema/objects"): _SCHEMA_READ,
    ("GET", "/schema/objects/{owner}/{object_name}"): _SCHEMA_READ,
    ("GET", "/schema/owners"): _SCHEMA_READ,
    ("POST", "/schema/refresh-jobs"): _allowed(SCHEMA_REFRESH_PERMISSION),
    ("GET", "/schema/refresh-jobs/active"): _SCHEMA_READ,
    ("GET", "/schema/refresh-jobs/{job_id}"): _SCHEMA_READ,
    # ---- システムの状態（`GET /nl2sql/persistence` は OPEN_API_OPERATIONS） ----
    ("POST", "/nl2sql/persistence/recover"): _allowed(PERSISTENCE_RECOVER_PERMISSION),
    ("GET", "/nl2sql/diagnostics"): _allowed(SYSTEM_STATUS_READ_PERMISSION),
    # ---- SQL 生成・実行 ----
    ("POST", "/nl2sql/preview"): _QUERY_GENERATE,
    ("POST", "/nl2sql/jobs"): _QUERY_GENERATE,
    # この環境で使える生成の方式（業務プロファイルの既定の生成方式の選択肢。#1524）。
    ("GET", "/nl2sql/engines"): _allowed(PROFILE_MANAGE_PERMISSION, QUERY_GENERATE_PERMISSION),
    ("GET", "/nl2sql/chats"): _QUERY_GENERATE,
    # チャットの添付（#2517）。会話の権限（持ち主・system_admin）は API が判定する。
    ("POST", "/chat-attachments"): _QUERY_GENERATE,
    ("GET", "/chat-attachments"): _QUERY_GENERATE,
    ("DELETE", "/chat-attachments/{attachment_id}"): _QUERY_GENERATE,
    ("GET", "/nl2sql/chats/{conversation_id}"): _QUERY_GENERATE,
    # 会話の名前の変更・削除（#1817）。本人の会話だけ（handler が利用者で絞る）。
    ("PATCH", "/nl2sql/chats/{conversation_id}"): _QUERY_GENERATE,
    ("DELETE", "/nl2sql/chats/{conversation_id}"): _QUERY_GENERATE,
    ("GET", "/nl2sql/jobs/{job_id}"): _QUERY_HISTORY_READ,
    # 処理の段階のイベント（polling・SSE。#1359）。ジョブの取得と同じ権限。
    ("GET", "/nl2sql/jobs/{job_id}/trace"): _QUERY_HISTORY_READ,
    ("POST", "/nl2sql/jobs/{job_id}/cancel"): _QUERY_HISTORY_READ,
    # チャットのターンの SQL の実行（#1154）。SQL 生成のジョブ・SELECT SQL の実行と同じ実行の権限。
    ("POST", "/nl2sql/jobs/{job_id}/execute"): _SQL_EXECUTE,
    # チャットのジョブの中で実行した結果の行の受け取り（#1176）。実行と同じ権限。
    ("POST", "/nl2sql/jobs/{job_id}/execution-result"): _SQL_EXECUTE,
    ("POST", "/nl2sql/execute"): _SQL_EXECUTE,
    ("POST", "/nl2sql/analyze"): _SQL_EXECUTE,
    ("POST", "/nl2sql/rewrite"): _QUERY_GENERATE,
    ("POST", "/nl2sql/recommend-profile"): _QUERY_GENERATE,
    ("POST", "/nl2sql/similar-history"): _QUERY_GENERATE,
    # ---- Ontology の query session（実行だけ SQL 実行の権限） ----
    ("POST", "/nl2sql/query-sessions"): _QUERY_GENERATE,
    ("GET", "/nl2sql/query-sessions/{session_id}"): _QUERY_GENERATE,
    ("POST", "/nl2sql/query-sessions/{session_id}/apply"): _QUERY_GENERATE,
    ("POST", "/nl2sql/query-sessions/{session_id}/cancel"): _QUERY_GENERATE,
    ("POST", "/nl2sql/query-sessions/{session_id}/clarification-answers"): _QUERY_GENERATE,
    ("POST", "/nl2sql/query-sessions/{session_id}/confirm-sql"): _QUERY_GENERATE,
    ("POST", "/nl2sql/query-sessions/{session_id}/execute"): _SQL_EXECUTE,
    ("POST", "/nl2sql/query-sessions/{session_id}/generate-sql"): _QUERY_GENERATE,
    ("POST", "/nl2sql/query-sessions/{session_id}/improvement-proposal"): _QUERY_GENERATE,
    ("PATCH", "/nl2sql/query-sessions/{session_id}/intent"): _QUERY_GENERATE,
    # ---- 履歴 ----
    ("GET", "/nl2sql/history"): _QUERY_HISTORY_READ,
    ("GET", "/nl2sql/history/{history_id}"): _QUERY_HISTORY_READ,
    ("GET", "/nl2sql/history/{history_id}/trace"): _QUERY_HISTORY_READ,
    # ---- フィードバック（一覧は管理、登録は利用者も） ----
    ("GET", "/nl2sql/feedback"): _FEEDBACK_MANAGE,
    ("POST", "/nl2sql/feedback"): _FEEDBACK_WRITE,
    ("GET", "/nl2sql/feedback-config"): _FEEDBACK_MANAGE,
    ("PATCH", "/nl2sql/feedback-config"): _FEEDBACK_MANAGE,
    ("GET", "/nl2sql/feedback-entries"): _FEEDBACK_MANAGE,
    ("POST", "/nl2sql/feedback-entries/delete"): _FEEDBACK_MANAGE,
    ("GET", "/nl2sql/feedback-index"): _FEEDBACK_MANAGE,
    ("POST", "/nl2sql/feedback-index/clear"): _FEEDBACK_MANAGE,
    ("POST", "/nl2sql/feedback-index/rebuild"): _FEEDBACK_MANAGE,
    ("POST", "/nl2sql/feedback/admin-review"): _FEEDBACK_MANAGE,
    ("DELETE", "/nl2sql/feedback/{history_id}"): _FEEDBACK_WRITE,
    # ---- データ準備: DB 管理（表・ビュー・データ・管理 SQL） ----
    # DB 管理チャット（#2264）。管理 SQL を実行の権限では代わりにしない。
    (
        "POST",
        "/nl2sql/db-admin-chat/conversations/{conversation_id}/turns/{turn_id}/analyze-error",
    ): _DB_ADMIN_CHAT,
    ("POST", "/nl2sql/db-admin-chat/execute"): _DB_ADMIN_CHAT,
    ("POST", "/nl2sql/db-admin-chat/generate"): _DB_ADMIN_CHAT,
    # DB 管理チャットの会話の履歴（#2319）。見え方はサービスが本人（system_admin は全員）に絞る。
    ("GET", "/nl2sql/db-admin-chat/conversations"): _DB_ADMIN_CHAT,
    ("GET", "/nl2sql/db-admin-chat/conversations/{conversation_id}"): _DB_ADMIN_CHAT,
    (
        "GET",
        "/nl2sql/db-admin-chat/conversations/{conversation_id}/turns/{turn_id}/trace",
    ): _DB_ADMIN_CHAT,
    ("PATCH", "/nl2sql/db-admin-chat/conversations/{conversation_id}"): _DB_ADMIN_CHAT,
    ("DELETE", "/nl2sql/db-admin-chat/conversations/{conversation_id}"): _DB_ADMIN_CHAT,
    (
        "PUT",
        "/nl2sql/db-admin-chat/conversations/{conversation_id}/turns/{turn_id}/plan",
    ): _DB_ADMIN_CHAT,
    ("POST", "/nl2sql/db-admin/analyze-error"): _ADMIN_SQL,
    ("POST", "/nl2sql/db-admin/drop-table"): _allowed("menu.table_management"),
    ("POST", "/nl2sql/db-admin/drop-view"): _allowed("menu.view_management"),
    ("POST", "/nl2sql/db-admin/execute"): _ADMIN_SQL,
    # ビュー管理の「JOIN/WHERE 条件抽出」だけが使う。画面と同じ権限にする（#934）。
    ("POST", "/nl2sql/db-admin/extract-join-where"): _allowed("menu.view_management"),
    ("POST", "/nl2sql/db-admin/import-tabular"): _DB_ADMIN_TABLE_DATA,
    ("GET", "/nl2sql/db-admin/objects"): DATA_PREP_MENUS,
    ("POST", "/nl2sql/db-admin/preview-data"): _DB_ADMIN_PREVIEW,
    ("POST", "/nl2sql/db-admin/preview-data/export.xlsx"): _DB_ADMIN_PREVIEW,
    ("POST", "/nl2sql/db-admin/statements"): _allowed(
        "menu.admin_sql",
        "menu.annotation_management",
        "menu.comment_management",
        "menu.data_management",
        "menu.domain_management",
        "menu.table_management",
        "menu.view_management",
    ),
    ("GET", "/nl2sql/db-admin/tables"): _DB_ADMIN_TABLE_DETAIL,
    ("GET", "/nl2sql/db-admin/tables/{table_name}"): _DB_ADMIN_TABLE_DETAIL,
    ("GET", "/nl2sql/db-admin/tables/{table_name}/export.xlsx"): _DB_ADMIN_TABLE_DETAIL,
    ("POST", "/nl2sql/db-admin/truncate-table"): _DB_ADMIN_TABLE_DATA,
    ("POST", "/nl2sql/db-admin/upload-csv"): _allowed("menu.data_management"),
    ("GET", "/nl2sql/db-admin/views"): _DB_ADMIN_VIEW_DETAIL,
    ("GET", "/nl2sql/db-admin/views/{view_name}"): _DB_ADMIN_VIEW_DETAIL,
    ("GET", "/nl2sql/db-admin/views/{view_name}/export.xlsx"): _DB_ADMIN_VIEW_DETAIL,
    # ---- データ準備: コメント・アノテーション・ドメイン ----
    ("POST", "/nl2sql/comments/apply"): _COMMENT_MANAGEMENT,
    ("POST", "/nl2sql/comments/generate-sql"): _COMMENT_MANAGEMENT,
    ("POST", "/nl2sql/comments/suggest"): _COMMENT_MANAGEMENT,
    ("POST", "/nl2sql/annotations/apply"): _ANNOTATION_MANAGEMENT,
    ("POST", "/nl2sql/annotations/generate"): _ANNOTATION_MANAGEMENT,
    ("POST", "/nl2sql/annotations/generate-sql"): _ANNOTATION_MANAGEMENT,
    ("POST", "/nl2sql/domains/generate-sql"): _allowed("menu.domain_management"),
    ("POST", "/nl2sql/domains/inventory"): _allowed("menu.domain_management"),
    ("POST", "/nl2sql/metadata-samples"): _allowed(
        "menu.annotation_management", "menu.comment_management", "menu.domain_management"
    ),
    # ---- データ準備: サンプルデータ・合成データ ----
    ("GET", "/nl2sql/sample-data"): _SAMPLE_DATA_MANAGE,
    ("POST", "/nl2sql/sample-data/delete"): _SAMPLE_DATA_MANAGE,
    ("POST", "/nl2sql/sample-data/import"): _SAMPLE_DATA_MANAGE,
    ("POST", "/nl2sql/synthetic-data/generate"): _SYNTHETIC_DATA,
    ("GET", "/nl2sql/synthetic-data/results"): _SYNTHETIC_DATA,
    ("GET", "/nl2sql/synthetic-data/runs"): _SYNTHETIC_DATA,
    ("POST", "/nl2sql/synthetic-data/runs"): _SYNTHETIC_DATA,
    ("GET", "/nl2sql/synthetic-data/runs/{run_id}"): _SYNTHETIC_DATA,
    ("POST", "/nl2sql/synthetic-data/runs/{run_id}/apply"): _SYNTHETIC_DATA,
    ("POST", "/nl2sql/synthetic-data/runs/{run_id}/discard"): _SYNTHETIC_DATA,
    ("GET", "/nl2sql/synthetic-data/runs/{run_id}/results"): _SYNTHETIC_DATA,
    # ---- 業務プロファイル ----
    ("GET", "/nl2sql/profiles"): _PROFILE_MANAGE,
    ("POST", "/nl2sql/profiles"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/search"): _allowed(PROFILE_READ_PERMISSION),
    ("GET", "/nl2sql/profiles/{profile_id}"): _PROFILE_MANAGE,
    ("PATCH", "/nl2sql/profiles/{profile_id}"): _PROFILE_MANAGE,
    ("DELETE", "/nl2sql/profiles/{profile_id}"): _PROFILE_MANAGE,
    ("POST", "/nl2sql/profiles/{profile_id}/archive"): _PROFILE_MANAGE,
    (
        "GET",
        "/nl2sql/profiles/{profile_id}/learning-material/export.xlsx",
    ): _PROFILE_LEARNING_MATERIAL,
    ("POST", "/nl2sql/profiles/{profile_id}/learning-material/import"): _PROFILE_LEARNING_MATERIAL,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-build"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-build-jobs"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-capabilities"): _ONTOLOGY_CAPABILITY_READ,
    (
        "GET",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/executions/{execution_id}",
    ): _ONTOLOGY_CAPABILITY_READ,
    (
        "PATCH",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/{definition_id}/binding",
    ): _allowed("nl2sql.ontology.capabilities.manage"),
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/{definition_id}/execute",
    ): _allowed("nl2sql.ontology.actions.execute"),
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/{definition_id}/invoke",
    ): _SQL_EXECUTE,
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/{definition_id}/preview",
    ): _allowed("nl2sql.ontology.actions.execute"),
    (
        "GET",
        "/nl2sql/profiles/{profile_id}/ontology-capabilities/{definition_id}/previews/{preview_id}/outcome",
    ): _ONTOLOGY_CAPABILITY_READ,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-context/search"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-markdown"): _PROFILE_MANAGE,
    ("PATCH", "/nl2sql/profiles/{profile_id}/ontology-markdown/draft"): _PROFILE_MANAGE,
    (
        "GET",
        "/nl2sql/profiles/{profile_id}/ontology-markdown/preparations/{preparation_id}",
    ): _PROFILE_MANAGE,
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-markdown/preparations/{preparation_id}/validate-data",
    ): _PROFILE_MANAGE,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-markdown/prepare"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-markdown/publication-outcome"): _PROFILE_MANAGE,
    (
        "GET",
        "/nl2sql/profiles/{profile_id}/ontology-markdown/publications/{snapshot_id}/diagnostics",
    ): _PROFILE_MANAGE,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-markdown/publish"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-proposals"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-published"): _ONTOLOGY_READ,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-releases/{release_id}"): _ONTOLOGY_READ,
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-releases/{release_id}/rollback",
    ): _ONTOLOGY_WRITE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-results"): _ONTOLOGY_READ,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}"): _ONTOLOGY_READ,
    ("PATCH", "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}"): _ONTOLOGY_WRITE,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/analyze"): _ONTOLOGY_WRITE,
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/changes/{change_id}/apply",
    ): _ONTOLOGY_WRITE,
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/conflicts/{index}/resolve",
    ): _ONTOLOGY_WRITE,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/notes"): _ONTOLOGY_WRITE,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/publish"): _ONTOLOGY_WRITE,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/review"): _ONTOLOGY_WRITE,
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/validate",
    ): _ONTOLOGY_WRITE,
    (
        "POST",
        "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/validation-jobs",
    ): _ONTOLOGY_WRITE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-results/{result_id}/workspace"): _ONTOLOGY_READ,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-source-documents"): _PROFILE_MANAGE,
    (
        "DELETE",
        "/nl2sql/profiles/{profile_id}/ontology-source-documents/{source_document_id}",
    ): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-validation-jobs/{job_id}"): _ONTOLOGY_READ,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-view"): _PROFILE_MANAGE,
    ("PATCH", "/nl2sql/profiles/{profile_id}/ontology-view"): _PROFILE_MANAGE,
    ("POST", "/nl2sql/profiles/{profile_id}/ontology-view/materialize"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/ontology-view/mermaid"): _PROFILE_MANAGE,
    ("POST", "/nl2sql/profiles/{profile_id}/oracle-sync-jobs"): _PROFILE_MANAGE,
    ("POST", "/nl2sql/profiles/{profile_id}/restore"): _PROFILE_MANAGE,
    ("POST", "/nl2sql/profiles/{profile_id}/select-ai-profile"): _PROFILE_MANAGE,
    ("GET", "/nl2sql/profiles/{profile_id}/usage-context"): _allowed(PROFILE_READ_PERMISSION),
    # ---- 学習素材（旧形式。全体の用語集・共通ルール）・デモ学習 ----
    # 用語・同義語は用語集だけ、共通ルールはルールだけを取り込める（#1006）。
    ("GET", "/nl2sql/legacy-learning-material"): _LEGACY_LEARNING_MATERIAL_READ,
    ("GET", "/nl2sql/legacy-learning-material/rules/export.xlsx"): _GLOBAL_RULES,
    ("POST", "/nl2sql/legacy-learning-material/rules/import"): _GLOBAL_RULES,
    ("GET", "/nl2sql/legacy-learning-material/terms/export.xlsx"): _GLOSSARY_RULES,
    ("POST", "/nl2sql/legacy-learning-material/terms/import"): _GLOSSARY_RULES,
    ("POST", "/nl2sql/demo/learning"): _allowed(
        FEEDBACK_MANAGE_PERMISSION, LEARNING_MATERIAL_MANAGE_PERMISSION
    ),
    # ---- Ontology の構築 ----
    ("GET", "/nl2sql/ontology-build/{job_id}"): _ONTOLOGY_BUILD,
    ("POST", "/nl2sql/ontology-build/{job_id}/cancel"): _ONTOLOGY_BUILD,
    ("POST", "/nl2sql/ontology-build/{job_id}/retry"): _ONTOLOGY_BUILD,
    ("GET", "/nl2sql/ontology-publish/{job_id}"): _ONTOLOGY_BUILD,
    ("POST", "/nl2sql/ontology/profile-recommendations"): _ONTOLOGY_BUILD,
    (
        "POST",
        "/nl2sql/ontology/profile-recommendations/{recommendation_id}/confirm",
    ): _ONTOLOGY_BUILD,
    ("POST", "/nl2sql/ontology/proposals/batch-accept"): _ONTOLOGY_BUILD,
    ("GET", "/nl2sql/ontology/proposals/{proposal_id}"): _ONTOLOGY_BUILD,
    ("POST", "/nl2sql/ontology/proposals/{proposal_id}/accept"): _ONTOLOGY_BUILD,
    ("POST", "/nl2sql/ontology/proposals/{proposal_id}/reject"): _ONTOLOGY_BUILD,
    ("GET", "/nl2sql/ontology/revisions"): _ONTOLOGY_BUILD,
    ("GET", "/nl2sql/ontology/revisions/current"): _ONTOLOGY_BUILD,
    ("GET", "/nl2sql/ontology/revisions/{revision_id}"): _ONTOLOGY_BUILD,
    ("POST", "/nl2sql/ontology/revisions/{revision_id}/drafts"): _ONTOLOGY_BUILD,
    ("POST", "/nl2sql/ontology/revisions/{revision_id}/publish"): _ONTOLOGY_BUILD,
    # ---- Oracle への同期 ----
    # Select AI の Credential が作成済みか（#1683 / #1688）。業務プロファイルの保存（Oracle 反映）・
    # SQL 生成とチャット・合成データ・SQL 生成評価・Select AI の資産の画面が、操作の前に読む。
    ("GET", "/nl2sql/select-ai/credential-readiness"): _SELECT_AI_CREDENTIAL_READINESS,
    ("GET", "/nl2sql/oracle-sync-jobs/{job_id}"): _ORACLE_SYNC,
    ("POST", "/nl2sql/oracle-sync-jobs/{job_id}/retry"): _ORACLE_SYNC,
    # ---- Select AI / Select AI Agent の資産 ----
    ("GET", "/nl2sql/select-ai-agent/assets"): _SELECT_AI_ASSETS_READ,
    ("POST", "/nl2sql/select-ai-agent/assets/cleanup"): _SELECT_AI_ASSETS_MANAGE,
    ("POST", "/nl2sql/select-ai-agent/assets/refresh"): _SELECT_AI_ASSETS_REFRESH,
    ("GET", "/nl2sql/select-ai-agent/conversations"): _SELECT_AI_ASSETS_READ,
    ("POST", "/nl2sql/select-ai-agent/conversations/create"): _SELECT_AI_ASSETS_MANAGE,
    ("GET", "/nl2sql/select-ai-agent/privileges/check"): _SELECT_AI_ASSETS_READ,
    ("POST", "/nl2sql/select-ai-agent/run-team"): _SELECT_AI_ASSETS_MANAGE,
    ("POST", "/nl2sql/select-ai-agent/run-tool"): _SELECT_AI_ASSETS_MANAGE,
    ("POST", "/nl2sql/select-ai/assets/cleanup"): _SELECT_AI_ASSETS_MANAGE,
    ("GET", "/nl2sql/select-ai/db-profile-refresh-jobs/{job_id}"): _SELECT_AI_ASSETS_READ,
    ("GET", "/nl2sql/select-ai/db-profiles"): _SELECT_AI_ASSETS_READ,
    ("POST", "/nl2sql/select-ai/db-profiles"): _SELECT_AI_ASSETS_MANAGE,
    ("POST", "/nl2sql/select-ai/db-profiles/refresh-jobs"): _SELECT_AI_ASSETS_REFRESH,
    ("GET", "/nl2sql/select-ai/db-profiles/{profile_name}"): _SELECT_AI_ASSETS_READ,
    ("PATCH", "/nl2sql/select-ai/db-profiles/{profile_name}"): _SELECT_AI_ASSETS_MANAGE,
    ("POST", "/nl2sql/select-ai/db-profiles/{profile_name}/drop"): _SELECT_AI_ASSETS_MANAGE,
    ("GET", "/nl2sql/select-ai/feedback"): _SELECT_AI_FEEDBACK,
    ("POST", "/nl2sql/select-ai/feedback/add"): _SELECT_AI_FEEDBACK,
    ("POST", "/nl2sql/select-ai/feedback/delete"): _SELECT_AI_FEEDBACK,
    ("POST", "/nl2sql/select-ai/feedback/vector-index"): _SELECT_AI_FEEDBACK,
    ("GET", "/nl2sql/select-ai/profiles/export.json"): _SELECT_AI_ASSETS_MANAGE,
    ("POST", "/nl2sql/select-ai/profiles/import-json"): _SELECT_AI_ASSETS_MANAGE,
    ("POST", "/nl2sql/select-ai/profiles/refresh"): _SELECT_AI_ASSETS_REFRESH,
    # ---- 質問分類モデル ----
    ("GET", "/nl2sql/classifier"): _QUESTION_CLASSIFIER_MODELS,
    ("POST", "/nl2sql/classifier/model/import"): _QUESTION_CLASSIFIER_MODELS,
    ("POST", "/nl2sql/classifier/models/import"): _QUESTION_CLASSIFIER_MODELS,
    ("POST", "/nl2sql/classifier/predict"): _QUESTION_CLASSIFIER_MODELS,
    ("POST", "/nl2sql/classifier/train"): _QUESTION_CLASSIFIER_MODELS,
    ("GET", "/nl2sql/classifier/training-candidates"): _QUESTION_CLASSIFIER_MODELS,
    ("GET", "/nl2sql/classifier/training-data"): _QUESTION_CLASSIFIER_MODELS,
    ("GET", "/nl2sql/classifier/training-data/export.xlsx"): _QUESTION_CLASSIFIER_MODELS,
    ("POST", "/nl2sql/classifier/training-data/from-feedback"): _QUESTION_CLASSIFIER_MODELS,
    ("POST", "/nl2sql/classifier/training-data/import"): _QUESTION_CLASSIFIER_MODELS,
    ("PATCH", "/nl2sql/classifier/training-data/{example_id}"): _QUESTION_CLASSIFIER_MODELS,
    ("DELETE", "/nl2sql/classifier/training-data/{example_id}"): _QUESTION_CLASSIFIER_MODELS,
    # ---- 品質評価 ----
    ("GET", "/nl2sql/quality-evaluations"): _EVALUATION,
    ("POST", "/nl2sql/quality-evaluations"): _EVALUATION,
    ("GET", "/nl2sql/quality-evaluations/capabilities"): _EVALUATION,
    ("GET", "/nl2sql/quality-evaluations/template.xlsx"): _EVALUATION,
    ("GET", "/nl2sql/quality-evaluations/{job_id}"): _EVALUATION,
    ("DELETE", "/nl2sql/quality-evaluations/{job_id}"): _EVALUATION,
    ("POST", "/nl2sql/quality-evaluations/{job_id}/cancel"): _EVALUATION,
    ("GET", "/nl2sql/quality-evaluations/{job_id}/results"): _EVALUATION,
    ("GET", "/nl2sql/quality-evaluations/{job_id}/results.xlsx"): _EVALUATION,
    # ---- SQL から質問 ----
    ("POST", "/nl2sql/reverse"): _SQL_TO_QUESTION,
    ("POST", "/nl2sql/reverse/deep"): _SQL_TO_QUESTION,
    ("POST", "/nl2sql/reverse/question-sql"): _SQL_TO_QUESTION,
    ("POST", "/nl2sql/reverse/sql"): _SQL_TO_QUESTION,
}


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
    return _allowed(UNCLASSIFIED_PERMISSION)
