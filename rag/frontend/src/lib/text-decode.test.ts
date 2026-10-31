import { describe, expect, it } from "vitest";

import { charsetFromContentType, decodeText } from "./text-decode";

describe("charsetFromContentType", () => {
  it("text/plain の charset を取り出す", () => {
    expect(charsetFromContentType("text/plain; charset=gb18030")).toBe("gb18030");
  });

  it("大文字・引用符・余白を正規化する", () => {
    expect(charsetFromContentType('text/plain; CHARSET="Shift_JIS"')).toBe("Shift_JIS");
  });

  it("charset が無ければ utf-8 を既定にする", () => {
    expect(charsetFromContentType("text/plain")).toBe("utf-8");
    expect(charsetFromContentType(null)).toBe("utf-8");
  });
});

describe("decodeText", () => {
  function encode(text: string, charset: string): ArrayBuffer {
    // Node の Buffer は legacy エンコーディングをサポートしないため
    // UTF-8 と非 ASCII バイト列で代表させる
    const buf = Buffer.from(text, charset as BufferEncoding);
    return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  }

  it("UTF-8 を正しくデコードする", () => {
    expect(decodeText(encode("となりのトトロ", "utf-8"), "utf-8")).toBe("となりのトトロ");
  });

  it("未知のラベルは UTF-8 にフォールバックする", () => {
    expect(decodeText(encode("トトロ", "utf-8"), "x-not-a-charset")).toBe("トトロ");
  });

  it("非 UTF-8 (gb18030) のバイト列を正しくデコードする", () => {
    // GB18030 の "中文" バイト列
    const bytes = new Uint8Array([0xd6, 0xd0, 0xce, 0xc4]);
    expect(decodeText(bytes.buffer, "gb18030")).toBe("中文");
  });
});
