import assert from "node:assert/strict";
import test from "node:test";

import {
  appendToBusinessText,
  conceptDisplayNames,
  findingName,
  findingTarget,
  partitionFindings,
  pendingSuggestions,
  summarizeChanges,
  summarizeOntologyGraph,
  type PublicationDefinition,
} from "../src/features/nl2sql/ontology/publicationSummary.ts";

// #1720: 表 3 つと view 2 つのサンプル（sql_assist_sample）を AI が構築した形。
const definitions: PublicationDefinition[] = [
  { kind: "object_type", api_name: "department", name_ja: "部門", properties: ["department_name"], mappings: [{ owner: "ADMIN", object_name: "DEPARTMENT", column_name: "" }] },
  { kind: "object_type", api_name: "employee", name_ja: "従業員", properties: ["employee_name"], mappings: [{ owner: "ADMIN", object_name: "EMPLOYEE", column_name: "" }] },
  { kind: "object_type", api_name: "v_emp_dept", name_ja: "従業員部門ビュー", properties: ["v_emp_dept_department_name"], mappings: [{ owner: "ADMIN", object_name: "V_EMP_DEPT", column_name: "" }] },
  { kind: "property", api_name: "department_name", name_ja: "部門名", object_type: "department" },
  { kind: "property", api_name: "employee_name", name_ja: "従業員名", object_type: "employee" },
  { kind: "property", api_name: "v_emp_dept_department_name", name_ja: "部門名", object_type: "v_emp_dept" },
  { kind: "link_type", api_name: "employee_department", name_ja: "所属", source: "employee", target: "department" },
  { kind: "value_type", api_name: "string_type", name_ja: "文字列型" },
  { kind: "metric", api_name: "headcount", name_ja: "人数" },
];

test("graph の要約はオブジェクト・関係・プロパティを数え、値型を数えない", () => {
  const summary = summarizeOntologyGraph(definitions);
  assert.equal(summary.objectCount, 3);
  assert.equal(summary.linkCount, 1);
  assert.equal(summary.propertyCount, 3);
  assert.equal(summary.otherCount, 1);
  const employee = summary.objects.find(object => object.apiName === "employee");
  assert.deepEqual(employee?.properties, ["従業員名"]);
  assert.deepEqual(employee?.links, ["所属: → 部門"]);
  assert.equal(employee?.physical, "ADMIN.EMPLOYEE");
  const department = summary.objects.find(object => object.apiName === "department");
  assert.deepEqual(department?.links, ["所属: ← 従業員"]);
});

test("同じ名前のプロパティには属するオブジェクトの名前を添える", () => {
  const names = conceptDisplayNames(definitions);
  assert.equal(names.get("department_name"), "部門名（部門）");
  assert.equal(names.get("v_emp_dept_department_name"), "部門名（従業員部門ビュー）");
  assert.equal(names.get("employee_name"), "従業員名");
});

test("同じ定義を重ねて渡しても所属の名前を添えず、名前に所属があれば重ねない (#1729)", () => {
  const project = { kind: "object_type", api_name: "project", name_ja: "プロジェクト" };
  const ownDepartmentId = { kind: "property", api_name: "department_id_fk_proj", name_ja: "部門ID（プロジェクト）", object_type: "project" };
  const startDate = { kind: "property", api_name: "start_date", name_ja: "開始日", object_type: "project" };
  const names = conceptDisplayNames([project, ownDepartmentId, startDate, project, ownDepartmentId, startDate]);
  assert.equal(names.get("start_date"), "開始日");
  assert.equal(names.get("department_id_fk_proj"), "部門ID（プロジェクト）");
  const other = { kind: "property", api_name: "project_department_id", name_ja: "部門ID（プロジェクト）", object_type: "project" };
  assert.equal(conceptDisplayNames([project, ownDepartmentId, other]).get("project_department_id"), "部門ID（プロジェクト）");
});

