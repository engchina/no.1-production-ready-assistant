import { describe, expect, it } from "vitest";

import { splitExtractedText } from "./extracted-text";

const PNG_URI =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
const SVG_URI = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=";
const BARE = "A".repeat(300);

describe("splitExtractedText", () => {
  it("通常テキストはそのまま、media は空", () => {
    const out = splitExtractedText("これは普通の本文です。");
    expect(out.text).toBe("これは普通の本文です。");
    expect(out.media).toHaveLength(0);
  });

  it("png data URI は image として分離し、残りを本文に残す", () => {
    const out = splitExtractedText(`図1 ${PNG_URI} を参照`);
    expect(out.media).toHaveLength(1);
    expect(out.media[0]).toMatchObject({ kind: "image", fragment: false });
    expect(out.media[0].src).toBe(PNG_URI);
    expect(out.text).toContain("図1");
    expect(out.text).toContain("を参照");
    expect(out.text).not.toContain("base64");
  });

  it("svg data URI は blob 扱い(img で描画しない)", () => {
    const out = splitExtractedText(SVG_URI);
    expect(out.media).toHaveLength(1);
    expect(out.media[0]).toMatchObject({ kind: "blob", fragment: false });
    expect(out.media[0].src).toBeUndefined();
    expect(out.text).toBe("");
  });

  it("prefix 無しの長い base64 断片は blob fragment", () => {
    const out = splitExtractedText(BARE);
    expect(out.media).toHaveLength(1);
    expect(out.media[0]).toMatchObject({ kind: "blob", fragment: true });
    expect(out.text).toBe("");
  });

  it("テキスト + 画像 + 断片の混在を正しく分離", () => {
    const out = splitExtractedText(`前 ${PNG_URI} 中 ${BARE} 後`);
    expect(out.text).toContain("前");
    expect(out.text).toContain("中");
    expect(out.text).toContain("後");
    expect(out.media).toHaveLength(2);
    expect(out.media[0].kind).toBe("image");
    expect(out.media[1].fragment).toBe(true);
  });
});
