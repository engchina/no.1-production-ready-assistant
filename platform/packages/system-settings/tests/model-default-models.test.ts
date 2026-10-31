import { describe, expect, it } from "vitest";

import {
  JUDGE_SAME_AS_ANSWER,
  followModelChange,
  judgeModelOptions,
  textModelOptions,
  validateDefaultModels,
  validateModelIds,
  visionModelOptions,
} from "../src/model/defaultModels";
import type { EnterpriseAiConfiguredModel } from "../src/model/types";

const models: EnterpriseAiConfiguredModel[] = [
  { model_id: "llm-a", display_name: "標準", vision_enabled: false },
  { model_id: "vlm-b", display_name: "", vision_enabled: true },
  { model_id: "  ", display_name: "未入力", vision_enabled: true },
];

describe("validateDefaultModels（#499。backend の validate_default_models と同じ規則）", () => {
  it("正しい選択はエラーにしない", () => {
    expect(
      validateDefaultModels({
        models,
        default_text_model_id: "llm-a",
        default_vision_model_id: "vlm-b",
      }),
    ).toEqual({});
    // テキストに Vision 対応のモデルを選んでもよい。
    expect(
      validateDefaultModels({
        models,
        default_text_model_id: "vlm-b",
        default_vision_model_id: "vlm-b",
      }),
    ).toEqual({});
  });

  it("モデルを登録していなければ既定のモデルは必須ではない", () => {
    expect(
      validateDefaultModels({
        models: [{ model_id: "", display_name: "", vision_enabled: false }],
        default_text_model_id: "",
        default_vision_model_id: "",
      }),
    ).toEqual({});
  });

  it("テキストの未選択・削除をエラーにする（#566。テキストも必須）", () => {
    expect(
      validateDefaultModels({
        models,
        default_text_model_id: "",
        default_vision_model_id: "vlm-b",
      }),
    ).toEqual({
      default_text_model_id: "既定のテキストモデルを選択してください。",
    });
    expect(
      validateDefaultModels({
        models,
        default_text_model_id: "gone",
        default_vision_model_id: "vlm-b",
      }),
    ).toEqual({
      default_text_model_id: expect.stringContaining("「gone」は登録モデルにありません"),
    });
  });

  it("Vision 対応のモデルがない・未選択・削除・Vision 対応でないをエラーにする", () => {
    const textOnly = [models[0]!];
    expect(
      validateDefaultModels({
        models: textOnly,
        default_text_model_id: "",
        default_vision_model_id: "",
      }).default_vision_model_id,
    ).toContain("画像入力に対応したモデルがありません");
    expect(
      validateDefaultModels({
        models,
        default_text_model_id: "",
        default_vision_model_id: "",
      }).default_vision_model_id,
    ).toBe("既定の画像対応モデルを選択してください。");
    expect(
      validateDefaultModels({
        models,
        default_text_model_id: "",
        default_vision_model_id: "gone",
      }).default_vision_model_id,
    ).toContain("「gone」は登録モデルにありません");
    expect(
      validateDefaultModels({
        models,
        default_text_model_id: "",
        default_vision_model_id: "llm-a",
      }).default_vision_model_id,
    ).toContain("「llm-a」は画像入力に対応していません");
  });
});

describe("既定のモデルの選択肢", () => {
  it("Vision は画像入力に対応した登録モデルだけ、テキストは全登録モデル（未選択の選択肢はない）", () => {
    expect(visionModelOptions(models)).toEqual([{ value: "vlm-b", label: "vlm-b" }]);
    expect(textModelOptions(models)).toEqual([
      { value: "llm-a", label: "標準", description: "llm-a" },
      { value: "vlm-b", label: "vlm-b" },
    ]);
  });
});

describe("followModelChange", () => {
  const defaults = {
    default_text_model_id: "llm-a",
    default_vision_model_id: "vlm-b",
    default_judge_model_id: "",
    default_embedding_model_id: "",
    default_rerank_model_id: "",
  };

  it("既定に選んだモデルの ID を書き換えたら追従する（消し切ったときは追従しない）", () => {
    expect(
      followModelChange(defaults, models[1], { ...models[1]!, model_id: "vlm-c" }),
    ).toEqual({ ...defaults, default_vision_model_id: "vlm-c" });
    expect(followModelChange(defaults, models[0], { ...models[0]!, model_id: "" })).toEqual(
      defaults,
    );
  });

  it("重複した行の片方の ID を直したときは、残った行を指したまま追従しない（#1035）", () => {
    const duplicated = [models[0]!, { ...models[0]!, model_id: "llm-a-2" }];
    expect(
      followModelChange(
        defaults,
        models[0],
        { ...models[0]!, model_id: "llm-a-2" },
        duplicated,
      ),
    ).toEqual(defaults);
    // 前の ID の行が残らなければ従来どおり追従する。
    expect(
      followModelChange(defaults, models[0], { ...models[0]!, model_id: "llm-x" }, [
        { ...models[0]!, model_id: "llm-x" },
        models[1]!,
      ]),
    ).toEqual({ ...defaults, default_text_model_id: "llm-x" });
  });

  it("Vision 対応をオンにしたとき、既定の画像対応モデルが未選択ならそのモデルを選ぶ", () => {
    const empty = {
      default_text_model_id: "",
      default_vision_model_id: "",
      default_judge_model_id: "",
      default_embedding_model_id: "",
      default_rerank_model_id: "",
    };
    expect(
      followModelChange(empty, models[0], { ...models[0]!, vision_enabled: true }),
    ).toEqual({ ...empty, default_vision_model_id: "llm-a" });
    // 既に選んでいれば変えない。Vision 対応のオフでも変えない（エラーで案内する）。
    expect(
      followModelChange(defaults, models[0], { ...models[0]!, vision_enabled: true }),
    ).toEqual(defaults);
    expect(
      followModelChange(defaults, models[1], { ...models[1]!, vision_enabled: false }),
    ).toEqual(defaults);
  });
});

