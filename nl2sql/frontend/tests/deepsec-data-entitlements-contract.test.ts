import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { t } from "../src/lib/i18n.ts";

const pageSource = readFileSync(
  new URL("../src/features/security/SecurityDeepSecPage.tsx", import.meta.url),
  "utf8"
);
const apiSource = readFileSync(
  new URL("../src/features/security/api.ts", import.meta.url),
  "utf8"
);

function sliceBetween(text: string, startMarker: string, endMarker: string): string {
  const start = text.indexOf(startMarker);
  assert.notEqual(start, -1);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1);
  return text.slice(start, end);
}

const entitlementsPanel = pageSource.slice(
  pageSource.indexOf('{activeView === "data-permissions"')
);
const objectPicker = sliceBetween(
  pageSource,
  "function DeepSecTargetObjectPicker",
  "function DeepSecPlanSteps"
);

test("DeepSec 対象ロール list は 5 行相当の固定高 scroll container を使う", () => {
  assert.match(entitlementsPanel, /data-testid="security-deepsec-entitlement-roles"/u);
  assert.match(entitlementsPanel, /max-h-\[17\.5rem\]/u);
  assert.match(entitlementsPanel, /overflow-auto/u);
  assert.doesNotMatch(entitlementsPanel, /max-h-\[30rem\]/u);
});

test("DeepSec 検証結果は 5 件相当の最大高で縦スクロールする", () => {
  assert.match(
    entitlementsPanel,
    /role="region"[\s\S]*security\.deepsec\.resultListAriaLabel[\s\S]*tabIndex=\{0\}/u
  );
  assert.match(
    entitlementsPanel,
    /className="grid max-h-\[23\.25rem\] gap-2 overflow-y-auto overscroll-contain[^"]*"[\s\S]*data-testid="security-deepsec-verification-results"/u
  );
  assert.match(entitlementsPanel, /className="flex min-h-\[4\.25rem\] items-start/u);
});

test("DeepSec Data Grant editor は preview と独立した固定高 workspace を持つ", () => {
  assert.match(
    entitlementsPanel,
    /className="grid min-w-0 gap-3"[\s\S]*data-testid="security-deepsec-entitlement-form"/u
  );
  assert.doesNotMatch(
    entitlementsPanel,
    /grid-rows-\[auto_minmax\(0,1fr\)_auto\]/u
  );
  assert.match(
    entitlementsPanel,
    /className="h-\[36rem\] min-h-0 md:h-\[32rem\]"[\s\S]*data-testid="security-deepsec-entitlement-workspace-frame"/u
  );
  assert.match(
    entitlementsPanel,
    /className="[^"]*h-full[^"]*overflow-hidden[^"]*"[\s\S]*data-testid="security-deepsec-entitlement-workspace"/u
  );
  assert.match(
    entitlementsPanel,
    /className="[^"]*min-h-0[^"]*overflow-y-auto[^"]*"[\s\S]*data-testid="security-deepsec-entitlement-rules-list"/u
  );
  assert.match(
    entitlementsPanel,
    /className="min-h-0 overflow-y-auto pr-1"[\s\S]*ref=\{entitlementEditorScrollRef\}[\s\S]*security-deepsec-entitlement-rule-\$\{selectedEntitlementDraftIndex\}/u
  );
  assert.match(
    entitlementsPanel,
    /className="[^"]*border-t border-border[^"]*"[\s\S]*data-testid="security-deepsec-entitlement-action-region"/u
  );
  assert.match(entitlementsPanel, /security\.deepsec\.entitlements\.emptyRulesTitle/u);
  assert.match(entitlementsPanel, /security\.deepsec\.entitlements\.emptyRulesHint/u);
});

