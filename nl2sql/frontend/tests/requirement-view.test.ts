// 「SQL の要件（AI の理解）」の見せ方（#1718）: 値のある部分だけを一覧にし、値の無い部分は「指定なし」に
// まとめ、状態は 確定 / AI の推測 / 要確認 の 3 つ。値の無い推測を「AI の推測」と示さない。
import assert from "node:assert/strict";
import test from "node:test";

import type { RequirementItem } from "../src/features/nl2sql/ontology/types.ts";
import {
  addConditionItem,
  canAnswerWithCandidate,
  confirmCorrection,
  confirmCorrectionKind,
  confirmTargetItem,
  currentCandidateId,
  requirementItemKind,
  requirementOverview,
} from "../src/features/nl2sql/requirementView.ts";

function item(overrides: Partial<RequirementItem> & Pick<RequirementItem, "key" | "section">): RequirementItem {
  return {
    id: `item-${overrides.key}`,
    label_ja: overrides.key,
    value_ja: "",
    status: "inferred",
    answer_kind: "free_text",
    candidates: [],
    source: "question",
    tables: [],
    columns: [],
    relationship_ids: [],
    answered: false,
    ...overrides,
  };
}

const employeeItems: RequirementItem[] = [
  item({ key: "select_all", section: "select", status: "confirmed", value_ja: "ADMIN.EMPLOYEE.*" }),
  item({ key: "from_employee", section: "from", status: "confirmed", value_ja: "ADMIN.EMPLOYEE" }),
  item({ key: "join_none", section: "join" }),
  item({ key: "period_none", section: "period", columns: ["ADMIN.EMPLOYEE.HIRE_DATE"], refs_ja: "EMPLOYEE.HIRE_DATE" }),
  item({ key: "where_none", section: "where" }),
  item({ key: "group_none", section: "group_by" }),
  item({ key: "order_none", section: "order_by" }),
  item({ key: "limit_none", section: "limit" }),
  item({ key: "subquery_none", section: "subquery" }),
  item({ key: "other_none", section: "other" }),
];

test("値の無い確定・推測は「指定なし」で、AI の推測とは示さない", () => {
  assert.equal(requirementItemKind(item({ key: "where_none", section: "where" })), "unspecified");
  assert.equal(
    requirementItemKind(item({ key: "where_none", section: "where", status: "confirmed" })),
    "unspecified"
  );
  assert.equal(
    requirementItemKind(item({ key: "order", section: "order_by", value_ja: "AMOUNT の多い順" })),
    "inferred"
  );
  assert.equal(
    requirementItemKind(item({ key: "order", section: "order_by", sql_hint: "ORDERS.AMOUNT DESC" })),
    "inferred"
  );
  assert.equal(requirementItemKind(item({ key: "select", section: "select", status: "confirmed", value_ja: "*" })), "confirmed");
  // 不足とあいまいは同じ「要確認」。答えた項目は値が無くても「確定」。
  assert.equal(requirementItemKind(item({ key: "missing", section: "where", status: "missing" })), "needs_answer");
  assert.equal(requirementItemKind(item({ key: "ambiguous", section: "period", status: "ambiguous" })), "needs_answer");
  assert.equal(
    requirementItemKind(item({ key: "answered", section: "where", status: "confirmed", answered: true })),
    "confirmed"
  );
});

test("select * 相当は 2 行の一覧と「指定なし」の 1 行、要確認なしの要約になる", () => {
  const overview = requirementOverview(employeeItems);
  assert.deepEqual(
    overview.listed.map(({ item: value, kind }) => [value.key, kind]),
    [
      ["from_employee", "confirmed"],
      ["select_all", "confirmed"],
    ]
  );
  assert.deepEqual(overview.unspecifiedSections, ["join", "period", "where", "group_by", "order_by", "subquery", "other"]);
  // 件数上限は「指定なし」には並べないが、条件として足せる。
  assert.ok(overview.addableSections.includes("limit"));
  assert.ok(!overview.addableSections.includes("select"));
  assert.ok(!overview.addableSections.includes("from"));
  assert.equal(overview.needsAnswerCount, 0);
  assert.equal(overview.summary, "使用テーブル ADMIN.EMPLOYEE ／ 表示列 ADMIN.EMPLOYEE.*");
});

