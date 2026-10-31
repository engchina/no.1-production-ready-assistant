import { Plus, RefreshCw, Table2 } from "lucide-react";
import { useId, useMemo, useState } from "react";

import {
  Button,
  Banner,
  EmptyState,
  LoadMoreFooter,
  Skeleton,
  TimedLoadingState,
  DisclosureChevron,
  ListToolbar,
  SearchField,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { SchemaRefreshProcessing } from "./SchemaRefreshFeedback";
import {
  buildSchemaInsertText,
  buildSchemaSqlIdentifierText,
  buildTableInsertText,
  buildTableSqlIdentifierText,
  normalizeObjectIdentifier,
  schemaTableQualifiedName,
} from "../workbenchState";
import { formatDbObjectPart } from "../dbObjectIdentity";
import { DbObjectName } from "./DbObjectName";
import { formatSampleValues, formatSchemaCount } from "../schemaDisplay";
import { schemaReferenceListCount } from "../schemaReferenceCount";
import type { SchemaCatalog, SchemaColumn, SchemaTable } from "../types";

type SchemaInsertMode = "logical" | "physical";

/**
 * クエリ/SQL への挿入補助に特化した compact なスキーマピッカー。
 * 業界のスキーマブラウザ慣行に合わせ、1 行密度のツリー + 行クリック=挿入 +
 * 詳細（型・サンプル・コメント）は tooltip、検索時は一致テーブルを自動展開する。
 */
export function SchemaReferencePanel({
  catalog,
  loading,
  profileUnselected = false,
  disabled,
  insertMode = "logical",
  allowedTableNames = null,
  scopeAppliedByServer = false,
  listMaxHeightClass = "max-h-72",
  onRefreshSchema,
  refreshing = false,
  refreshStarting = false,
  showRefreshProgress = true,
  searchQuery,
  onSearchQueryChange,
  hasMore = false,
  loadingMore = false,
  loadMoreError = "",
  totalCount,
  unfilteredTotalCount,
  detailLoadError = "",
  onLoadMore,
  onRetryLoadMore,
  onDismissDetailLoadError,
  onExpandTable,
  onInsert,
}: {
  catalog: SchemaCatalog | null;
  loading: boolean;
  /**
   * 業務プロファイルが未選択・使えないので表を取りに行っていない（#3413）。読み込み中にせず、選ぶと表と列を出すと
   * 案内する（検索欄は無効、件数・「スキーマ未取得です」の案内は出さない）。
   */
  profileUnselected?: boolean;
  disabled?: boolean;
  insertMode?: SchemaInsertMode;
  /** 非 null のとき、この表名集合（正規化比較）に絞り込む。null は全表表示。 */
  allowedTableNames?: string[] | null;
  /**
   * API が同じ業務プロファイルの許可する表で絞って返す（`profile_id` を渡している）とき true。
   * `allowedTableNames` で絞っていても、API の total（`totalCount`）をそのまま件数に使う（#1891）。
   */
  scopeAppliedByServer?: boolean;
  /** リスト領域の最大高さ Tailwind クラス。 */
  listMaxHeightClass?: string;
  /** catalog が空のときに「スキーマを更新」導線を出す（POST /api/schema/refresh-jobs）。 */
  onRefreshSchema?: () => void;
  /** スキーマの更新の送信中または job の実行中（進行の表示を出し、更新のボタンを無効にする）。 */
  refreshing?: boolean;
  /**
   * このパネルの「スキーマを更新」で始めた更新の送信中。このときだけボタンを loading にする。job の間と、
   * ほかのボタン（ページヘッダーなど）で始めた更新では無効にするだけ（buttons §8、#821）。
   */
  refreshStarting?: boolean;
  /**
   * 更新の進行をこのパネルに出すか（既定 true）。案内の中のボタンで始めた更新は、その案内が進行を出すので
   * false を渡す（同じ処理の詳細表示は 1 つ。messaging §3.7・§10.2、#2034）。
   */
  showRefreshProgress?: boolean;
  searchQuery?: string;
  onSearchQueryChange?: (value: string) => void;
  hasMore?: boolean;
  loadingMore?: boolean;
  loadMoreError?: string;
  /**
   * 今の検索語に一致した全件（API の total。任意）。読み込んだ範囲の外にも続きがあるとき（hasMore）の件数に使う。
   * `allowedTableNames` で画面の側だけが絞るときは API の total が表示の件数と合わないので使わない
   * （`scopeAppliedByServer` のときは使う）。
   */
  totalCount?: number | null;
  /** 絞り込む前の全件（任意。「N 件（全 M 件）」の M）。 */
  unfilteredTotalCount?: number | null;
  detailLoadError?: string;
  onLoadMore?: () => void;
  onRetryLoadMore?: () => void;
  onDismissDetailLoadError?: () => void;
  onExpandTable?: (table: SchemaTable) => void;
  onInsert: (text: string) => void;
}) {
  const [localQuery, setLocalQuery] = useState("");
  const query = searchQuery ?? localQuery;
  const searchId = useId();
  const setQuery = (value: string) => {
    if (onSearchQueryChange) onSearchQueryChange(value);
    else setLocalQuery(value);
  };
  // アコーディオン: 同時に展開できる表は 1 つだけ（外側スクロール量を抑える）。
  const [expandedTable, setExpandedTable] = useState<string | null>(null);

  const allowedSet = useMemo(
    () =>
      allowedTableNames
        ? new Set(allowedTableNames.map(normalizeObjectIdentifier))
        : null,
    [allowedTableNames]
  );

  const catalogEmpty = (catalog?.tables.length ?? 0) === 0;
  const searching = query.trim().length > 0;

  const scopedTables = useMemo(() => {
    if (!catalog) return [];
    if (!allowedSet) return catalog.tables;
    return catalog.tables.filter((table) => {
      const qualifiedName = normalizeObjectIdentifier(schemaTableQualifiedName(table));
      // table_name はカタログ上の名前（引用符なし）。引用が必要な名前だけ `"..."` で囲んで照合する。
      const objectName = formatDbObjectPart(table.table_name);
      // legacy profile は非修飾名、新しい cross-schema profile は OWNER.OBJECT を保持する。
      return allowedSet.has(qualifiedName) || allowedSet.has(objectName);
    });
  }, [catalog, allowedSet]);

  const filteredTables = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return scopedTables;
    // controlled query は server-side 検索済み。summary に未取得の列が無くても結果を隠さない。
    if (onSearchQueryChange) return scopedTables;
    return scopedTables.filter((table) => {
      return (
        table.owner.toLowerCase().includes(normalized) ||
        schemaTableQualifiedName(table).toLowerCase().includes(normalized) ||
        table.table_name.toLowerCase().includes(normalized) ||
        table.logical_name.toLowerCase().includes(normalized) ||
        table.columns.some(
          (column) =>
            column.column_name.toLowerCase().includes(normalized) ||
            column.logical_name.toLowerCase().includes(normalized)
        )
      );
    });
  }, [onSearchQueryChange, scopedTables, query]);

  // 絞り込む前の全件（「N 件（全 M 件）」の M）。検索語の無いときに全件を読み終えた件数を覚えておく
  // （検索語は API 側で絞るため、検索中の一覧からは分からない。#1891）。
  const [knownUnfilteredCount, setKnownUnfilteredCount] = useState<number | null>(null);
  // 一覧の読み込み中と業務プロファイルの未選択では、表の一覧・件数・空の案内を出さない。
  const listHidden = loading || profileUnselected;
  const loadedAllUnfiltered = !listHidden && !searching && !hasMore && catalog !== null;
  const loadedUnfilteredCount = scopedTables.length;
  // render の中で前の値と比べて合わせる（effect で setState しない。React の「props の変化で state を合わせる」型）。
  if (loadedAllUnfiltered && knownUnfilteredCount !== loadedUnfilteredCount) {
    setKnownUnfilteredCount(loadedUnfilteredCount);
  }
  const listCount = schemaReferenceListCount({
    loading: listHidden,
    searching,
    hasMore,
    scoped: allowedSet !== null,
    serverScoped: scopeAppliedByServer,
    shown: filteredTables.length,
    scopedTotal: scopedTables.length,
    serverSearch: Boolean(onSearchQueryChange),
    totalCount,
    unfilteredTotalCount: unfilteredTotalCount ?? knownUnfilteredCount,
  });

  const toggleExpanded = (qualifiedName: string) => {
    setExpandedTable((current) => {
      const next = current === qualifiedName ? null : qualifiedName;
      if (next) {
        const table = catalog?.tables.find(
          (item) => schemaTableQualifiedName(item) === qualifiedName
        );
        if (table) onExpandTable?.(table);
      }
      return next;
    });
  };

  return (
    <section
      className="grid min-w-0 max-w-full content-start gap-2 overflow-hidden"
      aria-label={t("nl2sql.schema.title")}
      data-testid="nl2sql-schema-reference"
    >
      <p className="flex min-w-0 items-center gap-2 text-sm font-semibold text-fg">
        <Table2 size={16} className="shrink-0" aria-hidden="true" />
        <span>{t("nl2sql.schema.title")}</span>
        <span className="min-w-0 truncate text-xs font-normal text-fg-muted">
          {t("nl2sql.schema.insertHint")}
        </span>
      </p>

      {/* 表の一覧の絞り込み（入力に合わせて適用。debounce・IME 対応は SearchField。#535）と件数（ListCount。#1891）は
          共通の ListToolbar。狭いパネルでは検索欄を全幅に置き、件数をその下に出す。 */}
      <ListToolbar
        testId="nl2sql-schema-toolbar"
        search={
          <SearchField
            id={`nl2sql-schema-search-${searchId}`}
            label={t("nl2sql.schema.search")}
            labelHidden
            value={query}
            onSearch={setQuery}
            clearLabel={t("common.clearSearch")}
            placeholder={t("nl2sql.schema.searchPlaceholder")}
            disabled={disabled || profileUnselected}
            className="min-w-0 max-w-full"
          />
        }
        count={listCount}
      />

      {profileUnselected ? (
        <div data-testid="nl2sql-schema-profile-unselected">
          <EmptyState
            title={t("nl2sql.schema.profileUnselected.title")}
            hint={t("nl2sql.schema.profileUnselected.hint")}
          />
        </div>
      ) : null}

      {loading && !profileUnselected && (
        <TimedLoadingState
          label={t("nl2sql.schema.loading")}
          operationKey="schema-reference-load"
          placement="panel"
          framed={false}
          testId="schema-reference-loading"
        >
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
        </TimedLoadingState>
      )}
      {!loading && refreshing && showRefreshProgress ? (
        <SchemaRefreshProcessing
          placement="panel"
          className="rounded-md border border-border bg-surface px-3 py-2"
          testId="schema-reference-refreshing"
        />
      ) : null}

      {!listHidden && detailLoadError ? (
        <div data-testid="nl2sql-schema-detail-error">
          <Banner
            severity="danger"
            action={onDismissDetailLoadError ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={disabled}
                onClick={onDismissDetailLoadError}
              >
                {t("common.dismiss")}
              </Button>
            ) : undefined}
          >
            {detailLoadError}
          </Banner>
        </div>
      ) : null}

      {!listHidden && filteredTables.length === 0 && catalogEmpty && onRefreshSchema && (
        <div className="grid min-w-0 gap-3 rounded-md border border-dashed border-border p-4 text-sm text-fg-muted">
          <p className="[overflow-wrap:anywhere]">{t("nl2sql.schema.emptyCatalog")}</p>
          <div>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              loading={refreshStarting}
              disabled={disabled || (refreshing && !refreshStarting)}
              onClick={onRefreshSchema} icon={RefreshCw}>
              <span>{t("nl2sql.schema.refresh")}</span>
            </Button>
          </div>
        </div>
      )}

      {!listHidden && filteredTables.length === 0 && !(catalogEmpty && onRefreshSchema) && (
        <p className="min-w-0 rounded-md border border-dashed border-border p-3 text-sm text-fg-muted [overflow-wrap:anywhere]">
          {t("nl2sql.schema.empty")}
        </p>
      )}

      {!listHidden && (
      <div
        className={`grid min-w-0 max-w-full ${listMaxHeightClass} content-start gap-1 overflow-x-hidden overflow-y-auto pr-1`}
      >
        {filteredTables.map((table) => (
          <SchemaTableItem
            key={schemaTableQualifiedName(table)}
            table={table}
            expanded={searching || expandedTable === schemaTableQualifiedName(table)}
            disabled={disabled}
            onToggleExpanded={toggleExpanded}
            insertMode={insertMode}
            onInsert={onInsert}
          />
        ))}
        {/* 「さらに読み込む」と失敗の再試行は共通の LoadMoreFooter（#1266）。件数は見出しの側で出すので summary は空。
            パネルが無効（実行中）の間は操作も無効にする。 */}
        {((hasMore && onLoadMore) || loadMoreError) && (
          <LoadMoreFooter
            summary=""
            hasMore={hasMore}
            loadingMore={loadingMore}
            loadMoreError={loadMoreError || undefined}
            disabled={disabled}
            onLoadMore={onLoadMore}
            onRetry={onRetryLoadMore}
            loadMoreLabel={t("profiles.action.loadMore")}
            retryLabel={t("common.retry")}
          />
        )}
      </div>
      )}
    </section>
  );
}

