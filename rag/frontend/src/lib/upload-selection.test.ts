import { describe, expect, it } from "vitest";

import { mergeUploadSelection, uploadFileProblem } from "./upload-selection";

function file(name: string, size: number, lastModified = 1): File {
  return new File([new Uint8Array(size)], name, { lastModified });
}

describe("uploadFileProblem", () => {
  it("形式・空・上限超えの順に送れない理由を返し、送れるなら null", () => {
    expect(uploadFileProblem(file("tool.exe", 5), 10)).toBe("unsupported");
    expect(uploadFileProblem(file("README", 5), 10)).toBe("unsupported");
    expect(uploadFileProblem(file("empty.txt", 0), 10)).toBe("empty");
    expect(uploadFileProblem(file("huge.PDF", 11), 10)).toBe("tooLarge");
    expect(uploadFileProblem(file("規程.md", 10), 10)).toBeNull();
  });

  it("HEIC / HEIF・EPUB は変換を案内する理由で止め、ODT・RTF・RST・Org は送る", () => {
    // pandoc・pi-heif を入れないため（#3351）。
    expect(uploadFileProblem(file("photo.HEIC", 5), 10)).toBe("heicImage");
    expect(uploadFileProblem(file("photo.heif", 5), 10)).toBe("heicImage");
    expect(uploadFileProblem(file("book.epub", 5), 10)).toBe("epubBook");
    for (const name of ["memo.odt", "memo.rtf", "guide.rst", "notes.org"]) {
      expect(uploadFileProblem(file(name, 5), 10)).toBeNull();
    }
  });

  // #3369
  it("音声・動画は取り込まない形式として案内する理由で止める", () => {
    for (const name of ["voice.mp3", "VOICE.WAV", "record.ogg", "meeting.m4a", "clip.mp4", "clip.mov", "clip.webm"]) {
      expect(uploadFileProblem(file(name, 5), 10)).toBe("audioVideo");
    }
  });
});

describe("mergeUploadSelection", () => {
  it("同じファイル（名前・サイズ・更新日時）は重ねず、選んだ順に足す", () => {
    const a = file("a.txt", 3);
    const b = file("b.txt", 3);
    const merged = mergeUploadSelection([a], [file("a.txt", 3), b, file("a.txt", 3, 2)]);
    expect(merged.map((item) => `${item.name}:${item.lastModified}`)).toEqual([
      "a.txt:1",
      "b.txt:1",
      "a.txt:2",
    ]);
  });
});
