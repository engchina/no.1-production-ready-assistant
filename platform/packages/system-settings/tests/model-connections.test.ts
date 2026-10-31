import { describe, expect, it } from "vitest";

import {
  addConnection,
  connectionFieldId,
  connectionLabel,
  connectionOptions,
  emptyConnection,
  firstConnectionError,
  modelsUsingConnection,
  normalizeModelSettings,
  removeConnection,
  unsavedConnectionIds,
  validateConnections,
  validateModelConnections,
} from "../src/model/connections";
import {
  ENTERPRISE_AI_CONNECTION_IDS,
  MAX_ENTERPRISE_AI_CONNECTIONS,
  type EnterpriseAiConfiguredModel,
  type EnterpriseAiConnectionSettings,
  type EnterpriseAiModelSettings,
  type ModelSettingsPayload,
} from "../src/model/types";

const primary: EnterpriseAiConnectionSettings = {
  ...emptyConnection("primary"),
  endpoint: "https://primary.example",
  has_api_key: true,
};
const secondary: EnterpriseAiConnectionSettings = {
  ...emptyConnection("secondary"),
  endpoint: "https://secondary.example",
  project_ocid: "ocid1.generativeaiproject.oc1..secondary",
  has_api_key: true,
};
// ターシャリ接続（#786）は OpenAI / OpenAI 互換 API 向けで、Project OCID を入れない。
const tertiary: EnterpriseAiConnectionSettings = {
  ...emptyConnection("tertiary"),
  endpoint: "https://api.openai.com/v1",
  has_api_key: true,
};
const models: EnterpriseAiConfiguredModel[] = [
  { model_id: "llm-a", display_name: "標準", vision_enabled: false },
  { model_id: "vlm-b", display_name: "", vision_enabled: true, connection_id: "secondary" },
  { model_id: " ", display_name: "未入力", vision_enabled: false, connection_id: "secondary" },
];

