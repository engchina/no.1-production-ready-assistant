import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ANSWER_GENERATION_TIMEOUT_MS,
  API_REQUEST_TIMEOUT_MS,
  ApiError,
  api,
} from "./api";
import {
  ApiTransportError,
  DEFAULT_API_TRANSPORT_MESSAGES,
  presentApiError,
} from "@production-ready/ui";

function timeoutMessage(timeoutMs: number): string {
  return (
    DEFAULT_API_TRANSPORT_MESSAGES.timeout(Math.ceil(timeoutMs / 1000)) +
    DEFAULT_API_TRANSPORT_MESSAGES.timeoutAction
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("api.request envelope", () => {
  it("成功時は data を取り出す", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: { status: "ok", check: "ok", detail: null },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.getDatabaseStatus();

    expect(result.status).toBe("ok");
    expect(fetchMock).toHaveBeenCalledWith("/api/ready/database", expect.anything());
  });

  it("ファイル準備の保存は JSON の Content-Type と変換方式を送る", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ data: { profile: "office_to_pdf" }, error_messages: [], warning_messages: [] })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.updatePreprocessSettings({ profile: "office_to_pdf" });

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/settings/preprocess");
    expect(init.method).toBe("PATCH");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ profile: "office_to_pdf" });
    expect(result.profile).toBe("office_to_pdf");
  });

  it.each([true, false])("実体・別名索引の保存は JSON の Content-Type と構築の選択 %s を送る", async (enabled) => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ data: { enabled }, error_messages: [], warning_messages: [] })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.updateEntityIndexSettings({ enabled });

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/settings/entity-index");
    expect(init.method).toBe("PATCH");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ enabled });
    expect(result.enabled).toBe(enabled);
  });

  it("エラー時は error_messages を持つ ApiError を投げる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>
        jsonResponse({ data: null, error_messages: ["権限がありません。"], warning_messages: [] }, 403)
      )
    );

    await expect(api.getDatabaseStatus()).rejects.toMatchObject({
      status: 403,
      messages: ["権限がありません。"],
    });
    await expect(api.getDatabaseStatus()).rejects.toBeInstanceOf(ApiError);
  });

  it("応答が返らない場合はタイムアウトを ApiError として返す（getDatabaseStatus は通常 API timeout）", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_path: string, init?: RequestInit) => {
      const signal = init?.signal as AbortSignal | undefined;
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const requestPromise = expect(api.getDatabaseStatus()).rejects.toMatchObject({
      status: 408,
      messages: [timeoutMessage(API_REQUEST_TIMEOUT_MS)],
    });
    await vi.advanceTimersByTimeAsync(API_REQUEST_TIMEOUT_MS);

    await requestPromise;
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/ready/database",
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
  });

  it("通信断（TypeError: Failed to fetch）は日本語の文の ApiError にし、英語の文は「詳細」にだけ残す（Issue 906）", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = await api.getDatabaseStatus().catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0 });
    expect((error as ApiError).message).toBe(
      DEFAULT_API_TRANSPORT_MESSAGES.network + DEFAULT_API_TRANSPORT_MESSAGES.networkAction
    );
    expect((error as ApiError).cause).toBeInstanceOf(ApiTransportError);
    const presented = presentApiError(error, "既定");
    expect(presented.summary).toBe(DEFAULT_API_TRANSPORT_MESSAGES.network);
    expect(presented.details).toContainEqual({ label: "元のメッセージ", value: "Failed to fetch" });
    expect(presented.details).toContainEqual({ label: "要求", value: "GET /api/ready/database" });
  });

  it("本文の読み取り中に接続が切れたら、空の成功にせず通信断の ApiError にする（Issue 906）", async () => {
    const response = jsonResponse({ data: { status: "ok" } });
    vi.spyOn(response, "json").mockRejectedValue(new TypeError("network error"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));

    await expect(api.getDatabaseStatus()).rejects.toMatchObject({
      status: 0,
      messages: [DEFAULT_API_TRANSPORT_MESSAGES.network + DEFAULT_API_TRANSPORT_MESSAGES.networkAction],
    });
  });

  it("利用者の中止（AbortError）は変換せずにそのまま投げる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new DOMException("The operation was aborted.", "AbortError"))
    );

    await expect(api.getDatabaseStatus()).rejects.toMatchObject({ name: "AbortError" });
  });

  it("回答を生成する検索は通常の timeout で打ち切らず、backend の回答生成の上限より長く待つ", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_path: string, init?: RequestInit) => {
      const signal = init?.signal as AbortSignal | undefined;
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    let settled = false;
    const requestPromise = api.search({ query: "得点" }).catch((error: unknown) => {
      settled = true;
      return error;
    });

    // backend の回答生成の上限（RAG_ANSWER_TIMEOUT_SECONDS の上限 600 秒）より長く待つ（#375）。
    expect(ANSWER_GENERATION_TIMEOUT_MS).toBeGreaterThan(600_000);
    await vi.advanceTimersByTimeAsync(API_REQUEST_TIMEOUT_MS + 1_000);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(ANSWER_GENERATION_TIMEOUT_MS);

    await expect(requestPromise).resolves.toMatchObject({ status: 408 });
    expect(fetchMock).toHaveBeenCalledWith("/api/search", expect.objectContaining({ method: "POST" }));
  });

  // Issue 390: 品質評価は job で動く。投入・状態の取得・取り消しは通常の API の timeout に収まる。
  it.each([["submitRunEvaluationJob", "/api/evaluation/jobs/run"]] as const)(
    "品質評価の job の投入（%s）は job の API に POST する",
    async (method, path) => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse(
        {
          data: { job_id: "job-1", status: "RUNNING", total_cases: 1, completed_cases: 0 },
          error_messages: [],
          warning_messages: [],
        },
        202
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const job = await api[method]({ cases: [] } as never);

    expect(job.job_id).toBe("job-1");
    expect(fetchMock).toHaveBeenCalledWith(
      path,
      expect.objectContaining({ method: "POST", body: JSON.stringify({ cases: [] }) })
    );
    }
  );

  // Issue 2065: 評価リクエストの Excel の検証は multipart（ファイルと KB の ID）で送る。
  it("importEvaluationCases はファイルと検索・回答プロファイルを multipart で送る", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({ data: { file_name: "cases.xlsx", cases: [] }, error_messages: [], warning_messages: [] })
    );
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["x"], "cases.xlsx");

    await api.importEvaluationCases(file, "profile-1");

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/evaluation/cases/import");
    expect(init.method).toBe("POST");
    const body = init.body as FormData;
    expect(body.get("file")).toBeInstanceOf(File);
    expect(body.getAll("search_answer_profile_id")).toEqual(["profile-1"]);
  });

  it("downloadEvaluationTemplate はファイルとファイル名を返し、失敗は ApiError にする", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response("xlsx", {
            status: 200,
            headers: { "Content-Disposition": 'attachment; filename="evaluation_request_template.xlsx"' },
          })
      )
      .mockImplementationOnce(async () =>
        jsonResponse({ data: null, error_messages: ["権限がありません。"], warning_messages: [] }, 403)
      );
    vi.stubGlobal("fetch", fetchMock);

    const file = await api.downloadEvaluationTemplate();
    expect(file.fileName).toBe("evaluation_request_template.xlsx");
    expect(await file.blob.text()).toBe("xlsx");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/evaluation/cases/template");

    await expect(api.downloadEvaluationTemplate()).rejects.toMatchObject({ status: 403 });
  });

  it("downloadEvaluationTemplate は選んだ検索・回答プロファイルを query に入れる（Issue 2086）", async () => {
    const fetchMock = vi.fn().mockImplementation(
      async () =>
        new Response("xlsx", {
          status: 200,
          headers: { "Content-Disposition": 'attachment; filename="evaluation_request_template.xlsx"' },
        })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.downloadEvaluationTemplate("profile 1");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/evaluation/cases/template?search_answer_profile_id=profile+1"
    );
  });

  it("getEvaluationJob / cancelEvaluationJob は job id を path に入れる", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: { job_id: "job/1", status: "CANCELLED" },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.getEvaluationJob("job/1");
    await api.cancelEvaluationJob("job/1");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/evaluation/jobs/job%2F1");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/evaluation/jobs/job%2F1/cancel");
    expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({ method: "POST" }));
  });

  it("listAnswerRecords はページングと trace_id の絞り込みを query string にする", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: { items: [], total: 0, limit: 10, offset: 20, has_next: false },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const page = await api.listAnswerRecords({ searchAnswerProfileId: "bv-1", limit: 10, offset: 20 });
    await api.listAnswerRecords({ searchAnswerProfileId: "bv-1", limit: 2, traceIds: ["t-1", "t-2"] });

    expect(page.total).toBe(0);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/search/answers?search_answer_profile_id=bv-1&limit=10&offset=20"
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/search/answers?search_answer_profile_id=bv-1&limit=2&offset=0&trace_id=t-1&trace_id=t-2"
    );
  });

  it("listDocuments は query string を組み立てる", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({ data: { items: [], total: 0, limit: 50, offset: 0, has_next: false }, error_messages: [], warning_messages: [] })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.listDocuments({
      status: "UPLOADED",
      q: "invoice",
      knowledge_base_id: "kb-1",
      limit: 20,
      offset: 40,
    });

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("status=UPLOADED");
    expect(url).toContain("q=invoice");
    expect(url).toContain("knowledge_base_id=kb-1");
    expect(url).toContain("limit=20");
    expect(url).toContain("offset=40");
  });

  it("listDocuments は縮退応答の warning_messages を data へ併設する", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>
        jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_next: false },
          error_messages: [],
          warning_messages: ["データベースに接続できませんでした。"],
        })
      )
    );

    const page = await api.listDocuments();

    expect(page.items).toEqual([]);
    expect(page.warning_messages).toEqual(["データベースに接続できませんでした。"]);
  });

  it("正常応答では warning_messages が空配列になる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>
        jsonResponse({
          data: { items: [], total: 0, limit: 50, offset: 0, has_next: false },
          error_messages: [],
          warning_messages: [],
        })
      )
    );

    const page = await api.listKnowledgeBases({ status: "ACTIVE" });

    expect(page.warning_messages).toEqual([]);
  });

  it("knowledge base API は CRUD endpoint を呼び分ける", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          id: "kb-1",
          name: "社内規程",
          description: null,
          status: "ACTIVE",
          default_search_mode: "hybrid",
          document_count: 0,
          indexed_document_count: 0,
          error_document_count: 0,
          searchable_chunk_count: 0,
          retrieval_config: {},
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          archived_at: null,
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.listKnowledgeBases({ status: "ACTIVE", q: "規程", limit: 20, offset: 0 });
    await api.createKnowledgeBase({ name: "社内規程", description: null });
    await api.archiveKnowledgeBase("kb-1");
    await api.assignDocumentsToKnowledgeBase("kb-1", { document_ids: ["doc-1"] });
    await api.removeDocumentFromKnowledgeBase("kb-1", "doc-1");

    expect(fetchMock.mock.calls[0][0]).toContain("/api/knowledge-bases?");
    expect(fetchMock.mock.calls[0][0]).toContain("status=ACTIVE");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/knowledge-bases");
    expect(fetchMock.mock.calls[2][0]).toBe("/api/knowledge-bases/kb-1/archive");
    expect(fetchMock.mock.calls[3][0]).toBe("/api/knowledge-bases/kb-1/documents");
    expect(fetchMock.mock.calls[4][0]).toBe("/api/knowledge-bases/kb-1/documents/doc-1");
  });

  it("document knowledge base API は所属取得と置換 endpoint を呼ぶ", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: [{ id: "kb-1", name: "社内規程" }],
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.listDocumentKnowledgeBases("doc-1");
    await api.replaceDocumentKnowledgeBases("doc-1", {
      knowledge_base_ids: ["kb-1", "kb-2"],
    });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/documents/doc-1/knowledge-bases");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/documents/doc-1/knowledge-bases");
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "PUT",
      body: JSON.stringify({ knowledge_base_ids: ["kb-1", "kb-2"] }),
    });
  });

  it("ingestion job API は status filter と queue 操作 endpoint を呼ぶ", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          id: "job-1",
          document_id: "doc-1",
          status: "QUEUED",
          parser_profile: "local_text_structure",
          quality_warnings: [],
          skip_reason: null,
          error_message: null,
          attempt_count: 0,
          max_attempts: 3,
          queued_at: "2026-06-15T00:00:00Z",
          started_at: null,
          finished_at: null,
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.enqueueDocumentIngestionJob("doc-1", true);
    await api.enqueueDocumentIngestionJob("doc-2");
    await api.getIngestionJob("job-1");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/documents/doc-1/ingestion-jobs?force=true&phase=PREPROCESS"
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/documents/doc-2/ingestion-jobs?phase=PREPROCESS"
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: "POST" });
    expect(fetchMock.mock.calls[2][0]).toBe("/api/documents/ingestion-jobs/job-1");
  });

  it("document workspace API はレシピの chunk / export と segment endpoint を呼ぶ", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: [],
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.listDocumentRecipeChunks("doc-1", "recipe-1");
    await api.exportDocumentRecipeExtraction("doc-1", "recipe-1", "chunks");
    await api.exportDocumentRecipeExtraction("doc-1", "recipe-1", "html");
    await api.listDocumentIngestionJobs("doc-1");
    await api.listDocumentIngestionSegments("doc-1");
    await api.retryFailedDocumentIngestionSegments("doc-1", "recipe-2");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/documents/doc-1/recipes/recipe-1/chunks");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/documents/doc-1/recipes/recipe-1/extraction-export?format=chunks"
    );
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/api/documents/doc-1/recipes/recipe-1/extraction-export?format=html"
    );
    expect(api.documentRecipeExtractionExportUrl("doc 1", "recipe/1", "markdown")).toBe(
      "/api/documents/doc%201/recipes/recipe%2F1/extraction-export?format=markdown&download=true"
    );
    expect(api.documentRecipeExtractionExportUrl("doc-1", "recipe-1", "chunks")).toBe(
      "/api/documents/doc-1/recipes/recipe-1/extraction-export?format=chunks&download=true"
    );
    expect(fetchMock.mock.calls[3][0]).toBe("/api/documents/doc-1/ingestion-jobs");
    expect(fetchMock.mock.calls[4][0]).toBe("/api/documents/doc-1/ingestion-segments");
    expect(fetchMock.mock.calls[5][0]).toBe(
      "/api/documents/doc-1/ingestion-segments/retry?recipe_id=recipe-2"
    );
    expect(fetchMock.mock.calls[5][1]).toMatchObject({ method: "POST" });
  });

  it("segment retry は recipe 未指定の legacy URL も維持する", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({ data: {}, error_messages: [], warning_messages: [] })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.retryFailedDocumentIngestionSegments("legacy-doc");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/documents/legacy-doc/ingestion-segments/retry"
    );
  });

  it("レシピ分割プレビューは一時設定を POST する", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          chunks: [],
          stats: {
            chunk_count: 0,
            min_chars: 0,
            average_chars: 0,
            max_chars: 0,
            overflow_count: 0,
            embedding_overflow_count: 0,
          },
          warnings: [],
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.previewDocumentRecipeChunks("doc-1", "recipe-1", {
      chunking_strategy: "recursive_character",
      chunk_size: 640,
    });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/documents/doc-1/recipes/recipe-1/chunk-preview"
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      chunking_strategy: "recursive_character",
      chunk_size: 640,
    });
  });

  it("updateModelSettings は Enterprise AI payload template を保持して送る", async () => {
    const payload = {
      enterprise_ai: {
        connections: [
          {
            connection_id: "primary" as const,
            endpoint: "https://enterprise-ai.example",
            project_ocid: "ocid1.generativeaiproject.oc1..example",
            api_key: "",
            has_api_key: false,
            clear_api_key: false,
          },
        ],
        models: [
          {
            model_id: "enterprise-llm",
            display_name: "標準 LLM",
            vision_enabled: true,
            max_output_tokens: 32768,
          },
        ],
        default_text_model_id: "",
        default_vision_model_id: "enterprise-llm",
        api_path: "/responses",
        vlm_input_mode: "files_api" as const,
        text_payload_template: '{"input":"${user_message}"}',
        vision_payload_template: '{"input":"${data_base64}"}',
        text_response_path: "/data/text",
        vision_response_path: "/data/document",
        timeout_seconds: 60,
        max_retries: 2,
      },
      generative_ai: {
        embedding_model: "text-embedding-v4",
        embedding_dim: 1536,
        embedding_base_url: "",
        rerank_model: "qwen3-rerank",
        rerank_base_url: "",
      },
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: { settings: payload, checks: {}, source: "runtime" },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.updateModelSettings(payload);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/model",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify(payload),
      })
    );
  });

  it("testModelSettings は対象モデルをテスト API へ送る", async () => {
    const payload = {
      settings: {
        enterprise_ai: {
          connections: [
            {
              connection_id: "primary" as const,
              endpoint: "https://enterprise-ai.example",
              project_ocid: "ocid1.generativeaiproject.oc1..example",
              api_key: "",
              has_api_key: true,
              clear_api_key: false,
            },
          ],
          models: [
            {
              model_id: "enterprise-llm",
              display_name: "標準 LLM",
              vision_enabled: false,
              max_output_tokens: 32768,
            },
          ],
          default_text_model_id: "enterprise-llm",
          default_vision_model_id: "",
          api_path: "/responses",
          vlm_input_mode: "files_api" as const,
          text_payload_template: "",
          vision_payload_template: "",
          text_response_path: "",
          vision_response_path: "",
          timeout_seconds: 60,
          max_retries: 2,
        },
        generative_ai: {
          embedding_model: "text-embedding-v4",
          embedding_dim: 1536,
          embedding_base_url: "",
          rerank_model: "qwen3-rerank",
          rerank_base_url: "",
        },
      },
      target_type: "enterprise_text" as const,
      model_id: "enterprise-llm",
      vision_enabled: false,
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          status: "success",
          target_type: "enterprise_text",
          model_id: "enterprise-llm",
          message: "ok",
          troubleshooting: [],
          raw_error: null,
          error_type: null,
          elapsed_ms: 12,
          checked_at: "2026-06-14T00:00:00Z",
          details: { surface: "llm" },
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.testModelSettings(payload);

    expect(result.status).toBe("success");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/model/test",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
      })
    );
  });

  it("testDatabaseSettings は timeout 診断付きの結果を返す", async () => {
    const payload = {
      user: "rag_app",
      dsn: "ragdb_high",
      wallet_dir: "/u01/aipoc/instantclient_23_26/network/admin",
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          status: "failed",
          readiness: "ok",
          message: "Oracle AI Database の接続テストが 15 秒でタイムアウトしました。",
          elapsed_ms: 15001,
          troubleshooting: ["ADB が起動中か確認してください。"],
          details: { timeout_seconds: 15, tcp_connect_timeout_seconds: 10 },
          checked_at: "2026-06-14T00:00:00Z",
          error_type: "OracleConnectionTimeoutError",
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.testDatabaseSettings(payload);

    expect(result.elapsed_ms).toBe(15001);
    expect(result.troubleshooting).toContain("ADB が起動中か確認してください。");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/database/test",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
      })
    );
  });

  it("updateUploadStorageSettings は保存先 payload を設定 API へ送る", async () => {
    const payload = {
      backend: "oci" as const,
      local_storage_dir: "/u01/data/production-ready-rag",
      object_storage_namespace: "example-namespace",
      object_storage_bucket: "rag-originals",
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          ...payload,
          readiness: "ok",
          max_upload_bytes: 209715200,
          config_source: "runtime",
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.updateUploadStorageSettings(payload);

    expect(result.backend).toBe("oci");
    expect(result.max_upload_bytes).toBe(209715200);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/upload-storage",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify(payload),
      })
    );
  });

  it("getParserAdapterSettings は adapter readiness API を読む", async () => {
    const payload = {
      adapter_backend: "docling",
      effective_order: ["docling"],
      adapters: [
        {
          backend: "docling",
          package_name: "docling",
          import_name: "docling",
          distribution_name: "docling",
          install_package: "docling==2.103.0",
          enabled: true,
          selected: true,
          installed: true,
          status: "active",
          version: "1.2.3",
          warning_code: null,
        },
        {
          backend: "unstructured",
          package_name: "unstructured",
          import_name: "unstructured",
          distribution_name: null,
          install_package: "unstructured[all-docs]==0.23.1",
          enabled: true,
          selected: false,
          installed: false,
          status: "ignored",
          version: null,
          warning_code: "adapter_flag_ignored_by_backend",
        },
      ],
      scorecard: {
        selected_backend: "docling",
        recommended_backend: "local",
        metrics_source: "runtime",
        metrics_applied_to: null,
        entries: [
          {
            backend: "local",
            rank: 1,
            score: 62,
            status: "recommended",
            recommended: true,
            executable: true,
            selected: false,
            enabled: true,
            installed: true,
            metric_source: "none",
            metric_count: 0,
            signals: {},
            reason_codes: ["local_parser_available"],
            warning_codes: [],
          },
          {
            backend: "unstructured",
            rank: 2,
            score: 24,
            status: "disabled",
            recommended: false,
            executable: false,
            selected: false,
            enabled: false,
            installed: false,
            metric_source: "none",
            metric_count: 0,
            signals: {},
            reason_codes: ["adapter_disabled"],
            warning_codes: [],
          },
        ],
      },
      source_routes: [
        {
          source_kind: "pdf",
          candidate_order: ["docling", "unstructured"],
          attempted_order: ["docling"],
          active_order: ["docling"],
          selected_backend: "docling",
          reason_codes: ["selected_adapter_supported_for_source"],
          warning_codes: [],
        },
      ],
      backend_source_kind_matrix: {
        evidence_source: "runtime_routes",
        required_source_kinds: ["pdf"],
        covered_source_kinds: ["pdf"],
        missing_source_kinds: [],
        backend_source_kinds: { docling: ["pdf"] },
        route_evidence: [],
      },
      capabilities: [
        { backend: "docling", modalities: ["pdf", "image"], extensions: [".pdf", ".png"] },
      ],
      config_source: "runtime",
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: payload,
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.getParserAdapterSettings();

    expect(result.adapter_backend).toBe("docling");
    expect(result.effective_order).toEqual(["docling"]);
    expect(result.adapters[1].warning_code).toBe("adapter_flag_ignored_by_backend");
    expect(result.adapters.map((adapter) => adapter.backend)).toEqual(["docling", "unstructured"]);
    expect(result.source_routes[0].candidate_order).toEqual(["docling", "unstructured"]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/parser-adapters",
      expect.objectContaining({
        credentials: "same-origin",
      })
    );
  });

  it("updateParserAdapterSettings は adapter backend と feature flags を保存する", async () => {
    const requestPayload = {
      adapter_backend: "docling" as const,
      docling_enabled: true,
      unstructured_enabled: true,
    };
    const responsePayload = {
      adapter_backend: "docling",
      effective_order: ["docling"],
      adapters: [
        {
          backend: "docling",
          package_name: "docling",
          import_name: "docling",
          distribution_name: null,
          install_package: "docling==2.103.0",
          enabled: true,
          selected: true,
          installed: false,
          status: "missing",
          version: null,
          warning_code: "adapter_package_missing",
        },
        {
          backend: "unstructured",
          package_name: "unstructured",
          import_name: "unstructured",
          distribution_name: null,
          install_package: "unstructured[all-docs]==0.23.1",
          enabled: true,
          selected: false,
          installed: false,
          status: "ignored",
          version: null,
          warning_code: "adapter_flag_ignored_by_backend",
        },
      ],
      scorecard: {
        selected_backend: "docling",
        recommended_backend: "local",
        metrics_source: "runtime",
        metrics_applied_to: null,
        entries: [],
      },
      source_routes: [],
      backend_source_kind_matrix: {
        evidence_source: "runtime_routes",
        required_source_kinds: [],
        covered_source_kinds: [],
        missing_source_kinds: [],
        backend_source_kinds: {},
        route_evidence: [],
      },
      capabilities: [],
      config_source: "runtime",
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: responsePayload,
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.updateParserAdapterSettings(requestPayload);

    expect(result.effective_order).toEqual(["docling"]);
    expect(result.adapters.map((adapter) => adapter.backend)).toEqual(["docling", "unstructured"]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/parser-adapters",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify(requestPayload),
      })
    );
  });

  it("readOciObjectStorageNamespace は OCI 設定 payload を送る", async () => {
    const payload = {
      config_file: "~/.oci/config",
      profile: "DEFAULT",
      region: "ap-osaka-1",
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: { namespace: "mytenancynamespace" },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.readOciObjectStorageNamespace(payload);

    expect(result.namespace).toBe("mytenancynamespace");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/oci/object-storage/namespace",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(payload),
      })
    );
  });

  it("updateOciSettings は OCI config 保存 payload を設定 API へ送る", async () => {
    const payload = {
      user: "ocid1.user.oc1..example",
      fingerprint: "12:34:56:78:90:ab:cd:ef",
      tenancy: "ocid1.tenancy.oc1..example",
      region: "ap-osaka-1",
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          config_file: "~/.oci/config",
          profile: "DEFAULT",
          user: payload.user,
          fingerprint: payload.fingerprint,
          tenancy: payload.tenancy,
          region: payload.region,
          key_file: "~/.oci/oci_api_key.pem",
          key_file_exists: false,
          config_file_exists: true,
          config_source: "runtime",
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.updateOciSettings(payload);

    expect(result.config_file_exists).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/oci",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify(payload),
      })
    );
  });

  it("updateOciObjectStorageSettings は Object Storage payload を設定 API へ送る", async () => {
    const payload = {
      object_storage_region: "us-chicago-1",
      object_storage_namespace: "mytenancynamespace",
    };
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          backend: "local",
          local_storage_dir: "/u01/data/production-ready-rag",
          object_storage_region: payload.object_storage_region,
          object_storage_namespace: payload.object_storage_namespace,
          object_storage_bucket: "",
          readiness: "ok",
          max_upload_bytes: 209715200,
          config_source: "runtime",
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.updateOciObjectStorageSettings(payload);

    expect(result.object_storage_region).toBe("us-chicago-1");
    expect(result.object_storage_namespace).toBe("mytenancynamespace");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/oci/object-storage",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify(payload),
      })
    );
  });

  it("testOciConfig は保存済み OCI config のテスト API を呼ぶ", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          status: "success",
          profile: "DEFAULT",
          config_file: "~/.oci/config",
          key_file: "~/.oci/oci_api_key.pem",
          config_file_exists: true,
          key_file_exists: true,
          missing_fields: [],
          permission_issues: [],
          oci_directory_mode: "0700",
          config_file_mode: "0600",
          key_file_mode: "0600",
          message: "OCI へ認証付きで接続できました（Object Storage GetNamespace）。",
          elapsed_ms: 412,
          checked_at: "2026-06-14T00:00:00Z",
          error_type: null,
          stages: [
            { key: "config_format", status: "success", message: "形式を確認しました。", action: null },
            { key: "key_file", status: "success", message: "鍵を確認しました。", action: null },
            { key: "region", status: "success", message: "応答がありました。", action: null },
            { key: "authentication", status: "success", message: "認証しました。", action: null },
          ],
          region: "ap-osaka-1",
          auth_check_operation: "Object Storage GetNamespace",
          http_status: null,
          service_code: null,
          request_id: null,
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.testOciConfig();

    expect(result.status).toBe("success");
    expect(result.stages.map((stage) => stage.status)).toEqual([
      "success",
      "success",
      "success",
      "success",
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/oci/config/test",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("uploadOciPrivateKey は秘密鍵ファイルを FormData で送る", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: { key_file: "~/.oci/oci_api_key.pem", saved: true },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"], "key.pem", {
      type: "application/x-pem-file",
    });
    const result = await api.uploadOciPrivateKey(file);

    expect(result.key_file).toBe("~/.oci/oci_api_key.pem");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/settings/oci/key-file",
      expect.objectContaining({
        method: "POST",
        body: expect.any(FormData),
      })
    );
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });
});

