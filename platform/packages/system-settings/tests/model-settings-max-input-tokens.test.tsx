// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MODEL_SETTINGS_QUERY_KEY,
  ModelSettingsPage,
  type ModelSettingsApi,
  type ModelSettingsData,
} from "../src";

// #1437: 登録モデルごとの入力の上限（tokens）。値の無いモデルは既定の 128000 を出し、範囲外は保存の前に
// 欄のエラーで止める（保存の操作の後から出す）。

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

function data(modelIds: string[] = ["llm"]): ModelSettingsData {
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
        models: modelIds.map((modelId) => ({
          model_id: modelId,
          display_name: "",
          vision_enabled: true,
          connection_id: "primary",
        })),
        default_text_model_id: modelIds[0] ?? "",
        default_vision_model_id: modelIds[0] ?? "",
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
        rerank_model: "cohere.rerank-v4.0-fast",
      },
    },
    model_settings_file: "model-settings.json",
    source: "runtime",
    secret_source: "environment",
    legacy_secret_detected: false,
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
  return client;
}

function input(id: string) {
  return host.querySelector<HTMLInputElement>(`#${id}`);
}

/** 欄の直下のエラー（TextField の FieldError。aria-describedby で結ばれる）。 */
function fieldError(id: string) {
  const describedBy = input(id)?.getAttribute("aria-describedby") ?? "";
  return describedBy
    .split(" ")
    .map((item) => (item ? document.getElementById(item) : null))
    .find((item) => item?.getAttribute("role") === "alert")?.textContent;
}

function buttonByText(text: string): HTMLButtonElement {
  const found = [...host.querySelectorAll<HTMLButtonElement>("button")].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
  if (!found) throw new Error(`button not found: ${text}`);
  return found;
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

async function submitModels() {
  const form = input("enterprise-model-0-model-id")?.closest("form");
  if (!form) throw new Error("models form not found");
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await flush();
}

describe("ModelSettingsPage の登録モデルの入力の上限（#1437）", () => {
  it("値の無いモデル・追加したモデルは既定の 128000 を出し、そのまま保存で送る", async () => {
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });

    expect(input("enterprise-model-max-input-tokens-0")?.value).toBe("128000");
    expect(input("enterprise-model-max-input-tokens-0")?.getAttribute("aria-describedby")).toContain(
      "enterprise-model-max-input-tokens-help",
    );
    await act(async () => buttonByText("追加").click());
    expect(input("enterprise-model-max-input-tokens-1")?.value).toBe("128000");
    await setInputValue("enterprise-model-1-model-id", "llm-2");
    await setInputValue("enterprise-model-max-input-tokens-1", "200000");

    await submitModels();

    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    expect(
      updateModelSettings.mock.calls[0]?.[0].enterprise_ai.models.map(
        (model: { max_input_tokens?: number }) => model.max_input_tokens,
      ),
    ).toEqual([128000, 200000]);
  });

  it.each([
    ["空欄", ""],
    ["下限より小さい", "4095"],
    ["上限より大きい", "2000001"],
    ["整数でない", "12000.5"],
  ])("%s値は保存を止め、欄の直下にエラーを出してフォーカスする", async (_name, value) => {
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });

    await setInputValue("enterprise-model-max-input-tokens-0", value);
    // 入力中（保存の操作の前）はエラーを出さない。
    expect(fieldError("enterprise-model-max-input-tokens-0")).toBeUndefined();

    await submitModels();

    expect(updateModelSettings).not.toHaveBeenCalled();
    expect(fieldError("enterprise-model-max-input-tokens-0")).toBe(
      "入力の上限は 4096〜2000000 の整数で入力してください。",
    );
    expect(document.activeElement?.id).toBe("enterprise-model-max-input-tokens-0");

    await setInputValue("enterprise-model-max-input-tokens-0", "4096");
    expect(fieldError("enterprise-model-max-input-tokens-0")).toBeUndefined();
    // 出力の上限（#1719）は入力の上限以下にする。
    await setInputValue("enterprise-model-max-output-tokens-0", "4096");
    await submitModels();
    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    expect(updateModelSettings.mock.calls[0]?.[0].enterprise_ai.models[0]?.max_input_tokens).toBe(4096);
  });
});
