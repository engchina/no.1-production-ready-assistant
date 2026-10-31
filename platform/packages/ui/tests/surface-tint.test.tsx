import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { describe, expect, it } from "vitest";

import { Banner } from "../src/components/ui/banner";
import { StatusBadge } from "../src/components/data/status-badge";
import { surfaceTintFor } from "../src/lib/surface-tint";

/**
 * 淡い色の面（bg-accent-subtle / -muted・bg-<状態色>-subtle）には data-surface-tint を付ける（#1671）。
 * 製品は adherence の lint が検出する。platform の 2 パッケージは lint を掛けていないため、同じ判定をここで掛ける。
 */
const packagesDir = fileURLToPath(new URL("../../", import.meta.url));
const SOURCE_DIRS = ["ui/src", "system-settings/src"].map((dir) => join(packagesDir, dir));

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) sourceFiles(path, out);
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(path);
  }
  return out;
}

type Finding = { file: string; line: number; text: string };

/** className の中で淡い面を塗るのに data-surface-tint の無い要素と、className の外（対応表・定数）の淡い面の class */
function scan(file: string) {
  const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const missing: Finding[] = [];
  const indirect: Finding[] = [];
  const visit = (node: ts.Node) => {
    const text =
      ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateLiteralToken(node)
        ? node.text
        : null;
    if (text !== null && surfaceTintFor(text)) {
      const finding = { file: file.slice(packagesDir.length), line: source.getLineAndCharacterOfPosition(node.getStart()).line + 1, text };
      let parent: ts.Node | undefined = node.parent;
      while (parent && !(ts.isJsxAttribute(parent) && parent.name.getText() === "className")) {
        if (ts.isSourceFile(parent)) parent = undefined;
        else parent = parent.parent;
      }
      if (!parent) {
        indirect.push(finding);
      } else {
        const attributes = (parent.parent as ts.JsxAttributes).properties;
        const tinted = attributes.some(
          (attribute) =>
            ts.isJsxSpreadAttribute(attribute) ||
            (ts.isJsxAttribute(attribute) && attribute.name.getText() === "data-surface-tint")
        );
        if (!tinted) missing.push(finding);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return { missing, indirect };
}

describe("surfaceTintFor（淡い面の class から系統を返す）", () => {
  it("変種なし・持続する状態の変種の淡い面を系統にする", () => {
    expect(surfaceTintFor("rounded bg-accent-subtle px-2")).toBe("accent");
    expect(surfaceTintFor("bg-accent-muted text-accent-fg-strong")).toBe("accent");
    expect(surfaceTintFor("border-info-border bg-info-subtle text-info-fg")).toBe("info");
    expect(surfaceTintFor("bg-success-subtle/60")).toBe("success");
    expect(surfaceTintFor("has-[:checked]:bg-warning-subtle")).toBe("warning");
    expect(surfaceTintFor("aria-selected:bg-danger-subtle")).toBe("danger");
    expect(surfaceTintFor("rounded bg-surface-hover text-fg-muted")).toBe("neutral");
  });

  it("hover / focus だけの面・淡い面でない class・空は undefined", () => {
    expect(surfaceTintFor("hover:bg-accent-subtle group-hover:bg-info-subtle hover:bg-surface-hover")).toBeUndefined();
    expect(surfaceTintFor("bg-accent-emphasis text-fg-on-accent")).toBeUndefined();
    expect(surfaceTintFor("bg-info-muted bg-surface-sunken")).toBeUndefined();
    expect(surfaceTintFor("")).toBeUndefined();
    expect(surfaceTintFor(undefined)).toBeUndefined();
    expect(surfaceTintFor(false)).toBeUndefined();
  });
});

describe("共有の部品は淡い面に data-surface-tint を付ける（#1671）", () => {
  it("StatusBadge と Banner は面の系統を付け、neutral は付けない", () => {
    expect(renderToStaticMarkup(<StatusBadge variant="info" label="情報" />)).toContain('data-surface-tint="info"');
    expect(renderToStaticMarkup(<StatusBadge variant="pending" label="待機" />)).toContain('data-surface-tint="warning"');
    expect(renderToStaticMarkup(<StatusBadge variant="neutral" label="未設定" />)).not.toContain("data-surface-tint");
    for (const severity of ["success", "info", "warning", "danger"] as const) {
      expect(renderToStaticMarkup(<Banner severity={severity}>本文</Banner>)).toContain(`data-surface-tint="${severity}"`);
    }
  });

  const files = SOURCE_DIRS.flatMap((dir) => sourceFiles(dir));
  const results = files.map((file) => ({ file, ...scan(file) }));

  it("className に淡い面を書いた要素には data-surface-tint がある", () => {
    expect(results.flatMap((result) => result.missing)).toEqual([]);
  });

  it("対応表・定数に淡い面の class を置くファイルは、使う要素に data-surface-tint を付ける", () => {
    // lint は対応表から要素までを追えないため、ファイルの中に data-surface-tint があることだけを確かめる。
    // 例外: Button の aria-pressed の面（cva の variant。文字は text-fg で、淡い面の上でも 14:1 以上）と、
    // Skeleton（文字を持たない読み込み中の形）。
    const exceptions = new Set(["ui/src/components/ui/button.tsx", "ui/src/components/ui/skeleton.tsx"]);
    const withIndirect = results.filter((result) => result.indirect.length > 0);
    const withoutTint = withIndirect
      .filter((result) => !exceptions.has(result.file.slice(packagesDir.length)))
      .filter((result) => !readFileSync(result.file, "utf8").includes("data-surface-tint"))
      .map((result) => result.indirect);
    expect(withoutTint).toEqual([]);
    // 走査が空振りしていない（StatusBadge の対応表などを見つけている）。
    expect(withIndirect.map((result) => result.file.slice(packagesDir.length))).toContain(
      "ui/src/components/data/status-badge.tsx"
    );
  });
});
