import { describe, expect, it } from "vitest";

import type { VectorIndexSettingsData } from "@/lib/api";

import { currentIndexLabel, orderedProfiles, reprovisionStatus, showReindexSql } from "./VectorIndexSettingsClient";

function settings(overrides: Partial<VectorIndexSettingsData>): VectorIndexSettingsData {
  return {
    profile: "accurate",
    target_accuracy: 98,
    neighbors: 48,
    efconstruction: 800,
    distance: "COSINE",
    requires_reprovision: false,
    index_status: "match",
    actual_neighbors: 48,
    actual_efconstruction: 800,
    profiles: [],
    reindex_sql: "DROP INDEX rag_chunks_embedding_hnsw_idx;",
    config_source: "runtime",
    ...overrides,
  };
}

describe("reprovisionStatus", () => {
  it("backend の判定が一致なら警告を出さない", () => {
    expect(reprovisionStatus("match")).toBeNull();
    expect(reprovisionStatus(undefined)).toBeNull();
  });

  it("違うときは再作成の警告、確認できないときは確認できない旨を出す", () => {
    expect(reprovisionStatus("reprovision")).toEqual({
      tone: "warning",
      key: "settings.vectorIndex.reprovision",
    });
    expect(reprovisionStatus("unknown")).toEqual({
      tone: "info",
      key: "settings.vectorIndex.reprovisionUnknown",
    });
  });
});

describe("showReindexSql", () => {
  it("一致なら再作成 SQL を出さず、違う・確認できないときは出す", () => {
    expect(showReindexSql(settings({ index_status: "match" }))).toBe(false);
    expect(showReindexSql(settings({ index_status: "reprovision" }))).toBe(true);
    expect(showReindexSql(settings({ index_status: "unknown" }))).toBe(true);
    expect(showReindexSql(settings({ index_status: "unknown", reindex_sql: "" }))).toBe(false);
  });
});

describe("currentIndexLabel", () => {
  it("実際の索引の値を出し、読めないときは確認できない旨を出す", () => {
    expect(currentIndexLabel(settings({}))).toBe("NEIGHBORS 48 / EFCONSTRUCTION 800");
    expect(
      currentIndexLabel(settings({ actual_neighbors: null, actual_efconstruction: null }))
    ).toBe("確認できません");
  });
});


describe("orderedProfiles", () => {
  it("高速・バランス・高精度の順に並べても保存済みの選択とパラメータを維持する", () => {
    const profiles: VectorIndexSettingsData["profiles"] = [
      { name: "balanced", target_accuracy: 95, neighbors: 32, efconstruction: 500 },
      { name: "accurate", target_accuracy: 98, neighbors: 48, efconstruction: 800 },
      { name: "fast", target_accuracy: 85, neighbors: 16, efconstruction: 300 },
    ].map((profile) => ({
      ...profile,
      name: profile.name as VectorIndexSettingsData["profile"],
      origin: "test",
      recommended_for: [],
      distance: "COSINE",
      selected: profile.name === "accurate",
      requires_reprovision: false,
      index_status: "match",
    }));
    const ordered = orderedProfiles(profiles);
    expect(ordered.map((profile) => profile.name)).toEqual(["fast", "balanced", "accurate"]);
    expect(ordered[0]).toBe(profiles[2]);
    expect(ordered[1]).toBe(profiles[0]);
    expect(ordered[2]).toBe(profiles[1]);
    expect(ordered.filter((profile) => profile.selected).map((profile) => profile.name)).toEqual(["accurate"]);
    expect(profiles.map((profile) => profile.name)).toEqual(["balanced", "accurate", "fast"]);
  });
});
