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

// #1719: 登録モデルごとの出力の上限（tokens）。入力の上限の隣の列で、値の無いモデルは既定の 32768 を出す。
// 範囲外（1〜2000000）と入力の上限を超える値は、保存の前に欄のエラーで止める（保存の操作の後から出す）。

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

describe("ModelSettingsPage の登録モデルの出力の上限（#1719）", () => {
  it("値の無いモデル・追加したモデルは既定の 32768 を出し、128k 以上も入力の上限の中なら保存で送る", async () => {
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });

    expect(input("enterprise-model-max-output-tokens-0")?.value).toBe("32768");
    expect(
      input("enterprise-model-max-output-tokens-0")?.getAttribute("aria-describedby"),
    ).toContain("enterprise-model-max-output-tokens-help");
    const help = host.querySelector<HTMLDetailsElement>('[data-testid="model-parameter-help"]');
    expect(help?.open).toBe(false);
    await act(async () => help?.querySelector("summary")?.click());
    expect(help?.open).toBe(true);
    // 入力の上限と取り違えないよう、両方の説明に違いを書く。
    expect(host.querySelector("#enterprise-model-max-output-tokens-help")?.textContent).toContain(
      "推論の token も含まれる場合があります",
    );
    expect(host.querySelector("#enterprise-model-max-input-tokens-help")?.textContent).toContain(
      "出力の上限は別の設定です",
    );
    await act(async () => buttonByText("追加").click());
    expect(input("enterprise-model-max-output-tokens-1")?.value).toBe("32768");
    await setInputValue("enterprise-model-1-model-id", "xai.grok-4.3");
    await setInputValue("enterprise-model-max-input-tokens-1", "1000000");
    await setInputValue("enterprise-model-max-output-tokens-1", "131072");

    await submitModels();

    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    const sent = updateModelSettings.mock.calls[0]?.[0].enterprise_ai;
    expect(
      sent.models.map((model: { max_output_tokens?: number }) => model.max_output_tokens),
    ).toEqual([32768, 131072]);
    // 共通の LLM / VLM の出力の上限は送らない。
    expect(sent).not.toHaveProperty("llm_max_output_tokens");
    expect(sent).not.toHaveProperty("vlm_max_output_tokens");
  });

  it("入力の上限が既定より小さいモデルは、値が無ければ入力の上限を出す", async () => {
    const settings = data();
    settings.settings.enterprise_ai.models[0]!.max_input_tokens = 8192;
    await renderPage({
      getModelSettings: async () => settings,
      updateModelSettings: pending,
      testModelSettings: pending,
    });

    expect(input("enterprise-model-max-output-tokens-0")?.value).toBe("8192");
  });

  it.each([
    ["空欄", "", "出力の上限は 1〜2000000 の整数で入力してください。"],
    ["下限より小さい", "0", "出力の上限は 1〜2000000 の整数で入力してください。"],
    ["整数でない", "1200.5", "出力の上限は 1〜2000000 の整数で入力してください。"],
    ["入力の上限より大きい", "128001", "出力の上限は入力の上限（128000）以下にしてください。"],
  ])("%s値は保存を止め、欄の直下にエラーを出してフォーカスする", async (_name, value, message) => {
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });

    await setInputValue("enterprise-model-max-output-tokens-0", value);
    expect(fieldError("enterprise-model-max-output-tokens-0")).toBeUndefined();

    await submitModels();

    expect(updateModelSettings).not.toHaveBeenCalled();
    expect(fieldError("enterprise-model-max-output-tokens-0")).toBe(message);
    expect(document.activeElement?.id).toBe("enterprise-model-max-output-tokens-0");

    await setInputValue("enterprise-model-max-output-tokens-0", "65536");
    expect(fieldError("enterprise-model-max-output-tokens-0")).toBeUndefined();
    await submitModels();
    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    expect(updateModelSettings.mock.calls[0]?.[0].enterprise_ai.models[0]?.max_output_tokens).toBe(
      65536,
    );
  });
});
