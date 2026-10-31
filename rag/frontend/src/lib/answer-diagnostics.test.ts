import { describe, expect, it } from "vitest";

import {
  answerAttachedImages,
  unreadSectionLinks,
  confidenceVariant,
  outcomeBadge,
  parseAnswerDiagnostics,
  parseRunStepDetails,
} from "./answer-diagnostics";

describe("parseAnswerDiagnostics", () => {
  it("回答フローの診断を表示用に正規化する", () => {
    const parsed = parseAnswerDiagnostics({
      confidence: "high",
      needs_human_review: false,
      insufficient_reason: "",
      reasoning_summary: "引用照合済みの説明 2 件。",
      external_data_required: true,
      external_data_items: ["対象の受注の登録状態", ""],
      question_type: ["操作方法"],
      auto_field_filter: {
        conditions: [
          { name: "金額", value_type: "number", op: "gte", value: "100000" },
          { name: "壊れた条件", value_type: "number", op: "between", value: "1" },
        ],
        relaxed: true,
      },
      rewritten_question: "受注入力画面での受注の登録方法は？",
      generated_queries: ["受注 登録"],
      models: {
        llm: { model_id: "model-a", label: "Model A" },
        attached_images: 2,
        embedding: "cohere.embed-v4.0",
        rerank: "",
      },
      execution_steps: [
        {
          name: "質問の理解",
          status: "complete",
          elapsed_seconds: 0.2,
          llm_calls: 1,
        },
      ],
      evidence_tree: [
        {
          parent_id: "doc-1:p1",
          source: "manual.pdf",
          page: 1,
          children: [
            {
              chunk_id: "doc-1:c1",
              role: "retrieved_anchor",
              is_model_used: true,
              answer_use: "reference",
              page: 1,
            },
          ],
        },
      ],
    });
    expect(parsed?.rewrittenQuestion).toBe("受注入力画面での受注の登録方法は？");
    expect(parsed?.reasoningSummary).toBe("引用照合済みの説明 2 件。");
    expect(parsed?.externalDataRequired).toBe(true);
    expect(parsed?.externalDataItems).toEqual(["対象の受注の登録状態"]);
    expect(parsed?.questionType).toEqual(["操作方法"]);
    expect(parsed?.autoFieldFilter).toEqual({
      conditions: [{ name: "金額", value_type: "number", op: "gte", value: "100000" }],
      relaxed: true,
    });
    expect(parsed?.models).toEqual({
      llm: { modelId: "model-a", label: "Model A" },
      attachedImages: 2,
      embedding: "cohere.embed-v4.0",
      rerank: "",
    });
    expect(parsed?.steps[0]).toEqual({
      name: "質問の理解",
      status: "complete",
      elapsedSeconds: 0.2,
      llmCalls: 1,
    });
    expect(parsed?.tree[0].children[0]).toEqual({
      chunkId: "doc-1:c1",
      role: "retrieved_anchor",
      modelUsed: true,
      // 回答での使われ方（#2040）。
      answerUse: "reference",
      page: 1,
    });
  });

  // #737
  it("承認済み FAQ から回答したときは、FAQ の原文を出典として読む", () => {
    expect(
      parseAnswerDiagnostics({
        answer_source: "approved_faq",
        approved_faq_question: "出張の日当はいくらですか？",
        approved_faq_answer: "一般は 1 泊 2,000 円です。",
      })?.approvedFaq,
    ).toEqual({ question: "出張の日当はいくらですか？", answer: "一般は 1 泊 2,000 円です。" });
    // 変更前に保存した回答は本文を持たない。
    expect(
      parseAnswerDiagnostics({ answer_source: "approved_faq", approved_faq_question: "Q" })?.approvedFaq,
    ).toEqual({ question: "Q", answer: "" });
  });

  // 関係情報で加えた根拠（Issue 2055）。
  it("関係情報で加えた根拠の数と使った見出しを読む", () => {
    expect(
      parseAnswerDiagnostics({
        graph_expansion: {
          added_count: 3,
          entities: [
            { entity: "手順書 > 第 2 章 作業の準備", added_count: 2 },
            { entity: "", added_count: 1 },
            { entity: "手順書 > 第 4 章 作業の記録", added_count: 1 },
          ],
        },
      })?.graphExpansion,
    ).toEqual({
      status: "completed",
      graphAvailable: null,
      plannedCount: 3,
      skippedExistingCount: 0,
      addedCount: 3,
      entities: [
        { name: "手順書 > 第 2 章 作業の準備", addedCount: 2 },
        { name: "手順書 > 第 4 章 作業の記録", addedCount: 1 },
      ],
    });
    // 0 件でも実行の記録は残し、記録のない回答と区別する。
    expect(parseAnswerDiagnostics({ graph_expansion: { added_count: 0 } })?.graphExpansion).toMatchObject({ addedCount: 0, status: "completed" });
    expect(parseAnswerDiagnostics({})?.graphExpansion).toBeNull();
  });

  it("回答フローの診断が無ければ null、信頼度を variant に写す", () => {
    expect(parseAnswerDiagnostics(null)).toBeNull();
    // モデルを持たない古い回答の記録でも壊れない。
    expect(parseAnswerDiagnostics({})?.autoFieldFilter).toBeNull();
    expect(parseAnswerDiagnostics({})?.models).toEqual({ llm: null, attachedImages: 0, embedding: "", rerank: "" });
    expect(parseAnswerDiagnostics({})?.approvedFaq).toBeNull();
    expect(confidenceVariant("high")).toBe("success");
    expect(confidenceVariant("low")).toBe("danger");
    expect(confidenceVariant("")).toBe("neutral");
  });
});

