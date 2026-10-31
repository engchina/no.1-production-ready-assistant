/**
 * 回答前の確認（SQL の要件の下書き。#1439）の e2e の応答。
 * backend の QuerySessionData（session・requirement_state）と同じ形。表・列は物理名で書く。
 */

export type RequirementStage =
  | "needs_answer"
  | "ready"
  | "corrected"
  | "limit"
  | "minimum"
  | "multi_select"
  // #3366: needs_answer の期間の質問に候補が無い（自由入力だけで答える確認事項）。
  | "free_text_question"
  | "applied"
  // 確認が要らない（SELECT・FROM が明確で、足りない・あいまいな情報が無く、まだ何も聞いていない。#1442）。
  | "clear"
  // #1718: `select * from employee` 相当の質問で、使う表が要確認（ADMIN.EMPLOYEE / ADMIN.EMPLOYEE_HISTORY）。
  | "select_all_table"
  // #1718: `select * from employee` 相当（確定 2 つと、値の無い部分＝指定なし）。
  | "select_all"
  // #1718: select_all に「条件を追加」で並び順を足した後。
  | "select_all_ordered"
  // #1718: 期間で絞る日付の列（今の質問）と、期間の基準日（一覧の候補で答える）が要確認。
  | "date_column"
  // #1718: date_column の基準日を一覧の候補で答えた後（日付の列はまだ要確認）。
  | "date_column_basis_answered"
  // #1718: date_column の基準日と日付の列を答えた後。
  | "date_column_answered"
  // #1718: すべての部分が確定。
  | "all_confirmed"
  // #1761: 「employee」（業務プロファイル ALL_VIEWS 相当）。確認の質問は 0 件で、AI が推測した表
  // （ADMIN.V_EMP_DEPT）に、ほかの表（ADMIN.V_DEPT_PROJECT）を選び直す候補が付いている。
  | "target_choice"
  // #1761: target_choice で ADMIN.V_DEPT_PROJECT を選び直した後（再検査 1 回）。
  | "target_chosen"
  // #1761: target_choice で「その他（自由入力）」に直しを書いた後（再検査 1 回）。
  | "other_corrected";

export const REQUIREMENT_SESSION_ID = "requirement-session-1";
export const REQUIREMENT_QUESTION = "今年度の地域ごとの受注金額";
export const REQUIREMENT_CLARIFIED_QUESTION =
  "2026-04-01〜2027-03-31（ORDERS.ORDER_DATE）の APP.ORDERS の ORDERS.AMOUNT の合計を、CUSTOMERS.REGION ごとに、合計の降順で表示してください。";

const baseItem = {
  value_ja: "",
  reason_ja: "",
  question_ja: "",
  answer_kind: "free_text",
  candidates: [] as Record<string, unknown>[],
  source: "question",
  tables: [] as string[],
  columns: [] as string[],
  relationship_ids: [] as string[],
  sql_hint: "",
  period: null as Record<string, unknown> | null,
  refs_ja: "",
  answered: false,
  answer_ja: "",
};

function item(fields: Record<string, unknown>): typeof baseItem & Record<string, unknown> {
  return { ...baseItem, ...fields };
}

const fromItem = item({
  id: "item-from",
  key: "from_tables",
  section: "from",
  label_ja: "使う表",
  value_ja: "APP.ORDERS と APP.CUSTOMERS",
  status: "confirmed",
  tables: ["APP.ORDERS", "APP.CUSTOMERS"],
  refs_ja: "APP.ORDERS（受注）、APP.CUSTOMERS（顧客）",
});

const joinItem = item({
  id: "item-join",
  key: "join_relationships",
  section: "join",
  label_ja: "表の結合の条件",
  value_ja: "ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID",
  status: "inferred",
  source: "schema",
  relationship_ids: ["fk:APP.FK_ORDERS_CUSTOMER"],
  sql_hint: "ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID",
});

const selectItem = item({
  id: "item-select",
  key: "select_region_total",
  section: "select",
  label_ja: "地域と受注金額の合計",
  value_ja: "CUSTOMERS.REGION と SUM(ORDERS.AMOUNT)",
  status: "inferred",
  columns: ["APP.CUSTOMERS.REGION", "APP.ORDERS.AMOUNT"],
  refs_ja: "CUSTOMERS.REGION（地域）、ORDERS.AMOUNT（受注金額）",
  sql_hint: "CUSTOMERS.REGION, SUM(ORDERS.AMOUNT)",
});

