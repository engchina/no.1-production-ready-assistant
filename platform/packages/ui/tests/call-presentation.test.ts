import { expect, it } from "vitest";
import { callPresentation } from "../src/lib/call-presentation";
import type { CallDetails } from "../src/components/data/run-step-details";
const saved = (value: unknown) => ({ status: "recorded" as const, text: JSON.stringify(value) });
const call = (input: unknown, output: unknown): CallDetails => ({ input: saved(input), output: saved(output), schema: saved({}) });
it("入力の role・思考・回答・使用量を分ける", () => {
  const view = callPresentation(call({ model: "model-a", instructions: "合成の指示", input: [{ role: "user", content: [{ type: "input_text", text: "合成の質問" }] }] }, { output: [{ type: "reasoning", content: [{ type: "reasoning_text", text: "合成の思考\n\n続き" }] }, { type: "message", content: [{ type: "output_text", text: "合成の回答" }] }], usage: { input_tokens: 10, output_tokens: 20, output_tokens_details: { reasoning_tokens: 12 } } }));
  expect(view.input).toEqual([{ role: "system", text: "合成の指示" }, { role: "user", text: "合成の質問" }]);
  expect(view.reasoning[0]?.text).toBe("合成の思考\n\n続き"); expect(view.output[0]?.text).toBe("合成の回答");
  expect(view.stats).toMatchObject({ inputTokens: 10, outputTokens: 20, reasoningTokens: 12 });
});
it("Chat Completions と thinking block を解釈する", () => {
  expect(callPresentation(call({}, { choices: [{ message: { reasoning_content: "合成の思考", content: "合成の回答" } }] })).reasoning[0]?.text).toBe("合成の思考");
  expect(callPresentation(call({}, { content: [{ type: "thinking", thinking: "合成の思考" }, { type: "text", text: "合成の回答" }] })).output[0]?.text).toBe("合成の回答");
});
it("暗号化された思考・切り詰め・権限不足から本文を補完しない", () => {
  const data = call({}, { output: [{ type: "reasoning", encrypted_content: "opaque" }] });
  expect(callPresentation(data).reasoning).toEqual([]);
  data.output = { status: "truncated", text: '{"output":[' }; expect(callPresentation(data).output).toEqual([]);
  data.input = { status: "withheld", text: '{"input":"private"}' }; expect(callPresentation(data).input).toEqual([]);
});

it("embedding の記録済み次元と省略を区別し、rerank の原始の順位・分数を保つ", () => {
  const view = callPresentation(call({inputs:["合成の入力"]}, {embeddings_by_type:{float:[[0.1,0.2,"[remaining items omitted]"]]}}));
  expect(view.input[0]?.text).toBe("合成の入力");
  expect(view.embeddings[0]).toMatchObject({ dimensions:2, partial:true });
  const ranked = callPresentation(call({input:"合成の質問",documents:["合成 A","合成 B"]}, {document_ranks:[{index:1,relevance_score:0.12},{index:0,relevance_score:0.01}]}));
  expect(ranked.ranks).toEqual([{rank:1,index:1,score:0.12,text:"合成 B"},{rank:2,index:0,score:0.01,text:"合成 A"}]);
});