describe("回答の対応と業務ガイド（issue 1252）", () => {
  it("回答の対応と業務ガイドを読み、未知の値・欠けた値は null にする", () => {
    const parsed = parseAnswerDiagnostics({
      outcome: "needs_clarification",
      guide: { guide_id: "g1", title: "権限の付与", revision: 2 },
    });
    expect(parsed?.outcome).toBe("needs_clarification");
    // 条件を持たない古い記録（#1238）も、名前と版だけで読む。
    expect(parsed?.guide).toEqual({
      guideId: "g1",
      title: "権限の付与",
      revision: 2,
      known: [],
      unresolved: [],
      applicability: [],
    });
    expect(parsed?.guideLoadFailed).toBe(false);
    expect(parseAnswerDiagnostics({ outcome: "other", guide: { title: "x" } })).toMatchObject({
      outcome: null,
      guide: null,
    });
    expect(parseAnswerDiagnostics({})).toMatchObject({ outcome: null, guide: null });
  });

  it("業務ガイドの条件の状態（分かっている・分からない・候補が複数）と適用範囲を読む（issue 1287）", () => {
    const parsed = parseAnswerDiagnostics({
      outcome: "conditional",
      guide: {
        guide_id: "g1",
        revision: 3,
        title: "権限の付与",
        decision: "branch",
        known_conditions: [
          { id: "role", label: "ロール", value: "管理者", source: "question", state: "known" },
          { id: "tenant", label: "テナント", value: "本社", source: "user", state: "known" },
          { id: "kind", label: "項目の種類", value: "選択", source: "document", state: "known" },
          { id: "", label: "", value: "x" },
        ],
        unknown_conditions: [
          { id: "plan", label: "契約プラン", handling: "branch", state: "unknown", candidates: [] },
          {
            id: "version",
            label: "製品の版",
            handling: "ask",
            state: "conflicting",
            candidates: ["v1", "v2"],
          },
          // 古い記録（#1238）は state を持たない。候補が 1 つだけの矛盾も「分からない」に寄せる。
          { id: "region", label: "地域" },
          { id: "os", label: "OS", handling: "other", state: "conflicting", candidates: ["Linux"] },
        ],
        applicability: { business_domains: "matched", versions: "unverified", other: "matched" },
      },
    });
    expect(parsed?.guide).toEqual({
      guideId: "g1",
      title: "権限の付与",
      revision: 3,
      known: [
        { id: "role", label: "ロール", value: "管理者", source: "question" },
        { id: "tenant", label: "テナント", value: "本社", source: "user" },
        { id: "kind", label: "項目の種類", value: "選択", source: "document" },
      ],
      unresolved: [
        { id: "plan", label: "契約プラン", state: "unknown", candidates: [], handling: "branch" },
        { id: "version", label: "製品の版", state: "conflicting", candidates: ["v1", "v2"], handling: "ask" },
        { id: "region", label: "地域", state: "unknown", candidates: [], handling: null },
        { id: "os", label: "OS", state: "unknown", candidates: [], handling: null },
      ],
      applicability: [
        { key: "business_domains", state: "matched" },
        { key: "versions", state: "unverified" },
      ],
    });
  });

  it("公開の業務ガイドを読み込めなかった記録を読む（issue 1287）", () => {
    expect(parseAnswerDiagnostics({ guide_load_failed: true })).toMatchObject({
      guide: null,
      guideLoadFailed: true,
    });
    expect(parseAnswerDiagnostics({ guide_load_failed: "true" })?.guideLoadFailed).toBe(false);
  });

  it("「答えた」以外の対応だけをバッジにする", () => {
    expect(outcomeBadge("answered")).toBeNull();
    expect(outcomeBadge(null)).toBeNull();
    expect(outcomeBadge("insufficient_evidence")).toEqual({
      variant: "danger",
      key: "search.answerDetails.outcome.insufficient_evidence",
    });
    expect(outcomeBadge("needs_clarification")?.variant).toBe("info");
    expect(outcomeBadge("conditional")?.variant).toBe("warning");
  });
});


