import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { extname } from "node:path";
import test from "node:test";

const sourceRoot = new URL("../src/", import.meta.url);

function sourceFiles(directory: URL): URL[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const child = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, directory);
    if (entry.isDirectory()) return sourceFiles(child);
    return [".ts", ".tsx"].includes(extname(entry.name)) ? [child] : [];
  });
}

function sourceText(): string {
  return sourceFiles(sourceRoot)
    .map((file) => `\n/* ${file.pathname} */\n${readFileSync(file, "utf8")}`)
    .join("\n");
}

test("messaging contract rejects native dialogs and legacy message tone state", () => {
  const source = sourceText();

  assert.doesNotMatch(source, /window\.(?:alert|confirm)\s*\(/u);
  assert.doesNotMatch(source, /type\s+MessageTone\b/u);
  assert.doesNotMatch(source, /\bsetMessageTone\s*\(/u);
});

test("toast and feedback component messages are not hard-coded Japanese literals", () => {
  const source = sourceText();

  assert.doesNotMatch(
    source,
    /toast\.(?:success|info|warning|error)\(\s*["'`][^"'`]*[ぁ-んァ-ン一-龯]/u
  );
  assert.doesNotMatch(
    source,
    /<(?:Banner|FormStatus|FieldError)\b[^>]*>\s*[ぁ-んァ-ン一-龯]/u
  );
});

test("toaster receives localized region and dismiss labels", () => {
  const main = readFileSync(new URL("../src/main.tsx", import.meta.url), "utf8");

  assert.match(main, /dismissLabel=\{t\("common\.dismiss"\)\}/u);
  assert.match(main, /regionLabel=\{t\("common\.notifications"\)\}/u);
});
