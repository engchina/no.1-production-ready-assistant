/**
 * e2e 用の API mock（hermetic）。
 *
 * e2e は実 backend を起動しない。実 backend は設定保存で利用者の実環境のファイル
 * （platform/.env、backend/.env、model-settings.json 等）へ書き込むため、
 * すべての /api をこの fixture の page.route で応答する。
 *
 * - 既定値 `api-defaults.json` は backend の GET 応答（初期状態）と同じ形。
 * - 保存（POST / PATCH / DELETE）は in-memory の state を更新し、`mockApi.requests` に記録する。
 * - spec 固有の応答は spec 側で後から page.route を登録して上書きする（Playwright は後に登録した
 *   handler を先に評価する）。spec 側で扱わないリクエストは `route.fallback()` でこの fixture に渡し、
 *   `route.continue()`（ネットワークへ流す）は使わない。
 * - どの handler も扱わなかった /api は 404 で終端し、テスト終了時に失敗させる。
 */
import { expect, test as base, type Page, type Route } from "./test";
import { readFileSync } from "node:fs";

import {
  ACCESS_TARGETS,
  LOCAL_CURRENT_USER,
  OPERATOR_ROLE,
  PERMISSION_CATALOG,
  SECURITY_USERS,
  SYSTEM_ADMIN_ROLE,
  expandPermissions,
  type CurrentUserPayload,
} from "./auth";
import { maskUrlCredentials, mcpUrlCredentialProblem } from "../../src/lib/mcp-url";
import { SYNTHETIC_TRACE_ID, progressEventsForStatus } from "./chat-progress-events";

type Json = Record<string, unknown>;

export const MOCK_NOW = "2026-06-28T00:00:00Z";

const defaults = JSON.parse(
  readFileSync(new URL("./api-defaults.json", import.meta.url), "utf-8")
) as Record<string, unknown>;

/** 組み込みの業務 Agent（backend の BUILTIN_AGENT_IDS。#1573）。 */
const BUILTIN_AGENTS = defaults.agents as Json[];
/** 既定の業務 Agent（default）の名前。 */
export const DEFAULT_AGENT_NAME = String(BUILTIN_AGENTS.find((agent) => agent.id === "default")?.name);

export interface RecordedRequest {
  method: string;
  path: string;
  searchParams: URLSearchParams;
  body: unknown;
  /** 小文字の header 名 → 値（CSRF header の検証に使う）。 */
  headers: Record<string, string>;
}

/** `POST /api/auth/login` で受け付ける利用者（spec が `mockApi.state.auth.accounts` に足す）。 */
export interface MockAccount {
  password: string;
  user: CurrentUserPayload;
}

/** MCP 接続の tools/list 相当（`GET /api/settings/mcp-connections/{id}/tools`。#757）。 */
const MOCK_MCP_TOOLS = [
  {
    name: "lookup_customer",
    description: "顧客情報を検索する",
    input_schema: { type: "object", properties: { customer_id: { type: "string" } } },
    output_schema: { type: "object" },
    read_only: true,
    metadata: { fixture: true },
  },
  {
    name: "update_order",
    description: "受注を更新する",
    input_schema: {
      type: "object",
      properties: { order_id: { type: "string" }, status: { type: "string" } },
    },
    output_schema: { type: "object" },
    read_only: false,
    metadata: { fixture: true },
  },
];

type SystemTableRow = { name: string; object_type: string; exists: boolean };

const SYSTEM_TABLE_OBJECTS: Array<[string, string]> = [
  ["AGENT_SCHEMA_OPERATIONS", "TABLE"],
  ["AGENT_SCHEMA_MIGRATIONS", "TABLE"],
  ["AGENT_ROLE_PERMISSIONS", "TABLE"],
  ["AGENT_ROLE_AGENTS", "TABLE"],
  ["AGENT_ROLE_AGENTS_AGENT_IDX", "INDEX"],
];
const SYSTEM_TABLE_MIGRATIONS = [
  "20261002_001_role_access",
  "20261002_002_remove_retired_permission_codes",
  "20261002_003_retire_role_business_views",
];

/** `GET /api/settings/database/system-tables` の応答（backend の `app.system_schema` と同じ形。#751）。 */
function systemTablesStatus(ready: boolean): Record<string, unknown> {
  const exists = (name: string) => ready || !name.startsWith("AGENT_SCHEMA") && name !== "AGENT_ROLE_AGENTS_AGENT_IDX";
  const objects: Array<SystemTableRow & Record<string, unknown>> = SYSTEM_TABLE_OBJECTS.map(([name, object_type]) => ({
    name,
    object_type,
    exists: exists(name),
    estimated_rows: exists(name) && object_type === "TABLE" ? 0 : null,
    created_at: exists(name) ? MOCK_NOW : null,
    last_analyzed_at: null,
  }));
  return {
    status: ready ? "ready" : "partial",
    schema_head: SYSTEM_TABLE_MIGRATIONS.at(-1),
    applied_versions: ready ? SYSTEM_TABLE_MIGRATIONS : [],
    pending_versions: ready ? [] : SYSTEM_TABLE_MIGRATIONS,
    pending_destructive_migrations: ready
      ? []
      : [
          {
            name: "20261002_003_retire_role_business_views",
            description:
              "権限管理でロールに割り当てていた検索・回答プロファイル（AGENT_ROLE_BUSINESS_VIEWS）を削除します。#750 から使っていません。",
          },
        ],
    expected_object_count: objects.length,
    existing_object_count: objects.filter((item) => item.exists).length,
    expected_table_count: 4,
    existing_table_count: objects.filter((item) => item.exists && item.object_type === "TABLE").length,
    missing_objects: objects.filter((item) => !item.exists).map(({ name, object_type }) => ({ name, object_type })),
    retired_objects: ready ? [] : [{ name: "AGENT_ROLE_BUSINESS_VIEWS", object_type: "TABLE" }],
    missing_columns: [],
    missing_foreign_keys: [],
    orphaned_foreign_keys: [],
    mismatched_foreign_keys: [],
    disabled_foreign_keys: [],
    tables: objects.filter((item) => item.object_type === "TABLE").map(({ object_type: _type, ...table }) => table),
    objects,
    operation_state: {
      status: "idle",
      operation_kind: null,
      lease_expires_at: null,
      last_error_code: null,
      schema_epoch: ready ? 1 : 0,
      updated_at: ready ? MOCK_NOW : null,
    },
  };
}

const SYSTEM_TABLES_LEGACY = systemTablesStatus(false);
const SYSTEM_TABLES_READY = systemTablesStatus(true);

/** `GET /api/runtime/storage`（保存先。#839）。Oracle に保存している状態。 */
export const RUNTIME_STORAGE_PERSISTENT = {
  backend: "oracle_checkpoint",
  configured_backend: "auto",
  persistent: true,
  database_configured: true,
  reason: null,
};

/** 保存先にメモリを明示している（DB は設定済み）。作成・変更した内容は再起動で消える。 */
export const RUNTIME_STORAGE_MEMORY = {
  backend: "memory",
  configured_backend: "memory",
  persistent: false,
  database_configured: true,
  reason: "memory_backend",
};

/** `GET /api/runtime/status`（組み込み Runtime。#754）の既定の応答。 */
export const BUILTIN_RUNTIME_STATUS = {
  id: "builtin",
  name: "組み込み Runtime",
  sdk: "openai-agents",
  sdk_version: "0.22.3",
  model_provider: "OpenAI 互換の API（Responses API）",
  model_id: "xai.grok-4",
  ready: true,
  error_code: null,
  message: null,
  models: [
    { model_id: "xai.grok-4", display_name: "Grok 4" },
    { model_id: "openai.gpt-oss-120b", display_name: "gpt-oss-120b" },
  ],
};

/** 業種テンプレートの既定（backend の `AGENT_TEMPLATES` の一部と、使えない Skill を含む例）。 */
export const AGENT_TEMPLATES = [
  {
    id: "internal-policy-helpdesk",
    category: "共通（総務・人事）",
    name: "社内規程の問い合わせ",
    description: "就業規則・各種規程・社内手続きの質問に、規程の条項を示して答えます。",
    instructions: "あなたは総務・人事の問い合わせ窓口です。\n- 業務 RAG で規程を検索し、条項を引用して答える。",
    skill_ids: ["business-rag-research"],
    sample_questions: ["育児休業はいつから取得できますか？", "在宅勤務の申請はどのように行いますか？"],
    evaluation_cases: [{ question: "在宅勤務の申請は？", expected: "申請の方法と期限" }],
  },
  {
    id: "sales-analytics",
    category: "営業",
    name: "営業分析",
    description: "売上・受注・顧客のデータを自然言語で集計し、数字の根拠とともに答えます。",
    instructions: "あなたは営業企画の分析担当です。\n- 構造化データ照会で集計し、期間と単位を示す。",
    skill_ids: ["structured-data-query"],
    sample_questions: ["今月の地域別の売上を教えてください。", "売上上位 10 社の顧客は？"],
    evaluation_cases: [{ question: "今月の地域別の売上は？", expected: "地域ごとの金額" }],
  },
  {
    id: "manufacturing-quality",
    category: "製造",
    name: "品質管理",
    description: "品質基準・不具合報告の文書と、検査・不良率のデータをあわせて調べます。",
    instructions: "あなたは製造部門の品質管理の担当です。",
    skill_ids: ["business-rag-research", "quality_lab_only"],
    sample_questions: ["先月のライン別の不良率を教えてください。", "溶接の外観検査の判定基準は？"],
    evaluation_cases: [{ question: "溶接の判定基準は？", expected: "合否の条件" }],
  },
];

/** 応答に出す評価（mock の内部の数 `_polls` を除く）。 */
function publicJob(job: Json): Json {
  return clone(Object.fromEntries(Object.entries(job).filter(([key]) => key !== "_polls")));
}

/**
 * 評価セットの入力を保存する形にする（backend の `number_cases` と同じ。#965）。
 * id を省いたケースは使われていない `case-<番号>`（位置の番号から探す）、明示した id の重複は 422。
 */
function normalizedSet(body: Json): Json {
  const cases = (body.cases as Json[] | undefined) ?? [];
  const explicit = cases.map((item) => String(item.id ?? "").trim()).filter(Boolean);
  if (new Set(explicit).size !== explicit.length) {
    throw new HttpError(422, "ケースの id が重複しています。");
  }
  const used = new Set(explicit);
  return {
    agent_id: body.agent_id,
    name: body.name,
    description: body.description ?? "",
    cases: cases.map((item, index) => {
      let id = String(item.id ?? "").trim();
      if (!id) {
        let number = index + 1;
        while (used.has(`case-${number}`)) number += 1;
        id = `case-${number}`;
        used.add(id);
      }
      return {
        id,
        question: item.question,
        expected: item.expected,
        expected_tools: item.expected_tools ?? [],
        expected_tools_in_order: Boolean(item.expected_tools_in_order),
        clarification_answers: item.clarification_answers ?? [],
        source_run_id: item.source_run_id ?? null,
      };
    }),
  };
}

/** 同じ質問かを比べる形（backend の `normalize_question` と同じ。#810）。 */
function normalizedQuestion(question: unknown): string {
  return String(question ?? "").trim().split(/\s+/).join(" ");
}

/** 評価の版（#810）。省略時は公開していない変更があれば下書き、なければ公開中の版。 */
/** 下書きで実行した Run か（backend の `metadata.agent_version == "draft"`。#2515）。 */
function isDraftRun(run: Json | undefined): boolean {
  return (run?.metadata as Json | undefined)?.agent_version === "draft";
}

function evaluationVersion(state: MockApiState, agentId: unknown, target: unknown): number | "draft" {
  const agent = state.agents.find((item) => item.id === agentId);
  const published = (agent?.published_version as number | null | undefined) ?? null;
  const chosen = target ?? (agent?.unpublished_changes || published === null ? "draft" : "published");
  if (chosen === "published") {
    if (published === null) {
      throw new HttpError(409, "公開した版がありません。下書きで評価するか、公開してから評価してください。");
    }
    return published;
  }
  return "draft";
}

/** 評価の概要（backend の `summarize` と同じ数え方）。 */
export function evaluationSummary(results: Json[]): Json {
  const judged = results.filter((item) => item.status === "judged" && item.judgement);
  const verdict = (name: string) => judged.filter((item) => (item.judgement as Json).verdict === name).length;
  const completed = results.filter((item) => item.status !== "pending" && item.status !== "running").length;
  const cancelled = results.filter((item) => item.status === "cancelled").length;
  const errors = results.filter((item) =>
    ["run_failed", "needs_approval", "timed_out", "judge_failed"].includes(String(item.status))
  ).length;
  // 判定できなかったケースは合格率の分母から除く（#1845）。
  const unjudged = results.filter((item) => item.status === "judge_failed").length;
  const scores = judged.map((item) => Number((item.judgement as Json).score));
  const finished = completed - cancelled - unjudged;
  const toolResults = results.filter((item) => item.tool_selection_correct !== null && item.tool_selection_correct !== undefined);
  const toolCorrect = toolResults.filter((item) => item.tool_selection_correct === true).length;
  return {
    total: results.length,
    completed,
    correct: verdict("correct"),
    incorrect: verdict("incorrect"),
    uncertain: verdict("uncertain"),
    errors,
    unjudged,
    pass_rate: finished > 0 ? verdict("correct") / finished : null,
    average_score: scores.length ? scores.reduce((sum, value) => sum + value, 0) / scores.length : null,
    tool_cases: toolResults.length,
    tool_correct: toolCorrect,
    tool_accuracy: toolResults.length ? toolCorrect / toolResults.length : null,
    // 出力の項目の判定（#2558。backend の summarize と同じ数え方）。
    ...fieldSummary(results),
  };
}

function fieldSummary(results: Json[]): Json {
  const judged = results.filter((item) => item.field_correct !== null && item.field_correct !== undefined);
  const items = judged.flatMap((item) => (item.field_results as Json[] | undefined) ?? []);
  const itemsCorrect = items.filter((item) => item.correct === true).length;
  const casesCorrect = judged.filter((item) => item.field_correct === true).length;
  return {
    field_cases: judged.length,
    field_correct: casesCorrect,
    field_accuracy: judged.length ? casesCorrect / judged.length : null,
    field_items: items.length,
    field_items_correct: itemsCorrect,
    field_item_accuracy: items.length ? itemsCorrect / items.length : null,
  };
}

function jobListItem(job: Json): Json {
  return {
    id: job.id,
    agent_id: job.agent_id,
    agent_name: job.agent_name,
    set_id: job.set_id ?? "",
    set_name: job.set_name ?? "",
    agent_version: job.agent_version ?? null,
    status: job.status,
    summary: job.summary,
    created_at: job.created_at,
    finished_at: job.finished_at,
  };
}

/** 2 つの評価の比較（backend の `compare_jobs` と同じ。質問で対応させ、合否で変化を決める。#1845）。 */
export function compareEvaluationJobs(base: Json, target: Json): Json {
  const outcome = (result: Json | undefined) => {
    if (!result) return null;
    const judgement = result.judgement as Json | null;
    const status = result.status === "judged" && judgement ? String(judgement.verdict) : String(result.status);
    return {
      case_id: (result.case as Json).id,
      status,
      passed: status === "correct",
      score: judgement ? Number(judgement.score) : null,
      tool_calls: result.tool_calls ?? [],
      tool_selection_correct: result.tool_selection_correct ?? null,
      run_id: result.run_id ?? null,
    };
  };
  const keyed = (job: Json) => {
    const map = new Map<string, Json>();
    const seen = new Map<string, number>();
    for (const result of job.results as Json[]) {
      const question = normalizedQuestion((result.case as Json).question);
      const number = seen.get(question) ?? 0;
      seen.set(question, number + 1);
      map.set(`${question}|${number}`, result);
    }
    return map;
  };
  const before = keyed(base);
  const after = keyed(target);
  const keys = [...before.keys(), ...[...after.keys()].filter((key) => !before.has(key))];
  const counts: Record<string, number> = { improved: 0, regressed: 0, unchanged: 0, base_only: 0, target_only: 0 };
  const rows = keys.map((key) => {
    const left = outcome(before.get(key));
    const right = outcome(after.get(key));
    const change = !left
      ? "target_only"
      : !right
        ? "base_only"
        : right.passed && !left.passed
          ? "improved"
          : left.passed && !right.passed
            ? "regressed"
            : "unchanged";
    counts[change] += 1;
    const source = before.get(key) ?? after.get(key);
    return { question: (source?.case as Json).question, base: left, target: right, change };
  });
  return { base: jobListItem(base), target: jobListItem(target), rows, ...counts };
}

/** 実行中の評価を完了にする（質問に「売上」を含むケースは誤り、それ以外は正しい）。 */
function finishEvaluation(job: Json) {
  for (const [index, result] of (job.results as Json[]).entries()) {
    const question = String((result.case as Json).question);
    const wrong = question.includes("売上");
    result.status = "judged";
    result.run_id = `run-eval-${index + 1}`;
    result.answer = wrong ? "分かりません。" : "毎月 25 日です。過ぎた分は翌月の精算になります。";
    result.judgement = wrong
      ? { verdict: "incorrect", score: 0, summary: "金額を答えていません。", missing_points: ["今月の売上の合計金額"] }
      : { verdict: "correct", score: 1, summary: "要点を満たしています。", missing_points: [] };
    result.duration_ms = 4200;
    const expectedTools = ((result.case as Json).expected_tools as string[] | undefined) ?? [];
    result.tool_calls = wrong ? [] : ["rag__rag_search"];
    // 問い返しへの答えを持つケースは、1 回答えてから回答したことにする（#1845）。
    const answers = ((result.case as Json).clarification_answers as Json[] | undefined) ?? [];
    result.clarification_rounds = answers.length
      ? [
          {
            run_id: `run-eval-${index + 1}-ask`,
            questions: [
              {
                item_id: "req-table-1",
                kind: "nl2sql",
                label: "対象の表",
                question: "どの表の売上を使いますか？",
                options: [],
                // 当てた答え（候補の表示名と自由入力。#1861）。
                selected: ((answers[0].option_ids as string[]) ?? []).filter(Boolean),
                other_text: String(answers[0].other_text ?? ""),
              },
            ],
            answers: answers.slice(0, 1),
            message: `対象の表: ${[...((answers[0].option_ids as string[]) ?? []), answers[0].other_text].filter(Boolean).join("、")}`,
          },
        ]
      : [];
    result.ended_with_clarification = false;
    result.tool_selection_correct = expectedTools.length
      ? expectedTools.every((tool) => (result.tool_calls as string[]).some((called) => called === tool || called.endsWith(`__${tool}`)))
      : null;
  }
  job.status = "completed";
  job.finished_at = MOCK_NOW;
  job.summary = evaluationSummary(job.results as Json[]);
}

