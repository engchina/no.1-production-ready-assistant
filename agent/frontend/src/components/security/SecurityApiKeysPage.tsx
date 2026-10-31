import { ApiKeysAdminPage } from "@production-ready/system-settings";

import { apiKeysAdminApi } from "@/lib/security-api";

/** 全員の API キーを一覧し、不要なキーを削除する共通画面。 */
export function SecurityApiKeysPage() {
  return <ApiKeysAdminPage api={apiKeysAdminApi} />;
}
