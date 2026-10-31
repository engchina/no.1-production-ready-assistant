// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  AnswerNoticeSettingsPage,
  answerNoticePayload,
  composeAnswerNotice,
  normalizeAnswerNoticeText,
  type AnswerNoticeApi,
  type AnswerNoticeSettingsData,
  type AnswerNoticeSettingsUpdate,
} from "../src";

// #2405: 回答の注意書きの設定（画面は 3 製品で共通、設定は製品ごと。#2449）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DEFAULT_TEXT = "既定の 1 行目\n既定の 2 行目";

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function settings(overrides: Partial<AnswerNoticeSettingsData> = {}): AnswerNoticeSettingsData {
  const text = overrides.text ?? null;
  const contact = overrides.contact ?? null;
  return {
    enabled: true,
    text,
    default_text: DEFAULT_TEXT,
    contact,
    default_contact: "既定の窓口",
    contact_template: "解決しない場合は、{contact}へお問い合わせください。",
    effective_text: `${text ?? DEFAULT_TEXT}\n解決しない場合は、${contact ?? "既定の窓口"}へお問い合わせください。`,
    max_chars: 500,
    contact_max_chars: 200,
    updated_at: null,
    ...overrides,
  };
}

function contactInput() {
  const element = host.querySelector<HTMLInputElement>("#answer-notice-contact");
  if (!element) throw new Error("contact input not found");
  return element;
}

