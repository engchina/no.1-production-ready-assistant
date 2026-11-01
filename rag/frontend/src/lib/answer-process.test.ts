import {
  chatProgressHasSpans,
  chatProgressStepsFromEvents,
  chatProgressTraceIdOf,
  traceTimeline,
  type ChatProgressEvent,
} from "@production-ready/ui";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  answerStageName,
  answerStepDisplayName,
  answerStepMeta,
  llmCallsLabel,
  RAG_TRACE_DETAILS_LABELS,
} from "./answer-process";
import { CHAT_PROGRESS_DEFINITIONS } from "./chat-progress";
import { t } from "./i18n";

// 回答の処理の経過（#2096 / #3226）: チャット・RAG 検索・実行の詳細は、回答の trace の span のイベントから同じ行を作る。

const T0 = Date.parse("2026-10-16T00:00:00.000Z");
const TRACE_ID = "a".repeat(32);

/** span のイベント（backend の `ChatProgressRecorder.record_span` と同じ形）。 */
function spanEvents(
  spans: { id: string; parent?: string; name: string; kind?: string; params?: Record<string, string | number>; attributes?: Record<string, string | number>; root?: boolean; status?: "running" | "done" | "failed" }[]
): ChatProgressEvent[] {
  return spans.map((span, index) => ({
    schema_version: 1,
    seq: index + 1,
    target_id: "t",
    attempt: 0,
    emitted_at: new Date(T0 + index * 100).toISOString(),
    type: "step",
    step_id: span.id.repeat(16),
    kind: span.kind ?? span.id.repeat(16),
    status: span.status ?? "done",
    started_at: new Date(T0 + index * 100).toISOString(),
    ...(span.status === "running" ? {} : { finished_at: new Date(T0 + index * 100 + 50).toISOString() }),
    ...(span.params ? { params: span.params } : {}),
    name: span.name,
    ...(span.parent ? { parent_id: span.parent.repeat(16) } : {}),
    ...(span.root ? { root: true } : {}),
    trace_id: TRACE_ID,
    ...(span.attributes ? { attributes: span.attributes } : {}),
  }));
}

describe("answerStepDisplayName", () => {
  it("backend の工程名を利用者向けの名前にし、回数つきの工程は回数を添える。知らない工程名はそのまま", async () => {
    const { answerStepDisplayName } = await import("./answer-process");
    expect(answerStepDisplayName("質問拡張戦略")).toBe("資料を探す検索表現を作る");
    expect(answerStepDisplayName("Rerank")).toBe("質問との関連度で候補を並べ替える");
    expect(answerStepDisplayName("rerank.request")).toBe("候補の並べ替えを要求する");
    expect(answerStageName("rerank.request")).toBe("候補の並べ替えを要求する");
    expect(answerStepDisplayName("文書検索（2回目）")).toBe("検索文から根拠の候補を集める（2 回目）");
    expect(answerStepDisplayName("回答文の生成と根拠確認（1回目）")).toBe("回答文を作って根拠と照合する（1 回目）");
    expect(answerStepDisplayName("検索語の再確定（書き換え後）")).toBe("探し直しに使う検索語を決める");
    // 用語・同義語と回答ルールは別の工程（設定画面のタブの名前と同じ語。#3451）。
    expect(answerStepDisplayName("用語・同義語の確認")).toBe("一致する用語の別名を検索文に加える");
    expect(answerStepDisplayName("回答ルールの確認")).toBe("確認と回答に使うルールを選ぶ");
    expect(answerStageName("回答ルールの確認")).toBe("確認と回答に使うルールを選ぶ");
    expect(answerStepDisplayName("新しい工程")).toBe("新しい工程");
    expect(answerStepDisplayName("新しい工程（2回目）")).toBe("新しい工程（2回目）");
  });

  it("backend の時間切れの文言の名前（ANSWER_STEP_DISPLAY_NAMES）と同じ名前にする", async () => {
    const { answerStepDisplayName } = await import("./answer-process");
    const source = readFileSync(resolve(__dirname, "../../../backend/app/rag/span_display.py"), "utf-8");
    const block = source.slice(source.indexOf("ANSWER_STEP_DISPLAY_NAMES"), source.indexOf("_STEP_ATTEMPT ="));
    const pairs = [...block.matchAll(/"([^"]+)": "([^"]+)"/g)].map((match) => [match[1], match[2]]);
    expect(pairs.length).toBeGreaterThan(10);
    for (const [name, label] of pairs) expect(answerStepDisplayName(name), name).toBe(label);
  });

  // #3272: 画面の製品。別の製品が書いた span は RAG の工程の定義で訳さず、書いた製品が付けた名前で出す。
  it("RAG の trace の文言は画面の製品を rag にする", async () => {
    const { RAG_TRACE_DETAILS_LABELS } = await import("./answer-process");
    expect(RAG_TRACE_DETAILS_LABELS.product).toBe("rag");
  });
});