test("要確認の部分は一覧に残り、指定なし・要約から外れる", () => {
  const items = [
    ...employeeItems.filter((value) => value.section !== "period"),
    item({
      key: "period_date_column",
      section: "period",
      status: "ambiguous",
      question_ja: "どの日付の列で期間を絞りますか？",
      answer_kind: "single_select",
      candidates: [
        { id: "c-hire", label_ja: "EMPLOYEE.HIRE_DATE（入社日）", source: "schema", tables: [], columns: [], relationship_ids: [] },
      ],
    }),
  ];
  const overview = requirementOverview(items);
  assert.equal(overview.needsAnswerCount, 1);
  assert.deepEqual(
    overview.listed.map(({ item: value }) => value.key),
    ["from_employee", "select_all", "period_date_column"]
  );
  assert.ok(!overview.unspecifiedSections.includes("period"));
  assert.ok(!overview.addableSections.includes("period"));
  assert.ok(!overview.summary.includes("HIRE_DATE"));
  assert.ok(canAnswerWithCandidate(items.at(-1)!));
  assert.ok(!canAnswerWithCandidate(item({ key: "free", section: "where", status: "missing" })));
});

test("「条件を追加」は add:<section> の自由入力の項目にする", () => {
  const added = addConditionItem("order_by");
  assert.equal(added.id, "add:order_by");
  assert.equal(added.section, "order_by");
  assert.equal(added.label_ja, "並び順");
  assert.equal(added.answer_kind, "free_text");
  assert.equal(added.question_ja, "並び順の条件を入力してください。");
});

// --- #1761: 確認のカードで対象の表を選び直し、「その他（自由入力）」で直す -------------------------

function candidate(id: string, tables: string[]) {
  return { id, label_ja: tables.join("、"), evidence_ja: "業務プロファイルの対象の表", source: "schema" as const, tables, columns: [], relationship_ids: [] };
}

const inferredFrom = item({
  key: "from_emp",
  section: "from",
  status: "inferred",
  value_ja: "ADMIN.V_EMP_DEPT",
  tables: ["ADMIN.V_EMP_DEPT"],
  answer_kind: "single_select",
  candidates: [candidate("c-emp", ["ADMIN.V_EMP_DEPT"]), candidate("c-project", ["ADMIN.V_DEPT_PROJECT"])],
});

test("確認のカードで選び直せる対象は、AI が決めた（答えていない）FROM に候補が 2 つ以上あるときだけ (#1761)", () => {
  assert.equal(confirmTargetItem([inferredFrom])?.key, "from_emp");
  assert.equal(currentCandidateId(inferredFrom), "c-emp");
  assert.equal(confirmTargetItem([{ ...inferredFrom, answered: true }]), null);
  assert.equal(confirmTargetItem([{ ...inferredFrom, candidates: inferredFrom.candidates.slice(0, 1) }]), null);
  // 要確認の FROM は確認の質問（回答欄）で答える。
  assert.equal(confirmTargetItem([{ ...inferredFrom, status: "ambiguous" }]), null);
  assert.equal(confirmTargetItem(employeeItems), null);
});

test("確認のカードの直しは、別の表を選んだ・その他（自由入力）を書いたときだけ送る (#1761)", () => {
  // 何も変えていない・今の表を選んだ: 直しは無い（そのまま SQL を生成できる）。
  assert.equal(confirmCorrection(inferredFrom, [], ""), null);
  assert.equal(confirmCorrection(inferredFrom, ["c-emp"], "  "), null);
  assert.equal(confirmCorrection(null, [], ""), null);
  // 別の表を選んだ: その FROM の項目への回答。
  assert.deepEqual(confirmCorrection(inferredFrom, ["c-project"], ""), {
    item_id: "item-from_emp",
    selected_candidate_ids: ["c-project"],
    free_text: "",
  });
  // その他（自由入力）: `add:other` への自由入力（AI の再検査が解釈を直す）。
  assert.deepEqual(confirmCorrection(null, [], " 部署ごとの人数 "), {
    item_id: "add:other",
    selected_candidate_ids: [],
    free_text: "部署ごとの人数",
  });
});

test("確認のカードの直しの種類で、生成のボタンが押せない理由の文を選ぶ (#1772 / #1776)", () => {
  // 直しが無い（何も変えていない・元の表に戻した・空白だけ）: 理由を出さない。
  assert.equal(confirmCorrectionKind(confirmCorrection(inferredFrom, [], "")), null);
  assert.equal(confirmCorrectionKind(confirmCorrection(inferredFrom, ["c-emp"], "  ")), null);
  // 別の表を選んだ: 表の選び直し。
  assert.equal(confirmCorrectionKind(confirmCorrection(inferredFrom, ["c-project"], "")), "table");
  // 自由入力: 自由入力（表の選択より先に送る）。
  assert.equal(confirmCorrectionKind(confirmCorrection(inferredFrom, [], "部署ごとの人数")), "free_text");
  assert.equal(confirmCorrectionKind(confirmCorrection(inferredFrom, ["c-project"], "部署ごとの人数")), "free_text");
});