test("診断は公開を止めるもの・警告・補うと精度が上がるものに分け、対象の名前を出す", () => {
  const findings = [
    { severity: "error", code: "MAPPING_OUTSIDE_PROFILE", definition_id: "v_emp_dept", field: "mappings", message_ja: "範囲外" },
    { severity: "warning", code: "DEFINITION_CONFLICT", message_ja: "競合" },
    { severity: "info", code: "OBJECT_IDENTITY_REQUIRED", definition_id: "v_emp_dept", field: "primary_key", message_ja: "識別子" },
  ];
  const partition = partitionFindings(findings);
  assert.equal(partition.blocking.length, 1);
  assert.equal(partition.warnings.length, 1);
  assert.equal(partition.improvements.length, 1);
  const names = conceptDisplayNames(definitions);
  // 対象は業務の名前で出し、ID・項目・診断コードは技術的な詳細に分ける（#1729）。
  assert.equal(findingName(findings[2], names), "従業員部門ビュー");
  assert.equal(findingName(findings[1], names), "");
  assert.equal(findingTarget(findings[2]), "v_emp_dept / primary_key / OBJECT_IDENTITY_REQUIRED");
});

test("初めての公開は件数の 1 行と確認が要るものだけにし、値型は出さない (#1729)", () => {
  const findings = [
    { severity: "warning", code: "DEFINITION_CONFLICT", definition_id: "employee_name", message_ja: "『従業員』の『従業員名』の必須が 2 通り…" },
    { severity: "info", code: "OBJECT_GRAIN_REQUIRED", definition_id: "employee", message_ja: "粒度" },
  ];
  const summary = summarizeChanges(definitions.map((after, index) => ({ id: `d-${index}`, before: null, after })), findings, definitions);
  assert.equal(summary.first, true);
  assert.equal(summary.total, 8);
  assert.deepEqual(summary.review.map(row => row.name), ["従業員名"]);
  assert.equal(summary.review[0].issues.length, 1);
});

test("2 回目の公開は確認が要る変更（問題・削除）を上に、問題の無い変更を畳む (#1729)", () => {
  const before = { kind: "property", api_name: "employee_name", name_ja: "従業員名", object_type: "employee", data_type: "string", id: "p1" };
  const after = { ...before, data_type: "number", unit: "円" };
  const removed = { kind: "property", api_name: "email", name_ja: "メール", object_type: "employee" };
  const added = { kind: "object_type", api_name: "project", name_ja: "プロジェクト" };
  const summary = summarizeChanges([
    { id: "p1", before, after },
    { id: "p2", before: removed, after: null },
    { id: "o1", before: null, after: added },
  ], [{ severity: "error", definition_id: "p1", message_ja: "問題" }], [...definitions, after, added]);
  assert.equal(summary.first, false);
  assert.deepEqual(summary.counts, { added: 1, changed: 1, removed: 1 });
  assert.deepEqual(summary.review.map(row => [row.name, row.status]), [["従業員名", "changed"], ["メール", "removed"]]);
  assert.deepEqual(summary.review[0].fields, ["data_type", "unit"]);
  assert.deepEqual(summary.others.map(row => row.name), ["プロジェクト"]);
});

test("改善案は業務説明に書かれた文を重ねず、追記は前の内容を残す (#1729)", () => {
  const suggestions = [
    { severity: "error", text: "部門（department）は ADMIN.DEPARTMENT の表で、主キーは DEPARTMENT_ID。" },
    { severity: "info", text: "従業員は DEPARTMENT_ID で部門に属する（多対 1）。" },
  ];
  const written = "受注の業務です。\n部門（department）は ADMIN.DEPARTMENT の表で、 主キーは DEPARTMENT_ID。";
  assert.deepEqual(pendingSuggestions(suggestions, written).map(item => item.severity), ["info"]);
  assert.equal(appendToBusinessText("受注の業務です。\n\n", "追記"), "受注の業務です。\n追記");
  assert.equal(appendToBusinessText("", "追記"), "追記");
});
