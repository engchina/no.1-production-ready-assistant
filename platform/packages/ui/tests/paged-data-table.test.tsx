import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PagedDataTable, type PaginationLabels } from "../src/components/data/paged-data-table";
import { INFORMATION_LIST_SCROLL_CLASS, INFORMATION_TABLE_ROW_CLASS } from "../src/lib/list-density";

type Row = { id: string; name: string };

const rows: Row[] = Array.from({ length: 53 }, (_, index) => ({ id: `r${index + 1}`, name: `ROW-${index + 1}` }));

const labels: Partial<PaginationLabels> = {
  summary: ({ start, end, total }) => `${start}–${end} / ${total} 件`,
  pageIndicator: (page, totalPages) => `${page} / ${totalPages} ページ`,
  prev: "前へ",
  next: "次へ",
  ariaLabel: "ページ送り",
};

function render(props: Partial<Parameters<typeof PagedDataTable<Row>>[0]> = {}) {
  return renderToStaticMarkup(
    <PagedDataTable<Row>
      rows={rows}
      columns={[{ key: "name", header: "名前" }]}
      getRowKey={(row) => row.id}
      paginationLabels={labels}
      {...props}
    />
  );
}

describe("PagedDataTable", () => {
  it("既定は 25 件/ページで、表頭の固定・行の最小高さと、直下の Pagination を出す", () => {
    const html = render({ paginationTestId: "pager" });
    expect(html).toContain("ROW-1<");
    expect(html).toContain("ROW-25<");
    expect(html).not.toContain("ROW-26<");
    expect(html).toContain("sticky");
    expect(html).toContain(INFORMATION_TABLE_ROW_CLASS);
    expect(html).toContain('data-testid="pager"');
    expect(html).toContain("1–25 / 53 件");
    expect(html).toContain("1 / 3 ページ");
    expect(html).toContain('aria-label="ページ送り"');
  });

  it("制御式の page で保持したページを出し、一覧ごとの aria-label で上書きできる", () => {
    const html = render({ page: 3, onPageChange: () => undefined, paginationAriaLabel: "Run のページ" });
    expect(html).toContain("ROW-51<");
    expect(html).toContain("ROW-53<");
    expect(html).not.toContain("ROW-50<");
    expect(html).toContain("51–53 / 53 件");
    expect(html).toContain('aria-label="Run のページ"');
  });

  it("1 ページしかないときは Pagination を出さない。行の className は最小高さと合わせる", () => {
    const html = render({
      rows: rows.slice(0, 4),
      paginationTestId: "pager",
      rowProps: () => ({ className: "align-top" }),
    });
    expect(html).not.toContain('data-testid="pager"');
    expect(html).toContain(`${INFORMATION_TABLE_ROW_CLASS} align-top`);
  });
  it("長いセルの表は共通の高さ上限でも表頭とページ送りを保つ", () => {
    const html = render({ viewport: "bounded", scrollAriaLabel: "内容の一覧" });
    for (const cls of INFORMATION_LIST_SCROLL_CLASS.split(" ")) expect(html).toContain(cls);
    expect(html).toContain("sticky");
    expect(html).toContain('role="region"');
    expect(html).toContain('tabindex="0"');
    expect(html).toContain("1–25 / 53 件");
    expect(html).not.toContain("overflow-visible");
  });

});