function answerTextOf(run: Json): string {
  const answer = ((run.artifacts as Json[] | undefined) ?? []).find((artifact) => artifact.kind === "answer");
  const text = (answer?.content as Json | undefined)?.text;
  return typeof text === "string" ? text : "";
}

/** Run の評価から `GET /api/feedback` の応答を作る（前の期間は 0 件。#774）。 */
function feedbackReportFromRuns(
  runs: Json[],
  days: number,
  filters: { agentId: string | null; rating: string | null; reason: string | null },
  page: { offset: number; limit: number }
): Json {
  const rated = runs.filter(
    (run) => (run.feedback || run.admin_review) && (!filters.agentId || run.agent_id === filters.agentId)
  );
  const owned = rated.filter((run) => run.feedback).map((run) => run.feedback as Json);
  const reviews = rated.filter((run) => run.admin_review).map((run) => run.admin_review as Json);
  const helpful = owned.filter((item) => item.rating === "helpful").length;
  const reasons = new Map<string, number>();
  for (const item of owned) {
    const reason = item.reason as string | null;
    if (reason) reasons.set(reason, (reasons.get(reason) ?? 0) + 1);
  }
  const summary = {
    rated_runs: rated.length,
    total: owned.length,
    helpful,
    not_helpful: owned.length - helpful,
    helpful_rate: owned.length ? helpful / owned.length : null,
    reason_counts: [...reasons.entries()].sort((a, b) => b[1] - a[1]).map(([reason, count]) => ({ reason, count })),
    admin_reviewed: reviews.length,
    admin_not_helpful: reviews.filter((item) => item.rating === "not_helpful").length,
  };
  const matches = (item: unknown) => {
    const rating = item as Json | null | undefined;
    return Boolean(
      rating &&
        (!filters.rating || rating.rating === filters.rating) &&
        (!filters.reason || rating.reason === filters.reason)
    );
  };
  const items = rated
    .filter((run) => matches(run.feedback) || matches(run.admin_review))
    .map((run) => ({
      run_id: run.id,
      thread_id: run.thread_id ?? null,
      agent_id: run.agent_id,
      agent_name: run.agent_id === "default" ? DEFAULT_AGENT_NAME : "",
      user_uuid: run.created_by_user_uuid ?? null,
      display_name: "ローカル利用者",
      question: run.goal,
      answer: answerTextOf(run),
      feedback: run.feedback ?? null,
      admin_review: run.admin_review ?? null,
      reviewer_display_name: run.admin_review ? "ローカル利用者" : "",
      updated_at: MOCK_NOW,
    }));
  return {
    days,
    source: "memory",
    since: MOCK_NOW,
    until: MOCK_NOW,
    summary,
    previous: {
      rated_runs: 0,
      total: 0,
      helpful: 0,
      not_helpful: 0,
      helpful_rate: null,
      reason_counts: [],
      admin_reviewed: 0,
      admin_not_helpful: 0,
    },
    // 一覧はサーバー側のページング（#794）。
    ...pageOfItems(items, page),
  };
}

/** 確かめるべき実行のシグナルの種類（表示の順。#1822。backend の `run_signals.SignalKind`）。 */
export const SIGNAL_KINDS = [
  "not_helpful",
  "validation_failed",
  "unvalidated",
  "ungrounded_no_tools",
  "unanswered",
  "approval_rejected",
  "tool_failed",
  "run_failed",
] as const;

/** 業務データの道具（RAG 以外の MCP 接続のツール）の成功した呼び出しの数（backend の `environment_tool_calls`）。 */
/**
 * 画面から根拠の原文を開いてよい、その Run の RAG の根拠（backend の `rag_figures.run_evidence_items`。#2427）。
 * 画面の根拠の一覧と同じ集合: 根拠のツール（`rag_search` / `rag_retrieve_evidence`）の step の出力、
 * 成果物「回答の根拠」の `items`、成果物「検索の根拠」（`rag__rag_search:*`）の `evidence`。
 */
function runEvidenceItems(run: Json): Json[] {
  const items: Json[] = [];
  const add = (value: unknown) => {
    if (Array.isArray(value)) items.push(...value.filter((item): item is Json => !!item && typeof item === "object"));
  };
  for (const step of (run.steps as Json[] | undefined) ?? []) {
    const call = step.tool_call as Json | null | undefined;
    const result = step.tool_result as Json | null | undefined;
    const tool = String(call?.name ?? "").split("__").pop();
    if (step.status !== "completed" || result?.success !== true) continue;
    if (tool !== "rag_search" && tool !== "rag_retrieve_evidence") continue;
    add((result.output as Json | null | undefined)?.evidence);
  }
  for (const artifact of (run.artifacts as Json[] | undefined) ?? []) {
    const content = artifact.content as Json | undefined;
    if (artifact.kind === "answer_evidence") add(content?.items);
    else if (artifact.kind === "rag_evidence" && String(artifact.name ?? "").startsWith("rag__")) add(content?.evidence);
  }
  return items;
}

function runEvidenceOf(run: Json, query: URLSearchParams): Json | undefined {
  return runEvidenceItems(run).find(
    (item) => item.document_id === query.get("document_id") && item.chunk_id === query.get("chunk_id")
  );
}

function dataToolCalls(run: Json): number {
  return ((run.steps as Json[] | undefined) ?? []).filter((step) => {
    const name = String((step.tool_call as Json | undefined)?.name ?? "");
    const base = name.includes("__") ? name.split("__").slice(1).join("__") : "";
    return step.status === "completed" && Boolean(base) && !base.startsWith("rag_");
  }).length;
}

/** 最終の検証が「資料の根拠を使わなかった」（`no_rag_evidence`。RAG の根拠のツールの有無を問わない）か。 */
function ungrounded(run: Json): boolean {
  const validation = [...((run.artifacts as Json[] | undefined) ?? [])].reverse().find((artifact) => artifact.kind === "answer_validation");
  const content = validation?.content as Json | undefined;
  return (content?.status === "unvalidated" || content?.status === "skipped") && content?.reason === "no_rag_evidence";
}

/** Run から確かめるべき実行のシグナルを決める（backend の `fact_signals` の簡略版。検証は根拠の有無だけを見る）。 */
function runSignals(run: Json): string[] {
  const notHelpful = [run.feedback, run.admin_review].some((item) => (item as Json | null | undefined)?.rating === "not_helpful");
  const answer = ((run.artifacts as Json[] | undefined) ?? []).find((artifact) => artifact.kind === "answer");
  const outcome = ((answer?.content as Json | undefined)?.outcome as Json | undefined)?.value;
  const toolFailures = ((run.steps as Json[] | undefined) ?? []).filter((step) => step.status === "failed").length;
  const rejected = ((run.approvals as Json[] | undefined) ?? []).filter((item) => item.status === "rejected").length;
  const checks: Record<string, boolean> = {
    not_helpful: notHelpful,
    validation_failed: false,
    unvalidated: false,
    // 拒答した Run（#1835。回答の対応が根拠不足・人への引き継ぎ）は「答えられなかった」だけに数える。
    ungrounded_no_tools: ungrounded(run) && dataToolCalls(run) === 0 && outcome !== "insufficient_evidence" && outcome !== "needs_human",
    unanswered: outcome === "insufficient_evidence" || outcome === "needs_human",
    approval_rejected: rejected > 0,
    tool_failed: toolFailures > 0,
    run_failed: run.status === "failed" || run.status === "cancelled",
  };
  return SIGNAL_KINDS.filter((kind) => checks[kind]);
}

function signalSummary(runs: Json[]): Json {
  const flagged = runs.filter((run) => runSignals(run).length > 0);
  return {
    runs: runs.length,
    flagged: flagged.length,
    flagged_rate: runs.length ? flagged.length / runs.length : null,
    counts: SIGNAL_KINDS.map((kind) => ({ kind, count: runs.filter((run) => runSignals(run).includes(kind)).length })),
  };
}

/** Run から `GET /api/feedback/signals` の応答を作る（前の期間は 0 件。#1822）。 */
function signalReportFromRuns(
  runs: Json[],
  days: number,
  filters: { agentId: string | null; kind: string | null },
  page: { offset: number; limit: number }
): Json {
  const scoped = runs.filter(
    (run) =>
      ["completed", "failed", "cancelled"].includes(String(run.status)) &&
      !(run.metadata as Json | undefined)?.evaluation_job_id &&
      (!filters.agentId || run.agent_id === filters.agentId)
  );
  const items = scoped
    .filter((run) => {
      const signals = runSignals(run);
      return signals.length > 0 && (!filters.kind || signals.includes(filters.kind));
    })
    .map((run) => {
      const answer = ((run.artifacts as Json[] | undefined) ?? []).find((artifact) => artifact.kind === "answer");
      const outcome = ((answer?.content as Json | undefined)?.outcome as Json | undefined)?.value ?? null;
      return {
        run_id: run.id,
        thread_id: run.thread_id ?? null,
        agent_id: run.agent_id,
        agent_name: run.agent_id === "default" ? DEFAULT_AGENT_NAME : "",
        user_uuid: run.created_by_user_uuid ?? null,
        display_name: "ローカル利用者",
        question: run.goal,
        answer: answerTextOf(run),
        status: run.status,
        signals: runSignals(run),
        answer_outcome: outcome,
        validation_result: null,
        tool_failures: ((run.steps as Json[] | undefined) ?? []).filter((step) => step.status === "failed").length,
        approvals_rejected: ((run.approvals as Json[] | undefined) ?? []).filter((item) => item.status === "rejected").length,
        data_tool_calls: dataToolCalls(run),
        feedback: run.feedback ?? null,
        admin_review: run.admin_review ?? null,
        created_at: run.created_at ?? MOCK_NOW,
      };
    });
  return {
    days,
    source: "memory",
    since: MOCK_NOW,
    until: MOCK_NOW,
    kind: filters.kind,
    summary: signalSummary(scoped),
    previous: signalSummary([]),
    ...pageOfItems(items, page),
  };
}

/** 集計で選べる期間（日。#794）。 */
export const REPORT_PERIOD_DAYS = [7, 30, 90, 180, 365];
const REPORT_PERIOD_DETAIL = "期間は 7・30・90・180・365 日のどれかにしてください。";

/** offset / limit の query（既定は 25 件/ページ。#1952）。 */
function pageOf(query: URLSearchParams): { offset: number; limit: number } {
  return { offset: Number(query.get("offset") ?? 0), limit: Number(query.get("limit") ?? 25) };
}

const EMPTY_USAGE_TOTALS = {
  runs: 0,
  runs_with_usage: 0,
  requests: 0,
  input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
};

