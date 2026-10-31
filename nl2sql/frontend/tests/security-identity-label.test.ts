import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  identityInlineLabel,
  identitySecondaryName,
} from "@production-ready/system-settings";

const source = (path: string) =>
  readFileSync(new URL(`../src/features/security/${path}`, import.meta.url), "utf8");
// ユーザー管理・ロール管理の画面は platform の共有パッケージにある（#206）。
const sharedSource = (path: string) =>
  readFileSync(
    new URL(`../../../platform/packages/system-settings/src/users-roles/${path}`, import.meta.url),
    "utf8"
  );

test("表示名は ID と異なるときだけ補助表示し、空・同一なら重複行を出さない", () => {
  assert.equal(identitySecondaryName("data_user", "データユーザー"), "データユーザー");
  assert.equal(identitySecondaryName("data_user", "  データユーザー "), "データユーザー");
  assert.equal(identitySecondaryName("data_user", "data_user"), "");
  assert.equal(identitySecondaryName("data_user", " data_user "), "");
  assert.equal(identitySecondaryName("data_user", ""), "");
  assert.equal(identitySecondaryName("data_user", null), "");
  assert.equal(identitySecondaryName("data_user"), "");
});

test("併記するときは ID を先に書く", () => {
  assert.equal(identityInlineLabel("DATA_ADMIN", "データ管理者"), "DATA_ADMIN（データ管理者）");
  assert.equal(identityInlineLabel("DATA_ADMIN", "DATA_ADMIN"), "DATA_ADMIN");
});

test("ユーザーは表示名だけを出して表示名で並べ、ロールはロール名だけを出してロール名で並べる", () => {
  const users = sharedSource("UserManagementPage.tsx");
  const roles = sharedSource("RoleManagementPage.tsx");
  const deepsec = source("SecurityDeepSecPage.tsx");

  // 一覧の行の題名はユーザーの表示名（空ならログイン ID）だけ。ログイン ID は「ログインユーザーID」の項目にだけ出す（#1696）。
  assert.match(users, /<RowTitleButton\s+title=\{userDisplayName\(user\)\}/u);
  assert.match(users, /compareText\(userDisplayName\(left\), userDisplayName\(right\), sort\.direction\)/u);
  assert.doesNotMatch(users, /<SecurityIdentityRowTitleButton/u);
  assert.doesNotMatch(users, /showUser", \{ name: user\.login_user_id \}/u);

  // ロールはロール名（表示名。空ならコード）だけを出し、ID・コードを出さない（#1682）。
  assert.match(users, /\{roleDisplayName\(role\)\}/u);
  assert.doesNotMatch(users, /<SecurityIdentityLines id=\{role\.role_code\}/u);
  assert.match(roles, /<RoleNameRowTitleButton\s+securityRole=\{role\}/u);
  assert.match(roles, /compareText\(roleDisplayName\(left\), roleDisplayName\(right\), sort\.direction\)/u);
  assert.doesNotMatch(roles, /id=\{role\.role_code\}/u);

  assert.match(deepsec, /\{roleDisplayName\(role\)\}/u);
  assert.match(deepsec, /\.sort\(compareRoleDisplayNames\)/u);
  assert.doesNotMatch(deepsec, /id=\{role\.role_code\}/u);
});