const groupItem = item({
  id: "item-group",
  key: "group_region",
  section: "group_by",
  label_ja: "集計の単位",
  value_ja: "CUSTOMERS.REGION ごと",
  status: "confirmed",
  columns: ["APP.CUSTOMERS.REGION"],
  refs_ja: "CUSTOMERS.REGION（地域）",
  sql_hint: "CUSTOMERS.REGION",
});

const orderItem = item({
  id: "item-order",
  key: "order_total",
  section: "order_by",
  label_ja: "並び順",
  value_ja: "受注金額の合計の降順",
  status: "inferred",
  sql_hint: "SUM(ORDERS.AMOUNT) DESC",
});

const fiscalPeriod = {
  expression_ja: "今年度",
  relative_kind: "this_fiscal_year",
  reference_basis: "execution_date",
  reference_date: "2026-10-11",
  date_column: "APP.ORDERS.ORDER_DATE",
};

const periodQuestion = item({
  id: "item-period",
  key: "period_this_fiscal_year",
  section: "period",
  label_ja: "期間",
  value_ja: "今年度",
  status: "ambiguous",
  question_ja: "「今年度」は、どの期間ですか？",
  reason_ja:
    "相対的な期間の表現は、具体的な日付の範囲で確かめます。基準日は実行日（2026-10-11）を既定にしています（表のデータは読みません）。範囲は開始日と終了日を含みます。年度の開始月が業務プロファイル・用語集・オントロジーに定義されていません。日付の列は ORDERS.ORDER_DATE（受注日）です。",
  answer_kind: "single_select",
  columns: ["APP.ORDERS.ORDER_DATE"],
  refs_ja: "ORDERS.ORDER_DATE（受注日）",
  period: fiscalPeriod,
  candidates: [
    {
      id: "candidate-fy-april",
      label_ja: "2026-04-01〜2027-03-31",
      description_ja: "今年度（4 月始まりの年度、年度の全体）。開始日と終了日を含みます。",
      evidence_ja: "基準日（実行日）2026-10-11 から計算",
      source: "calendar",
      tables: [],
      columns: ["APP.ORDERS.ORDER_DATE"],
      relationship_ids: [],
      sql_hint: "ORDERS.ORDER_DATE >= DATE '2026-04-01' AND ORDERS.ORDER_DATE < DATE '2027-04-01'",
      refs_ja: "ORDERS.ORDER_DATE（受注日）",
    },
    {
      id: "candidate-fy-january",
      label_ja: "2026-01-01〜2026-12-31",
      description_ja: "今年度（1 月始まりの年度、年度の全体）。開始日と終了日を含みます。",
      evidence_ja: "基準日（実行日）2026-10-11 から計算",
      source: "calendar",
      tables: [],
      columns: ["APP.ORDERS.ORDER_DATE"],
      relationship_ids: [],
      sql_hint: "ORDERS.ORDER_DATE >= DATE '2026-01-01' AND ORDERS.ORDER_DATE < DATE '2027-01-01'",
      refs_ja: "ORDERS.ORDER_DATE（受注日）",
    },
    {
      id: "candidate-fy-latest",
      label_ja: "データの最新日を基準にする",
      description_ja:
        "「今年度」を、実行日ではなくデータの最新日から数えます。表のデータは確認の段階では読まないため、日付は SQL の中で求めます。",
      evidence_ja: "基準日の選択（データの最新日）",
      source: "calendar",
      tables: [],
      columns: ["APP.ORDERS.ORDER_DATE"],
      relationship_ids: [],
      sql_hint: "",
      refs_ja: "ORDERS.ORDER_DATE（受注日）",
    },
  ],
});

const periodAnswered = item({
  ...periodQuestion,
  value_ja: "2026-04-01〜2027-03-31",
  status: "confirmed",
  question_ja: "",
  reason_ja: "",
  candidates: [],
  source: "user",
  answered: true,
  answer_ja: "2026-04-01〜2027-03-31",
  sql_hint: "ORDERS.ORDER_DATE >= DATE '2026-04-01' AND ORDERS.ORDER_DATE < DATE '2027-04-01'",
  period: {
    ...fiscalPeriod,
    start_date: "2026-04-01",
    end_date: "2027-03-31",
    range_ja: "2026-04-01〜2027-03-31（開始日と終了日を含む）",
  },
});

