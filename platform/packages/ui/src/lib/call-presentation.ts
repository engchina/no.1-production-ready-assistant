/** 記録済みの要求・応答を本文と返却された思考へ投影する。未記録の内容は補完しない。 */
import type { CallContent, CallDetails } from "../components/data/run-step-details";

export interface CallTextBlock { role: string; text: string; collapsed?: boolean }
type Value = Record<string, unknown>;
const record = (value: unknown): Value => value != null && typeof value === "object" && !Array.isArray(value) ? value as Value : {};
const text = (value: unknown): string => typeof value === "string" ? value : "";
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
function parse(content: CallContent): unknown {
  if (content.status !== "recorded" && content.status !== "truncated") return undefined;
  try { return JSON.parse(content.text); } catch { return undefined; }
}
function contentText(value: unknown): string {
  if (typeof value === "string") return value;
  return array(value).flatMap(item => {
    const block = record(item);
    return ["text", "input_text", "output_text", "reasoning_text", "summary_text"].includes(text(block.type)) && typeof block.text === "string" ? [block.text] : [JSON.stringify(item, null, 2)];
  }).join("\n\n");
}
function block(value: unknown, fallbackRole: string): CallTextBlock | null {
  if (typeof value === "string") return { role: fallbackRole, text: value };
  const node = record(value);
  const body = contentText(node.content);
  if (body) return { role: text(node.role) || fallbackRole, text: body };
  if (Object.keys(node).length) return { role: text(node.role) || text(node.type) || fallbackRole, text: JSON.stringify(node, null, 2) };
  return null;
}
export function callPresentation(call: CallDetails) {
  const request = call.capture_enabled === false ? {} : record(parse(call.input));
  const response = call.capture_enabled === false ? {} : record(parse(call.output));
  const input: CallTextBlock[] = [];
  const instructions = request.instructions ?? request.system;
  const system = contentText(instructions);
  if (system && system !== "[unsupported value]") input.push({ role: "system", text: system });
  const messages = request.input ?? request.messages;
  if (typeof messages === "string") input.push({ role: "user", text: messages });
  else for (const message of array(messages)) { const value = block(message, "user"); if (value) input.push(value); }
  const embeddingInputs = request.inputs ?? request.embed_contents;
  if (!input.length && Array.isArray(embeddingInputs)) for (const value of embeddingInputs) {
    const item = block(value, `input ${input.length + 1}`); if (item) input.push(item);
  }
  if (Array.isArray(request.documents)) for (const value of request.documents) {
    const item = block(value, `document ${input.length}`); if (item) input.push(item);
  }
  const output: CallTextBlock[] = [];
  const reasoning: CallTextBlock[] = [];
  for (const value of array(response.output)) {
    const node = record(value);
    if (node.type === "reasoning") {
      const body = contentText(node.content); const summary = contentText(node.summary);
      if (body) reasoning.push({ role: "reasoning", text: body });
      if (summary) reasoning.push({ role: "summary", text: summary });
    } else { const item = block(value, "assistant"); if (item) output.push(item); }
  }
  for (const value of array(response.choices)) {
    const message = record(record(value).message);
    const thought = text(message.reasoning_content) || text(message.reasoning);
    if (thought) reasoning.push({ role: "reasoning", text: thought });
    const body = contentText(message.content); if (body) output.push({ role: "assistant", text: body });
  }
  for (const value of array(response.content)) {
    const node = record(value);
    if (node.type === "thinking" && typeof node.thinking === "string") reasoning.push({ role: "reasoning", text: node.thinking });
    if (node.type === "text" && typeof node.text === "string") output.push({ role: "assistant", text: node.text });
  }
  if (!output.length && typeof response.output_text === "string") output.push({ role: "assistant", text: response.output_text });
  if (!output.length && typeof response.output === "string") output.push({ role: "assistant", text: response.output });
  const vectors = Array.isArray(response.embeddings) ? response.embeddings : array(record(response.embeddings_by_type).float).length ? array(record(response.embeddings_by_type).float) : array(response.data).map(value => record(value).embedding).filter(Array.isArray);
  const embeddings = vectors.filter(Array.isArray).map((values, index) => ({
    index, dimensions: values.filter(value => typeof value === "number").length,
    partial: values.some(value => typeof value !== "number"),
    values: JSON.stringify(values),
  }));
  const documents = array(request.documents);
  const ranks = array(response.document_ranks ?? response.results).map((value, rank) => {
    const node = record(value);
    const index = typeof node.index === "number" ? node.index : null;
    const document = index != null ? documents[index] : node.document;
    return { rank: rank + 1, index, score: typeof node.relevance_score === "number" ? node.relevance_score : null,
      text: typeof document === "string" ? document : document == null ? "" : text(record(document).text) || JSON.stringify(document, null, 2) };
  });
  const usage = record(response.usage);
  const stats = {
    model: response.model ?? request.model ?? record(request.serving_mode).model_id,
    inputTokens: usage.input_tokens ?? usage.prompt_tokens,
    outputTokens: usage.output_tokens ?? usage.completion_tokens,
    reasoningTokens: record(usage.output_tokens_details).reasoning_tokens ?? record(usage.completion_tokens_details).reasoning_tokens,
    maxOutput: request.max_output_tokens ?? request.max_tokens,
    temperature: request.temperature,
    effort: record(request.reasoning).effort,
    requestedDimensions: request.output_dimensions ?? request.dimensions,
    inputType: request.input_type,
    topN: request.top_n,
  };
  return { input, output, reasoning, stats, embeddings, ranks, partial: ["in_progress", "interrupted", "incomplete", "failed"].includes(text(response.status)) };
}
