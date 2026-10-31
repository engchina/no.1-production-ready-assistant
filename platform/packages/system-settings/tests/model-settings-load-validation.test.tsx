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

// #1035: 登録モデルのモデル ID の重複は保存の前に欄のエラーで止める。
// #1036: 裏の再取得の失敗で編集中のフォームを取得失敗の表示に置き換えない。読み込み中は経過時間を出す。

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

describe("ModelSettingsPage の登録モデルのモデル ID の重複（#1035）", () => {
  it("同じモデル ID の行は保存を止め、2 行目の欄の直下にエラーを出してフォーカスする", async () => {
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });

    await act(async () => buttonByText("追加").click());
    await setInputValue("enterprise-model-1-model-id", " llm ");
    // 入力中（保存の操作の前）はエラーを出さない。
    expect(fieldError("enterprise-model-1-model-id")).toBeUndefined();

    await submitModels();

    expect(updateModelSettings).not.toHaveBeenCalled();
    expect(fieldError("enterprise-model-0-model-id")).toBeUndefined();
    expect(fieldError("enterprise-model-1-model-id")).toBe(
      "モデル ID「llm」はすでに登録されています。同じモデルは 1 行にまとめてください。",
    );
    expect(document.activeElement?.id).toBe("enterprise-model-1-model-id");

    // 別の ID に直せば保存できる。
    await setInputValue("enterprise-model-1-model-id", "llm-2");
    expect(fieldError("enterprise-model-1-model-id")).toBeUndefined();
    await submitModels();
    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    expect(
      updateModelSettings.mock.calls[0]?.[0].enterprise_ai.models.map(
        (model: { model_id: string }) => model.model_id,
      ),
    ).toEqual(["llm", "llm-2"]);
  });

  it("保存済みの登録モデルに重複があれば、開いた時点でエラーを出す", async () => {
    await renderPage({
      getModelSettings: async () => data(["llm", "llm"]),
      updateModelSettings: pending,
      testModelSettings: pending,
    });

    expect(fieldError("enterprise-model-1-model-id")).toContain("モデル ID「llm」はすでに登録されています。");
  });
});

describe("ModelSettingsPage の読み込み（#1036）", () => {
  it("読み込み中は経過時間つきの状態表示と Skeleton を出す", async () => {
    await renderPage({
      getModelSettings: pending,
      updateModelSettings: pending,
      testModelSettings: pending,
    });

    const loading = host.querySelector('[data-testid="settings-model-loading"]');
    expect(loading?.getAttribute("aria-busy")).toBe("true");
    expect(loading?.textContent).toContain("モデル設定を読み込んでいます。");
    expect(loading?.querySelectorAll('[data-skeleton="form"]')).toHaveLength(3);
  });

  it("初回の取得の失敗は取得失敗の表示を出す", async () => {
    await renderPage({
      getModelSettings: async () => {
        throw new Error("boom");
      },
      updateModelSettings: pending,
      testModelSettings: pending,
    });

    expect(host.textContent).toContain("モデル設定の取得に失敗しました。");
    expect(input("enterprise-model-0-model-id")).toBeNull();
  });

  it("裏の再取得が失敗しても、編集中のフォームと入力を残す", async () => {
    let fail = false;
    const client = await renderPage({
      getModelSettings: async () => {
        if (fail) throw new Error("boom");
        return data();
      },
      updateModelSettings: pending,
      testModelSettings: pending,
    });
    await setInputValue("enterprise-model-0-model-id", "edited");

    fail = true;
    await act(async () => {
      await client.refetchQueries({ queryKey: MODEL_SETTINGS_QUERY_KEY });
    });
    await flush();

    expect(client.getQueryState(MODEL_SETTINGS_QUERY_KEY)?.status).toBe("error");
    expect(host.textContent).not.toContain("モデル設定の取得に失敗しました。");
    expect(input("enterprise-model-0-model-id")?.value).toBe("edited");
  });
});