describe("unreadSectionLinks", () => {
  it("回答に未反映の関連章節の行と、その章節の文書・頁を読む", () => {
    const line = "「a.pdf」の「第2章」（p.3〜5）は、読み込み量の上限に達したため未反映です。";
    expect(
      unreadSectionLinks({
        section_expansion: {
          unread: [
            { document_id: "doc-1", section_id: "s2", label: "「a.pdf」の「第2章」（p.3〜5）", page: 3, line },
            // 本文の行か文書の分からない記録は使わない。
            { document_id: "doc-1", section_id: "s3", label: "x" },
            { section_id: "s4", line: "行" },
          ],
        },
      })
    ).toEqual([{ line, label: "「a.pdf」の「第2章」（p.3〜5）", documentId: "doc-1", page: 3 }]);
    expect(unreadSectionLinks(null)).toEqual([]);
  });
});

// 処理の経過の段の詳細（#2341）。
describe("parseRunStepDetails", () => {
  it("見出しと値・行・省略の印を読み、値の無い見出しと空の行は捨てる", () => {
    expect(
      parseRunStepDetails({
        fields: [
          { label: "検索の候補", value: "4 件" },
          { label: "空", value: "" },
          { label: "ID", value: "id-1", mono: true },
          "壊れた値",
        ],
        lines: ["1. 文書 A", "", 3],
        truncated: true,
      })
    ).toEqual({
      fields: [
        { label: "検索の候補", value: "4 件" },
        { label: "ID", value: "id-1", mono: true },
      ],
      lines: ["1. 文書 A"],
      truncated: true,
    });
  });

  it("旧い記録の行の配列は行として読み、中身が無ければ undefined", () => {
    expect(parseRunStepDetails(["検索文: 2 本", ""])).toEqual({ lines: ["検索文: 2 本"] });
    expect(parseRunStepDetails([])).toBeUndefined();
    expect(parseRunStepDetails({ fields: [], lines: [], truncated: true })).toBeUndefined();
    expect(parseRunStepDetails(null)).toBeUndefined();
    expect(parseRunStepDetails("行")).toBeUndefined();
  });

  it("回答の記録の工程の詳細を読む", () => {
    const diagnostics = parseAnswerDiagnostics({
      execution_steps: [
        { name: "文書検索", status: "complete", details: { fields: [{ label: "結果", value: "根拠 2 件" }] } },
        { name: "質問の理解", status: "complete" },
      ],
    });
    expect(diagnostics?.steps.map((step) => step.details)).toEqual([
      { fields: [{ label: "結果", value: "根拠 2 件" }] },
      undefined,
    ]);
  });
});

// #3444
describe("answerAttachedImages", () => {
  it("作成した直後は配信の診断、保存した回答は会話のメッセージから、回答モデルへ添付した画像の件数を読む", () => {
    expect(answerAttachedImages({ models: { llm: { model_id: "m1" }, attached_images: 2 } }, null)).toBe(2);
    expect(answerAttachedImages(null, { answer_attached_images: 3 })).toBe(3);
  });

  it("画像を添付しなかった回答・記録の無い回答・形の違う値は 0", () => {
    expect(answerAttachedImages({ models: { llm: { model_id: "m1" }, attached_images: 0 } }, null)).toBe(0);
    expect(answerAttachedImages(null, { answer_attached_images: 0 })).toBe(0);
    expect(answerAttachedImages(null, { answer_attached_images: "2" })).toBe(0);
    expect(answerAttachedImages(null, { answer_attached_images: -1 })).toBe(0);
    expect(answerAttachedImages(null, null)).toBe(0);
    expect(answerAttachedImages(undefined, { content: "回答" })).toBe(0);
  });
});
