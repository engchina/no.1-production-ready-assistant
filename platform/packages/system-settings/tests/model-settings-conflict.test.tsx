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

// #1037: 保存は読み込んだ時点の版（revision）を base_revision として送る。画面を開いた後にほかの画面
// （別の製品を含む）で保存されていれば API は 409 を返すので、操作の行に案内と「最新の設定を読み込む」を出す。

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

async function submitGenerativeAi() {
  const form = input("genai-rerank-model")?.closest("form");
  if (!form) throw new Error("generative ai form not found");
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await flush();
}

describe("ModelSettingsPage の保存の競合（#1037）", () => {
  it("読み込んだ版を送り、409 なら操作の行に案内を出し、最新の設定を読み直して保存し直せる", async () => {
    let server = data("rev-1");
    const updateModelSettings = vi.fn(async (payload: ModelSettingsUpdatePayload) => {
      if (payload.base_revision !== server.revision) {
        throw Object.assign(new Error("conflict"), { status: 409 });
      }
      server = { ...data("rev-3", payload.generative_ai.rerank_model) };
      return server;
    });
    await renderPage({
      getModelSettings: async () => server,
      updateModelSettings,
      testModelSettings: pending,
    });
    // 画面を開いた後に、ほかの画面（別の製品）が保存した。
    server = data("rev-2", "rerank-from-other-product");

    await setInputValue("genai-rerank-model", "rerank-mine");
    await submitGenerativeAi();

    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    expect(updateModelSettings.mock.calls[0]?.[0].base_revision).toBe("rev-1");
    expect(host.textContent).toContain(
      "モデル設定は、この画面を開いた後にほかの画面（別の製品を含む）で更新されました。",
    );
    // 入力は残す。
    expect(input("genai-rerank-model")?.value).toBe("rerank-mine");
    const reload = buttonByText("最新の設定を読み込む");
    expect(reload).toBeDefined();
    // 競合した節（Generative AI）の操作の行にだけ出す。
    expect(
      [...host.querySelectorAll("button")].filter(
        (button) => button.textContent?.trim() === "最新の設定を読み込む",
      ),
    ).toHaveLength(1);

    // 保存していない入力があるので、破棄を確認してから読み直す。
    await act(async () => reload!.click());
    await flush();
    const dialog = document.querySelector<HTMLElement>('[role="alertdialog"]');
    expect(dialog?.textContent).toContain("最新の設定を読み込みますか？");
    await act(async () => buttonByText("読み込む", dialog!)!.click());
    await flush();

    expect(input("genai-rerank-model")?.value).toBe("rerank-from-other-product");
    expect(buttonByText("最新の設定を読み込む")).toBeUndefined();
    expect(host.textContent).not.toContain("この画面を開いた後にほかの画面");

    // 読み直した版で保存し直せる。
    await setInputValue("genai-rerank-model", "rerank-mine");
    await submitGenerativeAi();
    expect(updateModelSettings).toHaveBeenCalledTimes(2);
    expect(updateModelSettings.mock.calls[1]?.[0].base_revision).toBe("rev-2");
    expect(input("genai-rerank-model")?.value).toBe("rerank-mine");
  });

  it("版を返さない backend には base_revision を送らない", async () => {
    const legacy = data("");
    delete legacy.revision;
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => legacy,
      updateModelSettings,
      testModelSettings: pending,
    });

    await submitGenerativeAi();

    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    expect(updateModelSettings.mock.calls[0]?.[0]).not.toHaveProperty("base_revision");
  });
});