describe("api.services", () => {
  it("getServiceCatalog は /api/services/catalog からプローブなし一覧を取り出す", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          control_enabled: true,
          deployment_mode: "local",
          services: [
            {
              service_id: "parser-docling",
              category: "parser",
              profile: "cpu",
              label_key: "settings.services.item.parserDocling",
              execution_policy: "selected_adapter",
              configured: true,
            },
          ],
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.getServiceCatalog();

    expect(result.deployment_mode).toBe("local");
    expect(result.services[0].service_id).toBe("parser-docling");
    expect(fetchMock).toHaveBeenCalledWith("/api/services/catalog", expect.anything());
  });

  it("getServiceStatus は service_id を URL エンコードして状態を取り出す", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          service_id: "parser-unstructured",
          category: "parser",
          profile: "cpu",
          label_key: "settings.services.item.parserUnstructured",
          execution_policy: "selected_adapter",
          status: "running",
          configured: true,
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.getServiceStatus("parser-unstructured");

    expect(result.status).toBe("running");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/services/parser-unstructured/status",
      expect.anything()
    );
  });

  it("controlService は service_id を URL エンコードして POST する", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: { service_id: "parser-unstructured", action: "start", status: "running" },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.controlService("parser-unstructured", "start");

    expect(result.status).toBe("running");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/services/parser-unstructured/start",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("getServiceLogs は service_id と lines を URL エンコードして取得する", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({
        data: {
          service_id: "parser-docling",
          source: "journald",
          lines: 200,
          content: "ready",
        },
        error_messages: [],
        warning_messages: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.getServiceLogs("parser-docling", 200);

    expect(result.content).toBe("ready");
    expect(result.source).toBe("journald");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/services/parser-docling/logs?lines=200",
      expect.anything()
    );
  });
});

