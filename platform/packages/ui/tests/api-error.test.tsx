import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  ApiErrorBanner,
  ApiErrorDetailList,
  ApiErrorState,
  ApiTransportError,
  ErrorState,
  apiErrorMessage,
  httpApiErrorPresentation,
  isAbortError,
  isTimeoutError,
  isTransportError,
  presentApiError,
  toApiTransportError,
  transportErrorOf,
  type ApiErrorPresentable,
} from "../src";

/** 製品の ApiError の形（RAG / Agent / NL2SQL は toApiErrorPresentation を実装する）。 */
class ProductApiError extends Error implements ApiErrorPresentable {
  readonly status: number;
  readonly messages: string[];
  readonly requestId?: string;

  constructor(status: number, messages: string[], requestId?: string, options?: { cause?: unknown }) {
    super(messages[0], options);
    this.status = status;
    this.messages = messages;
    this.requestId = requestId;
  }

  toApiErrorPresentation() {
    return httpApiErrorPresentation(this);
  }
}

const networkFailure = () =>
  new ApiTransportError("network", { method: "GET", path: "/api/x" }, new TypeError("Failed to fetch"));

describe("ApiTransportError（#900 / #906）", () => {
  it("timeout は上限の秒数を出し、name は TimeoutError のまま。path の query は詳細に出さない", () => {
    const error = new ApiTransportError(
      "timeout",
      { method: "POST", path: "/api/search?q=secret", timeoutMs: 29_500 },
      new DOMException("signal timed out", "TimeoutError"),
    );
    expect(error.summary).toBe("サーバーの応答が 30 秒以内に返りませんでした。");
    expect(error.message).toBe(`${error.summary}${error.nextAction}`);
    expect(error.message).not.toMatch(/signal timed out/u);
    expect(error.path).toBe("/api/search");
    expect(isTimeoutError(error)).toBe(true);
    expect(isTransportError(error)).toBe(true);
  });

  it("上限が分からない timeout と通信断", () => {
    expect(new ApiTransportError("timeout", { method: "GET", path: "/a" }, null).summary).toBe(
      "サーバーの応答が規定の時間内に返りませんでした。",
    );
    const network = networkFailure();
    expect(network.summary).toBe("サーバーに接続できませんでした。");
    expect(network.nextAction).toMatch(/ネットワークの接続/u);
    expect(network.name).toBe("NetworkError");
    expect(network.causeName).toBe("TypeError");
    expect(network.causeMessage).toBe("Failed to fetch");
  });

  it("toApiTransportError は timeout・通信断だけを変え、利用者の中止は変えない", () => {
    const request = { method: "GET", path: "/api/x", timeoutMs: 1000 };
    expect(toApiTransportError(new DOMException("aborted", "AbortError"), request)).toBeNull();
    expect(toApiTransportError(new Error("boom"), request)).toBeNull();
    expect(toApiTransportError(new TypeError("Failed to fetch"), request)?.kind).toBe("network");
    const timeout = toApiTransportError(new DOMException("signal timed out", "TimeoutError"), request);
    expect(timeout?.kind).toBe("timeout");
    expect(toApiTransportError(timeout, request)).toBe(timeout);
    expect(isAbortError(new DOMException("aborted", "AbortError"))).toBe(true);
  });

  it("製品の ApiError が cause に包んだ ApiTransportError も取り出す", () => {
    const transport = networkFailure();
    const wrapped = new ProductApiError(0, [transport.message], undefined, { cause: transport });
    expect(transportErrorOf(wrapped)).toBe(transport);
    expect(transportErrorOf(new ProductApiError(500, ["x"]))).toBeNull();
  });
});