function SchemaTableItem({
  table,
  expanded,
  disabled,
  insertMode,
  onToggleExpanded,
  onInsert,
}: {
  table: SchemaTable;
  expanded: boolean;
  disabled?: boolean;
  insertMode: SchemaInsertMode;
  onToggleExpanded: (tableName: string) => void;
  onInsert: (text: string) => void;
}) {
  const tableInsertText =
    insertMode === "physical" ? buildTableSqlIdentifierText(table) : buildTableInsertText(table);
  return (
    <article
      className="min-w-0 max-w-full overflow-hidden rounded-md border border-border bg-surface"
      data-testid="nl2sql-schema-table-item"
    >
      <div className="flex min-h-9 w-full min-w-0 items-stretch">
        {/* chevron=展開/折りたたみ、表名クリック=挿入（列行と同じ「クリック=挿入」で一貫）。 */}
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          type="button"
          className="shrink-0"
          aria-expanded={expanded}
          aria-label={t("nl2sql.schema.toggleTable", { name: table.logical_name })}
          onClick={() => onToggleExpanded(schemaTableQualifiedName(table))}
        >
          <DisclosureChevron expanded={expanded} size={16} />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          type="button"
          disabled={disabled}
          title={table.comment || schemaTableQualifiedName(table)}
          onClick={() => onInsert(tableInsertText)}
          className="group w-full min-w-0 flex-1 justify-between"
        >
          <span className="min-w-0 truncate text-sm font-medium text-fg">
            {table.logical_name}
          </span>
          {/* 所有者は修飾名に含まれるため、別の owner バッジは置かない（#555）。 */}
          <DbObjectName
            value={schemaTableQualifiedName(table)}
            size="xs"
            interactive
            truncate
            className="min-w-0"
            data-testid="nl2sql-schema-table-qualified-name"
          />
          <span className="ml-auto flex shrink-0 items-center gap-1.5 text-xs text-fg-muted">
            {t("schema.table.rows", { count: formatSchemaCount(table.row_count) })}
            <Plus
              size={14}
              className="text-accent-fg opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 motion-reduce:transition-none"
              aria-hidden="true"
            />
          </span>
        </Button>
      </div>
      {expanded && (
        <ul className="grid min-w-0 content-start gap-0.5 border-t border-border p-1">
          {table.columns.map((column) => (
            <SchemaColumnItem
              key={column.column_name}
              table={table}
              column={column}
              disabled={disabled}
              insertMode={insertMode}
              onInsert={onInsert}
            />
          ))}
        </ul>
      )}
    </article>
  );
}

