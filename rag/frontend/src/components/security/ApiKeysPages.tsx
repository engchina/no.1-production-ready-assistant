import { AccountPage, ApiKeysAdminPage, mcpEndpoints } from "@production-ready/system-settings";

import { APP_BASE_PATH } from "@/lib/base-path";
import { APP_ROUTES } from "@/lib/routes";
import { apiKeysAdminApi, myApiKeysApi } from "@/lib/security-api";

/**
 * アカウントの画面（API キー / パスワード変更のタブ。#1873）。画面の実体は platform の共有パッケージ。
 * ログインしている全員が使える（メニュー権限を問わない）。
 */
export function AccountRoute() {
  return (
    <AccountPage
      loginRoute={APP_ROUTES.login}
      apiKeys={{ api: myApiKeysApi, endpoints: mcpEndpoints("rag", APP_BASE_PATH, window.location.origin) }}
    />
  );
}

/** 全員の API キー（「ユーザーとロール」の管理の画面。#1805）。 */
export function SecurityApiKeysPage() {
  return <ApiKeysAdminPage api={apiKeysAdminApi} />;
}