test("DeepSec object picker は単一選択なので共通の検索できる選択欄を使う（Issue 608）", () => {
  assert.match(entitlementsPanel, /<DeepSecTargetObjectPicker/u);
  // 大量の候補（数千件）から 1 つを選ぶ: SearchableSelectField をサーバー側の検索（remote）で使う（#578）。
  assert.match(objectPicker, /<SearchableSelectField/u);
  assert.match(objectPicker, /remote=\{\{[\s\S]*hasMore,[\s\S]*loadingMore,[\s\S]*searching: loading,[\s\S]*onLoadMore,/u);
  assert.match(objectPicker, /onQueryChange=\{onSearchChange\}/u);
  assert.match(objectPicker, /required/u);
  assert.match(objectPicker, /helper=\{t\("security\.deepsec\.entitlements\.oracleHelper"\)\}/u);
  // 手書きの listbox・所有者の欄・「さらに読み込む」のボタンは持たない（選択欄の一覧の下端で続きを読む）。
  assert.doesNotMatch(objectPicker, /role="listbox"/u);
  assert.doesNotMatch(objectPicker, /DbObjectSearchOwnerFields/u);
  assert.doesNotMatch(objectPicker, /security-deepsec-object-picker-load-more/u);
  assert.equal(t("security.deepsec.entitlements.objectSearchPlaceholder"), "スキーマ名・名前・コメントで検索");
  assert.doesNotMatch(entitlementsPanel, /<select[\s\S]*security\.deepsec\.entitlements\.resource/u);
  // 検索語は選択欄の SearchField が確定する（親で遅延させない。UX 契約「一覧の絞り込みの検索」7）。
  assert.doesNotMatch(pageSource, /void loadTargetObjectsRef\.current\(\);\s*\}, 250\)/u);
});

test("DeepSec object picker の読み込みの失敗は再試行付きで出す", () => {
  assert.match(
    objectPicker,
    /security\.deepsec\.entitlements\.objectLoadMoreError[\s\S]*security-deepsec-object-picker-retry-\$\{index\}[\s\S]*onClick=\{onRetry\}/u
  );
});

test("DeepSec target objects wrapper は専用 endpoint に cursor paging と所有者 prefix・検索を渡す", () => {
  assert.match(apiSource, /limit = 50/u);
  assert.match(apiSource, /\/api\/security\/deepsec\/target-objects/u);
  assert.doesNotMatch(apiSource, /\/api\/nl2sql\/db-admin\/objects/u);
  assert.match(apiSource, /params\.set\("q", q\.trim\(\)\)/u);
  assert.doesNotMatch(apiSource, /query_scope/u);
  assert.doesNotMatch(apiSource, /row_state/u);
  assert.match(apiSource, /params\.set\("owner_prefix", ownerPrefix\.trim\(\)\)/u);
  assert.doesNotMatch(apiSource, /params\.set\("owner", ownerPrefix\.trim\(\)\)/u);
  assert.match(apiSource, /params\.set\("cursor", cursor\)/u);
  assert.doesNotMatch(apiSource, /limit=100&type=all&row_state=all/u);
});

test("DeepSec DATA USER password sync wrapper は body なしで専用 endpoint を呼ぶ", () => {
  const syncBlock = sliceBetween(apiSource, "syncDeepSecConfigPassword", "applyDeepSecStep");

  assert.match(
    syncBlock,
    /apiPost<DeepSecStatus>\("\/api\/security\/deepsec\/config\/sync-password"\)/u
  );
  assert.doesNotMatch(syncBlock, /data_user_password/u);
});

test("DeepSec DATA USER password sync button は保存 action と独立している", () => {
  const dataUserPanel = sliceBetween(
    pageSource,
    'id="security-deepsec-panel-data-user"',
    '{activeView === "foundation"'
  );

  assert.match(dataUserPanel, /security\.deepsec\.config\.sync/u);
  assert.match(dataUserPanel, /icon=\{RefreshCw\}/u);
  assert.match(
    dataUserPanel,
    /type="button"[\s\S]*variant="secondary"[\s\S]*loading=\{configSyncing\}[\s\S]*disabled=\{passwordSyncDisabled \|\| actionBlocked\}/u
  );
  assert.match(
    pageSource,
    /!hasSavedDataUserPassword \|\| dataUserPassword \|\| configSaving \|\| configSyncing/u
  );
  assert.equal(t("security.deepsec.config.sync"), "Oracle へ同期");
  assert.equal(
    t("security.deepsec.config.synced"),
    "保存済み DATA USER パスワードを Oracle END USER へ同期しました。"
  );
});

test("DeepSec object picker は総件数未取得時に loaded 件数のみ表示する", () => {
  assert.match(objectPicker, /security\.deepsec\.entitlements\.objectLoadedCount/u);
  assert.match(
    pageSource,
    /setTargetObjectTotal\(typeof page\.total === "number" \? page\.total : null\)/u
  );
  assert.equal(t("security.deepsec.entitlements.objectLoadedCount", { loaded: 3 }), "3 件");
  assert.equal(
    t("security.deepsec.entitlements.objectEmptyUnfiltered"),
    "参照可能な対象 object がありません。"
  );
});