function columnTooltip(table: SchemaTable, column: SchemaColumn): string {
  const lines = [
    `${schemaTableQualifiedName(table)}.${column.column_name} · ${column.data_type}${column.nullable ? "" : " · NOT NULL"}`,
  ];
  if (column.comment) lines.push(column.comment);
  if (column.sample_values.length > 0) {
    lines.push(`${t("schema.col.sample")}: ${formatSampleValues(column.sample_values)}`);
  }
  return lines.join("\n");
}

function SchemaColumnItem({
  table,
  column,
  disabled,
  insertMode,
  onInsert,
}: {
  table: SchemaTable;
  column: SchemaColumn;
  disabled?: boolean;
  insertMode: SchemaInsertMode;
  onInsert: (text: string) => void;
}) {
  const insertText =
    insertMode === "physical"
      ? buildSchemaSqlIdentifierText(table, column)
      : buildSchemaInsertText(table, column);
  return (
    <li className="min-w-0">
      <Button
        variant="ghost"
        size="sm"
        type="button"
        disabled={disabled}
        title={columnTooltip(table, column)}
        onClick={() => onInsert(insertText)}
        className="group w-full min-w-0 flex-1 justify-between" trailingIcon={Plus}>
        <span className="min-w-0 truncate text-sm text-fg">{column.logical_name}</span>
        <span className="min-w-0 truncate font-mono text-xs text-fg-muted">
          {column.column_name} · {column.data_type}
        </span>
      </Button>
    </li>
  );
}
