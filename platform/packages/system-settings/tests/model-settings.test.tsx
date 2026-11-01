import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { ModelSettingsPage, type ModelSettingsPayload } from "../src";
import { buildSectionSavePayload } from "../src/model/ModelSettingsPage";

const baseline: ModelSettingsPayload = {
  enterprise_ai: {
    connections: [
      {
        connection_id: "primary",
        endpoint: "https://saved.example",
        project_id: "ocid1.generativeaiproject.oc1..saved",
        api_key: "",
        has_api_key: true,
        clear_api_key: false,
      },
    ],
    models: [
      {
        model_id: "saved-llm",
        display_name: "Saved",
        vision_enabled: false,
        kind: "text",
        connection_id: "primary",
        reasoning_effort: "medium",
        max_input_tokens: 128000,
        max_output_tokens: 32768,
      },
    ],
    default_text_model_id: "saved-llm",
    default_vision_model_id: "",
    api_path: "/custom",
    vlm_input_mode: "files_api",
    text_payload_template: "",
    vision_payload_template: "",
    text_response_path: "/text",
    vision_response_path: "",
    timeout_seconds: 120,
    max_retries: 1,
    llm_max_output_tokens: 1600,
    vlm_max_output_tokens: 64000,
  },
};

describe("buildSectionSavePayload", () => {
  const draft: ModelSettingsPayload = {
    enterprise_ai: {
      ...baseline.enterprise_ai,
      connections: [
        {
          ...baseline.enterprise_ai.connections[0]!,
          endpoint: "https://draft.example",
          api_key: "sk-draft",
        },
      ],
      models: [
        {
          model_id: "draft-llm",
          display_name: "Draft",
          vision_enabled: true,
          connection_id: "primary",
        },
      ],
      default_text_model_id: "",
      default_vision_model_id: "draft-llm",
      default_embedding_model_id: "draft-embed",
      default_rerank_model_id: "draft-rerank",
    },
  };

  it("保存する節の入力だけを送り、他の節と画面にない項目は保存済みの値を保つ", () => {
    const connection = buildSectionSavePayload(baseline, draft, "enterprise_connection");
    expect(connection.enterprise_ai.connections[0]?.endpoint).toBe("https://draft.example");
    expect(connection.enterprise_ai.connections[0]?.api_key).toBe("sk-draft");
    expect(connection.enterprise_ai.models).toEqual(baseline.enterprise_ai.models);
    expect(connection.enterprise_ai.default_rerank_model_id).toBe("");
    expect(connection.enterprise_ai.api_path).toBe("/custom");
    expect(connection.enterprise_ai.vlm_input_mode).toBe("files_api");

    const models = buildSectionSavePayload(baseline, draft, "enterprise_models");
    expect(models.enterprise_ai.connections[0]?.endpoint).toBe("https://saved.example");
    expect(models.enterprise_ai.default_text_model_id).toBe("");
    expect(models.enterprise_ai.default_vision_model_id).toBe("draft-llm");
    expect(connection.enterprise_ai.default_vision_model_id).toBe("");
    // 既定の埋め込み / 再ランクのモデル（#45）は登録モデルの節で送る。
    expect(models.enterprise_ai.default_embedding_model_id).toBe("draft-embed");
    expect(models.enterprise_ai.default_rerank_model_id).toBe("draft-rerank");
    expect(models).not.toHaveProperty("generative_ai");
  });
});

describe("buildSectionSavePayload（接続。#533 / #542）", () => {
  const secondary = {
    connection_id: "secondary" as const,
    endpoint: "https://secondary.example",
    project_id: "ocid1.generativeaiproject.oc1..secondary",
    api_key: "",
    has_api_key: true,
    clear_api_key: false,
  };
  const twoConnections: ModelSettingsPayload = {
    ...baseline,
    enterprise_ai: {
      ...baseline.enterprise_ai,
      connections: [...baseline.enterprise_ai.connections, secondary],
      models: [
        { ...baseline.enterprise_ai.models[0]!, connection_id: "secondary" },
      ],
    },
  };

  it("接続の節は接続の一覧を送り、削除した接続を使う保存済みのモデルはプライマリ接続に移す", () => {
    const draft: ModelSettingsPayload = {
      ...twoConnections,
      enterprise_ai: {
        ...twoConnections.enterprise_ai,
        connections: [...baseline.enterprise_ai.connections],
      },
    };
    const payload = buildSectionSavePayload(twoConnections, draft, "enterprise_connection");
    expect(payload.enterprise_ai.connections.map((c) => c.connection_id)).toEqual(["primary"]);
    expect(payload.enterprise_ai.models[0]?.connection_id).toBe("primary");
  });

  it("登録モデルの節はモデルの接続を送り、接続は保存済みのまま", () => {
    const draft: ModelSettingsPayload = {
      ...twoConnections,
      enterprise_ai: {
        ...twoConnections.enterprise_ai,
        connections: [
          ...baseline.enterprise_ai.connections,
          { ...secondary, endpoint: "https://edited.example" },
        ],
        models: [{ ...baseline.enterprise_ai.models[0]!, connection_id: "primary" }],
      },
    };
    const payload = buildSectionSavePayload(twoConnections, draft, "enterprise_models");
    expect(payload.enterprise_ai.models[0]?.connection_id).toBe("primary");
    expect(payload.enterprise_ai.connections[1]?.endpoint).toBe("https://secondary.example");
  });
});

describe("buildSectionSavePayload（ターシャリ接続。#786）", () => {
  const tertiary = {
    connection_id: "tertiary" as const,
    endpoint: "https://api.openai.com/v1",
    project_id: "",
    api_key: "",
    has_api_key: true,
    clear_api_key: false,
  };
  const withTertiary: ModelSettingsPayload = {
    ...baseline,
    enterprise_ai: {
      ...baseline.enterprise_ai,
      connections: [...baseline.enterprise_ai.connections, tertiary],
      models: [{ ...baseline.enterprise_ai.models[0]!, connection_id: "tertiary" }],
    },
  };

  it("ターシャリ接続を使う登録モデルはそのまま送り、Project ID は空のまま送る", () => {
    const payload = buildSectionSavePayload(withTertiary, withTertiary, "enterprise_models");
    expect(payload.enterprise_ai.models[0]?.connection_id).toBe("tertiary");
    expect(payload.enterprise_ai.connections.map((c) => c.connection_id)).toEqual([
      "primary",
      "tertiary",
    ]);
    expect(payload.enterprise_ai.connections[1]?.project_id).toBe("");
  });

  it("ターシャリ接続を削除すると、使っていた保存済みのモデルをプライマリ接続に移す", () => {
    const draft: ModelSettingsPayload = {
      ...withTertiary,
      enterprise_ai: {
        ...withTertiary.enterprise_ai,
        connections: [...baseline.enterprise_ai.connections],
      },
    };
    const payload = buildSectionSavePayload(withTertiary, draft, "enterprise_connection");
    expect(payload.enterprise_ai.connections.map((c) => c.connection_id)).toEqual(["primary"]);
    expect(payload.enterprise_ai.models[0]?.connection_id).toBe("primary");
  });
});

describe("ModelSettingsPage", () => {
  it("読み込み中は状態表示を出す", () => {
    const pending = () => new Promise<never>(() => undefined);
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient()}>
          <ConfirmProvider>
            <ModelSettingsPage
              api={{
                getModelSettings: pending,
                updateModelSettings: pending,
                testModelSettings: pending,
              }}
            />
          </ConfirmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    expect(html).toContain('data-testid="settings-model-loading"');
    expect(html).toContain("モデル設定を読み込んでいます。");
  });
});
