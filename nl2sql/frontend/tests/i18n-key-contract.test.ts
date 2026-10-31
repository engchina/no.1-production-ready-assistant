import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { t } from "../src/lib/i18n.ts";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(frontendRoot, "..");
const sourceRoot = resolve(frontendRoot, "src");
const dictionaryPaths = [
  resolve(sourceRoot, "lib/i18n.ts"),
  resolve(sourceRoot, "lib/nl2sql-base-i18n.ts"),
];

function filesMatching(directory: string, fileNamePattern: RegExp): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) return filesMatching(path, fileNamePattern);
    return fileNamePattern.test(entry.name) ? [path] : [];
  });
}

function sourceFiles(directory: string): string[] {
  return filesMatching(directory, /\.tsx?$/u);
}

function lineNumber(source: string, index: number): number {
  return source.slice(0, index).split("\n").length;
}

test("静的な i18n key はすべて日本語辞書に定義されている", () => {
  const definedKeys = new Set(
    dictionaryPaths.flatMap((path) =>
      Array.from(readFileSync(path, "utf8").matchAll(/^\s*"([^"]+)"\s*:/gmu), (match) =>
        match[1]
      )
    )
  );
  const missing = sourceFiles(sourceRoot).flatMap((path) => {
    const source = readFileSync(path, "utf8");
    return Array.from(source.matchAll(/\bt\(\s*"([^"]+)"/gu))
      .filter((match) => !definedKeys.has(match[1]))
      .map(
        (match) =>
          `${match[1]} (${relative(frontendRoot, path)}:${lineNumber(source, match.index)})`
      );
  });

  assert.equal(
    missing.length,
    0,
    `日本語辞書に未定義の i18n key があります:\n${missing.join("\n")}`
  );
});

test("共通・スキーマ読込状態は利用者向けの日本語ラベルを返す", () => {
  assert.equal(t("common.loading"), "読み込んでいます");
  assert.equal(t("nl2sql.schema.loading"), "スキーマ情報を読み込んでいます");
});

test("一括選択は範囲が明確な共通文言を返す", () => {
  assert.equal(t("common.selection.selectAll"), "すべて選択");
  assert.equal(t("common.selection.clearAll"), "選択をすべて解除");
  assert.equal(t("common.selection.selectVisible"), "表示中をすべて選択");
  assert.equal(t("common.selection.clearVisible"), "表示中の選択をすべて解除");
  assert.equal(t("common.selection.selectGroup", { name: "APP" }), "APP をすべて選択");
  assert.equal(
    t("common.selection.clearGroup", { name: "APP" }),
    "APP の選択をすべて解除"
  );
  assert.equal(t("profiles.objects.selectSchemaAction"), "すべて選択");
  assert.equal(t("profiles.objects.clearSchema"), "選択をすべて解除");
  assert.equal(t("knowledgeBasePicker.selectAll"), "すべて選択");
  assert.equal(t("knowledgeBasePicker.clear"), "選択をすべて解除");
});

test("利用者が入力する自然言語の呼び名は「質問」に統一されている", () => {
  // 3 製品で利用者の自然言語の入力は「質問」と呼ぶ（#1183）。「クエリ」は SQL・技術の概念
  // （サブクエリなど）にだけ使い、辞書の文言には書かない。旧称「検索クエリ」はコードにも残さない。
  assert.equal(t("nl2sql.question.label"), "質問");
  assert.equal(t("nl2sql.query.actions.startNew"), "新しい質問を開始");
  assert.equal(t("nl2sql.result.rewritten"), "書き換えた質問");
  assert.equal(t("history.rewritten"), "書き換えた質問");

  const dictionaryViolations = dictionaryPaths.flatMap((path) => {
    const source = readFileSync(path, "utf8");
    return Array.from(source.matchAll(/(?<!サブ)クエリ/gu), (match) =>
      `${relative(repositoryRoot, path)}:${lineNumber(source, match.index)}`
    );
  });
  assert.equal(
    dictionaryViolations.length,
    0,
    `利用者向けの文言に「クエリ」が残っています。利用者の自然言語の入力は「質問」と呼んでください:\n${dictionaryViolations.join("\n")}`
  );

  const forbiddenTerm = /検索\s*クエリ/u;
  const checkedFiles = [
    ...filesMatching(sourceRoot, /\.tsx?$/u),
    ...filesMatching(resolve(repositoryRoot, "backend/app"), /\.py$/u),
  ];
  const violations = checkedFiles.flatMap((path) => {
    const source = readFileSync(path, "utf8");
    const match = forbiddenTerm.exec(source);
    return match
      ? [`${relative(repositoryRoot, path)}:${lineNumber(source, match.index)}`]
      : [];
  });

  assert.equal(
    violations.length,
    0,
    `自然言語入力の旧称が残っています。「質問」へ変更してください:\n${violations.join("\n")}`
  );
});

