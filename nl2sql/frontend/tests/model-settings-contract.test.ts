import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// モデル設定の API 型は共有パッケージにある（#103）。
const apiSource = readFileSync(
  new URL("../../../platform/packages/system-settings/src/model/types.ts", import.meta.url),
  "utf8"
);
const settingsE2eSource = readFileSync(
  new URL("./e2e/nl2sql-system-settings.spec.ts", import.meta.url),
  "utf8"
);
// モデル設定の schema は3製品共通の pr_system_settings.model にある（#103）。
const backendSettingsSchemaSource = readFileSync(
  new URL(
    "../../../platform/packages/system_settings_backend/src/pr_system_settings/model.py",
    import.meta.url
  ),
  "utf8"
);

test("Enterprise AI model settings keep the default text / vision model fields in frontend contract (#499)", () => {
  for (const field of ["default_text_model_id", "default_vision_model_id"]) {
    assert.match(backendSettingsSchemaSource, new RegExp(`${field}: str`, "u"));
    assert.match(apiSource, new RegExp(`${field}: string;`, "u"));
    assert.match(settingsE2eSource, new RegExp(`${field}:`, "u"));
  }
  assert.doesNotMatch(apiSource, /\bdefault_model_id\b/u);
});

test("Enterprise AI model settings keep the per-model max output tokens in frontend contract (#1719)", () => {
  // 出力の上限は登録モデルごと。共通の LLM / VLM の出力の上限は持たない。
  assert.match(backendSettingsSchemaSource, /max_output_tokens: int = Field\(/u);
  assert.match(apiSource, /max_output_tokens\?: number;/u);
  assert.match(settingsE2eSource, /max_output_tokens:/u);
  for (const field of ["llm_max_output_tokens", "vlm_max_output_tokens"]) {
    assert.doesNotMatch(backendSettingsSchemaSource, new RegExp(`${field}: int`, "u"));
    assert.doesNotMatch(apiSource, new RegExp(`${field}: number;`, "u"));
  }
});
