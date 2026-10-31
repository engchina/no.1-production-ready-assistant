import type { Page, Route } from "@playwright/test";

function envelope(data: unknown) {
  return { data, error_messages: [], warning_messages: [] };
}

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(envelope(data)),
  });
}

/**
 * `GET /api/security/profile-access/profiles` の応答（#608）。backend と同じく `q`（名前・カテゴリ・説明）・
 * `ids`・`limit` / `offset` で絞った Page（items / total）を返す。
 */
export function profileAccessPage<
  T extends { id: string; name: string; category?: string; description?: string },
>(url: string, profiles: readonly T[]) {
  const params = new URL(url).searchParams;
  const q = (params.get("q") ?? "").toLowerCase();
  const ids = params.getAll("ids");
  const limit = Number(params.get("limit") ?? "50");
  const offset = Number(params.get("offset") ?? "0");
  const matched = profiles.filter(
    (profile) =>
      (ids.length === 0 || ids.includes(profile.id)) &&
      (!q ||
        [profile.name, profile.category ?? "", profile.description ?? ""].join(" ").toLowerCase().includes(q))
  );
  return {
    items: matched.slice(offset, offset + limit),
    total: matched.length,
    limit,
    offset,
    has_next: offset + limit < matched.length,
  };
}

export const systemAdminMe = {
  user_uuid: "00000000-0000-0000-0000-000000000001",
  login_user_id: "SYSTEM",
  display_name: "システム管理者",
  status: "ACTIVE",
  force_password_change: false,
  role_codes: ["SYSTEM_ADMIN"],
  role_display_names: ["システム管理者"],
  is_system_admin: true,
  permissions: [],
  data_entitlements: [],
  allowed_profile_ids: [],
  debug_mode: false,
  password_change_allowed: true,
};

/** 通常の E2E は Oracle snapshot が利用可能な状態から開始する。 */
export async function mockDatabaseGateReady(page: Page) {
  await page.route("**/api/auth/me", (route) => fulfill(route, systemAdminMe));
  await page.route("**/api/schema/refresh-jobs/active", (route) =>
    fulfill(route, { active_job: null })
  );
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), []))
  );
  await page.route("**/api/ready/database", (route) =>
    fulfill(route, { status: "ok", check: "ok", detail: null })
  );
  await page.route("**/api/nl2sql/persistence", (route) =>
    fulfill(route, {
      mode: "oracle",
      ready: true,
      durable: true,
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: "2026-07-19T00:00:00Z",
    })
  );
  await page.route("**/api/nl2sql/persistence/recover", (route) =>
    fulfill(route, {
      mode: "oracle",
      ready: true,
      durable: true,
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: "2026-07-19T00:00:00Z",
    })
  );
  await page.route("**/api/settings/database/system-tables", (route) =>
    fulfill(route, {
      status: "ready",
      schema_head: 15,
      applied_versions: [0, 1, 2, 3, 5, 6, 7, 8, 9, 15],
      pending_versions: [],
      expected_object_count: 51,
      existing_object_count: 51,
      missing_objects: [],
      tables: [],
      operation_state: {
        status: "idle",
        operation_kind: null,
        lease_expires_at: null,
        last_error_code: null,
        schema_epoch: 1,
        updated_at: "2026-07-19T00:00:00Z",
      },
    })
  );
}
