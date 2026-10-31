import {
  Button,
  Card,
  CardContent,
  ColumnBrowser,
  FieldError,
  ListSkeleton,
  ListToolbar,
  PAGE_SIZE_OPTIONS,
  PageBody,
  PageHeader,
  PagedDataTable,
  RowActionMenu,
  RowTitleButton,
  SearchField,
  TextField,
  TimedLoadingState,
  toast,
  type ColumnBrowserColumn,
  type ColumnBrowserItem,
  type EntityAction,
} from "@production-ready/ui";
import { ArrowDown, ArrowUp, Pencil, Plus, Save, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "@/components/security/AuthProvider";
import { ApiErrorState, EmptyState } from "@/components/StateViews";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { api, ApiError, type DocumentCategory } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { listCountProps, useClientListPaging } from "@/lib/list-paging";
import { listToolbarLabels, paginationLabels } from "@/lib/pagination-labels";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import { useDocumentCategories, useDocumentCategoryMutation } from "@/lib/queries";

/** 段（1 = 大分類、2 = 中分類、3 = 小分類）の名前の i18n キー。 */
const LEVEL_KEYS = {
  1: "documentCategories.level.large",
  2: "documentCategories.level.middle",
  3: "documentCategories.level.small",
} as const;
const MAX_LEVEL = 3;
const NAME_MAX_LENGTH = 200;
/** 選んだ大分類・中分類を残す URL の query（共有・再読込で同じ位置を開く）。 */
const SELECTION_PARAMS = { 1: "large", 2: "middle" } as const;

/** 開いている入力（新しい分類の追加・名前の変更）。同時に 1 つだけ開く。 */
type Editing = { mode: "add"; parentId: string | null; level: number } | { mode: "rename"; node: DocumentCategory };

/** 検索の一致の行（分類と、その上の段の名前）。 */
interface CategoryMatch {
  node: DocumentCategory;
  ancestors: DocumentCategory[];
}

function levelLabel(level: number): string {
  return t(LEVEL_KEYS[level as keyof typeof LEVEL_KEYS] ?? LEVEL_KEYS[1]);
}

function failureMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/** 比べる形（NFKC・小文字）。全角・半角の英数字の違いで当たり外れが変わらないようにする。 */
function searchKey(value: string): string {
  return value.normalize("NFKC").toLowerCase();
}

function flatten(nodes: readonly DocumentCategory[], ancestors: DocumentCategory[] = []): CategoryMatch[] {
  return nodes.flatMap((node) => [{ node, ancestors }, ...flatten(node.children, [...ancestors, node])]);
}

/**
 * 文書の分類の一覧（大分類 → 中分類 → 小分類。#2131）を、段ごとの 3 列のカラムブラウザで管理する（#2141）。
 * 大分類を選ぶと右の列にその下の中分類、中分類を選ぶと小分類を出す。各列は中で縦にスクロールし、ページ全体を
 * 伸ばさない。選んだ位置は URL（`?large=` / `?middle=`）に残す。上の検索欄は全段の名前を対象にし、一致した分類を
 * 段のパスつきで一覧して、選ぶとその位置を開く（狭い画面でもその分類の列を出し、その行を選択中にしてフォーカスする。
 * #2143）。
 *
 * ここで作った分類を、文書の詳細と RAG 検索で選ぶ（どちらも必須ではない）。操作はその場で保存する（1 件ずつの
 * 操作で、まとめて保存するフォームではない）。名前を変えると、その分類を使っている文書の分類の値も変わる。下に
 * 分類がある・文書が使っている分類は削除できない（backend が理由を返す）。
 */
export function DocumentCategoriesClient() {
  const query = useDocumentCategories();
  const confirm = useConfirm();
  const canManage = useAuth().hasPermission(MENU_PERMISSIONS.documentCategories);
  const [searchParams, setSearchParams] = useSearchParams();
  const [editing, setEditing] = useState<Editing | null>(null);
  const [search, setSearch] = useState("");
  // 検索の結果から開いた分類（狭い画面でもその列を出し、行を選択中の印にしてフォーカスする。#2143）。
  // 列の行を選ぶ・戻るを押したら外し、一番右の列を出す。
  const [opened, setOpened] = useState<{ level: number; id: string } | null>(null);
  // フォーカスを移す行。URL の選択（`setSearchParams`）は後の描画で効くので、行が描かれるまで待つ。
  const pendingFocus = useRef<string | null>(null);
  const create = useDocumentCategoryMutation(api.createDocumentCategory);
  const rename = useDocumentCategoryMutation(({ id, name }: { id: string; name: string }) =>
    api.renameDocumentCategory(id, name),
  );
  const move = useDocumentCategoryMutation(({ id, direction }: { id: string; direction: "up" | "down" }) =>
    api.moveDocumentCategory(id, direction),
  );
  const remove = useDocumentCategoryMutation(api.deleteDocumentCategory);
  const busy = create.isPending || rename.isPending || move.isPending || remove.isPending;

  const tree = useMemo(() => query.data ?? [], [query.data]);
  const all = useMemo(() => flatten(tree), [tree]);
  // URL の選択は、今の一覧にある分類だけを使う（削除された・別の大分類の中分類は選んでいないものとして扱う）。
  const large = tree.find((node) => node.id === searchParams.get(SELECTION_PARAMS[1])) ?? null;
  const middle = large?.children.find((node) => node.id === searchParams.get(SELECTION_PARAMS[2])) ?? null;

  const keyword = searchKey(search.trim());
  const matches = useMemo(
    () => (keyword ? all.filter(({ node }) => searchKey(node.name).includes(keyword)) : []),
    [all, keyword],
  );
  const paging = useClientListPaging("documentCategories.searchPaging", keyword);

  /** 大分類・中分類の選択を URL に残す（履歴を積まない）。`null` は外す。下の段の選択は外す。 */
  const select = (selection: { large: string | null; middle?: string | null }) =>
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        for (const [key, value] of [
          [SELECTION_PARAMS[1], selection.large],
          [SELECTION_PARAMS[2], selection.middle ?? null],
        ] as const) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        return next;
      },
      { replace: true },
    );

  const openMatch = ({ node, ancestors }: CategoryMatch) => {
    const path = [...ancestors, node];
    select({ large: path[0]?.id ?? null, middle: path.length >= 2 ? path[1].id : null });
    setOpened({ level: node.level, id: node.id });
    pendingFocus.current = node.id;
    setSearch("");
  };

  // 開いた分類の行が描かれたら、その行へフォーカスを移す（列の中のスクロールもその行へ寄る）。
  useEffect(() => {
    if (!pendingFocus.current) return;
    const row = document.querySelector<HTMLButtonElement>(
      `[data-testid="document-category-${CSS.escape(pendingFocus.current)}"] [data-column-browser-row]`,
    );
    if (!row) return;
    row.focus();
    pendingFocus.current = null;
  });

  const onDelete = async (node: DocumentCategory) => {
    const confirmed = await confirm({
      title: t("documentCategories.delete.title", { name: node.name }),
      description: t("documentCategories.delete.description"),
      confirmLabel: t("common.delete"),
      tone: "danger",
    });
    if (!confirmed) return;
    remove.mutate(node.id, {
      onSuccess: () => {
        toast.success(t("documentCategories.delete.done", { name: node.name }));
        // 選んでいた分類を消したら、その段から下の選択を外す。
        if (node.id === large?.id) select({ large: null });
        else if (node.id === middle?.id) select({ large: large?.id ?? null });
      },
      onError: (error) => toast.error(failureMessage(error, t("documentCategories.error.delete"))),
    });
  };

  const onMove = (node: DocumentCategory, direction: "up" | "down") =>
    move.mutate(
      { id: node.id, direction },
      { onError: (error) => toast.error(failureMessage(error, t("documentCategories.error.move"))) },
    );

  const rowActions = (node: DocumentCategory, index: number, siblingCount: number): EntityAction[] => [
    {
      id: "rename",
      label: t("documentCategories.action.rename"),
      icon: Pencil,
      disabled: busy,
      onSelect: () => setEditing({ mode: "rename", node }),
    },
    {
      id: "up",
      label: t("documentCategories.action.moveUp"),
      icon: ArrowUp,
      disabled: busy || index === 0,
      onSelect: () => onMove(node, "up"),
    },
    {
      id: "down",
      label: t("documentCategories.action.moveDown"),
      icon: ArrowDown,
      disabled: busy || index === siblingCount - 1,
      onSelect: () => onMove(node, "down"),
    },
    {
      id: "delete",
      label: t("documentCategories.action.delete"),
      icon: Trash2,
      tone: "danger",
      disabled: busy,
      onSelect: () => void onDelete(node),
    },
  ];

  const nameForm = (current: Editing) => (
    <CategoryNameForm
      key={current.mode === "rename" ? `rename-${current.node.id}` : `add-${current.parentId ?? "root"}`}
      idSuffix={current.mode === "rename" ? current.node.id : (current.parentId ?? "root")}
      label={
        current.mode === "rename"
          ? t("documentCategories.field.rename", { level: levelLabel(current.node.level) })
          : t("documentCategories.field.add", { level: levelLabel(current.level) })
      }
      initialName={current.mode === "rename" ? current.node.name : ""}
      submitLabel={current.mode === "rename" ? t("documentCategories.action.saveName") : t("documentCategories.action.add")}
      pending={create.isPending || rename.isPending}
      onCancel={() => setEditing(null)}
      onSubmit={(name, setError) => {
        if (current.mode === "rename") {
          rename.mutate(
            { id: current.node.id, name },
            {
              onSuccess: (result) => {
                setEditing(null);
                toast.success(
                  result.updated_document_count > 0
                    ? t("documentCategories.rename.doneWithDocuments", { count: result.updated_document_count })
                    : t("documentCategories.rename.done"),
                );
              },
              onError: (error) => setError(failureMessage(error, t("documentCategories.error.save"))),
            },
          );
          return;
        }
        create.mutate(
          { parent_id: current.parentId, name },
          {
            onSuccess: () => {
              setEditing(null);
              toast.success(t("documentCategories.add.done", { name }));
            },
            onError: (error) => setError(failureMessage(error, t("documentCategories.error.save"))),
          },
        );
      }}
    />
  );

  /** 1 つの段の列。`parent` は上の段で選んだ分類（大分類の列は `null`）、`nodes` はこの段の分類。 */
  const column = (
    level: number,
    parent: DocumentCategory | null,
    nodes: readonly DocumentCategory[] | null,
    selectedId: string | null,
    onSelect?: (id: string) => void,
  ): ColumnBrowserColumn => {
    const parentId = parent?.id ?? null;
    const adding = editing?.mode === "add" && editing.parentId === parentId && editing.level === level;
    const items: ColumnBrowserItem[] = (nodes ?? []).map((node) => ({
      id: node.id,
      label: node.name,
      meta:
        node.level < MAX_LEVEL
          ? t("documentCategories.meta.withChildren", {
              level: levelLabel(node.level + 1),
              children: formatNumber(node.children.length),
              count: formatNumber(node.document_count),
            })
          : t("documentCategories.documentCount", { count: formatNumber(node.document_count) }),
      hasChildren: node.level < MAX_LEVEL,
      testId: `document-category-${node.id}`,
      actions: canManage ? (
        <RowActionMenu
          actions={rowActions(node, (nodes ?? []).indexOf(node), (nodes ?? []).length)}
          ariaLabel={t("documentCategories.actions.aria", { name: node.name })}
          testId={`document-category-actions-${node.id}`}
        />
      ) : undefined,
    }));
    return {
      id: `document-category-level-${level}`,
      testId: `document-category-column-${level}`,
      title: parent
        ? t("documentCategories.column.childrenOf", { parent: parent.name, level: levelLabel(level) })
        : levelLabel(level),
      count: nodes ? t("documentCategories.column.count", { count: formatNumber(nodes.length) }) : undefined,
      headerAction:
        canManage && nodes ? (
          <Button
            size="sm"
            variant="secondary"
            icon={Plus}
            disabled={busy || adding}
            onClick={() => setEditing({ mode: "add", parentId, level })}
          >
            {t("documentCategories.action.addChild", { level: levelLabel(level) })}
          </Button>
        ) : undefined,
      items,
      selectedId,
      onSelect,
      placeholder: nodes
        ? undefined
        : t("documentCategories.column.placeholder", { parent: levelLabel(level - 1), level: levelLabel(level) }),
      before: adding && editing ? <div className="border-b border-border p-2">{nameForm(editing)}</div> : undefined,
      empty: adding ? undefined : (
        <EmptyState
          title={t("documentCategories.column.empty", { level: levelLabel(level) })}
          hint={
            canManage
              ? t("documentCategories.column.emptyHint", {
                  action: t("documentCategories.action.addChild", { level: levelLabel(level) }),
                })
              : undefined
          }
        />
      ),
      renderItem: (item) =>
        editing?.mode === "rename" && editing.node.id === item.id ? <div className="p-1">{nameForm(editing)}</div> : undefined,
    };
  };

  const columns = [
    column(1, null, tree, large?.id ?? null, (id) => {
      setOpened(null);
      select({ large: id });
    }),
    column(2, large, large?.children ?? null, middle?.id ?? null, (id) => {
      setOpened(null);
      select({ large: large?.id ?? null, middle: id });
    }),
    // 小分類は下の段が無いので選ばない。検索から開いた小分類だけ選択中の印にする。
    column(3, middle, middle?.children ?? null, opened?.level === 3 ? opened.id : null),
  ];

  return (
    <div>
      <PageHeader wide title={t("nav.documentCategories")} subtitle={t("documentCategories.subtitle")} />
      <PageBody wide>
        <Card>
          <CardContent className="space-y-3 pt-5">
            <p className="text-xs leading-relaxed text-fg-muted">{t("documentCategories.description")}</p>
            <ListToolbar
              search={
                <SearchField
                  id="document-category-search"
                  label={t("documentCategories.search.label")}
                  labelHidden
                  value={search}
                  onSearch={setSearch}
                  clearLabel={t("common.clearSearch")}
                  resultCountLabel={
                    keyword ? t("common.searchResultCount", { count: formatNumber(matches.length) }) : ""
                  }
                  placeholder={t("documentCategories.search.placeholder")}
                />
              }
              searchActive={keyword !== ""}
              labels={listToolbarLabels()}
              count={listCountProps({
                matchedTotal: query.data ? (keyword ? matches.length : all.length) : undefined,
                filtered: keyword !== "",
                allTotal: all.length,
              })}
              testId="document-category-toolbar"
            />
            {query.isPending ? (
              <TimedLoadingState label={t("documentCategories.loading")} operationKey="document-categories">
                <ListSkeleton />
              </TimedLoadingState>
            ) : query.isError ? (
              <ApiErrorState
                error={query.error}
                fallback={t("documentCategories.error.load")}
                onRetry={() => void query.refetch()}
              />
            ) : keyword ? (
              matches.length === 0 ? (
                <EmptyState title={t("documentCategories.search.empty")} />
              ) : (
                <PagedDataTable<CategoryMatch>
                  paginationLabels={paginationLabels()}
                  {...paging}
                  pageSizeOptions={PAGE_SIZE_OPTIONS}
                  paginationAriaLabel={t("documentCategories.search.pagination")}
                  ariaLabel={t("documentCategories.search.results")}
                  rows={matches}
                  getRowKey={(row) => row.node.id}
                  columns={[
                    {
                      key: "name",
                      header: t("documentCategories.search.column.name"),
                      rowHeader: true,
                      render: (row) => (
                        <RowTitleButton
                          title={row.node.name}
                          subtitle={
                            row.ancestors.length > 0 ? row.ancestors.map((item) => item.name).join(" › ") : undefined
                          }
                          aria-label={t("documentCategories.search.open", { name: row.node.name })}
                          onClick={() => openMatch(row)}
                        />
                      ),
                    },
                    {
                      key: "level",
                      header: t("documentCategories.search.column.level"),
                      render: (row) => levelLabel(row.node.level),
                    },
                    {
                      key: "documents",
                      header: t("documentCategories.search.column.documents"),
                      align: "right",
                      className: "tabular-nums",
                      render: (row) => formatNumber(row.node.document_count),
                    },
                  ]}
                  testId="document-category-search-results"
                />
              )
            ) : (
              <ColumnBrowser
                columns={columns}
                ariaLabel={t("documentCategories.browser.label")}
                activeColumnId={opened ? `document-category-level-${opened.level}` : undefined}
                backLabel={(index) => t("documentCategories.back", { level: levelLabel(index) })}
                onBack={(index) => {
                  setOpened(null);
                  if (index === 1) select({ large: null });
                  else select({ large: large?.id ?? null });
                }}
                testId="document-category-browser"
              />
            )}
          </CardContent>
        </Card>
      </PageBody>
    </div>
  );
}