/**
 * アップロードは送信の進み具合を取るため XHR で送る（#306）。node の test 環境には XHR がないため、
 * 送信・応答を手で進められる最小の fake に差し替える。
 */
class FakeXhr {
  static instances: FakeXhr[] = [];
  method = "";
  url = "";
  body: unknown = null;
  status = 0;
  responseText = "";
  readonly requestHeaders: Record<string, string> = {};
  private readonly responseHeaders: Record<string, string> = {};
  private readonly listeners: Record<string, Array<(event: unknown) => void>> = {};
  private readonly uploadListeners: Record<string, Array<(event: unknown) => void>> = {};
  readonly upload = {
    addEventListener: (type: string, listener: (event: unknown) => void) => {
      (this.uploadListeners[type] ??= []).push(listener);
    },
  };

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string) {
    this.requestHeaders[name.toLowerCase()] = value;
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    (this.listeners[type] ??= []).push(listener);
  }

  getResponseHeader(name: string): string | null {
    return this.responseHeaders[name.toLowerCase()] ?? null;
  }

  send(body: unknown) {
    this.body = body;
    FakeXhr.instances.push(this);
  }

  emitUploadProgress(loaded: number, total: number, lengthComputable = true) {
    for (const listener of this.uploadListeners.progress ?? []) {
      listener({ loaded, total, lengthComputable });
    }
  }

  respond(status: number, body: unknown, headers: Record<string, string> = {}) {
    this.status = status;
    this.responseText = typeof body === "string" ? body : JSON.stringify(body);
    for (const [name, value] of Object.entries(headers)) {
      this.responseHeaders[name.toLowerCase()] = value;
    }
    for (const listener of this.listeners.load ?? []) listener({});
  }

  failNetwork() {
    for (const listener of this.listeners.error ?? []) listener({});
  }
}

