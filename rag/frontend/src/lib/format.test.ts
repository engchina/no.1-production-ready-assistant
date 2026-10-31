import { setDisplayTimeZone } from "@production-ready/ui";
import { afterEach, describe, expect, it } from "vitest";

import { formatBytes, formatDateTime, formatNumber, parseApiDateTime } from "./format";

describe("formatBytes", () => {
  it("null は em-dash", () => expect(formatBytes(null)).toBe("—"));
  it("バイト", () => expect(formatBytes(512)).toBe("512 B"));
  it("KB", () => expect(formatBytes(2048)).toBe("2.0 KB"));
  it("MB", () => expect(formatBytes(1048576)).toBe("1.0 MB"));
});

describe("formatNumber", () => {
  it("カンマ区切り", () => expect(formatNumber(38421)).toBe("38,421"));
});

describe("formatDateTime", () => {
  afterEach(() => setDisplayTimeZone(null));
  it("無効値は em-dash", () => expect(formatDateTime("not-a-date")).toBe("—"));
  it("システムの設定の時間帯で書く", () => {
    // 既定は UTC（#3274）。
    expect(formatDateTime("2026-06-14T16:42:00Z")).toBe("06/14 16:42");
    setDisplayTimeZone("Asia/Tokyo");
    expect(formatDateTime("2026-06-14T16:42:00Z")).toBe("06/15 01:42");
  });
  it("ISO を整形する", () => expect(formatDateTime("2026-06-14T10:42:00Z")).not.toBe(""));
  it("タイムゾーン無しの API ISO は UTC として扱う", () => {
    expect(parseApiDateTime("2026-06-23T00:34:00")?.toISOString()).toBe(
      "2026-06-23T00:34:00.000Z"
    );
    expect(formatDateTime("2026-06-23T00:34:00")).toBe(
      formatDateTime("2026-06-23T00:34:00Z")
    );
  });
});
