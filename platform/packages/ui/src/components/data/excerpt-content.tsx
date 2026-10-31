import { useMemo } from "react";

import { resolveContentLang } from "../../lib/content-lang";
import { cn } from "../../lib/utils";
import { parseExcerpt, type ExcerptBlock, type ExcerptTableCell } from "../../lib/excerpt-text";
import { DataTable, type DataTableColumn } from "./data-table";

export interface ExcerptContentProps {
  /** 抜粋・chunk の本文（HTML の表・`<br>` を含んでよい。HTML として注入しない）。 */
  text: string;
  /** 外枠の追加 class（文字の大きさ・色は呼び出し側）。 */
  className?: string;
  /** 表の読み上げの名前。 */
  tableLabel?: string;
  /** 本文の言語（#3335。`resolveContentLang`）。渡さなければ本文から判定し、`null` なら `lang` を付けない。 */
  lang?: string | null;
  testId?: string;
}

/**
 * 抜粋の全文の表示（#2387）。本文は改行を保って出し、文書の表（HTML の `<table>`）は共通の `DataTable` で表にする。
 * 3 行に畳む表示は {@link excerptPlainText}（表は「セル | セル」の行）を使う。本文の言語の `lang`（#3335）は本文の段落・
 * 表の題・セルにだけ付け、表の読み上げの名前（`tableLabel`。部品の文言）は画面の言語のまま（#3349）。
 */
export function ExcerptContent({ text, className, tableLabel = "抜粋の表", lang, testId }: ExcerptContentProps) {
  const blocks = useMemo(() => parseExcerpt(text), [text]);
  const contentLang = useMemo(() => resolveContentLang(lang, text), [lang, text]);
  return (
    <div className={cn("min-w-0 space-y-2", className)} data-testid={testId}>
      {blocks.map((block, index) =>
        block.kind === "text" ? (
          <p key={index} className="whitespace-pre-wrap break-words" lang={contentLang}>
            {block.text}
          </p>
        ) : (
          <ExcerptTable key={index} block={block} label={tableLabel} lang={contentLang} />
        )
      )}
    </div>
  );
}

type TableRow = { key: number; cells: ExcerptTableCell[] };

function ExcerptTable({
  block,
  label,
  lang,
}: {
  block: Extract<ExcerptBlock, { kind: "table" }>;
  label: string;
  lang: string | undefined;
}) {
  const [head, ...body] = block.rows;
  // 1 行だけの表は見出しと区別できないので、行の文字にする。
  if (!head || body.length === 0) {
    return (
      <p className="whitespace-pre-wrap break-words" lang={lang}>
        {[block.caption, head?.map((cell) => cell.text).join(" | ")].filter(Boolean).join("\n")}
      </p>
    );
  }
  // 見出しの行（`<th>`・`<thead>`）が無い表も、文書の表は先頭の行が項目名のことが多いので先頭の行を見出しにする。
  const columns: DataTableColumn<TableRow>[] = head.map((cell, column) => ({
    key: `c${column}`,
    header: <span lang={lang}>{cell.text}</span>,
    render: (row) => <span lang={lang}>{row.cells[column]?.text ?? ""}</span>,
    className: "min-w-24 whitespace-pre-wrap break-words align-top",
    headerClassName: "min-w-24 whitespace-pre-wrap break-words align-bottom",
  }));
  const rows: TableRow[] = body.map((cells, index) => ({ key: index, cells }));
  return (
    <figure className="min-w-0 space-y-1" data-testid="excerpt-table">
      {block.caption ? (
        <figcaption className="text-xs text-fg-muted" lang={lang}>
          {block.caption}
        </figcaption>
      ) : null}
      <DataTable columns={columns} rows={rows} getRowKey={(row) => row.key} dense ariaLabel={block.caption || label} />
    </figure>
  );
}
