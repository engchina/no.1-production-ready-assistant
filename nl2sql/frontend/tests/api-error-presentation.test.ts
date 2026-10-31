import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, ApiTransportError } from "../src/lib/api.ts";
import { presentApiError } from "@production-ready/ui";

test("timeout は日本語の要約・次の操作と、英語の元の文を含む詳細に分ける (#900)", () => {
  const cause = new DOMException("signal timed out", "TimeoutError");
  const presented = presentApiError(
    new ApiTransportError(
      "timeout",
      { method: "POST", path: "/api/nl2sql/jobs", timeoutMs: 30_000 },
      cause,
    ),
    "SQL の生成を開始できませんでした。",
  );
  assert.equal(presented.summary, "サーバーの応答が 30 秒以内に返りませんでした。");
  assert.match(presented.nextAction ?? "", /画面を更新して結果を確かめ/u);
  assert.doesNotMatch(`${presented.summary}${presented.nextAction}`, /signal timed out/u);
  assert.deepEqual(presented.details, [
    { label: "要求", value: "POST /api/nl2sql/jobs" },
    { label: "待ち時間の上限", value: "30 秒" },
    { label: "エラー種別", value: "TimeoutError" },
    { label: "元のメッセージ", value: "signal timed out" },
  ]);
});

test("通信断は接続の確認を案内する (#900)", () => {
  const presented = presentApiError(
    new ApiTransportError(
      "network",
      { method: "GET", path: "/api/nl2sql/chats" },
      new TypeError("Failed to fetch"),
    ),
    "会話を読み込めませんでした。",
  );
  assert.equal(presented.summary, "サーバーに接続できませんでした。");
  assert.match(presented.nextAction ?? "", /ネットワークの接続/u);
  assert.deepEqual(
    presented.details.map((item) => item.label),
    ["要求", "エラー種別", "元のメッセージ"],
  );
});

test("NL2SQL の ApiError は request ID を本文に重ねず詳細に出す（共通の presentApiError。#906）", () => {
  const presented = presentApiError(
    new ApiError(503, ["モデルへ接続できません。"], "MODEL_UNAVAILABLE", undefined, undefined, "req-1"),
    "SQL の生成を開始できませんでした。",
  );
  assert.equal(presented.summary, "モデルへ接続できません。");
  assert.equal(presented.nextAction, undefined);
  assert.deepEqual(presented.details, [
    { label: "HTTP ステータス", value: "503" },
    { label: "エラーコード", value: "MODEL_UNAVAILABLE" },
    { label: "リクエストID", value: "req-1" },
  ]);
});

test("Error 以外は画面の既定の文にする", () => {
  assert.deepEqual(presentApiError("x", "既定の文"), { summary: "既定の文", details: [] });
});
