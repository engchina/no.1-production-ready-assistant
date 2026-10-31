import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  applySchemaBulkSelection,
  countSelectedObjectsInOwner,
  normalizeObjectKey,
  profileFormEquals,
  selectedObjectKeys,
  toggleObjectSelection,
} from "../src/features/nl2sql/profileObjectSelection.ts";

const profilePage = readFileSync(
  new URL("../src/features/nl2sql/pages/ProfileManagementPage.tsx", import.meta.url),
  "utf8",
);
const incrementalQueries = readFileSync(
  new URL("../src/features/nl2sql/incrementalQueries.ts", import.meta.url),
  "utf8",
);

test("フィルタ未適用のスキーマ全選択はスキーマ全体を置き換える", () => {
  const next = applySchemaBulkSelection({
    current: ["SALES.ORDERS", "HR.EMPLOYEES"],
    snapshot: ["SALES.ORDERS", "SALES.ITEMS"],
    ownerPrefix: "SALES.",
    select: true,
    filtered: false,
  });
  assert.deepEqual(next, ["HR.EMPLOYEES", "SALES.ORDERS", "SALES.ITEMS"]);
});

test("フィルタ未適用のスキーマ全解除はそのスキーマだけを外す", () => {
  const next = applySchemaBulkSelection({
    current: ["SALES.ORDERS", "HR.EMPLOYEES"],
    snapshot: [],
    ownerPrefix: "SALES.",
    select: false,
    filtered: false,
  });
  assert.deepEqual(next, ["HR.EMPLOYEES"]);
});

test("フィルタ適用中の全選択はヒットした object 以外の選択を広げない", () => {
  const next = applySchemaBulkSelection({
    current: ["SALES.ORDERS"],
    snapshot: ["SALES.ORDER_ITEMS"],
    ownerPrefix: "SALES.",
    select: true,
    filtered: true,
  });
  // フィルタ外の SALES.ORDERS は保持され、スキーマ内の未ヒット object は追加されない。
  assert.deepEqual(next, ["SALES.ORDERS", "SALES.ORDER_ITEMS"]);
});

test("フィルタ適用中の全解除はヒットした object だけを外す", () => {
  const next = applySchemaBulkSelection({
    current: ["SALES.ORDERS", "SALES.ORDER_ITEMS", "HR.EMPLOYEES"],
    snapshot: ["SALES.ORDER_ITEMS"],
    ownerPrefix: "SALES.",
    select: false,
    filtered: true,
  });
  assert.deepEqual(next, ["SALES.ORDERS", "HR.EMPLOYEES"]);
});

test("引用符付き・小文字の保存値も同じキーとして突合する", () => {
  const next = applySchemaBulkSelection({
    current: ['"SALES"."ORDERS"', "sales.items"],
    snapshot: ["SALES.ORDERS"],
    ownerPrefix: "sales.",
    select: false,
    filtered: true,
  });
  assert.deepEqual(next, ["sales.items"]);
});

