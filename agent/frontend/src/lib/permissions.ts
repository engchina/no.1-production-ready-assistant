import { useMemo } from "react";
import { useAuth, type HasPermission } from "@production-ready/system-settings";

/**
 * Agent の権限コード（backend `app/security/permissions.py` の `PERMISSION_CATALOG` と同じ。#215）。
 *
 * - メニュー権限（`menu.*`）は画面の表示を許可する。ナビ項目とルートの保護に使う。
 * - capability（`agent.*`）は Run などの実データの閲覧とページ内の操作を許可する（従来の 5 ロール
 *   viewer / operator / approver / auditor / admin に対応）。backend は `implies` を展開済みの
 *   `permissions` を返すため、frontend は一覧に含まれるか（SYSTEM_ADMIN は常に true）だけを見る。
 * システム設定・ユーザーとロールのコードは NL2SQL / RAG と同じ（3製品共通の画面）。
 */
export const MENU_PERMISSIONS = {
  chat: "menu.chat",
  agents: "menu.agents",
  skills: "menu.skills",
  runtimes: "menu.runtimes",
  runs: "menu.runs",
  automations: "menu.automations",
  approvals: "menu.approvals",
  audit: "menu.audit",
  evaluation: "menu.evaluation",
  feedback: "menu.feedback",
  usage: "menu.usage",
  settingsSystemTables: "menu.settings_system_tables",
  settingsExternalMcp: "menu.settings_external_mcp",
  settingsUploadStorage: "menu.settings_upload_storage",
  settingsModel: "menu.settings_model",
  settingsDatabase: "menu.settings_database",
  settingsAppearance: "menu.settings_appearance",
  settingsAnswerNotice: "menu.settings_answer_notice",
  settingsTraceRecording: "menu.settings_trace_recording",
  settingsChatAttachments: "menu.settings_chat_attachments",
  securityUsers: "menu.security_users",
  securityRoles: "menu.security_roles",
  securityApiKeys: "menu.security_api_keys",
  securityPermissions: "menu.security_permissions",
} as const;

export const CAPABILITY_PERMISSIONS = {
  /** Run・イベント・成果物の閲覧（viewer）。 */
  runsView: "agent.runs.view",
  /** Run の作成・取消・再開・再実行（operator）。 */
  runsOperate: "agent.runs.operate",
  /** 承認・却下（approver）。 */
  approvalsDecide: "agent.approvals.decide",
  /** 監査記録・ツール呼出し履歴の閲覧（auditor）。 */
  auditView: "agent.audit.view",
  /** 自分の業務 Agent の作成（#2523。持ち主として自分の Agent だけを管理する）。 */
  buildAgents: "agent.agents.build",
  /** 業務 Agent・スキル・運用設定・システム設定の変更とすべての操作（admin）。 */
  admin: "agent.admin",
} as const;

export type MenuPermission = (typeof MENU_PERMISSIONS)[keyof typeof MENU_PERMISSIONS];
export type CapabilityPermission =
  (typeof CAPABILITY_PERMISSIONS)[keyof typeof CAPABILITY_PERMISSIONS];

/**
 * 画面で使う capability の判定（backend の `require_*` と同じ組み合わせ）。
 * `agent.admin` はすべての操作を含む（backend の `_require_actor_roles` が admin を常に通す）。
 */
export interface AgentCapabilities {
  /** Control Plane の実データ（Run・Agent・スキル・Runtime など）を読める（viewer 相当のいずれか）。 */
  viewRuns: boolean;
  operateRuns: boolean;
  decideApprovals: boolean;
  viewAudit: boolean;
  /** 業務 Agent を作れる（Agent 管理か、自分の業務 Agent の作成。#2523）。 */
  buildAgents: boolean;
  admin: boolean;
}

export function agentCapabilities(hasPermission: HasPermission): AgentCapabilities {
  const admin = hasPermission(CAPABILITY_PERMISSIONS.admin);
  const operateRuns = admin || hasPermission(CAPABILITY_PERMISSIONS.runsOperate);
  const decideApprovals = admin || hasPermission(CAPABILITY_PERMISSIONS.approvalsDecide);
  const viewAudit = admin || hasPermission(CAPABILITY_PERMISSIONS.auditView);
  return {
    viewRuns:
      operateRuns || decideApprovals || viewAudit || hasPermission(CAPABILITY_PERMISSIONS.runsView),
    operateRuns,
    decideApprovals,
    viewAudit,
    buildAgents: admin || hasPermission(CAPABILITY_PERMISSIONS.buildAgents),
    admin,
  };
}

/** ログイン中の利用者の capability。ローカル DEBUG（SYSTEM_ADMIN）はすべて true。 */
export function useCapabilities(): AgentCapabilities {
  const { hasPermission } = useAuth();
  return useMemo(() => agentCapabilities(hasPermission), [hasPermission]);
}
