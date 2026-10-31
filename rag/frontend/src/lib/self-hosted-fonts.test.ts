import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

// frontend の build は Google Fonts などの外部の font の取得に依存しない（実行時の確認は e2e の
// self-hosted-fonts.spec.ts）。以前は rag/backend の test_deployment_contract.py が frontend/src を読んでいたが、
// frontend だけの変更で backend の job を動かさないため、frontend の単体テストに移した（#1592）。
const SRC = resolve(__dirname, "..");
const EXTERNAL_FONT_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.(css|ts|tsx)$/.test(entry.name) && !entry.name.endsWith(".test.ts") ? [path] : [];
  });
}

describe("自前ホストの font", () => {
  it("frontend のソースは外部の font の CDN を参照しない", () => {
    const files = sourceFiles(SRC);
    expect(files.length).toBeGreaterThan(0);
    const offenders = files.filter((file) => {
      const text = readFileSync(file, "utf-8");
      return EXTERNAL_FONT_HOSTS.some((host) => text.includes(host));
    });
    expect(offenders).toEqual([]);
  });
});
