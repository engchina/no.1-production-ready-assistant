// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import { RunStepDetailsView } from "../src/components/data/run-step-details";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
it("選択した内容だけ表示し、未記録と制限・切り詰めを区別する", () => {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  try {
    act(() => root.render(<RunStepDetailsView details={{ fields: [{ label: "モデル", value: "model-a" }], call: {
      input: { status: "recorded", text: "synthetic-input" },
      output: { status: "withheld", text: "private-output" },
      schema: { status: "truncated", text: "synthetic-schema" },
    } }} timing={{ startedAt: "2026-01-01T00:00:00Z", durationMs: 42 }} />));
    const click = (label: string) => act(() => (Array.from(host.querySelectorAll<HTMLButtonElement>('[role="tab"]')).find(el => el.textContent === label))!.click());
    expect(host.textContent).toContain("synthetic-input");
    click("概要"); expect(host.textContent).toContain("model-a");
    click("入力"); expect(host.textContent).toContain("synthetic-input");
    click("結果"); expect(host.textContent).toContain("現在の権限"); expect(host.textContent).not.toContain("private-output");
    click("Schema"); expect(host.textContent).toContain("synthetic-schema"); expect(host.textContent).toContain("先頭だけ");
    click("計時"); expect(host.textContent).toContain("0.1 秒未満"); expect(host.textContent).toContain("記録されていません");
  } finally { act(() => root.unmount()); host.remove(); }
});


it("思考と回答を JSON に隠さず本文として別に描く", () => {
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  const saved = (value: unknown) => ({ status: "recorded" as const, text: JSON.stringify(value) });
  try {
    act(() => root.render(<RunStepDetailsView details={{ call: {
      input: saved({ input: [{ role: "system", content: "合成の指示" }, { role: "user", content: "合成の質問" }] }),
      output: saved({ output: [{ type: "reasoning", content: [{ type: "reasoning_text", text: "合成の思考" }] }, { type: "message", content: [{ type: "output_text", text: "合成の回答" }] }] }), schema: saved({}),
    } }} />));
    const click = (label: string) => act(() => Array.from(host.querySelectorAll<HTMLButtonElement>('[role="tab"]')).find(el => el.textContent === label)!.click());
    expect(host.querySelector('[data-call-readable]')?.textContent).toContain("合成の質問");
    click("思考"); expect(host.querySelector('[data-call-readable]')?.textContent).toContain("合成の思考");
    expect(host.querySelector('[data-call-readable]')?.textContent).not.toContain("合成の回答");
    expect(host.querySelector('[data-call-readable] pre')).toBeNull();
    expect(host.querySelector('[data-call-readable] details[open]')?.textContent).toContain("合成の思考");
    click("結果"); expect(host.querySelector('[data-call-readable]')?.textContent).toContain("合成の回答");
  } finally { act(() => root.unmount()); host.remove(); }
});

it("ベクトルの数値を初期表示で折りたたみ、記録済みの次元を示す", () => {
  const host=document.createElement("div"); document.body.append(host); const root=createRoot(host);
  const saved=(value:unknown)=>({status:"recorded" as const,text:JSON.stringify(value)});
  try {
    act(()=>root.render(<RunStepDetailsView details={{call:{input:saved({inputs:["合成の質問"]}),output:saved({embeddings:[[0.1,0.2,0.3]]}),schema:saved({})}}} />));
    act(()=>Array.from(host.querySelectorAll<HTMLButtonElement>('[role="tab"]')).find(el=>el.textContent==="結果")!.click());
    expect(host.querySelector('[data-call-semantic]')?.textContent).toContain("記録されたベクトル: 1");
    expect(host.querySelector('[data-call-semantic]')?.textContent).toContain("記録された次元数: 3");
    expect(host.querySelector('[data-call-semantic] details')?.hasAttribute("open")).toBe(false);
    expect(host.querySelector('[data-call-semantic] [data-language]')).toBeNull();
  } finally {act(()=>root.unmount());host.remove();}
});

it("設定で止めた本文を未記録と区別する", () => {
  const host=document.createElement("div"); const root=createRoot(host);
  const empty={status:"recorded" as const,text:"private-synthetic-disabled"};
  try {
    act(()=>root.render(<RunStepDetailsView details={{call:{capture_enabled:false,input:empty,output:empty,schema:empty}}} />));
    expect(host.textContent).toContain("本文の記録は設定で無効");
    expect(host.querySelector('[data-call-readable]')).toBeNull();
    expect(host.textContent).not.toContain("private-synthetic-disabled");
  } finally {act(()=>root.unmount());}
});
