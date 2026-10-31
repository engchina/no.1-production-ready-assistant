import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const usersPage = readFileSync(
  new URL("../../../platform/packages/system-settings/src/users-roles/UserManagementPage.tsx", import.meta.url),
  "utf8"
);

test("ユーザー作成後は値を保持して編集 view へ遷移する", () => {
  assert.doesNotMatch(usersPage, /create-result/u);
  assert.match(usersPage, /type UserPanelView = "list" \| "create" \| "edit"/u);
  assert.match(
    usersPage,
    /if \(operationBusy \|\| !canSubmitUserForm\) return/u
  );
  assert.match(usersPage, /startEdit\(created\.user, created\.temporary_password\)/u);
  assert.doesNotMatch(usersPage, /OneTimePasswordResult|security-users-create-password/u);
});

test("作成・リセット結果は同じ読み取り専用欄とコピー操作へ統合する", () => {
  assert.match(usersPage, /readOnly=\{activeView === "edit"\}/u);
  assert.match(usersPage, /type=\{activeView === "create" \? "password" : "text"\}/u);
  assert.match(usersPage, /data-testid="security-user-temporary-password"/u);
  // コピーは共有の CopyButton（#2099）。
  assert.match(usersPage, /testId="security-user-temporary-password-copy"/u);
  assert.match(usersPage, /<CopyButton\s+text=\{draft\.temporaryPassword\}/u);
  assert.match(usersPage, /temporaryPassword: result\.temporary_password/u);
  assert.doesNotMatch(usersPage, /oneTimePassword\.createdTitle|oneTimePassword\.resetTitle/u);
});

test("無効ユーザーは保存とパスワードリセットを表示・実行しない", () => {
  assert.match(
    usersPage,
    /const userFormReadOnly = activeView === "edit" && editingUser\?\.status !== "ACTIVE"/u
  );
  assert.match(usersPage, /const canSubmitUserForm = !userFormReadOnly/u);
  assert.match(
    usersPage,
    /if \(operationBusy \|\| !canSubmitUserForm\) return/u
  );
  assert.match(
    usersPage,
    /if \(user\.status !== "ACTIVE" \|\| operationBusy\) return/u
  );
  assert.match(
    usersPage,
    /visible: user\.status === "ACTIVE" && !user\.is_bootstrap_admin/u
  );
  // 保存・作成はフォームの末尾の FormActionBar で、保存できないときは出さない（#1786。#618 では PageHeader の右端）。
  assert.match(usersPage, /\{canSubmitUserForm \? \([\s\S]*?<FormActionBar[\s\S]*?id: activeView === "edit" \? "save" : "create"/u);
});

test("無効ユーザーの編集内容は選択状態を表示したまま読み取り専用になる", () => {
  assert.match(usersPage, /if \(userFormReadOnly\) return;\s*setDraft/u);
  assert.match(usersPage, /disabled=\{inputReadOnly\}/u);
  assert.match(
    usersPage,
    /disabled=\{userFormReadOnly \|\| !draft\.temporaryPassword\}/u
  );
  assert.match(
    usersPage,
    /<fieldset className="grid gap-2" disabled=\{inputReadOnly\}>/u
  );
  assert.match(
    usersPage,
    /const disabled = userFormReadOnly;/u
  );
});

test("一覧・詳細のパスワードリセットも編集フォームへ結果を集約する", () => {
  const listActionsStart = usersPage.indexOf("const userActions");
  const formActionsStart = usersPage.indexOf("const editObjectActions");
  const formActionsEnd = usersPage.indexOf("const selectRole", formActionsStart);

  assert.notEqual(listActionsStart, -1);
  assert.notEqual(formActionsStart, -1);
  assert.notEqual(formActionsEnd, -1);

  const listActions = usersPage.slice(listActionsStart, formActionsStart);
  const formActions = usersPage.slice(formActionsStart, formActionsEnd);
  assert.match(listActions, /resetPasswordAction\(user\)/u);
  assert.match(formActions, /return userActions\(editingUser\)\.filter/u);
  assert.match(usersPage, /startEdit\(result\.user, result\.temporary_password\)/u);
  assert.match(usersPage, /security-users-reset-password-error/u);
});
