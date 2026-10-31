// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ModelSettingsPage,
  type ModelSettingsApi,
  type ModelSettingsData,
  type ModelSettingsTestRequest,
} from "../src";

// テストの結果は、操作した欄の行のすぐ下に出す（messaging.md §10。#705）。OCI Generative AI の
// Embedding のテストの結果を、下の Rerank の欄より後ろに出さない。

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

const pending = () => new Promise<never>(() => undefined);

function data(revision: string, rerankModel = "cohere.rerank-v4.0-fast"): ModelSettingsData {
  return {
    settings: {
      enterprise_ai: {
        connections: [
          {
            connection_id: "primary",
            endpoint: "https://primary.example",
            project_ocid: "ocid1.generativeaiproject.oc1..primary",
            api_key: "",
            has_api_key: true,
            clear_api_key: false,
          },
        ],
        models: [
          { model_id: "vlm", display_name: "", vision_enabled: true, connection_id: "primary" },
        ],
        default_text_model_id: "vlm",
        default_vision_model_id: "vlm",
        api_path: "/responses",
        vlm_input_mode: "auto",
        text_payload_template: "",
        vision_payload_template: "",
        text_response_path: "",
        vision_response_path: "",
        timeout_seconds: 600,
        max_retries: 3,
      },
      generative_ai: {
        embedding_model: "cohere.embed-v4.0",
        embedding_dim: 1536,
        rerank_model: rerankModel,
      },
    },
    model_settings_file: "model-settings.json",
    source: "runtime",
    secret_source: "environment",
    legacy_secret_detected: false,
    revision,
  };
}

async function flush() {
  for (let i = 0; i < 20; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

async function renderPage(api: ModelSettingsApi) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ConfirmProvider>
            <ModelSettingsPage api={api} />
          </ConfirmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
  });
  await flush();
}

function input(id: string) {
  return host.querySelector<HTMLInputElement>(`#${id}`);
}

function buttonByText(text: string, scope: ParentNode = host): HTMLButtonElement | undefined {
  return [...scope.querySelectorAll<HTMLButtonElement>("button")].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
}

async function setInputValue(id: string, value: string) {
  const element = input(id);
  if (!element) throw new Error(`input not found: ${id}`);
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(async () => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("OCI Generative AI のテストの結果の位置", () => {
  it("Embedding の結果は Embedding の行の下・Rerank の欄の上、Rerank の結果は Rerank の欄の下に出す", async () => {
    const testModelSettings = vi.fn(async (request: ModelSettingsTestRequest) => ({
      status: "success" as const,
      target_type: request.target_type,
      model_id: request.model_id,
      message: `${request.target_type} の結果`,
      troubleshooting: [],
      raw_error: null,
      error_type: null,
      elapsed_ms: 10,
      checked_at: "2026-10-15T00:00:00Z",
      details: {},
    }));
    await renderPage({
      getModelSettings: async () => data("rev-1"),
      updateModelSettings: pending,
      testModelSettings,
    });
    const form = input("genai-embedding-model")?.closest("form");
    if (!form) throw new Error("generative ai form not found");
    const [embeddingTest, rerankTest] = [...form.querySelectorAll<HTMLButtonElement>("button")].filter(
      (button) => button.textContent?.trim() === "テスト",
    );
    if (!embeddingTest || !rerankTest) throw new Error("test buttons not found");

    await act(async () => embeddingTest.click());
    await flush();
    const embeddingResult = [...form.querySelectorAll<HTMLElement>("*")].find(
      (element) => element.textContent?.trim().startsWith("embedding の結果") && element.children.length === 0,
    );
    const rerankField = input("genai-rerank-model");
    if (!embeddingResult || !rerankField) throw new Error("embedding result not found");
    // 文書の順で、Embedding の欄 → Embedding の結果 → Rerank の欄。
    expect(input("genai-embedding-model")!.compareDocumentPosition(embeddingResult) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(embeddingResult.compareDocumentPosition(rerankField) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await act(async () => rerankTest.click());
    await flush();
    const rerankResult = [...form.querySelectorAll<HTMLElement>("*")].find(
      (element) => element.textContent?.trim().startsWith("rerank の結果") && element.children.length === 0,
    );
    if (!rerankResult) throw new Error("rerank result not found");
    expect(rerankField.compareDocumentPosition(rerankResult) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