describe("接続（#533 / #542）", () => {
  it("接続はプライマリ接続・セカンダリ接続・ターシャリ接続の 3 つで、この順に並ぶ", () => {
    expect(MAX_ENTERPRISE_AI_CONNECTIONS).toBe(3);
    expect(ENTERPRISE_AI_CONNECTION_IDS).toEqual(["primary", "secondary", "tertiary"]);
  });

  it("名前はタブと同じ「プライマリ接続」「セカンダリ接続」で、選択肢は設定した接続だけ", () => {
    expect(connectionLabel("primary")).toBe("プライマリ接続");
    expect(connectionLabel("secondary")).toBe("セカンダリ接続");
    expect(connectionLabel("tertiary")).toBe("ターシャリ接続");
    expect(connectionLabel("unknown")).toBe("プライマリ接続");
    expect(connectionOptions([primary])).toEqual([
      { value: "primary", label: "プライマリ接続" },
    ]);
    expect(connectionOptions([primary, secondary])).toEqual([
      { value: "primary", label: "プライマリ接続" },
      { value: "secondary", label: "セカンダリ接続" },
    ]);
    expect(connectionOptions([primary, secondary, tertiary])).toEqual([
      { value: "primary", label: "プライマリ接続" },
      { value: "secondary", label: "セカンダリ接続" },
      { value: "tertiary", label: "ターシャリ接続" },
    ]);
  });

  it("プライマリ接続の入力欄の id は #533 より前のまま", () => {
    expect(connectionFieldId("primary", "endpoint")).toBe("enterprise-endpoint");
    expect(connectionFieldId("primary", "project_ocid")).toBe("enterprise-project-ocid");
    expect(connectionFieldId("primary", "api_key")).toBe("enterprise-api-key");
    expect(connectionFieldId("secondary", "endpoint")).toBe("enterprise-secondary-endpoint");
    expect(connectionFieldId("secondary", "api_key")).toBe("enterprise-secondary-api-key");
    expect(connectionFieldId("tertiary", "endpoint")).toBe("enterprise-tertiary-endpoint");
  });

  it("ターシャリ接続は Endpoint URL と API key が必須で、Project OCID は任意（#786）", () => {
    expect(validateConnections([primary, tertiary])).toEqual({});
    expect(validateConnections([primary, emptyConnection("tertiary")])).toEqual({
      tertiary: {
        endpoint: "Endpoint URL を入力してください。",
        api_key: "API key を入力してください。",
      },
    });
  });

  it("接続を設定すると、設定した順にかかわらずタブの順に並べる（#786）", () => {
    const withTertiary = addConnection([primary], "tertiary");
    expect(withTertiary.map((connection) => connection.connection_id)).toEqual([
      "primary",
      "tertiary",
    ]);
    const all = addConnection(withTertiary, "secondary");
    expect(all.map((connection) => connection.connection_id)).toEqual([
      "primary",
      "secondary",
      "tertiary",
    ]);
    // すでにある接続は入力を消さない。
    expect(addConnection([primary, tertiary], "tertiary")).toEqual([primary, tertiary]);
  });

  it("セカンダリ接続を設定したら Endpoint URL・Project OCID・API key は必須（プライマリは止めない）", () => {
    expect(validateConnections([emptyConnection("primary")])).toEqual({});
    const errors = validateConnections([primary, emptyConnection("secondary")]);
    expect(errors).toEqual({
      secondary: {
        endpoint: "Endpoint URL を入力してください。",
        project_ocid: "Project OCID を入力してください。",
        api_key: "API key を入力してください。",
      },
    });
    expect(firstConnectionError(errors)).toEqual({
      connectionId: "secondary",
      field: "endpoint",
    });
    expect(validateConnections([primary, secondary])).toEqual({});
    expect(firstConnectionError({})).toBeNull();
  });

  it("API key は新しい入力か、削除しない保存済みの key があればよい", () => {
    const noSaved = { ...secondary, has_api_key: false };
    expect(validateConnections([primary, noSaved]).secondary).toEqual({
      api_key: "API key を入力してください。",
    });
    expect(validateConnections([primary, { ...noSaved, api_key: "sk-new" }])).toEqual({});
    expect(
      validateConnections([primary, { ...secondary, clear_api_key: true }]).secondary?.api_key,
    ).toBe("API key を入力してください。");
  });

  it("保存済みから変えた接続（設定・削除・入力）を「未保存」にする", () => {
    expect(unsavedConnectionIds([primary], [primary])).toEqual(new Set());
    expect(unsavedConnectionIds([primary, emptyConnection("secondary")], [primary])).toEqual(
      new Set(["secondary"]),
    );
    expect(unsavedConnectionIds([primary], [primary, secondary])).toEqual(
      new Set(["secondary"]),
    );
    expect(
      unsavedConnectionIds([{ ...primary, api_key: "sk-new" }, secondary], [primary, secondary]),
    ).toEqual(new Set(["primary"]));
    expect(
      unsavedConnectionIds(
        [primary, { ...secondary, endpoint: "https://other.example" }],
        [primary, secondary],
      ),
    ).toEqual(new Set(["secondary"]));
  });

  it("ない接続・保存前の接続を指すモデルを欄のエラーにする（モデル ID の空の行は除く）", () => {
    expect(validateModelConnections(models, [primary, secondary], ["primary", "secondary"])).toEqual(
      {},
    );
    expect(validateModelConnections(models, [primary], ["primary", "secondary"])).toEqual({
      1: "「vlm-b」の接続がありません。登録されている接続から選び直してください。",
    });
    expect(validateModelConnections(models, [primary, secondary], ["primary"])[1]).toContain(
      "セカンダリ接続 はまだ保存されていません",
    );
  });

  it("セカンダリ接続を削除すると、使っていたモデルをプライマリ接続に移す", () => {
    const enterprise = { connections: [primary, secondary], models } as EnterpriseAiModelSettings;
    expect(modelsUsingConnection(models, "secondary").map((model) => model.model_id)).toEqual([
      "vlm-b",
    ]);
    const next = removeConnection(enterprise, "secondary");
    expect(next.connections.map((connection) => connection.connection_id)).toEqual(["primary"]);
    expect(next.models.map((model) => model.connection_id)).toEqual([
      "primary",
      "primary",
      "primary",
    ]);
  });

  it("ターシャリ接続を削除すると、使っていたモデルをプライマリ接続に移す（#786）", () => {
    const withTertiary = [
      ...models,
      { model_id: "gpt-c", display_name: "", vision_enabled: false, connection_id: "tertiary" },
    ];
    const enterprise = {
      connections: [primary, secondary, tertiary],
      models: withTertiary,
    } as EnterpriseAiModelSettings;
    const next = removeConnection(enterprise, "tertiary");
    expect(next.connections.map((connection) => connection.connection_id)).toEqual([
      "primary",
      "secondary",
    ]);
    expect(next.models.map((model) => model.connection_id)).toEqual([
      "primary",
      "secondary",
      "secondary",
      "primary",
    ]);
  });

  it("接続の一覧がない応答（#533 より前の形）はプライマリ接続として読む", () => {
    const legacy = {
      enterprise_ai: {
        endpoint: "https://legacy.example",
        project_ocid: "ocid1.legacy",
        api_key: "",
        has_api_key: true,
        clear_api_key: false,
        models: [{ model_id: "m", display_name: "", vision_enabled: true }],
        default_text_model_id: "",
        default_vision_model_id: "m",
      },
      generative_ai: { embedding_model: "e", embedding_dim: 1536, rerank_model: "r" },
    } as unknown as ModelSettingsPayload;
    const normalized = normalizeModelSettings(legacy);
    expect(normalized.enterprise_ai.connections).toEqual([
      {
        ...emptyConnection("primary"),
        endpoint: "https://legacy.example",
        project_ocid: "ocid1.legacy",
        has_api_key: true,
      },
    ]);
    expect(normalized.enterprise_ai.models[0]?.connection_id).toBe("primary");
    expect("endpoint" in normalized.enterprise_ai).toBe(false);
  });

  it("#533 の表示名（display_name）が残った応答も読み、接続から落とす", () => {
    const withNames = {
      enterprise_ai: {
        connections: [
          { ...primary, display_name: "大阪" },
          { ...secondary, display_name: "シカゴ" },
        ],
        models: [],
        default_text_model_id: "",
        default_vision_model_id: "",
      },
      generative_ai: { embedding_model: "e", embedding_dim: 1536, rerank_model: "r" },
    } as unknown as ModelSettingsPayload;
    const normalized = normalizeModelSettings(withNames);
    expect(normalized.enterprise_ai.connections).toEqual([primary, secondary]);
  });
});
