import { describe, expect, it } from "vitest";

import { answerEvidenceData } from "@/lib/answer-evidence";
import { validationReview } from "@/lib/answer-review";
import { noticeHeading, structureAnswer, type StructuredBlock } from "@/lib/answer-structure";

// #2328: Agent の回答を RAG のチャットと同じ節に組み立てる（表示だけ。本文の文は書き換えない）。内容は合成のデータ。

const TITLES = { confirmed: "確認できた", unconfirmed: "確認できなかった", reference: "参考", referenceNote: "原文の説明" };
const HEADING = "資料で確認できず除いた内容";

function evidence(passages: { text: string; chunk_ids: string[] }[], markers: { text: string; chunk_ids: string[] }[] = []) {
  return answerEvidenceData({
    items: [
      { document_id: "d1", chunk_id: "c1", file_name: "文書 A.pdf", excerpt: "x", locator: { page_start: 3 }, cited: true },
      { document_id: "d2", chunk_id: "c2", file_name: "文書 B.pdf", excerpt: "y", locator: { section_path: ["第 1 章"] }, cited: true },
    ],
    passages,
    markers,
  });
}

/** 段落の照合のための原文（`raw`。#2738）を除いた形（節の組み立ての比較）。 */
function withoutRaw<T extends { raw?: string }>(value: T): Omit<T, "raw"> {
  const copy: T = { ...value };
  delete copy.raw;
  return copy;
}

function shape(blocks: StructuredBlock[] | null) {
  return blocks?.map((block) =>
    block.kind === "section" ? { ...block, entries: block.entries.map(withoutRaw) } : withoutRaw(block)
  );
}

describe("structureAnswer", () => {
  it("要約の段落と、根拠の付いた項目の「確認できた」節、末尾の節を「確認できなかった」節にする", () => {
    const answer = [
      "答えは次のとおりです。",
      "",
      "- 申請は 30 日前です【証拠 ID: c1】。",
      "- 承認は部署が行います。",
      "",
      `**${HEADING}**`,
      "",
      "根拠で確かめられなかった次の内容は、回答に載せていません。",
      "- 根拠で確かめられない: 「延長は 2 回です。」",
    ].join("\n");
    const blocks = structureAnswer(
      answer,
      evidence(
        [
          { text: "- 申請は 30 日前です【証拠 ID: c1】。", chunk_ids: ["c1"] },
          { text: "- 承認は部署が行います。", chunk_ids: ["c2"] },
        ],
        [{ text: "【証拠 ID: c1】", chunk_ids: ["c1"] }]
      ),
      HEADING,
      TITLES
    );
    expect(shape(blocks)).toEqual([
      { kind: "paragraph", key: "b0", text: "答えは次のとおりです。", chunkIds: [] },
      {
        kind: "section",
        key: "confirmed",
        title: "確認できた",
        ordered: false,
        entries: [
          { key: "b1-0", text: "申請は 30 日前です。", chunkIds: ["c1"] },
          { key: "b1-1", text: "承認は部署が行います。", chunkIds: ["c2"] },
        ],
      },
      {
        kind: "section",
        key: "notice",
        title: "確認できなかった",
        ordered: false,
        collapsible: true,
        entries: [
          { key: "n0", text: "根拠で確かめられなかった次の内容は、回答に載せていません。", chunkIds: [], muted: true },
          { key: "n1", text: "根拠で確かめられない: 「延長は 2 回です。」", chunkIds: [] },
        ],
      },
    ]);
  });

  it("回答の見出しは残し、根拠の無い段落が後ろにあれば「確認できた」の見出しを足さない", () => {
    const answer = "**結論**\n期限は 30 日です。\n\n補足として、資料に記載はありません。";
    const blocks = structureAnswer(answer, evidence([{ text: "期限は 30 日です。", chunk_ids: ["c1"] }]), null, TITLES);
    expect(blocks?.map((block) => (block.kind === "section" ? block.title : block.kind))).toEqual(["結論", "paragraph"]);
    const plain = structureAnswer(
      "期限は 30 日です。\n\n資料に記載はありません。",
      evidence([{ text: "期限は 30 日です。", chunk_ids: ["c1"] }]),
      null,
      TITLES
    );
    expect(plain?.map((block) => block.kind)).toEqual(["paragraph", "paragraph"]);
  });

  it("表は Markdown の塊のまま、根拠を結び付けられなければ null", () => {
    const table = "| 項目 | 値 |\n|---|---|\n| 期限 | 30 日 |";
    const blocks = structureAnswer(`期限は 30 日です。\n\n${table}`, evidence([{ text: "期限は 30 日です。", chunk_ids: ["c1"] }]), null, TITLES);
    expect(blocks?.[1]).toMatchObject({ kind: "markdown", text: table });
    expect(structureAnswer("期限は 30 日です。", evidence([]), null, TITLES)).toBeNull();
    expect(structureAnswer("期限は 30 日です。", null, null, TITLES)).toBeNull();
  });

  it("節の見出しは資料との照合の成果物の notice から読む", () => {
    expect(noticeHeading({ notice: { heading: HEADING } })).toBe(HEADING);
    expect(noticeHeading({})).toBeNull();
    expect(noticeHeading(null)).toBeNull();
  });
});

