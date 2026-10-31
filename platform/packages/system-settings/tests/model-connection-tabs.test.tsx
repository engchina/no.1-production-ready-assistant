// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ModelSettingsPage, type ModelSettingsApi, type ModelSettingsData } from "../src";

// #542: OCI Enterprise AI の接続は、カードの中の共有 Tabs（プライマリ接続 / セカンダリ接続 / ターシャリ接続）で
// 切り替える。表示名の欄はなく、セカンダリ接続は「設定」したときだけ入力欄を出し、3 つの欄とも必須にする。
// #786: ターシャリ接続（OpenAI / OpenAI 互換 API 向け）は Endpoint URL と API key が必須で、Project OCID は任意。

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

function data(withSecondary = false): ModelSettingsData {
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
          ...(withSecondary
            ? [
                {
                  connection_id: "secondary" as const,
                  endpoint: "https://secondary.example",
                  project_ocid: "ocid1.generativeaiproject.oc1..secondary",
                  api_key: "",
                  has_api_key: true,
                  clear_api_key: false,
                },
              ]
            : []),
        ],
        models: [
          {
            model_id: "vlm",
            display_name: "Vision",
            vision_enabled: true,
            connection_id: withSecondary ? "secondary" : "primary",
          },
        ],
        default_text_model_id: "",
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
        rerank_model: "cohere.rerank-v4.0-fast",
      },
    },
    model_settings_file: "model-settings.json",
    source: "runtime",
    secret_source: "environment",
    legacy_secret_detected: false,
  };
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
  // 取得（Promise の解決）と、下書きへの反映（effect）を待つ。
  for (let i = 0; i < 20 && !host.querySelector('[role="tablist"]'); i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

/** 欄の直下のエラー（TextField / SecretField の FieldError。aria-describedby で結ばれる）。 */
function fieldError(id: string) {
  const describedBy = host.querySelector(`#${id}`)?.getAttribute("aria-describedby") ?? "";
  return describedBy
    .split(" ")
    .map((item) => (item ? document.getElementById(item) : null))
    .find((item) => item?.getAttribute("role") === "alert")?.textContent;
}

function tab(name: string): HTMLButtonElement {
  const found = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find((item) =>
    item.textContent?.startsWith(name),
  );
  if (!found) throw new Error(`tab not found: ${name}`);
  return found;
}

function buttonByText(text: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll<HTMLButtonElement>("button")].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
  if (!found) throw new Error(`button not found: ${text}`);
  return found;
}

async function click(element: HTMLElement) {
  await act(async () => element.click());
  await act(async () => {
    await Promise.resolve();
  });
}