const currencyQuestion = item({
  id: "item-currency",
  key: "other_currency",
  section: "other",
  label_ja: "金額の税",
  value_ja: "",
  status: "missing",
  question_ja: "ORDERS.AMOUNT（受注金額）は税込と税抜のどちらで集計しますか？",
  reason_ja: "列のコメントに税の扱いが書かれていません。",
});

const statusQuestion = item({
  id: "item-status",
  key: "where_status",
  section: "where",
  label_ja: "受注状態",
  value_ja: "",
  status: "ambiguous",
  question_ja: "どの受注状態（ORDERS.STATUS）に絞りますか？",
  answer_kind: "multi_select",
  columns: ["APP.ORDERS.STATUS"],
  refs_ja: "ORDERS.STATUS（受注状態）",
  candidates: [
    {
      id: "candidate-shipped",
      label_ja: "出荷済み（SHIPPED）",
      evidence_ja: "列のコメント",
      source: "schema",
      tables: [],
      columns: ["APP.ORDERS.STATUS"],
      relationship_ids: [],
      sql_hint: "ORDERS.STATUS = 'SHIPPED'",
      refs_ja: "ORDERS.STATUS（受注状態）",
    },
    {
      id: "candidate-cancelled",
      label_ja: "取消（CANCELLED）",
      evidence_ja: "列のコメント",
      source: "schema",
      tables: [],
      columns: ["APP.ORDERS.STATUS"],
      relationship_ids: [],
      sql_hint: "ORDERS.STATUS = 'CANCELLED'",
      refs_ja: "ORDERS.STATUS（受注状態）",
    },
  ],
});

// --- #1718 の例（ADMIN.EMPLOYEE） ---------------------------------------------------------

const employeeFrom = item({
  id: "item-employee-from",
  key: "from_employee",
  section: "from",
  label_ja: "使用テーブル",
  value_ja: "ADMIN.EMPLOYEE",
  status: "confirmed",
  tables: ["ADMIN.EMPLOYEE"],
  refs_ja: "ADMIN.EMPLOYEE（社員）",
});

const employeeSelect = item({
  id: "item-employee-select",
  key: "select_all",
  section: "select",
  label_ja: "表示列",
  value_ja: "ADMIN.EMPLOYEE.*",
  status: "confirmed",
  tables: ["ADMIN.EMPLOYEE"],
  sql_hint: "EMPLOYEE.*",
});

/** 値の無い推測（＝その句を使わない「指定なし」）。期間は日付の列だけが付いている（#1718 の症状）。 */
const employeeUnspecified = [
  item({ id: "item-e-join", key: "join_none", section: "join", label_ja: "結合", status: "inferred" }),
  item({
    id: "item-e-period",
    key: "period_none",
    section: "period",
    label_ja: "期間",
    status: "inferred",
    columns: ["ADMIN.EMPLOYEE.HIRE_DATE"],
    refs_ja: "EMPLOYEE.HIRE_DATE（入社日）",
  }),
  item({ id: "item-e-where", key: "where_none", section: "where", label_ja: "絞り込み", status: "inferred" }),
  item({ id: "item-e-group", key: "group_none", section: "group_by", label_ja: "集計", status: "inferred" }),
  item({ id: "item-e-order", key: "order_none", section: "order_by", label_ja: "並び順", status: "inferred" }),
  item({ id: "item-e-limit", key: "limit_none", section: "limit", label_ja: "件数上限", status: "inferred" }),
  item({ id: "item-e-subquery", key: "subquery_none", section: "subquery", label_ja: "サブクエリ", status: "inferred" }),
  item({ id: "item-e-other", key: "other_none", section: "other", label_ja: "その他", status: "inferred" }),
];

const employeeOrderAdded = item({
  id: "item-e-order",
  key: "order_none",
  section: "order_by",
  label_ja: "並び順",
  value_ja: "EMPLOYEE.HIRE_DATE の新しい順",
  status: "confirmed",
  source: "user",
  answered: true,
  answer_ja: "EMPLOYEE.HIRE_DATE の新しい順",
  columns: ["ADMIN.EMPLOYEE.HIRE_DATE"],
  refs_ja: "EMPLOYEE.HIRE_DATE（入社日）",
});

