import { describe, expect, it } from "vitest";

import type { RetrievedChunk } from "./api";
import {
  citationLocation,
  matchCitation,
  matchCitationLine,
  parseAnswerText,
  parseCitationLine,
  resolveCitationLine,
  resolveCitationLineTargets,
} from "./answer-text";

describe("parseAnswerText", () => {
  it("回答エンジンの本文を要約・節・説明・根拠に分ける", () => {
    const text = [
      "受注は受注入力画面で登録します。",
      "",
      "資料により確認できた事項",
      "",
      "・受注番号は自動で採番されます。",
      "根拠：受注マニュアル.pdf p.3",
      "",
      "操作手順（受注入力）",
      "",
      "1. 受注入力画面を開きます。",
      "2. 登録ボタンを押します。",
      "根拠：受注マニュアル.pdf p.5",
      "",
      "資料から確認できなかった事項",
      "",
      "・既存の受注の修正方法",
    ].join("\n");
    expect(parseAnswerText(text)).toEqual([
      { kind: "paragraph", text: "受注は受注入力画面で登録します。" },
      {
        kind: "section",
        title: "資料により確認できた事項",
        ordered: false,
        entries: [
          {
            kind: "item",
            text: "受注番号は自動で採番されます。",
            citations: ["根拠：受注マニュアル.pdf p.3"],
            citationLines: [1],
          },
        ],
      },
      {
        kind: "section",
        title: "操作手順（受注入力）",
        ordered: true,
        entries: [
          { kind: "item", text: "受注入力画面を開きます。", citations: [], citationLines: [] },
          {
            kind: "item",
            text: "登録ボタンを押します。",
            citations: ["根拠：受注マニュアル.pdf p.5"],
            citationLines: [2],
          },
        ],
      },
      {
        kind: "section",
        title: "資料から確認できなかった事項",
        ordered: false,
        entries: [{ kind: "item", text: "既存の受注の修正方法", citations: [], citationLines: [] }],
      },
    ]);
  });

  it("節の後ろに続く注記は節の中の文として残す", () => {
    const blocks = parseAnswerText("資料により確認できた事項\n\n・説明\n根拠：a.pdf p.1\n\n旧形式の文書 2 件は検索対象外です。");
    expect(blocks?.[0]).toMatchObject({
      entries: [
        { kind: "item", text: "説明", citations: ["根拠：a.pdf p.1"] },
        { kind: "text", text: "旧形式の文書 2 件は検索対象外です。" },
      ],
    });
  });

  // 印の付いた説明の節（Issue 3394）。
  it("確かめきれていない説明の節は、確認できた事項の節と分ける（表示形式の無い回答）", () => {
    const blocks = parseAnswerText(
      [
        "資料により確認できた事項",
        "",
        "・確かめた説明",
        "根拠：a.pdf p.1",
        "",
        "根拠で確かめきれていない事項",
        "",
        "次の説明は根拠で確かめきれていないか、注意が必要です。",
        "・印の付いた説明",
        "根拠：b.pdf p.2",
      ].join("\n")
    );
    expect(blocks).toMatchObject([
      { kind: "section", title: "資料により確認できた事項", entries: [{ kind: "item", text: "確かめた説明" }] },
      {
        kind: "section",
        title: "根拠で確かめきれていない事項",
        entries: [
          { kind: "text", text: "次の説明は根拠で確かめきれていないか、注意が必要です。" },
          { kind: "item", text: "印の付いた説明", citations: ["根拠：b.pdf p.2"] },
        ],
      },
    ]);
  });

  it("参考・未確認・確認することの節も節に分け、参考の節の直下の説明は節の中の文にする", () => {
    const blocks = parseAnswerText(
      [
        "資料の記載を以下に示します。",
        "",
        "参考情報",
        "",
        "以下は資料の原文です。本質問の条件への該当は確認できていません。",
        "・「上限は 10 万円です。」",
        "根拠：a.pdf p.1",
        "",
        "回答に未反映の関連章節",
        "",
        "・読めなかった章節",
        "",
        "回答を確定するために確認すること",
        "",
        "・申請の種類",
      ].join("\n")
    );
    expect(blocks?.map((block) => (block.kind === "section" ? block.title : block.kind))).toEqual([
      "paragraph",
      "参考情報",
      "回答に未反映の関連章節",
      "回答を確定するために確認すること",
    ]);
    expect(blocks?.[1]).toMatchObject({
      entries: [
        { kind: "text", text: "以下は資料の原文です。本質問の条件への該当は確認できていません。" },
        { kind: "item", text: "「上限は 10 万円です。」", citations: ["根拠：a.pdf p.1"] },
      ],
    });
  });

  it("文言を変える前に保存した回答の見出しも節に分ける（参考の原文が手順の節に混ざらない）", () => {
    const blocks = parseAnswerText(
      "操作手順（申請）\n\n1. 申請します。\n\n資料の記載（今回への適用は未確認）\n\n・「原文」"
    );
    expect(blocks?.map((block) => (block.kind === "section" ? block.entries.length : 0))).toEqual([1, 1]);
  });

  it("書式の見出しが無い本文は null（そのまま出す）", () => {
    expect(parseAnswerText("検索された資料に回答を裏付ける十分な根拠がないため、回答できません。")).toBeNull();
    // 本文中に同じ語があっても、行全体が見出しでなければ節にしない。
    expect(parseAnswerText("資料により確認できた事項は次のとおりです。")).toBeNull();
  });
});

