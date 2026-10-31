import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { t } from "../src/lib/i18n.ts";

const dataManagementSource = readFileSync(
  new URL("../src/features/nl2sql/pages/DataManagementPage.tsx", import.meta.url),
  "utf8"
);
const tableManagementSource = readFileSync(
  new URL("../src/features/nl2sql/pages/TableManagementPage.tsx", import.meta.url),
  "utf8"
);
const viewManagementSource = readFileSync(
  new URL("../src/features/nl2sql/pages/ViewManagementPage.tsx", import.meta.url),
  "utf8"
);
const metadataSqlManagementSource = readFileSync(
  new URL("../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx", import.meta.url),
  "utf8"
);
const dbObjectSharedSource = readFileSync(
  new URL("../src/features/nl2sql/components/DbObjectManagementShared.tsx", import.meta.url),
  "utf8"
);
const dbObjectFilterFieldsSource = readFileSync(
  new URL("../src/components/DbObjectFilterFields.tsx", import.meta.url),
  "utf8"
);
const incrementalQueriesSource = readFileSync(
  new URL("../src/features/nl2sql/incrementalQueries.ts", import.meta.url),
  "utf8"
);

test("データ管理プレビューは未取得行数を統計未取得として表示する", () => {
  assert.equal(t("dataMgmt.preview.rowUnknown"), "統計未取得");
  assert.match(
    dataManagementSource,
    /rowCount == null\) return t\("dataMgmt\.preview\.rowUnknown"\)/u
  );
});

