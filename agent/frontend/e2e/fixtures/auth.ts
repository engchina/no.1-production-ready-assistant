/**
 * e2e の認証・権限のデータ（#215）。
 *
 * 権限コードとカタログは backend `app/security/permissions.py` の `PERMISSION_CATALOG` と同じ
 * （`permission-catalog.spec.ts` が backend の定義・frontend の `lib/permissions.ts` と一致することを確かめる）。
 * mock-api.ts の既定の利用者はローカル（`PLATFORM_AUTH_MODE=local`）の全権限の利用者なので、
 * 認証を扱わない既存の spec はログインなしで全画面を使える。
 */

type Json = Record<string, unknown>;

export const MENU_PERMISSION_CODES = [
  "menu.chat",
  "menu.runs",
  "menu.approvals",
  "menu.agents",
  "menu.skills",
  "menu.evaluation",
  "menu.feedback",
  "menu.usage",
  "menu.audit",
  "menu.security_permissions",
  "menu.security_users",
  "menu.security_roles",
  "menu.security_api_keys",
  "menu.settings_system_tables",
  "menu.settings_trace_recording",
  "menu.settings_chat_attachments",
  "menu.settings_answer_notice",
  "menu.runtimes",
  "menu.settings_external_mcp",
  "menu.settings_upload_storage",
  "menu.settings_model",
  "menu.settings_database",
  "menu.settings_appearance",
] as const;

export const CAPABILITY_PERMISSION_CODES = [
  "agent.runs.view",
  "agent.audit.view",
  "agent.runs.operate",
  "agent.approvals.decide",
  "agent.agents.build",
  "agent.admin",
] as const;

export const ALL_PERMISSION_CODES: string[] = [...MENU_PERMISSION_CODES, ...CAPABILITY_PERMISSION_CODES];

const ADMIN_MENUS = [
  "menu.security_api_keys",
  "menu.chat",
  "menu.runs",
  "menu.approvals",
  "menu.agents",
  "menu.skills",
  "menu.evaluation",
  "menu.feedback",
  "menu.usage",
  "menu.audit",
  "menu.settings_system_tables",
  "menu.settings_trace_recording",
  "menu.settings_chat_attachments",
  "menu.settings_answer_notice",
  "menu.runtimes",
  "menu.settings_external_mcp",
  "menu.settings_upload_storage",
  "menu.settings_model",
  "menu.settings_database",
  "menu.settings_appearance",
];

const MENU_LABELS: Record<(typeof MENU_PERMISSION_CODES)[number], [group: string, label: string]> = {
  "menu.chat": ["AI 活用", "チャット"],
  "menu.approvals": ["AI 活用", "承認"],
  "menu.runs": ["AI 活用", "実行履歴"],
  "menu.agents": ["Agent 構築", "業務 Agent"],
  "menu.skills": ["Agent 構築", "スキル"],
  "menu.evaluation": ["改善・運用", "品質評価"],
  "menu.feedback": ["改善・運用", "フィードバック"],
  "menu.usage": ["改善・運用", "利用状況"],
  "menu.audit": ["改善・運用", "監査ログ"],
  "menu.security_permissions": ["セキュリティ設定", "権限管理"],
  "menu.settings_system_tables": ["運用設定", "システムテーブル"],
  "menu.settings_trace_recording": ["運用設定", "軌跡の記録"],
  "menu.settings_chat_attachments": ["運用設定", "チャットの添付"],
  "menu.settings_answer_notice": ["運用設定", "回答の注意書き"],
  "menu.runtimes": ["運用設定", "実行環境"],
  "menu.settings_external_mcp": ["運用設定", "MCP 接続"],
  "menu.security_users": ["ユーザーとロール", "ユーザー管理"],
  "menu.security_roles": ["ユーザーとロール", "ロール管理"],
  "menu.security_api_keys": ["ユーザーとロール", "API キー"],
  "menu.settings_upload_storage": ["システム設定", "アップロード保存先"],
  "menu.settings_model": ["システム設定", "モデル"],
  "menu.settings_database": ["システム設定", "データベース"],
  "menu.settings_appearance": ["システム設定", "外観と証明書"],
};

// capability のグループは NL2SQL / RAG と同じ「参照権限 / 実行権限 / 管理権限」（#791）。
const READ_GROUP = "参照権限";
const EXECUTE_GROUP = "実行権限";
const MANAGE_GROUP = "管理権限";

/** `GET /api/security/permissions` の応答（backend の PERMISSION_CATALOG と同じ並び・implies）。 */
export const PERMISSION_CATALOG: Json[] = [
  ...MENU_PERMISSION_CODES.map((code) => {
    const [group, label] = MENU_LABELS[code];
    return { code, group, label, description: `「${label}」の画面を表示し、関連操作を利用できます。`, implies: [] };
  }),
  {
    code: "agent.runs.view",
    group: READ_GROUP,
    label: "実行履歴の参照",
    description: "利用できる業務 Agent の実行（Run）・イベント・成果物を表示できます。",
    implies: ["menu.runs"],
  },
  {
    code: "agent.audit.view",
    group: READ_GROUP,
    label: "監査ログの参照",
    description: "利用できる業務 Agent の実行の監査記録・ツール呼出し履歴を表示できます（実行履歴の参照を含みます）。",
    implies: ["menu.audit"],
  },
  {
    code: "agent.runs.operate",
    group: EXECUTE_GROUP,
    label: "業務 Agent の実行",
    description:
      "利用できる業務 Agent でチャットと、実行（Run）の作成・取消・再開・再実行ができます（実行履歴の参照を含みます）。",
    implies: ["menu.runs", "menu.chat"],
  },
  {
    code: "agent.approvals.decide",
    group: EXECUTE_GROUP,
    label: "承認の判断",
    description: "利用できる業務 Agent の実行の承認・却下ができます（実行履歴の参照を含みます）。",
    implies: ["menu.approvals"],
  },
  {
    code: "agent.agents.build",
    group: MANAGE_GROUP,
    label: "自分の業務 Agent の作成",
    description:
      "自分の業務 Agent を作り、試し、公開して自分で使えます。ほかの業務 Agent・スキル・設定は変えられません。部署などに共有するには、Agent 管理の承認を申請します（業務 Agent の実行と品質評価を含みます）。",
    implies: ["menu.agents", "menu.evaluation", "agent.runs.operate"],
  },
  {
    code: "agent.admin",
    group: MANAGE_GROUP,
    label: "Agent 管理",
    description:
      "業務 Agent・スキル・運用設定・システム設定の変更と、すべての操作ができます（業務 Agent の対象範囲の制限を受けません）。",
    implies: ADMIN_MENUS,
  },
];