/** Run の無い期間の `GET /api/usage`（#772）。日は MOCK_NOW までの `days` 日（古い順）。 */
export function emptyUsageReport(days: number, timezone = "Asia/Tokyo"): Json {
  const end = new Date(MOCK_NOW);
  const dayList = Array.from({ length: days }, (_, index) => {
    const day = new Date(end);
    day.setUTCDate(end.getUTCDate() - (days - 1 - index));
    return day.toISOString().slice(0, 10);
  });
  return {
    days,
    source: "memory",
    timezone,
    since: `${dayList[0]}T00:00:00+09:00`,
    until: MOCK_NOW,
    totals: { ...EMPTY_USAGE_TOTALS },
    previous: { ...EMPTY_USAGE_TOTALS },
    by_agent: [],
    by_user: [],
    by_model: [],
    by_day: dayList.map((day) => ({ ...EMPTY_USAGE_TOTALS, day })),
  };
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

/** 版の履歴に残す版の数（backend の AGENT_MAX_PUBLISHED_VERSIONS の既定。#2356）。 */
const MAX_PUBLISHED_VERSIONS = 5;

/**
 * 公開のときに古い版を消す（backend の versions_to_prune と同じ規則。#2356）。`keep`（公開中の版など）は古くても
 * 残し、残りの枠を新しい版から埋める。番号は振り直さない。
 */
function pruneVersions(versions: Json[], ...keep: number[]): Json[] {
  const numbers = versions.map((item) => Number(item.version)).sort((a, b) => b - a);
  const kept = new Set(keep.filter((version) => numbers.includes(version)));
  for (const version of numbers) {
    if (kept.size >= MAX_PUBLISHED_VERSIONS) break;
    kept.add(version);
  }
  return versions.filter((item) => kept.has(Number(item.version)));
}

/** データの範囲の候補の既定（#1378）。NL2SQL は 3 件、RAG は 2 件。 */
const DATA_SCOPE_CANDIDATES: Record<string, Json[]> = {
  nl2sql: [
    { id: "profile-sales", name: "売上", description: "売上の集計" },
    { id: "profile-cost", name: "原価", description: "原価と粗利" },
    { id: "profile-hr", name: "人事", description: "人員と勤怠" },
  ],
  rag: [
    { id: "bv-sales", name: "営業の検索・回答プロファイル", description: "営業部門の文書" },
    { id: "bv-legal", name: "法務の検索・回答プロファイル", description: "契約と規程" },
  ],
};

const DATA_SCOPE_LABELS: Record<string, string> = {
  nl2sql: "データ問い合わせ（NL2SQL）",
  rag: "ナレッジ検索（RAG）",
};

function createState() {
  const d = clone(defaults) as Record<string, Json & Json[]>;
  return {
    health: d.health as Json,
    // 組み込み Runtime の状態（#754）。既定は実行できる状態。
    runtimeStatus: clone(BUILTIN_RUNTIME_STATUS) as Json,
    // 保存先（`GET /api/runtime/storage`。#839）。既定は Oracle に保存している状態。
    runtimeStorage: clone(RUNTIME_STORAGE_PERSISTENT) as Json,
    runs: [] as Json[],
    // 会話の一覧（`GET /api/threads`）の期間の日数（`history_days`。#2340）。null は応答に付けない（期間の表示なし）。
    chatHistoryDays: null as number | null,
    // 承認の判断者・版の公開者（ログイン ID）→ 表示名（`GET /api/approvals/{id}` / `GET /api/agents` の `user_display_names`。#1696）。
    // ここに無いログイン ID は削除済みの利用者として扱う。
    userDisplayNames: {
      local: "ローカル利用者",
      system_admin: "システム管理者",
      "reviewer.user": "確認 花子",
      "another.reviewer": "別の 確認者",
    } as Record<string, string>,
    // Run を実行した利用者（UUID。mock の Run は "local"）→ 表示名。system_admin の「利用者」の列と会話の履歴（#1889）。
    // ここに無い UUID は削除済みの利用者として扱う。
    creatorDisplayNames: {
      local: "ローカル利用者",
      "00000000-0000-0000-0000-000000000000": "ローカル利用者",
    } as Record<string, string>,
    // 根拠の原文のプレビュー（`GET /api/runs/{id}/source-preview`。#2389）。chunk_id ごとに上書きできる
    // （`{ error: { status, message } }` で失敗、ほかの項目はプレビューの値の上書き）。発行ごとに URL を変える。
    sourcePreviews: {} as Record<string, Json>,
    sourcePreviewsIssued: 0,
    agents: d.agents as unknown as Json[],
    skills: d.skills as unknown as Json[],
    tools: d.tools as unknown as Json[],
    // 監査の記録（`GET /api/audit/tool-calls`）。offset / limit で切り出して返す（#265）。
    auditRecords: [] as Json[],
    // 業種テンプレート（`GET /api/agent-templates`。#780）。
    agentTemplates: clone(AGENT_TEMPLATES) as Json[],
    // 説明から作る下書きの提案（`POST /api/agents/draft-suggestion`。#2519）。null は既定の提案（説明を名前に使う）。
    draftSuggestion: null as Json | null,
    // チャットの Run が記録する出力の項目（成果物 `structured_output`。#2549）。null は記録しない。
    runOutputContent: null as Json | null,
    // 公開前の評価の門で断る文（`POST /api/agents/{id}/publish` の 409。#2519）。null は門なし。
    publishEvaluationRefusal: null as string | null,
    // データの範囲に選べるプロファイル（`GET /api/agent-data-scopes/{接続}/candidates`。#1378）。
    // 編集者が使えるものだけ（backend は編集者として MCP に問い合わせる）。`dataScopeCandidateErrors` は接続ごとの失敗。
    dataScopeCandidates: clone(DATA_SCOPE_CANDIDATES) as Record<string, Json[]>,
    dataScopeCandidateErrors: {} as Record<string, { status: number; message: string }>,
    // フィードバック（`GET /api/feedback`。#774）。null なら Run の評価から作る。
    feedbackReport: null as Json | null,
    // 確かめるべき実行（`GET /api/feedback/signals`。#1822）。null なら Run から作る。
    signalReport: null as Json | null,
    // 品質評価（#776）。新しい順。作成直後は実行中で、`pollsUntilDone` 回の取得の後に完了する。
    evaluations: [] as Json[],
    evaluationPollsUntilDone: 1,
    // 評価セット（#776）と、Excel の取り込み（parse-xlsx）が返すケース。
    evaluationSets: [] as Json[],
    parsedCases: [
      { id: "from-excel-1", question: "Excel の質問 1", expected: "Excel の要点 1", expected_tools: ["rag_search"] },
      { id: "from-excel-2", question: "Excel の質問 2", expected: "Excel の要点 2", expected_tools: [] },
    ] as Json[],
    // 利用状況（`GET /api/usage`。#772）。期間（日数）ごとの応答。無い期間は Run の無い集計を返す。
    usageReports: {} as Record<string, Json>,
    tracePolicy: d.tracePolicy as Json,
    toolPolicy: d.toolPolicy as Json,
    // MCP 接続（RAG / NL2SQL / 外部 MCP。#757）。
    mcpConnections: d.mcpConnections as Json as { connections: Json[] },
    // MCP 接続ごとの tools/list（未指定の接続は MOCK_MCP_TOOLS。#1814）。
    mcpConnectionTools: {} as Record<string, Json[]>,
    // tools/list を取れない接続の理由（#1814）。
    mcpConnectionErrors: {} as Record<string, string>,
    modelSettings: d.modelSettings as Json,
    databaseSettings: d.databaseSettings as Json,
    uploadStorage: d.uploadStorage as Json,
    // 回答の注意書き（設定は製品ごと。#2405・#2449）。既定は未設定（既定の文）。
    answerNotice: { enabled: true, text: null } as { enabled: boolean; text: string | null },
    // チャットの添付を使うか（#2517。既定は無効）。
    chatAttachmentsEnabled: false,
    traceRecordingEnabled: false,
    // システムテーブルの状態（#751）。既定は旧版の DB（検索・回答プロファイルの表の削除を承認する前）。
    systemTables: clone(SYSTEM_TABLES_LEGACY) as Json,
    // 作成・更新の結果の、再起動が要るプロセス（#1844。spec が設定する）。
    systemTablesRestartRequired: null as Json | null,
    // 作成・更新の失敗（更新しても列が欠けたまま、など。#2458）。null なら成功する。
    systemTablesInitializeError: null as { status: number; message: string; errorCode: string } | null,
    // DB の状態（`GET /api/ready/database`。DB ゲートが使う。#325）。既定は使える状態。
    databaseStatus: {
      status: "ok",
      check: "ok",
      detail: null,
      context_id: "e2e-context",
      schema_status: null,
    } as Json,
    // 認証（#215）。既定はローカルの全権限の利用者（ログインなし）。null は未ログイン（me が 401）。
    auth: {
      currentUser: clone(LOCAL_CURRENT_USER) as CurrentUserPayload | null,
      accounts: {} as Record<string, MockAccount>,
    },
    security: {
      users: clone(SECURITY_USERS),
      roles: [clone(SYSTEM_ADMIN_ROLE), clone(OPERATOR_ROLE)],
      accessTargets: clone(ACCESS_TARGETS) as { agents: Json[] },
    },
    // 自分の API キー（アカウントの画面の「API キー」のタブ。#1873・#1874）。
    apiKeys: { keys: [] as Json[], publicBaseUrl: "" },
  };
}

export type MockApiState = ReturnType<typeof createState>;

export interface MockApi {
  state: MockApiState;
  requests: RecordedRequest[];
  unmocked: string[];
  /** 条件に合う最後のリクエスト。保存 payload の検証に使う。 */
  lastRequest(method: string, path: string): RecordedRequest | undefined;
  /** ログイン中の利用者を差し替える（null で未ログイン = 401）。 */
  setCurrentUser(user: CurrentUserPayload | null): void;
}

class HttpError extends Error {
  readonly status: number;
  readonly errorCode?: string;

  constructor(status: number, message: string, errorCode?: string) {
    super(message);
    this.status = status;
    this.errorCode = errorCode;
  }
}

// ---- backend の権限の判定（#1113）----
// mock は backend と同じく、API の権限 manifest（`app/security/permissions.py` の `ROUTE_PERMISSIONS`）と、
// router の capability の判定（`Depends(require_*)`）で 403 を返す。定義は backend のソースから読み、
// 書き写さない（backend の manifest を変えると mock も追従する）。ローカルの全権限の利用者
// （システム管理者）は判定しない。
const BACKEND_APP_DIR = new URL("../../../backend/app/", import.meta.url);

interface BackendRouteRule {
  method: string;
  pattern: RegExp;
  /** 固定の segment の数（固定の path を `{...}` を含む path より先に選ぶ）。 */
  staticSegments: number;
  permissions?: string[];
  /** router の `require_*` が受け付ける従来のロール（admin は常に通る）。 */
  roles?: string[];
}

const REQUIRE_ROLES: Record<string, string[]> = {
  viewer: ["viewer", "operator", "approver", "auditor"],
  operator: ["operator"],
  approver: ["approver"],
  auditor: ["auditor"],
  admin: ["admin"],
};

/** capability → 従来のロール（backend の `CAPABILITY_ROLES`）。 */
const CAPABILITY_ROLES: Record<string, string> = {
  "agent.runs.view": "viewer",
  "agent.runs.operate": "operator",
  "agent.approvals.decide": "approver",
  "agent.audit.view": "auditor",
  "agent.admin": "admin",
};

function routePattern(template: string): { pattern: RegExp; staticSegments: number } {
  const segments = template.split("/").filter(Boolean);
  const source = segments
    .map((segment) => (/^\{.+\}$/.test(segment) ? "[^/]+" : segment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))
    .join("/");
  return {
    pattern: new RegExp(`^/${source}$`),
    staticSegments: segments.filter((segment) => !segment.startsWith("{")).length,
  };
}

function loadBackendRouteRules(): BackendRouteRule[] {
  const permissions = readFileSync(new URL("security/permissions.py", BACKEND_APP_DIR), "utf-8");
  const codes = new Map(
    [...permissions.matchAll(/^([A-Z][A-Z0-9_]*) = "((?:menu|agent)\.[a-z0-9_.]+)"$/gm)].map((match) => [match[1], match[2]])
  );
  const names = (list: string) =>
    list
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean);
  const groups = new Map(
    [...permissions.matchAll(/^(_[A-Z][A-Z0-9_]*) = _any\(([^)]*)\)/gm)].map((match) => [
      match[1],
      names(match[2]).map((name) => codes.get(name) ?? name),
    ])
  );
  const paths = new Map([...permissions.matchAll(/^(_[A-Z][A-Z0-9_]*) = "(\/[^"]*)"$/gm)].map((match) => [match[1], match[2]]));
  const manifest = permissions.slice(permissions.indexOf("ROUTE_PERMISSIONS:"), permissions.indexOf("\n}\n", permissions.indexOf("ROUTE_PERMISSIONS:")));
  const rules = new Map<string, BackendRouteRule>();
  const entries = /\(\s*"([A-Z]+)",\s*(?:(f?)"([^"]+)"|(_[A-Z][A-Z0-9_]*))\s*\):\s*(?:_any\(([^)]*)\)|(_[A-Z][A-Z0-9_]*))/g;
  for (const match of manifest.matchAll(entries)) {
    const [, method, isFormat, rawPath, pathName, inline, groupName] = match;
    const path =
      pathName !== undefined
        ? (paths.get(pathName) ?? "")
        : isFormat
          ? rawPath
              .replace(/\{(_[A-Z][A-Z0-9_]*)\}/g, (_all, name: string) => paths.get(name) ?? "")
              .replace(/\{\{|\}\}/g, (brace) => brace[0])
          : rawPath;
    const allowed = inline !== undefined ? names(inline).map((name) => codes.get(name) ?? name) : groups.get(groupName) ?? [];
    rules.set(`${method} ${path}`, { method, ...routePattern(path), permissions: allowed });
  }
  const router = readFileSync(new URL("features/agent/router.py", BACKEND_APP_DIR), "utf-8");
  for (const match of router.matchAll(/@router\.(get|post|put|patch|delete)\(\s*"([^"]+)"[^\n]*\n(?:[^\n]*\n)*?(?:async )?def \w+\(([\s\S]*?)\)\s*->/g)) {
    const [, rawMethod, path, params] = match;
    const role = /Depends\(require_(viewer|operator|approver|auditor|admin)\)/.exec(params)?.[1];
    if (!role) continue;
    const key = `${rawMethod.toUpperCase()} ${path}`;
    const rule = rules.get(key) ?? { method: rawMethod.toUpperCase(), ...routePattern(path) };
    rules.set(key, { ...rule, roles: REQUIRE_ROLES[role] });
  }
  return [...rules.values()];
}

const BACKEND_ROUTE_RULES = loadBackendRouteRules();

function isAllAccess(user: CurrentUserPayload): boolean {
  return user.is_system_admin;
}

/** 利用者が使える業務 Agent（null は制限なし。backend の `allowed_agent_ids`。Agent 管理は全対象）。 */
function agentScope(state: MockApiState): Set<string> | null {
  const user = state.auth.currentUser;
  if (!user || isAllAccess(user) || user.allowed_agent_ids === null || user.permissions.includes("agent.admin")) {
    return null;
  }
  // 持ち主の個人の業務 Agent は、ロールに割り当てなくても使える（backend と同じ。#2523）。
  const owned = state.agents.filter((agent) => agent.owner_user_uuid === user.user_uuid).map((agent) => String(agent.id));
  return new Set([...user.allowed_agent_ids, ...owned]);
}

/** Agent 管理か（backend の `"admin" in policy.roles`）。未ログインの local は Agent 管理とみなす。 */
function isAgentAdmin(state: MockApiState): boolean {
  const user = state.auth.currentUser;
  return !user || user.is_system_admin || user.permissions.includes("agent.admin");
}

/** 業務 Agent を変更・下書きで実行できるか（Agent 管理か、個人の Agent の持ち主。backend の `_policy_manages_agent`）。 */
function managesAgent(state: MockApiState, agent: Json): boolean {
  if (isAgentAdmin(state)) return true;
  const user = state.auth.currentUser;
  return Boolean(
    user?.permissions.includes("agent.agents.build") && agent.owner_user_uuid && agent.owner_user_uuid === user.user_uuid
  );
}

function requireManagesAgent(state: MockApiState, agent: Json) {
  if (!agentAllowed(state, agent.id)) throw new HttpError(404, "業務 Agent が見つかりません。");
  if (!managesAgent(state, agent)) {
    throw new HttpError(403, "この業務 Agent を変更できるのは、Agent 管理か持ち主だけです。");
  }
}

function agentAllowed(state: MockApiState, agentId: unknown): boolean {
  const scope = agentScope(state);
  return scope === null || scope.has(String(agentId));
}

/** 業務 Agent の対象範囲の外（backend の `_require_agent_access`）。 */
function requireAgentAccess(state: MockApiState, agentId: unknown) {
  if (!agentAllowed(state, agentId)) throw new HttpError(403, "この業務 Agent を使う権限がありません。");
}

/** capability の不足の文（backend の `_capability_denied_message`。権限管理と同じ名前・並び）。 */
function capabilityDeniedMessage(roles: string[]): string {
  const allowed = new Set([...roles, "admin"]);
  const labels = PERMISSION_CATALOG.filter((item) => allowed.has(CAPABILITY_ROLES[String(item.code)])).map((item) =>
    String(item.label)
  );
  return `この操作を行う権限がありません。必要な権限（いずれか）: ${labels.join("、")}`;
}

/** API の権限 manifest と router の capability の判定（backend の `authorize_api_request` と `require_*`）。 */
function enforceBackendPermissions(state: MockApiState, method: string, apiPath: string) {
  const user = state.auth.currentUser;
  if (!user || isAllAccess(user)) return;
  const path = apiPath.replace(/^\/api/, "") || "/";
  const rule = BACKEND_ROUTE_RULES.filter((candidate) => candidate.method === method && candidate.pattern.test(path)).sort(
    (left, right) => right.staticSegments - left.staticSegments
  )[0];
  if (!rule) return;
  if (rule.permissions && !rule.permissions.some((code) => user.permissions.includes(code))) {
    throw new HttpError(403, "この機能を利用する権限がありません。", "SECURITY_ROUTE_FORBIDDEN");
  }
  if (rule.roles) {
    const roles = new Set(user.permissions.map((code) => CAPABILITY_ROLES[code]).filter(Boolean));
    if (!roles.has("admin") && !rule.roles.some((role) => roles.has(role))) {
      throw new HttpError(403, capabilityDeniedMessage(rule.roles));
    }
  }
}

function findRole(state: MockApiState, roleId: string): Json {
  return findOr404(state.security.roles, "role_id", roleId, "role");
}

/** 実行履歴の種類の絞り込み（backend の `_run_kind_matches`。`not_evaluation` は品質評価を除く。#1983）。 */
function runKindMatches(source: string, kind: string | null): boolean {
  if (!kind) return true;
  if (kind === "not_evaluation") return source !== "evaluation";
  return source === kind;
}

function skillFromPayload(payload: Json, source: string, current?: Json): Json {
  return {
    description: "",
    instructions: "",
    mcp_requirements: [],
    references: [],
    enabled: true,
    tags: [],
    created_at: MOCK_NOW,
    ...current,
    ...payload,
    source: current?.source ?? source,
    updated_at: MOCK_NOW,
  };
}

/** MCP 接続の公開設定（backend の `_mcp_connection_settings` と同じ形。資格情報の値は返さない）。 */
function mcpConnection(payload: Json, current?: Json): Json {
  const merged: Json = {
    timeout_seconds: 10,
    label: null,
    auth_mode: "none",
    source: "runtime",
    removable: true,
    api_key_configured: false,
    oauth_configured: false,
    session_configured: false,
    service_token_configured: false,
    ...current,
    ...payload,
  };
  const baseUrl = typeof merged.base_url === "string" && merged.base_url ? merged.base_url : null;
  const apiKeyConfigured = Boolean(payload.api_key) || Boolean(current?.api_key_configured);
  const oauthConfigured =
    Boolean(payload.oauth_token_url && payload.oauth_client_id && payload.oauth_client_secret) ||
    Boolean(current?.oauth_configured);
  const mode = String(merged.auth_mode);
  const ready =
    mode === "none" ||
    (mode === "api_key" && apiKeyConfigured) ||
    (mode === "oauth_client_credentials" && oauthConfigured) ||
    (mode === "service_token" && Boolean(merged.service_token_configured));
  return {
    server_id: merged.server_id,
    label: merged.label ?? null,
    base_url: baseUrl,
    auth_mode: mode,
    service_audience: mode === "service_token" ? merged.service_audience || merged.server_id : null,
    timeout_seconds: merged.timeout_seconds,
    source: merged.source,
    removable: merged.source === "runtime",
    configured: Boolean(baseUrl) && ready,
    api_key_configured: apiKeyConfigured,
    oauth_configured: oauthConfigured,
    session_configured: Boolean(payload.session_id) || Boolean(current?.session_configured),
    service_token_configured: Boolean(merged.service_token_configured),
    // 標準の接続（RAG / NL2SQL）は注釈を常に使う。ほかは接続ごと（既定はオフ。#1875）。
    trust_tool_annotations: merged.source === "builtin" || merged.trust_tool_annotations === true,
  };
}

/**
 * 保存の API の URL の検証（backend の `_validate_mcp_url` と同じ 422。#1056）。
 * userinfo・資格情報らしい query は拒否する（値は文に含めない）。
 */
function validateMcpUrl(body: Json) {
  if (body.base_url == null) return;
  const value = String(body.base_url).trim();
  if (!value) return;
  const prefix = "body.base_url: Value error, ";
  if (!/^https?:\/\/[^\s/]+/.test(value)) {
    throw new HttpError(422, `${prefix}MCP の URL は http:// または https:// で始めてください。`);
  }
  const problem = mcpUrlCredentialProblem(value);
  const guide = "資格情報は「認証」の欄（API キー・OAuth）で設定してください。";
  if (problem?.kind === "userinfo") {
    throw new HttpError(422, `${prefix}MCP の URL にユーザー名・パスワード（user:pass@）を含めないでください。${guide}`);
  }
  if (problem?.kind === "secretQuery") {
    throw new HttpError(
      422,
      `${prefix}MCP の URL に資格情報のパラメータ（${problem.names.join("・")}）を含めないでください。${guide}`
    );
  }
}

/** 一覧・保存の応答の MCP 接続（保存した URL の資格情報は `***` に伏せる。backend と同じ。#1056）。 */
function publicMcpConnection(connection: Json): Json {
  const raw = typeof connection.base_url === "string" ? connection.base_url : null;
  const masked = raw ? maskUrlCredentials(raw) : raw;
  return { ...connection, base_url: masked, base_url_masked: Boolean(raw) && masked !== raw };
}

function findOr404<T extends Json>(items: T[], key: string, id: string, label: string): T {
  const item = items.find((candidate) => candidate[key] === id);
  if (!item) throw new HttpError(404, `${label} not found: ${id}`);
  return item;
}

function accessTargetPage(items: Json[], query: URLSearchParams) {
  const q = (query.get("q") ?? "").toLowerCase();
  const ids = query.getAll("ids");
  const limit = Number(query.get("limit") ?? "50");
  const offset = Number(query.get("offset") ?? "0");
  const matched = items.filter(
    (item) =>
      (ids.length === 0 || ids.includes(item.id as string)) &&
      (!q || [item.id, item.name, item.description ?? ""].join(" ").toLowerCase().includes(q)),
  );
  return pageOfItems(matched, { offset, limit });
}

/** 検索語（空白区切り。大文字・小文字と全角・半角は区別しない。backend の `_search_terms`）。 */
function searchTerms(value: string | null): string[] {
  return (value ?? "").normalize("NFKC").toLowerCase().split(/\s+/).filter(Boolean);
}

function termsMatch(terms: string[], fields: unknown[]): boolean {
  if (!terms.length) return true;
  const haystack = fields
    .filter((field) => typeof field === "string" && field)
    .join("\n")
    .normalize("NFKC")
    .toLowerCase();
  return terms.every((term) => haystack.includes(term));
}

