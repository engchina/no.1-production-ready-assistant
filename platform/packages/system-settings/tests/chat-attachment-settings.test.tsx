// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CHAT_ATTACHMENT_SETTINGS_NAV_ITEM,
  ChatAttachmentSettingsPage,
  type ChatAttachmentSettingsApi,
  type ChatAttachmentSettingsData,
} from "../src";

// #2517: チャットの添付の設定（画面は 3 製品で共通、設定は製品ごと。既定は無効）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

function settings(overrides: Partial<ChatAttachmentSettingsData> = {}): ChatAttachmentSettingsData {
  return { enabled: false, max_bytes: 20 * 1024 * 1024, max_files: 10, ttl_hours: 24, ...overrides };
}

async function flush() {
  for (let i = 0; i < 20; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

async function renderPage(api: ChatAttachmentSettingsApi) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ConfirmProvider>
            <ChatAttachmentSettingsPage api={api} />
          </ConfirmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
  });
  await flush();
}

const toggle = () => host.querySelector<HTMLButtonElement>('[data-testid="chat-attachments-enabled"]')!;

describe("ChatAttachmentSettingsPage", () => {
  it("既定の無効と上限を出し、有効にして保存する", async () => {
    const update = vi.fn(async ({ enabled }: { enabled: boolean }) => settings({ enabled }));
    await renderPage({ get: async () => settings(), update });
    expect(toggle().getAttribute("aria-checked")).toBe("false");
    const limits = host.querySelector('[data-testid="chat-attachments-limits"]')!.textContent ?? "";
    expect(limits).toContain("20 MB");
    expect(limits).toContain("10 件");
    expect(limits).toContain("最後に使ってから 24 時間");
    await act(async () => toggle().click());
    expect(toggle().getAttribute("aria-checked")).toBe("true");
    await act(async () => {
      host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await flush();
    expect(update).toHaveBeenCalledWith({ enabled: true });
  });

  it("ナビの項目は運用設定の Paperclip", () => {
    expect(CHAT_ATTACHMENT_SETTINGS_NAV_ITEM.href).toBe("/settings/chat-attachments");
    expect(CHAT_ATTACHMENT_SETTINGS_NAV_ITEM.labelKey).toBe("nav.settingsChatAttachments");
  });
});
