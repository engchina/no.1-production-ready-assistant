import { readFileSync } from "node:fs";

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  FIXED_SPLIT_LAYOUT_HYSTERESIS_PX,
  FIXED_SPLIT_STORAGE_PREFIX,
  FixedSplitPane,
  fixedSplitStateForRatio,
  fixedSplitStorageKey,
  nextFixedSplitWideEnough,
  parseFixedSplitStorageValue,
} from "../src";

const tokensCss = readFileSync(new URL("../src/styles/tokens.css", import.meta.url), "utf8");

describe("FixedSplitPane", () => {
  it("横並びと縦積みは境目の近くの幅の揺れで往復しない（#1222）", () => {
    const threshold = 654;
    expect(nextFixedSplitWideEnough(false, threshold - 1, threshold)).toBe(false);
    expect(nextFixedSplitWideEnough(false, threshold, threshold)).toBe(true);
    // 横並びになった後は、境目より少し狭くなっても横並びのまま（スクロールバー 1 本分の揺れなど）。
    expect(nextFixedSplitWideEnough(true, threshold - 15, threshold)).toBe(true);
    expect(nextFixedSplitWideEnough(true, threshold - FIXED_SPLIT_LAYOUT_HYSTERESIS_PX - 1, threshold)).toBe(false);
  });

  it("保存 key の前置きは既定が共通で、製品が上書きできる", () => {
    expect(fixedSplitStorageKey("list")).toBe(`${FIXED_SPLIT_STORAGE_PREFIX}.list`);
    expect(fixedSplitStorageKey("list", "production-ready-nl2sql.fixedSplitPane")).toBe(
      "production-ready-nl2sql.fixedSplitPane.list"
    );
  });

  it("左右のパネルと、既定の日本語で読み上げる divider を描く", () => {
    const html = renderToStaticMarkup(<FixedSplitPane splitId="list" left={<p>一覧</p>} right={<p>詳細</p>} />);
    expect(html).toContain("fixed-split-pane__panel--left");
    expect(html).toContain("fixed-split-pane__panel--right");
    expect(html).toContain('role="separator"');
    expect(html).toContain('aria-label="左右ペインの表示比率"');
  });

  it("保存が無いときは、どの画面も分割線を 50% に置く（#1572）", () => {
    const html = renderToStaticMarkup(<FixedSplitPane splitId="list" left={<p>一覧</p>} right={<p>詳細</p>} />);
    expect(html).toContain('data-split-ratio="equal"');
    expect(html).toContain('data-split-left-fraction="0.5000"');
    expect(html).toContain('aria-valuenow="50"');
    expect(html).toContain('aria-valuetext="左右 1:1 50%"');
  });

  it("保存が無い・壊れているときの状態は 50%、保存した位置はそれを使う（#1572）", () => {
    expect(parseFixedSplitStorageValue(null)).toEqual(fixedSplitStateForRatio("equal"));
    expect(parseFixedSplitStorageValue("not-json")).toEqual(fixedSplitStateForRatio("equal"));
    expect(parseFixedSplitStorageValue(JSON.stringify({ ratio: "rightWide", leftFraction: 0.3 }))).toEqual({
      ratio: "rightWide",
      leftFraction: 0.3,
    });
  });

  it("labels で文言を差し替えられる", () => {
    const html = renderToStaticMarkup(
      <FixedSplitPane
        splitId="list"
        left={null}
        right={null}
        labels={{ separator: "Split ratio", equal: "Equal" }}
      />
    );
    expect(html).toContain('aria-label="Split ratio"');
    expect(html).toContain("Equal 50%");
  });

  it("構造 CSS を共有 tokens.css から配布する", () => {
    expect(tokensCss).toContain('@import "./structure/fixed-split-pane.css";');
  });
});