/** 実行履歴の状態の絞り込み（`active` は queued / running / waiting_confirmation。backend と同じ）。 */
function runStatusMatches(status: string, filter: string | null): boolean {
  if (!filter) return true;
  if (filter === "active") return ["queued", "running", "waiting_confirmation"].includes(status);
  // 応答待ち（承認待ち・確認待ち。実行履歴の共通の状態。#1955）。
  if (filter === "waiting") return ["waiting_approval", "waiting_confirmation"].includes(status);
  return status === filter;
}

/** 終わった Run の終了日時（backend の `run_finished_at`。終わりの Event の時刻）。 */
function runFinishedAt(run: Json): string | null {
  if (!["completed", "failed", "cancelled"].includes(String(run.status))) return null;
  const events = ((run.events as Json[] | undefined) ?? []).slice().reverse();
  const terminal = events.find((event) => ["run.completed", "runtime.failed", "run.cancelled"].includes(String(event.type)));
  return String(terminal?.created_at ?? run.updated_at);
}

/** mock の利用者（UUID）が見ている利用者か（mock の "local" は本人とみなす）。 */
function isViewer(state: MockApiState, uuid: unknown): boolean {
  return ["", "local", state.auth.currentUser?.user_uuid].includes(String(uuid ?? ""));
}

function viewerDisplayName(state: MockApiState, uuid: unknown): string {
  if (isViewer(state, uuid)) return String(state.auth.currentUser?.display_name ?? state.creatorDisplayNames.local ?? "");
  return state.creatorDisplayNames[String(uuid ?? "")] ?? "";
}

/**
 * 見ている利用者から見た Run の事実（backend の `RunViewer`。#1954）。待ちの操作は Run を実行した人と system_admin だけ
 * （会話の持ち主でも、system_admin が実行した Run は操作しない。#1928）。
 */
function runViewerOf(state: MockApiState, run: Json): Json {
  const executor = run.created_by_user_uuid ?? "local";
  const owner = run.thread_owner_user_uuid ?? executor;
  const inThread = Boolean(run.thread_id);
  const executedByMe = isViewer(state, executor);
  return {
    executed_by_me: executedByMe,
    executor_display_name: viewerDisplayName(state, executor),
    owned_by_me: inThread ? isViewer(state, owner) : null,
    conversation_owner_display_name: inThread ? viewerDisplayName(state, owner) : null,
    can_decide: executedByMe || state.auth.currentUser?.is_system_admin !== false,
  };
}

/** system_admin でも他の利用者の会話・Run に内容を足す操作は 403（backend の `OTHERS_CONTENT_FORBIDDEN_MESSAGE`。#3244）。 */
const OTHERS_CONTENT_FORBIDDEN_MESSAGE =
  "他の利用者の会話・実行には、続きの質問・実行・回答・評価を追加できません。閲覧・停止・削除はできます。調べ直すときは自分の会話で質問してください。";

/** Run の持ち主（会話の持ち主。会話の無い Run は実行した人）が見ている利用者か（#3244）。 */
function ownsRunContent(state: MockApiState, run: Json): boolean {
  return isViewer(state, run.thread_owner_user_uuid ?? run.created_by_user_uuid ?? "local");
}

/** 実行履歴の一覧の行（backend の `run_summary`。#1897）。 */
function runSummaryOf(run: Json): Json {
  const pending = ((run.approvals as Json[] | undefined) ?? []).filter((item) => item.status === "pending");
  const metadata = (run.metadata as Json | undefined) ?? {};
  return {
    id: run.id,
    goal: run.goal,
    agent_id: run.agent_id,
    runtime_id: run.runtime_id ?? "builtin",
    status: run.status,
    created_at: run.created_at,
    updated_at: run.updated_at,
    created_by_user_uuid: run.created_by_user_uuid ?? null,
    thread_id: run.thread_id ?? null,
    pending_approval_count: pending.length,
    pending_approval: pending.length
      ? { id: pending[0].id, tool_name: String((pending[0].tool_call as Json | undefined)?.name ?? "") }
      : null,
    resume_pending:
      run.runtime_id === "builtin" && run.status === "queued" && typeof metadata._builtin_sdk_state === "string",
    // きっかけ（backend の `run_facts.run_source`。#1874）。
    source: metadata.evaluation_job_id
      ? "evaluation"
      : metadata.source === "mcp"
        ? "mcp"
        : metadata.replayed_from_run_id
          ? "replay"
          : "user",
    api_key_name: typeof metadata.api_key_name === "string" ? metadata.api_key_name : null,
    // 終了日時・所要時間・token（#1955）。
    finished_at: runFinishedAt(run),
    duration_ms: (() => {
      const finished = runFinishedAt(run);
      if (!finished) return null;
      const ms = Date.parse(finished) - Date.parse(String(run.created_at));
      return Number.isFinite(ms) ? Math.max(0, ms) : null;
    })(),
    total_tokens: typeof (run.usage as Json | undefined)?.total_tokens === "number" ? (run.usage as Json).total_tokens : null,
  };
}

/** 承認の一覧の行（backend の `approval_summary`。#1897）。 */
function approvalSummaryOf(run: Json, approval: Json): Json {
  return {
    id: approval.id,
    run_id: run.id,
    tool_name: String((approval.tool_call as Json | undefined)?.name ?? ""),
    status: approval.status,
    created_at: approval.created_at,
    decided_by: approval.decided_by ?? null,
    decided_at: approval.decided_at ?? null,
    run_goal: run.goal,
    run_status: run.status,
    agent_id: run.agent_id,
  };
}

/** 共通の `Page[T]` の形（backend の `paginate_slice` と同じ。`total` は省略すると items の全件数）。 */
function pageOfItems<T>(items: T[], page: { offset: number; limit: number }, total?: number) {
  const pageItems = items.slice(page.offset, page.offset + page.limit);
  const count = total ?? items.length;
  return {
    items: pageItems,
    total: count,
    limit: page.limit,
    offset: page.offset,
    has_next: page.offset + pageItems.length < count,
  };
}

/** backend の `tool_policy_catalog` と同じ一覧（URL の無い接続は取得できない理由を返す）。 */
function toolPolicyTools(state: MockApiState): Json {
  const policy = state.toolPolicy as { default_mode: string; allow: string[]; ask: string[]; deny: string[] };
  // backend の ToolPolicy.explain と同じ判定と理由（注釈は信頼した接続だけ使う。#1832）。
  const explain = (level: string, sideEffects: boolean, trusted: boolean, standard = false) => {
    if (standard) return { default_decision: "allow", default_reason: "standard" };
    const readOnly = level === "read" && !sideEffects;
    if (readOnly && trusted) return { default_decision: "allow", default_reason: "read_only" };
    return {
      default_decision: policy.default_mode === "deny" ? "deny" : "ask",
      default_reason: readOnly ? "untrusted_annotation" : "default_mode",
    };
  };
  const tools: Json[] = (state.tools as Json[]).map((tool) => ({
    name: tool.name,
    description: tool.description,
    server_id: "control-plane",
    server_label: "",
    permission_level: tool.permission_level,
    side_effects: tool.side_effects,
    ...explain(String(tool.permission_level), Boolean(tool.side_effects), true),
    available: true,
  }));
  const connections: Json[] = [];
  for (const connection of state.mcpConnections.connections) {
    const serverId = String(connection.server_id);
    const label = String(connection.label || serverId);
    const listed =
      connection.configured && !state.mcpConnectionErrors[serverId] ? (state.mcpConnectionTools[serverId] ?? MOCK_MCP_TOOLS) : [];
    connections.push({
      server_id: serverId,
      label,
      tool_count: listed.length,
      configured: Boolean(connection.configured),
      trust_tool_annotations: connection.source === "builtin" || connection.trust_tool_annotations === true,
      error: (state.mcpConnectionErrors[serverId] as string | undefined) ?? null,
    });
    for (const tool of listed) {
      const readOnly = Boolean(tool.read_only);
      const trusted = connection.source === "builtin" || connection.trust_tool_annotations === true;
      tools.push({
        name: `${serverId}__${tool.name}`,
        description: `[${label}] ${tool.description}`,
        server_id: serverId,
        server_label: label,
        permission_level: readOnly ? "read" : "write",
        side_effects: !readOnly,
        // 標準の接続の nl2sql_query は既定で自動実行（backend の STANDARD_AUTO_APPROVED_TOOLS）。
        ...explain(
          readOnly ? "read" : "write",
          !readOnly,
          trusted,
          connection.source === "builtin" && serverId === "nl2sql" && tool.name === "nl2sql_query"
        ),
        available: true,
      });
    }
  }
  const listed = new Set(tools.map((tool) => String(tool.name)));
  const saved = [...policy.allow, ...policy.ask, ...policy.deny].filter((name) => !listed.has(name)).sort();
  for (const name of new Set(saved)) {
    const [serverId, toolName] = name.split("__");
    const source = toolName ? serverId : "control-plane";
    tools.push({
      name,
      description: "",
      server_id: source,
      server_label: String(connections.find((connection) => connection.server_id === source)?.label ?? ""),
      permission_level: null,
      default_reason: null,
      side_effects: null,
      default_decision: null,
      available: false,
    });
  }
  return { tools, connections };
}

/** 1 リクエストを state に対して処理する。未対応は undefined を返す。 */
function isWithdrawn(run: Json): boolean {
  return (run.metadata as Json | undefined)?.clarification_withdrawn === true;
}

/**
 * 回答前の確認の操作（#1839。`POST /api/runs/{id}/confirmations/{cid}`）。backend と同じく、答え・スキップは
 * Agent 自身の確認なら同じ Run の続き（mock はすぐ完了）、ツールの結果の確認なら次の Run を作る。やめると確認の Run を
 * 会話から外し、質問を返す。spec は `mockApi.state.runs` の確認の状態と、記録した request で確かめる。
 */
function decideConfirmation(state: MockApiState, run: Json, confirmationId: string, body: Json): Json {
  const confirmations = (run.confirmations as Json[] | undefined) ?? [];
  const confirmation = confirmations.find((item) => item.id === confirmationId);
  if (!confirmation) throw new HttpError(404, "回答前の確認が見つかりません。");
  if (confirmation.status !== "pending") {
    throw new HttpError(409, "この確認は答え済みか、会話が先へ進んだため答えられません。", "confirmation_closed");
  }
  const action = String(body.action ?? "");
  const items = (confirmation.items as Json[] | undefined) ?? [];
  if (action === "cancel") {
    confirmation.status = "cancelled";
    run.metadata = { ...((run.metadata as Json | undefined) ?? {}), clarification_withdrawn: true };
    if (confirmation.source === "agent") run.status = "cancelled";
    return { run, next_run: null, question: run.goal };
  }
  const answers = action === "answer" ? ((body.answers as Json[] | undefined) ?? []) : [];
  if (action === "answer" && items.some((item) => !answers.some((answer) => answer.item_id === item.id))) {
    throw new HttpError(422, "すべての確認事項で、選択肢を選ぶか「その他（自由入力）」に書いてください。");
  }
  confirmation.status = action === "answer" ? "answered" : "skipped";
  confirmation.answers = answers;
  if (confirmation.source === "agent") {
    run.status = "completed";
    run.artifacts = [
      ...((run.artifacts as Json[] | undefined) ?? []),
      { id: `${run.id}-answer`, name: "回答", kind: "answer", created_at: MOCK_NOW, content: { text: "確認の答えで回答しました。" } },
    ];
    return { run, next_run: null, question: null };
  }
  const entries = items.map((item) => {
    const answer = answers.find((candidate) => candidate.item_id === item.id);
    const optionIds = (answer?.option_ids as string[] | undefined) ?? [];
    const options = (item.options as Json[] | undefined) ?? [];
    const labels = new Map(options.map((option) => [option.id, option.label]));
    return {
      item_id: item.id,
      prompt: item.prompt,
      selected: optionIds.map((id) => String(labels.get(id) ?? "")),
      option_ids: optionIds,
      other_text: String(answer?.other_text ?? ""),
      // backend の `record_entries` と同じく、選択肢の無い確認事項を残す（#3410）。
      free_text_only: options.length === 0,
    };
  });
  const id = `run-chat-${state.runs.length + 1}`;
  const nextRun: Json = {
    id,
    goal: entries.map((entry) => `${String(entry.prompt)}: ${entry.selected.join("、")}`).join("\n"),
    agent_id: run.agent_id,
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    confirmations: [],
    artifacts: [{ id: `${id}-answer`, name: "回答", kind: "answer", created_at: MOCK_NOW, content: { text: "確認の答えで回答しました。" } }],
    pending_tool_calls: [],
    metadata: {
      clarification_response: {
        confirmation_id: confirmation.id,
        source: confirmation.source,
        action,
        source_run_id: run.id,
        question_run_id: run.id,
        round: confirmation.round,
        entries,
      },
      clarification_round: confirmation.round,
    },
    created_by_user_uuid: "local",
    thread_id: run.thread_id,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  };
  state.runs.push(nextRun);
  return { run, next_run: nextRun, question: null };
}

