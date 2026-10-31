import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { t } from "../src/lib/i18n.ts";

// 共通のシステム設定の画面に、離脱の確認の文言を製品の i18n から渡す（RAG / Agent と同じ。#1118）。
const settingsDir = resolve(dirname(fileURLToPath(import.meta.url)), "../src/components/settings");
const CLIENTS = ["ModelSettingsClient.tsx", "DatabaseSettingsClient.tsx", "UploadStorageSettingsClient.tsx"];

test("共通のシステム設定の画面に draftGuardMessages を渡す", () => {
  for (const name of CLIENTS) {
    const source = readFileSync(resolve(settingsDir, name), "utf8");
    assert.match(source, /import \{ draftGuardMessages \} from "@\/lib\/draft-guard-messages";/, name);
    assert.match(source, /draftGuardMessages=\{draftGuardMessages\(\)\}/, name);
  }
});

test("離脱の確認の文言は i18n にある", () => {
  for (const key of ["settings.leaveGuard.title", "settings.leaveGuard.description", "settings.leaveGuard.confirm"]) {
    const message = t(key);
    assert.notEqual(message, key, key);
    assert.ok(message.length > 0, key);
  }
});
