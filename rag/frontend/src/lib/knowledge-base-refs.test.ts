import { describe, expect, it } from "vitest";

import { resolveKnowledgeBaseSelection } from "./knowledge-base-refs";

const missingName = (id: string) => `見つからない (${id})`;

describe("resolveKnowledgeBaseSelection（参照 KB の状態）", () => {
  it("ID で引いた結果を優先し、アーカイブ済みと見つからない KB を分ける", () => {
    const health = resolveKnowledgeBaseSelection({
      ids: ["kb-1", "kb-archived", "kb-gone", "kb-1"],
      found: [
        { id: "kb-1", name: "社内規程", status: "ACTIVE", document_count: 3 },
        { id: "kb-archived", name: "旧規程", status: "ARCHIVED", document_count: 1 },
      ],
      lookupSettled: true,
      missingName,
    });

    expect(health.items.map((item) => item.id)).toEqual(["kb-1", "kb-archived", "kb-gone"]);
    expect(health.archived.map((item) => item.name)).toEqual(["旧規程"]);
    expect(health.missing).toEqual([
      { id: "kb-gone", name: "見つからない (kb-gone)", status: "ACTIVE", document_count: 0, missing: true },
    ]);
  });

  it("範囲外で引けない KB は、画面が持つ参照（検索・回答プロファイルの参照 KB）の名前と状態で出す", () => {
    const health = resolveKnowledgeBaseSelection({
      ids: ["kb-out-of-scope"],
      found: [],
      known: [{ id: "kb-out-of-scope", name: "他部署の規程", status: "ARCHIVED" }],
      lookupSettled: true,
      missingName,
    });

    expect(health.missing).toEqual([]);
    expect(health.archived.map((item) => item.name)).toEqual(["他部署の規程"]);
  });

  it("ID の検索が終わるまでは見つからないと判定しない（保存時に分かっている欠落だけ出す）", () => {
    const health = resolveKnowledgeBaseSelection({
      ids: ["kb-pending", "kb-deleted"],
      knownMissingIds: ["kb-deleted"],
      lookupSettled: false,
      missingName,
    });

    expect(health.items.map((item) => item.id)).toEqual(["kb-deleted"]);
    expect(health.missing.map((item) => item.id)).toEqual(["kb-deleted"]);
  });
});
