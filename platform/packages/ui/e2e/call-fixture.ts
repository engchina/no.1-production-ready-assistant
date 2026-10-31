/** 呼び出しの詳細の合成データ（3 製品で同じ契約を確認）。 */
export const syntheticCallDetails = {
  visibility: "owner",
  input: { status: "recorded", text: JSON.stringify({ model: "synthetic-model", input: [{ role: "system", content: "synthetic-system-instructions" }, { role: "user", content: "synthetic-call-input" }] }) },
  output: { status: "recorded", text: JSON.stringify({ model: "synthetic-model", output: [{ type: "reasoning", content: [{ type: "reasoning_text", text: "synthetic-call-thinking" }] }, { type: "message", role: "assistant", content: [{ type: "output_text", text: "synthetic-call-output" }] }], usage: { input_tokens: 10, output_tokens: 20 } }) },
  schema: { status: "truncated", text: '{"type":"object","title":"synthetic-call-schema"}' },
};

/** 工程の本文と時刻の境界を確認する合成の詳細。 */
export const syntheticStageDetails = {
  fields: [{label: "目的", value: "合成の工程"}, {label: "結果", value: "2 件の資料を参照しました"}, {label: "影響", value: "参照資料で回答を作ります"}],
  lines: ["synthetic-stage-detail", "合成の項目 B"],
};