function handle(state: MockApiState, method: string, path: string, query: URLSearchParams, body: Json) {
  const segments = path.replace(/^\/api\/?/, "").split("/").map(decodeURIComponent);
  const [head, second, third] = segments;
  const at = (...parts: string[]) =>
    parts.length === segments.length && parts.every((part, index) => part === "*" || part === segments[index]);

  // --- 認証（platform の共通認証。#215） ---
  if (head === "auth") {
    if (method === "GET" && at("auth", "me")) {
      if (!state.auth.currentUser) throw new HttpError(401, "ログインが必要です。", "auth.unauthenticated");
      return state.auth.currentUser;
    }
    if (method === "POST" && at("auth", "login")) {
      const account = state.auth.accounts[String(body.login_user_id ?? "")];
      if (!account || account.password !== body.password) {
        throw new HttpError(401, "ログインユーザーIDまたはパスワードを確認してください。", "auth.invalid_credentials");
      }
      state.auth.currentUser = clone(account.user);
      return state.auth.currentUser;
    }
    if (method === "POST" && at("auth", "logout")) {
      state.auth.currentUser = null;
      return { logged_out: true };
    }
    if (method === "POST" && at("auth", "password", "change")) {
      // 変更後はセッションを失効させ、新しいパスワードでのログインを求める（backend と同じ）。
      state.auth.currentUser = null;
      return { changed: true };
    }
  }
  // --- 自分の API キー（`/api/account/api-keys*`。ログインだけで使える。#1873・#1874） ---
  if (head === "account" && second === "api-keys") {
    if (method === "GET" && at("account", "api-keys")) {
      return { keys: state.apiKeys.keys, public_base_url: state.apiKeys.publicBaseUrl };
    }
    if (method === "POST" && at("account", "api-keys")) {
      const id = `key${String(state.apiKeys.keys.length + 1).padStart(13, "0")}`;
      const key = {
        id,
        name: String(body.name ?? ""),
        token_prefix: `prak_${id}_E2Ex`,
        products: (body.products as Json[] | undefined) ?? [],
        resource_scopes: (body.resource_scopes as Json | undefined) ?? {},
        owner_user_uuid: String(state.auth.currentUser?.user_uuid ?? ""),
        owner_display_name: String(state.auth.currentUser?.display_name ?? ""),
        created_at: "2026-10-15T00:00:00Z",
        expires_at: null,
        last_used_at: null,
        expired: false,
      };
      state.apiKeys.keys = [key, ...state.apiKeys.keys];
      return { key, token: `prak_${id}_${"E".repeat(43)}` };
    }
    if (method === "DELETE" && at("account", "api-keys", "*")) {
      state.apiKeys.keys = state.apiKeys.keys.filter((key) => (key as { id: string }).id !== third);
      return { id: third };
    }
  }
  if (head === "security") {
    if (method === "GET" && at("security", "api-keys")) return { keys: state.apiKeys.keys };
    if (method === "DELETE" && at("security", "api-keys", "*")) {
      state.apiKeys.keys = state.apiKeys.keys.filter((key) => (key as { id: string }).id !== third);
      return { id: third };
    }
    if (method === "GET" && at("security", "users")) return state.security.users;
    if (method === "GET" && at("security", "roles")) {
      const includeArchived = query.get("include_archived") === "true";
      return state.security.roles.filter((role) => includeArchived || !role.archived);
    }
    if (method === "GET" && at("security", "permissions")) return PERMISSION_CATALOG;
    // 権限管理の対象の候補（#608）: backend と同じく q（名前・ID・説明）・ids・limit / offset で絞った Page を返す。
    if (method === "GET" && at("security", "access-targets", "agents")) {
      return accessTargetPage(state.security.accessTargets.agents, query);
    }
    if (method === "PUT" && at("security", "roles", "*", "access")) {
      const role = findRole(state, third);
      // backend の SecurityService.update_role_access と同じ順の確認。
      if (role.is_built_in) {
        throw new HttpError(409, "組み込み SYSTEM_ADMIN ロールは変更できません。", "SECURITY_STATE_CONFLICT");
      }
      if (role.archived) throw new HttpError(409, "アーカイブ済みロールは変更できません。", "SECURITY_STATE_CONFLICT");
      const catalog = new Set(PERMISSION_CATALOG.map((item) => String(item.code)));
      const requested = ((body.permissions as string[] | undefined) ?? []).map((code) => code.trim()).filter(Boolean);
      const unknownCodes = [...new Set(requested.filter((code) => !catalog.has(code)))].sort();
      if (unknownCodes.length) {
        throw new HttpError(400, `未登録の権限コードです: ${unknownCodes.join(", ")}`, "SECURITY_REQUEST_INVALID");
      }
      const permissions = [...new Set(requested)].sort();
      // 業務 Agent の存在は Runtime の業務 Agent（mock は state.agents と権限管理の対象の候補）で確かめる。
      const knownAgents = new Set(
        [...state.agents, ...state.security.accessTargets.agents].map((agent) => String(agent.id))
      );
      const currentAgents = new Set(((role.agent_ids as string[] | undefined) ?? []).map(String));
      const requestedAgents = [
        ...new Set(((body.agent_ids as string[] | undefined) ?? []).map((id) => id.trim()).filter(Boolean)),
      ];
      const unknownAgents = requestedAgents.filter((id) => !knownAgents.has(id) && !currentAgents.has(id)).sort();
      if (unknownAgents.length) {
        throw new HttpError(400, `エージェントが見つかりません: ${unknownAgents.join(", ")}`, "SECURITY_REQUEST_INVALID");
      }
      const grantsAll = expandPermissions(permissions).includes("agent.admin");
      // agent.admin を含むロールは対象を空に正規化する（backend と同じ）。
      const agentIds = grantsAll ? [] : requestedAgents.filter((id) => knownAgents.has(id)).sort();
      const actor = state.auth.currentUser;
      if (actor && !actor.is_system_admin) {
        // 自分が持たない権限・自分の範囲外の業務 Agent を足すと 403（権限の昇格を防ぐ）。
        const before = new Set(expandPermissions((role.permissions as string[] | undefined) ?? []));
        const added = expandPermissions(permissions).filter((code) => !before.has(code));
        if (!added.every((code) => actor.permissions.includes(code))) {
          throw new HttpError(403, "自分が持たない権限をロールに追加することはできません。", "SECURITY_PERMISSION_DENIED");
        }
        const scope = actor.allowed_agent_ids;
        if (scope !== null && agentIds.some((id) => !currentAgents.has(id) && !scope.includes(id))) {
          throw new HttpError(
            403,
            "自分が利用できないエージェントをロールに追加することはできません。",
            "SECURITY_PERMISSION_DENIED"
          );
        }
      }
      if (body.version !== role.version) {
        throw new HttpError(409, "ロールが別の操作で更新されています。", "SECURITY_STATE_CONFLICT");
      }
      Object.assign(role, { version: (role.version as number) + 1, permissions, agent_ids: agentIds });
      return role;
    }
  }

  // --- health ---
  if (method === "GET" && at("health")) return state.health;
  if (method === "GET" && at("ready", "database")) return state.databaseStatus;
  // システムテーブル（#751）。作成・更新は承認（allow_destructive）が無ければ 409（backend と同じ）。
  if (method === "GET" && at("settings", "database", "system-tables")) return state.systemTables;
  if (method === "POST" && at("settings", "database", "system-tables", "initialize")) {
    const current = state.systemTables as Json;
    const failure = state.systemTablesInitializeError;
    if (failure) throw new HttpError(failure.status, failure.message, failure.errorCode);
    const destructive = (current.pending_destructive_migrations as Json[] | undefined) ?? [];
    if (!body.recreate && destructive.length > 0 && body.allow_destructive !== true) {
      throw new HttpError(409, "データを削除する未適用の migration があります。内容を確認して承認してください。");
    }
    state.systemTables = clone(SYSTEM_TABLES_READY) as Json;
    return {
      ...(state.systemTables as Json),
      operation: body.recreate ? "recreated" : "migrated",
      dropped_object_count: 1,
      created_object_count: 3,
      restart_required: state.systemTablesRestartRequired,
    };
  }

  // --- 組み込み Runtime（#754） ---
  if (method === "GET" && at("runtime", "status")) return state.runtimeStatus;
  if (method === "GET" && at("runtime", "storage")) return state.runtimeStorage;

  // --- Run / 承認 / 監査 ---
  // 一覧は利用者の対象範囲の業務 Agent の Run だけ（backend の `_filter_runs_for_actor`）。行は要約で、
  // 状態・検索語で絞り、limit / offset で切り出す（#1897）。並びは state の順（spec が並べた順）。
  if (method === "GET" && at("runs")) {
    const visible = state.runs.filter((run) => agentAllowed(state, run.agent_id));
    const status = query.get("status");
    const terms = searchTerms(query.get("q"));
    const agentNames = new Map(state.agents.map((agent) => [String(agent.id), String(agent.name)]));
    // 実行者の絞り込み（すべて / 自分が実行 / 他の人が実行。#1954）。
    const executor = query.get("executor") ?? "all";
    // 種類（きっかけ）と期間（`since` 以後に始めた Run。#1955）。
    const kind = query.get("kind");
    const since = query.get("since");
    // 画面は端末の今の時刻から期間の開始を求める。モックの記録は MOCK_NOW の時刻なので、backend の時計が
    // MOCK_NOW にあるとみなし、今から同じ幅だけ前を期間の開始として比べる（既定の過去 7 日。#3386）。
    const sinceMs = since ? Date.parse(MOCK_NOW) - (Date.now() - Date.parse(since)) : null;
    const matched = visible.filter(
      (run) =>
        runStatusMatches(String(run.status), status) &&
        runKindMatches(String(runSummaryOf(run).source), kind) &&
        (sinceMs === null || Date.parse(String(run.created_at)) >= sinceMs) &&
        (executor === "all" || isViewer(state, run.created_by_user_uuid ?? "local") === (executor === "mine")) &&
        termsMatch(terms, [run.goal, run.id, run.agent_id, agentNames.get(String(run.agent_id))]),
    );
    const page = pageOfItems(
      matched.map((run): Json => {
        const viewer = runViewerOf(state, run);
        return {
          ...runSummaryOf(run),
          executed_by_me: viewer.executed_by_me,
          executor_display_name: viewer.executor_display_name,
          can_decide: viewer.can_decide,
        };
      }),
      pageOf(query)
    );
    // system_admin には実行した利用者の表示名（「利用者」の列。#1889）を返す。持ち主での絞り込みは backend のテストで確かめる。
    const creatorDisplayNames = state.auth.currentUser?.is_system_admin
      ? Object.fromEntries(
          page.items
            .map((run) => String(run.created_by_user_uuid ?? ""))
            .filter((uuid) => uuid && state.creatorDisplayNames[uuid])
            .map((uuid) => [uuid, state.creatorDisplayNames[uuid]])
        )
      : {};
    // 他の人が実行した Run を見られるか（「実行者」の列と絞り込み。#1955）。
    const hasOtherExecutors =
      Boolean(state.auth.currentUser?.is_system_admin) ||
      visible.some((run) => !isViewer(state, run.created_by_user_uuid ?? "local"));
    // 「全 N 件」は種類の範囲で数える（品質評価を含まない種類のときは品質評価の Run を除く。#1983）。
    const evaluationTotal = visible.filter((run) => runSummaryOf(run).source === "evaluation").length;
    return {
      ...page,
      unfiltered_total: !kind || kind === "evaluation" ? visible.length : visible.length - evaluationTotal,
      evaluation_total: evaluationTotal,
      creator_display_names: creatorDisplayNames,
      has_other_executors: hasOtherExecutors,
    };
  }
  // 承認の一覧（#1897）。状態（pending / decided）・検索語で絞り、limit / offset で切り出す。
  if (method === "GET" && at("approvals")) {
    // 承認は Run を実行した人と system_admin だけ（会話の持ち主でも、system_admin が実行した Run の承認は出さない。#1928）。
    const visible = state.runs
      .filter((run) => agentAllowed(state, run.agent_id) && runViewerOf(state, run).can_decide)
      .flatMap((run) => ((run.approvals as Json[] | undefined) ?? []).map((approval) => approvalSummaryOf(run, approval)));
    const status = query.get("status");
    const terms = searchTerms(query.get("q"));
    const matched = visible.filter(
      (row) =>
        (!status || (row.status === "pending") === (status === "pending")) &&
        termsMatch(terms, [row.id, row.tool_name, row.run_goal, row.run_id]),
    );
    return { ...pageOfItems(matched, pageOf(query)), unfiltered_total: visible.length };
  }
  // 承認の詳細（承認と、その実行の要約）。範囲外・無い承認は 404（#1897）。
  if (method === "GET" && at("approvals", "*")) {
    for (const run of state.runs) {
      const approval = ((run.approvals as Json[] | undefined) ?? []).find((candidate) => candidate.id === second);
      if (approval && agentAllowed(state, run.agent_id)) {
        return { approval, run: runSummaryOf(run), user_display_names: state.userDisplayNames };
      }
    }
    throw new HttpError(404, "承認の依頼が見つかりません。");
  }
  // チャット（#768）。mock の Run はすぐ完了し、質問を引いた回答の成果物を持つ（実行中・承認待ちは spec が state を書き換える）。
  if (method === "POST" && at("runs")) {
    // backend の create_run と同じ確認（対象範囲・業務 Agent の有無・移行・無効・下書き・公開）。
    const agentId = String(body.agent_id ?? "default");
    requireAgentAccess(state, agentId);
    const agent = state.agents.find((candidate) => candidate.id === agentId);
    if (!agent) throw new HttpError(404, "業務 Agent が見つかりません。");
    if (agent.migration_required) {
      throw new HttpError(409, "Skill を選択して Agent の移行を完了してください。", "agent_migration_required");
    }
    if (!agent.enabled) throw new HttpError(409, "無効な業務 Agent は実行できません。", "agent_disabled");
    const currentUser = state.auth.currentUser;
    if (body.draft && !managesAgent(state, agent)) {
      throw new HttpError(403, "下書きで実行できるのは Agent 管理の権限がある利用者だけです。");
    }
    if (!body.draft && (agent.published_version ?? null) === null) {
      throw new HttpError(
        409,
        "公開していない業務 Agent は実行できません。公開するか、下書きで実行してください。",
        "agent_unpublished"
      );
    }
    const threadId = typeof body.thread_id === "string" ? body.thread_id : null;
    const threadRuns = threadId ? state.runs.filter((run) => run.thread_id === threadId) : [];
    if (threadId && threadRuns.length === 0) {
      throw new HttpError(404, "会話が見つかりません。");
    }
    // 会話の持ち主（mock の "local" は本人とみなす）。続けられるのは持ち主だけ。他の利用者の会話は 404、
    // system_admin は見えるので 403（#3244）。
    const threadOwner = String(threadRuns[0]?.thread_owner_user_uuid ?? threadRuns[0]?.created_by_user_uuid ?? "local");
    const ownThread = ["", "local", currentUser?.user_uuid].includes(threadOwner);
    if (threadId && !ownThread) {
      if (currentUser?.is_system_admin) throw new HttpError(403, OTHERS_CONTENT_FORBIDDEN_MESSAGE);
      throw new HttpError(404, "会話が見つかりません。");
    }
    // 会話は始めた Run の種類（下書き / 公開中の版）に固定する（backend と同じ。#2515）。
    if (threadId && isDraftRun(threadRuns[0]) !== Boolean(body.draft)) {
      throw new HttpError(
        409,
        "下書きで試した会話は公開中の版で、公開中の版の会話は下書きで続けられません。新しい会話で質問してください。",
        "thread_kind_mismatch"
      );
    }
    const id = `run-chat-${state.runs.length + 1}`;
    const run: Json = {
      id,
      goal: String(body.goal ?? ""),
      agent_id: agentId,
      runtime_id: "builtin",
      status: "completed",
      steps: [],
      events: [],
      approvals: [],
      artifacts: [
        { id: `${id}-answer`, name: "回答", kind: "answer", created_at: MOCK_NOW, content: { text: `「${String(body.goal ?? "")}」への回答です。` } },
        ...(state.runOutputContent
          ? [{ id: `${id}-output`, name: "出力の項目", kind: "structured_output", created_at: MOCK_NOW, content: clone(state.runOutputContent) }]
          : []),
      ],
      pending_tool_calls: [],
      // 確認の取り消しの操作（#1441・#1445）は、backend と同じく Run の記録に残す。
      metadata: {
        ...(body.clarification_cancel === "nl2sql" || body.clarification_cancel === "rag"
          ? { clarification_cancel: [{ kind: body.clarification_cancel, trigger: "button" }] }
          : {}),
        // 使った版（backend の metadata.agent_version。下書きは "draft"）。
        ...(body.draft ? { agent_version: "draft" } : {}),
      },
      created_by_user_uuid: "local",
      thread_id: threadId ?? `thread_${String(state.runs.length + 1).padStart(32, "0")}`,
      thread_owner_user_uuid: threadId ? threadOwner : "local",
      // 質問に添付したファイル（#2517）。backend と同じく名前と種類を Run に残す（添付の API は spec が mock する）。
      attachments: Array.isArray(body.attachment_ids)
        ? (body.attachment_ids as string[]).map((attachmentId) => ({
            attachment_id: attachmentId,
            file_name: `${attachmentId}.csv`,
            kind: "csv",
            size_bytes: 24,
          }))
        : [],
      created_at: MOCK_NOW,
      updated_at: MOCK_NOW,
    };
    state.runs.push(run);
    return run;
  }
  // フィードバックの集計と一覧（#774）。`state.feedbackReport` があればそれを返し、無ければ Run の評価から作る。
  if (method === "GET" && at("feedback")) {
    const days = Number(query.get("days") ?? 30);
    if (!REPORT_PERIOD_DAYS.includes(days)) throw new HttpError(422, REPORT_PERIOD_DETAIL);
    const page = pageOf(query);
    if (state.feedbackReport) {
      const report = state.feedbackReport;
      const items = (report.items as Json[] | undefined) ?? [];
      return {
        source: "memory",
        ...report,
        ...pageOfItems(items, page, (report.total as number | undefined) ?? items.length),
      };
    }
    return feedbackReportFromRuns(
      state.runs,
      days,
      { agentId: query.get("agent_id"), rating: query.get("rating"), reason: query.get("reason") },
      page
    );
  }
  // 確かめるべき実行（#1822）。`state.signalReport` があればそれを返し（一覧は種類で絞る）、無ければ Run から作る。
  if (method === "GET" && at("feedback", "signals")) {
    const days = Number(query.get("days") ?? 30);
    if (!REPORT_PERIOD_DAYS.includes(days)) throw new HttpError(422, REPORT_PERIOD_DETAIL);
    const page = pageOf(query);
    const kind = query.get("kind");
    if (state.signalReport) {
      const report = state.signalReport;
      const all = (report.items as Json[] | undefined) ?? [];
      const items = kind ? all.filter((item) => (item.signals as string[]).includes(kind)) : all;
      const total = kind ? items.length : ((report.total as number | undefined) ?? items.length);
      return { source: "memory", ...report, kind, ...pageOfItems(items, page, total) };
    }
    return signalReportFromRuns(state.runs, days, { agentId: query.get("agent_id"), kind }, page);
  }
  if (method === "GET" && at("threads")) {
    const agentId = query.get("agent_id");
    // `draft=true` は編集画面で下書きを試した会話（Agent 管理だけ）、既定は公開中の版の会話（#2515）。
    const draft = query.get("draft") === "true";
    const viewer = state.auth.currentUser;
    if (draft && viewer && !isAgentAdmin(state) && !viewer.permissions.includes("agent.agents.build")) {
      throw new HttpError(403, "下書きの会話を見られるのは Agent 管理の権限がある利用者だけです。");
    }
    const grouped = new Map<string, Json[]>();
    for (const run of state.runs) {
      if (typeof run.thread_id !== "string") continue;
      if (agentId && run.agent_id !== agentId) continue;
      // 「確認をやめる」で外した Run（#1839）は会話に出さない（backend と同じ）。
      if (isWithdrawn(run)) continue;
      if (!agentAllowed(state, run.agent_id)) continue;
      grouped.set(run.thread_id, [...(grouped.get(run.thread_id) ?? []), run]);
    }
    // 会話の履歴のページング（limit / offset と全件数。#1265）。
    const limit = Number(query.get("limit") ?? 25);
    const offset = Number(query.get("offset") ?? 0);
    const threads = [...grouped.entries()]
      .filter(([, runs]) => isDraftRun(runs[0]) === draft)
      .map(([threadId, runs]) => ({
      thread_id: threadId,
      agent_id: runs[0].agent_id,
      // 利用者が変えた名前（#1817。backend は最初の Run の metadata.thread_title）。
      title: String((runs[0].metadata as Json | undefined)?.thread_title ?? runs[0].goal).split("\n")[0],
      run_count: runs.length,
      last_status: runs[runs.length - 1].status,
      created_at: runs[0].created_at,
      updated_at: runs[runs.length - 1].updated_at,
      // 見ている利用者の会話か（mock の "local" は本人とみなす）と、system_admin の利用者の表示名（#1889）。
      // 持ち主の無い Run を seed した spec は本人の会話のまま扱う。
      owned: [undefined, null, "", "local", state.auth.currentUser?.user_uuid].includes(
        runs[0].created_by_user_uuid as string | null | undefined
      ),
      ...(state.auth.currentUser?.is_system_admin
        ? { actor_display_name: state.creatorDisplayNames[String(runs[0].created_by_user_uuid ?? "")] ?? "" }
        : {}),
    })).reverse();
    return {
      ...pageOfItems(threads, { offset, limit }),
      ...(state.chatHistoryDays == null ? {} : { history_days: state.chatHistoryDays }),
    };
  }
  // 会話の名前の変更・削除（#1817）。削除は Run を残し、会話からの参照（thread_id）だけを外す。
  if ((method === "PATCH" || method === "DELETE") && at("threads", "*")) {
    const runs = state.runs.filter((run) => run.thread_id === second);
    if (runs.length === 0) throw new HttpError(404, "会話が見つかりません。");
    if (method === "PATCH") {
      // 名前の変更は会話の持ち主だけ（system_admin でも 403。#3244）。削除は運用の操作としてできる。
      if (!isViewer(state, runs[0].thread_owner_user_uuid ?? runs[0].created_by_user_uuid)) {
        throw new HttpError(403, OTHERS_CONTENT_FORBIDDEN_MESSAGE);
      }
      const title = String(body.title ?? "").trim();
      if (!title || title.length > 80) throw new HttpError(422, "会話名を入力してください。");
      runs[0].metadata = { ...((runs[0].metadata as Json | undefined) ?? {}), thread_title: title };
      return {
        thread_id: second,
        agent_id: runs[0].agent_id,
        title,
        run_count: runs.length,
        last_status: runs[runs.length - 1].status,
        created_at: runs[0].created_at,
        updated_at: runs[runs.length - 1].updated_at,
      };
    }
    if (runs.some((run) => !["completed", "failed", "cancelled"].includes(String(run.status)))) {
      throw new HttpError(
        409,
        "回答の作成中・承認待ちの会話は削除できません。停止するか、回答が終わってから削除してください。"
      );
    }
    for (const run of runs) {
      run.metadata = { ...((run.metadata as Json | undefined) ?? {}), deleted_thread_id: second };
      run.thread_id = null;
    }
    return null;
  }
  if (method === "GET" && at("threads", "*")) {
    const runs = state.runs.filter((run) => run.thread_id === second);
    if (runs.length === 0) throw new HttpError(404, "会話が見つかりません。");
    // 見ている利用者の会話か（mock の "local" は本人とみなす）と、system_admin の利用者の表示名（#1889）。
    const owner = String(runs[0].thread_owner_user_uuid ?? runs[0].created_by_user_uuid ?? "");
    const viewer = state.auth.currentUser;
    const visible = runs.filter((run) => !isWithdrawn(run));
    return {
      thread_id: second,
      agent_id: runs[0].agent_id,
      // 各 Run に見ている利用者から見た事実（実行者・待ちの操作の可否。#1954）を付ける。
      runs: visible.map((run) => ({ ...run, ...runViewerOf(state, run) })),
      owned: ["", "local", viewer?.user_uuid].includes(owner),
      actor_display_name: viewer?.is_system_admin ? (state.creatorDisplayNames[owner] ?? "") : "",
      // 持ち主以外（system_admin）が送った往復の、送った人の表示名（Run の ID → 表示名。#1928）。
      sender_display_names: Object.fromEntries(
        visible
          .filter((run) => String(run.created_by_user_uuid ?? "") !== owner)
          .map((run) => [
            String(run.id),
            String(run.created_by_user_uuid) === viewer?.user_uuid
              ? viewer.display_name
              : (state.creatorDisplayNames[String(run.created_by_user_uuid ?? "")] ?? ""),
          ])
      ),
    };
  }
  if (head === "runs" && second) {
    const run = findOr404(state.runs, "id", second, "run");
    // 実行の詳細・操作は利用者の対象範囲の業務 Agent だけ（backend の `_require_agent_access`。
    // 評価ケースの下書きは範囲外を 404 にする）。
    if (at("runs", "*", "evaluation-case") && !agentAllowed(state, run.agent_id)) {
      throw new HttpError(404, "実行が見つかりません。");
    }
    requireAgentAccess(state, run.agent_id);
    // 実行の詳細（実行履歴の詳細・行の判断の前の読み直しが読む。#1897）。
    if (method === "GET" && at("runs", "*")) return { ...run, ...runViewerOf(state, run) };
    if (method === "GET" && at("runs", "*", "audit")) {
      // 実行の監査（backend の `GET /api/runs/{id}/audit`）。監査の記録のうち、この実行のものを返す（#2403）。
      return {
        run_id: run.id,
        goal: run.goal,
        status: run.status,
        records: state.auditRecords.filter((record) => record.run_id === run.id),
      };
    }
    if (method === "GET" && at("runs", "*", "artifacts")) return { artifacts: [] };
    // Run の処理の経過（共通の span の表の snapshot。backend の `GET /api/runs/{id}/trace`。#3226 の案 A）。
    if (method === "GET" && at("runs", "*", "trace")) return traceSnapshot(run);
    // RAG の根拠の原文のプレビュー（#2389）。spec は `http://rag.e2e.test/**` を page.route で応答する。
    if (method === "GET" && at("runs", "*", "source-preview")) {
      if (!runEvidenceOf(run, query)) throw new HttpError(404, "この実行の根拠に、その資料はありません。");
      const chunkId = query.get("chunk_id") ?? "";
      const override = (state.sourcePreviews[chunkId] ?? {}) as Json;
      const error = override.error as { status: number; message: string } | undefined;
      if (error) throw new HttpError(error.status, error.message);
      state.sourcePreviewsIssued += 1;
      const base = `http://rag.e2e.test/api/source-previews/p1.${encodeURIComponent(chunkId)}.${state.sourcePreviewsIssued}.sig`;
      return {
        kind: "pdf",
        file_name: "合成の文書.pdf",
        pages: [
          { page_number: 1, width: 600, height: 800 },
          { page_number: 2, width: 600, height: 800 },
        ],
        page_url_template: `${base}/pages/{page}`,
        content_url: `${base}/content`,
        highlight_metadata: {
          page_start: 1,
          page_width: 600,
          page_height: 800,
          bbox: "[60, 80, 300, 200]",
          bbox_unit: "absolute",
        },
        expires_at: "2026-06-28T00:05:00+00:00",
        expires_in_seconds: 300,
        ...override,
      };
    }
    // Run から評価ケースの下書きを作る（#810）。
    if (method === "GET" && at("runs", "*", "evaluation-case")) {
      const agent = state.agents.find((item) => item.id === run.agent_id);
      const review = run.admin_review as Json | null | undefined;
      const tools = ((run.steps as Json[] | undefined) ?? [])
        .map((step) => (step.tool_call as Json | null | undefined)?.name)
        .filter((name): name is string => typeof name === "string");
      return {
        agent_id: run.agent_id,
        agent_name: agent?.name ?? run.agent_id,
        source_run_id: run.id,
        question: String(run.goal ?? "").trim(),
        expected: String(review?.comment ?? "").trim(),
        expected_tools: [...new Set(tools)],
        existing_set_ids: state.evaluationSets
          .filter(
            (item) =>
              item.agent_id === run.agent_id &&
              (item.cases as Json[]).some(
                (candidate) => normalizedQuestion(candidate.question) === normalizedQuestion(run.goal)
              )
          )
          .map((item) => item.id),
      };
    }
    if (method === "POST" && at("runs", "*", "cancel")) {
      // 処理の経過の終端は Run の状態から作る（backend と同じく Run の状態が正本。#3226）。
      run.status = "cancelled";
      return run;
    }
    // 回答前の確認（#1839）。backend の `decide_confirmation` と同じ結果の形。mock の Run は答えですぐ完了する。
    if (method === "POST" && at("runs", "*", "confirmations", "*")) {
      // 答え・スキップは Run の持ち主だけ（system_admin でも 403。やめるは運用の操作。#3244）。
      if (body.action !== "cancel" && !ownsRunContent(state, run)) throw new HttpError(403, OTHERS_CONTENT_FORBIDDEN_MESSAGE);
      return decideConfirmation(state, run, segments[3], body);
    }
    if (method === "POST" && at("runs", "*", "resume")) {
      // 承認がすべて決まり、保存した SDK の状態から再開を待つ組み込み Runtime の Run だけ（`builtin_resume_pending`）。
      const resumable =
        run.runtime_id === "builtin" &&
        run.status === "queued" &&
        typeof (run.metadata as Json | undefined)?._builtin_sdk_state === "string";
      if (!resumable) {
        throw new HttpError(409, "承認待ちが残っているか、再開できる状態ではありません。", "run_not_resumable");
      }
      return run;
    }
    // チャットの回答への評価（#774）。役に立たなかったときは理由が必須。役に立った評価は理由・コメントを残さない。
    // 管理者の評価（#774）。本人の評価とは別に残す。
    if (method === "PUT" && at("runs", "*", "admin-review")) {
      if (run.status !== "completed") throw new HttpError(409, "回答が出た Run にだけ評価を付けられます。");
      const helpful = body.rating === "helpful";
      if (!helpful && !body.reason) throw new HttpError(422, "役に立たなかった理由を選んでください。");
      run.admin_review = {
        rating: body.rating,
        reason: helpful ? null : body.reason,
        comment: helpful ? "" : String(body.comment ?? "").trim(),
        user_uuid: "local",
        updated_at: MOCK_NOW,
      };
      return run;
    }
    if (method === "PUT" && at("runs", "*", "feedback")) {
      // 評価は Run の持ち主だけ（system_admin でも 403。#3244）。
      if (!ownsRunContent(state, run)) throw new HttpError(403, OTHERS_CONTENT_FORBIDDEN_MESSAGE);
      if (run.status !== "completed") throw new HttpError(409, "回答が出た Run にだけフィードバックを付けられます。");
      const helpful = body.rating === "helpful";
      if (!helpful && !body.reason) throw new HttpError(422, "役に立たなかった理由を選んでください。");
      run.feedback = {
        rating: body.rating,
        reason: helpful ? null : body.reason,
        comment: helpful ? "" : String(body.comment ?? "").trim(),
        user_uuid: "local",
        updated_at: MOCK_NOW,
      };
      return run;
    }
    if (method === "POST" && at("runs", "*", "replay")) {
      // backend の replay_run と同じく、同じ業務 Agent・ゴールの新しい Run（待ち）を作る。元の Run の成果物・
      // 経過・承認・評価・会話は引き継がない。
      const replayId = `${String(run.id)}-replay-${state.runs.length}`;
      const replay: Json = {
        id: replayId,
        goal: run.goal,
        agent_id: run.agent_id,
        runtime_id: "builtin",
        status: "queued",
        steps: [],
        events: [],
        approvals: [],
        artifacts: [],
        pending_tool_calls: [],
        metadata: { replayed_from_run_id: run.id },
        created_by_user_uuid: state.auth.currentUser?.user_uuid ?? null,
        thread_id: `thread_${String(state.runs.length + 1).padStart(32, "0")}`,
        created_at: MOCK_NOW,
        updated_at: MOCK_NOW,
      };
      state.runs.unshift(replay);
      return replay;
    }
  }
  if (method === "POST" && at("approvals", "*", "decision")) {
    for (const run of state.runs) {
      const approval = ((run.approvals as Json[] | undefined) ?? []).find((candidate) => candidate.id === second);
      if (approval) {
        const decidedBy = state.auth.currentUser?.login_user_id ?? "local";
        // backend の decide_approval と同じく、終了した Run の保留中の承認は取り消し、判断済みの承認
        // （ほかの操作者が先に判断した）は変えずに 200 で返す。
        if (["completed", "failed", "cancelled"].includes(String(run.status))) {
          if (approval.status === "pending") {
            Object.assign(approval, { status: "cancelled", decided_by: decidedBy, decided_at: MOCK_NOW });
          }
          return run;
        }
        if (approval.status !== "pending") return run;
        if (body.approved && body.always_allow) {
          // 「今後は自動で実行」は Agent 管理だけ（backend と同じ 403）。ツール権限の allow に足す（#1832）。
          const permissions = state.auth.currentUser?.permissions as string[] | undefined;
          if (permissions && !permissions.includes("agent.admin")) {
            throw new HttpError(403, "この機能を利用する権限がありません。");
          }
          const toolName = String((approval.tool_call as Json).name);
          const policy = state.toolPolicy as { allow: string[]; ask: string[] };
          policy.ask = policy.ask.filter((name) => name !== toolName);
          if (!policy.allow.includes(toolName)) policy.allow = [...policy.allow, toolName].sort();
        }
        approval.status = body.approved ? "approved" : "rejected";
        approval.decided_by = decidedBy;
        approval.decided_at = MOCK_NOW;
        // 組み込み Runtime の Run は、承認待ちが残らなければ再開を待つ状態（queued）にする。
        const pending = ((run.approvals as Json[] | undefined) ?? []).some((item) => item.status === "pending");
        if (run.runtime_id === "builtin" && !pending) {
          run.status = "queued";
          run.updated_at = MOCK_NOW;
        }
        return run;
      }
    }
    throw new HttpError(404, `approval not found: ${second}`);
  }
  if (method === "GET" && at("agent-templates")) return { templates: state.agentTemplates };
  // データの範囲の候補（#1378）。backend と同じく範囲を持てる接続だけ。
  if (method === "GET" && at("agent-data-scopes", "*", "candidates")) {
    const connection = String(second);
    if (!(connection in DATA_SCOPE_LABELS)) throw new HttpError(404, "データの範囲を設定できない接続です。");
    const failure = state.dataScopeCandidateErrors[connection];
    if (failure) throw new HttpError(failure.status, failure.message, failure.status === 403 ? "data_scope_candidates_forbidden" : undefined);
    return {
      connection,
      label: DATA_SCOPE_LABELS[connection],
      profiles: state.dataScopeCandidates[connection] ?? [],
    };
  }
  // --- 品質評価（#776） ---
  // --- 評価セット（#776） ---
  if (method === "GET" && at("evaluation-sets")) {
    const agentId = query.get("agent_id");
    return {
      sets: state.evaluationSets
        .filter((item) => !agentId || item.agent_id === agentId)
        .map((item) => {
          const latest = state.evaluations.find((job) => job.set_id === item.id);
          return {
            id: item.id,
            agent_id: item.agent_id,
            name: item.name,
            description: item.description,
            case_count: (item.cases as Json[]).length,
            updated_at: item.updated_at,
            last_job_id: latest?.id ?? null,
            last_job_status: latest?.status ?? null,
            last_pass_rate: latest ? ((latest.summary as Json).pass_rate ?? null) : null,
          };
        }),
    };
  }
  if (method === "POST" && at("evaluation-sets", "parse-xlsx")) return { cases: state.parsedCases };
  // 業種テンプレートの評価ケースで評価セットを作る（#810）。
  if (method === "POST" && at("evaluation-sets", "from-template")) {
    const agent = findOr404(state.agents, "id", String(body.agent_id), "agent");
    const template = state.agentTemplates.find((item) => item.id === agent.template_id);
    if (!template) {
      throw new HttpError(409, "この業務 Agent はテンプレートから作っていないため、評価ケースがありません。");
    }
    const item: Json = {
      ...normalizedSet({
        agent_id: agent.id,
        name: `${String(template.name)}（テンプレート）`,
        description: "業種テンプレートの評価ケースから作りました。",
        cases: template.evaluation_cases,
      }),
      id: `evset-${state.evaluationSets.length + 1}`,
      created_by_user_uuid: "local",
      created_at: MOCK_NOW,
      updated_at: MOCK_NOW,
    };
    state.evaluationSets.unshift(item);
    return item;
  }
  if (method === "POST" && at("evaluation-sets")) {
    const item: Json = {
      ...normalizedSet(body),
      id: `evset-${state.evaluationSets.length + 1}`,
      created_by_user_uuid: "local",
      created_at: MOCK_NOW,
      updated_at: MOCK_NOW,
    };
    state.evaluationSets.unshift(item);
    return item;
  }
  if (head === "evaluation-sets" && second) {
    const item = findOr404(state.evaluationSets, "id", second, "evaluation set");
    if (method === "GET" && at("evaluation-sets", "*")) return item;
    // ケースを 1 件足す（同じ質問・50 件の上限は 409。#810）。
    if (method === "POST" && at("evaluation-sets", "*", "cases")) {
      const cases = item.cases as Json[];
      if (cases.some((candidate) => normalizedQuestion(candidate.question) === normalizedQuestion(body.question))) {
        throw new HttpError(409, "同じ質問の評価ケースが、この評価セットに既にあります。");
      }
      if (cases.length >= 50) {
        throw new HttpError(409, "評価セットのケースは 50 件までです。別の評価セットに追加してください。");
      }
      const used = new Set(cases.map((candidate) => candidate.id));
      let number = cases.length + 1;
      while (used.has(`case-${number}`)) number += 1;
      cases.push({
        id: `case-${number}`,
        question: String(body.question).trim(),
        expected: String(body.expected).trim(),
        expected_tools: body.expected_tools ?? [],
        source_run_id: body.source_run_id ?? null,
      });
      item.updated_at = MOCK_NOW;
      return item;
    }
    if (method === "PUT" && at("evaluation-sets", "*")) {
      if (body.agent_id !== item.agent_id) throw new HttpError(422, "評価セットの業務 Agent は変えられません。");
      Object.assign(item, normalizedSet(body), { updated_at: MOCK_NOW });
      return item;
    }
    if (method === "DELETE" && at("evaluation-sets", "*")) {
      if (state.evaluations.some((job) => job.set_id === item.id && (job.status === "running" || job.status === "queued"))) {
        throw new HttpError(409, "この評価セットで評価を実行しています。終わってから削除してください。");
      }
      state.evaluationSets = state.evaluationSets.filter((candidate) => candidate !== item);
      return null;
    }
  }
  // --- 品質評価（#776） ---
  if (method === "POST" && at("evaluations")) {
    // backend と同じく、待っている（queued）評価も実行中に数える。
    if (state.evaluations.some((job) => job.status === "running" || job.status === "queued")) {
      throw new HttpError(409, "ほかの評価を実行しています。終わってから始めてください。");
    }
    const evaluationSet = findOr404(state.evaluationSets, "id", String(body.set_id), "evaluation set");
    // 評価する業務 Agent（省略すると評価セットの業務 Agent。#1845）。
    const evaluatedAgentId = body.agent_id ?? evaluationSet.agent_id;
    // 無効・移行が要る業務 Agent は評価しない（backend の create_evaluation）。
    const evaluatedAgent = state.agents.find((agent) => agent.id === evaluatedAgentId);
    if (!evaluatedAgent) throw new HttpError(404, "業務 Agent が見つかりません。");
    if (!evaluatedAgent.enabled || evaluatedAgent.migration_required) {
      throw new HttpError(409, "この業務 Agent は実行できない状態です。");
    }
    // 下書きの評価は Agent 管理だけ（backend と同じ。#2522）。
    const evaluator = state.auth.currentUser;
    const canRunDraft = !evaluator || evaluator.is_system_admin || evaluator.permissions.includes("agent.admin");
    // 省略時の既定も、Agent 管理でなければ公開中の版。
    const evaluatedVersion = evaluationVersion(state, evaluatedAgentId, body.agent_version ?? (canRunDraft ? undefined : "published"));
    if (!canRunDraft && evaluatedVersion === "draft") {
      throw new HttpError(403, "下書きを評価できるのは Agent 管理の権限がある利用者だけです。");
    }
    const cases = evaluationSet.cases as Json[];
    const job: Json = {
      id: `eval-${state.evaluations.length + 1}`,
      agent_id: evaluatedAgentId,
      agent_name: evaluatedAgentId === "default" ? DEFAULT_AGENT_NAME : String(evaluatedAgent.name ?? evaluatedAgentId),
      set_id: evaluationSet.id,
      set_name: evaluationSet.name,
      agent_version: evaluatedVersion,
      status: "running",
      created_by_user_uuid: "local",
      results: cases.map((item) => ({
        case: item,
        status: "pending",
        run_id: null,
        answer: "",
        judgement: null,
        tool_calls: [],
        tool_selection_correct: null,
        error: null,
        duration_ms: null,
      })),
      error: null,
      summary: evaluationSummary([]),
      created_at: MOCK_NOW,
      started_at: MOCK_NOW,
      finished_at: null,
      previous_job_id: null,
      previous_summary: null,
      previous_agent_version: null,
      previous_created_at: null,
      _polls: 0,
    };
    job.summary = evaluationSummary(job.results as Json[]);
    const previous = state.evaluations.find(
      (item) => item.set_id === evaluationSet.id && item.agent_id === evaluatedAgentId && item.status === "completed"
    );
    if (previous) {
      job.previous_job_id = previous.id;
      job.previous_summary = previous.summary;
      job.previous_agent_version = previous.agent_version ?? null;
      job.previous_created_at = previous.created_at;
    }
    state.evaluations.unshift(job);
    return publicJob(job);
  }
  if (method === "GET" && at("evaluations")) {
    // 評価の履歴はサーバー側のページング（#794）。
    const page = pageOf(query);
    return {
      ...pageOfItems(state.evaluations, page),
      items: state.evaluations.slice(page.offset, page.offset + page.limit).map((job) => ({
        id: job.id,
        agent_id: job.agent_id,
        agent_name: job.agent_name,
        set_id: job.set_id ?? "",
        set_name: job.set_name ?? "",
        agent_version: job.agent_version ?? null,
        status: job.status,
        summary: job.summary,
        created_at: job.created_at,
        finished_at: job.finished_at,
      })),
    };
  }
  if (method === "GET" && at("evaluations", "compare")) {
    // 2 つの評価を同じ質問のケースどうしで並べる（backend の compare_jobs と同じ決め方。#1845）。
    const baseId = query.get("base_job_id");
    const targetId = query.get("target_job_id");
    if (baseId === targetId) throw new HttpError(422, "違う評価を 2 つ選んでください。");
    const base = findOr404(state.evaluations, "id", String(baseId), "evaluation");
    const target = findOr404(state.evaluations, "id", String(targetId), "evaluation");
    return compareEvaluationJobs(base, target);
  }
  if (head === "evaluations" && second) {
    const job = findOr404(state.evaluations, "id", second, "evaluation");
    if (method === "GET" && at("evaluations", "*")) {
      if (job.status === "running") {
        job._polls = Number(job._polls) + 1;
        if (Number(job._polls) > state.evaluationPollsUntilDone) finishEvaluation(job);
      }
      return publicJob(job);
    }
    if (method === "POST" && at("evaluations", "*", "cancel")) {
      // 終わった評価は変えない（backend の EvaluationStore.cancel は実行中・待ちの評価だけを取り消す）。
      if (job.status !== "running" && job.status !== "queued") return publicJob(job);
      job.status = "cancelled";
      for (const result of job.results as Json[]) {
        if (result.status === "pending" || result.status === "running") result.status = "cancelled";
      }
      job.summary = evaluationSummary(job.results as Json[]);
      return publicJob(job);
    }
    if (method === "DELETE" && at("evaluations", "*")) {
      if (job.status === "running" || job.status === "queued") {
        throw new HttpError(409, "実行中の評価は削除できません。取り消してから削除してください。");
      }
      state.evaluations = state.evaluations.filter((item) => item !== job);
      return null;
    }
  }
  if (method === "GET" && at("usage")) {
    const days = Number(query.get("days") ?? 30);
    if (!REPORT_PERIOD_DAYS.includes(days)) throw new HttpError(422, REPORT_PERIOD_DETAIL);
    return state.usageReports[String(days)] ?? emptyUsageReport(days, query.get("timezone") ?? "Asia/Tokyo");
  }
  if (method === "GET" && at("audit", "tool-calls")) {
    const offset = Number(query.get("offset") ?? 0);
    const limit = Number(query.get("limit") ?? 25);
    // backend の `_audit_record_matches` と同じ絞り込み（完全一致）。
    const exact: [string, string][] = [
      ["run_id", "run_id"],
      ["tool_name", "tool_name"],
      ["status", "status"],
      ["approval_status", "approval_status"],
      ["error_code", "error_code"],
    ];
    const warnings = query.get("has_guardrail_warnings");
    const matched = state.auditRecords.filter(
      (record) =>
        exact.every(([param, field]) => !query.has(param) || record[field] === query.get(param)) &&
        (warnings === null ||
          ((record.guardrail_warnings as unknown[] | undefined) ?? []).length > 0 === (warnings === "true"))
    );
    return {
      ...pageOfItems(matched, { offset, limit }),
      filters: Object.fromEntries([...query.entries()].filter(([key]) => key !== "offset" && key !== "limit")),
      // 絞り込みに依らない、記録されたツール名（#983）。
      tool_names: [...new Set(state.auditRecords.map((record) => String(record.tool_name)))].sort(),
    };
  }
  if (method === "GET" && at("tools")) return { tools: state.tools };

  // --- 業務 Agent ---
  if (head === "agents") {
    // 版に残す項目（#770）と、登録されていないスキルの検証（backend の `_validate_agent_skills`。#925）。
    const VERSIONED = [
      "name",
      "description",
      "instructions",
      "skill_ids",
      "model_id",
      "data_scopes",
      "clarification_policy",
      "clarification_max_rounds",
      "skill_policy",
      "output_fields",
    ] as const;
    // 範囲に新しく加えるプロファイルは、編集者が使える（候補にある）ものだけ（backend の `_verify_data_scopes`。#1378）。
    const requireUsableScopes = (requested: unknown, current: unknown) => {
      const scopes = (requested ?? {}) as Record<string, { profile_ids?: string[] }>;
      const before = (current ?? {}) as Record<string, { profile_ids?: string[] }>;
      for (const [connection, scope] of Object.entries(scopes)) {
        const known = new Set((state.dataScopeCandidates[connection] ?? []).map((item) => String(item.id)));
        const existing = new Set(before[connection]?.profile_ids ?? []);
        const missing = (scope.profile_ids ?? []).filter((id) => !existing.has(id) && !known.has(id));
        if (missing.length) {
          throw new HttpError(400, `プロファイル「${missing.join("、")}」は、あなたが使えないか見つかりません。候補から選んでください。`);
        }
      }
    };
    const requireKnownSkills = (skillIds: unknown) => {
      const known = new Set(state.skills.map((skill) => String(skill.id)));
      const unknown = [...new Set(((skillIds as string[] | undefined) ?? []).filter((id) => !known.has(id)))].sort();
      if (unknown.length) {
        throw new HttpError(400, `登録されていないスキルがあります: ${unknown.join(", ")}。スキルの選択から外してください。`);
      }
    };
    // 公開中の版と下書きの版の項目が違うか（backend の `has_unpublished_changes`）。
    const unpublishedChanges = (agent: Json) => {
      const published = ((agent.versions as Json[] | undefined) ?? []).find(
        (item) => item.version === agent.published_version
      );
      if (!published) return true;
      // データの範囲は、無い・空（{}）を同じとみなす（backend は空の範囲を {} で持つ）。
      // 回答前の確認（#1839）の項目の無い版は既定（確認してから回答する・上限 3 回）と同じ（backend の既定値）。
      const comparable = (field: string, value: unknown) =>
        (field === "data_scopes" && (!value || Object.keys(value as Json).length === 0)) ||
        (field === "output_fields" && (!value || (value as unknown[]).length === 0))
          ? null
          : field === "clarification_policy"
            ? (value ?? "confirm")
            : field === "clarification_max_rounds"
              ? (value ?? 0)
              : field === "skill_policy"
                ? (value ?? "pinned")
                : (value ?? null);
      return VERSIONED.some(
        (field) => JSON.stringify(comparable(field, agent[field])) !== JSON.stringify(comparable(field, published[field]))
      );
    };
    // 一覧は利用者の対象範囲の業務 Agent だけ（backend の list_agents）。
    if (method === "GET" && at("agents"))
      return {
        agents: state.agents.filter((agent) => agentAllowed(state, agent.id)),
        share_approvals: state.agents.filter((agent) => {
          const sharing = agent.share_request as Json | null | undefined;
          return sharing?.status === "approved" && sharing.requested_by_user_uuid === state.auth.currentUser?.user_uuid && !agentAllowed(state, agent.id);
        }).map((agent) => {
          const sharing = agent.share_request as Json;
          return { agent_id: agent.id, name: sharing.approved_name, decided_at: sharing.decided_at };
        }),
        user_display_names: state.userDisplayNames,
        max_published_versions: MAX_PUBLISHED_VERSIONS,
        // 個人の業務 Agent の持ち主（user_uuid）→ 表示名（#2523）。
        owner_display_names: state.creatorDisplayNames,
      };
    // 説明から下書きを提案する（#2519。保存しない）。
    if (method === "POST" && at("agents", "draft-suggestion")) {
      const description = String(body.description ?? "").trim();
      if (!description) throw new HttpError(422, "業務の説明を入力してください。");
      return (
        state.draftSuggestion ?? {
          name: `${description.slice(0, 20)} の Agent`,
          description,
          instructions: "根拠の箇所を示して答える。",
          skill_ids: [],
          data_scopes: {},
          output_fields: [],
          evaluation_cases: [],
          notices: [],
        }
      );
    }
    if (method === "POST" && at("agents")) {
      // backend の create_agent と同じく、送られた ID は URL に置ける形だけを受け付ける（#1033）。
      const id = String(body.id ?? `agent-${state.agents.length + 1}`).trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(id)) {
        throw new HttpError(422, "業務 Agent の ID は英数字で始め、英数字・_・-・. の 100 文字以内にしてください。");
      }
      if (state.agents.some((agent) => agent.id === id)) {
        throw new HttpError(400, "同じ ID の業務 Agent があります。");
      }
      // backend の `_require_agent_name` と、知らない業種テンプレートの 400（#810 / #925）。
      if (!String(body.name ?? "").trim()) throw new HttpError(422, "業務 Agent の名前を入力してください。");
      if (body.template_id && !state.agentTemplates.some((template) => template.id === body.template_id)) {
        throw new HttpError(400, "業種テンプレートが見つかりません。");
      }
      requireKnownSkills(body.skill_ids);
      requireUsableScopes(body.data_scopes, null);
      const agent = {
        description: "",
        instructions: "",
        source: "runtime",
        created_at: MOCK_NOW,
        updated_at: MOCK_NOW,
        ...body,
        id,
        // 画面・API で作る Agent は下書きから始める（#770）。
        versioned: true,
        versions: [],
        published_version: null,
        unpublished_changes: true,
        // Agent 管理が作れば組織の業務 Agent、それ以外は作った人の個人の業務 Agent（#2523）。
        owner_user_uuid: isAgentAdmin(state) ? null : (state.auth.currentUser?.user_uuid ?? null),
        share_request: null,
      };
      state.agents.push(agent);
      return agent;
    }
    if (method === "PATCH" && at("agents", "*")) {
      const agent = findOr404(state.agents, "id", second, "agent");
      requireManagesAgent(state, agent);
      if (body.name !== undefined && body.name !== null && !String(body.name).trim()) {
        throw new HttpError(422, "業務 Agent の名前を入力してください。");
      }
      if (body.skill_ids !== undefined) requireKnownSkills(body.skill_ids);
      if (body.data_scopes !== undefined) requireUsableScopes(body.data_scopes, agent.data_scopes);
      // 変えられるのは AgentProfilePatch の項目だけ（版・由来などは送られても変えない）。
      const patchable = [
        "name",
        "description",
        "instructions",
        "skill_ids",
        "model_id",
        "data_scopes",
        "clarification_policy",
        "clarification_max_rounds",
        "skill_policy",
        "tool_names",
        "enabled",
      ];
      const patch = Object.fromEntries(
        Object.entries(body).filter(([key, value]) => patchable.includes(key) && value !== null && value !== undefined)
      );
      Object.assign(agent, patch, { updated_at: MOCK_NOW });
      // スキルを選び直すと移行が済む（backend の patch_agent）。
      if (Array.isArray(body.skill_ids)) agent.migration_required = false;
      // 有効の切り替えなど版に残さない項目だけの変更は「公開していない変更」にしない（backend と同じ）。
      // スキルの更新（#1855）も公開していない変更に数える。
      agent.draft_changed = unpublishedChanges(agent);
      agent.unpublished_changes = Boolean(agent.draft_changed) || Boolean((agent.skill_updates as string[] | undefined)?.length);
      return agent;
    }
    // 固定したスキルと今のスキルの差（#1867）。mock は変わったスキルごとに決まった差分を返す。
    if (method === "GET" && at("agents", "*", "skill-updates")) {
      const agent = findOr404(state.agents, "id", second, "agent");
      return {
        agent_id: agent.id,
        published_version: agent.published_version ?? null,
        items: ((agent.skill_updates as string[] | undefined) ?? []).map((id) => {
          const skill = state.skills.find((item) => item.id === id) ?? {};
          const snapshot = { name: skill.name ?? id, description: skill.description ?? "", enabled: true, tools: [], references: [] };
          return {
            skill_id: id,
            change: "changed",
            pinned: snapshot,
            current: snapshot,
            instructions_diff: [
              { kind: "hunk", text: "@@ -1,2 +1,2 @@" },
              { kind: "context", text: "資料を調べて答える。" },
              { kind: "removed", text: "前の手順で調べる。" },
              { kind: "added", text: "新しい手順で調べる。" },
            ],
            truncated: false,
            tools_added: ["rag:rag_retrieve_evidence"],
            tools_removed: [],
            references_added: [],
            references_removed: [],
            references_changed: [],
          };
        }),
      };
    }
    // 下書きを版として公開する・前の版に戻す（#770）。
    // 個人の業務 Agent の共有の申請・承認・却下（#2523）。
    if (method === "POST" && at("agents", "*", "share-request")) {
      const agent = findOr404(state.agents, "id", second, "agent");
      requireManagesAgent(state, agent);
      if (!agent.owner_user_uuid) throw new HttpError(409, "組織の業務 Agent は、共有の申請をせずにロールで共有します。");
      if ((agent.share_request as Json | null | undefined)?.status === "requested") {
        throw new HttpError(409, "共有を申請しています。結果を待ってください。");
      }
      if ((agent.published_version ?? null) === null) throw new HttpError(409, "公開してから共有を申請してください。");
      agent.share_request = { status: "requested", note: String(body.note ?? "").trim(), requested_at: MOCK_NOW, reason: "", decided_at: null };
      return agent;
    }
    if (method === "POST" && (at("agents", "*", "share-request", "approve") || at("agents", "*", "share-request", "reject"))) {
      const agent = findOr404(state.agents, "id", second, "agent");
      if ((agent.share_request as Json | null | undefined)?.status !== "requested") {
        throw new HttpError(409, "この業務 Agent は共有を申請していません。");
      }
      if (segments[3] === "approve") {
        agent.share_request = { status: "approved", requested_by_user_uuid: agent.owner_user_uuid,
          approved_name: agent.name, requested_at: (agent.share_request as Json).requested_at,
          decided_at: MOCK_NOW, note: "", reason: "" };
        agent.owner_user_uuid = null;
      } else {
        const reason = String(body.reason ?? "").trim();
        if (!reason) throw new HttpError(422, "却下の理由を入力してください。");
        agent.share_request = { ...(agent.share_request as Json), status: "rejected", reason, decided_at: MOCK_NOW };
      }
      return agent;
    }
    if (method === "POST" && at("agents", "*", "publish")) {
      const agent = findOr404(state.agents, "id", second, "agent");
      requireManagesAgent(state, agent);
      if (state.publishEvaluationRefusal) {
        throw new HttpError(409, state.publishEvaluationRefusal, "publish_evaluation_required");
      }
      requireKnownSkills(agent.skill_ids);
      const versions = (agent.versions as Json[] | undefined) ?? [];
      const version = Math.max(0, ...versions.map((item) => Number(item.version))) + 1;
      const snapshot: Json = { version, note: body.note ?? "", published_at: MOCK_NOW, published_by: "local" };
      for (const field of VERSIONED) {
        snapshot[field] = clone(agent[field] ?? (field === "skill_ids" || field === "output_fields" ? [] : field === "data_scopes" ? {} : ""));
      }
      // 使うスキルの今の中身を版に固定する（backend の publish_agent。#1855）。
      snapshot.skills = ((agent.skill_ids as string[] | undefined) ?? []).map((id) => {
        const skill = state.skills.find((item) => item.id === id) ?? {};
        return { id, name: skill.name ?? id, description: skill.description ?? "", content_hash: `hash-${id}` };
      });
      agent.versions = pruneVersions([...versions, snapshot], version);
      agent.published_version = version;
      agent.unpublished_changes = false;
      agent.draft_changed = false;
      agent.skill_updates = [];
      agent.skills_pinned = true;
      return agent;
    }
    if (method === "POST" && at("agents", "*", "versions", "*", "restore")) {
      const agent = findOr404(state.agents, "id", second, "agent");
      const target = ((agent.versions as Json[] | undefined) ?? []).find(
        (item) => String(item.version) === segments[3]
      );
      if (!target) throw new HttpError(404, "業務 Agent の版が見つかりません。");
      for (const field of VERSIONED) agent[field] = clone(target[field]);
      agent.published_version = target.version;
      agent.unpublished_changes = false;
      return agent;
    }
  }

  // --- スキルの更新の取り込み（#1867）。公開中の版の項目のまま、スキルを固定し直した版を作る ---
  if (method === "POST" && at("agent-skill-updates", "publish")) {
    const results = [...new Set((body.agent_ids as string[] | undefined) ?? [])].map((agentId) => {
      const agent = state.agents.find((item) => item.id === agentId);
      if (!agent) return { agent_id: agentId, name: "", status: "skipped", version: null, previous_version: null, reason: "業務 Agent が見つかりません。" };
      const versions = (agent.versions as Json[] | undefined) ?? [];
      const published = versions.find((item) => item.version === agent.published_version);
      if (!published || !((agent.skill_updates as string[] | undefined) ?? []).length) {
        return { agent_id: agentId, name: agent.name, status: "skipped", version: null, previous_version: null, reason: "取り込むスキルの更新がありません。" };
      }
      const previous = Number(published.version);
      const version = Math.max(0, ...versions.map((item) => Number(item.version))) + 1;
      agent.versions = pruneVersions(
        [
          ...versions,
          { ...clone(published), version, note: `スキルの更新を取り込む（v${previous} から）`, published_at: MOCK_NOW, published_by: "local" },
        ],
        version,
        previous,
      );
      agent.published_version = version;
      // 取り消し（rollback）で前の版に戻したときの更新の印（mock は前の値を覚えておく）。
      agent._skill_updates_before = clone(agent.skill_updates);
      agent.skill_updates = [];
      agent.unpublished_changes = Boolean(agent.draft_changed);
      return { agent_id: agentId, name: agent.name, status: "published", version, previous_version: previous, reason: null };
    });
    return { results };
  }
  if (method === "POST" && at("agent-skill-updates", "rollback")) {
    const changed = ((body.items as Json[] | undefined) ?? []).map((item) => {
      const agent = findOr404(state.agents, "id", String(item.agent_id), "agent");
      agent.published_version = item.version;
      agent.skill_updates = clone(agent._skill_updates_before ?? []);
      agent.unpublished_changes = Boolean(agent.draft_changed) || Boolean((agent.skill_updates as string[]).length);
      return agent;
    });
    return { agents: changed, user_display_names: state.userDisplayNames };
  }

  // --- Skill ---
  if (head === "skills") {
    if (method === "GET" && at("skills")) {
      return { skills: state.skills, metadata: { count: state.skills.length } };
    }
    if (method === "POST" && at("skills", "reload")) {
      return { skills: state.skills, metadata: { count: state.skills.length } };
    }
    if (method === "POST" && at("skills")) {
      // backend の create_agent_skill と同じ検証（#926）。
      const id = String(body.id ?? "").trim();
      if (!id) throw new HttpError(400, "スキルの ID を入力してください。");
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(id)) {
        throw new HttpError(422, "スキルの ID は英数字で始め、英数字・_・-・. の 100 文字以内にしてください。");
      }
      if (state.skills.some((skill) => skill.id === id)) {
        throw new HttpError(409, "同じ ID のスキルがあります。");
      }
      const skill = skillFromPayload(body, "runtime");
      state.skills.push(skill);
      return skill;
    }
    if (at("skills", "*")) {
      const skill = findOr404(state.skills, "id", second, "skill");
      if (skill.source !== "runtime" && method === "PATCH") {
        throw new HttpError(
          400,
          "組み込み・ファイル・環境変数のスキルは画面から変更できません。読み込み元の定義を変更してください。"
        );
      }
      if (skill.source !== "runtime" && method === "DELETE") {
        throw new HttpError(400, "組み込み・ファイル・環境変数のスキルは画面から削除できません。");
      }
      if (method === "GET") return skill;
      if (method === "PATCH") {
        Object.assign(skill, skillFromPayload(body, "runtime", skill));
        return skill;
      }
      if (method === "DELETE") {
        // 業務 Agent の下書きか公開中の版が使っていれば断る（backend の delete_agent_skill。#926）。
        const users = state.agents
          .filter((agent) => {
            const published = ((agent.versions as Json[] | undefined) ?? []).find(
              (item) => item.version === agent.published_version
            );
            return [agent.skill_ids, published?.skill_ids].some((ids) => ((ids as string[] | undefined) ?? []).includes(second));
          })
          .map((agent) => String(agent.name || agent.id));
        if (users.length) {
          throw new HttpError(
            409,
            `このスキルは業務 Agent（${users.join("、")}）が使っています。業務 Agent のスキルから外してから削除してください。`
          );
        }
        state.skills = state.skills.filter((candidate) => candidate.id !== second);
        return { skills: state.skills, metadata: { count: state.skills.length } };
      }
    }
  }

  // --- 設定 ---
  // チャットの添付（#2517）。添付の操作は spec が page.route で mock する。ここは入力欄が読む一覧と上限だけ。
  if (method === "GET" && at("chat-attachments")) {
    return {
      items: [],
      limits: {
        max_bytes: 20 * 1024 * 1024,
        max_files: 10,
        ttl_hours: 24,
        accept: [".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt"],
        image_enabled: false,
        enabled: state.chatAttachmentsEnabled,
      },
    };
  }
  if (head === "settings") {
    // ツール権限の画面のツール（組み込みと MCP 接続のツール。#1814）。判定は backend の ToolPolicy.decide と同じ。
    if (method === "GET" && at("settings", "tool-policy", "tools")) return toolPolicyTools(state);
    const patchable: Record<string, keyof MockApiState> = {
      "trace-policy": "tracePolicy",
      "tool-policy": "toolPolicy",
    };
    if (at("settings", "*") && second in patchable) {
      const key = patchable[second];
      if (method === "GET") return state[key];
      if (method === "PATCH") {
        Object.assign(state[key] as Json, body);
        return state[key];
      }
    }
    if (second === "mcp-connections") {
      const store = state.mcpConnections;
      const listData = () => ({ connections: store.connections.map(publicMcpConnection) });
      if (method === "GET" && at("settings", "mcp-connections")) return listData();
      if (method === "POST" && at("settings", "mcp-connections")) {
        const id = String(body.server_id ?? "").trim();
        if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/.test(id) || id.includes("__")) {
          throw new HttpError(422, "接続 ID は英数字で始まる 40 文字以内の英数字・「-」・「_」で入力してください。");
        }
        validateMcpUrl(body);
        if (store.connections.some((connection) => connection.server_id === id)) {
          throw new HttpError(409, "MCP 接続の ID はすでに使われています。");
        }
        const connection = mcpConnection({ ...body, server_id: id });
        store.connections.push(connection);
        store.connections.sort((left, right) => String(left.server_id).localeCompare(String(right.server_id)));
        return publicMcpConnection(connection);
      }
      if (third && at("settings", "mcp-connections", "*", "tools") && method === "GET") {
        const connection = findOr404(store.connections, "server_id", third, "MCP 接続");
        if (!connection.configured) throw new HttpError(400, "MCP 接続の URL が設定されていません。");
        return {
          tools: MOCK_MCP_TOOLS.map((tool) => ({
            ...tool,
            server_id: third,
            function_name: `${third}__${tool.name}`,
          })),
          metadata: { server_id: third, method: "tools/list" },
        };
      }
      if (third && at("settings", "mcp-connections", "*")) {
        const connection = findOr404(store.connections, "server_id", third, "MCP 接続");
        if (method === "PATCH") {
          validateMcpUrl(body);
          // RAG / NL2SQL の認証方式・audience は変えられない（backend と同じ 400。#1014）。
          const builtinAuthChanged =
            connection.source === "builtin" &&
            ((body.auth_mode != null && body.auth_mode !== connection.auth_mode) ||
              (body.service_audience != null &&
                (String(body.service_audience).trim() || third) !== connection.service_audience));
          if (builtinAuthChanged) {
            throw new HttpError(400, "標準の接続（RAG / NL2SQL）の認証方式と audience は変えられません。");
          }
          // 標準の接続の名前と、配備が決めた URL は変えられない（backend と同じ 400。#1325）。
          if (
            connection.source === "builtin" &&
            body.label != null &&
            (String(body.label).trim() || null) !== connection.label
          ) {
            throw new HttpError(400, "標準の接続（RAG / NL2SQL）の名前は変えられません。");
          }
          if (
            connection.base_url_locked &&
            body.base_url != null &&
            (String(body.base_url) || null) !== connection.base_url
          ) {
            const envName = third === "rag" ? "AGENT_EXTERNAL_RAG_MCP_URL" : "AGENT_EXTERNAL_NL2SQL_MCP_URL";
            throw new HttpError(
              400,
              `この接続の URL は配備（${envName}）で設定しているため、画面・API では変えられません。変えるときは配備の設定を変えて再起動してください。`
            );
          }
          Object.assign(connection, mcpConnection({ ...body, server_id: third }, connection));
          return publicMcpConnection(connection);
        }
        if (method === "DELETE") {
          if (!connection.removable) {
            throw new HttpError(400, "標準の接続（RAG / NL2SQL）・宣言の MCP 接続は削除できません。");
          }
          store.connections = store.connections.filter((candidate) => candidate.server_id !== third);
          return listData();
        }
      }
    }

    if (at("settings", "model")) {
      if (method === "GET") return state.modelSettings;
      if (method === "PATCH") {
        const enterpriseAi = { ...(body.enterprise_ai as Json) };
        // backend と同じく API key の値は応答へ返さない（接続ごと。#533）。
        const connections = ((enterpriseAi.connections as Json[] | undefined) ?? []).map(
          (connection) => ({
            ...connection,
            api_key: "",
            has_api_key:
              !connection.clear_api_key &&
              (Boolean(connection.api_key) || Boolean(connection.has_api_key)),
            clear_api_key: false,
          })
        );
        state.modelSettings = {
          ...state.modelSettings,
          settings: {
            enterprise_ai: { ...enterpriseAi, connections },
          },
        };
        return state.modelSettings;
      }
    }
    if (at("settings", "database")) {
      if (method === "GET") return state.databaseSettings;
      if (method === "PATCH") {
        const { password, wallet_password: walletPassword, ...rest } = body;
        Object.assign(state.databaseSettings, rest, {
          has_password: Boolean(password) || state.databaseSettings.has_password,
          has_wallet_password: Boolean(walletPassword) || state.databaseSettings.has_wallet_password,
        });
        return state.databaseSettings;
      }
    }
    if (at("settings", "answer-notice")) {
      if (method === "PUT") {
        state.answerNotice = { enabled: Boolean(body.enabled), text: (body.text as string | null) ?? null };
      }
      const defaultText = "この回答は AI が生成したもので、誤りや不足を含む場合があります。";
      return {
        ...state.answerNotice,
        default_text: defaultText,
        effective_text: state.answerNotice.text ?? defaultText,
        max_chars: 500,
        updated_at: null,
      };
    }
    if (at("settings", "trace-recording")) {
      if (method === "PUT") state.traceRecordingEnabled = Boolean(body.enabled);
      return { enabled: state.traceRecordingEnabled, updated_at: null };
    }
    if (at("settings", "chat-attachments")) {
      if (method === "PUT") state.chatAttachmentsEnabled = Boolean(body.enabled);
      return {
        enabled: state.chatAttachmentsEnabled,
        updated_at: null,
        max_bytes: 20 * 1024 * 1024,
        max_files: 10,
        ttl_hours: 24,
      };
    }
    if (at("settings", "upload-storage")) {
      if (method === "GET") return state.uploadStorage;
      if (method === "PATCH") {
        Object.assign(state.uploadStorage, body);
        return state.uploadStorage;
      }
    }
  }

  return undefined;
}

