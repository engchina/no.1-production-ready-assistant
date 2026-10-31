import assert from "node:assert/strict";
import test from "node:test";

import {
  QUESTION_FILTER_LABELS,
  QUESTION_SLOT_LABELS,
  QUESTION_TEMPLATES,
} from "../src/features/nl2sql/questionTemplates.ts";

test("すべてのテンプレートが対象テーブル行を持つ穴埋め形式である", () => {
  assert.equal(QUESTION_TEMPLATES.length, 5);
  assert.deepEqual(QUESTION_TEMPLATES[0], {
    labelKey: "nl2sql.question.template.default",
    body: "",
  });
  for (const template of QUESTION_TEMPLATES.slice(1)) {
    assert.match(template.body, /^対象テーブル/);
    assert.match(template.body, /抽出項目：|集計内容/);
    assert.ok(template.body.includes("抽出条件："));
    // 全行が「ラベル：」形式(値は空欄)
    for (const line of template.body.split("\n")) {
      assert.match(line, /：$/);
    }
  }
});

test("テンプレ本文の全ラベルは解析用スロットラベル一覧に含まれる(単一ソース契約)", () => {
  // GeneratedSqlPanel はこの一覧でテンプレ質問を解析する。
  // テンプレ本文とラベル一覧が乖離すると解釈表示が壊れるため、同一ファイルで管理し整合を固定する。
  for (const template of QUESTION_TEMPLATES.slice(1)) {
    for (const line of template.body.split("\n")) {
      const label = line.replace(/：$/, "");
      assert.ok(
        QUESTION_SLOT_LABELS.includes(label),
        `テンプレ行「${label}」が QUESTION_SLOT_LABELS にありません`
      );
    }
  }
  for (const filterLabel of QUESTION_FILTER_LABELS) {
    assert.ok(QUESTION_SLOT_LABELS.includes(filterLabel));
  }
});

test("ラベルキーが重複せず nl2sql.question.template 名前空間に属する", () => {
  const keys = QUESTION_TEMPLATES.map((template) => template.labelKey);
  assert.equal(new Set(keys).size, keys.length);
  for (const key of keys) {
    assert.match(key, /^nl2sql\.question\.template\./);
  }
});