/** React の制御された input に値を入れる（value の setter を経由して onChange を起こす）。 */
async function setInputValue(id: string, value: string) {
  const input = host.querySelector<HTMLInputElement>(`#${id}`);
  if (!input) throw new Error(`input not found: ${id}`);
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(async () => {
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submitConnections() {
  const form = host
    .querySelector<HTMLFormElement>(
      "#enterprise-endpoint, #enterprise-secondary-endpoint, #enterprise-secondary-add, #enterprise-tertiary-endpoint, #enterprise-tertiary-add",
    )
    ?.closest("form");
  if (!form) throw new Error("connection form not found");
  expect(form.noValidate).toBe(true);
  await act(async () => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await act(async () => {
    await Promise.resolve();
  });
}

function labelText(id: string) {
  return host.querySelector(`label[for="${id}"]`)?.textContent;
}

describe("ModelSettingsPage の接続のタブ（#542）", () => {
  it("プライマリ接続とセカンダリ接続を Tabs で切り替え、表示名の欄を出さない", async () => {
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings: pending,
      testModelSettings: pending,
    });

    const tablist = host.querySelector('[role="tablist"][aria-label="OCI Enterprise AI の接続"]');
    expect(tablist).not.toBeNull();
    // タブの並びはプライマリ接続 → セカンダリ接続 → ターシャリ接続（#786）。
    expect([...tablist!.querySelectorAll('[role="tab"]')].map((item) => item.textContent)).toEqual([
      "プライマリ接続",
      "セカンダリ接続",
      "ターシャリ接続",
    ]);
    expect(tab("プライマリ接続").getAttribute("aria-selected")).toBe("true");
    expect(tab("セカンダリ接続").getAttribute("aria-selected")).toBe("false");
    expect(host.querySelector("#enterprise-connection-name")).toBeNull();
    expect(host.textContent).not.toContain("表示名を");
    // プライマリ接続の 3 つの欄は、同じ条件付きの必須（OCI 運用時必須）。
    expect(labelText("enterprise-endpoint")).toBe("Endpoint URLOCI 運用時必須");
    expect(labelText("enterprise-project-ocid")).toBe("Project OCIDOCI 運用時必須");
    expect(labelText("enterprise-api-key")).toBe("API keyOCI 運用時必須");
    expect(host.textContent).toContain("登録モデルで接続を選ばなければ、プライマリ接続を使います。");

    // ← → / Home / End で移る（WAI-ARIA Tabs）。
    await act(async () => {
      tab("プライマリ接続").dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
      );
    });
    expect(tab("セカンダリ接続").getAttribute("aria-selected")).toBe("true");
    expect(host.querySelector('[data-testid="enterprise-connection-secondary-empty"]')).not.toBeNull();
    expect(host.textContent).toContain("セカンダリ接続は設定されていません。");
    await act(async () => {
      tab("セカンダリ接続").dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
      );
    });
    expect(tab("ターシャリ接続").getAttribute("aria-selected")).toBe("true");
    expect(host.querySelector('[data-testid="enterprise-connection-tertiary-empty"]')).not.toBeNull();
    expect(host.textContent).toContain("ターシャリ接続は設定されていません。");
    await act(async () => {
      tab("ターシャリ接続").dispatchEvent(
        new KeyboardEvent("keydown", { key: "Home", bubbles: true }),
      );
    });
    expect(tab("プライマリ接続").getAttribute("aria-selected")).toBe("true");
  });

  it("セカンダリ接続を設定すると必須の欄を出し、未入力の保存はそのタブの最初の欄へ戻す", async () => {
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });

    await click(tab("セカンダリ接続"));
    await click(buttonByText("セカンダリ接続を設定"));
    expect(document.activeElement?.id).toBe("enterprise-secondary-endpoint");
    expect(labelText("enterprise-secondary-endpoint")).toBe("Endpoint URL必須");
    expect(labelText("enterprise-secondary-project-ocid")).toBe("Project OCID必須");
    expect(labelText("enterprise-secondary-api-key")).toBe("API key必須");
    // API key だけを消す指定は出さない（消すときはセカンダリ接続ごと削除する）。
    expect(host.querySelector("#enterprise-secondary-api-key-clear")).toBeNull();
    expect(host.querySelector('[data-testid="enterprise-connection-tab-secondary-unsaved"]')?.textContent).toBe(
      "未保存",
    );

    // プライマリ接続のタブから保存しても、エラーのあるセカンダリ接続のタブへ切り替えてフォーカスする。
    await click(tab("プライマリ接続"));
    await submitConnections();
    expect(tab("セカンダリ接続").getAttribute("aria-selected")).toBe("true");
    expect(tab("セカンダリ接続").getAttribute("data-invalid")).toBe("true");
    expect(document.activeElement?.id).toBe("enterprise-secondary-endpoint");
    expect(fieldError("enterprise-secondary-endpoint")).toBe(
      "Endpoint URL を入力してください。",
    );
    expect(fieldError("enterprise-secondary-project-ocid")).toBe(
      "Project OCID を入力してください。",
    );
    expect(fieldError("enterprise-secondary-api-key")).toBe(
      "API key を入力してください。",
    );
    expect(updateModelSettings).not.toHaveBeenCalled();

    // 同じタブのまま保存し直しても、最初のエラーの欄へフォーカスする。
    (document.activeElement as HTMLElement | null)?.blur();
    await submitConnections();
    expect(document.activeElement?.id).toBe("enterprise-secondary-endpoint");
  });

  it("使っているモデルがあるセカンダリ接続の削除は確認し、空の状態の「設定」へフォーカスを移す", async () => {
    await renderPage({
      getModelSettings: async () => data(true),
      updateModelSettings: pending,
      testModelSettings: pending,
    });

    await click(tab("セカンダリ接続"));
    expect(host.querySelector('[data-testid="enterprise-connection-secondary"]')).not.toBeNull();
    await click(buttonByText("セカンダリ接続を削除"));
    const dialog = document.querySelector<HTMLElement>('[role="alertdialog"]');
    expect(dialog?.textContent).toContain("セカンダリ接続を削除しますか？");
    expect(dialog?.textContent).toContain("これらのモデルはプライマリ接続を使います");
    await click(buttonByText("プライマリ接続に移して削除", dialog!));

    expect(host.querySelector('[data-testid="enterprise-connection-secondary-empty"]')).not.toBeNull();
    expect(document.activeElement?.id).toBe("enterprise-secondary-add");
    expect(host.querySelector('[data-testid="enterprise-connection-tab-secondary-unsaved"]')).not.toBeNull();
  });

  it("ターシャリ接続は Endpoint URL と API key だけで保存でき、Project OCID は任意（#786）", async () => {
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });

    await click(tab("ターシャリ接続"));
    expect(host.textContent).toContain("OpenAI や OpenAI 互換 API");
    await click(buttonByText("ターシャリ接続を設定"));
    expect(document.activeElement?.id).toBe("enterprise-tertiary-endpoint");
    expect(labelText("enterprise-tertiary-endpoint")).toBe("Endpoint URL必須");
    expect(labelText("enterprise-tertiary-project-ocid")).toBe("Project OCID");
    expect(labelText("enterprise-tertiary-api-key")).toBe("API key必須");
    // OpenAI の例を出す。
    expect(
      host.querySelector<HTMLInputElement>("#enterprise-tertiary-endpoint")?.placeholder,
    ).toBe("https://api.openai.com/v1");
    expect(
      host.querySelector<HTMLInputElement>("#enterprise-tertiary-api-key")?.placeholder,
    ).toMatch(/^sk-/);
    expect(host.textContent).toContain("OCI Enterprise AI を使うときだけ");
    expect(host.textContent).not.toContain("任意");

    // 未入力の保存は Endpoint URL と API key だけをエラーにする。
    await submitConnections();
    expect(fieldError("enterprise-tertiary-endpoint")).toBe("Endpoint URL を入力してください。");
    expect(fieldError("enterprise-tertiary-project-ocid")).toBeUndefined();
    expect(fieldError("enterprise-tertiary-api-key")).toBe("API key を入力してください。");
    expect(updateModelSettings).not.toHaveBeenCalled();

    await setInputValue("enterprise-tertiary-endpoint", "https://api.openai.com/v1");
    await setInputValue("enterprise-tertiary-api-key", "sk-openai");
    await submitConnections();
    expect(updateModelSettings).toHaveBeenCalledTimes(1);
    const payload = updateModelSettings.mock.calls[0]![0];
    expect(payload.enterprise_ai.connections.map((c) => c.connection_id)).toEqual([
      "primary",
      "tertiary",
    ]);
    expect(payload.enterprise_ai.connections[1]).toMatchObject({
      endpoint: "https://api.openai.com/v1",
      project_ocid: "",
      api_key: "sk-openai",
    });
  });

  it("ターシャリ接続を先に設定してからセカンダリ接続を設定しても、タブの順に送る（#786）", async () => {
    const updateModelSettings = vi.fn(pending);
    await renderPage({
      getModelSettings: async () => data(),
      updateModelSettings,
      testModelSettings: pending,
    });

    await click(tab("ターシャリ接続"));
    await click(buttonByText("ターシャリ接続を設定"));
    await setInputValue("enterprise-tertiary-endpoint", "https://api.openai.com/v1");
    await setInputValue("enterprise-tertiary-api-key", "sk-openai");
    await click(tab("セカンダリ接続"));
    await click(buttonByText("セカンダリ接続を設定"));
    await setInputValue("enterprise-secondary-endpoint", "https://secondary.example");
    await setInputValue("enterprise-secondary-project-ocid", "ocid1.generativeaiproject.oc1..s");
    await setInputValue("enterprise-secondary-api-key", "sk-secondary");
    await submitConnections();

    const payload = updateModelSettings.mock.calls[0]![0];
    expect(payload.enterprise_ai.connections.map((c) => c.connection_id)).toEqual([
      "primary",
      "secondary",
      "tertiary",
    ]);
  });
});