/** implies を展開した実効権限（backend の `expand_permissions` と同じ）。 */
export function expandPermissions(codes: readonly string[]): string[] {
  const byCode = new Map(PERMISSION_CATALOG.map((item) => [item.code as string, item.implies as string[]]));
  const expanded = new Set<string>();
  const pending = [...codes];
  while (pending.length) {
    const code = pending.pop() as string;
    if (expanded.has(code)) continue;
    expanded.add(code);
    pending.push(...(byCode.get(code) ?? []));
  }
  return [...expanded].sort();
}

export interface CurrentUserPayload {
  user_uuid: string;
  login_user_id: string;
  display_name: string;
  status: string;
  force_password_change: boolean;
  role_codes: string[];
  role_display_names: string[];
  is_system_admin: boolean;
  permissions: string[];
  allowed_agent_ids: string[] | null;
  debug_mode: boolean;
  password_change_allowed: boolean;
  /** システムの時間帯（PLATFORM_TIMEZONE）。無ければ画面は既定の UTC で表示する（#3274）。 */
  timezone?: string;
}

/** ローカル（`PLATFORM_AUTH_MODE=local`）の利用者。ログインなしで全画面を使える。 */
export const LOCAL_CURRENT_USER: CurrentUserPayload = {
  user_uuid: "00000000-0000-0000-0000-000000000000",
  login_user_id: "local",
  display_name: "ローカル利用者",
  status: "ACTIVE",
  force_password_change: false,
  role_codes: ["SYSTEM_ADMIN"],
  role_display_names: ["システム管理者"],
  is_system_admin: true,
  permissions: ALL_PERMISSION_CODES,
  allowed_agent_ids: null,
  debug_mode: true,
  password_change_allowed: false,
};

/**
 * ログイン済みの DB ユーザー。既定は権限なし・範囲なし。`permissions` は直接付けたコードを渡せば
 * implies を展開する（backend の `/auth/me` と同じ）。
 */
export function dbUser(overrides: Partial<CurrentUserPayload> = {}): CurrentUserPayload {
  const user: CurrentUserPayload = {
    user_uuid: "11111111-1111-1111-1111-111111111111",
    login_user_id: "user01",
    display_name: "利用者 一郎",
    status: "ACTIVE",
    force_password_change: false,
    role_codes: ["AGENT_USER"],
    is_system_admin: false,
    permissions: [],
    allowed_agent_ids: [],
    debug_mode: false,
    password_change_allowed: true,
    ...overrides,
    // ロールの表示名を渡さずに role_codes だけを変えた利用者は、表示名を code にする（表示名の無いロールと同じ）。
    role_display_names: overrides.role_display_names ?? [...(overrides.role_codes ?? ["AGENT_USER"])],
  };
  return { ...user, permissions: expandPermissions(user.permissions) };
}

export const SYSTEM_ADMIN_ROLE: Json = {
  role_id: "role-admin",
  role_code: "SYSTEM_ADMIN",
  display_name: "システム管理者",
  description: "すべての権限",
  is_built_in: true,
  archived: false,
  version: 1,
  permissions: [],
  agent_ids: [],
};

export const OPERATOR_ROLE: Json = {
  role_id: "role-operator",
  role_code: "AGENT_OPERATOR",
  display_name: "Agent 実行担当",
  description: "Run の実行と閲覧",
  is_built_in: false,
  archived: false,
  version: 3,
  permissions: ["agent.runs.operate", "menu.agents"],
  agent_ids: ["default"],
};

export const SECURITY_USERS: Json[] = [
  {
    user_uuid: "u-admin",
    login_user_id: "admin",
    display_name: "管理 太郎",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: ["role-admin"],
    assigned_roles: [
      { role_id: "role-admin", role_code: "SYSTEM_ADMIN", display_name: "システム管理者", is_built_in: true, archived: false },
    ],
    is_bootstrap_admin: true,
  },
  {
    user_uuid: "u-operator",
    login_user_id: "operator.user",
    display_name: "実行 花子",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 2,
    role_ids: ["role-operator"],
    assigned_roles: [
      {
        role_id: "role-operator",
        role_code: "AGENT_OPERATOR",
        display_name: "Agent 実行担当",
        is_built_in: false,
        archived: false,
      },
    ],
    is_bootstrap_admin: false,
  },
];

/**
 * 権限管理の対象の候補（`GET /api/security/access-targets/agents` の元データ。#608）。
 */
export const ACCESS_TARGETS: Json = {
  agents: [
    { id: "default", name: "資料・データ横断アシスタント", description: "既定 Agent", status: "enabled" },
    { id: "finance", name: "経理 Agent", description: null, status: "disabled" },
  ],
};
