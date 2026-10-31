import assert from "node:assert/strict";
import { test } from "node:test";
import { bindWorkspaceOwner, clearWorkspaceDrafts, draftKey, draftValueOnScopeChange, readDraft, writeDraft, WORKSPACE_DRAFT_TTL_MS, type DraftCarryOver, type DraftStorage } from "../src/lib/workspace-drafts";
class Storage implements DraftStorage {
  data = new Map<string, string>();
  get length() { return this.data.size; }
  key(index: number) { return [...this.data.keys()][index] ?? null; }
  getItem(key: string) { return this.data.get(key) ?? null; }
  setItem(key: string, value: string) { this.data.set(key, value); }
  removeItem(key: string) { this.data.delete(key); }
}
test("草稿はユーザー・DB・ページ・field で隔離し、別ログインと失効で消去する", () => {
  const storage = new Storage();
  storage.setItem("theme", "dark");
  bindWorkspaceOwner(storage, "alice");
  const key = draftKey("alice", "db1", "/query", "question:p1");
  assert.equal(writeDraft(storage, "alice", key, "下書き", 100), true);
  assert.equal(readDraft(storage, key, "", 101), "下書き");
  for (const other of [draftKey("alice", "db2", "/query", "question:p1"), draftKey("alice", "db1", "/query", "question:p2"), draftKey("bob", "db1", "/query", "question:p1")]) assert.equal(readDraft(storage, other, "", 101), "");
  assert.equal(readDraft(storage, key, "", 100 + WORKSPACE_DRAFT_TTL_MS), "");
  writeDraft(storage, "alice", key, "下書き", 100);
  bindWorkspaceOwner(storage, "bob");
  assert.equal(storage.getItem(key), null);
  assert.equal(writeDraft(storage, "alice", key, "遅延応答"), false);
  clearWorkspaceDrafts(storage);
  assert.equal(storage.length, 1);
  assert.equal(storage.getItem("theme"), "dark");
});
test("壊れた値・型の違う値・巨大な草稿・書込失敗を安全に扱う", () => {
  const storage = new Storage();
  bindWorkspaceOwner(storage, "alice");
  const key = draftKey("alice", "db1", "/query", "question");
  storage.setItem(key, "not-json");
  assert.equal(readDraft(storage, key, ""), "");
  writeDraft(storage, "alice", key, { bad: true });
  assert.equal(readDraft(storage, key, ""), "");
  assert.equal(writeDraft(storage, "alice", key, "x".repeat(200_001)), false);
  storage.setItem = () => { throw new Error("quota exceeded"); };
  assert.equal(writeDraft(storage, "alice", key, "SQL"), false);
});
test("置き場所が変わった render は新しい草稿を読み、carryOver が認めた field の変化だけ値を引き継ぐ（#3357）", () => {
  const scope = { owner: "alice", context: "db1", page: "/query", field: "question:" };
  const next = { ...scope, field: "question:p1" };
  const calls: unknown[] = [];
  // SQL 生成の画面と同じ規則: 未確定のプロファイルの入力を、新しいプロファイルの草稿が空のときだけ引き継ぐ。
  const carry: DraftCarryOver<string> = (change) => {
    calls.push(change);
    return change.from === "question:" && Boolean(change.value.trim()) && !change.stored.trim();
  };
  assert.equal(draftValueOnScopeChange(scope, next, "合成の質問", "", carry), "合成の質問");
  assert.deepEqual(calls, [{ from: "question:", to: "question:p1", value: "合成の質問", stored: "" }]);
  // 新しい field の草稿があれば、それを読む。
  assert.equal(draftValueOnScopeChange(scope, next, "合成の質問", "残っていた草稿", carry), "残っていた草稿");
  // 確定したプロファイルどうしの切り替えは引き継がない。
  assert.equal(draftValueOnScopeChange({ ...scope, field: "question:p1" }, { ...scope, field: "question:p2" }, "p1 の質問", "", carry), "");
  // carryOver の無い草稿は、今までどおり新しい草稿を読む。
  assert.equal(draftValueOnScopeChange(scope, next, "合成の質問", ""), "");
  // 利用者・DB・画面が変わったら、field が変わっても引き継がない（判定も呼ばない）。
  calls.length = 0;
  for (const other of [{ owner: "bob" }, { context: "db2" }, { page: "/chat" }]) {
    assert.equal(draftValueOnScopeChange(scope, { ...next, ...other }, "合成の質問", "", carry), "");
  }
  assert.equal(calls.length, 0);
});
