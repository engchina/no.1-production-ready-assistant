import { describe, expect, it } from "vitest";

import { ACCEPTED_UPLOAD_TYPES } from "@/lib/upload-selection";

describe("Dropzone accepted upload types", () => {
  const accepted = ACCEPTED_UPLOAD_TYPES.split(",");

  it("does not advertise broad image/* support beyond backend parser contracts", () => {
    expect(accepted).not.toContain("image/*");
    expect(accepted).not.toContain(".bmp");
    expect(accepted).not.toContain("image/bmp");
  });

  it("matches supported image and table entry points", () => {
    expect(accepted).toEqual(
      expect.arrayContaining([
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
        ".tsv",
        "image/gif",
        "image/webp",
        "image/tif",
        "image/tiff",
        "text/tab-separated-values",
      ])
    );
  });

  it("includes upload-metadata-only formats that backend skips with explicit warnings", () => {
    expect(accepted).toEqual(
      expect.arrayContaining([
        ".msg",
        ".doc",
        ".ppt",
        ".xls",
        "application/vnd.ms-outlook",
        "application/x-msg",
        "application/msword",
        "application/vnd.ms-powerpoint",
        "application/vnd.ms-excel",
      ])
    );
  });

  // 音声・動画は取り込まない形式（#3369）。
  it("does not offer audio or video files, which are not ingested", () => {
    for (const item of [".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav", ".mp4", ".mov", ".webm"]) {
      expect(accepted).not.toContain(item);
    }
    expect(accepted.filter((item) => item.startsWith("audio/") || item.startsWith("video/"))).toEqual([]);
    expect(accepted).not.toContain("application/ogg");
  });

  it("advertises formats read without pandoc and hides formats whose parsers are not installed", () => {
    // pandoc・pi-heif を入れないため（#3351）。
    expect(accepted).toEqual(
      expect.arrayContaining([
        ".rst",
        ".org",
        ".odt",
        ".rtf",
        "text/x-rst",
        "text/org",
        "application/vnd.oasis.opendocument.text",
        "application/rtf",
        "text/rtf",
      ])
    );
    for (const item of [".heic", ".heif", ".epub", "image/heic", "image/heif", "application/epub+zip"]) {
      expect(accepted).not.toContain(item);
    }
  });

  it("keeps semantic HTML MIME variants aligned with SourceProfile", () => {
    expect(accepted).toEqual(expect.arrayContaining([".xhtml", "application/xhtml+xml"]));
  });

  it("keeps JSON Lines variants aligned with backend upload whitelist", () => {
    expect(accepted).toEqual(
      expect.arrayContaining([
        ".jsonl",
        ".ndjson",
        "application/jsonl",
        "application/jsonlines",
        "application/ndjson",
        "application/x-ndjson",
      ])
    );
  });
});
