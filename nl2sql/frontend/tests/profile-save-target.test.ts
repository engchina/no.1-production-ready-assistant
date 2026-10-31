import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  NEW_PROFILE_TARGET,
  isProfileSaveTargetCurrent,
} from "../src/features/nl2sql/profileSaveTarget.ts";

test("既存の業務プロファイルの保存は、同じ業務プロファイルを開いている間だけ反映する", () => {
  assert.equal(isProfileSaveTargetCurrent("sales", "sales", "sales"), true);
  assert.equal(isProfileSaveTargetCurrent("other", "sales", "sales"), false);
  assert.equal(isProfileSaveTargetCurrent(null, "sales", "sales"), false);
});

test("新規作成の保存は、URL が作成した ID に変わった後も反映する（#1784）", () => {
  // 作成の応答の直後（URL はまだ ?profile=new）。
  assert.equal(isProfileSaveTargetCurrent(NEW_PROFILE_TARGET, NEW_PROFILE_TARGET, "created"), true);
  // URL が ?profile=<作成した ID> に変わった後の応答。
  assert.equal(isProfileSaveTargetCurrent("created", NEW_PROFILE_TARGET, "created"), true);
});

test("新規作成の保存でも、別の業務プロファイル・一覧へ移ったら反映しない", () => {
  assert.equal(isProfileSaveTargetCurrent("other", NEW_PROFILE_TARGET, "created"), false);
  assert.equal(isProfileSaveTargetCurrent(null, NEW_PROFILE_TARGET, "created"), false);
  assert.equal(isProfileSaveTargetCurrent("", NEW_PROFILE_TARGET, ""), false);
});

test("保存の後のフォームの更新は、作成後の URL の変更でも続ける条件を使う（#1784）", () => {
  const source = readFileSync(
    new URL("../src/features/nl2sql/pages/ProfileManagementPage.tsx", import.meta.url),
    "utf8"
  );
  const save = source.slice(source.indexOf("const save = async"), source.indexOf("const discardChanges"));
  assert.ok(save.includes("isProfileSaveTargetCurrent(editTargetRef.current, target, saved.id)"));
  assert.ok(!save.includes("oracle-sync-jobs"));
});