function chunk(fileName: string, pageStart?: number, pageEnd?: number): RetrievedChunk {
  const metadata: RetrievedChunk["metadata"] = {};
  if (pageStart != null) metadata.page_start = pageStart;
  if (pageEnd != null) metadata.page_end = pageEnd;
  return {
    document_id: fileName,
    chunk_id: `${fileName}:${pageStart}`,
    text: "",
    score: 0,
    rerank_score: null,
    file_name: fileName,
    category_name: null,
    metadata,
  };
}

describe("citationLocation", () => {
  // #3353: 長いファイル名を省略しても、位置（頁・シート）は省略せずに出す。
  it("ファイル名の後ろの位置を返し、位置が無い・ファイル名が読めないときは undefined", () => {
    expect(citationLocation("根拠：文書 A.pdf p.3", "文書 A.pdf")).toBe("p.3");
    expect(citationLocation("根拠：表 B.xlsx シート「集計」A3:D6", "表 B.xlsx")).toBe("シート「集計」A3:D6");
    expect(citationLocation("根拠：文書 A.pdf", "文書 A.pdf")).toBeUndefined();
    expect(citationLocation("根拠：文書 A.pdf p.3", null)).toBeUndefined();
  });
});

describe("parseCitationLine", () => {
  it("根拠の行のファイル名と頁を読む", () => {
    expect(parseCitationLine("根拠：受注 マニュアル.pdf p.12")).toEqual({ fileName: "受注 マニュアル.pdf", page: 12 });
    // 頁の無い行（頁が空）も、ファイル名だけで当てる。
    expect(parseCitationLine("根拠：規程.docx p.")).toEqual({ fileName: "規程.docx", page: null });
    expect(parseCitationLine("根拠です")).toBeNull();
  });

  it("表計算の根拠の行のシートとセル範囲の行を読む（Issue 1224）", () => {
    expect(parseCitationLine("根拠：経費 コード.xlsx シート「費目 コード」A3:D6")).toEqual({
      fileName: "経費 コード.xlsx",
      page: null,
      sheet: "費目 コード",
      rowStart: 3,
      rowEnd: 6,
    });
    expect(parseCitationLine("根拠：a.xlsx シート「手順」")).toEqual({
      fileName: "a.xlsx",
      page: null,
      sheet: "手順",
      rowStart: null,
      rowEnd: null,
    });
  });
});

describe("matchCitation", () => {
  const citations = [
    chunk("manual.pdf", 2),
    chunk("Manual.pdf", 5, 7),
    chunk("manual.pdf", 6),
    chunk("other.pdf", 6),
  ];

  it("同じファイル（大小文字・全角半角を区別しない）で頁の範囲に入る引用を順位の順に選ぶ", () => {
    expect(matchCitation({ fileName: "ＭＡＮＵＡＬ.pdf", page: 6 }, citations)).toBe(1);
    expect(matchCitation({ fileName: "manual.pdf", page: 2 }, citations)).toBe(0);
  });

  it("頁の範囲に入る引用が無ければ頁の近い引用、頁が無ければ同じファイルの先頭", () => {
    expect(matchCitation({ fileName: "manual.pdf", page: 4 }, citations)).toBe(1);
    expect(matchCitation({ fileName: "manual.pdf", page: null }, citations)).toBe(0);
    // 頁の無い引用は 1 頁目として扱う（backend の _stored_child と同じ）。
    expect(matchCitation({ fileName: "a.pdf", page: 1 }, [chunk("a.pdf")])).toBe(0);
  });

  it("同じファイルの引用が無ければ -1", () => {
    expect(matchCitation({ fileName: "missing.pdf", page: 1 }, citations)).toBe(-1);
    expect(matchCitation({ fileName: "manual.pdf", page: 1 }, [])).toBe(-1);
  });
});

