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
  type ModelSettingsUpdatePayload,
} from "../src";

// 埋め込み / 再ランクのモデルは、登録モデルに種類と接続を選んで登録する（#45）。独立したカードは無く、
// 行の「テスト」は種類で呼び分け、既定の埋め込み / 再ランクモデルは種類が合う登録モデルだけから選ぶ。

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
const BAILIAN = "https://llm-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1";

function data(): ModelSettingsData {
  return {
    settings: {
      enterprise_ai: {
        connections: [
          {
            connection_id: "primary",
            endpoint: "https://api.deepseek.com",
            project_id: "",
            api_key: "",
            has_api_key: true,
            clear_api_key: false,
          },
          {
            connection_id: "secondary",
            endpoint: BAILIAN,
            project_id: "",
            api_key: "",
            has_api_key: true,
            clear_api_key: false,
          },
        ],
        models: [
          { model_id: "deepseek-flash", display_name: "", vision_enabled: true, kind: "text" },
          {
            model_id: "text-embedding-v4",
            display_name: "",
            vision_enabled: false,
            kind: "embedding",
            connection_id: "secondary",
          },
          {
            model_id: "qwen3-rerank",
            display_name: "",
            vision_enabled: false,
            kind: "rerank",
            connection_id: "secondary",
          },
        ],
        default_text_model_id: "deepseek-flash",
        default_vision_model_id: "deepseek-flash",
        default_embedding_model_id: "text-embedding-v4",
        default_rerank_model_id: "qwen3-rerank",
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

async function click(element: Element | null | undefined) {
  if (!element) throw new Error("element not found");
  await act(async () => {
    (element as HTMLElement).click();
  });
  await flush();
}

function testButton(model: string) {
  return host.querySelector<HTMLButtonElement>(`button[aria-label="${model} をテスト"]`);
}

async function optionValues(fieldId: string): Promise<string[]> {
  const trigger = host.querySelector(`#${fieldId}`);
  await click(trigger);
  const listboxId = trigger?.getAttribute("aria-controls");
  const scope = (listboxId && document.getElementById(listboxId)) || document;
  const values = [...scope.querySelectorAll<HTMLElement>('[role="option"]')].map(
    (option) => option.dataset.value ?? "",
  );
  // 開いた一覧を閉じる。
  await click(trigger);
  return values;
}

describe("埋め込み / 再ランクの登録モデル（#45）", () => {
  it("独立した埋め込み / 再ランクのカードは無く、行の種類を出す", async () => {
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings: pending,
      testModelSettings: pending,
    });
    expect(host.querySelector("#genai-embedding-model")).toBeNull();
    expect(host.textContent).not.toContain("Base URL");
    expect(host.querySelector("#enterprise-model-kind-1")?.textContent).toContain("埋め込み");
    expect(host.querySelector("#enterprise-model-kind-2")?.textContent).toContain("再ランク");
    // 推論の量・上限・画像入力は LLM の行だけ。埋め込み / 再ランクの行は無効表示。
    expect(host.querySelector<HTMLButtonElement>("#enterprise-model-reasoning-effort-0")?.disabled).toBe(false);
    for (const index of [1, 2]) {
      expect(host.querySelector<HTMLButtonElement>(`#enterprise-model-reasoning-effort-${index}`)?.disabled).toBe(true);
      expect(host.querySelector<HTMLInputElement>(`#enterprise-model-max-input-tokens-${index}`)?.disabled).toBe(true);
      expect(
        host.querySelector<HTMLButtonElement>(`button[role="switch"][aria-label="画像入力に対応 ${index + 1}"]`)?.disabled,
      ).toBe(true);
    }
    // 登録モデルの必須の印は条件の無い「必須」。
    expect(host.querySelector("#enterprise-model-catalog-label")?.textContent).toContain("必須");
    expect(host.querySelector("#enterprise-model-catalog-label")?.textContent).not.toContain("利用時必須");
  });

  it("行のテストは種類で呼び分ける（埋め込みは embedding、再ランクは rerank）", async () => {
    const testModelSettings = vi.fn(async (request: ModelSettingsTestRequest) => ({
      status: "success" as const,
      target_type: request.target_type,
      model_id: request.model_id,
      message: `${request.target_type} の結果`,
      troubleshooting: [],
      raw_error: null,
      error_type: null,
      elapsed_ms: 10,
      checked_at: "2026-10-31T00:00:00Z",
      details: {},
    }));
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings: pending,
      testModelSettings,
    });

    await click(testButton("text-embedding-v4"));
    await click(testButton("qwen3-rerank"));
    await click(testButton("deepseek-flash"));

    expect(testModelSettings.mock.calls.map(([request]) => [request.target_type, request.model_id])).toEqual([
      ["embedding", "text-embedding-v4"],
      ["rerank", "qwen3-rerank"],
      ["enterprise_vision", "deepseek-flash"],
    ]);
    expect(host.textContent).toContain("embedding の結果");
  });

  it("既定の埋め込み / 再ランクはテキスト / 画像対応の次、判定の前に並ぶ", async () => {
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings: pending,
      testModelSettings: pending,
    });
    const ids = [...host.querySelectorAll<HTMLElement>('[id^="enterprise-default-"]')]
      .map((element) => element.id)
      .filter((id) => /^enterprise-default-[a-z]+-model$/.test(id));
    expect(ids).toEqual([
      "enterprise-default-text-model",
      "enterprise-default-vision-model",
      "enterprise-default-embedding-model",
      "enterprise-default-rerank-model",
      "enterprise-default-judge-model",
    ]);
  });

  it("既定の埋め込み / 再ランクモデルの選択肢は種類が合う登録モデルだけ", async () => {
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings: pending,
      testModelSettings: pending,
    });
    expect(await optionValues("enterprise-default-embedding-model")).toEqual(["text-embedding-v4"]);
    expect(await optionValues("enterprise-default-rerank-model")).toEqual(["qwen3-rerank"]);
    expect(await optionValues("enterprise-default-text-model")).toEqual(["deepseek-flash"]);
  });

  it("登録モデルの保存は既定の埋め込み / 再ランクモデルを送り、generative_ai は送らない", async () => {
    const updateModelSettings = vi.fn(async (_payload: ModelSettingsUpdatePayload) => data());
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });
    const form = host.querySelector("#enterprise-model-catalog")?.closest("form");
    if (!form) throw new Error("models form not found");
    await act(async () => {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await flush();

    const payload = updateModelSettings.mock.calls[0]?.[0];
    expect(payload).toBeDefined();
    expect(payload).not.toHaveProperty("generative_ai");
    expect(payload?.enterprise_ai.default_embedding_model_id).toBe("text-embedding-v4");
    expect(payload?.enterprise_ai.default_rerank_model_id).toBe("qwen3-rerank");
    expect(payload?.enterprise_ai.models.map((model) => [model.model_id, model.kind, model.connection_id])).toEqual([
      ["deepseek-flash", "text", "primary"],
      ["text-embedding-v4", "embedding", "secondary"],
      ["qwen3-rerank", "rerank", "secondary"],
    ]);
  });
});