/** 分類の名前の入力（追加・名前の変更）。列の幅に収まるよう、欄の下に操作を置く。保存の失敗は欄の下に出す。 */
function CategoryNameForm({
  idSuffix,
  label,
  initialName,
  submitLabel,
  pending,
  onCancel,
  onSubmit,
}: {
  idSuffix: string;
  label: string;
  initialName: string;
  submitLabel: string;
  pending: boolean;
  onCancel: () => void;
  onSubmit: (name: string, setError: (message: string) => void) => void;
}) {
  const [name, setName] = useState(initialName);
  const [error, setError] = useState("");
  const errorId = `document-category-name-${idSuffix}-error`;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError(t("documentCategories.error.nameRequired"));
      return;
    }
    setError("");
    onSubmit(trimmed, setError);
  };
  return (
    <form
      onSubmit={submit}
      className="space-y-2 rounded-md bg-surface-sunken p-2"
      data-testid="document-category-form"
    >
      <TextField
        id={`document-category-name-${idSuffix}`}
        label={label}
        width="full"
        value={name}
        maxLength={NAME_MAX_LENGTH}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        autoFocus
        disabled={pending}
        onValueChange={(value) => {
          setName(value);
          if (error) setError("");
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
        }}
      />
      <FieldError id={errorId} message={error} />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" icon={Save} loading={pending}>
          {submitLabel}
        </Button>
        <Button type="button" variant="secondary" icon={X} disabled={pending} onClick={onCancel}>
          {t("common.cancel")}
        </Button>
      </div>
    </form>
  );
}