const employeePeriod = item({
  id: "item-e-period-value",
  key: "period_last_year",
  section: "period",
  label_ja: "期間",
  value_ja: "直近 1 年（2025-10-12〜2026-10-11）",
  status: "confirmed",
  sql_hint: "",
});

const employeeDateColumnQuestion = item({
  id: "item-e-date-column",
  key: "period_date_column",
  section: "period",
  label_ja: "期間で絞る日付の列",
  status: "ambiguous",
  question_ja: "「直近 1 年」は、どの日付の列で絞りますか？",
  reason_ja: "ADMIN.EMPLOYEE に日付の列が 2 つあり、質問からは決められません。",
  answer_kind: "single_select",
  candidates: [
    {
      id: "candidate-hire-date",
      label_ja: "EMPLOYEE.HIRE_DATE（入社日）",
      description_ja: "社員が入社した日",
      evidence_ja: "列のコメント",
      source: "schema",
      tables: [],
      columns: ["ADMIN.EMPLOYEE.HIRE_DATE"],
      relationship_ids: [],
      refs_ja: "EMPLOYEE.HIRE_DATE（入社日）",
    },
    {
      id: "candidate-updated-at",
      label_ja: "EMPLOYEE.UPDATED_AT（更新日）",
      description_ja: "社員の情報を更新した日",
      evidence_ja: "列のコメント",
      source: "schema",
      tables: [],
      columns: ["ADMIN.EMPLOYEE.UPDATED_AT"],
      relationship_ids: [],
      refs_ja: "EMPLOYEE.UPDATED_AT（更新日）",
    },
  ],
});

const employeeDateColumnAnswered = item({
  ...employeeDateColumnQuestion,
  value_ja: "EMPLOYEE.HIRE_DATE（入社日）",
  status: "confirmed",
  question_ja: "",
  reason_ja: "",
  candidates: [],
  source: "user",
  answered: true,
  answer_ja: "EMPLOYEE.HIRE_DATE（入社日）",
  columns: ["ADMIN.EMPLOYEE.HIRE_DATE"],
  refs_ja: "EMPLOYEE.HIRE_DATE（入社日）",
});

const employeeBasisQuestion = item({
  id: "item-e-basis",
  key: "period_reference_basis",
  section: "period",
  label_ja: "期間の基準日",
  status: "ambiguous",
  question_ja: "「直近 1 年」は、いつから数えますか？",
  reason_ja: "基準日によって期間が変わります。",
  answer_kind: "single_select",
  candidates: [
    {
      id: "candidate-basis-execution",
      label_ja: "実行日（2026-10-11）から",
      description_ja: "2025-10-12〜2026-10-11",
      evidence_ja: "基準日（実行日）",
      source: "calendar",
      tables: [],
      columns: [],
      relationship_ids: [],
    },
    {
      id: "candidate-basis-latest",
      label_ja: "データの最新日から",
      description_ja: "日付は SQL の中で求めます",
      evidence_ja: "基準日の選択（データの最新日）",
      source: "calendar",
      tables: [],
      columns: [],
      relationship_ids: [],
    },
  ],
});

const employeeBasisAnswered = item({
  ...employeeBasisQuestion,
  value_ja: "実行日（2026-10-11）から",
  status: "confirmed",
  question_ja: "",
  reason_ja: "",
  candidates: [],
  source: "user",
  answered: true,
  answer_ja: "実行日（2026-10-11）から",
});

const employeeTableQuestion = item({
  id: "item-employee-from",
  key: "from_employee",
  section: "from",
  label_ja: "使用テーブル",
  status: "ambiguous",
  question_ja: "「社員」は、どの表ですか？",
  reason_ja: "社員の表が 2 つあり、質問からは決められません。",
  answer_kind: "single_select",
  candidates: [
    {
      id: "candidate-employee",
      label_ja: "ADMIN.EMPLOYEE（社員）",
      evidence_ja: "表のコメント",
      source: "schema",
      tables: ["ADMIN.EMPLOYEE"],
      columns: [],
      relationship_ids: [],
    },
    {
      id: "candidate-employee-history",
      label_ja: "ADMIN.EMPLOYEE_HISTORY（社員の履歴）",
      evidence_ja: "表のコメント",
      source: "schema",
      tables: ["ADMIN.EMPLOYEE_HISTORY"],
      columns: [],
      relationship_ids: [],
    },
  ],
});

// --- #1761 の例（ADMIN.V_EMP_DEPT / ADMIN.V_DEPT_PROJECT） -------------------------------------

