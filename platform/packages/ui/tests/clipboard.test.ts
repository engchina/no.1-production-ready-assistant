import { afterEach, describe, expect, it, vi } from "vitest";

import { copyTextToClipboard } from "../src/lib/clipboard";

// #2099: NL2SQL・system-settings の copyTextToClipboard と RAG の copyTextFromPromise を共有 UI の 1 つにした。

class FakeClipboardItem {
  constructor(readonly items: Record<string, Promise<Blob> | Blob>) {}
}

function fakeLegacyDocument(expected: string) {
  const calls = { selected: false, selectionEnd: -1, copied: false, focusRestored: false };
  const textarea = {
    value: "",
    style: {},
    setAttribute() {},
    focus() {},
    select() {
      calls.selected = true;
    },
    setSelectionRange(_start: number, end: number) {
      calls.selectionEnd = end;
    },
  };
  const document = {
    activeElement: {
      focus() {
        calls.focusRestored = true;
      },
    },
    body: {
      appendChild(node: unknown) {
        expect(node).toBe(textarea);
      },
      removeChild(node: unknown) {
        expect(node).toBe(textarea);
      },
    },
    createElement(tagName: string) {
      expect(tagName).toBe("textarea");
      return textarea;
    },
    execCommand(command: string) {
      expect(command).toBe("copy");
      expect(textarea.value).toBe(expected);
      calls.copied = true;
      return true;
    },
  };
  return { document, calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("copyTextToClipboard", () => {
  it("非 secure context では隠した textarea を選んで execCommand で書き、フォーカスを戻す", async () => {
    const { document, calls } = fakeLegacyDocument("copy over http");
    vi.stubGlobal("isSecureContext", false);
    vi.stubGlobal("navigator", { clipboard: { writeText: () => Promise.reject(new Error("insecure")) } });
    vi.stubGlobal("document", document);

    await copyTextToClipboard("copy over http");

    expect(calls).toEqual({ selected: true, selectionEnd: "copy over http".length, copied: true, focusRestored: true });
  });

  it("secure context では Clipboard API を使い、execCommand を使わない", async () => {
    const writeText = vi.fn(async () => undefined);
    const execCommand = vi.fn(() => true);
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    vi.stubGlobal("document", { execCommand });

    await copyTextToClipboard("secure copy");

    expect(writeText).toHaveBeenCalledWith("secure copy");
    expect(execCommand).not.toHaveBeenCalled();
  });

  it("secure context の Clipboard API の失敗は reject する（呼び出し側が失敗を出す）", async () => {
    const execCommand = vi.fn(() => true);
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("navigator", {
      clipboard: { writeText: async () => Promise.reject(new Error("clipboard denied")) },
    });
    vi.stubGlobal("document", { execCommand });

    await expect(copyTextToClipboard("secure copy")).rejects.toThrow("clipboard denied");
    expect(execCommand).not.toHaveBeenCalled();
  });

  it("Promise の内容は、ClipboardItem が使えるとき取得を待つ前に clipboard.write へ渡す（Safari）", async () => {
    let resolveText: (value: string) => void = () => undefined;
    const text = new Promise<string>((resolve) => {
      resolveText = resolve;
    });
    const written: FakeClipboardItem[] = [];
    const writeText = vi.fn();
    const write = vi.fn(async (items: FakeClipboardItem[]) => {
      written.push(...items);
      await items[0].items["text/plain"];
    });
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("ClipboardItem", FakeClipboardItem);
    vi.stubGlobal("navigator", { clipboard: { writeText, write } });

    const copying = copyTextToClipboard(text);
    // 取得が終わる前に write が呼ばれている（Safari のユーザー操作の制約）。
    expect(write).toHaveBeenCalledTimes(1);
    resolveText("# 見出し");
    await copying;

    const blob = await written[0].items["text/plain"];
    expect(blob.type).toBe("text/plain");
    await expect(blob.text()).resolves.toBe("# 見出し");
    expect(writeText).not.toHaveBeenCalled();
  });

  it("ClipboardItem が無いときは、取得を待ってから writeText する", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("ClipboardItem", undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await copyTextToClipboard(Promise.resolve("<article />"));

    expect(writeText).toHaveBeenCalledWith("<article />");
  });

  it("取得に失敗したら reject する", async () => {
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("ClipboardItem", FakeClipboardItem);
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: vi.fn(async () => undefined),
        write: vi.fn(async (items: FakeClipboardItem[]) => {
          await items[0].items["text/plain"];
        }),
      },
    });

    await expect(copyTextToClipboard(Promise.reject(new Error("404")))).rejects.toThrow("404");
  });
});
