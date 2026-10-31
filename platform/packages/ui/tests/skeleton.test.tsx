import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DataTable } from "../src/components/data/data-table";
import { LoadingState } from "../src/components/feedback/state-views";
import { ChatSkeleton, FormSkeleton, ListSkeleton, SKELETON_CLASS, Skeleton, TableSkeleton } from "../src/components/ui/skeleton";
import * as ui from "../src/index";
import {
  INFORMATION_COMPACT_LIST_FIVE_ROW_SCROLL_CLASS,
  INFORMATION_LIST_ROW_CLASS,
  INFORMATION_LIST_SCROLL_CLASS,
  INFORMATION_LIST_VISIBLE_ROWS,
  INFORMATION_TABLE_FIXED_VISIBLE_ROWS,
  INFORMATION_TABLE_FOCUS_CLASS,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
} from "../src/lib/list-density";

function count(html: string, pattern: RegExp) {
  return html.match(pattern)?.length ?? 0;
}

describe("一覧の表示密度の定数（#265、NL2SQL の基準と同じ値）", () => {
  it("md 未満 5 行・md 以上 8 行、行 3.5rem・表頭 2.5rem", () => {
    expect(INFORMATION_LIST_VISIBLE_ROWS).toEqual({ mobile: 5, desktop: 8, rowHeightRem: 3.5, headerHeightRem: 2.5 });
    expect(INFORMATION_TABLE_VISIBLE_ROWS).toEqual({ base: 5, md: 8 });
    expect(INFORMATION_TABLE_FIXED_VISIBLE_ROWS).toBe(5);
    expect(INFORMATION_TABLE_ROW_CLASS).toBe("h-[3.5rem]");
    expect(INFORMATION_LIST_ROW_CLASS).toBe("min-h-[3.5rem]");
  });

  it("行リストのスクロールの高さは 5 行 / 8 行（3.5rem × 行数）に一致する", () => {
    const rem = (rows: number) => `${rows * INFORMATION_LIST_VISIBLE_ROWS.rowHeightRem}rem`;
    expect(INFORMATION_LIST_SCROLL_CLASS).toBe(`max-h-[${rem(5)}] overflow-auto [scrollbar-gutter:stable] md:max-h-[${rem(8)}]`);
    // h-56 = 14rem = 3.5rem × 4 行 + 余白。5 行目の一部が見えてスクロールできると分かる。
    expect(INFORMATION_COMPACT_LIST_FIVE_ROW_SCROLL_CLASS).toBe("h-56 max-h-56 overflow-auto [scrollbar-gutter:stable]");
    expect(INFORMATION_TABLE_FOCUS_CLASS).toContain("focus-visible:outline-focus-ring");
    expect(INFORMATION_TABLE_FOCUS_CLASS).not.toMatch(/ring-/);
  });

  it("パッケージのルートから export される", () => {
    expect(ui.INFORMATION_TABLE_VISIBLE_ROWS).toBe(INFORMATION_TABLE_VISIBLE_ROWS);
    expect(ui.INFORMATION_TABLE_ROW_CLASS).toBe(INFORMATION_TABLE_ROW_CLASS);
    expect(ui.TableSkeleton).toBe(TableSkeleton);
    expect(ui.ListSkeleton).toBe(ListSkeleton);
  });
});

describe("Skeleton の見た目の統一（#265）", () => {
  it("Skeleton は surface-hover の地で、reduced-motion では点滅しない", () => {
    const html = renderToStaticMarkup(<Skeleton className="h-6 w-44" testId="s" />);
    expect(html).toBe(`<div class="${SKELETON_CLASS} h-6 w-44" aria-hidden="true" data-testid="s"></div>`);
    expect(SKELETON_CLASS).toContain("bg-surface-hover");
    expect(SKELETON_CLASS).toContain("motion-reduce:animate-none");
    expect(html).not.toContain("bg-border");
  });

  it("LoadingState と DataTable の loading 行も同じ見た目を使う", () => {
    const loading = renderToStaticMarkup(<LoadingState label="読み込んでいます" rows={2} />);
    expect(count(loading, /bg-surface-hover motion-reduce:animate-none/g)).toBe(2);
    // 文言と経過時間の行を領域の先頭に出す（Skeleton だけにしない。#2510）。
    expect(loading).toContain("読み込んでいます");
    expect(loading.indexOf("読み込んでいます")).toBeLessThan(loading.indexOf("bg-surface-hover"));
    const table = renderToStaticMarkup(
      <DataTable columns={[{ key: "a", header: "A" }]} rows={[]} getRowKey={(_, index) => index} loading loadingRows={2} />
    );
    expect(count(table, /data-row-kind="skeleton"/g)).toBe(2);
    expect(table).toContain("bg-surface-hover motion-reduce:animate-none");
  });

  it("DataTable の loading は visibleRows があれば、その行数のスケルトンで高さを予約する", () => {
    const table = renderToStaticMarkup(
      <DataTable columns={[{ key: "a", header: "A" }]} rows={[]} getRowKey={(_, index) => index} loading visibleRows={6} />
    );
    expect(count(table, /data-row-kind="skeleton"/g)).toBe(6);
    const fallback = renderToStaticMarkup(
      <DataTable columns={[{ key: "a", header: "A" }]} rows={[]} getRowKey={(_, index) => index} loading />
    );
    expect(count(fallback, /data-row-kind="skeleton"/g)).toBe(3);
  });
});