test("データ管理プレビューは所有者前方一致入力と種別フィルタを表示し、行数フィルタを出さない", () => {
  // 一覧 + 詳細の一覧の列（DbObjectListDetail → DbObjectSearchOwnerFields）が検索・所有者を持ち、種別は filters に渡す（#2315）。
  assert.match(dataManagementSource, /<DbObjectListDetail[\s\S]{0,4000}filters=\{\s*<FilterSelect/u);
  assert.match(dbObjectSharedSource, /<DbObjectSearchOwnerFields[\s\S]{0,600}filters=\{filters\}/u);
  assert.match(dbObjectSharedSource, /t\("dbAdmin\.owner\.label"\)/u);
  assert.match(dbObjectSharedSource, /t\("dbAdmin\.search\.label"\)/u);
  assert.match(dbObjectSharedSource, /t\("dbAdmin\.search\.placeholder"\)/u);
  assert.match(dataManagementSource, /t\("dataMgmt\.preview\.kindFilter"\)/u);
  assert.doesNotMatch(dataManagementSource, /t\("dataMgmt\.preview\.rowFilter"\)/u);
});

test("対象画面の検索・所有者・種類フィルタを共通化する", () => {
  assert.equal(t("dbAdmin.search.label"), "検索");
  assert.equal(t("dbAdmin.search.placeholder"), "名前・コメントを入力");
  assert.equal(t("dbAdmin.owner.label"), "所有者");
  assert.equal(t("dbAdmin.ownerPrefix.placeholder"), "所有者の先頭（例：ADM）");
  assert.match(dbObjectFilterFieldsSource, /export function DbManagementSearchField/u);
  assert.match(dbObjectFilterFieldsSource, /export function DbOwnerPrefixFilterField/u);
  assert.match(dbObjectFilterFieldsSource, /export function DbManagementSelectField/u);
  assert.match(dbObjectFilterFieldsSource, /export function DbObjectSearchOwnerFields/u);
  // 検索・所有者・絞り込み・件数は共通の ListToolbar に並べる（検索欄は部品が lg で固定、所有者は md。#1891）。
  assert.match(dbObjectFilterFieldsSource, /<ListToolbar/u);
  assert.match(dbObjectFilterFieldsSource, /width = "md"/u);
  assert.doesNotMatch(dbObjectFilterFieldsSource, /md:grid-cols-2/u);
  // 種類の選択は共有の SelectField で SearchField と同じ高さ（md）・見た目にする（#613 / #631）。
  assert.match(dbObjectFilterFieldsSource, /<SelectField<T>/u);
  assert.doesNotMatch(dbObjectFilterFieldsSource, /<select\b/u);
  assert.doesNotMatch(dbObjectFilterFieldsSource, /min-h-\[44px\]|touchTarget/u);
  // フォーカスの表示は outline に一本化（ring を使わない。#355）
  assert.doesNotMatch(dbObjectFilterFieldsSource, /focus(?:-visible)?:ring-/u);
  // 検索・所有者の接頭辞は共有の SearchField（debounce・IME 対応）。接頭辞は確定した値を大文字にする（#535）。
  assert.match(dbObjectFilterFieldsSource, /<SearchField/u);
  assert.doesNotMatch(dbObjectFilterFieldsSource, /<TextField/u);
  assert.match(dbObjectFilterFieldsSource, /value\.trim\(\)\.toUpperCase\(\)/u);
  assert.match(dbObjectFilterFieldsSource, /normalize=\{normalizeOwnerPrefix\}/u);
  assert.match(dbObjectSharedSource, /from "@\/components\/DbObjectFilterFields"/u);
  assert.match(dbObjectSharedSource, /<DbObjectSearchOwnerFields/u);
  // 所有者のあるツールバーも無いツールバーも ListToolbar（絞り込みは filters、件数は count。#1891）。
  assert.match(dbObjectSharedSource, /<ListToolbar/u);
  assert.match(dbObjectSharedSource, /filters=\{children\}/u);
  assert.doesNotMatch(dbObjectSharedSource, /xl:grid-cols-\[minmax\(0,1fr\)_minmax\(0,1fr\)_auto\]/u);
  // 件数はパネルの見出しのバッジではなく ListToolbar の count 1 か所（#1891）。
  assert.match(dbObjectSharedSource, /count=\{count\}/u);
  assert.doesNotMatch(dbObjectSharedSource, /label=\{labels\.count\}/u);
  assert.doesNotMatch(dataManagementSource, /dataMgmt\.preview\.objectTotalCount/u);
  assert.doesNotMatch(metadataSqlManagementSource, /t\("command\.count"/u);
  assert.match(tableManagementSource, /useDbAdminObjects\("", "table", "all", "", "name_comment"\)/u);
  assert.match(viewManagementSource, /useDbAdminObjects\("", "view", "all", "", "name_comment"\)/u);
  assert.match(metadataSqlManagementSource, /useDbAdminObjects\("", "all", "all", "", "name_comment"\)/u);
  // 検索・所有者と並べる種類の絞り込みは FilterSelect（「種類: すべて」。既定の幅 sm）。条件のクリアと、
  // 3 つ以上の絞り込みの狭い置き場所のシートを渡す（#1933）。
  assert.match(dataManagementSource, /<FilterSelect[\s\S]{0,120}t\("dataMgmt\.preview\.kindFilter"\)/u);
  assert.match(metadataSqlManagementSource, /<FilterSelect[\s\S]{0,120}t\("metadataSql\.targets\.typeFilter"\)/u);
  assert.doesNotMatch(dataManagementSource, /<DbManagementSelectField/u);
  for (const source of [dataManagementSource, metadataSqlManagementSource]) {
    assert.match(source, /onClearFilters=\{/u);
    assert.match(source, /filtersLayout="sheet"/u);
  }
  // ツールバーの中の検索欄・所有者は欄の上にラベルを置かない（読み上げ名だけ。#1933）。
  assert.match(dbObjectFilterFieldsSource, /labelHidden = true/u);
  assert.match(dbObjectFilterFieldsSource, /onClearFilters=\{onClearFilters\}/u);
  assert.doesNotMatch(dbObjectSharedSource, /md:grid-cols-\[minmax\(0,1fr\)_13rem\]/u);
});

test("テーブル・ビュー管理は所有者前方一致入力と名前・コメント検索を使う", () => {
  assert.doesNotMatch(tableManagementSource, /useSchemaOwners/u);
  assert.doesNotMatch(viewManagementSource, /useSchemaOwners/u);
  assert.match(tableManagementSource, /tableOwnerPrefix/u);
  assert.match(viewManagementSource, /viewOwnerPrefix/u);
  assert.match(tableManagementSource, /useDbAdminObjects\([\s\S]{0,120}tableOwnerPrefix/u);
  assert.match(viewManagementSource, /useDbAdminObjects\([\s\S]{0,120}viewOwnerPrefix/u);
  assert.match(dbObjectSharedSource, /ownerPrefix: DbObjectOwnerPrefix/u);
  assert.doesNotMatch(dbObjectSharedSource, /ownerOptions/u);
  assert.doesNotMatch(dbObjectSharedSource, /ownerFilterAll/u);
  assert.doesNotMatch(dbObjectSharedSource, /ownerFilter: string/u);
  assert.match(tableManagementSource, /tableOwnerPrefix,\s*"name_comment"/u);
  assert.match(viewManagementSource, /viewOwnerPrefix,\s*"name_comment"/u);
  assert.match(
    incrementalQueriesSource,
    /params\.set\("owner_prefix", ownerPrefix\.trim\(\)\)/u
  );
  assert.match(
    incrementalQueriesSource,
    /params\.set\("query_scope", queryScope\)/u
  );
  assert.doesNotMatch(
    incrementalQueriesSource,
    /params\.set\("owner", ownerPrefix\.trim\(\)\)/u
  );
  assert.doesNotMatch(dbObjectSharedSource, /value="with_rows"/u);
  assert.doesNotMatch(dbObjectSharedSource, /value="empty_rows"/u);
});

test("コメント・アノテーション管理は共通所有者入力を種類フィルタの前に表示する", () => {
  const ownerFilterIndex = metadataSqlManagementSource.indexOf('t("dbAdmin.owner.label")');
  const typeFilterIndex = metadataSqlManagementSource.indexOf('t("metadataSql.targets.typeFilter")');
  assert.notEqual(ownerFilterIndex, -1);
  assert.notEqual(typeFilterIndex, -1);
  assert.ok(ownerFilterIndex < typeFilterIndex);
  assert.match(metadataSqlManagementSource, /ownerPrefixField=\{\{/u);
  assert.match(metadataSqlManagementSource, /targetOwnerPrefix/u);
  assert.match(
    metadataSqlManagementSource,
    /useDbAdminObjects\([\s\S]{0,140}targetOwnerPrefix/u,
  );
  assert.match(metadataSqlManagementSource, /targetOwnerPrefix,\s*"name_comment"/u);
});

test("データプレビューと COMMENT/ANNOTATION は name_comment scope を送る", () => {
  assert.match(dataManagementSource, /previewObjectOwnerPrefix,\s*"name_comment"/u);
  assert.match(metadataSqlManagementSource, /targetOwnerPrefix,\s*"name_comment"/u);
});

test("データ管理プレビューは取得件数上限を指定でき、詳細条件入力を出さない", () => {
  assert.match(dataManagementSource, /const DEFAULT_DATA_PREVIEW_ROW_LIMIT = DEFAULT_SQL_ROW_LIMIT/u);
  assert.match(dataManagementSource, /useWorkspaceState\("previewRowLimitInput", String\(DEFAULT_DATA_PREVIEW_ROW_LIMIT\)\)/u);
  assert.match(dataManagementSource, /parseSqlRowLimit\(previewRowLimitInput\)/u);
  assert.match(dataManagementSource, /limit: rowLimit/u);
  assert.match(dataManagementSource, /<RowLimitField/u);
  assert.match(dataManagementSource, /icon=\{Play\}/u);
  assert.match(dataManagementSource, /icon=\{X\}/u);
  assert.match(dataManagementSource, /size="lg"[\s\S]*?\{t\("dataMgmt\.preview\.show"\)\}/u);
  // 行を選ぶとすぐにデータを表示し、取得のたびに結果を置き換えるので、取得件数上限と結果を既定に戻すボタンは持たない（#2330）。
  assert.doesNotMatch(dataManagementSource, /dataMgmt\.preview\.clear|onClearPreview|canClearPreview/u);
  // 行を選ぶと URL の選択を変え、選んでいる行をもう一度押すとデータを取り直す（#2315）。
  assert.match(dataManagementSource, /onSelect=\{\(name\) => \(name === previewObject \? void showPreview\(name\) : previewSelection\.select\(name\)\)\}/u);
  // 表示結果は共通の結果の表（要約・プレビュー・すべての行のシート・CSV。#1178）。
  assert.match(dataManagementSource, /<QueryResultTable\s+results=\{preview\.results\}\s+rowLimit=\{executedRowLimit\}/u);
  assert.match(dataManagementSource, /where_clause: ""/u);
  assert.equal(t("queryResults.rowLimit.helper"), "1〜100000 の整数。取得上限を明示してください。");
  assert.equal(t("dataMgmt.preview.clear"), "dataMgmt.preview.clear");
  assert.doesNotMatch(dataManagementSource, /const DATA_PREVIEW_ROW_LIMIT/u);
  assert.doesNotMatch(dataManagementSource, /limit: DATA_PREVIEW_ROW_LIMIT/u);
  assert.doesNotMatch(dataManagementSource, /dataMgmt\.preview\.fixedLimit/u);
  assert.doesNotMatch(dataManagementSource, /onSelect=\{\(item\) => onShowPreview\(item\.key\)\}/u);
  assert.doesNotMatch(dataManagementSource, /t\("dataMgmt\.preview\.limit"\)/u);
  assert.doesNotMatch(dataManagementSource, /t\("dataMgmt\.preview\.where"\)/u);
  assert.doesNotMatch(dataManagementSource, /t\("dataMgmt\.preview\.wherePlaceholder"\)/u);
});

test("データ管理プレビューは一覧 + 詳細（ListDetailLayout）で、選択を URL に持ちステップを出さない", () => {
  // 左右 1:1 の FixedSplitPane（C 型）ではなく、テーブル / ビューの管理と同じ一覧 + 詳細（B 型。#2281・#2315）。
  assert.doesNotMatch(dataManagementSource, /splitId="data-management-preview"/u);
  assert.match(dataManagementSource, /<DbObjectListDetail[\s\S]{0,200}idPrefix="data-management-preview"/u);
  assert.match(dataManagementSource, /id="data-management-panel-preview"[\s\S]{0,300}framed=\{false\}/u);
  // 一覧 + 詳細の構成が段階（左で選ぶ → 右で結果）を示すので、ステップの表示は持たない。
  assert.doesNotMatch(dataManagementSource, /data-preview-steps/u);
  assert.doesNotMatch(dataManagementSource, /dataMgmt\.preview\.step/u);
  // 選択は URL の ?object=（keep-alive の画面の useWorkspaceUrlSelection。タブが開いている間だけ有効）。
  assert.match(dataManagementSource, /const PREVIEW_OBJECT_PARAM = "object"/u);
  assert.match(dataManagementSource, /useWorkspaceUrlSelection\(PREVIEW_OBJECT_PARAM, \{ enabled: activeView === "preview" \}\)/u);
  // 先頭の行を自動で選ばない（一覧の変化で選択を置き換える selectedVisibleStringKey を使わない）。
  assert.doesNotMatch(dataManagementSource, /selectedVisibleStringKey\(previewObject/u);
  assert.doesNotMatch(dataManagementSource, /useWorkspaceState\("previewObject"/u);
  // 1 ペインの「一覧へ戻る」は PageHeader の back。
  assert.match(dataManagementSource, /!previewSelection\.split && previewObject[\s\S]{0,200}onClick: previewSelection\.clear/u);
  assert.equal(t("dataMgmt.preview.backToList"), "一覧へ戻る");
});

test("データ管理の対象ピッカーはヘッダーソート契約を持つ", () => {
  assert.match(dbObjectSharedSource, /export type DbObjectPickerSortKey = "name" \| "kind" \| "row_count" \| "owner"/u);
  assert.match(dbObjectSharedSource, /export interface DbObjectPickerSortState/u);
  assert.match(dbObjectSharedSource, /rowCount\?: number \| null/u);
  // 並べ替えの aria-sort は共有 DataTable の <th> が持つ（#530）。
  assert.match(dbObjectSharedSource, /<DataTable[\s\S]{0,400}onSortChange=\{onSortChange \? \(next\) => onSortChange\(next\.key as DbObjectPickerSortKey\)/u);
  assert.match(dbObjectSharedSource, /onSortChange\?: \(key: DbObjectPickerSortKey\) => void/u);
  assert.match(dataManagementSource, /previewObjectSort/u);
  assert.match(dataManagementSource, /csvTableSort/u);
  assert.equal(t("objectSelector.sort.button", { label: "対象名", direction: "昇順" }), "対象名: 昇順");
});
