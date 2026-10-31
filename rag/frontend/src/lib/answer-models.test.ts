// 回答するモデルの名前と説明（#675。画像対応モデルを兼ねる 1 件は #888）。
import { describe, expect, it } from "vitest";

import { answerModelHelpKey, answerModelLabel } from "./answer-models";
import type { CompareModel } from "./api";

const text: CompareModel = { model_id: "text-m", display_name: "gpt-oss-120b", kind: "text" };
const vision: CompareModel = { model_id: "vision-m", display_name: "grok-4.3", kind: "vision" };
const both: CompareModel = { model_id: "both-m", display_name: "xai.grok-4.3", kind: "text_vision" };

describe("answerModelLabel", () => {
  it("役割をモデル名の後ろに出す", () => {
    expect(answerModelLabel(text)).toBe("gpt-oss-120b（テキスト）");
    expect(answerModelLabel(vision)).toBe("grok-4.3（画像対応）");
  });

  it("テキストと画像対応を兼ねるモデルは両方の役割を出す", () => {
    expect(answerModelLabel(both)).toBe("xai.grok-4.3（テキスト・画像対応）");
  });
});

describe("answerModelHelpKey", () => {
  it("候補が 2 件なら画面ごとの説明", () => {
    expect(answerModelHelpKey([text, vision], "search.answerModel.help")).toBe("search.answerModel.help");
    expect(answerModelHelpKey([text, vision], "search.answerModel.help")).toBe(
      "search.answerModel.help"
    );
  });

  it("テキストモデルだけなら「テキストモデルで回答します」", () => {
    expect(answerModelHelpKey([text], "search.answerModel.help")).toBe("search.answerModel.helpTextOnly");
  });

  it("画像対応モデルを兼ねる 1 件なら、画像も読むことを伝える", () => {
    expect(answerModelHelpKey([both], "search.answerModel.help")).toBe(
      "search.answerModel.helpTextVision"
    );
  });
});