test("DeepSec Data Grant editor は必須表示を共有の SelectField / FieldLegend で統一する (#531 / #631)", () => {
  assert.match(pageSource, /SelectField,\s*TextField,\s*FieldLegend,\s*\} from "@production-ready\/ui"/u);
  assert.doesNotMatch(pageSource, /required-field"|<RequiredBadge\b/u);
  // 対象 object は検索できる選択欄（Issue 608）。「必須」は選択欄の required（ラベルの読み上げる「必須」のタグ）で出す。
  assert.match(objectPicker, /<SearchableSelectField[\s\S]*\brequired\b/u);
  // 行 scope はネイティブの select ではなく共有の SelectField（#631）。「必須」は required（ラベルの印と aria-required）で出す。
  assert.match(
    entitlementsPanel,
    /<SelectField\s+id=\{`deepsec-entitlement-scope-mode-\$\{index\}`\}\s+label=\{t\("security\.deepsec\.entitlements\.scopeMode"\)\}\s+required/u
  );
  assert.doesNotMatch(pageSource, /<select\b|<input\s+id="deepsec-data-user-password"/u);
  assert.match(entitlementsPanel, /<FieldLegend[\s\S]*required[\s\S]*security\.deepsec\.entitlements\.columns/u);
  assert.match(entitlementsPanel, /<fieldset className="grid gap-2"/u);
});