test("利用者の自然言語の入力とその実行を「検索」と呼ばない", () => {
  // 利用者の自然言語の入力は「質問」、それによる SQL の生成と実行は「SQL の生成と実行」、
  // その結果は「実行結果」と呼ぶ（#1189）。一覧の絞り込みの検索欄（「検索語をクリア」など）、
  // 選択欄の候補の検索、類似検索・ベクトル検索などの技術の概念は対象外なので、辞書全体ではなく
  // SQL 生成の画面の主な文言と、旧称の言い回しだけを検査する。
  assert.equal(t("nl2sql.workbench.title"), "質問から SQL を生成して実行");
  assert.equal(t("nl2sql.results.title", { count: 3 }), "実行結果（3 行）");
  // 結果のカードの見出しは、行を開いたシートの見出しとそろえる。
  assert.equal(
    t("queryResults.table.sheetTitle", { name: t("queryResults.name.default"), count: 3 }),
    t("nl2sql.results.title", { count: 3 })
  );
  assert.equal(t("nl2sql.error.submitFailed"), "SQL の生成と実行を開始できませんでした。");
  // チャットの空の状態は 3 製品で同じ文（RAG・Agent と同じ。#1189）。
  assert.equal(t("chat.empty"), "質問を入力して会話を始めます");

  const questionKeys = [
    "nl2sql.workbench.title",
    "nl2sql.workbench.description",
    "nl2sql.question.label",
    "nl2sql.action.run",
    "nl2sql.results.title",
    "nl2sql.error.submitFailed",
    "nl2sql.selectAiOverrides.hint",
    "nl2sql.schema.description",
    "nl2sql.sample.importHint",
    "nl2sql.ontology.nodeKind.queryPlan",
    "feedbackManagement.similarityIndex.summaryHint",
    "feedbackManagement.similarityIndex.indexedHint",
  ] as const;
  const keyViolations = questionKeys.filter((key) => t(key).includes("検索"));
  assert.deepEqual(
    keyViolations,
    [],
    `SQL 生成の文言に「検索」が残っています。「質問」「SQL の生成と実行」「実行結果」へ変更してください: ${keyViolations.join(", ")}`
  );

  // 旧称の言い回し（「NL2SQL 検索」「検索実行」「検索ワークベンチ」「検索画面」「検索結果（N件）」）は辞書のどこにも書かない。
  const legacyPhrase = /NL2SQL\s*検索|検索実行|検索ワークベンチ|検索画面|検索結果（/gu;
  const dictionaryViolations = dictionaryPaths.flatMap((path) => {
    const source = readFileSync(path, "utf8");
    return Array.from(
      source.matchAll(legacyPhrase),
      (match) => `${relative(repositoryRoot, path)}:${lineNumber(source, match.index)} ${match[0]}`
    );
  });
  assert.deepEqual(
    dictionaryViolations,
    [],
    `利用者の質問の実行を「検索」と呼ぶ文言が残っています:\n${dictionaryViolations.join("\n")}`
  );

  // AI 要件確認が利用者へ出す質問・選択肢・書き換えた質問（backend）も同じ。
  const clarificationPath = resolve(
    repositoryRoot,
    "backend/app/features/nl2sql/sql_requirement.py"
  );
  const clarification = readFileSync(clarificationPath, "utf8");
  const clarificationViolations = Array.from(
    clarification.matchAll(/検索(?:結果|対象)/gu),
    (match) =>
      `${relative(repositoryRoot, clarificationPath)}:${lineNumber(clarification, match.index)} ${match[0]}`
  );
  assert.deepEqual(
    clarificationViolations,
    [],
    `AI 要件確認の文言に「検索」が残っています:\n${clarificationViolations.join("\n")}`
  );
});
