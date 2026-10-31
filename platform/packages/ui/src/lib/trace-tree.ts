import { DEFAULT_TRACE_DETAILS_LABELS, TRACE_FIELD_LABEL_KEYS, type TraceDetailsLabels, type TraceFieldName, type TraceSpanNames, type TraceSpanStatus } from "./trace-labels";
import type { RunTimelineStep } from "../components/data/run-detail";

function textAttribute(attributes: Readonly<Record<string, unknown>>, key: string): string | undefined {
  const value = attributes[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

/**
 * 技術の span の名前と、名前に添える対象（モデル・ツール）の属性（先に見つかったもの）。MCP の呼び出しは呼んだツール、
 * ツール以外の要求（`initialize` など）は JSON-RPC の method（OpenTelemetry の MCP の semantic conventions。#3272）。
 */
const TECHNICAL_SPANS: Readonly<Record<string, { name: Exclude<keyof TraceSpanNames, "attempt" | "root">; target?: readonly string[] }>> = {
  "agent.attachment_evidence": { name: "attachments" },
  "nl2sql.attachment_evidence": { name: "attachments" },
  "rag.attachment_evidence": { name: "attachments" },
  "gen_ai.chat": { name: "chat", target: ["gen_ai.request.model"] },
  "gen_ai.embeddings": { name: "embeddings", target: ["gen_ai.request.model"] },
  "gen_ai.rerank": { name: "rerank", target: ["gen_ai.request.model"] },
  "rerank.request": { name: "rerankRequest" },
  "gen_ai.execute_tool": { name: "executeTool", target: ["gen_ai.tool.name"] },
  "mcp.request": { name: "mcpRequest", target: ["pr.tool.title", "gen_ai.tool.name", "mcp.method.name"] },
  "http.request": { name: "httpRequest" },
};

export interface TraceSpanNameInput {
  operationName: string;
  status: TraceSpanStatus;
  root: boolean;
  attributes: Readonly<Record<string, unknown>>;
}

/**
 * span の行の名前（3 製品の 9 画面で共通の規則。#3219）。root は「実行の全体」、段（`pr.stage`）は製品の段の名前
 * （処理の経過と同じ。無ければ `pr.stage` の値）、`gen_ai.*`・MCP・HTTP は日本語の名前に対象を添え、
 * それ以外は `operation_name`。2 回目以降の試行（`pr.attempt`）は回数を付ける。元の名前は詳細の `Span name` に残す。
 */
export function traceSpanDisplayName(span: TraceSpanNameInput, labels: TraceDetailsLabels = DEFAULT_TRACE_DETAILS_LABELS): string {
  const names = labels.spanNames;
  const stage = textAttribute(span.attributes, "pr.stage");
  let name: string;
  if (span.root) {
    name = names.root;
  } else if (stage) {
    name = (labels.stageName?.(stage, span.status)?.trim() || names.other).slice(0, 128);
  } else {
    const technical = TECHNICAL_SPANS[span.operationName] ?? (span.operationName.startsWith("gen_ai.") ? { name: "genAi" as const, target: ["gen_ai.request.model"] } : undefined);
    if (!technical) return names.other;
    const target = technical.target?.map((key) => textAttribute(span.attributes, key)).find(Boolean);
    const purpose = ["gen_ai.chat", "gen_ai.embeddings", "gen_ai.rerank"].includes(span.operationName) ? textAttribute(span.attributes, "pr.gen_ai.purpose") : undefined;
    const purposeName = purpose ? labels.modelPurposes?.[purpose] : undefined;
    name = purposeName || (span.operationName === "gen_ai.chat" ? names.chat : target ? `${names[technical.name]} · ${target.slice(0, 128)}` : names[technical.name]);
  }
  const attempt = span.attributes["pr.attempt"];
  return typeof attempt === "number" && Number.isInteger(attempt) && attempt > 1 ? names.attempt(name, attempt) : name;
}

/**
 * MCP の手続きの span（OpenTelemetry の MCP の semantic conventions の `mcp.method.name` が `tools/call` 以外。
 * `initialize`・`notifications/*`・`tools/list` など。#3272）。利用者向けの処理の経過の木には出さず（子は親の位置に
 * 上げる）、技術の trace の一覧（`traceTimeline`）には残す。method の無い古い記録は今までどおり出す。
 */
export function isMcpProtocolSpan(attributes: Readonly<Record<string, unknown>>): boolean {
  const method = textAttribute(attributes, "mcp.method.name");
  return method !== undefined && method !== "tools/call";
}

/**
 * span の技術の値（詳細の見出しと値）の allowlist。識別子（段・モデル・ツール・method・試行）→ 利用量 → 入力の文脈の記録
 * （見積もり・予算・圧縮・指紋。#2583。Agent のモデルの呼び出しだけが持つ）→ エラーの種類の順に出す。
 */
const TECHNICAL_FIELDS = ["pr.stage", "gen_ai.request.model", "gen_ai.tool.name", "mcp.method.name", "pr.attempt", ...Object.keys(TRACE_FIELD_LABEL_KEYS), "error.type"];
const CACHE_READ_FIELD = "gen_ai.usage.cache_read.input_tokens";

function technicalFieldLabel(key: string, labels: TraceDetailsLabels): string {
  return Object.prototype.hasOwnProperty.call(TRACE_FIELD_LABEL_KEYS, key) ? labels.fieldNames[key as TraceFieldName] : key;
}

/**
 * span の詳細の技術の値（3 製品共通）。行の名前は日本語にし、実測の記録を引けるように元の span の名前を残す。
 * 利用量と入力の文脈の記録は日本語の見出し、識別子は属性名のまま。値は数値も文字列もそのまま（属性の無い値は出さない）。
 * `gen_ai.chat` の利用量が無いときは 0 にせず「未計測」、利用量はあるのにキャッシュから読んだ入力が無い（プロバイダが
 * 返さない）ときは 0 と区別して「不明」と出す。
 */
export function traceSpanFields(
  spanId: string | undefined,
  operationName: string,
  attributes: Readonly<Record<string, unknown>>,
  labels: TraceDetailsLabels = DEFAULT_TRACE_DETAILS_LABELS
): { label: string; value: string; mono: boolean; technical?: boolean }[] {
  const fields: { label: string; value: string; mono: boolean; technical?: boolean }[] = [];
  if (spanId) fields.push({ label: "Span ID", value: spanId, mono: true });
  fields.push({ label: "Span name", value: operationName, mono: true });
  const chat = operationName === "gen_ai.chat";
  for (const key of TECHNICAL_FIELDS) {
    const v = attributes[key];
    const label = technicalFieldLabel(key, labels);
    if (typeof v === "string" || (typeof v === "number" && Number.isFinite(v))) fields.push({ label, value: String(v).slice(0, 128), mono: true });
    else if (chat && v == null && (key === "gen_ai.usage.input_tokens" || key === "gen_ai.usage.output_tokens")) fields.push({ label, value: labels.unknown, mono: false });
    // 利用量そのものが無い呼び出しは「未計測」にそろえ、利用量はあるのにキャッシュの値が無いときだけ「不明」。
    else if (chat && v == null && key === CACHE_READ_FIELD) fields.push({ label, value: attributes["gen_ai.usage.input_tokens"] == null ? labels.unknown : labels.notReported, mono: false });
  }
  return fields.map(field => ({...field, technical: true}));
}

/** 外部の summary を検証し、親子関係を有限回で並べる。本文や任意の属性は描画しない。 */
export function traceTimeline(value: unknown, labels: TraceDetailsLabels = DEFAULT_TRACE_DETAILS_LABELS): { steps: RunTimelineStep[]; partial: boolean; traceId?: string } {
  const empty = { steps: [], partial: true };
  if (!value || typeof value !== "object") return empty;
  const data = value as Record<string, unknown>;
  if (!Array.isArray(data.spans) || (data.schema_version != null && data.schema_version !== 1)) return empty;
  const spans = new Map<string, Record<string, unknown>>();
  let partial = data.completeness !== "complete" || data.truncated === true || data.spans.length > 1000;
  for (const raw of data.spans.slice(0, 1000)) {
    if (!raw || typeof raw !== "object") { partial = true; continue; }
    const span = raw as Record<string, unknown>;
    if (typeof span.span_id !== "string" || !/^[0-9a-f]{16}$/.test(span.span_id) || spans.has(span.span_id) ||
        typeof span.trace_id !== "string" || !/^[0-9a-f]{32}$/.test(span.trace_id) ||
        typeof span.operation_name !== "string" || !["done", "failed", "cancelled", "skipped"].includes(String(span.status))) {
      partial = true; continue;
    }
    spans.set(span.span_id, span);
  }
  const ordered = [...spans.values()].sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)));
  const visited = new Set<string>();
  const steps: RunTimelineStep[] = [];
  const pending: { span: Record<string, unknown>; level: number }[] = [];
  function emit(span: Record<string, unknown>, level: number) {
    const id = String(span.span_id);
    if (visited.has(id)) { partial = true; return; }
    visited.add(id);
    const attributes = span.attributes && typeof span.attributes === "object" ? span.attributes as Record<string, unknown> : {};
    const operationName = String(span.operation_name).slice(0, 128);
    const label = traceSpanDisplayName({
      operationName,
      status: span.status as TraceSpanStatus,
      root: data.root_span_id != null ? id === data.root_span_id : !span.parent_span_id,
      attributes,
    }, labels);
    const fields = traceSpanFields(id, operationName, attributes, labels);
    if (typeof span.parent_span_id === "string") fields.push({ label: "Parent span ID", value: span.parent_span_id, mono: true, technical: true });
    steps.push({ id, label, level, operationName,
      parentId: typeof span.parent_span_id === "string" ? span.parent_span_id : undefined,
      status: span.status === "cancelled" ? "skipped" : span.status as "done" | "failed" | "skipped",
      detail: span.status === "cancelled" ? labels.cancelled : undefined,
      startedAt: typeof span.started_at === "string" ? span.started_at : null,
      finishedAt: typeof span.finished_at === "string" ? span.finished_at : null,
      durationMs: typeof span.duration_ms === "number" && Number.isFinite(span.duration_ms) && span.duration_ms >= 0 ? span.duration_ms : null,
      details: { fields },
    });
    const children = ordered.filter(child => child.parent_span_id === id);
    for (const child of children.reverse()) pending.push({ span: child, level: level + 1 });
  }
  const roots = ordered.filter(span => !spans.has(String(span.parent_span_id)));
  for (const root of roots) {
    if (root.span_id !== data.root_span_id && root.parent_span_id) partial = true;
    pending.push({ span: root, level: 0 });
    while (pending.length) { const item = pending.pop()!; emit(item.span, item.level); }
  }
  // cycle や孤立した記録も捨てずに出し、完全な記録とは表示しない。
  for (const span of ordered) if (!visited.has(String(span.span_id))) {
    partial = true; pending.push({ span, level: 0 });
    while (pending.length) { const item = pending.pop()!; emit(item.span, item.level); }
  }
  return { steps, partial, traceId: typeof data.telemetry_trace_id === "string" && /^[0-9a-f]{32}$/.test(data.telemetry_trace_id) ? data.telemetry_trace_id : undefined };
}