describe("answerEvidenceData", () => {
  it("根拠の行の場所は頁、無ければ節", () => {
    const data = evidence([]);
    expect(data.entries.map((entry) => entry.citationLocation)).toEqual(["p.3", "第 1 章"]);
    expect(data.entries[0].item).toMatchObject({ fileName: "文書 A.pdf", usedInAnswer: true, routeBadges: ["Evidence"] });
  });
});

describe("validationReview", () => {
  // #2361: 除いた内容の一覧は本文の節の 1 か所だけ。照合の結果には節の項目の数（件数のバッジ）だけを持つ。
  it("件数は本文の節の項目の数（notice.items）を持ち、除いた内容の一覧は持たない", () => {
    const claims = Array.from({ length: 6 }, (_, index) => ({ answer_quote: `段落 ${index}`, status: "unsupported", reason: "r" }));
    const content = {
      status: "completed",
      result: { status: "completed", valid: false, claims, findings: [], stale_evidence: [], missing_evidence: [] },
      withheld: { claims: 6, findings: 0, all: false },
      notice: { items: 6, heading: HEADING },
    };
    expect(validationReview(content)).toEqual({
      state: "withheld",
      cause: null,
      withheldClaims: 6,
      withheldAll: false,
      listedItems: 6,
      passages: null,
    });
    // 節の記録が無い古い Run は 0（画面は外した段落の数で出す）。
    expect(validationReview({ ...content, notice: undefined })?.listedItems).toBe(0);
  });
});

// #2707: 最終の検証が時間切れ・失敗のときは、決定的な照合の根拠を「参考情報」にする。
describe("structureAnswer の照合が判定を出さなかった回答", () => {
  const UNVERIFIED = "この回答は資料との照合を終えられず、本質問の条件への該当は確認できていません。";
  const answer = [
    "答えは次のとおりです。",
    "",
    "- 申請は 30 日前までに行います。",
    "- 承認は部署が行います。",
    "",
    "**根拠**",
    "",
    "- 文書 A.pdf p.3",
    "",
    UNVERIFIED,
  ].join("\n");

  function textEvidence(references: { quote: string; chunk_ids: string[] }[]) {
    return answerEvidenceData({
      items: [
        { document_id: "d1", chunk_id: "c1", file_name: "文書 A.pdf", excerpt: "x", locator: { page_start: 3 }, cited: true },
      ],
      passages: [
        { text: "- 申請は 30 日前までに行います。", chunk_ids: ["c1"] },
        { text: "- 文書 A.pdf p.3", chunk_ids: ["c1"] },
      ],
      binding: "text",
      references,
      citation_lines: ["- 文書 A.pdf p.3"],
    });
  }

  it("「確認できた」節を作らず、本文に根拠の行を付けず、根拠の原文を「参考」節にする", () => {
    const blocks = structureAnswer(answer, textEvidence([{ quote: "申請は 30 日前までに行う。", chunk_ids: ["c1"] }]), null, TITLES);
    expect(shape(blocks)).toEqual([
      { kind: "paragraph", key: "b0", text: "答えは次のとおりです。", chunkIds: [] },
      {
        kind: "section",
        key: "b1",
        title: null,
        ordered: false,
        entries: [
          { key: "b1-0", text: "申請は 30 日前までに行います。", chunkIds: [] },
          { key: "b1-1", text: "承認は部署が行います。", chunkIds: [] },
        ],
      },
      // 根拠に結び付いた平文の出典の行と、中身が無くなった見出し（**根拠**）は出さない。
      { kind: "paragraph", key: "b2", text: UNVERIFIED, chunkIds: [] },
      {
        kind: "section",
        key: "reference",
        title: "参考",
        ordered: false,
        entries: [
          { key: "reference-note", text: "原文の説明", chunkIds: [], muted: true, plain: true, uiText: true },
          { key: "reference-0", text: "「申請は 30 日前までに行う。」", chunkIds: ["c1"], plain: true },
        ],
      },
    ]);
    expect(blocks?.some((block) => block.kind === "section" && block.title === TITLES.confirmed)).toBe(false);
  });

  it("根拠を 1 つも結び付けられなければ null（Markdown のまま）", () => {
    expect(structureAnswer(answer, textEvidence([]), null, TITLES)).toBeNull();
  });

  it("出典の行の後ろに本文が続く見出しは残す", () => {
    const withBody = "答えです。\n\n**根拠**\n\n- 文書 A.pdf p.3\n\n**補足**\n\n期限があります。";
    const blocks = structureAnswer(withBody, textEvidence([{ quote: "q", chunk_ids: ["c1"] }]), null, TITLES);
    const titles = blocks?.filter((block) => block.kind === "section").map((block) => (block.kind === "section" ? block.title : null));
    expect(titles).toEqual(["補足", "参考"]);
  });
});

