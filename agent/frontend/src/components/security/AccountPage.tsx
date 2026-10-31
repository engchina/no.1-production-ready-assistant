import { useQuery } from "@tanstack/react-query";
import { AccountPage, mcpEndpoints } from "@production-ready/system-settings";

import { useAuth } from "@/components/security/AuthProvider";
import { agentApi } from "@/lib/api";
import { APP_BASE_PATH } from "@/lib/base-path";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import { APP_ROUTES } from "@/lib/routes";
import { myApiKeysApi } from "@/lib/security-api";

/**
 * アカウントの画面（API キー / パスワード変更のタブ。#1873）。画面の実体は platform の共有パッケージ。
 * ログインしている全員が使える（メニュー権限を問わない）。
 *
 * 業務 Agent を読める利用者（チャット・業務 Agent・実行履歴のメニュー）には、キーで使える業務 Agent を絞る
 * 選択肢を渡す（#1874）。読めない利用者には渡さない（読めない一覧を取りに行って 403 にしない）。
 */
export function AccountRoute() {
  const { hasPermission } = useAuth();
  const canReadAgents = [MENU_PERMISSIONS.chat, MENU_PERMISSIONS.agents, MENU_PERMISSIONS.runs].some((code) =>
    hasPermission(code)
  );
  const agents = useQuery({ queryKey: ["agents"], queryFn: agentApi.listAgents, enabled: canReadAgents });
  const agentScopeOptions = agents.data
    ? agents.data.agents.map((agent) => ({ id: agent.id, name: agent.name }))
    : undefined;
  return (
    <AccountPage
      loginRoute={APP_ROUTES.login}
      apiKeys={{
        api: myApiKeysApi,
        endpoints: mcpEndpoints("agent", APP_BASE_PATH, window.location.origin),
        agentScopeOptions,
      }}
    />
  );
}
