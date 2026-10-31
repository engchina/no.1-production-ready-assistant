import { describe, expect, it } from "vitest";

import { chunkingPayload } from "./ChunkingSettingsClient";

const SAVED: Parameters<typeof chunkingPayload>[0] = {
  strategy: "structure_aware",
  chunk_size: 800,
  overlap: 120,
  min_chars: 120,
  delimiter: "\\n\\n",
  chunk_child_target_chars: 1000,
  chunk_table_child_target_chars: 3000,
  chunk_parent_target_chars: 6000,
  chunk_parent_max_pages: 3,
  chunk_parent_max_children: 12,
};

describe("chunkingPayload（Issue 966）", () => {
  it("選んだ方式で出していない欄の空・範囲外は保存済みの値で送る", () => {
    const payload = chunkingPayload(
      {
        ...SAVED,
        strategy: "small_to_big",
        min_chars: Number.NaN,
        delimiter: "  ",
      },
      SAVED
    );
    expect(payload.min_chars).toBe(120);
    expect(payload.delimiter).toBe("\\n\\n");
  });

  it("隠れた欄でも正しい値は利用者の入力のまま送る", () => {
    const payload = chunkingPayload({ ...SAVED, strategy: "fixed_delimiter", min_chars: 40 }, SAVED);
    expect(payload.min_chars).toBe(40);
  });

  it("出している欄は置き換えない（画面の検証が欄の直下に出す）", () => {
    const payload = chunkingPayload(
      { ...SAVED, strategy: "small_to_big", chunk_child_target_chars: Number.NaN, min_chars: 5000 },
      SAVED
    );
    expect(payload.chunk_child_target_chars).toBeNaN();
    expect(payload.min_chars).toBe(120);
  });
});