// 出典行の番号で引用を結ぶ（#1330）。
describe("matchCitationLine", () => {
  function numbered(fileName: string, page: number, lines: number[]): RetrievedChunk {
    const base = chunk(fileName, page);
    return { ...base, metadata: { ...base.metadata, answer_citation_lines: lines } };
  }

  it("本文の n 番目の根拠の行は、回答エンジンが n を付けた引用に結ぶ（同じ頁・同じファイルでも取り違えない）", () => {
    const citations = [numbered("規程.pdf", 2, [2]), numbered("規程.pdf", 2, [1, 3])];
    const ref = { fileName: "規程.pdf", page: 2 };

    expect(matchCitationLine(1, ref, citations)).toBe(1);
    expect(matchCitationLine(2, ref, citations)).toBe(0);
    expect(matchCitationLine(3, ref, citations)).toBe(1);
  });

  it("番号の無い回答（古い記録）はファイル名と頁で選ぶ", () => {
    const citations = [chunk("規程.pdf", 2), chunk("規程.pdf", 5)];

    expect(matchCitationLine(1, { fileName: "規程.pdf", page: 5 }, citations)).toBe(1);
    expect(matchCitationLine(undefined, { fileName: "規程.pdf", page: 2 }, citations)).toBe(0);
    expect(matchCitationLine(undefined, null, citations)).toBe(-1);
  });

  it("出典行の番号は、項目に付かない根拠の行も含めて本文の順に数える", () => {
    const blocks = parseAnswerText(
      ["資料により確認できた事項", "", "・説明 A", "根拠：a.pdf p.1", "続きの文", "根拠：a.pdf p.2", "・説明 B", "根拠：b.pdf p.3"].join(
        "\n",
      ),
    );
    const section = blocks?.[0];
    const items = section?.kind === "section" ? section.entries.filter((entry) => entry.kind === "item") : [];

    expect(items.map((entry) => (entry.kind === "item" ? entry.citationLines : []))).toEqual([[1], [3]]);
  });
});

// 出典行の主張を支える箇所と確定した引用だけを正確な位置にする（#2710）。合成の文書: 親 A の同じ頁の子 H（見出し）と F（図の説明）。
describe("resolveCitationLine", () => {
  function located(chunkId: string, metadata: RetrievedChunk["metadata"]): RetrievedChunk {
    const base = chunk("文書 A.pdf", 3);
    return { ...base, chunk_id: chunkId, metadata: { ...base.metadata, ...metadata } };
  }
  const ref = { fileName: "文書 A.pdf", page: 3 };

  it("確定の印のある番号は、引用の並びの先の H ではなく F を正確な位置として開く", () => {
    const citations = [
      located("H", { evidence_model_used: false }),
      located("F", { answer_citation_lines: [1], answer_citation_provenance: "child", evidence_model_used: true }),
    ];
    expect(resolveCitationLine(1, ref, citations)).toEqual({ index: 1, position: { kind: "exact" } });
  });

  it("同頁の全確定片段を返し、候補は独立した未確定の対象として保つ", () => {
    const first = located("a", { answer_citation_lines: [1, 2], answer_citation_provenance: "child" });
    const second = located("b", { answer_citation_lines: [2], answer_citation_provenance: "child" });
    const candidate = located("candidate", { answer_citation_candidate_lines: [2] });
    const unused = located("unused", {});
    const citations = [first, second, candidate, unused];
    expect(resolveCitationLineTargets(1, ref, citations)).toEqual([{ index: 0, position: { kind: "exact" } }]);
    expect(resolveCitationLineTargets(1, ref, [first, first])).toHaveLength(1);
    expect(resolveCitationLineTargets(2, ref, citations)).toEqual([
      { index: 0, position: { kind: "exact" } },
      { index: 1, position: { kind: "exact" } },
      { index: 2, position: { kind: "unresolved", reason: "candidates", candidates: [2] } },
    ]);
    for (const order of [citations, [...citations].reverse()]) {
      expect(resolveCitationLineTargets(2, ref, order).filter(target => target.position.kind === "exact")
        .map(target => order[target.index].chunk_id).sort()).toEqual(["a", "b"]);
    }
  });

  it("複数対象の resolver でも既存の未確定候補と旧本文を確定へ昇格しない", () => {
    const citations = [located("a", { answer_citation_lines: [1] }), located("b", {})];
    expect(resolveCitationLineTargets(1, ref, citations)).toEqual([resolveCitationLine(1, ref, citations)]);
    expect(resolveCitationLineTargets(1, null, [])).toEqual([]);
  });

  it("候補の番号だけの行は、先頭の候補を開くが位置は未確定にする", () => {
    const citations = [
      located("H", { answer_citation_candidate_lines: [1] }),
      located("F", { answer_citation_candidate_lines: [1] }),
      located("other", {}),
    ];
    expect(resolveCitationLine(1, ref, citations)).toEqual({
      index: 0,
      position: { kind: "unresolved", reason: "candidates", candidates: [0, 1] },
    });
  });

  it("以前の回答の番号（確定の印が無い）は、記録の対応のまま開き、同じ頁に別の箇所があれば未確定にする", () => {
    const legacy = [located("H", { answer_citation_lines: [1] }), located("F", {})];
    expect(resolveCitationLine(1, ref, legacy)).toEqual({
      index: 0,
      position: { kind: "unresolved", reason: "legacy", candidates: [0, 1] },
    });
    // 同じ頁に別の箇所が無ければ、取り違えようがないので確定。
    const alone = [located("H", { answer_citation_lines: [1] }), { ...located("F", {}), metadata: { page_start: 9 } }];
    expect(resolveCitationLine(1, ref, alone)).toEqual({ index: 0, position: { kind: "exact" } });
  });

  it("番号の無い回答は文書名と頁で選び、同じ頁に複数あれば未確定。当たらなければ null", () => {
    const citations = [located("H", {}), located("F", {})];
    expect(resolveCitationLine(undefined, ref, citations)?.position).toEqual({
      kind: "unresolved",
      reason: "legacy",
      candidates: [0, 1],
    });
    expect(resolveCitationLine(undefined, { fileName: "文書 B.pdf", page: 1 }, citations)).toBeNull();
    expect(matchCitationLine(undefined, { fileName: "文書 B.pdf", page: 1 }, citations)).toBe(-1);
  });
});

