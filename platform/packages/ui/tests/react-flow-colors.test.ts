import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(new URL(`../src/styles/${path}`, import.meta.url), "utf8");
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

describe("React Flow の配色をトークンに結び付ける（#1137）", () => {
  const css = stripComments(read("integrations/react-flow.css"));
  const declarations = Object.fromEntries(
    [...css.matchAll(/(--xy-[a-z-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2].trim()])
  );

  it("エントリ（styles.css）から読み込む", () => {
    expect(read("tokens.css")).toContain('@import "./integrations/react-flow.css";');
  });

  it("Controls・帰属表示・辺のラベルの地はテーマの面のトークン", () => {
    expect(declarations["--xy-controls-button-background-color"]).toBe("var(--color-surface-raised)");
    expect(declarations["--xy-controls-button-background-color-hover"]).toBe("var(--color-surface-hover)");
    expect(declarations["--xy-controls-button-color"]).toBe("var(--color-fg)");
    expect(declarations["--xy-controls-button-border-color"]).toBe("var(--color-border)");
    expect(declarations["--xy-attribution-background-color"]).toBe("var(--color-surface-raised)");
    expect(declarations["--xy-edge-label-background-color"]).toBe("var(--color-surface)");
    // 帰属表示のリンクは変数が無いので、React Flow の CSS（後から読まれる）より詳細度を上げて上書きする。
    expect(css).toMatch(/\.react-flow \.react-flow__attribution a \{\s*color: var\(--color-fg-muted\);\s*\}/);
  });

  it("React Flow の既定（-default）は上書きせず、生の色を書かない", () => {
    expect(Object.keys(declarations).length).toBeGreaterThan(0);
    for (const [name, value] of Object.entries(declarations)) {
      expect(name).not.toMatch(/-default$/);
      expect(value, name).not.toMatch(/#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(/i);
    }
  });
});
