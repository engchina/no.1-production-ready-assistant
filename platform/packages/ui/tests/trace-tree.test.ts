import { describe, expect, it } from "vitest";
import { traceSpanFields, traceTimeline } from "../src/lib/trace-tree";
import { TRACE_DETAILS_JA, createTraceDetailsLabels } from "../src/lib/trace-labels";
import { runTimelineDuration } from "../src/components/data/run-detail";
const at = (n: number) => new Date(Date.UTC(2026, 0, 1) + n).toISOString();
const span = (id: string, parent?: string) => ({ span_id: id.repeat(16), parent_span_id: parent?.repeat(16), trace_id: "a".repeat(32), operation_name: "synthetic", status: "done", started_at: at(0), finished_at: at(2000), duration_ms: 2000 });
describe("安全な trace の表示", () => {
  it("親より先の子・孤立親・cycle を有限回で描き、欠落を明示する", () => {
    const value = traceTimeline({ completeness: "complete", root_span_id: "1".repeat(16), spans: [span("2", "1"), span("1"), span("3", "4"), span("4", "3"), span("5", "6")] });
    expect(value.steps.map(s => s.id)).toEqual(["1", "2", "5", "3", "4"].map(id => id.repeat(16)));
    expect(value.steps.map(s => s.level)).toEqual([0, 1, 0, 0, 1]);
    expect(value.partial).toBe(true);
  });
  it("任意の属性・重複 ID・不正な型を描かない", () => {
    const value = traceTimeline({ completeness: "complete", spans: [{ ...span("1"), attributes: { prompt: "private-payload", "gen_ai.usage.input_tokens": 0 } }, span("1"), null] });
    expect(value.steps).toHaveLength(1);
    expect(JSON.stringify(value)).not.toContain("private-payload");
    expect(value.steps[0].details?.fields?.some(f => f.value === "0")).toBe(true);
    expect(value.partial).toBe(true);
  });
  it("並列の 2 秒の呼び出しを 4 秒と表示しない", () => {
    const steps = traceTimeline({ completeness: "complete", spans: [span("1"), span("2")] }).steps;
    expect(runTimelineDuration(steps)).toBe(2000);
  });
  it("停止を失敗に置き換えず、壊れた記録を許容する", () => {
    const steps = traceTimeline({ spans: [{ ...span("1"), status: "cancelled" }] }).steps;
    expect(steps[0].status).toBe("skipped");
    expect(steps[0].detail).toBe("停止");
    expect(traceTimeline(null).steps).toEqual([]);
  });
});
describe("span の表示名（#3219。3 製品の 9 画面で共通の規則）", () => {
  const stage = (id: string, parent: string | undefined, operation_name: string, attributes: Record<string, unknown> = {}) => ({ ...span(id, parent), operation_name, attributes });
  const summary = { completeness: "complete", root_span_id: "1".repeat(16), spans: [
    stage("1", undefined, "op.answer", { "pr.attempt": 2 }),
    stage("2", "1", "op.answer", { "pr.stage": "answer" }),
    stage("3", "2", "op.engine.stage", { "pr.stage": "工程 A" }),
    stage("4", "3", "gen_ai.chat", { "gen_ai.request.model": "model-x" }),
    stage("5", "3", "gen_ai.execute_tool", { "gen_ai.tool.name": "tool_y", "gen_ai.request.model": "model-x" }),
    stage("6", "5", "mcp.request", { "pr.attempt": 3 }),
    stage("7", "3", "gen_ai.embeddings", { "gen_ai.request.model": "embed-x" }),
    stage("8", "3", "gen_ai.rerank", { "gen_ai.request.model": "rerank-x" }),
    stage("9", "3", "gen_ai.invoke_agent", { "gen_ai.request.model": "model-x" }),
    stage("b", "3", "custom.operation"),
    stage("c", "5", "mcp.request", { "mcp.method.name": "tools/call", "gen_ai.tool.name": "tool_y" }),
    stage("d", "5", "mcp.request", { "mcp.method.name": "initialize" }),
  ] };
  it("既定は root・段・技術の span を日本語にし、対象と試行の回数を添える", () => {
    const steps = traceTimeline(summary).steps;
    expect(steps.map(s => s.label)).toEqual([
      "実行の全体（2 回目）", "処理を実行する", "処理を実行する", "モデルを呼び出す（用途の記録なし）", "ツールの実行 · tool_y", "MCP の呼び出し（3 回目）",
      // 技術の trace の一覧は MCP の手続きも残し、呼んだツールか method を添える（#3272）。
      "MCP の呼び出し · tool_y", "MCP の呼び出し · initialize",
      "テキストの埋め込みを作る · embed-x", "質問との関連度で候補を並べ替える · rerank-x", "AI の呼び出し · model-x", "処理を実行する",
    ]);
  });
  it("MCP の呼び出しは呼んだツールの表示名（pr.tool.title）を先に添え、内部名は詳細に残す（#3468）", () => {
    const steps = traceTimeline({ completeness: "complete", root_span_id: "1".repeat(16), spans: [
      stage("1", undefined, "op.answer"),
      stage("2", "1", "mcp.request", { "mcp.method.name": "tools/call", "gen_ai.tool.name": "tool_y", "pr.tool.title": "合成の照合" }),
    ] }).steps;
    expect(steps[1].label).toBe("MCP の呼び出し · 合成の照合");
    expect(steps[1].details?.fields).toContainEqual({ label: "gen_ai.tool.name", value: "tool_y", mono: true, technical: true });
  });
  it("段は製品の段の名前（処理の経過と同じ関数）を使い、詳細に元の span の名前と段を残す", () => {
    const seen: [string, string][] = [];
    const labels = createTraceDetailsLabels(key => TRACE_DETAILS_JA[key], {
      stageName: (name, status) => { seen.push([name, status]); return name === "answer" ? "回答の段" : name === "工程 A" ? "表示 工程 A" : undefined; },
    });
    const steps = traceTimeline(summary, labels).steps;
    expect(steps.map(s => s.label).slice(0, 3)).toEqual(["実行の全体（2 回目）", "回答の段", "表示 工程 A"]);
    expect(seen).toEqual([["answer", "done"], ["工程 A", "done"]]);
    const fields = steps[2].details?.fields ?? [];
    expect(fields).toContainEqual({ label: "Span name", value: "op.engine.stage", mono: true, technical: true });
    expect(fields).toContainEqual({ label: "pr.stage", value: "工程 A", mono: true, technical: true });
  });
});
describe("モデルの呼び出しの入力の文脈の記録と利用量（#2583）", () => {
  const context = {
    "gen_ai.request.model": "model-x",
    "gen_ai.usage.input_tokens": 1200,
    "gen_ai.usage.output_tokens": 30,
    "gen_ai.usage.cache_read.input_tokens": 0,
    "pr.context.instructions.static_fingerprint": "0123456789abcdef",
    "pr.context.instructions.fingerprint": "fedcba9876543210",
    "pr.context.tools.fingerprint": "00112233aabbccdd",
    "pr.context.tools.count": 4,
    "pr.context.input.items": 7,
    "pr.context.estimate.static_instructions_tokens": 300,
    "pr.context.estimate.dynamic_state_tokens": 40,
    "pr.context.estimate.tool_definitions_tokens": 500,
    "pr.context.estimate.history_tokens": 200,
    "pr.context.estimate.tool_outputs_tokens": 150,
    "pr.context.estimate.input_tokens": 1190,
    "pr.context.budget.input_tokens": 100000,
    "pr.context.budget.remaining_tokens": 98810,
    "pr.context.compaction.tokens_before": 5000,
    "pr.context.compaction.tokens_after": 1190,
    "pr.context.compaction.outputs": 2,
    prompt: "private-payload",
  };
  it("利用量と文脈の値を日本語の見出しで、識別子 → 利用量 → 見積もり → 予算 → 圧縮 → 指紋の順に出す", () => {
    expect(traceSpanFields(undefined, "gen_ai.chat", context)).toEqual([
      { label: "Span name", value: "gen_ai.chat", mono: true, technical: true },
      { label: "gen_ai.request.model", value: "model-x", mono: true, technical: true },
      { label: "入力トークン", value: "1200", mono: true, technical: true },
      { label: "出力トークン", value: "30", mono: true, technical: true },
      { label: "キャッシュから読んだ入力", value: "0", mono: true, technical: true },
      { label: "入力の見積もり（指示・固定）", value: "300", mono: true, technical: true },
      { label: "入力の見積もり（動的な状態）", value: "40", mono: true, technical: true },
      { label: "入力の見積もり（ツールの定義）", value: "500", mono: true, technical: true },
      { label: "入力の見積もり（履歴）", value: "200", mono: true, technical: true },
      { label: "入力の見積もり（ツールの結果）", value: "150", mono: true, technical: true },
      { label: "入力の見積もり（合計）", value: "1190", mono: true, technical: true },
      { label: "入力の項目数", value: "7", mono: true, technical: true },
      { label: "文脈の予算", value: "100000", mono: true, technical: true },
      { label: "文脈の予算の残り", value: "98810", mono: true, technical: true },
      { label: "圧縮の前", value: "5000", mono: true, technical: true },
      { label: "圧縮の後", value: "1190", mono: true, technical: true },
      { label: "圧縮したツールの結果の数", value: "2", mono: true, technical: true },
      { label: "指示の指紋（固定）", value: "0123456789abcdef", mono: true, technical: true },
      { label: "指示の指紋（全体）", value: "fedcba9876543210", mono: true, technical: true },
      { label: "ツール定義の指紋", value: "00112233aabbccdd", mono: true, technical: true },
      { label: "ツール定義の数", value: "4", mono: true, technical: true },
    ]);
  });
  it("キャッシュの値が無いときは 0 と区別して「不明」、利用量そのものが無いときは「未計測」にする", () => {
    const { "gen_ai.usage.cache_read.input_tokens": _cache, ...withoutCache } = context;
    expect(traceSpanFields(undefined, "gen_ai.chat", withoutCache)).toContainEqual({ label: "キャッシュから読んだ入力", value: "不明", mono: false, technical: true });
    expect(traceSpanFields(undefined, "gen_ai.chat", context)).toContainEqual({ label: "キャッシュから読んだ入力", value: "0", mono: true, technical: true });
    expect(traceSpanFields(undefined, "gen_ai.chat", {}).filter(field => !field.mono)).toEqual([
      { label: "入力トークン", value: "未計測", mono: false, technical: true },
      { label: "出力トークン", value: "未計測", mono: false, technical: true },
      { label: "キャッシュから読んだ入力", value: "未計測", mono: false, technical: true },
    ]);
  });
  it("文脈の記録の無い span（RAG / NL2SQL・モデル以外の呼び出し）には文脈の行を足さない", () => {
    const labels = traceSpanFields(undefined, "gen_ai.embeddings", { "gen_ai.request.model": "embed-x" }).map(field => field.label);
    expect(labels).toEqual(["Span name", "gen_ai.request.model"]);
    // 予算・圧縮の無い呼び出し（Run の外の呼び出し）は、その行を出さない。
    const outside = traceSpanFields(undefined, "gen_ai.chat", { "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1, "pr.context.estimate.input_tokens": 9 }).map(field => field.label);
    expect(outside).toContain("入力の見積もり（合計）");
    expect(outside.some(label => label.startsWith("文脈の予算") || label.startsWith("圧縮"))).toBe(false);
  });
  it("見出しは製品の辞書（createTraceDetailsLabels の t）から付ける", () => {
    const labels = createTraceDetailsLabels(key => (key.startsWith("trace.field.") ? `訳:${key}` : TRACE_DETAILS_JA[key]));
    const fields = traceTimeline({ completeness: "complete", spans: [{ ...span("1"), operation_name: "gen_ai.chat", attributes: context }] }, labels).steps[0].details?.fields ?? [];
    expect(fields).toContainEqual({ label: "訳:trace.field.budget.remaining", value: "98810", mono: true, technical: true });
    expect(JSON.stringify(fields)).not.toContain("private-payload");
  });
});

describe("モデル用途の共通表示", () => {
  it("製品にかかわらず記録した用途を見出しにし、モデル名を詳細へ残す", () => {
    for (const product of ["rag", "nl2sql", "agent"]) {
      const steps = traceTimeline({ spans: [{ ...span("1", "0"), operation_name: "gen_ai.chat", attributes: {
        "pr.product": product, "pr.gen_ai.purpose": "answer.validate", "gen_ai.request.model": "model-x",
      } }] }).steps;
      expect(steps[0].label).toBe("回答の段落を取得した資料と照合する");
      expect(steps[0].details?.fields).toContainEqual({ label: "gen_ai.request.model", value: "model-x", mono: true, technical: true });
    }
  });
  it("未知の用途を本文から推測せず、モデル以外の処理に流用しない", () => {
    const steps = traceTimeline({ spans: [
      { ...span("1", "0"), operation_name: "gen_ai.chat", attributes: { "pr.gen_ai.purpose": "unknown-purpose", "gen_ai.request.model": "model-x" } },
      { ...span("2", "0"), operation_name: "http.request", attributes: { "pr.gen_ai.purpose": "answer.validate" } },
    ] }).steps;
    expect(steps.map(step => step.label)).toEqual(["モデルを呼び出す（用途の記録なし）", "HTTP の要求"]);
  });
  it("埋め込みの用途と再試行回数を表示する", () => {
    const steps = traceTimeline({ spans: [{ ...span("1", "0"), operation_name: "gen_ai.embeddings", attributes: {
      "pr.gen_ai.purpose": "sql.generate", "pr.attempt": 2,
    } }] }).steps;
    expect(steps[0].label).toBe("質問に答える SQL を作る（2 回目）");
  });
});


describe("モデル接続確認の用途", () => {
  it.each(["gen_ai.chat", "gen_ai.embeddings", "gen_ai.rerank"])("%s は接続確認を明示し、モデル名を詳細に残す", (operation_name) => {
    const step = traceTimeline({ spans: [{ ...span("1", "0"), operation_name, attributes: { "pr.gen_ai.purpose": "connection.check", "gen_ai.request.model": "model-probe" } }] }).steps[0];
    expect(step.label).toBe("モデルが応答するか接続を確かめる");
    expect(step.details?.fields?.some(field => field.value === "model-probe")).toBe(true);
  });
  it.each(["gen_ai.embeddings", "gen_ai.rerank"])("%s の用途未記録は元の操作とモデル名を使う", (operation_name) => {
    const step = traceTimeline({ spans: [{ ...span("1", "0"), operation_name, attributes: { "gen_ai.request.model": "model-normal" } }] }).steps[0];
    expect(step.label).toContain("model-normal");
    expect(step.label).not.toContain("接続を確かめる");
  });
});


it.each(["done", "failed", "cancelled"])("論理的な並べ替え要求は%sでも日本語名にし、SDKと元IDを残す", (status) => {
  const request = { ...span("3", "2"), operation_name: "rerank.request", status, attributes: { "pr.attempt": 2 } };
  const tree = traceTimeline({ root_span_id: "1".repeat(16), spans: [span("1"), span("2", "1"), request, { ...span("4", "3"), operation_name: "gen_ai.rerank", attributes: { "gen_ai.request.model": "synthetic-rerank" } }, { ...span("5", "2"), operation_name: "rerank.request" }] });
  expect(tree.steps.map(step => step.level)).toEqual([0, 1, 2, 3, 2]);
  expect(tree.steps[2].label).toBe("候補の並べ替えを要求する（2 回目）");
  expect(tree.steps[3].label).toBe("質問との関連度で候補を並べ替える · synthetic-rerank");
  expect(tree.steps[3].parentId).toBe("3".repeat(16));
  expect(tree.steps[3].details?.fields).toContainEqual({ label: "Parent span ID", value: "3".repeat(16), mono: true, technical: true });
  expect(tree.steps[4].label).toBe("候補の並べ替えを要求する");
  expect(tree.steps[2].details?.fields).toContainEqual({ label: "Span name", value: "rerank.request", mono: true, technical: true });
  expect(request.operation_name).toBe("rerank.request");
});

it.each(["rag", "agent", "nl2sql"])("%s の無効な添付は同じ名前とスキップの状態で表示する", product => {
  const result = traceTimeline({spans: [{...span("1", "0"), operation_name: `${product}.attachment_evidence`, status: "skipped"}]}).steps[0];
  expect(result.label).toBe("会話の添付から根拠を選ぶ");
  expect(result.status).toBe("skipped");
});