describe("answerStepMeta", () => {
  it("結果の説明（complete・中断以外）と LLM の回数を「 · 」でつなぐ", () => {
    expect(answerStepMeta({ llmCalls: 0, note: "complete" })).toBeUndefined();
    expect(answerStepMeta({ llmCalls: 3, note: "省略" })).toBe("省略 · LLM 3 回");
    expect(answerStepMeta({ note: "エラーで中断" })).toBeUndefined();
  });
});

describe("処理の経過の span の木（Issue 3226）", () => {
  const events = spanEvents([
    { id: "1", name: "rag.answer", root: true },
    { id: "2", parent: "1", name: "rag.history_rewrite", kind: "history_rewrite", attributes: { "pr.stage": "history_rewrite" } },
    { id: "3", parent: "1", name: "rag.answer", attributes: { "pr.stage": "answer" } },
    { id: "4", parent: "3", name: "rag.engine.stage", kind: "answer_step", params: { name: "回答生成フロー" }, attributes: { "pr.stage": "回答生成フロー" } },
    { id: "5", parent: "4", name: "rag.engine.stage", kind: "answer_step", params: { name: "文書検索", llm_calls: 2, note: "該当なし" }, attributes: { "pr.stage": "文書検索" } },
    { id: "6", parent: "5", name: "gen_ai.embeddings", attributes: { "gen_ai.request.model": "synthetic-embed" } },
    { id: "7", parent: "1", name: "rag.answer_record_save", kind: "answer_record_save", status: "running" },
  ]);

  it("root を行にせず、工程の定義・段の名前・技術の span の名前で、親子の入れ子にする", () => {
    const steps = chatProgressStepsFromEvents(events, CHAT_PROGRESS_DEFINITIONS, "t", { traceLabels: RAG_TRACE_DETAILS_LABELS });
    expect(steps.map((step) => [step.label, step.level ?? 0, step.status, step.meta ?? null])).toEqual([
      [t("search.stage.historyRewrite"), 0, "done", null],
      [t("search.stage.answer"), 0, "done", null],
      [t("answer.step.answerFlow"), 1, "done", null],
      [answerStepDisplayName("文書検索"), 2, "done", "該当なし · LLM 2 回"],
      [`${t("trace.span.embeddings")} · synthetic-embed`, 3, "done", null],
      // 実行中の span は実行中の行になる。
      [t("search.stage.answerRecordSave"), 0, "running", null],
    ]);
    // 行を開くと技術の値（span の名前）が付く。
    expect(steps[3].details?.fields?.some((field) => field.label === "Span name" && field.value === "rag.engine.stage")).toBe(true);
    expect(chatProgressTraceIdOf(events)).toBe(TRACE_ID);
    expect(chatProgressHasSpans(events)).toBe(true);
  });

  it("span の無い古い形式の段だけの記録は span の記録としない", () => {
    const legacy = events.map((event): ChatProgressEvent => {
      if (event.type !== "step") return event;
      const copy = { ...event };
      delete copy.name;
      delete copy.parent_id;
      delete copy.root;
      delete copy.trace_id;
      delete copy.attributes;
      return copy;
    });
    expect(chatProgressHasSpans(legacy)).toBe(false);
  });
});

// 送り直しで API への送信が呼び出しより多いとき（#2966）。
describe("LLM の回数と送信の回数", () => {
  it("送り直しで API への送信が多いときだけ、送信の回数も示す", () => {
    expect(answerStepMeta({ llmCalls: 1, llmSends: 2 })).toBe("LLM 1 回・送信 2 回");
    expect(answerStepMeta({ llmCalls: 2, llmSends: 2 })).toBe("LLM 2 回");
    expect(answerStepMeta({ llmCalls: 1 })).toBe("LLM 1 回");
    expect(llmCallsLabel("history.process", 1, 3)).toBe("LLM 1 回・送信 3 回");
  });
});