describe("presentApiError", () => {
  it("timeout は日本語の要約・次の操作と、英語の元の文を含む詳細に分ける", () => {
    const presented = presentApiError(
      new ApiTransportError(
        "timeout",
        { method: "POST", path: "/api/nl2sql/jobs", timeoutMs: 30_000 },
        new DOMException("signal timed out", "TimeoutError"),
      ),
      "SQL の生成を開始できませんでした。",
    );
    expect(presented.summary).toBe("サーバーの応答が 30 秒以内に返りませんでした。");
    expect(presented.nextAction).toMatch(/画面を更新して結果を確かめ/u);
    expect(presented.details).toEqual([
      { label: "要求", value: "POST /api/nl2sql/jobs" },
      { label: "待ち時間の上限", value: "30 秒" },
      { label: "エラー種別", value: "TimeoutError" },
      { label: "元のメッセージ", value: "signal timed out" },
    ]);
  });

  it("製品の ApiError に包んだ通信断も、通信断として出す", () => {
    const transport = networkFailure();
    const presented = presentApiError(
      new ProductApiError(0, [transport.message], undefined, { cause: transport }),
      "既定",
    );
    expect(presented.summary).toBe("サーバーに接続できませんでした。");
    expect(presented.details.map((item) => item.label)).toEqual(["要求", "エラー種別", "元のメッセージ"]);
  });

  it("backend の失敗は request ID を本文に重ねず詳細に出す", () => {
    const presented = presentApiError(new ProductApiError(503, ["モデルへ接続できません。"], "req-1"), "既定");
    expect(presented).toEqual({
      summary: "モデルへ接続できません。",
      details: [
        { label: "HTTP ステータス", value: "503" },
        { label: "リクエストID", value: "req-1" },
      ],
    });
  });

  it("入力の検証エラー（422）は利用者向けの文を要約にし、技術的な原文は詳細に出す（#1065）", () => {
    const presented = httpApiErrorPresentation({
      status: 422,
      messages: ["cases[0].query: 必須の項目です。入力してください。", "ケースの id が重複しています。"],
      errorCode: "REQUEST_VALIDATION_FAILED",
      requestId: "req-422",
      fieldErrors: [
        {
          pointer: "/cases/0/query",
          code: "missing",
          message: "必須の項目です。入力してください。",
          raw_location: "body.cases.0.query",
          raw_message: "Field required",
        },
        {
          pointer: "/cases",
          code: "value_error",
          message: "ケースの id が重複しています。",
          raw_location: "body.cases",
          raw_message: "Value error, ケースの id が重複しています。",
        },
        // 原文の無い field error（業務の 409 / 422 など）は詳細に出さない。
        { pointer: "/name", code: "conflict", message: "同じ名前があります。" },
      ],
    });
    expect(presented).toEqual({
      summary: "cases[0].query: 必須の項目です。入力してください。\nケースの id が重複しています。",
      details: [
        { label: "HTTP ステータス", value: "422" },
        { label: "エラーコード", value: "REQUEST_VALIDATION_FAILED" },
        {
          label: "入力の検証の原文",
          value:
            "body.cases.0.query: Field required (missing)\nbody.cases: Value error, ケースの id が重複しています。 (value_error)",
        },
        { label: "リクエストID", value: "req-422" },
      ],
    });
    const html = renderToStaticMarkup(<ApiErrorDetailList details={presented.details} />);
    expect(html).toContain("whitespace-pre-line");
  });

  it("組み込みの例外（英語の文）は既定の文にし、元の文は詳細に出す", () => {
    const presented = presentApiError(new SyntaxError("Unexpected token '<'"), "読み込めませんでした。");
    expect(presented.summary).toBe("読み込めませんでした。");
    expect(presented.details).toEqual([
      { label: "エラー種別", value: "SyntaxError" },
      { label: "元のメッセージ", value: "Unexpected token '<'" },
    ]);
    expect(presentApiError(new TypeError("Failed to fetch"), "既定").summary).toBe("既定");
  });

  it("製品の日本語の Error はそのまま、Error 以外は既定の文", () => {
    expect(presentApiError(new Error("ファイルが空です。"), "既定").summary).toBe("ファイルが空です。");
    expect(presentApiError("x", "既定の文")).toEqual({ summary: "既定の文", details: [] });
  });

  it("apiErrorMessage は要約と次の操作をつなげ、技術的な詳細を含めない", () => {
    const message = apiErrorMessage(networkFailure(), "既定");
    expect(message).toMatch(/^サーバーに接続できませんでした。ネットワークの接続/u);
    expect(message).not.toMatch(/Failed to fetch/u);
  });

  it("apiErrorMessage は製品の ApiError の message をそのまま、組み込みの例外は既定の文にする", () => {
    expect(apiErrorMessage(new ProductApiError(500, ["失敗しました。（リクエストID: r1）"]), "既定")).toBe(
      "失敗しました。（リクエストID: r1）",
    );
    expect(apiErrorMessage(new SyntaxError("Unexpected token"), "既定")).toBe("既定");
    expect(apiErrorMessage(undefined, "既定")).toBe("既定");
  });
});

describe("ApiErrorBanner / ApiErrorDetailList", () => {
  it("要約・次の操作・開いた「詳細」を danger の Banner で出し、英語の文は詳細にだけ出す", () => {
    const html = renderToStaticMarkup(
      <ApiErrorBanner error={networkFailure()} fallback="読み込めませんでした。" testId="api-error" />,
    );
    expect(html).toContain('data-testid="api-error"');
    expect(html).toContain('role="alert"');
    expect(html).toContain("サーバーに接続できませんでした。");
    expect(html).toContain("ネットワークの接続");
    expect(html).toContain("<dd");
    expect(html.indexOf("Failed to fetch")).toBeGreaterThan(html.indexOf("詳細"));
  });

  it("要約と次の操作を画面の文で差し替えられる", () => {
    const html = renderToStaticMarkup(
      <ApiErrorBanner error={new Error("x")} fallback="既定" summary="画面の要約" nextAction="画面の次の操作" />,
    );
    expect(html).toContain("画面の要約");
    expect(html).toContain("画面の次の操作");
  });

  it("詳細が無ければ折りたたみを描かない", () => {
    expect(renderToStaticMarkup(<ApiErrorDetailList details={[]} />)).toBe("");
  });

  it("ErrorState は details を本文の下に出す", () => {
    const html = renderToStaticMarkup(
      <ErrorState
        message="一覧を読み込めませんでした。"
        details={<ApiErrorDetailList details={[{ label: "要求", value: "GET /api/x" }]} />}
      />,
    );
    expect(html.indexOf("GET /api/x")).toBeGreaterThan(html.indexOf("一覧を読み込めませんでした。"));
  });

  it("ApiErrorState は要約と次の操作を本文に、英語の文を「詳細」に出し、再試行を出す", () => {
    const html = renderToStaticMarkup(
      <ApiErrorState error={networkFailure()} fallback="一覧を読み込めませんでした。" onRetry={() => undefined} />,
    );
    expect(html).toContain("サーバーに接続できませんでした。");
    expect(html).toContain("再試行");
    expect(html.indexOf("Failed to fetch")).toBeGreaterThan(html.indexOf("詳細"));
  });
});