/**
 * Run の span のイベント（spec が `run.progress_events` に `ProgressLog` で積んだ記録。無ければ状態から作る最小の
 * 記録）。mock の中だけの記録で、画面へは `traceSnapshot` の形（共通の span の表の snapshot）で返す。
 */
function runProgressEvents(run: Json): Json[] {
  return Array.isArray(run.progress_events) ? (run.progress_events as Json[]) : progressEventsForStatus(run);
}

const RUN_TERMINAL: Readonly<Record<string, "done" | "failed" | "cancelled">> = {
  completed: "done",
  failed: "failed",
  cancelled: "cancelled",
};

/**
 * 共通の span の表の snapshot（backend の `trace_progress_events`）。span ごとに最新の状態だけを、最初に現れた順に
 * 番号を振り直して返す。終わった Run は、表で実行中のままの span を Run の終端に合わせて閉じ（停止はスキップ）、
 * 終端のイベントを足す（Run の状態が正本）。trace の無い Run は空。
 */
function traceSnapshot(run: Json): Json {
  const latest = new Map<string, Json>();
  for (const event of runProgressEvents(run)) {
    if (event.type === "step") latest.set(String(event.step_id), event);
  }
  const terminal = RUN_TERMINAL[String(run.status)];
  const emittedAt = typeof run.updated_at === "string" ? run.updated_at : new Date().toISOString();
  const events: Json[] = [...latest.values()].map((event, index) => {
    const step: Json = { ...event, seq: index + 1, target_id: run.id };
    if (terminal && (step.status === "running" || step.status === "pending")) {
      step.status = terminal === "done" ? "done" : terminal === "failed" ? "failed" : "skipped";
      step.finished_at = step.finished_at ?? emittedAt;
      if (terminal === "cancelled") step.cancelled = true;
    }
    return step;
  });
  const traceId = runTraceId(run, events);
  if (traceId === null) return { target_id: run.id, trace_id: null, events: [], dropped_spans: 0 };
  if (terminal) {
    events.push({
      schema_version: 1,
      type: "terminal",
      seq: events.length + 1,
      target_id: run.id,
      attempt: 0,
      emitted_at: emittedAt,
      status: terminal,
    });
  }
  return { target_id: run.id, trace_id: traceId, events, dropped_spans: Number(run.trace_dropped_spans ?? 0) };
}