export const TARGET_QUESTION = "employee";
export const TARGET_OTHER_TEXT = "部署ごとの社員数を表示してください";
/** 確認のカードの自由入力に文字がある間の説明（#1772。SQL 生成の画面とチャットで同じ文言）。 */
export const REQUIREMENT_PENDING_HINT = "入力した内容は「直しを伝える」で AI に伝えてから、SQL を生成してください。";
/** 確認のカードで別の表を選んだだけ（未送信）の間の説明（#1776。SQL 生成の画面とチャットで同じ文言）。 */
export const REQUIREMENT_TARGET_PENDING_HINT =
  "選んだ内容は「直しを伝える」で AI に伝えてから、SQL を生成してください。";

const viewCandidates = [
  {
    id: "candidate-v-emp-dept",
    label_ja: "ADMIN.V_EMP_DEPT",
    description_ja: "社員と部署",
    evidence_ja: "業務プロファイルの対象の表",
    source: "schema",
    tables: ["ADMIN.V_EMP_DEPT"],
    columns: [],
    relationship_ids: [],
    refs_ja: "ADMIN.V_EMP_DEPT",
  },
  {
    id: "candidate-v-dept-project",
    label_ja: "ADMIN.V_DEPT_PROJECT",
    description_ja: "部署とプロジェクト",
    evidence_ja: "業務プロファイルの対象の表",
    source: "schema",
    tables: ["ADMIN.V_DEPT_PROJECT"],
    columns: [],
    relationship_ids: [],
    refs_ja: "ADMIN.V_DEPT_PROJECT",
  },
];

const viewFromInferred = item({
  id: "item-view-from",
  key: "from_view",
  section: "from",
  label_ja: "使用テーブル",
  value_ja: "ADMIN.V_EMP_DEPT",
  status: "inferred",
  source: "schema",
  answer_kind: "single_select",
  tables: ["ADMIN.V_EMP_DEPT"],
  refs_ja: "ADMIN.V_EMP_DEPT",
  candidates: viewCandidates,
});

const viewSelectAll = item({
  id: "item-view-select",
  key: "select_all",
  section: "select",
  label_ja: "表示列",
  value_ja: "全列",
  status: "inferred",
  sql_hint: "*",
});

const viewFromChosen = item({
  ...viewFromInferred,
  value_ja: "ADMIN.V_DEPT_PROJECT",
  status: "confirmed",
  source: "user",
  answered: true,
  answer_ja: "ADMIN.V_DEPT_PROJECT",
  tables: ["ADMIN.V_DEPT_PROJECT"],
  refs_ja: "ADMIN.V_DEPT_PROJECT",
  candidates: [],
});

const viewOtherAnswered = item({
  id: "item-view-other",
  key: "added_other",
  section: "other",
  label_ja: "そのほか",
  value_ja: TARGET_OTHER_TEXT,
  status: "confirmed",
  source: "user",
  answered: true,
  answer_ja: TARGET_OTHER_TEXT,
});

const TARGET_STAGES: RequirementStage[] = ["target_choice", "target_chosen", "other_corrected"];

const employeeWithoutPeriod = employeeUnspecified.filter((value) => value.section !== "period");

const SKELETON = [
  "SELECT CUSTOMERS.REGION, SUM(ORDERS.AMOUNT)",
  "FROM APP.ORDERS",
  "  JOIN APP.CUSTOMERS ON ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID",
  "WHERE ORDERS.ORDER_DATE >= DATE '2026-04-01' AND ORDERS.ORDER_DATE < DATE '2027-04-01'",
  "GROUP BY CUSTOMERS.REGION",
  "ORDER BY SUM(ORDERS.AMOUNT) DESC",
].join("\n");

const PENDING_SKELETON = SKELETON.replace(
  "WHERE ORDERS.ORDER_DATE >= DATE '2026-04-01' AND ORDERS.ORDER_DATE < DATE '2027-04-01'",
  "WHERE ⟨未確定: 期間⟩"
);

const EMPLOYEE_STAGES: RequirementStage[] = [
  "select_all_table",
  "select_all",
  "select_all_ordered",
  "date_column",
  "date_column_basis_answered",
  "date_column_answered",
];

