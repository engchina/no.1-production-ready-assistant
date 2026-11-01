"""Agent の権限カタログと API の権限 manifest（#215）。

- `PERMISSION_CATALOG`: ロールに付けられる権限（メニュー権限と capability）。
- `permission_for_route(method, path)`: API（method × route template）→ 必要な権限の集合
  （いずれか 1 つを持てばよい）。**既定は拒否**で、登録外の API は `UNCLASSIFIED_PERMISSION`
  を返し、認可で 403 にする。公開 API とログインだけで使える API は None を返す。
- capability は従来の 5 ロール（viewer / operator / approver / auditor / admin）に対応する
  （`CAPABILITY_ROLES`）。Cookie のセッションの利用者は capability から従来のロールを作り、
  router の既存の判定（`require_*`・対象範囲の絞り込み）にそのまま流す。

manifest の読み取り API は「その API を使う画面のメニュー権限」で許可し、Run・監査などの実データは
router の `require_*`（capability から作ったロール）でも確認する。つまり画面の表示にはメニュー権限、
実データの閲覧・操作には capability が必要になる。capability は関連するメニューを暗黙に含む
（implies）ため、capability だけを付けたロールでも画面を開ける。

システム設定・ユーザーとロールの共通メニューは NL2SQL / RAG と同じコード・グループ名を使う
（3 製品共通の画面。#206）。権限管理は「セキュリティ設定」の製品固有メニュー
（セクション名は 3 製品で同じ。#658）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pr_system_settings.auth.dependencies import UNCLASSIFIED_PERMISSION as UNCLASSIFIED_PERMISSION


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


# ---- メニュー権限（agent/frontend の nav-config.ts と同じ並び。コードは保存値なので変えない） ----

# 業務利用者のチャット（#768）。
MENU_CHAT = "menu.chat"
MENU_RUNS = "menu.runs"
MENU_APPROVALS = "menu.approvals"
MENU_AGENTS = "menu.agents"
MENU_SKILLS = "menu.skills"
MENU_EVALUATION = "menu.evaluation"
MENU_FEEDBACK = "menu.feedback"
MENU_USAGE = "menu.usage"
MENU_AUDIT = "menu.audit"
MENU_SECURITY_PERMISSIONS = "menu.security_permissions"
MENU_SETTINGS_SYSTEM_TABLES = "menu.settings_system_tables"
MENU_RUNTIMES = "menu.runtimes"
# MCP 接続（#757。旧「外部 MCP」。権限コードは保存値なので変えない）。
MENU_SETTINGS_EXTERNAL_MCP = "menu.settings_external_mcp"
MENU_SECURITY_USERS = "menu.security_users"
MENU_SECURITY_ROLES = "menu.security_roles"
MENU_SECURITY_API_KEYS = "menu.security_api_keys"
MENU_SETTINGS_UPLOAD_STORAGE = "menu.settings_upload_storage"
MENU_SETTINGS_MODEL = "menu.settings_model"
MENU_SETTINGS_DATABASE = "menu.settings_database"
# チャットの添付を使うか（設定は製品ごと。運用設定のシステムテーブルの下。#2517・#2546）。
MENU_SETTINGS_TRACE_RECORDING = "menu.settings_trace_recording"
MENU_SETTINGS_CHAT_ATTACHMENTS = "menu.settings_chat_attachments"
# 回答の注意書き（設定は製品ごと。運用設定のチャットの添付の下。#2405・#2449・#2546）。
MENU_SETTINGS_ANSWER_NOTICE = "menu.settings_answer_notice"

# ---- capability（従来の 5 ロールに対応） ----

RUNS_VIEW = "agent.runs.view"
RUNS_OPERATE = "agent.runs.operate"
APPROVALS_DECIDE = "agent.approvals.decide"
AUDIT_VIEW = "agent.audit.view"
ADMIN = "agent.admin"
# 自分の業務 Agent の作成（#2523）。従来の 5 ロールには対応させない（持ち主の判定は router）。
AGENTS_BUILD = "agent.agents.build"

# capability → 従来のロール（router の `require_*` と `ActorPolicy.roles` が使う名前）。
CAPABILITY_ROLES: dict[str, str] = {
    RUNS_VIEW: "viewer",
    RUNS_OPERATE: "operator",
    APPROVALS_DECIDE: "approver",
    AUDIT_VIEW: "auditor",
    ADMIN: "admin",
}

# グループ・名前・並び順は左のナビ（frontend の nav-config.ts と、i18n の
# サイドナビの表示名）と同じにする（#567 / #580。一致は
# tests/test_permission_catalog_nav.py が確かめる）。上に一般の利用者の画面（AI 活用）、
# 下に管理者の画面を置く（RAG / NL2SQL と同じ並び。#658 / #791）。
# ナビに無い capability は後ろに置き、グループは NL2SQL / RAG と同じ「参照権限 / 実行権限 /
# 管理権限」にする（#791）。
_GROUP_USE = "AI 活用"
_GROUP_BUILD = "Agent 構築"
_GROUP_IMPROVE = "改善・運用"
_GROUP_SECURITY = "セキュリティ設定"
_GROUP_OPERATIONS = "運用設定"
_GROUP_USERS_ROLES = "ユーザーとロール"
_GROUP_SETTINGS = "システム設定"
_GROUP_READ = "参照権限"
_GROUP_EXECUTE = "実行権限"
_GROUP_MANAGE = "管理権限"

_SYSTEM_SETTINGS_MENUS = (
    MENU_SETTINGS_UPLOAD_STORAGE,
    MENU_SETTINGS_MODEL,
    MENU_SETTINGS_DATABASE,
)
# agent.admin が暗黙に含むメニュー（ユーザーとロール・権限管理は含まない）。
_ADMIN_MENUS = (
    MENU_CHAT,
    MENU_APPROVALS,
    MENU_RUNS,
    MENU_AGENTS,
    MENU_SKILLS,
    MENU_EVALUATION,
    MENU_FEEDBACK,
    MENU_USAGE,
    MENU_AUDIT,
    MENU_SECURITY_API_KEYS,
    MENU_SETTINGS_SYSTEM_TABLES,
    MENU_SETTINGS_TRACE_RECORDING,
    MENU_SETTINGS_CHAT_ATTACHMENTS,
    MENU_SETTINGS_ANSWER_NOTICE,
    MENU_RUNTIMES,
    MENU_SETTINGS_EXTERNAL_MCP,
    *_SYSTEM_SETTINGS_MENUS,
)


PERMISSION_CATALOG: tuple[PermissionDefinition, ...] = (
    # 一般の利用者が毎日使う画面（NL2SQL の「AI 活用」と同じ名前。#791）。
    _menu_permission(MENU_CHAT, _GROUP_USE, "チャット"),
    # Run の一覧（NL2SQL の「実行履歴」と同じ名前。#791）。
    _menu_permission(MENU_APPROVALS, _GROUP_USE, "承認"),
    _menu_permission(MENU_RUNS, _GROUP_USE, "実行履歴"),
    # 管理者が業務 Agent を作る画面（RAG「ナレッジ構築」・NL2SQL「データ準備」に当たる。#791）。
    _menu_permission(MENU_AGENTS, _GROUP_BUILD, "業務 Agent"),
    _menu_permission(MENU_SKILLS, _GROUP_BUILD, "スキル"),
    # 業務のセクションの後の「改善・運用」（RAG / NL2SQL と同じ名前・位置。#658 / #776）。
    _menu_permission(MENU_EVALUATION, _GROUP_IMPROVE, "品質評価"),
    # 業務のセクションの後の「改善・運用」（RAG / NL2SQL と同じ名前・位置。#658 / #774）。
    _menu_permission(MENU_FEEDBACK, _GROUP_IMPROVE, "フィードバック"),
    # 利用状況（#772。RAG / NL2SQL に無い Agent の項目なので、共通の 2 項目の後ろ）。
    _menu_permission(MENU_USAGE, _GROUP_IMPROVE, "利用状況"),
    # 実行をまたぐツールの実行・承認・警告の記録（#791）。
    _menu_permission(MENU_AUDIT, _GROUP_IMPROVE, "監査ログ"),
    # 権限管理は Agent 固有。ユーザー管理・ロール管理は 3 製品共通の画面（#206）。
    _menu_permission(MENU_SECURITY_PERMISSIONS, _GROUP_SECURITY, "権限管理"),
    # 並びはサイドナビと同じ
    # （セキュリティ設定 → ユーザーとロール → 運用設定 → システム設定。#658）。
    _menu_permission(MENU_SECURITY_USERS, _GROUP_USERS_ROLES, "ユーザー管理"),
    _menu_permission(MENU_SECURITY_ROLES, _GROUP_USERS_ROLES, "ロール管理"),
    _menu_permission(MENU_SECURITY_API_KEYS, _GROUP_USERS_ROLES, "API キー"),
    # 運用設定の先頭はシステムテーブル（RAG / NL2SQL と同じ。#658 / #751）。
    _menu_permission(MENU_SETTINGS_SYSTEM_TABLES, _GROUP_OPERATIONS, "システムテーブル"),
    _menu_permission(MENU_SETTINGS_TRACE_RECORDING, _GROUP_OPERATIONS, "軌跡の記録"),
    _menu_permission(MENU_SETTINGS_CHAT_ATTACHMENTS, _GROUP_OPERATIONS, "チャットの添付"),
    _menu_permission(MENU_SETTINGS_ANSWER_NOTICE, _GROUP_OPERATIONS, "回答の注意書き"),
    # 組み込み Runtime の状態とモデルの確認（#754 / #791）。
    _menu_permission(MENU_RUNTIMES, _GROUP_OPERATIONS, "実行環境"),
    _menu_permission(MENU_SETTINGS_EXTERNAL_MCP, _GROUP_OPERATIONS, "MCP 接続"),
    _menu_permission(MENU_SETTINGS_UPLOAD_STORAGE, _GROUP_SETTINGS, "アップロード保存先"),
    _menu_permission(MENU_SETTINGS_MODEL, _GROUP_SETTINGS, "モデル"),
    _menu_permission(MENU_SETTINGS_DATABASE, _GROUP_SETTINGS, "データベース"),
    # capability は従来の 5 ロール（viewer / operator / approver / auditor / admin）に対応する。
    # 名前は利用者の言葉にし、ロール名は docs/security-rbac.md の対応表に書く（#791）。
    _permission(
        RUNS_VIEW,
        _GROUP_READ,
        "実行履歴の参照",
        "利用できる業務 Agent の実行（Run）・イベント・成果物を表示できます。",
        implies=(MENU_RUNS,),
    ),
    _permission(
        AUDIT_VIEW,
        _GROUP_READ,
        "監査ログの参照",
        "利用できる業務 Agent の実行の監査記録・ツール呼出し履歴を表示できます"
        "（実行履歴の参照を含みます）。",
        implies=(MENU_AUDIT,),
    ),
    _permission(
        RUNS_OPERATE,
        _GROUP_EXECUTE,
        "業務 Agent の実行",
        "利用できる業務 Agent でチャットと、実行（Run）の作成・取消・再開・再実行ができます"
        "（実行履歴の参照を含みます）。",
        implies=(MENU_RUNS, MENU_CHAT),
    ),
    _permission(
        APPROVALS_DECIDE,
        _GROUP_EXECUTE,
        "承認の判断",
        "利用できる業務 Agent の実行の承認・却下ができます（実行履歴の参照を含みます）。",
        implies=(MENU_APPROVALS,),
    ),
    _permission(
        AGENTS_BUILD,
        _GROUP_MANAGE,
        "自分の業務 Agent の作成",
        "自分の業務 Agent を作り、試し、公開して自分で使えます。ほかの業務 Agent・スキル・設定は"
        "変えられません。部署などに共有するには、Agent 管理の承認を申請します"
        "（業務 Agent の実行と品質評価を含みます）。",
        implies=(MENU_AGENTS, MENU_EVALUATION, RUNS_OPERATE),
    ),
    _permission(
        ADMIN,
        _GROUP_MANAGE,
        "Agent 管理",
        "業務 Agent・スキル・運用設定・システム設定の変更と、"
        "すべての操作ができます（業務 Agent の対象範囲の制限を受けません）。",
        implies=_ADMIN_MENUS,
    ),
)

ALL_PERMISSION_CODES = frozenset(item.code for item in PERMISSION_CATALOG)

# 廃止した権限コード。既存ロールに残る行は、システムテーブルの migration
# （`app.system_schema`）が削除する。
# 削除前でも `normalize_permission_codes` が捨てるため、実効権限・権限管理の表示と保存には現れない。
# - `menu.dashboard`: ダッシュボード機能の廃止（#262）。
# - `menu.settings_external_rag` / `menu.settings_external_nl2sql`: 外部 RAG / 外部 NL2SQL の画面を
#   MCP 接続（`menu.settings_external_mcp`）へまとめた（#757）。
# - `menu.settings_connection`: 中身のない「Agent 接続設定」の画面を削除した（#762）。
# - `menu.plugin_marketplaces`: プラグインの配布元の機能を削除し、プラグインの画面の
#   `menu.plugins` に置き換えた（#1798。migration 008 がロールの割り当てを引き継ぐ）。
# - `menu.plugins`: プラグインの機能を削除した（#1803。スキルだけを持つ）。
# - `menu.settings_api_keys`: Agent は外部に MCP を公開しないため、外部の MCP クライアント向けの
#   API キーの画面と `POST /mcp` を削除した（#1809）。
# - `menu.settings_runtime_snapshot`: 運用設定の「バックアップと復元」を削除した（#1961）。
# - `menu.automations`: 業務 Agent の定時実行の機能を削除した（#20。スケジューラと画面を消した）。
# - `menu.settings_appearance`: Nginx と自作 CA の TLS 証明書を配備から外し、システム設定の
#   「外観と証明書」の画面を削除した（#19）。
RETIRED_PERMISSION_CODES: tuple[str, ...] = (
    "menu.dashboard",
    "menu.settings_external_rag",
    "menu.settings_external_nl2sql",
    "menu.settings_connection",
    "menu.plugin_marketplaces",
    "menu.plugins",
    "menu.settings_api_keys",
    "menu.settings_runtime_snapshot",
    "menu.automations",
    "menu.settings_appearance",
)
PERMISSION_BY_CODE = {item.code: item for item in PERMISSION_CATALOG}
CAPABILITY_CODES = frozenset(CAPABILITY_ROLES)


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


def grants_all_targets(codes: Iterable[str]) -> bool:
    """`agent.admin` はすべてのエージェントを利用できる（対象範囲の制限を受けない）。"""
    return ADMIN in expand_permissions(codes)


def roles_for_permissions(codes: Iterable[str]) -> set[str]:
    """実効権限の capability → 従来のロール名（router の `require_*` が使う）。"""
    return {CAPABILITY_ROLES[code] for code in codes if code in CAPABILITY_ROLES}


# ---- API の権限 manifest ----

# 公開 path。`/ready/database` は画面の DB ゲートがログイン前に使う（接続先・資格情報は返さない。
# #325）。外部 Runtime の Binding の MCP（`/mcp/{binding_id}`）は #754 で削除した。
PUBLIC_API_PATHS = frozenset({"/health", "/ready", "/ready/database", "/auth/login"})
# 外部の MCP クライアントの API キー（`Authorization: Bearer prak_…`。#1874。共通の
# `PLATFORM_API_KEYS`、使える製品に agent）で認証する path。MCP の入口だけ。ほかの API にキーを
# 付けたら 401（画面用の Cookie / CSRF の API を機械から呼ばせない）。権限はツールごとに判定する
# （`features/agent/mcp_server.py`）。
MCP_API_PATH = "/mcp"
API_KEY_API_PATHS = frozenset({MCP_API_PATH})
# ログインだけで使える（メニュー権限を要しない）path。自分の API キーの発行・一覧・削除（#1874）。
LOGIN_ONLY_API_PATHS = frozenset({"/account/api-keys", "/account/api-keys/{key_id}"})
AUTHENTICATED_WITHOUT_PERMISSION = frozenset({"/auth/me", "/auth/logout", "/auth/password/change"})
# 権限なしで通す操作（method × path）。公開・ログインだけの path でも、ここにない method は
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
        # MCP（#1874）。キーの利用者として認証し、ツールごとに利用者の権限で判定する。
        ("POST", MCP_API_PATH),
        ("GET", "/account/api-keys"),
        ("POST", "/account/api-keys"),
        ("DELETE", "/account/api-keys/{key_id}"),
    }
)


def _any(*codes: str) -> frozenset[str]:
    return frozenset(codes)


# router の `require_*` が受け付ける capability（manifest の書込み・操作 API に使う）。
_ADMIN_ONLY = _any(ADMIN)
_OPERATE = _any(RUNS_OPERATE, ADMIN)
# 業務 Agent の作成・管理（Agent 管理か、自分の業務 Agent の作成。持ち主の判定は router。#2523）。
_BUILD = _any(AGENTS_BUILD, ADMIN)
_DECIDE = _any(APPROVALS_DECIDE, ADMIN)
# 画面の読み取り（メニュー権限。capability は関連メニューを暗黙に含む）。
_RUN_LIST = _any(MENU_RUNS, MENU_APPROVALS)
_RUN_DETAIL = _any(MENU_RUNS, MENU_APPROVALS, MENU_AUDIT)
_RUN_PROGRESS = _any(MENU_CHAT, MENU_RUNS, MENU_APPROVALS, MENU_AUDIT)
# 業務 Agent を選ぶ・名前で示す画面（品質評価・フィードバックの絞り込み）も読む。
# 読めないと開いた直後の 403（経路の権限拒否）で権限なしの画面へ移る（#1113。対象範囲で絞る）。
_AGENT_READ = _any(
    MENU_AGENTS,
    MENU_RUNS,
    MENU_CHAT,
    MENU_EVALUATION,
    MENU_FEEDBACK,
)
_TOOL_READ = _any(MENU_AUDIT, ADMIN)
# 保存先の状態（#839）を出す画面のメニュー（業務 Agent・スキル・実行・
# 品質評価・実行環境・MCP 接続・システムテーブル）と、
# ツール権限（Agent 管理）。
_RUNTIME_STORAGE_READ = _any(
    MENU_AGENTS,
    MENU_SKILLS,
    MENU_RUNS,
    MENU_EVALUATION,
    MENU_RUNTIMES,
    MENU_SETTINGS_EXTERNAL_MCP,
    MENU_SETTINGS_SYSTEM_TABLES,
    ADMIN,
)
_SECURITY_ROLE_READ = _any(MENU_SECURITY_USERS, MENU_SECURITY_ROLES, MENU_SECURITY_PERMISSIONS)

_RUN = "/runs/{run_id}"

# (METHOD, route template) → 許可する権限（いずれか）。`/api` は付けない。
ROUTE_PERMISSIONS: dict[tuple[str, str], frozenset[str]] = {
    # ---- 観測性（画面からは使わない。運用スクリプトの確認と監査の閲覧者向け） ----
    ("GET", "/observability/status"): _any(MENU_AUDIT),
    ("GET", "/observability/events"): _any(MENU_AUDIT),
    ("POST", "/observability/export-retry/flush"): _ADMIN_ONLY,
    ("GET", "/settings/trace-policy"): _any(MENU_AUDIT),
    ("PATCH", "/settings/trace-policy"): _ADMIN_ONLY,
    # ツール定義は監査・ツール一覧・ツール権限（管理者だけの非表示画面）が読む。
    ("GET", "/tools"): _TOOL_READ,
    ("POST", "/tools/invoke"): _OPERATE,
    # ---- Control Plane: 業務 Agent ----
    ("GET", "/agents"): _AGENT_READ,
    ("POST", "/agents"): _BUILD,
    ("POST", "/agents/draft-suggestion"): _BUILD,
    ("PATCH", "/agents/{agent_id}"): _BUILD,
    ("DELETE", "/agents/{agent_id}"): _BUILD,
    # 業種テンプレート（#780。業務 Agent の新規作成の画面が読む）。
    ("GET", "/agent-templates"): _any(MENU_AGENTS),
    # 業務 Agent の公開・前の版に戻す（#770）。
    ("POST", "/agents/{agent_id}/publish"): _BUILD,
    # データの範囲に選べるプロファイル（#1378。編集者として RAG / NL2SQL の MCP に問い合わせる）。
    ("GET", "/agent-data-scopes/{connection}/candidates"): _BUILD,
    ("POST", "/agents/{agent_id}/versions/{version}/restore"): _BUILD,
    # 個人の業務 Agent の共有の申請（持ち主）と承認・却下（Agent 管理。#2523）。
    ("POST", "/agents/{agent_id}/share-request"): _BUILD,
    ("POST", "/agents/{agent_id}/share-request/approve"): _ADMIN_ONLY,
    ("POST", "/agents/{agent_id}/share-request/reject"): _ADMIN_ONLY,
    # スキルの更新の取り込み（#1867）。差分はAgent を読める人、取り込み・取り消しは公開と同じ。
    ("GET", "/agents/{agent_id}/skill-updates"): _AGENT_READ,
    ("POST", "/agent-skill-updates/publish"): _ADMIN_ONLY,
    ("POST", "/agent-skill-updates/rollback"): _ADMIN_ONLY,
    # ---- Control Plane: スキル ----
    ("GET", "/skills"): _any(MENU_SKILLS, MENU_AGENTS),
    ("GET", "/skills/{skill_id}"): _any(MENU_SKILLS, MENU_AGENTS),
    # Agent Skills の形の書き出し（#1874。読めるスキルと同じ権限）。
    ("GET", "/skills/{skill_id}/export"): _any(MENU_SKILLS, MENU_AGENTS),
    ("POST", "/skills"): _ADMIN_ONLY,
    ("POST", "/skills/reload"): _ADMIN_ONLY,
    ("PATCH", "/skills/{skill_id}"): _ADMIN_ONLY,
    ("DELETE", "/skills/{skill_id}"): _ADMIN_ONLY,
    # ---- Control Plane: 組み込み Runtime の状態 ----
    # ---- Control Plane: Run・承認・監査 ----
    # 組み込み Runtime の状態（#754）。
    # 実行履歴の「実行を作成」も、実行できる状態か（モデル未設定）を作成の前に確かめる（#1113）。
    ("GET", "/runtime/status"): _any(MENU_RUNTIMES, MENU_AGENTS, RUNS_OPERATE),
    # 保存先の状態（#839）。再起動で消えることを、定義・実行を作る画面と運用設定の画面が出す。
    ("GET", "/runtime/storage"): _RUNTIME_STORAGE_READ,
    ("GET", "/runs"): _RUN_LIST,
    ("POST", "/runs"): _OPERATE,
    # チャットの会話（#768）。会話は作った利用者だけが読む（handler が利用者で絞る）。
    ("GET", "/threads"): _any(MENU_CHAT),
    ("GET", "/threads/{thread_id}"): _any(MENU_CHAT),
    # 会話の名前の変更・削除（#1817）。作った利用者だけ（handler が利用者で絞る）。
    ("PATCH", "/threads/{thread_id}"): _any(MENU_CHAT),
    ("DELETE", "/threads/{thread_id}"): _any(MENU_CHAT),
    # チャットの添付（#2517）。会話の権限（持ち主・system_admin）は API が判定する。
    ("POST", "/chat-attachments"): _any(MENU_CHAT),
    ("GET", "/chat-attachments"): _any(MENU_CHAT),
    ("DELETE", "/chat-attachments/{attachment_id}"): _any(MENU_CHAT),
    ("GET", _RUN): _RUN_DETAIL,
    ("GET", f"{_RUN}/audit"): _RUN_DETAIL,
    ("GET", f"{_RUN}/artifacts"): _RUN_DETAIL,
    ("GET", f"{_RUN}/artifacts/{{artifact_id}}"): _RUN_DETAIL,
    # RAG の図の根拠を開く短命の URL（#1311。閲覧者として RAG に作らせる）。
    ("GET", f"{_RUN}/figure-url"): _RUN_DETAIL,
    # RAG の根拠の原文のプレビュー（#2389。閲覧者として RAG に作らせる）。
    ("GET", f"{_RUN}/source-preview"): _RUN_DETAIL,
    ("GET", f"{_RUN}/events"): _any(MENU_RUNS, MENU_APPROVALS),
    # チャットと実行の詳細の処理の経過（共通の span の表の snapshot。#1359・#3226）。チャットと、
    # Run を見る画面が読む（handler が Run の判定で絞る）。
    ("GET", f"{_RUN}/trace"): _RUN_PROGRESS,
    ("POST", f"{_RUN}/cancel"): _OPERATE,
    # 回答前の確認（#1839）に答える・やめる。Run を作った利用者だけ（handler が確かめる）。
    ("POST", f"{_RUN}/confirmations/{{confirmation_id}}"): _OPERATE,
    ("POST", f"{_RUN}/resume"): _OPERATE,
    ("POST", f"{_RUN}/replay"): _OPERATE,
    # 承認の一覧と詳細（#1897。全 Run を読まずに承認の要約・承認の Run だけを返す）。
    ("GET", "/approvals"): _RUN_LIST,
    ("GET", "/approvals/{approval_id}"): _RUN_LIST,
    ("POST", "/approvals/{approval_id}/decision"): _DECIDE,
    ("GET", "/audit/tool-calls"): _any(MENU_AUDIT),
    ("GET", "/audit/tool-calls.csv"): _any(MENU_AUDIT),
    # ---- 改善・運用 ----
    # 品質評価（#776。評価の Run は始めた利用者の Run。router が業務 Agent の対象範囲を確かめる）。
    ("GET", "/evaluation-sets"): _any(MENU_EVALUATION),
    ("POST", "/evaluation-sets"): _any(MENU_EVALUATION),
    ("GET", "/evaluation-sets/template.xlsx"): _any(MENU_EVALUATION),
    ("POST", "/evaluation-sets/parse-xlsx"): _any(MENU_EVALUATION),
    ("GET", "/evaluation-sets/{set_id}"): _any(MENU_EVALUATION),
    ("PUT", "/evaluation-sets/{set_id}"): _any(MENU_EVALUATION),
    ("DELETE", "/evaluation-sets/{set_id}"): _any(MENU_EVALUATION),
    ("GET", "/evaluation-sets/{set_id}/cases.xlsx"): _any(MENU_EVALUATION),
    ("POST", "/evaluation-sets/{set_id}/cases"): _any(MENU_EVALUATION),
    ("POST", "/evaluation-sets/from-template"): _any(MENU_EVALUATION),
    ("GET", f"{_RUN}/evaluation-case"): _any(MENU_EVALUATION),
    ("POST", "/evaluations"): _any(MENU_EVALUATION),
    ("GET", "/evaluations"): _any(MENU_EVALUATION),
    ("GET", "/evaluations/compare"): _any(MENU_EVALUATION),
    ("GET", "/evaluations/ablation"): _any(MENU_EVALUATION),
    ("GET", "/evaluations/{job_id}"): _any(MENU_EVALUATION),
    ("POST", "/evaluations/{job_id}/cancel"): _any(MENU_EVALUATION),
    ("DELETE", "/evaluations/{job_id}"): _any(MENU_EVALUATION),
    # チャットの回答への評価（#774。会話をした利用者だけ。router が作成者を確かめる）。
    ("PUT", f"{_RUN}/feedback"): _OPERATE,
    # 管理者の評価（#774。だれの回答にも付けられる）。
    ("PUT", f"{_RUN}/admin-review"): _ADMIN_ONLY,
    # ---- 改善・運用 ----
    # フィードバック（#774。集計の対象は Run の一覧と同じく利用できる業務 Agent の Run）。
    ("GET", "/feedback"): _any(MENU_FEEDBACK),
    # 確かめるべき実行（#1822。暗黙のシグナル。対象はフィードバックと同じ）。
    ("GET", "/feedback/signals"): _any(MENU_FEEDBACK),
    # 利用状況（#772。集計の対象は Run の一覧と同じく利用できる業務 Agent の Run）。
    ("GET", "/usage"): _any(MENU_USAGE),
    # ---- 運用設定 ----
    # システムテーブル（#751。NL2SQL と同じくメニュー権限で状態の確認と作成・更新）。
    ("GET", "/settings/database/system-tables"): _any(MENU_SETTINGS_SYSTEM_TABLES),
    ("POST", "/settings/database/system-tables/initialize"): _any(MENU_SETTINGS_SYSTEM_TABLES),
    # MCP 接続（#757）。一覧は Skill の編集（使う接続を選ぶ）からも読む。ツールの取得は接続の確認。
    ("GET", "/settings/mcp-connections"): _any(MENU_SETTINGS_EXTERNAL_MCP, MENU_SKILLS),
    ("POST", "/settings/mcp-connections"): _ADMIN_ONLY,
    ("PATCH", "/settings/mcp-connections/{server_id}"): _ADMIN_ONLY,
    ("DELETE", "/settings/mcp-connections/{server_id}"): _ADMIN_ONLY,
    ("GET", "/settings/mcp-connections/{server_id}/tools"): _any(MENU_SETTINGS_EXTERNAL_MCP),
    # ナビに出さない設定（ツール権限）は管理者だけ。
    ("GET", "/settings/tool-policy"): _ADMIN_ONLY,
    # ツール権限の画面のツール（MCP 接続のツールを含む。#1814）。
    ("GET", "/settings/tool-policy/tools"): _ADMIN_ONLY,
    ("PATCH", "/settings/tool-policy"): _ADMIN_ONLY,
    # ---- システム設定（3 製品共通。RAG / NL2SQL と同じくメニュー権限で保存・操作できる） ----
    ("GET", "/settings/upload-storage"): _any(MENU_SETTINGS_UPLOAD_STORAGE),
    ("PATCH", "/settings/upload-storage"): _any(MENU_SETTINGS_UPLOAD_STORAGE),
    ("GET", "/settings/model"): _any(MENU_SETTINGS_MODEL),
    ("PATCH", "/settings/model"): _any(MENU_SETTINGS_MODEL),
    ("POST", "/settings/model/test"): _any(MENU_SETTINGS_MODEL),
    ("GET", "/settings/database"): _any(MENU_SETTINGS_DATABASE),
    ("PATCH", "/settings/database"): _any(MENU_SETTINGS_DATABASE),
    ("POST", "/settings/database/wallet"): _any(MENU_SETTINGS_DATABASE),
    ("POST", "/settings/database/test"): _any(MENU_SETTINGS_DATABASE),
    # 回答の注意書き（設定は製品ごと。#2449）。
    ("GET", "/settings/answer-notice"): _any(MENU_SETTINGS_ANSWER_NOTICE),
    ("PUT", "/settings/answer-notice"): _any(MENU_SETTINGS_ANSWER_NOTICE),
    # チャットの添付を使うか（設定は製品ごと。#2517）。
    ("GET", "/settings/trace-recording"): _any(MENU_SETTINGS_TRACE_RECORDING),
    ("PUT", "/settings/trace-recording"): _any(MENU_SETTINGS_TRACE_RECORDING),
    ("GET", "/settings/chat-attachments"): _any(MENU_SETTINGS_CHAT_ATTACHMENTS),
    ("PUT", "/settings/chat-attachments"): _any(MENU_SETTINGS_CHAT_ATTACHMENTS),
    # ---- ユーザーとロール（platform の共通 router。RAG / NL2SQL と同じ割り当て）----
    # 前方一致で割り当てず、route ごとに登録する（新しい route は登録するまで拒否。#503）。
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
    # ---- セキュリティ設定: 権限管理 ----
    ("GET", "/security/permissions"): _any(MENU_SECURITY_PERMISSIONS),
    ("GET", "/security/access-targets/agents"): _any(MENU_SECURITY_PERMISSIONS),
    ("PUT", "/security/roles/{role_id}/access"): _any(MENU_SECURITY_PERMISSIONS),
    # 全員の API キーの一覧・削除（3 製品共通の管理メニュー）。
    ("GET", "/security/api-keys"): _any(MENU_SECURITY_API_KEYS),
    ("DELETE", "/security/api-keys/{key_id}"): _any(MENU_SECURITY_API_KEYS),
}


def permission_for_route(method: str, route_path: str) -> frozenset[str] | None:
    """method + route template（`/api` なし）→ 許可する権限の集合。

    None は公開 API・ログインだけで使える API・MCP（ツールごとに権限を判定）。登録外は
    `UNCLASSIFIED_PERMISSION`（拒否）。
    """
    method = method.upper()
    if (method, route_path) in OPEN_API_OPERATIONS:
        return None
    exact = ROUTE_PERMISSIONS.get((method, route_path))
    if exact is not None:
        return exact
    return _any(UNCLASSIFIED_PERMISSION)
