import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// データベース設定の画面と型は3製品共通の共有パッケージにある（#108）。
const apiSource = readFileSync(
  new URL("../../../platform/packages/system-settings/src/database/types.ts", import.meta.url),
  "utf8"
);
const databaseSettingsSource = readFileSync(
  new URL("../../../platform/packages/system-settings/src/database/DatabaseSettingsPage.tsx", import.meta.url),
  "utf8"
);
const settingsE2eSource = readFileSync(
  new URL("./e2e/nl2sql-system-settings.spec.ts", import.meta.url),
  "utf8"
);
// データベース設定の schema は3製品共通の pr_system_settings.database にある（#108）。
const backendSettingsSchemaSource = readFileSync(
  new URL(
    "../../../platform/packages/system_settings_backend/src/pr_system_settings/database.py",
    import.meta.url
  ),
  "utf8"
);

test("database connection test status matches backend schema", () => {
  assert.match(
    backendSettingsSchemaSource,
    /DatabaseConnectionTestStatus = Literal\["success", "failed"\]/u
  );
  assert.match(
    apiSource,
    /export type DatabaseConnectionTestStatus = "success" \| "failed";/u
  );
  assert.doesNotMatch(databaseSettingsSource, /status === "skipped"/u);
  assert.doesNotMatch(settingsE2eSource, /status:\s*"skipped"/u);
});