describe("既定の判定モデル（#3574）", () => {
  const valid = {
    models,
    default_text_model_id: "llm-a",
    default_vision_model_id: "vlm-b",
  };

  it("既定のテキストモデル（空）・回答と同じモデル・登録モデルを受け付け、登録に無い ID はエラーにする", () => {
    for (const judge of [undefined, "", JUDGE_SAME_AS_ANSWER, "llm-a", "vlm-b"]) {
      expect(validateDefaultModels({ ...valid, default_judge_model_id: judge })).toEqual({});
    }
    expect(validateDefaultModels({ ...valid, default_judge_model_id: "gone" })).toEqual({
      default_judge_model_id:
        "「gone」は登録モデルにありません。登録モデルから選び直してください。",
    });
  });

  it("選択肢は「既定のテキストモデル」（空）→「回答と同じモデル」→ 登録モデルの順", () => {
    expect(judgeModelOptions(models)).toEqual([
      { value: "", label: "既定のテキストモデル" },
      { value: JUDGE_SAME_AS_ANSWER, label: "回答と同じモデル" },
      ...textModelOptions(models),
    ]);
    // 「既定のテキストモデル」には、今の既定のテキストモデルを説明に出す。
    expect(judgeModelOptions(models, "llm-a")[0]).toEqual({
      value: "",
      label: "既定のテキストモデル",
      description: "標準",
    });
  });

  it("判定に選んだモデルの ID を書き換えたら追従し、回答と同じモデルは変えない", () => {
    const judged = {
      default_text_model_id: "llm-a",
      default_vision_model_id: "vlm-b",
      default_judge_model_id: "llm-a",
    };
    expect(
      followModelChange(judged, models[0], { ...models[0]!, model_id: "llm-c" }),
    ).toEqual({
      default_text_model_id: "llm-c",
      default_vision_model_id: "vlm-b",
      default_judge_model_id: "llm-c",
      default_embedding_model_id: "",
      default_rerank_model_id: "",
    });
    const same = { ...judged, default_judge_model_id: JUDGE_SAME_AS_ANSWER };
    expect(
      followModelChange(same, models[0], { ...models[0]!, model_id: "llm-c" })
        .default_judge_model_id,
    ).toBe(JUDGE_SAME_AS_ANSWER);
  });
});

describe("validateModelIds（#1035。backend の validate_model_ids と同じ規則）", () => {
  it("2 回目以降に現れた行だけをエラーにし、前後の空白と未入力の行は無視する", () => {
    expect(validateModelIds(models)).toEqual({});
    expect(
      validateModelIds([
        ...models,
        { model_id: " llm-a ", display_name: "", vision_enabled: false },
        { model_id: "", display_name: "", vision_enabled: false },
      ]),
    ).toEqual({
      3: "モデル ID「llm-a」はすでに登録されています。同じモデルは 1 行にまとめてください。",
    });
  });
});

describe("埋め込み / 再ランクの既定のモデル（#45）", () => {
  const kinds: EnterpriseAiConfiguredModel[] = [
    { model_id: "llm-a", display_name: "", vision_enabled: true, kind: "text" },
    { model_id: "emb", display_name: "", vision_enabled: false, kind: "embedding" },
    { model_id: "rr", display_name: "", vision_enabled: false, kind: "rerank" },
  ];
  const valid = {
    models: kinds,
    default_text_model_id: "llm-a",
    default_vision_model_id: "llm-a",
    default_embedding_model_id: "emb",
    default_rerank_model_id: "rr",
  };

  it("種類が合うモデルだけを選択肢・既定にでき、その種類を登録したら必須", () => {
    expect(validateDefaultModels(valid)).toEqual({});
    expect(textModelOptions(kinds).map((option) => option.value)).toEqual(["llm-a"]);
    expect(validateDefaultModels({ ...valid, default_text_model_id: "emb" })).toHaveProperty(
      "default_text_model_id",
    );
    expect(validateDefaultModels({ ...valid, default_embedding_model_id: "rr" })).toEqual({
      default_embedding_model_id:
        "「rr」は種類が「埋め込み」の登録モデルにありません。登録モデルから選び直してください。",
    });
    expect(validateDefaultModels({ ...valid, default_rerank_model_id: "" })).toEqual({
      default_rerank_model_id: "既定の再ランクモデルを選択してください。",
    });
    // その種類を登録しないときは検証しない（backend の .env の値のまま）。
    expect(
      validateDefaultModels({
        ...valid,
        models: [kinds[0]!],
        default_embedding_model_id: "text-embedding-v4",
        default_rerank_model_id: "",
      }),
    ).toEqual({});
  });

  it("種類を埋め込みにした行は、既定の埋め込みモデルが未選択なら選ぶ", () => {
    const next = followModelChange(
      { ...valid, default_embedding_model_id: "" },
      { model_id: "emb-2", display_name: "", vision_enabled: false, kind: "text" },
      { model_id: "emb-2", display_name: "", vision_enabled: false, kind: "embedding" },
    );
    expect(next.default_embedding_model_id).toBe("emb-2");
  });
});
