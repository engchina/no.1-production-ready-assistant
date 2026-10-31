import {
  ConfirmProvider,
  Toaster,
  toast,
  useConfirm,
  RowActionMenu,
} from "@production-ready/ui";
import { MemoryRouter } from "react-router-dom";
import { DataTable, type DataTableSort } from "@production-ready/ui";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { Copy, Eye, Plus, RefreshCw, Trash2 } from "lucide-react";
import { Button, buttonVariants, FormActionBar, PageHeader, Pagination, TextField } from "@production-ready/ui";
import { ErrorState } from "../../src/components/StateViews";
import "../../src/globals.css";

function Standards() {
  const [count, setCount] = useState(0);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState(false);
  const [sort, setSort] = useState<DataTableSort>({ key: "name", direction: "asc" });
  const confirm = useConfirm();
  return <>
    <PageHeader title="ボタン UI 標準" subtitle="位置と機能に応じた共通アクション"
      actions={[
        { id: "create", kind: "primary", label: "新規作成", icon: Plus, onClick: () => setCount(c => c + 1) },
        { id: "refresh", kind: "utility", label: "最新情報を取得", icon: RefreshCw, onClick: () => setCount(c => c + 1) },
      ]} />
    <main style={{ padding: 16, display: "grid", gap: 24 }}>
      {(["sm", "md", "lg"] as const).map(size => <section key={size} aria-label={size}>
        <h2>{size} — {size === "sm" ? "ツールバー・一覧" : size === "md" ? "カード内の操作" : "フォーム・実行"}</h2>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 8 }}>
          {(["primary", "secondary", "ghost", "danger"] as const).map(variant =>
            <Button key={variant} size={size} variant={variant} icon={variant === "danger" ? Trash2 : Copy} data-testid={`${size}-${variant}`}
              onClick={() => setCount(c => c + 1)}>
              {{ primary: "保存する", secondary: "コピー", ghost: "選択解除", danger: "削除する" }[variant]}
            </Button>)}
          <Button size={size} icon={Plus} disabled data-testid={`${size}-disabled`}>保存不可</Button>
          <Button size={size} icon={Plus} loading data-testid={`${size}-loading`}>保存する</Button>
        </div>
      </section>)}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <Button iconOnly variant="ghost" aria-label="コピーする" data-testid="icon"><Copy aria-hidden /></Button>
        {/* 入力欄と、その横の操作は同じ size（既定 md）でそろえる。タッチ端末は両方 44px（#613）。 */}
        <TextField id="standards-field" label="入力" labelHidden width="sm" value="" onChange={() => {}} data-testid="field-input" />
        <Button variant="secondary" data-testid="field">入力横の操作</Button>
        <Button variant="secondary" tone="danger" data-testid="danger-trigger" onClick={async () => {
          if (await confirm({ title: "削除の確認", confirmLabel: "削除する", tone: "danger" })) setCount(c => c + 1);
        }}>削除</Button>
        <Button variant="secondary" aria-pressed={selected} onClick={() => setSelected(!selected)}>選択状態</Button>
        <a href="#results" className={buttonVariants({ variant: "secondary", size: "sm" })}>結果へ移動</a>
        <RowActionMenu ariaLabel="対象の操作" actions={[{ id: "delete", label: "削除", tone: "danger", onSelect: () => {} }]} />
      </div>
      <FormActionBar ariaLabel="フォーム操作" testId="form-actions"
        primaryActions={[{ id: "save", label: "変更内容を保存", icon: Copy, onClick: () => {} }]}
        secondaryActions={[{ id: "view", label: "保存済みの内容を確認", icon: Eye, href: "#results" }]}
        dangerActions={[{ id: "delete", label: "保存済みの内容を削除", icon: Trash2, onClick: () => {} }]} />
      <Pagination page={page} totalPages={3} onPageChange={setPage} range={{ start: (page - 1) * 10 + 1, end: page * 10, total: 30 }} />
      <ErrorState message="最新情報を取得できませんでした。" onRetry={() => setCount(c => c + 1)} />
      <Button variant="secondary" onClick={() => toast.success("保存しました", { duration: 0 })}>保存通知</Button>
      <Toaster />
      <output id="results">操作回数: {count}</output>
      <DataTable
        ariaLabel="一覧の列名"
        testId="sort-header-table"
        columns={[
          { key: "name", header: "名称", sortable: true, render: (row: { name: string }) => row.name },
          { key: "description", header: "説明", render: () => "確認用の項目" },
        ]}
        rows={(sort.direction === "asc" ? ["A", "B"] : ["B", "A"]).map(name => ({ name }))}
        getRowKey={(row: { name: string }) => row.name}
        sort={sort}
        onSortChange={setSort}
      />
    </main>
  </>;
}
createRoot(document.getElementById("root")!).render(<MemoryRouter><ConfirmProvider labels={{ confirm: "実行", cancel: "キャンセル" }}><Standards /></ConfirmProvider></MemoryRouter>);
