import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  DataTable,
  measureVisibleRowsHeight,
  resolveVisibleRows,
  shouldRemeasureOnResize,
  stabilizeMeasuredHeight,
  type DataTableColumn,
} from "../src/components/data/data-table";

type Row = { id: string; name: string; count: number };

const rows: Row[] = [
  { id: "a", name: "ALPHA", count: 1 },
  { id: "b", name: "BRAVO", count: 2 },
];

const columns: DataTableColumn<Row>[] = [
  { key: "name", header: "名前", sortable: true },
  { key: "count", header: "件数", align: "right" },
];

describe("DataTable の既定出力", () => {
  it("新しい props を渡さなければ、スクロール領域・選択・sticky の属性を出さない", () => {
    const html = renderToStaticMarkup(<DataTable columns={columns} rows={rows} getRowKey={(row) => row.id} />);
    // relative: 行の中の sr-only（absolute）のラベルを表のスクロール領域の中に閉じ込める（#1116）。
    expect(html).toMatch(/^<div class="relative rounded-md border border-border bg-surface overflow-x-auto">/);
    expect(html).not.toContain('role="region"');
    expect(html).not.toContain("data-selected");
    expect(html).not.toContain("aria-current");
    expect(html).not.toContain("sticky");
    expect(html).not.toContain("style=");
    expect(html).toContain('class="min-w-full text-left text-xs divide-y divide-border"');
  });

  it("並べ替え列は th に aria-sort を持ち、ボタンは折り返さず sm ボタン高（タッチ 44px）を当たり判定にする", () => {
    const html = renderToStaticMarkup(
      <DataTable columns={columns} rows={rows} getRowKey={(row) => row.id} sort={{ key: "name", direction: "desc" }} onSortChange={() => undefined} />
    );
    expect(html).toMatch(/<th scope="col" aria-sort="descending"[^>]*><button type="button" class="[^"]*min-h-\[var\(--button-height-sm\)\][^"]*whitespace-nowrap/);
    expect(html).toMatch(/<th scope="col" class="[^"]*whitespace-nowrap[^"]*">件数<\/th>/);
  });
});

describe("DataTable の一覧向け機能", () => {
  it("stickyHeader は thead を固定し、罫線を th の内側に持たせる", () => {
    const html = renderToStaticMarkup(<DataTable columns={columns} rows={rows} getRowKey={(row) => row.id} stickyHeader />);
    expect(html).toMatch(/<thead class="bg-surface-sunken text-fg-muted sticky top-0 z-10">/);
    expect(html).toContain("shadow-[inset_0_-1px_0_var(--color-border)]");
    expect(html).toContain('class="min-w-full text-left text-xs"');
  });

  it("scrollAriaLabel はスクロール領域を Tab で到達できる region にする", () => {
    const html = renderToStaticMarkup(
      <DataTable columns={columns} rows={rows} getRowKey={(row) => row.id} scrollAriaLabel="一覧。スクロールできます。" scrollTestId="list-scroll" />
    );
    expect(html).toMatch(/^<div role="region" aria-label="一覧。スクロールできます。" tabindex="0" data-testid="list-scroll" class="[^"]*overflow-auto[^"]*focus-visible:outline-focus-ring/);
  });

  it("selectedRowKey は該当行だけに aria-current・選択背景・左バー・淡アクセント面の文字スコープを付け、全行に data-selected を出す", () => {
    const html = renderToStaticMarkup(
      <DataTable columns={columns} rows={rows} getRowKey={(row) => row.id} selectedRowKey="b" onRowClick={() => undefined} />
    );
    expect(html).toMatch(/<tr data-row-kind="data" data-selected="false" class="transition-colors cursor-pointer hover:bg-surface-hover">/);
    expect(html).toMatch(
      /<tr data-row-kind="data" data-selected="true" data-surface-tint="accent" aria-current="true" class="transition-colors cursor-pointer bg-accent-subtle \[&amp;&gt;:first-child\]:shadow-\[inset_0\.25rem_0_0_var\(--color-accent-fg\)\]">/
    );
    expect(html.match(/data-surface-tint/g)).toHaveLength(1);
  });

  it("isRowSelected は背景と左バーだけを付け、aria-current は付けない（状態はチェックボックスが伝える）", () => {
    const html = renderToStaticMarkup(
      <DataTable
        columns={columns}
        rows={rows}
        getRowKey={(row) => row.id}
        isRowSelected={(row) => row.id === "a"}
        renderRowDetail={() => <p>補足</p>}
      />
    );
    expect(html).toMatch(/<tr data-row-kind="data" data-selected="true" data-surface-tint="accent" class="transition-colors bg-accent-subtle \[&amp;&gt;:first-child\]:shadow-/);
    // 詳細行は選択行の続きとして同じ面・左バー・文字スコープを持つ。非選択行の詳細行は持たない。
    expect(html).toMatch(/<tr data-row-kind="detail" data-surface-tint="accent" class="bg-accent-subtle \[&amp;&gt;:first-child\]:shadow-/);
    expect(html).toMatch(/<tr data-row-kind="detail"><td/);
    expect(html).not.toContain("aria-current");
  });

  it("rowProps・rowHeader・renderRowDetail・tableClassName を反映する", () => {
    const html = renderToStaticMarkup(
      <DataTable
        columns={[{ ...columns[0], sortable: false, rowHeader: true }, columns[1]]}
        rows={rows}
        getRowKey={(row) => row.id}
        tableClassName="table-fixed min-w-[40rem]"
        rowProps={(row) => ({ className: "h-[3.5rem]", "aria-label": `${row.name} を表示`, "data-testid": `row-${row.id}` })}
        renderRowDetail={(row) => (row.id === "a" ? <p>補足</p> : null)}
      />
    );
    expect(html).toContain('class="text-left text-xs divide-y divide-border table-fixed min-w-[40rem]"');
    expect(html).toMatch(/<tr data-row-kind="data" aria-label="ALPHA を表示" data-testid="row-a" class="h-\[3.5rem\]"><th scope="row" class="px-3 py-2 text-left font-normal">ALPHA<\/th>/);
    expect(html).toMatch(/<tr data-row-kind="detail"><td colSpan="2" class="px-3 py-2"><p>補足<\/p><\/td><\/tr>/i);
    expect(html.match(/data-row-kind="detail"/g)).toHaveLength(1);
  });

  it("読込中は loadingRows 行のスケルトンを出す", () => {
    const html = renderToStaticMarkup(<DataTable columns={columns} rows={[]} getRowKey={(row) => row.id} loading loadingRows={5} />);
    expect(html.match(/data-row-kind="skeleton"/g)).toHaveLength(5);
  });
});

describe("表示行数の計算", () => {
  it("{ base, md } は md 以上で md を使う", () => {
    expect(resolveVisibleRows(undefined, true)).toBeUndefined();
    expect(resolveVisibleRows(8, false)).toBe(8);
    expect(resolveVisibleRows({ base: 5, md: 8 }, false)).toBe(5);
    expect(resolveVisibleRows({ base: 5, md: 8 }, true)).toBe(8);
    expect(resolveVisibleRows({ base: 5 }, true)).toBe(5);
  });

  it("表頭 + 先頭 N 行の実測下端 + 枠で高さを決める（2 行セルで行高が変わっても N 行ちょうど）", () => {
    // 表頭 33px、1 行目 49px、2 行目以降 51px（2 行セル）
    const bottoms = [82, 133, 184, 235, 286, 337, 388, 439, 490];
    expect(measureVisibleRowsHeight({ tableTop: 0, headerBottom: 33, rowBottoms: bottoms, rows: 8, chrome: 2, fill: false })).toBe(441);
    expect(measureVisibleRowsHeight({ tableTop: 100, headerBottom: 133, rowBottoms: bottoms.map((b) => b + 100), rows: 5, chrome: 2, fill: false })).toBe(288);
  });

  it("行が N 未満なら max-height は全行分、fill では最後の行高で N 行分を確保する", () => {
    const bottoms = [82, 131];
    expect(measureVisibleRowsHeight({ tableTop: 0, headerBottom: 33, rowBottoms: bottoms, rows: 5, chrome: 2, fill: false })).toBe(133);
    expect(measureVisibleRowsHeight({ tableTop: 0, headerBottom: 33, rowBottoms: bottoms, rows: 5, chrome: 2, fill: true })).toBe(133 + 49 * 3);
    expect(measureVisibleRowsHeight({ tableTop: 0, headerBottom: 33, rowBottoms: [82], rows: 5, chrome: 0, fill: true })).toBe(82 + 49 * 4);
    expect(measureVisibleRowsHeight({ tableTop: 0, headerBottom: 33, rowBottoms: [], rows: 5, chrome: 0, fill: true })).toBeUndefined();
  });

  it("測り直しが 1px を往復しても高さを変えない（拡大 125% の丸めで揺れない。#1222）", () => {
    // 159 → 158 → 159 → 158 … と測れても、最初に大きい方へ寄せた後は動かない。
    let height = stabilizeMeasuredHeight(undefined, 158);
    for (const measured of [159, 158, 159, 158]) height = stabilizeMeasuredHeight(height, measured);
    expect(height).toBe(159);
    // 2px 以上の変化（行の追加・幅の変化による折り返し）はそのまま反映する。
    expect(stabilizeMeasuredHeight(159, 210)).toBe(210);
    expect(stabilizeMeasuredHeight(210, 159)).toBe(159);
    // 未測定・測れないときは測定値に従う。
    expect(stabilizeMeasuredHeight(undefined, 133)).toBe(133);
    expect(stabilizeMeasuredHeight(133, undefined)).toBeUndefined();
  });

  it("スクロール領域の高さだけの変化（自分で書いた max-height）では測り直さない（#1232）", () => {
    const scroller = { id: "scroller" };
    const table = { id: "table" };
    const widths: { scroller?: number } = {};
    const entry = (target: unknown, width: number) => ({ target, contentRect: { width } });
    // observe の最初の通知（幅が未知）は測り直す。
    expect(shouldRemeasureOnResize([entry(table, 640), entry(scroller, 640)], scroller, widths)).toBe(true);
    expect(widths.scroller).toBe(640);
    // 測った高さを max-height に書いた後の通知は、スクロール領域の幅が同じなので測り直さない（循環を断つ）。
    expect(shouldRemeasureOnResize([entry(scroller, 640)], scroller, widths)).toBe(false);
    expect(shouldRemeasureOnResize([entry(scroller, 640)], scroller, widths)).toBe(false);
    // 幅の変化（横スクロールバーの出入り・分割ペインのドラッグ）は測り直し、次の比較の基準も更新する。
    expect(shouldRemeasureOnResize([entry(scroller, 626.4)], scroller, widths)).toBe(true);
    expect(widths.scroller).toBe(626.4);
    expect(shouldRemeasureOnResize([entry(scroller, 626.4)], scroller, widths)).toBe(false);
    // 表（内容）の変化は幅が同じでも測り直す（行の追加・折り返し）。
    expect(shouldRemeasureOnResize([entry(table, 626.4)], scroller, widths)).toBe(true);
    expect(shouldRemeasureOnResize([entry(scroller, 626.4), entry(table, 626.4)], scroller, widths)).toBe(true);
    expect(shouldRemeasureOnResize([], scroller, widths)).toBe(false);
  });
});

describe("行クリックの対象判定", () => {
  // vitest は DOM を持たないため、closest / contains だけを持つ最小の Element で判定ロジックを確かめる。
  class FakeElement {
    constructor(
      readonly tag: string,
      readonly parent: FakeElement | null = null
    ) {}
    closest(selector: string): FakeElement | null {
      const tags = selector.split(",").filter((part) => /^[a-z]+$/.test(part));
      for (let node: FakeElement | null = this; node; node = node.parent) if (tags.includes(node.tag)) return node;
      return null;
    }
    contains(other: FakeElement) {
      for (let node: FakeElement | null = other; node; node = node.parent) if (node === this) return true;
      return false;
    }
  }

  it("行の地は行クリック、行内の button と portal（行の DOM の外）は行クリックにしない", async () => {
    const original = (globalThis as { Element?: unknown }).Element;
    (globalThis as { Element?: unknown }).Element = FakeElement;
    try {
      const { isInteractiveRowTarget } = await import("../src/components/data/data-table");
      const row = new FakeElement("tr");
      const cell = new FakeElement("td", row);
      const button = new FakeElement("button", cell);
      const label = new FakeElement("span", button);
      const portalItem = new FakeElement("div", new FakeElement("body"));
      const asRow = row as unknown as Element;
      expect(isInteractiveRowTarget(cell as unknown as EventTarget, asRow)).toBe(false);
      expect(isInteractiveRowTarget(label as unknown as EventTarget, asRow)).toBe(true);
      expect(isInteractiveRowTarget(portalItem as unknown as EventTarget, asRow)).toBe(true);
      expect(isInteractiveRowTarget(null, asRow)).toBe(false);
    } finally {
      (globalThis as { Element?: unknown }).Element = original;
    }
  });
});
