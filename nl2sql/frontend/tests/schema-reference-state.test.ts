import assert from "node:assert/strict";
import test from "node:test";

import { schemaReferenceState } from "../src/features/nl2sql/schemaReferenceState.ts";

const base = {
  profileId: "",
  profilesPending: false,
  prefillPending: false,
  profileReady: false,
  profileFailed: false,
  schemaPending: true,
};

test("業務プロファイルが未選択なら、無効の query の pending を読み込み中にしない (#3413)", () => {
  assert.equal(schemaReferenceState(base), "profile-unselected");
});

test("業務プロファイルの一覧・受け取った業務プロファイルの確認の間は読み込み中", () => {
  assert.equal(schemaReferenceState({ ...base, profilesPending: true }), "loading");
  assert.equal(schemaReferenceState({ ...base, prefillPending: true }), "loading");
});

test("選んだ業務プロファイルを確かめている間は読み込み中、使えなければ未選択と同じ案内", () => {
  assert.equal(schemaReferenceState({ ...base, profileId: "p1" }), "loading");
  assert.equal(
    schemaReferenceState({ ...base, profileId: "p1", profileFailed: true }),
    "profile-unselected"
  );
});

test("使える業務プロファイルの表は、読み終えるまで読み込み中", () => {
  const ready = { ...base, profileId: "p1", profileReady: true };
  assert.equal(schemaReferenceState(ready), "loading");
  assert.equal(schemaReferenceState({ ...ready, schemaPending: false }), "ready");
  // 一覧で使えると分かっていれば、利用の情報の失敗でも表は出す（一覧が正）。
  assert.equal(
    schemaReferenceState({ ...ready, profileFailed: true, schemaPending: false }),
    "ready"
  );
});
