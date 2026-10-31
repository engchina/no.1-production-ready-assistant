import {
  arrangePermissionsByNav,
  permissionNavSections,
  type PermissionDefinition,
  type RolePermissionTargetItem,
  type RolePermissionTargetPage,
  type RolePermissionTargetQuery,
  type RolePermissionTargetSection,
  type RolePermissionsApi,
} from "@production-ready/system-settings";

import { NAV_SECTIONS } from "@/components/layout/nav-config";
import type { AccessTargetPage, AgentAccessTarget, SecurityRole } from "./api";
import { t } from "./i18n";
import { CAPABILITY_PERMISSIONS } from "./permissions";
import { securityApi } from "./security-api";

/**
 * 権限管理画面（共通の RolePermissionsPage）へ渡す Agent の保存 API と「利用できる対象」（#215）。
 * 対象はエージェントだけ。`agent.admin` を持つロールで全件が対象（backend も空に正規化する）。
 * 検索・回答プロファイルの判定は、RAG が Run の利用者のサービストークンで行う（#750）。
 */

/** 対象の key。要素 ID とテスト ID は `security-roles-<key>-*` になる。 */
export const AGENT_ACCESS_KEY = "agent-access";

/**
 * 権限管理の機能の一覧は、左のナビ（nav-config の NAV_SECTIONS）を正本にして、グループ・並び順・名前を
 * そろえる（#567）。ナビに無い権限（画面の中の操作を許可する capability の「参照権限 / 実行権限 / 管理権限」）は、
 * ナビの後ろに backend のカタログのまま置く。
 */
export function arrangeAgentPermissions(catalog: readonly PermissionDefinition[]): PermissionDefinition[] {
  return arrangePermissionsByNav(catalog, permissionNavSections(NAV_SECTIONS, t));
}

export const PERMISSIONS_API: RolePermissionsApi<SecurityRole> = {
  roles: (includeArchived, options) => securityApi.roles(includeArchived, options),
  permissions: (options) => securityApi.permissions(options).then(arrangeAgentPermissions),
  // 機能権限とエージェントの対象範囲を Agent の保存 API（PUT /access）へ送る。
  // 全件が対象のとき（SYSTEM_ADMIN・agent.admin）は共通画面が空の一覧を渡す。
  save: (role, draft) =>
    securityApi.updateRoleAccess({
      role_id: role.role_id,
      version: role.version,
      permissions: draft.permissions,
      agent_ids: draft.targets[AGENT_ACCESS_KEY] ?? [],
    }),
};

function agentTargetItem(item: AgentAccessTarget): RolePermissionTargetItem {
  return {
    id: item.id,
    name: item.name,
    description: item.description ?? undefined,
    // 有効なエージェントには状態を出さず、無効だけを示す。
    status: item.status === "disabled" ? t("security.permissions.agentDisabled") : undefined,
  };
}

/** 候補を読む関数（`GET /api/security/access-targets/agents`。検索とページング。#608）。 */
export interface AccessTargetFetchers {
  agents: (query: RolePermissionTargetQuery, signal: AbortSignal) => Promise<AccessTargetPage<AgentAccessTarget>>;
}

const grantsAllByAdmin = (effective: ReadonlySet<string>) => effective.has(CAPABILITY_PERMISSIONS.admin);

/** エージェントの対象範囲（RolePermissionsPage の targets）。候補はサーバー側で検索し、50 件ずつ読む。 */
export function agentPermissionTargets(fetchers: AccessTargetFetchers): RolePermissionTargetSection<SecurityRole>[] {
  return [
    {
      key: AGENT_ACCESS_KEY,
      messages: {
        title: t("security.permissions.agents"),
        hint: t("security.permissions.agentsHint"),
        all: t("security.permissions.agentsAll"),
        searchLabel: t("security.permissions.agentsSearch"),
        searchPlaceholder: t("security.permissions.agentsSearchPlaceholder"),
        empty: t("security.permissions.agentsEmpty"),
        noResults: t("security.permissions.agentsNoResults"),
        loadWarning: t("security.permissions.agentsLoadWarning"),
        grantsAllByPermission: t("security.permissions.agentsManagedAll"),
        grantsAllSystemAdmin: t("security.permissions.agentsSystemAdmin"),
      },
      query: (query, { signal }): Promise<RolePermissionTargetPage> =>
        fetchers.agents(query, signal).then((page) => ({ items: page.items.map(agentTargetItem), total: page.total })),
      selectedIds: (role) => role.agent_ids ?? [],
      grantsAll: grantsAllByAdmin,
    },
  ];
}
