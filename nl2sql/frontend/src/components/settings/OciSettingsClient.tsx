import { OciSettingsPage } from "@production-ready/system-settings";

import { ApiError, api } from "@/lib/api";

/** OCI 認証設定。画面の実体は platform の共有パッケージ（#100）。 */
export function OciSettingsClient() {
  return (
    <OciSettingsPage
      api={api}
      errorMessage={(error) => (error instanceof ApiError ? error.message : undefined)}
    />
  );
}