test("スキーマ一括操作は検索フィルタをサーバ問い合わせへ伝搬する", () => {
  assert.match(profilePage, /applySchemaBulkSelection\(\{/u);
  assert.match(
    profilePage,
    /getSchemaObjectSnapshot\(owner, kind === "table" \? "TABLE" : "VIEW", filter\)/u,
  );
  assert.match(incrementalQueries, /q: normalizedQuery,/u);
  // フィルタ適用中は解除側も snapshot 範囲に限定する(スキーマ全体へ波及させない)。
  assert.match(profilePage, /select \|\| filtered/u);
});

test("表示一覧はサーバ検索結果をそのまま使い二重フィルタしない", () => {
  assert.doesNotMatch(profilePage, /filterProfileObjects/u);
  assert.match(profilePage, /tableObjects=\{tableObjects\}/u);
  assert.match(profilePage, /viewObjects=\{viewObjects\}/u);
});

test("業務プロファイル画面の検索は SearchField が確定した値で問い合わせる（二重に遅延させない。#535）", () => {
  const filterFields = readFileSync(
    new URL("../src/components/DbObjectFilterFields.tsx", import.meta.url),
    "utf8",
  );
  // debounce と IME の対応は共有の SearchField が持つ。
  assert.match(filterFields, /<SearchField/u);
  assert.match(profilePage, /useProfileSummaries\(profileSearch, profileSort\)/u);
  assert.match(profilePage, /useSchemaObjects\(objectFilter, "TABLE"\)/u);
  assert.match(profilePage, /useSchemaObjects\(objectFilter, "VIEW"\)/u);
  // 一括操作も表示中の一覧と同じ条件を使う。
  assert.match(profilePage, /const filter = objectFilter\.trim\(\);/u);
});

test("管理系一覧ページは検索語を独自に遅延させない（SearchField の debounce に一本化。#535）", () => {
  for (const page of [
    "TableManagementPage",
    "ViewManagementPage",
    "MetadataSqlManagementPage",
    "DataManagementPage",
    "ProfileManagementPage",
  ]) {
    const source = readFileSync(
      new URL(`../src/features/nl2sql/pages/${page}.tsx`, import.meta.url),
      "utf8",
    );
    assert.doesNotMatch(source, /useDebouncedValue|setTimeout\([^)]*[Ss]earch/u, page);
  }
});

test("トグルは表記が違う保存値でも同じ object として外せる", () => {
  assert.deepEqual(toggleObjectSelection(['"APP"."TABLE_01"'], "APP.TABLE_01"), []);
  assert.deepEqual(toggleObjectSelection(["app.table_01"], "APP.TABLE_01"), []);
  assert.deepEqual(toggleObjectSelection(["APP.TABLE_02"], "APP.TABLE_01"), [
    "APP.TABLE_02",
    "APP.TABLE_01",
  ]);
});

test("引用名は大文字化せず、大文字の同名表と別の object として選択する (#561)", () => {
  assert.equal(normalizeObjectKey('SALES."Mixed_Case"'), 'SALES."Mixed_Case"');
  assert.equal(normalizeObjectKey('"SALES"."Mixed_Case"'), 'SALES."Mixed_Case"');
  assert.equal(normalizeObjectKey('"SALES"."MIXED_CASE"'), "SALES.MIXED_CASE");
  assert.equal(normalizeObjectKey('APP."売上"'), 'APP."売上"');

  const quotedSelected = toggleObjectSelection([], 'SALES."Mixed_Case"');
  assert.deepEqual(quotedSelected, ['SALES."Mixed_Case"']);
  const both = toggleObjectSelection(quotedSelected, "SALES.MIXED_CASE");
  assert.deepEqual(both, ['SALES."Mixed_Case"', "SALES.MIXED_CASE"]);
  assert.deepEqual([...selectedObjectKeys(both)].sort(), ['SALES."Mixed_Case"', "SALES.MIXED_CASE"]);
  // 大文字の表を外しても引用名の表の選択は残る。
  assert.deepEqual(toggleObjectSelection(both, '"SALES"."MIXED_CASE"'), ['SALES."Mixed_Case"']);
  assert.equal(
    profileFormEquals(
      { allowedTables: ['SALES."Mixed_Case"'], allowedViews: [] },
      { allowedTables: ["SALES.MIXED_CASE"], allowedViews: [] },
    ),
    false,
  );
});

test("スキーマ単位の件数と一括解除は owner 部分で判定し、前方一致で他スキーマを巻き込まない", () => {
  const selected = ["SALES.ORDERS", 'SALES."Mixed_Case"', "SALES_ARCHIVE.ORDERS", "ORDERS"];
  assert.equal(countSelectedObjectsInOwner(selectedObjectKeys(selected), "SALES"), 2);
  assert.deepEqual(
    applySchemaBulkSelection({
      current: selected,
      snapshot: [],
      ownerPrefix: "SALES.",
      select: false,
      filtered: false,
    }),
    ["SALES_ARCHIVE.ORDERS", "ORDERS"],
  );
  assert.deepEqual(
    applySchemaBulkSelection({
      current: ["SALES.MIXED_CASE", 'SALES."Mixed_Case"'],
      snapshot: ['SALES."Mixed_Case"'],
      ownerPrefix: "SALES.",
      select: false,
      filtered: true,
    }),
    ["SALES.MIXED_CASE"],
  );
});

test("スキーマ一括選択の snapshot は引用名を大文字化しない", () => {
  assert.match(
    incrementalQueries,
    /names\.add\(formatDbObjectName\(\{ owner: object\.owner, name: object\.object_name \}\)\);/u,
  );
  assert.doesNotMatch(incrementalQueries, /`\$\{object\.owner\}\.\$\{object\.object_name\}`\.toUpperCase\(\)/u);
});

test("トグルは重複を積み上げない", () => {
  let selection: string[] = [];
  for (let index = 0; index < 3; index += 1) {
    selection = toggleObjectSelection(selection, "APP.TABLE_01");
    selection = toggleObjectSelection(selection, '"APP"."TABLE_01"');
  }
  assert.deepEqual(selection, []);
  assert.deepEqual(toggleObjectSelection(selection, "app.table_01"), ["APP.TABLE_01"]);
});

test("突合用集合は引用符と大文字小文字を吸収する", () => {
  assert.deepEqual(
    [...selectedObjectKeys(['"APP"."TABLE_01"', "app.table_02"])].sort(),
    ["APP.TABLE_01", "APP.TABLE_02"],
  );
});

test("チェック判定・トグル・件数が同じ正規化キーを共有する", () => {
  assert.match(profilePage, /selectedObjectKeys\(selectedItems\)/u);
  assert.match(profilePage, /toggleObjectSelection\(current\[key\], name\)/u);
  // 候補の key と選択の集合が同じ正規化キー（共通の ListPicker の selectedKeys で判定する。#600）。
  assert.match(profilePage, /key: normalizeObjectKey\(qualified\)/u);
  assert.match(profilePage, /selectedKeys=\{selectedSet\}/u);
  assert.match(profilePage, /countSelectedObjectsInOwner\(selectedSet, owner\)/u);
  assert.doesNotMatch(profilePage, /name\.startsWith\(ownerKeyPrefix\)/u);
  assert.doesNotMatch(profilePage, /current\[key\]\.includes\(name\)/u);
});

test("手動更新の失敗は初回ロード用 Banner state と分離して保持する", () => {
  // クエリ状態を監視する effect は message を空文字で上書きするため、
  // 手動更新の失敗を同じ state に載せると通知が消える。
  assert.match(profilePage, /const \[refreshError, setRefreshError\] = useState\(""\);/u);
  assert.match(profilePage, /setRefreshError\(t\("profiles\.error\.load"\)\);/u);
  assert.doesNotMatch(profilePage, /setMessage\(t\("profiles\.error\.load"\)\);/u);
  assert.match(profilePage, /: refreshError\n\s*\? \{ tone: "danger" as const/u);
});

test("保存成功時に実行確認語をクリアして破壊的操作のゲートを再武装する", () => {
  assert.match(
    profilePage,
    /setRequiredErrors\(\{\}\);\n\s*\/\/[^\n]*\n\s*\/\/[^\n]*\n\s*setOracleConfirmation\(""\);/u,
  );
});

test("未保存判定は許可オブジェクトの並び順で変化しない", () => {
  const baseline = {
    name: "SALES",
    category: "販売",
    allowedTables: ["APP.T1", "APP.T2"],
    allowedViews: ["APP.V1"],
  };
  // チェックを外して付け直すと末尾に回るだけで、内容は同じ。
  const reordered = { ...baseline, allowedTables: ["APP.T2", "APP.T1"] };
  assert.equal(profileFormEquals(baseline, reordered), true);
  // 表記揺れも同一とみなす。
  assert.equal(
    profileFormEquals(baseline, { ...baseline, allowedTables: ['"APP"."T1"', "app.t2"] }),
    true,
  );
});

test("未保存判定は実際の差分を見逃さない", () => {
  const baseline = {
    name: "SALES",
    category: "販売",
    allowedTables: ["APP.T1"],
    allowedViews: [],
  };
  assert.equal(profileFormEquals(baseline, { ...baseline, category: "財務" }), false);
  assert.equal(
    profileFormEquals(baseline, { ...baseline, allowedTables: ["APP.T1", "APP.T2"] }),
    false,
  );
  assert.equal(profileFormEquals(baseline, { ...baseline, allowedViews: ["APP.V1"] }), false);
});

test("編集画面の dirty 判定は順序非依存の比較を使う", () => {
  assert.match(profilePage, /return !profileFormEquals\(form, baseline\);/u);
  assert.doesNotMatch(profilePage, /JSON\.stringify\(form\) !== JSON\.stringify\(baseline\)/u);
});

test("編集画面はルート遷移とタブ離脱の両方を未保存ガードで保護する", () => {
  const guard = readFileSync(
    // 実装は platform の共有パッケージ（#97）。
    new URL(
      "../../../platform/packages/system-settings/src/guards/useUnsavedChangesGuard.ts",
      import.meta.url,
    ),
    "utf8",
  );
  // 内部リンクの click を capture 段階で受ける。戻る/進む（POP）だけを、data router の中で
  // useBlocker で確認する（画面内のボタンが自分で確認した後の navigate を二重に止めない。#138）。
  assert.match(guard, /UNSAFE_DataRouterContext/u);
  assert.match(guard, /historyAction === "POP"/u);
  assert.match(guard, /document\.addEventListener\("click", handleClick, true\)/u);
  assert.match(guard, /window\.addEventListener\("beforeunload", handleBeforeUnload\)/u);
  assert.match(guard, /url\.origin !== window\.location\.origin/u);

  assert.match(
    profilePage,
    /useUnsavedChangesGuard\(activeView === "editor" && isDirty, confirmDiscard\)/u,
  );
  // 「一覧に戻る」も同じ確認ダイアログを共有する。
  assert.match(profilePage, /if \(isDirty && !\(await confirmDiscard\(\)\)\) return;/u);
});

test("スキーマ単位の件数・一括解除は小文字を含む owner を大文字の同名 owner と区別する (#563)", () => {
  const selected = ['"Sales".ORDERS', "SALES.ORDERS", 'SALES."Mixed_Case"'];
  const keys = selectedObjectKeys(selected);

  // owner はスキーマ一覧のカタログ上の名前（大文字小文字を保持）で渡る。
  assert.equal(countSelectedObjectsInOwner(keys, "Sales"), 1);
  assert.equal(countSelectedObjectsInOwner(keys, "SALES"), 2);
  assert.deepEqual(
    applySchemaBulkSelection({
      current: selected,
      snapshot: [],
      ownerPrefix: "Sales.",
      select: false,
      filtered: false,
    }),
    ["SALES.ORDERS", 'SALES."Mixed_Case"'],
  );
  assert.doesNotMatch(profilePage, /object\.owner\.toUpperCase\(\)/u);
  assert.doesNotMatch(incrementalQueries, /owner\.trim\(\)\.toUpperCase\(\)/u);
});
