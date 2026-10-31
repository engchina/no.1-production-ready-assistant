import { describe, expect, it } from "vitest";

import type { DocumentSummary } from "@/lib/api";

import { documentVersionOptions, savedVersionOption } from "./DocumentVersionEditor.logic";

function doc(id: string, supersededBy: string | null = null): DocumentSummary {
  return {
    id,
    file_name: `${id}.pdf`,
    status: "INDEXED",
    category_name: null,
    content_type: "application/pdf",
    file_size_bytes: 1,
    content_sha256: null,
    duplicate_of_document_id: null,
    uploaded_at: "2026-10-07T00:00:00Z",
    indexed_at: null,
    knowledge_bases: [],
    source_profile: null,
    superseded_by_document_id: supersededBy,
  };
}

describe("documentVersionOptions", () => {
  it("文書自身と、この文書に置き換えられた文書を候補から外す", () => {
    const options = documentVersionOptions(
      [doc("v1", "v2"), doc("v2"), doc("v3"), doc("v0", "v3")],
      "v2",
      { superseded: "旧版" },
    );
    expect(options.map((option) => option.value)).toEqual(["v3", "v0"]);
  });

  it("候補自身が旧版なら印を付ける", () => {
    const options = documentVersionOptions([doc("v0", "v3"), doc("v3")], "v1", {
      superseded: "旧版",
    });
    expect(options).toEqual([
      { value: "v0", label: "v0.pdf", badge: "旧版" },
      { value: "v3", label: "v3.pdf", badge: undefined },
    ]);
  });
});

describe("savedVersionOption", () => {
  it("保存済みの新しい版の名前を出し、見えない文書は ID で出す", () => {
    const unknown = (id: string) => `表示できない文書（${id}）`;
    expect(savedVersionOption(null, null, unknown)).toBeNull();
    expect(savedVersionOption("v2", "v2.pdf", unknown)).toEqual({ value: "v2", label: "v2.pdf" });
    expect(savedVersionOption("v9", null, unknown)).toEqual({
      value: "v9",
      label: "表示できない文書（v9）",
    });
  });
});