function lastXhr(): FakeXhr {
  const xhr = FakeXhr.instances.at(-1);
  if (!xhr) throw new Error("XHR が送られていません");
  return xhr;
}

describe("文書アップロードの送信（XHR）", () => {
  const uploadResult = {
    id: "doc-1",
    file_name: "policy.txt",
    status: "UPLOADED",
    file_size_bytes: 4,
    content_sha256: "a".repeat(64),
    duplicate_of_document_id: null,
    knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
  };

  function stubXhr() {
    FakeXhr.instances = [];
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    // fetch では送信の進み具合を取れない。アップロードで fetch を使ったら失敗させる。
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("fetch は使わない")));
  }

  it("uploadDocument は knowledge_base_ids を multipart に含め、ingestion_mode は送らない", async () => {
    stubXhr();

    const pending = api.uploadDocument(new File(["test"], "policy.txt"), ["kb-1", "kb-2"]);
    const xhr = lastXhr();
    xhr.respond(200, { data: uploadResult, error_messages: [], warning_messages: [] });

    await expect(pending).resolves.toMatchObject({ id: "doc-1" });
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/api/documents/upload");
    expect(xhr.requestHeaders.accept).toBe("application/json");
    expect(xhr.body).toBeInstanceOf(FormData);
    const form = xhr.body as FormData;
    expect(form.getAll("knowledge_base_ids")).toEqual(["kb-1", "kb-2"]);
    expect(form.has("ingestion_mode")).toBe(false);
  });

  it("送信の進み具合（送信済み / 合計のバイト数）を通知する", async () => {
    stubXhr();
    const progress = vi.fn();

    const pending = api.batchUploadDocuments(
      [new File(["a"], "a.txt"), new File(["b"], "b.txt")],
      [],
      progress,
    );
    const xhr = lastXhr();
    xhr.emitUploadProgress(0, 0, false);
    xhr.emitUploadProgress(512, 2048);
    xhr.emitUploadProgress(2048, 2048);
    xhr.respond(200, {
      data: { items: [], failed_items: [], total_count: 2, uploaded_count: 0, failed_count: 0 },
      error_messages: [],
      warning_messages: [],
    });

    await pending;
    expect(xhr.url).toBe("/api/documents/batch-upload");
    expect((xhr.body as FormData).getAll("files")).toHaveLength(2);
    // 合計が分からない通知は捨てる。
    expect(progress.mock.calls).toEqual([
      [{ loaded: 512, total: 2048 }],
      [{ loaded: 2048, total: 2048 }],
    ]);
  });

  it("Cookie の CSRF token を X-CSRF-Token として付ける", async () => {
    stubXhr();
    vi.stubGlobal("document", { cookie: "other=1; pr_csrf=token-123" });

    const pending = api.uploadDocument(new File(["test"], "policy.txt"));
    const xhr = lastXhr();
    xhr.respond(200, { data: uploadResult, error_messages: [], warning_messages: [] });
    await pending;

    expect(xhr.requestHeaders["x-csrf-token"]).toBe("token-123");
    // FormData の boundary はブラウザが付けるため Content-Type は指定しない。
    expect(xhr.requestHeaders).not.toHaveProperty("content-type");
  });

  it("既定の API タイムアウトを過ぎても送信を打ち切らない", async () => {
    vi.useFakeTimers();
    stubXhr();

    const pending = api.uploadDocument(new File(["test"], "policy.txt"));
    await vi.advanceTimersByTimeAsync(API_REQUEST_TIMEOUT_MS * 4);
    lastXhr().respond(200, { data: uploadResult, error_messages: [], warning_messages: [] });

    await expect(pending).resolves.toMatchObject({ id: "doc-1" });
  });

  it("エラー応答は error_messages と request ID を持つ ApiError にする", async () => {
    stubXhr();

    const pending = api.uploadDocument(new File(["test"], "policy.txt"));
    lastXhr().respond(
      415,
      { data: null, error_messages: ["対応していないファイル形式です。"], warning_messages: [] },
      { "X-Request-ID": "req-1" },
    );

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 415,
      message: "対応していないファイル形式です。",
      requestId: "req-1",
    });
  });

  it("ApiResponse でない応答（proxy の 413 の HTML）は status だけの ApiError にする", async () => {
    stubXhr();

    const pending = api.uploadDocument(new File(["test"], "policy.txt"));
    lastXhr().respond(413, "<html>413 Request Entity Too Large</html>");

    await expect(pending).rejects.toMatchObject({ status: 413, message: "APIエラー (413)" });
  });

  it("401 は共通の認証イベントを通知する", async () => {
    stubXhr();
    const dispatchEvent = vi.fn();
    vi.stubGlobal("window", { dispatchEvent });
    vi.stubGlobal(
      "CustomEvent",
      class {
        constructor(readonly type: string) {}
      },
    );

    const pending = api.uploadDocument(new File(["test"], "policy.txt"));
    lastXhr().respond(401, { data: null, error_messages: ["ログインしてください。"] });

    await expect(pending).rejects.toMatchObject({ status: 401 });
    expect(dispatchEvent).toHaveBeenCalledWith(
      expect.objectContaining({ type: "app-auth-unauthorized" }),
    );
  });

  it("接続の失敗は通信断の ApiError（日本語の文）にする（Issue 906）", async () => {
    stubXhr();

    const pending = api.uploadDocument(new File(["test"], "policy.txt"));
    lastXhr().failNetwork();

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 0,
      messages: [DEFAULT_API_TRANSPORT_MESSAGES.network + DEFAULT_API_TRANSPORT_MESSAGES.networkAction],
    });
    expect((error as ApiError).cause).toBeInstanceOf(ApiTransportError);
  });
});

