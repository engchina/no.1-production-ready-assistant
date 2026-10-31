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
  type ModelSettingsUpdatePayload,
} from "../src";

// #1416: 保存が成功したら、製品が渡した cache（既定のモデルから作る「回答するモデル」の候補など）も捨てる。
// 捨てないと、チャットへ戻ったときに保存前の既定のモデルを出す。

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

function data(rerankModel: string): ModelSettingsData {
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
    revision: "rev-1",
  };
}

async function flush() {
  for (let i = 0; i < 20; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

async function setInputValue(id: string, value: string) {
  const element = host.querySelector<HTMLInputElement>(`#${id}`);
  if (!element) throw new Error(`input not found: ${id}`);
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(async () => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submitGenerativeAi() {
  const form = host.querySelector("#genai-rerank-model")?.closest("form");
  if (!form) throw new Error("generative ai form not found");
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await flush();
}

describe("ModelSettingsPage の保存で製品の cache を捨てる（#1416）", () => {
  it("保存が成功したら invalidateQueryKeys の query を捨て、ほかの query は残す", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["chat", "models"], [{ model_id: "old-llm" }]);
    client.setQueryData(["search", "models"], [{ model_id: "old-llm" }]);
    client.setQueryData(["unrelated"], "keep");
    const updateModelSettings = vi.fn(async (payload: ModelSettingsUpdatePayload) =>
      data(payload.generative_ai.rerank_model),
    );
    const api: ModelSettingsApi = {
      getModelSettings: async () => data("cohere.rerank-v4.0-fast"),
      updateModelSettings,
      testModelSettings: pending,
    };
    await act(async () => {
      root.render(
        <MemoryRouter>
          <QueryClientProvider client={client}>
            <ConfirmProvider>
              <ModelSettingsPage
                api={api}
                invalidateQueryKeys={[["chat", "models"], ["search", "models"]]}
              />
            </ConfirmProvider>
          </QueryClientProvider>
        </MemoryRouter>,
      );
    });
    await flush();
    expect(client.getQueryState(["chat", "models"])?.isInvalidated).toBe(false);

    await setInputValue("genai-rerank-model", "cohere.rerank-v4.0");
    await submitGenerativeAi();

    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    expect(client.getQueryState(["chat", "models"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["search", "models"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["unrelated"])?.isInvalidated).toBe(false);
  });

  it("保存に失敗したら製品の cache を捨てない", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["chat", "models"], [{ model_id: "old-llm" }]);
    const api: ModelSettingsApi = {
      getModelSettings: async () => data("cohere.rerank-v4.0-fast"),
      updateModelSettings: vi.fn(async () => {
        throw new Error("failed");
      }),
      testModelSettings: pending,
    };
    await act(async () => {
      root.render(
        <MemoryRouter>
          <QueryClientProvider client={client}>
            <ConfirmProvider>
              <ModelSettingsPage api={api} invalidateQueryKeys={[["chat", "models"]]} />
            </ConfirmProvider>
          </QueryClientProvider>
        </MemoryRouter>,
      );
    });
    await flush();

    await setInputValue("genai-rerank-model", "cohere.rerank-v4.0");
    await submitGenerativeAi();

    expect(client.getQueryState(["chat", "models"])?.isInvalidated).toBe(false);
  });
});