// #3214 / #3219: 保存した trace の span も、処理の経過の行と同じ名前（同じ関数）にし、技術の span も日本語にする。
describe("RAG_TRACE_DETAILS_LABELS（トレースの span の名前）", () => {
  const at = (ms: number) => new Date(T0 + ms).toISOString();
  const span = (id: string, parent: string | null, operation_name: string, attributes: Record<string, unknown> = {}) => ({
    span_id: id.repeat(16), parent_span_id: parent?.repeat(16) ?? null, trace_id: "a".repeat(32), operation_name, kind: "internal",
    status: "done", started_at: at(0), finished_at: at(1000), duration_ms: 1000, attributes,
  });
  const summary = { schema_version: 1, completeness: "complete", root_span_id: "1".repeat(16), spans: [
    span("1", null, "rag.answer"),
    span("2", "1", "rag.history_rewrite", { "pr.stage": "history_rewrite" }),
    span("3", "1", "rag.answer", { "pr.stage": "answer" }),
    span("4", "3", "rag.engine.stage", { "pr.stage": "回答生成フロー" }),
    span("5", "4", "rag.engine.stage", { "pr.stage": "文書検索（2回目）" }),
    span("6", "5", "gen_ai.embeddings", { "gen_ai.request.model": "synthetic-embed" }),
    span("7", "1", "rag.answer_record_save", { "pr.stage": "answer_record_save" }),
  ] };

  it("処理の経過と同じ名前を付け、root と中の同名の段を分ける", () => {
    const labels = traceTimeline(summary, RAG_TRACE_DETAILS_LABELS).steps.map((step) => step.label);
    expect(labels).toEqual([
      t("trace.span.root"),
      t("search.stage.historyRewrite"),
      t("search.stage.answer"),
      answerStepDisplayName("回答生成フロー"),
      answerStepDisplayName("文書検索（2回目）"),
      `${t("trace.span.embeddings")} · synthetic-embed`,
      t("search.stage.answerRecordSave"),
    ]);
    expect(labels.some((label) => /rag\.|gen_ai\./.test(label))).toBe(false);
    expect(labels[3]).toBe(t("answer.step.answerFlow"));
    // 入れ子の段（回答エンジンの全体と、その中の回答生成フロー）は別の名前にする。
    expect(labels[2]).not.toBe(labels[3]);
  });

  it("処理の経過の段の名前と trace の段の名前は同じ関数から作る", () => {
    for (const stage of ["history_rewrite", "field_filter", "answer", "evidence", "retrieval", "answer_record_save", "質問の理解", "根拠検索フロー", "根拠確認（2回目）"]) {
      expect(RAG_TRACE_DETAILS_LABELS.stageName?.(stage, "done")).toBe(answerStageName(stage));
    }
    expect(answerStageName("evidence")).toBe("質問の条件を整理して根拠を探す");
    expect(answerStageName("根拠検索フロー")).toBe("回答に使う根拠を検索する");
    expect(answerStageName("未知の工程")).toBeUndefined();
  });
});


it("リランクの論理要求とSDK試行をstageの有無に関わらず同じ階層で訳す", () => {
  const events = spanEvents([
    { id: "1", name: "rag.answer", root: true },
    { id: "2", parent: "1", name: "rag.engine.stage", attributes: { "pr.stage": "Rerank" } },
    { id: "3", parent: "2", name: "rerank.request", attributes: { "pr.stage": "rerank.request" } },
    { id: "4", parent: "3", name: "gen_ai.rerank", attributes: { "gen_ai.request.model": "synthetic-rerank", "pr.attempt": 2 } },
    { id: "5", parent: "2", name: "rerank.request" },
  ]);
  const steps = chatProgressStepsFromEvents(events, CHAT_PROGRESS_DEFINITIONS, "t", { traceLabels: RAG_TRACE_DETAILS_LABELS });
  expect(steps.map(step => [step.label, step.level ?? 0])).toEqual([
    ["質問との関連度で候補を並べ替える", 0],
    ["候補の並べ替えを要求する", 1],
    ["質問との関連度で候補を並べ替える · synthetic-rerank（2 回目）", 2],
    ["候補の並べ替えを要求する", 1],
  ]);
  expect(steps[1].details?.fields).toContainEqual({ label: "Span name", value: "rerank.request", mono: true, technical: true });
  expect(traceTimeline({ spans: events.filter(event => event.type === "step").map(event => ({ span_id: event.step_id, trace_id: event.trace_id, parent_span_id: event.parent_id, operation_name: event.name, status: event.status, started_at: event.started_at, finished_at: event.finished_at, attributes: event.attributes })), root_span_id: "1".repeat(16) }, RAG_TRACE_DETAILS_LABELS).steps[2].label).toBe("候補の並べ替えを要求する");
});