describe("document delete confirmation", () => {
  it("通信断後は DELETE を再送せず、消えた文書を確認して警告つきで返す", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("network failure"))
      .mockResolvedValueOnce(jsonResponse({data: {id: "doc-1"}}))
      .mockResolvedValueOnce(jsonResponse({data: null, error_messages: ["not found"]}, 404));
    vi.stubGlobal("fetch", fetchMock);
    const pending = api.deleteDocument("doc-1", "sample.pdf");
    await vi.advanceTimersByTimeAsync(2_000);
    const result = await pending;
    expect(result.id).toBe("doc-1");
    expect(result.file_name).toBe("sample.pdf");
    expect(result.warning_messages).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  });

  it("文書が残っている通信断を、確定した削除失敗とは判定しない", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation(async (_path, init) => {
      if(init?.method === "DELETE") throw new TypeError("network failure");
      return jsonResponse({data: {id:"doc-1"}});
    });
    vi.stubGlobal("fetch", fetchMock);
    const pending = expect(api.deleteDocument("doc-1")).rejects.toMatchObject({errorCode:"document_delete_unconfirmed"});
    await vi.advanceTimersByTimeAsync(20_000);
    await pending;
    expect(fetchMock.mock.calls.filter(([, init])=>init?.method==="DELETE")).toHaveLength(1);
  });

  it("確定した 409 は再取得や再送をせず、その理由を返す", async () => {
    const fetchMock=vi.fn().mockResolvedValue(jsonResponse({error_messages:["競合しています。"]},409));
    vi.stubGlobal("fetch",fetchMock);
    await expect(api.deleteDocument("doc-1")).rejects.toMatchObject({status:409});
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("45 秒後の削除成功を通常の 30 秒で打ち切らない", async () => {
    vi.useFakeTimers();
    let aborted=false;
    vi.stubGlobal("fetch",vi.fn((_path,init)=>new Promise((resolve,reject)=>{
      init?.signal?.addEventListener("abort",()=>{aborted=true; reject(new DOMException("aborted","AbortError"));});
      setTimeout(()=>resolve(jsonResponse({data:{id:"doc-1",file_name:"sample.pdf"},warning_messages:[]})),45_000);
    })));
    const pending=api.deleteDocument("doc-1");
    await vi.advanceTimersByTimeAsync(30_001);
    expect(aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(14_999);
    expect((await pending).id).toBe("doc-1");
  });
});


it("結果 Excel の取得は job ID と入力を POST し、ファイル名を保持する", async () => {
  const fetchMock=vi.fn().mockResolvedValue(new Response("xlsx",{status:200,headers:{"Content-Disposition":'attachment; filename="evaluation_results.xlsx"'}}));
  vi.stubGlobal("fetch",fetchMock);
  const body={rows:[],cases:[],profile_name:"プロファイル A",compare_profile_name:null};
  const file=await api.downloadEvaluationResults("job/1",body);
  expect(fetchMock.mock.calls[0][0]).toBe("/api/evaluation/jobs/job%2F1/results.xlsx");
  expect(fetchMock.mock.calls[0][1]).toMatchObject({method:"POST",body:JSON.stringify(body),credentials:"same-origin"});
  expect(file.fileName).toBe("evaluation_results.xlsx");
});