function employeeSkeleton(stage: RequirementStage, ready: boolean) {
  const lines = ["SELECT EMPLOYEE.*", "FROM ADMIN.EMPLOYEE"];
  if (stage === "date_column" || stage === "date_column_basis_answered" || stage === "date_column_answered") {
    lines.push(
      ready
        ? "WHERE EMPLOYEE.HIRE_DATE >= DATE '2025-10-12' AND EMPLOYEE.HIRE_DATE < DATE '2026-10-12'"
        : "WHERE ⟨未確定: 期間で絞る日付の列⟩"
    );
  }
  if (stage === "select_all_ordered") lines.push("-- 並び順: EMPLOYEE.HIRE_DATE の新しい順");
  return lines.join("\n");
}

export function requirementClarifiedQuestion(stage: RequirementStage) {
  if (stage === "target_choice") return "ADMIN.V_EMP_DEPT の全列を表示してください。";
  if (stage === "target_chosen") return "ADMIN.V_DEPT_PROJECT の全列を表示してください。";
  if (stage === "other_corrected") return "ADMIN.V_EMP_DEPT の部署ごとの社員数を表示してください。";
  if (stage === "date_column" || stage === "date_column_basis_answered" || stage === "date_column_answered") {
    return "EMPLOYEE.HIRE_DATE が直近 1 年（2025-10-12〜2026-10-11）の ADMIN.EMPLOYEE のすべての列を表示してください。";
  }
  if (EMPLOYEE_STAGES.includes(stage)) return "ADMIN.EMPLOYEE のすべての列を表示してください。";
  return REQUIREMENT_CLARIFIED_QUESTION;
}

function stateFor(stage: RequirementStage, items: Record<string, unknown>[]) {
  const unresolved = items.filter(
    (value) => !value.answered && (value.status === "missing" || value.status === "ambiguous")
  );
  const minimum = stage === "minimum"
    ? ["どの表から（FROM）が明確ではありません。", "何を表示するか（SELECT）が明確ではありません。"]
    : [];
  const limit = stage === "limit";
  const ready = unresolved.length === 0 && minimum.length === 0;
  return {
    status: ready ? "ready" : "needs_answer",
    current_item: unresolved[0] ?? null,
    unresolved_items: unresolved,
    answered_count: items.filter((value) => value.answered).length,
    review_round: limit
      ? 3
      : stage === "needs_answer" ||
          stage === "minimum" ||
          stage === "multi_select" ||
          stage === "free_text_question" ||
          stage === "clear" ||
          stage === "target_choice" ||
          stage === "select_all_table" ||
          stage === "date_column" ||
          stage === "all_confirmed"
        ? 0
        : 1,
    max_review_rounds: 3,
    review_limit_reached: limit,
    select_from_clear: minimum.length === 0,
    minimum_missing_ja: minimum,
    can_apply: minimum.length === 0 && (ready || limit),
    summary_ja: "",
    sql_skeleton: TARGET_STAGES.includes(stage)
      ? `SELECT *\nFROM ${stage === "target_chosen" ? "ADMIN.V_DEPT_PROJECT" : "ADMIN.V_EMP_DEPT"}`
      : EMPLOYEE_STAGES.includes(stage)
      ? employeeSkeleton(stage, ready)
      : ready || limit
        ? SKELETON
        : PENDING_SKELETON,
    message_ja: ready
      ? "SQL の要件をまとめました。内容を確かめ、違う所があれば直してから SQL を生成してください。"
      : limit
        ? "AI の再検査は上限（3 回）に達しました。まだ確認の要る項目が 1 件あります。"
        : "SQL を正しく作るため、足りない情報・明確でない情報を 1 つずつ確認します。",
  };
}

