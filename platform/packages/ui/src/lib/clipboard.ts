/**
 * 文字をクリップボードへ書く（共有の `CopyButton` が使う。#2099 で NL2SQL・system-settings・RAG の実装を 1 つにした）。
 *
 * - `value` が Promise（押してから取得する内容）で `ClipboardItem` が使えるときは、取得を待つ前に Promise のまま
 *   `clipboard.write` へ渡す。Safari はクリックの処理の中で clipboard を呼ばないと書き込みを拒否するため（#561）。
 * - secure context でない（http の配備など）・Clipboard API が無いときは、隠した textarea を選んで
 *   `document.execCommand("copy")` で書き、フォーカスを元の要素へ戻す。
 * - 取得の失敗も書き込みの失敗も、返す Promise の reject になる（呼び出し側で失敗を表示する）。
 */
export async function copyTextToClipboard(value: string | Promise<string>): Promise<void> {
  const clipboard = globalThis.navigator?.clipboard;
  if (globalThis.isSecureContext && clipboard) {
    const ClipboardItemCtor = globalThis.ClipboardItem;
    if (typeof value !== "string" && ClipboardItemCtor && typeof clipboard.write === "function") {
      const blob = value.then((text) => new Blob([text], { type: "text/plain" }));
      await clipboard.write([new ClipboardItemCtor({ "text/plain": blob })]);
      return;
    }
    if (typeof clipboard.writeText === "function") {
      await clipboard.writeText(await value);
      return;
    }
  }
  if (copyTextWithLegacySelection(await value)) return;
  throw new Error("Clipboard write is not available in this browser context.");
}

function copyTextWithLegacySelection(value: string): boolean {
  if (typeof document === "undefined" || !document.body || typeof document.execCommand !== "function") {
    return false;
  }

  const activeElement = document.activeElement;
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.setAttribute("aria-hidden", "true");
  Object.assign(textarea.style, {
    position: "fixed",
    top: "0",
    left: "0",
    width: "1px",
    height: "1px",
    padding: "0",
    border: "0",
    opacity: "0",
    pointerEvents: "none",
  });

  document.body.appendChild(textarea);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, value.length);
    return document.execCommand("copy");
  } finally {
    document.body.removeChild(textarea);
    restoreFocus(activeElement);
  }
}

function restoreFocus(element: Element | null): void {
  const focus = (element as { focus?: (options?: FocusOptions) => void } | null)?.focus;
  if (typeof focus !== "function") return;
  try {
    focus.call(element, { preventScroll: true });
  } catch {
    focus.call(element);
  }
}
