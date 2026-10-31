import { describe, expect, it } from "vitest";

import type { ToolCallDataScope } from "./api";
import { dataScopeNotes } from "./data-scope";
import { formatDateTime, formatNumber } from "./format";

// 実行の step に出す、データの範囲の記録の文（#1378）と、表示用のフォーマッタ。

const scope = (patch: Partial<ToolCallDataScope>): ToolCallDataScope => ({
  connection: "nl2sql",
  allowed_profile_ids: ["sales", "hr"],
  ...patch,
});

describe("dataScopeNotes", () => {
  it("範囲が無い・効かない呼び出しは空", () => {
    expect(dataScopeNotes(null)).toEqual([]);
    expect(dataScopeNotes(undefined)).toEqual([]);
    expect(dataScopeNotes(scope({}))).toEqual([]);
  });

  it("action ごとに、使ったプロファイルと求められたプロファイルを文にする", () => {
    expect(dataScopeNotes(scope({ action: "filled", profile_id: "sales" }))).toEqual(["sales を範囲から補完"]);
    expect(dataScopeNotes(scope({ action: "overridden", profile_id: "sales", requested_profile_id: "all" }))).toEqual([
      "all を範囲の sales に置き換え",
    ]);
    expect(dataScopeNotes(scope({ action: "kept", profile_id: "hr" }))).toEqual(["hr（範囲内）"]);
    expect(dataScopeNotes(scope({ action: "rejected", requested_profile_id: "all" }))).toEqual([
      "all は範囲外のため実行していません",
    ]);
    expect(dataScopeNotes(scope({ action: "filtered" }))).toEqual(["結果を範囲（sales、hr）に絞り込み"]);
  });

  it("使わなかったナレッジベースは action と別の文で足す", () => {
    expect(dataScopeNotes(scope({ action: "kept", profile_id: "hr", ignored_knowledge_base_ids: ["kb1", "kb2"] }))).toEqual([
      "hr（範囲内）",
      "指定されたナレッジベース（kb1、kb2）は使っていません",
    ]);
  });
});

describe("format", () => {
  it("件数は ja-JP のカンマ区切り", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
  });

  it("未設定・無効な日時はダッシュ", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime("")).toBe("—");
    expect(formatDateTime("not-a-date")).toBe("—");
  });

  it("日時は「MM/DD HH:mm」（システムの設定の時間帯。既定は UTC）", () => {
    expect(formatDateTime("2026-01-01T18:04:00Z")).toBe("01/01 18:04");
  });
});