function itemsFor(stage: RequirementStage): Record<string, unknown>[] {
  switch (stage) {
    case "needs_answer":
      return [fromItem, selectItem, joinItem, periodQuestion, groupItem, orderItem];
    case "multi_select":
      return [fromItem, selectItem, joinItem, statusQuestion, groupItem, orderItem];
    case "free_text_question":
      return [fromItem, selectItem, joinItem, { ...periodQuestion, candidates: [] }, groupItem, orderItem];
    case "ready":
    case "applied":
      return [fromItem, selectItem, joinItem, periodAnswered, groupItem, orderItem];
    case "clear":
      return [
        fromItem,
        selectItem,
        joinItem,
        { ...periodAnswered, source: "question", answered: false, answer_ja: "" },
        groupItem,
        orderItem,
      ];
    case "corrected":
      return [
        fromItem,
        {
          ...selectItem,
          value_ja: "CUSTOMERS.REGION と AVG(ORDERS.AMOUNT)",
          status: "confirmed",
          source: "user",
          answered: true,
          answer_ja: "CUSTOMERS.REGION と AVG(ORDERS.AMOUNT)",
          sql_hint: "",
        },
        joinItem,
        periodAnswered,
        groupItem,
        orderItem,
      ];
    case "limit":
      return [fromItem, selectItem, joinItem, periodAnswered, groupItem, orderItem, currencyQuestion];
    case "select_all_table":
      return [employeeTableQuestion, employeeSelect, ...employeeUnspecified];
    case "select_all":
      return [{ ...employeeFrom, source: "user", answered: true, answer_ja: "ADMIN.EMPLOYEE（社員）" }, employeeSelect, ...employeeUnspecified];
    case "select_all_ordered":
      return [
        employeeFrom,
        employeeSelect,
        ...employeeUnspecified.map((value) => (value.key === "order_none" ? employeeOrderAdded : value)),
      ];
    case "date_column":
      return [
        employeeFrom,
        employeeSelect,
        employeePeriod,
        employeeDateColumnQuestion,
        employeeBasisQuestion,
        ...employeeWithoutPeriod,
      ];
    case "date_column_basis_answered":
      return [
        employeeFrom,
        employeeSelect,
        employeePeriod,
        employeeDateColumnQuestion,
        employeeBasisAnswered,
        ...employeeWithoutPeriod,
      ];
    case "date_column_answered":
      return [
        employeeFrom,
        employeeSelect,
        employeePeriod,
        employeeDateColumnAnswered,
        employeeBasisAnswered,
        ...employeeWithoutPeriod,
      ];
    case "all_confirmed":
      return [fromItem, selectItem, joinItem, periodAnswered, groupItem, orderItem].map((value) =>
        value.status === "inferred" ? { ...value, status: "confirmed" } : value
      );
    case "target_choice":
      return [viewFromInferred, viewSelectAll];
    case "target_chosen":
      return [viewFromChosen, viewSelectAll];
    case "other_corrected":
      return [viewFromInferred, viewSelectAll, viewOtherAnswered];
    case "minimum":
      return [
        item({
          id: "item-from-missing",
          key: "from_tables",
          section: "from",
          label_ja: "使う表",
          status: "ambiguous",
          question_ja: "どの表のデータを使いますか？",
          answer_kind: "multi_select",
          candidates: [
            {
              id: "candidate-orders",
              label_ja: "APP.ORDERS（受注）",
              evidence_ja: "業務プロファイルの対象の表",
              source: "schema",
              tables: ["APP.ORDERS"],
              columns: [],
              relationship_ids: [],
              refs_ja: "APP.ORDERS（受注）",
            },
          ],
        }),
        item({
          id: "item-select-missing",
          key: "select_output",
          section: "select",
          label_ja: "表示する項目",
          status: "missing",
          question_ja: "結果に表示する列・集計を、Schema の列名で教えてください。",
        }),
      ];
  }
}

/** `POST /query-sessions` などの応答（`data` の中身）。 */
export function requirementSessionData(stage: RequirementStage, version = 1) {
  const items = itemsFor(stage);
  return {
    session: {
      id: REQUIREMENT_SESSION_ID,
      profile_id: "default",
      profile_view_id: "profile-view-requirement",
      ontology_revision_id: "revision-1",
      status: stage === "applied" ? "done" : "awaiting_intent_confirmation",
      original_question: REQUIREMENT_QUESTION,
      current_intent_version: version,
      intents: [],
      clarification_mode: "guided",
      requirement: {
        version,
        question_original: REQUIREMENT_QUESTION,
        clarified_question_ja: requirementClarifiedQuestion(stage),
        items,
        unanswerable_reason_ja: "",
        warnings_ja: stage === "needs_answer" ? ["Schema に無い・業務プロファイルの範囲の外の名前を除きました: APP.SALES"] : [],
        reference_date: "2026-10-11",
        model: "stub",
        sql_skeleton: "",
      },
      requirement_review_round: 0,
      requirement_applied_at: stage === "applied" ? "2026-10-11T00:00:00Z" : null,
      actor_user_uuid: "user-1",
    },
    requirement_state: stateFor(stage, items),
  };
}