test("DeepSec 許可列は一括選択バーと明示的な余白を持つ", () => {
  assert.match(pageSource, /import \{[^}]*\bBulkSelectionActions\b[^}]*\} from "@production-ready\/ui"/u);
  assert.match(entitlementsPanel, /selectLabel=\{t\("common\.selection\.selectAll"\)\}/u);
  assert.match(entitlementsPanel, /clearLabel=\{t\("common\.selection\.clearAll"\)\}/u);
  assert.match(
    entitlementsPanel,
    /dataTestId=\{`security-deepsec-entitlement-column-selection-actions-\$\{index\}`\}/u
  );
  assert.match(
    entitlementsPanel,
    /data-testid=\{`security-deepsec-entitlement-columns-grid-\$\{index\}`\}/u
  );
  assert.match(entitlementsPanel, /className="mt-1 flex min-w-0 justify-start"/u);
  assert.match(entitlementsPanel, /className="mt-1 grid max-h-48 gap-2 overflow-auto/u);
  assert.match(pageSource, /useLayoutEffect\(\(\) => \{[\s\S]*pendingEntitlementScrollPositionsRef/u);
  assert.match(pageSource, /data-entitlement-scroll-container/u);
  assert.match(
    entitlementsPanel,
    /onSelectAll=\{\(\) =>[\s\S]*setEntitlementColumnsPreservingScroll\([\s\S]*availableColumnNames/u
  );
  assert.match(
    entitlementsPanel,
    /onClearAll=\{\(\) =>[\s\S]*setEntitlementColumnsPreservingScroll\(index, \[\]\)/u
  );
});

test("DeepSec Data Grant editor は対象 object、許可列、行 scope の順に表示する", () => {
  const targetPickerIndex = entitlementsPanel.indexOf("<DeepSecTargetObjectPicker");
  const columnsIndex = entitlementsPanel.indexOf(
    "security.deepsec.entitlements.columns",
    targetPickerIndex
  );
  const scopeModeIndex = entitlementsPanel.indexOf(
    "security.deepsec.entitlements.scopeMode",
    columnsIndex
  );

  assert.notEqual(targetPickerIndex, -1);
  assert.notEqual(columnsIndex, -1);
  assert.notEqual(scopeModeIndex, -1);
  assert.ok(targetPickerIndex < columnsIndex);
  assert.ok(columnsIndex < scopeModeIndex);
});

test("DeepSec Data Grant 行削除は icon-only ではなく短い削除ラベルを表示する", () => {
  assert.match(entitlementsPanel, /aria-label=\{t\("security\.deepsec\.entitlements\.remove"\)\}/u);
  assert.match(entitlementsPanel, /icon=\{Trash2\}>[\s\S]*security\.deepsec\.entitlements\.removeButtonLabel/u);
  assert.equal(t("security.deepsec.entitlements.remove"), "データ権限を削除");
  assert.equal(t("security.deepsec.entitlements.removeButtonLabel"), "削除");
});

test("DeepSec 条件は専用の条件グループ editor で表示する", () => {
  assert.match(pageSource, /ScopeExpressionEditor/u);
  assert.match(pageSource, /canonicalExpression/u);
  assert.match(pageSource, /expressionError/u);
  assert.doesNotMatch(entitlementsPanel, /<option value="COLUMN_EQUALS">/u);
});

test("DeepSec Data Grant editor はロール全体を preview と apply に渡す", () => {
  const applyButtonBlock = sliceBetween(
    entitlementsPanel,
    'variant="danger"',
    'onClick={() => void handleApplyEntitlements()}'
  );

  assert.match(apiSource, /previewDeepSecDataEntitlements/u);
  assert.match(apiSource, /data-entitlements\/\$\{role\.role_id\}\/preview/u);
  assert.match(apiSource, /version: role\.version/u);
  assert.match(apiSource, /data_entitlements: dataEntitlementPayload\(role\)/u);
  assert.match(entitlementsPanel, /security\.deepsec\.entitlements\.generatePreview/u);
  assert.match(entitlementsPanel, /handlePreviewEntitlements/u);
  assert.match(entitlementsPanel, /data-testid="security-deepsec-sql-preview"/u);
  assert.match(entitlementsPanel, /max-h-\[min\(28rem,45dvh\)\]/u);
  assert.match(entitlementsPanel, /overflow-y-auto overscroll-contain/u);
  assert.match(entitlementsPanel, /data-testid="security-deepsec-sql-preview-toolbar"/u);
  assert.match(entitlementsPanel, /data-testid="security-deepsec-sql-preview-generate"/u);
  assert.match(entitlementsPanel, /className="w-full min-w-0 justify-center lg:w-auto"/u);
  assert.match(
    pageSource,
    /const handleApplyEntitlements = async \(\) => \{[\s\S]*securityApi\.applyDeepSecDataEntitlements\([\s\S]*data_entitlements: normalizedEntitlementDraftRows/u
  );
  assert.doesNotMatch(
    sliceBetween(pageSource, "const handleApplyEntitlements", "return ("),
    /updateDeepSecDataEntitlements/u
  );
  assert.doesNotMatch(apiSource, /entitlement_ids/u);
  assert.match(entitlementsPanel, /selectedRoleCleanupSql/u);
  assert.match(entitlementsPanel, /selectedRolePreviewRows\.map/u);
  assert.match(entitlementsPanel, /savedEntitlementRows\.length === 0/u);
  assert.match(pageSource, /type DataEntitlementDraft = DataEntitlement & \{ client_key: string \}/u);
  assert.match(entitlementsPanel, /key=\{entitlement\.client_key\}/u);
  assert.match(entitlementsPanel, /data-testid=\{`security-deepsec-entitlement-rule-tab-\$\{index\}`\}/u);
  assert.match(entitlementsPanel, /aria-pressed=\{selected\}/u);
  assert.match(entitlementsPanel, /setSelectedEntitlementDraftKey\(entitlement\.client_key\)/u);
  assert.doesNotMatch(entitlementsPanel, /<details[\s\S]*security-deepsec-entitlement-rule-\$\{index\}/u);
  assert.doesNotMatch(apiSource, /client_key/u);
  assert.match(entitlementsPanel, /targetKey \|\| t\("security\.deepsec\.entitlements\.ruleTitle"\)/u);
  assert.doesNotMatch(
    entitlementsPanel,
    /t\("security\.deepsec\.entitlements\.ruleTitle",\s*\{\s*index: index \+ 1/u
  );
  assert.doesNotMatch(applyButtonBlock, /normalizedEntitlementDraftRows\.length === 0/u);
  assert.doesNotMatch(pageSource, /handleSaveEntitlements/u);
  assert.doesNotMatch(entitlementsPanel, /security\.deepsec\.entitlements\.save"/u);
  assert.doesNotMatch(entitlementsPanel, /security\.deepsec\.entitlements\.saveHelper/u);
  assert.doesNotMatch(entitlementsPanel, /security\.common\.save/u);
});
