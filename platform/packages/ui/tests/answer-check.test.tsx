// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AnswerCheckPanel, parseAnswerPassageRecords, type AnswerCheck } from "../src";

// #141: 資料との照合の行（RAG・Agent 共通の 5 状態）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function render(check: AnswerCheck | null) {
  act(() => root.render(<AnswerCheckPanel check={check} />));
  return host.textContent ?? "";
}

describe("AnswerCheckPanel", () => {
  it("5 つの状態を文言とアイコン付きのバッジで出す", () => {
    expect(render({ state: "verified" })).toBe("資料との照合照合済み");
    expect(render({ state: "off" })).toContain("照合オフ設定で照合を無効にしています");
    expect(render({ state: "failed", reason: "時間内に終わりませんでした" })).toContain("照合に失敗時間内に終わりませんでした");
    expect(render({ state: "skipped", reason: "回答が空のため" })).toContain("照合の対象外回答が空のため");
    expect(host.querySelector("[data-answer-check] svg")).not.toBeNull();
  });

  it("印は合計の件数と、2 種類以上なら内訳を出す", () => {
    const passages = parseAnswerPassageRecords({
      passage_marks: [
        { text: "一。", state: "unverified", reasons: [] },
        { text: "二。", state: "unverified", reasons: [] },
        { text: "三。", state: "warning", reasons: [] },
      ],
    });
    expect(render({ state: "flagged", passages })).toContain("照合で要確認 3 か所（未確認 2・要注意 1）");
    const one = parseAnswerPassageRecords({ passage_marks: [{ text: "一。", state: "unverified", reasons: [] }] });
    expect(render({ state: "flagged", passages: one })).toBe("資料との照合照合で要確認 1 か所");
    expect(render({ state: "flagged", count: 0 })).toContain("照合で要確認あり");
  });

  it("照合の記録も製品のバッジも中身も無ければ何も出さない", () => {
    expect(render(null)).toBe("");
  });
});