describe("matchCitation（表計算）", () => {
  function sheetChunk(sheet: string, rowStart: number, rowEnd: number): RetrievedChunk {
    const base = chunk("book.xlsx");
    return {
      ...base,
      chunk_id: `${sheet}:${rowStart}`,
      metadata: { sheet_name: sheet, row_start: rowStart, row_end: rowEnd },
    };
  }
  const citations = [sheetChunk("手順", 3, 5), sheetChunk("費目", 1, 1), sheetChunk("費目", 3, 6)];

  it("同じシートで行の範囲が重なる引用を選ぶ", () => {
    const ref = { fileName: "book.xlsx", page: null, sheet: "費目", rowStart: 4, rowEnd: 4 };
    expect(matchCitation(ref, citations)).toBe(2);
  });

  it("行が重ならなければ同じシートの先頭、シートが無ければ同じファイルの先頭", () => {
    expect(matchCitation({ fileName: "book.xlsx", page: null, sheet: "費目", rowStart: 20, rowEnd: 21 }, citations)).toBe(1);
    expect(matchCitation({ fileName: "book.xlsx", page: null, sheet: "別", rowStart: 1, rowEnd: 1 }, citations)).toBe(0);
  });
});


describe("生成時に保存した回答の表示形式", () => {
  const format = { rules_section: "確認結果[例]", gaps_section: "不足箇所", citation_prefix: "出典[例]：",
    item_bullet: "※", operation_steps_heading: "実行順", confirmation_steps_heading: "確認順",
    steps_heading_template: "{heading}[{title}]", page_location: "頁[{page}]", sheet_location: "表[{sheet}] {cell_range}" };

  it("上書きした節・箇条・出典行を読み、根拠の行の番号を保持する", () => {
    const blocks = parseAnswerText("要約\n\n確認結果[例]\n※説明\n出典[例]：文書 A.pdf 頁[3]\n\n実行順[機能 A]\n1. 操作\n出典[例]：文書 A.pdf 頁[4]", format);
    expect(blocks?.[1]).toEqual({ kind: "section", title: "確認結果[例]", ordered: false, entries: [
      { kind: "item", text: "説明", citations: ["出典[例]：文書 A.pdf 頁[3]"], citationLines: [1] },
    ] });
    expect(blocks?.[2]).toMatchObject({ title: "実行順[機能 A]", entries: [{ citationLines: [2] }] });
    expect(parseCitationLine("出典[例]：文書 A.pdf 頁[3]", format)).toEqual({ fileName: "文書 A.pdf", page: 3 });
    expect(parseCitationLine("出典[例]：文書 A.xlsx 表[シート A] A3:D6", format)).toEqual({ fileName: "文書 A.xlsx", page: null, sheet: "シート A", rowStart: 3, rowEnd: 6 });
  });
});
