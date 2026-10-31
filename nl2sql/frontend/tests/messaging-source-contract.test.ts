import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { extname, relative } from "node:path";
import test from "node:test";

const sourceRoot = new URL("../src/", import.meta.url);

function tsxFiles(directoryUrl: URL): URL[] {
  return readdirSync(directoryUrl, { withFileTypes: true }).flatMap((entry) => {
    const child = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, directoryUrl);
    if (entry.isDirectory()) return tsxFiles(child);
    return extname(entry.name) === ".tsx" ? [child] : [];
  });
}

const sources = tsxFiles(sourceRoot).map((url) => ({
  path: relative(new URL("..", sourceRoot).pathname, url.pathname),
  source: readFileSync(url, "utf8"),
}));

test("business pages do not create raw alert live regions", () => {
  const violations = sources.flatMap(({ path, source }) =>
    path !== "src/components/StateViews.tsx" && /role\s*=\s*(?:"alert"|\{[^}]*["']alert["'][^}]*\})/gu.test(source) ? [path] : []
  );
  assert.deepEqual(
    violations,
    [],
    "FieldError/FormStatus/Banner/ErrorState 等の標準チャネルを使用してください。"
  );
});

test("handwritten danger surfaces stay limited to structured state and field controls", () => {
  const allowedOccurrenceCounts = new Map<string, number>([
    ["src/components/ExecutionActivityPanel.tsx", 1],
    ["src/components/ui/file-dropzone.tsx", 1],
    // 確認語欄の不一致バッジは packages/ui の ExecutionConfirmationField に移した（#379）。
    // 危険な操作の区画（WorkSection の tone="danger"）は packages/ui の Disclosure の tone に移した（#397）。
    ["src/features/nl2sql/components/WorkflowProgressStrip.tsx", 1],
    ["src/features/nl2sql/pages/DataManagementPage.tsx", 1],
    ["src/components/StateViews.tsx", 1],
  ]);
  const dangerSurface = /(?:border-danger[^"'\n]*bg-danger|bg-danger[^"'\n]*border-danger)/gu;
  const actual = new Map<string, number>();
  for (const { path, source } of sources) {
    const count = [...source.matchAll(dangerSurface)].length;
    if (count > 0) actual.set(path, count);
  }
  assert.deepEqual(
    [...actual.entries()].sort(),
    [...allowedOccurrenceCounts.entries()].sort(),
    "新しい通知面は semantic token の手書きではなく標準メッセージコンポーネントを使用してください。"
  );
});

test("security create forms bind server field errors without parsing Japanese strings", () => {
  const users = readFileSync(new URL("../../../platform/packages/system-settings/src/users-roles/UserManagementPage.tsx", import.meta.url), "utf8");
  const roles = readFileSync(new URL("../../../platform/packages/system-settings/src/users-roles/RoleManagementPage.tsx", import.meta.url), "utf8");

  assert.match(users, /"\/login_user_id": "loginUserId"/u);
  // 欄のエラーは共有の TextField の error で欄の下に出し、aria-describedby で結び付ける（#631）。
  assert.match(users, /id="security-user-login-user-id"[^>]*?error=\{fieldErrors\.loginUserId\}/u);
  assert.match(roles, /"\/role_code": "roleCode"/u);
  assert.match(roles, /id="security-role-code"[^>]*?error=\{fieldErrors\.roleCode\}/u);
  assert.doesNotMatch(users, /このログインユーザーIDは既に使用されています/u);
  assert.doesNotMatch(roles, /このロールコードは既に使用されています/u);
});

test("danger toast uses the shared default duration (messaging §3.1)", () => {
  // danger の Toast は共有 UI の既定（8 秒。ホバー・フォーカス中は止まる）で消える（#351・#2470）。
  // 製品側で自動消滅の時間を上書きしない。
  const storeSource = readFileSync(
    new URL("../../../platform/packages/ui/src/store/toast-store.ts", import.meta.url),
    "utf8"
  );
  assert.match(storeSource, /danger: 8000,/u);
  const timedDangerCalls = sources.flatMap(({ path, source }) =>
    /toast\.error\([^;]*duration\s*:/u.test(source) ? [path] : []
  );
  assert.deepEqual(timedDangerCalls, []);
});