// #2700・#2707: 検証が時間の上限までに一部の段落だけを確かめた回答（`binding: "partial"`）。
describe("structureAnswer の一部の段落だけを確かめた回答", () => {
  const answer = [
    "答えは次のとおりです。",
    "",
    "- 申請は 30 日前までに行います。",
    "- 承認は部署が行います。",
    "",
    `**${HEADING}**`,
    "",
    "次の内容は、資料との照合が時間の上限までに終わらず確かめきれませんでした。",
    "- 確かめが終わっていない: 「承認は部署が行います。」",
  ].join("\n");

  it("確かめ終わった段落に根拠の行、確かめ終わらなかった段落の根拠は「参考」節、除いた内容の節はその後", () => {
    const data = answerEvidenceData({
      items: [
        { document_id: "d1", chunk_id: "c1", file_name: "文書 A.pdf", excerpt: "x", locator: { page_start: 3 }, cited: true },
        { document_id: "d2", chunk_id: "c2", file_name: "文書 B.pdf", excerpt: "y", locator: { page_start: 5 }, cited: true },
      ],
      passages: [{ text: "- 申請は 30 日前までに行います。", chunk_ids: ["c1"] }],
      binding: "partial",
      references: [{ quote: "承認は部署が行う。", chunk_ids: ["c2"] }],
      citation_lines: [],
    });
    const blocks = structureAnswer(answer, data, HEADING, TITLES);
    const sections = blocks?.filter((block) => block.kind === "section") ?? [];
    expect(sections.map((block) => (block.kind === "section" ? block.title : null))).toEqual([null, "参考", "確認できなかった"]);
    const [list, reference] = sections;
    if (list.kind !== "section" || reference.kind !== "section") throw new Error("節がありません");
    // 確かめ終わった段落だけに根拠の行。確かめ終わらなかった段落は本文に残し、根拠の行を付けない。
    expect(list.entries.map((entry) => entry.chunkIds)).toEqual([["c1"], []]);
    expect(reference.entries.map((entry) => entry.text)).toEqual(["原文の説明", "「承認は部署が行う。」"]);
    expect(reference.entries[1].chunkIds).toEqual(["c2"]);
  });

  it("確かめ終わった段落が無くても、参考情報があれば節に組み立てる", () => {
    const data = answerEvidenceData({
      items: [{ document_id: "d2", chunk_id: "c2", file_name: "文書 B.pdf", excerpt: "y", locator: { page_start: 5 }, cited: true }],
      passages: [],
      binding: "partial",
      references: [{ quote: "承認は部署が行う。", chunk_ids: ["c2"] }],
    });
    const blocks = structureAnswer(answer, data, HEADING, TITLES);
    expect(blocks?.some((block) => block.kind === "section" && block.title === TITLES.confirmed)).toBe(false);
    expect(blocks?.some((block) => block.kind === "section" && block.title === "参考")).toBe(true);
  });
});