describe("TableSkeleton", () => {
  it("既定は表頭 + 8 行（6〜8 行目は md 以上だけ）× 4 列で、読み上げない", () => {
    const html = renderToStaticMarkup(<TableSkeleton testId="t" />);
    expect(html).toMatch(/^<div class="overflow-hidden rounded-md border border-border bg-surface" aria-hidden="true" data-testid="t" data-skeleton="table">/);
    expect(html).toContain('class="flex h-[2.5rem] items-center gap-4 bg-surface-sunken px-3"');
    expect(count(html, /data-skeleton-row="base"/g)).toBe(5);
    expect(count(html, /data-skeleton-row="md"/g)).toBe(3);
    expect(count(html, /data-skeleton-row="md" class="h-\[3\.5rem\] items-center gap-4 border-t border-border\/70 px-3 hidden md:flex"/g)).toBe(3);
    // 表頭 4 本 + 8 行 × 4 列
    expect(count(html, /bg-surface-hover/g)).toBe(4 + 8 * 4);
    expect(html).not.toContain('role="status"');
  });

  it("rows と columns で読み込み後の表の形に合わせる", () => {
    const html = renderToStaticMarkup(<TableSkeleton rows={3} columns={2} />);
    expect(count(html, /data-skeleton-row="base"/g)).toBe(3);
    expect(html).not.toContain('data-skeleton-row="md"');
    expect(count(html, /bg-surface-hover/g)).toBe(2 + 3 * 2);
  });

  it("md の行数が base より少なくても base の行は隠さない", () => {
    const html = renderToStaticMarkup(<TableSkeleton rows={{ base: 4, md: 2 }} columns={1} />);
    expect(count(html, /data-skeleton-row="base"/g)).toBe(4);
    expect(html).not.toContain('data-skeleton-row="md"');
  });
});

describe("ListSkeleton", () => {
  it("既定は 3.5rem の塊を 8 個（6〜8 個目は md 以上だけ）", () => {
    const html = renderToStaticMarkup(<ListSkeleton testId="l" />);
    expect(html).toMatch(/^<div class="grid gap-2" aria-hidden="true" data-testid="l" data-skeleton="list">/);
    expect(count(html, /h-\[3\.5rem\] block/g)).toBe(5);
    expect(count(html, /h-\[3\.5rem\] hidden md:block/g)).toBe(3);
  });

  it("rowClassName で行の高さを変えられる", () => {
    const html = renderToStaticMarkup(<ListSkeleton rows={2} rowClassName="h-20" />);
    expect(count(html, /h-20 block/g)).toBe(2);
    expect(html).not.toContain("h-[3.5rem]");
  });
});

describe("FormSkeleton", () => {
  it("見出し + ラベルと入力欄 × 4 + 操作行。入力欄はコントロールの高さ", () => {
    const html = renderToStaticMarkup(<FormSkeleton testId="f" />);
    expect(html).toContain('data-testid="f" data-skeleton="form"');
    expect(count(html, /data-skeleton-part="title"/g)).toBe(1);
    expect(count(html, /data-skeleton-part="field"/g)).toBe(4);
    expect(count(html, /h-\[var\(--button-height-md\)\] w-full/g)).toBe(4);
    expect(count(html, /data-skeleton-part="actions"/g)).toBe(1);
    expect(html).toContain('aria-hidden="true"');
  });

  it("fields・title・actions で形を合わせる", () => {
    const html = renderToStaticMarkup(<FormSkeleton fields={2} title={false} actions={false} />);
    expect(html).not.toContain('data-skeleton-part="title"');
    expect(html).not.toContain('data-skeleton-part="actions"');
    expect(count(html, /data-skeleton-part="field"/g)).toBe(2);
  });
});

describe("ChatSkeleton（#1153）", () => {
  it("既定は質問（右寄せ）と回答の組を 2 つ。読み上げない", () => {
    const html = renderToStaticMarkup(<ChatSkeleton testId="c" />);
    expect(html).toMatch(/^<div class="grid gap-4" aria-hidden="true" data-testid="c" data-skeleton="chat">/);
    expect(count(html, /data-skeleton-part="turn"/g)).toBe(2);
    expect(count(html, /ml-auto h-12 max-w-\[85%\] w-2\/3" data-skeleton-part="question"/g)).toBe(1);
    expect(count(html, /ml-auto h-12 max-w-\[85%\] w-1\/2" data-skeleton-part="question"/g)).toBe(1);
    expect(count(html, /data-skeleton-part="answer"/g)).toBe(2);
    expect(count(html, /bg-surface-hover motion-reduce:animate-none/g)).toBe(4);
    expect(html).not.toContain('role="status"');
  });

  it("turns で組の数を変えられる（1 未満でも 1 組は出す）", () => {
    expect(count(renderToStaticMarkup(<ChatSkeleton turns={3} />), /data-skeleton-part="turn"/g)).toBe(3);
    expect(count(renderToStaticMarkup(<ChatSkeleton turns={0} />), /data-skeleton-part="turn"/g)).toBe(1);
  });

  it("パッケージのルートから export される", () => {
    expect(ui.ChatSkeleton).toBe(ChatSkeleton);
  });
});

describe("TableSkeleton の狭い幅", () => {
  it("4 列目以降は md 以上だけに出す（375px で棒が点にならない）", () => {
    const html = renderToStaticMarkup(<TableSkeleton rows={1} columns={5} />);
    // 表頭 + 1 行 × 2 列（4・5 列目）
    expect(count(html, /class="min-w-0 flex-1 hidden md:block"/g)).toBe(4);
    expect(count(html, /class="min-w-0 flex-1"/g)).toBe(6);
  });
});