/** Run の trace id（spec が入れていなければ、span の記録があれば mock の trace id）。 */
function runTraceId(run: Json, events?: Json[]): string | null {
  if (typeof run.trace_id === "string" || run.trace_id === null) return run.trace_id as string | null;
  const spans = events ?? runProgressEvents(run).filter((event) => event.type === "step");
  return spans.length > 0 ? SYNTHETIC_TRACE_ID : null;
}

function isRunPayload(value: unknown): value is Json {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as Json).goal === "string" &&
    typeof (value as Json).status === "string" &&
    Array.isArray((value as Json).steps)
  );
}

/**
 * 応答の Run（単体・`runs` の一覧）に、共通の span の表の記録の項目（`trace_store`・`trace_id`・
 * `trace_dropped_spans`）を入れる（state は書き換えない）。mock の中の span の記録（`progress_events`）は返さない。
 */
function withRunProgress(data: unknown): unknown {
  const decorate = (run: Json): Json => {
    const rest = { ...run };
    delete rest.progress_events;
    return {
      ...rest,
      trace_store: run.trace_store ?? true,
      trace_id: runTraceId(run),
      trace_dropped_spans: Number(run.trace_dropped_spans ?? 0),
    };
  };
  if (isRunPayload(data)) return decorate(data);
  if (typeof data === "object" && data !== null && Array.isArray((data as Json).runs)) {
    return { ...(data as Json), runs: ((data as Json).runs as unknown[]).map((run) => (isRunPayload(run) ? decorate(run) : run)) };
  }
  return data;
}