async function typeContact(value: string) {
  const element = contactInput();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(async () => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function flush() {
  for (let i = 0; i < 20; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

async function renderPage(api: AnswerNoticeApi) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ConfirmProvider>
            <AnswerNoticeSettingsPage api={api} />
          </ConfirmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
  });
  await flush();
}

function textarea() {
  const element = host.querySelector<HTMLTextAreaElement>("#answer-notice-text");
  if (!element) throw new Error("textarea not found");
  return element;
}

function buttonByText(text: string): HTMLButtonElement {
  const button = [...host.querySelectorAll<HTMLButtonElement>("button")].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
  if (!button) throw new Error(`button not found: ${text}`);
  return button;
}

async function typeText(value: string) {
  const element = textarea();
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  await act(async () => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submit() {
  const form = textarea().closest("form");
  if (!form) throw new Error("form not found");
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await flush();
}

describe("answerNoticePayload", () => {
  it("改行と行末の空白をそろえ、既定の文と同じなら null（既定に戻す）を送る", () => {
    expect(normalizeAnswerNoticeText(" a  \r\nb\r\n\n")).toBe("a\nb");
    expect(answerNoticePayload({ enabled: true, text: `${DEFAULT_TEXT}\n`, contact: "既定の窓口" }, DEFAULT_TEXT, "既定の窓口")).toEqual({
      enabled: true,
      text: null,
      contact: null,
    });
    expect(answerNoticePayload({ enabled: false, text: "独自の文", contact: " 窓口 A\n（内線 1） " }, DEFAULT_TEXT, "既定の窓口")).toEqual({
      enabled: false,
      text: "独自の文",
      contact: "窓口 A （内線 1）",
    });
    expect(composeAnswerNotice("本文", "窓口 A")).toBe("本文\n解決しない場合は、窓口 Aへお問い合わせください。");
    expect(composeAnswerNotice("本文", "")).toBe("本文");
  });
});

describe("AnswerNoticeSettingsPage", () => {
  it("未設定なら既定の文を入力欄と表示の確認に出し、「既定の文に戻す」は押せない", async () => {
    await renderPage({ get: async () => settings(), update: vi.fn() });
    expect(textarea().value).toBe(DEFAULT_TEXT);
    expect(host.querySelector('[data-testid="answer-notice-source"]')?.textContent).toBe("既定の文を使っています。");
    expect(host.querySelector('[data-testid="answer-notice-preview-notice"]')?.textContent).toContain("既定の 2 行目");
    expect(buttonByText("既定の文に戻す").disabled).toBe(true);
  });

  it("入力した文を保存し、表示の確認も入力中の値に変わる", async () => {
    const update = vi.fn(async (payload: AnswerNoticeSettingsUpdate) => settings({ text: payload.text }));
    await renderPage({ get: async () => settings(), update });
    await typeText("窓口は https://example.com/help です。");
    const preview = host.querySelector('[data-testid="answer-notice-preview-notice"]');
    expect(preview?.querySelector("a")?.getAttribute("href")).toBe("https://example.com/help");
    await submit();
    expect(update).toHaveBeenCalledWith({ enabled: true, text: "窓口は https://example.com/help です。", contact: null });
    expect(host.querySelector('[data-testid="answer-notice-source"]')?.textContent).toBe(
      "管理者が変えた文を使っています。",
    );
  });

  it("「既定の文に戻す」で入力欄を既定の文にし、保存すると null を送る", async () => {
    const update = vi.fn(async (payload: AnswerNoticeSettingsUpdate) => settings({ text: payload.text }));
    await renderPage({ get: async () => settings({ text: "独自の文", contact: "窓口 A" }), update });
    expect(textarea().value).toBe("独自の文");
    expect(contactInput().value).toBe("窓口 A");
    await act(async () => buttonByText("既定の文に戻す").click());
    expect(textarea().value).toBe(DEFAULT_TEXT);
    expect(contactInput().value).toBe("既定の窓口");
    await submit();
    expect(update).toHaveBeenCalledWith({ enabled: true, text: null, contact: null });
  });

  it("無効にすると表示の確認に注意書きを出さず、無効を保存する", async () => {
    const update = vi.fn(async (payload: AnswerNoticeSettingsUpdate) =>
      settings({ enabled: payload.enabled, text: payload.text }),
    );
    await renderPage({ get: async () => settings(), update });
    const toggle = host.querySelector<HTMLButtonElement>('[data-testid="answer-notice-enabled"]');
    expect(toggle?.getAttribute("aria-checked")).toBe("true");
    await act(async () => toggle?.click());
    expect(host.querySelector('[data-testid="answer-notice-preview-notice"]')).toBeNull();
    expect(host.querySelector('[data-testid="answer-notice-preview"]')?.textContent).toContain("注意書きは無効です");
    await submit();
    expect(update).toHaveBeenCalledWith({ enabled: false, text: null, contact: null });
  });

  it("空の本文は保存せず、欄の下に理由を出す", async () => {
    const update = vi.fn();
    await renderPage({ get: async () => settings(), update });
    await typeText("   ");
    await submit();
    expect(update).not.toHaveBeenCalled();
    expect(host.textContent).toContain("本文を入力してください。");
    expect(textarea().getAttribute("aria-invalid")).toBe("true");
  });
});

describe("AnswerNoticeSettingsPage の窓口（#2400）", () => {
  it("窓口を書くと表示の確認の案内の 1 行に入り、保存する", async () => {
    const update = vi.fn(async (payload: AnswerNoticeSettingsUpdate) =>
      settings({ text: payload.text, contact: payload.contact }),
    );
    await renderPage({ get: async () => settings(), update });
    expect(contactInput().value).toBe("既定の窓口");
    const preview = () => host.querySelector('[data-testid="answer-notice-preview-notice"]')?.textContent ?? "";
    expect(preview()).toContain("解決しない場合は、既定の窓口へお問い合わせください。");
    await typeContact("窓口 A（https://example.com/help）");
    expect(preview()).toContain("解決しない場合は、窓口 A（https://example.com/help）へお問い合わせください。");
    await submit();
    expect(update).toHaveBeenCalledWith({ enabled: true, text: null, contact: "窓口 A（https://example.com/help）" });
  });

  it("長すぎる窓口は保存せず、欄の下に理由を出す", async () => {
    const update = vi.fn();
    await renderPage({ get: async () => settings(), update });
    await typeContact("あ".repeat(201));
    await submit();
    expect(update).not.toHaveBeenCalled();
    expect(host.textContent).toContain("窓口は 200 字以内で入力してください。");
  });
});
