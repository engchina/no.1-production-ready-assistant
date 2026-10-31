import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  DisclosureChevron,
  ObjectActionBar,
  RowActionMenu,
  splitObjectActions,
  visibleEntityActions,
  type EntityAction,
} from "../src";

const noop = () => {};
const actions: EntityAction[] = [
  { id: "edit", label: "編集", onSelect: noop },
  { id: "reset", label: "リセット", onSelect: noop },
  { id: "unlock", label: "ロック解除", onSelect: noop },
  { id: "delete", label: "削除", tone: "danger", onSelect: noop },
  { id: "hidden", label: "非表示", visible: false, onSelect: noop },
];

describe("EntityAction の振り分け", () => {
  it("visible=false を外し、inline は非危険操作の最大 2 件、残りは overflow", () => {
    expect(visibleEntityActions(actions).map((a) => a.id)).toEqual(["edit", "reset", "unlock", "delete"]);
    const { inline, overflow } = splitObjectActions(actions);
    expect(inline.map((a) => a.id)).toEqual(["edit", "reset"]);
    expect(overflow.map((a) => a.id)).toEqual(["unlock", "delete"]);
  });
});

describe("ObjectActionBar", () => {
  it("inline の操作と、既定の日本語の「その他の操作」メニューボタンを描く", () => {
    const html = renderToStaticMarkup(<ObjectActionBar actions={actions} ariaLabel="対象の操作" testId="obj" />);
    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="対象の操作"');
    expect(html).toContain(">編集<");
    expect(html).toContain(">リセット<");
    expect(html).not.toContain(">削除<");
    expect(html).toContain('aria-haspopup="menu"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('data-testid="obj-more"');
    expect(html).toContain("その他の操作");
    // 読み上げ名に対象を足し、ページの「その他の操作」と区別する（#582）。
    expect(html).toContain('aria-label="その他の操作（対象の操作）"');
  });

  it("moreLabel で文言を差し替え、操作が無ければ何も描かない", () => {
    expect(renderToStaticMarkup(<ObjectActionBar actions={actions} ariaLabel="x" moreLabel="More" />)).toContain("More");
    expect(renderToStaticMarkup(<ObjectActionBar actions={[]} ariaLabel="x" />)).toBe("");
  });
});

describe("RowActionMenu", () => {
  it("閉じた状態では trigger だけを描き、aria-label と menu の関係を持つ", () => {
    const html = renderToStaticMarkup(<RowActionMenu actions={actions} ariaLabel="orders の操作" />);
    expect(html).toContain('aria-label="orders の操作"');
    expect(html).toContain('aria-haspopup="menu"');
    expect(html).not.toContain('role="menu"');
  });
});

describe("DisclosureChevron", () => {
  it("展開状態を data-state で示し、読み上げない", () => {
    const expanded = renderToStaticMarkup(<DisclosureChevron expanded />);
    expect(expanded).toContain('data-state="expanded"');
    expect(expanded).toContain('aria-hidden="true"');
    expect(renderToStaticMarkup(<DisclosureChevron expanded={false} />)).toContain("rotate-90");
  });
});