async function fulfillJson(route: Route, status: number, payload: unknown) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
}

async function fulfillHttpError(route: Route, error: HttpError) {
  await fulfillJson(route, error.status, {
    data: null,
    error_messages: [error.message],
    warning_messages: [],
    error_code: error.errorCode ?? null,
    detail: error.message,
  });
}

export async function installMockApi(page: Page): Promise<MockApi> {
  const mockApi: MockApi = {
    state: createState(),
    requests: [],
    unmocked: [],
    lastRequest(method, path) {
      return [...this.requests]
        .reverse()
        .find((request) => request.method === method && request.path === path);
    },
    setCurrentUser(user) {
      this.state.auth.currentUser = user ? clone(user) : null;
    },
  };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    let body: unknown;
    try {
      body = request.postDataJSON();
    } catch {
      body = request.postData();
    }
    mockApi.requests.push({
      method,
      path: url.pathname,
      searchParams: url.searchParams,
      body,
      headers: await request.allHeaders(),
    });
    // backend と同じ権限の判定（manifest と capability。#1113）。
    try {
      enforceBackendPermissions(mockApi.state, method, url.pathname);
    } catch (error) {
      if (!(error instanceof HttpError)) throw error;
      await fulfillHttpError(route, error);
      return;
    }
    // Run のイベント購読（SSE）。e2e は stream を保てないため空の stream を返して閉じる
    // （画面は購読の停止を示す。#215）。
    // Excel の書き出し・テンプレート（#776）。中身は確かめないため、Excel の形の空のデータを返す。
    if (method === "GET" && url.pathname.endsWith(".xlsx")) {
      await route.fulfill({
        status: 200,
        contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        body: Buffer.from("PK-mock-xlsx"),
      });
      return;
    }
    if (method === "GET" && /^\/api\/runs\/[^/]+\/events$/.test(url.pathname)) {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
      return;
    }
    try {
      const data = handle(
        mockApi.state,
        method,
        url.pathname,
        url.searchParams,
        (body && typeof body === "object" ? body : {}) as Json
      );
      if (data === undefined) {
        mockApi.unmocked.push(`${method} ${url.pathname}${url.search}`);
        await fulfillJson(route, 404, {
          data: null,
          error_messages: [`e2e unmocked API: ${method} ${url.pathname}`],
          warning_messages: [],
        });
        return;
      }
      await fulfillJson(route, 200, { data: withRunProgress(data), error_messages: [], warning_messages: [] });
    } catch (error) {
      if (error instanceof HttpError) {
        await fulfillHttpError(route, error);
        return;
      }
      throw error;
    }
  });

  return mockApi;
}

export const test = base.extend<{ mockApi: MockApi }>({
  mockApi: [
    async ({ page }, use) => {
      const mockApi = await installMockApi(page);
      await use(mockApi);
      expect(mockApi.unmocked, "e2e/fixtures/mock-api.ts が扱っていない API が呼ばれました").toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
