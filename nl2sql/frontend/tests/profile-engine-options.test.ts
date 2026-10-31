import assert from "node:assert/strict";
import test from "node:test";

import {
  defaultEngineOptions,
  resolveProfileEngine,
} from "../src/features/nl2sql/profileEngineOptions.ts";
import type { Nl2SqlEnginesData } from "../src/features/nl2sql/types.ts";

function engines(available: Record<string, boolean>): Nl2SqlEnginesData {
  return {
    engines: (["select_ai", "select_ai_agent", "enterprise_ai_direct"] as const).map((engine) => ({
      engine,
      available: available[engine] ?? false,
      reason: available[engine] ? "" : "使えません",
    })),
  };
}

test("既定の生成方式の選択肢は、この環境で使える方式だけ（#1524）", () => {
  const all = defaultEngineOptions(
    "select_ai",
    engines({ select_ai: true, select_ai_agent: true, enterprise_ai_direct: true })
  );
  assert.deepEqual(
    all.map((option) => option.value),
    ["select_ai", "select_ai_agent", "enterprise_ai_direct"]
  );
  assert.deepEqual(
    all.map((option) => option.label),
    ["Select AI", "Select AI Agent", "Enterprise AI Direct"]
  );

  const selectAiOnly = defaultEngineOptions("select_ai", engines({ select_ai: true }));
  assert.deepEqual(
    selectAiOnly.map((option) => option.value),
    ["select_ai"]
  );
});

test("保存済みの方式が使えなくなっても、選択を残して使えないことを示す（#1524）", () => {
  const options = defaultEngineOptions("enterprise_ai_direct", engines({ select_ai: true }));
  assert.deepEqual(
    options.map((option) => option.value),
    ["select_ai", "enterprise_ai_direct"]
  );
  assert.equal(options[1]?.label, "Enterprise AI Direct（この環境では使えません）");
});

test("選択肢を読むまでは Select AI と今の値だけを出す（#1524）", () => {
  assert.deepEqual(
    defaultEngineOptions("select_ai_agent", undefined).map((option) => [option.value, option.label]),
    [
      ["select_ai", "Select AI"],
      ["select_ai_agent", "Select AI Agent"],
    ]
  );
});

test("応答に方式の一覧が無くても落とさず、読む前と同じく Select AI と今の値だけを出す（#1524）", () => {
  const malformed = {} as Nl2SqlEnginesData;
  assert.deepEqual(
    defaultEngineOptions("enterprise_ai_direct", malformed).map((option) => [option.value, option.label]),
    [
      ["select_ai", "Select AI"],
      ["enterprise_ai_direct", "Enterprise AI Direct"],
    ]
  );
});

test("画面の生成の方式は、選んだ方式 → 業務プロファイルの既定 → Select AI の順（#1524）", () => {
  assert.equal(resolveProfileEngine("select_ai", "enterprise_ai_direct"), "select_ai");
  assert.equal(resolveProfileEngine("", "enterprise_ai_direct"), "enterprise_ai_direct");
  assert.equal(resolveProfileEngine("unknown", "select_ai_agent"), "select_ai_agent");
  assert.equal(resolveProfileEngine("", undefined), "select_ai");
});
