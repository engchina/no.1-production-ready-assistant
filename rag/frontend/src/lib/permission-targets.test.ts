import { afterEach, describe, expect, it, vi } from "vitest";

import type { AccessTargetPage, SecurityRole } from "./api";
import { CAPABILITY_PERMISSIONS } from "./permissions";
import {
  SEARCH_ANSWER_PROFILE_ACCESS_KEY,
  KNOWLEDGE_BASE_ACCESS_KEY,
  PERMISSIONS_API,
  ragPermissionTargets,
} from "./permission-targets";
import { canSubmitUpload, uploadKnowledgeBaseRequired } from "./upload-scope";
import { ragIdentityKey } from "@/components/security/AuthProvider";

const VIEWS: AccessTargetPage = {
  items: [
    { id: "bv-1", name: "人事 FAQ", status: "ACTIVE", description: "人事規程" },
    { id: "bv-2", name: "旧経理", status: "ARCHIVED", description: null },
  ],
  total: 120,
  limit: 50,
  offset: 0,
  has_next: true,
};

const ROLE: SecurityRole = {
  role_id: "role-1",
  role_code: "HR",
  display_name: "人事",
  description: "",
  is_built_in: false,
  archived: false,
  version: 3,
  permissions: ["menu.search"],
  search_answer_profile_ids: ["bv-1"],
  knowledge_base_ids: ["kb-1"],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("権限管理の対象（検索・回答プロファイル・KB）", () => {
  it("候補は対象ごとの API をサーバー側の検索とページングで読み、アーカイブ済みだけに状態を添える（Issue 608）", async () => {
    const fetchTargets = vi.fn().mockResolvedValue(VIEWS);
    const [views, bases] = ragPermissionTargets(fetchTargets);
    const signal = new AbortController().signal;
    const query = { q: "人事", limit: 50, offset: 0 };

    const page = await views.query(query, { signal });
    expect(fetchTargets).toHaveBeenCalledWith("search-answer-profiles", query, signal);
    expect(page).toEqual({
      items: [
        { id: "bv-1", name: "人事 FAQ", description: "人事規程", status: undefined },
        { id: "bv-2", name: "旧経理", description: undefined, status: "アーカイブ済み" },
      ],
      total: 120,
    });
    await bases.query({ q: "", limit: 2, offset: 0, ids: ["kb-1", "kb-2"] }, { signal });
    expect(fetchTargets).toHaveBeenLastCalledWith(
      "knowledge-bases",
      { q: "", limit: 2, offset: 0, ids: ["kb-1", "kb-2"] },
      signal,
    );
  });

  it("検索・回答プロファイルは検索・回答プロファイル管理、KB はナレッジベース管理の権限で全件が対象", () => {
    const [views, bases] = ragPermissionTargets(vi.fn());
    expect(views.key).toBe(SEARCH_ANSWER_PROFILE_ACCESS_KEY);
    expect(bases.key).toBe(KNOWLEDGE_BASE_ACCESS_KEY);
    const viewsManage = new Set([CAPABILITY_PERMISSIONS.searchAnswerProfilesManage]);
    const basesManage = new Set([CAPABILITY_PERMISSIONS.knowledgeBasesManage]);
    expect(views.grantsAll?.(viewsManage)).toBe(true);
    expect(views.grantsAll?.(basesManage)).toBe(false);
    expect(bases.grantsAll?.(basesManage)).toBe(true);
    expect(bases.grantsAll?.(viewsManage)).toBe(false);
    expect(views.selectedIds(ROLE)).toEqual(["bv-1"]);
    expect(bases.selectedIds(ROLE)).toEqual(["kb-1"]);
  });

  it("保存は PUT /api/security/roles/{id}/access へ version・権限・検索・回答プロファイル・KB を送る", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ data: { ...ROLE, version: 4 }, error_messages: [], warning_messages: [] }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const saved = await PERMISSIONS_API.save(ROLE, {
      permissions: ["menu.search", "menu.chat"],
      targets: { [SEARCH_ANSWER_PROFILE_ACCESS_KEY]: ["bv-1", "bv-2"] },
    });

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/security/roles/role-1/access");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({
      version: 3,
      permissions: ["menu.search", "menu.chat"],
      search_answer_profile_ids: ["bv-1", "bv-2"],
      // 全件が対象のときや未選択のときは共通画面が空の一覧を渡す（key が無い場合も空）。
      knowledge_base_ids: [],
    });
    expect(saved.version).toBe(4);
  });
});

describe("利用者の対象範囲", () => {
  const user = {
    user_uuid: "u1",
    login_user_id: "u1",
    display_name: "U1",
    status: "ACTIVE",
    force_password_change: false,
    role_codes: ["HR"],
    role_display_names: ["人事"],
    is_system_admin: false,
    permissions: ["menu.search", "menu.chat"],
    allowed_search_answer_profile_ids: ["bv-2", "bv-1"],
    allowed_knowledge_base_ids: null,
    debug_mode: false,
    password_change_allowed: true,
  };

  it("identity key は権限・検索・回答プロファイル・KB の変化を検出し、並び順の違いは無視する", () => {
    const base = ragIdentityKey(user);
    expect(ragIdentityKey({ ...user, permissions: ["menu.chat", "menu.search"] })).toBe(base);
    expect(ragIdentityKey({ ...user, allowed_search_answer_profile_ids: ["bv-1", "bv-2"] })).toBe(base);
    expect(ragIdentityKey({ ...user, allowed_search_answer_profile_ids: ["bv-1"] })).not.toBe(base);
    expect(ragIdentityKey({ ...user, allowed_knowledge_base_ids: [] })).not.toBe(base);
    expect(ragIdentityKey({ ...user, permissions: ["menu.search"] })).not.toBe(base);
    expect(ragIdentityKey({ ...user, user_uuid: "u2" })).not.toBe(base);
  });

  it("KB が制限された利用者は、アップロード先の KB を選ばないと送信できない", () => {
    expect(uploadKnowledgeBaseRequired(user)).toBe(false);
    expect(uploadKnowledgeBaseRequired({ allowed_knowledge_base_ids: [] })).toBe(true);
    expect(uploadKnowledgeBaseRequired({ allowed_knowledge_base_ids: ["kb-1"] })).toBe(true);
    expect(uploadKnowledgeBaseRequired(null)).toBe(false);
    expect(canSubmitUpload(true, [])).toBe(false);
    expect(canSubmitUpload(true, ["kb-1"])).toBe(true);
    expect(canSubmitUpload(false, [])).toBe(true);
  });
});
