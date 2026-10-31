import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DataTable } from "../src/components/data/data-table";
import { publishedVersionColumns } from "../src/components/data/published-version-columns";

type Row = { version: number; at: string; by: string; note: string; extra: string };

const rows: Row[] = [
  { version: 7, at: "2026-10-18", by: "利用者 A", note: "", extra: "列 A" },
  { version: 3, at: "2026-10-01", by: "利用者 B", note: "メモ B", extra: "列 B" },
];

function render(currentVersion: number | null) {
  const columns = publishedVersionColumns<Row>({
    getVersion: (row) => row.version,
    currentVersion,
    formatVersion: (version) => `v${version}`,
    labels: {
      version: "版",
      current: "公開中",
      publishedAt: "公開日時",
      publishedBy: "公開者",
      note: "メモ",
      actions: "操作",
    },
    renderPublishedAt: (row) => row.at,
    renderPublishedBy: (row) => row.by,
    renderNote: (row) => row.note || "-",
    extraColumns: [{ key: "extra", header: "製品の列", render: (row) => row.extra }],
    renderActions: (row) => <button type="button">{`操作 v${row.version}`}</button>,
  });
  return {
    keys: columns.map((column) => column.key),
    html: renderToStaticMarkup(<DataTable<Row> rows={rows} columns={columns} getRowKey={(row) => String(row.version)} />),
  };
}

describe("publishedVersionColumns", () => {
  it("版 → 公開日時 → 公開者 → 製品の列 → メモ → 操作 の順にそろえる", () => {
    expect(render(7).keys).toEqual(["version", "published_at", "published_by", "extra", "note", "actions"]);
  });

  it("公開中の版の行だけに文言のバッジを出す（古い版を公開中にしても同じ）", () => {
    const latest = render(7).html;
    expect(latest.match(/公開中/g)).toHaveLength(1);
    expect(latest.indexOf("公開中")).toBeLessThan(latest.indexOf("v3"));

    const old = render(3).html;
    expect(old.match(/公開中/g)).toHaveLength(1);
    expect(old.indexOf("公開中")).toBeGreaterThan(old.indexOf("v3"));
    expect(old).toContain("操作 v3");
    expect(old).toContain("メモ B");
  });

  it("公開中の版が無ければバッジを出さない", () => {
    expect(render(null).html).not.toContain("公開中");
  });
});