describe("structureAnswer の除いた内容の節", () => {
  it("「資料から確認できなかった事項」の節は全件を同じ書式で持ち、畳める節にする", () => {
    const items = Array.from({ length: 12 }, (_, index) => `- 根拠で確かめられない: 「段落 ${index}」`);
    const answer = `期限は 30 日です。【証拠 ID: c1】\n\n**${HEADING}**\n\n根拠で確かめられなかった次の内容は、回答に載せていません。\n${items.join("\n")}`;
    const blocks = structureAnswer(answer, evidence([{ text: "期限は 30 日です。【証拠 ID: c1】", chunk_ids: ["c1"] }]), HEADING, TITLES);
    const notice = blocks?.find((block) => block.kind === "section" && block.key === "notice");
    expect(notice).toMatchObject({ kind: "section", title: TITLES.unconfirmed, collapsible: true });
    if (notice?.kind !== "section") throw new Error("節がありません");
    expect(notice.entries.filter((entry) => !entry.muted)).toHaveLength(12);
    expect(notice.entries.some((entry) => entry.text.includes("ほか"))).toBe(false);
  });
});

// #2738: 根拠と矛盾した段落だけを外し、確かめられない段落・規則の検査の段落は本文に残して印を付ける。
describe("段落の印の記録のある回答", () => {
  const marks = {
    passage_marks: [
      {
        text: "- 承認は部署が行います。",
        state: "unverified",
        reasons: [{ code: "unsupported", label: "根拠で確かめられない", detail: "" }],
      },
    ],
    removed_passages: [{ text: "延長は 2 回です。", reasons: [{ code: "contradicted", label: "根拠と矛盾", detail: "" }] }],
    answer_warnings: [],
  };

  it("照合の結果は印の記録を持ち、過去の判定（確かめられない段落で外す）を使わない", () => {
    const content = {
      status: "completed",
      result: {
        status: "completed",
        valid: false,
        claims: [{ answer_quote: "承認は部署が行います。", status: "unsupported", reason: "" }],
        findings: [{ check: "guide", severity: "error", message: "手順の順序" }],
      },
      withheld: { removed: 1, unverified: 1, warnings: 0, all: false, listed: 2 },
      ...marks,
    };
    const review = validationReview(content);
    expect(review?.state).toBe("marked");
    expect(review?.passages?.marks).toHaveLength(1);
    expect(review?.passages?.removed).toHaveLength(1);
    // 印も外した段落も回答の注意も無ければ「照合済み」（規則の検査の結果だけでは本文を外さない）。
    expect(
      validationReview({ ...content, passage_marks: [], removed_passages: [], answer_warnings: [] })
    ).toMatchObject({ state: "verified", passages: null });
  });

  it("印の付いた項目は「確認できた」節に入れず、記録の節の見出し（過去の節・確認が必要な点）は本文から除く", () => {
    const answer = [
      "答えは次のとおりです。",
      "",
      "- 申請は 30 日前です【証拠 ID: c1】。",
      "- 承認は部署が行います。",
      "",
      "**確認が必要な点**",
      "",
      "- 「承認は部署が行います。」根拠で確かめられない",
    ].join("\n");
    const blocks = structureAnswer(
      answer,
      evidence(
        [
          { text: "- 申請は 30 日前です【証拠 ID: c1】。", chunk_ids: ["c1"] },
          { text: "- 承認は部署が行います。", chunk_ids: ["c2"] },
        ],
        [{ text: "【証拠 ID: c1】", chunk_ids: ["c1"] }]
      ),
      HEADING,
      TITLES,
      validationReview({ status: "completed", ...marks })?.passages ?? null
    );
    expect(blocks?.some((block) => block.kind === "section" && block.title === TITLES.confirmed)).toBe(false);
    expect(blocks?.some((block) => block.kind === "section" && block.title === TITLES.unconfirmed)).toBe(false);
    const list = blocks?.find((block) => block.kind === "section" && block.key === "b1");
    if (list?.kind !== "section") throw new Error("箇条書きがありません");
    expect(list.entries.map((entry) => entry.raw)).toEqual(["- 申請は 30 日前です【証拠 ID: c1】。", "- 承認は部署が行います。"]);
    expect(JSON.stringify(blocks)).not.toContain("確認が必要な点");
  });

  it("根拠を結び付けられない回答も、記録があれば段落に組み立てる", () => {
    const blocks = structureAnswer("段落 1。\n\n段落 2。", null, null, TITLES, {
      marks: [{ text: "段落 2。", state: "warning", reasons: [] }],
      removed: [],
      warnings: [],
    });
    expect(shape(blocks)).toEqual([
      { kind: "paragraph", key: "b0", text: "段落 1。", chunkIds: [] },
      { kind: "paragraph", key: "b1", text: "段落 2。", chunkIds: [] },
    ]);
  });
});
