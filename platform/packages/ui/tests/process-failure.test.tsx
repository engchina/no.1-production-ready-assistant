// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ChatProgress,
  ProcessFailureNotice,
  RunTimeline,
  processFailureDetails,
  processFailureFacts,
  type ProcessFailure,
} from "../src";

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

const FAILURE: ProcessFailure = {
  summary: "回答の保存で失敗しました。",
  cause: "回答の記録をデータベースに保存できませんでした。",
  nextAction: "もう一度送信してください。続くときは、トレース ID を添えて管理者に連絡してください。",
  failedStep: "回答の記録の保存",
  errorType: "DatabaseError",
  errorCode: "ORA-03146",
  traceId: "trace-1",
  occurredAt: "2026-10-18T06:30:41.000Z",
  extraDetails: [{ label: "元のメッセージ", value: "" }],
};

describe("ProcessFailureNotice（#2342）", () => {
  it("何が起きたか → 原因 → 次の操作 → 開いた「詳細」（止まった段・種類・コード・トレース ID・時刻）の順に出す", () => {
    act(() => root.render(<ProcessFailureNotice failure={FAILURE} testId="failure" />));
    const notice = host.querySelector('[data-testid="failure"]');
    expect(notice?.getAttribute("data-process-failure")).toBe("danger");
    expect(notice?.querySelector('[role="alert"]')).not.toBeNull();
    const text = notice?.textContent ?? "";
    const order = [FAILURE.summary, FAILURE.cause!, FAILURE.nextAction!, "詳細", "止まった段", "ORA-03146", "trace-1"];
    const positions = order.map((part) => text.indexOf(part));
    expect(positions.every((position) => position >= 0)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);
    // 失敗のときは「詳細」を開いて出す（messaging.md §10.3）。値の無い項目は並べない。
    expect(notice?.querySelector("details")?.open).toBe(true);
    expect(text).not.toContain("元のメッセージ");
  });

  it("warning（結果はあるが保存できなかった）は「詳細」を閉じて出し、role=alert にしない", () => {
    act(() => root.render(<ProcessFailureNotice failure={FAILURE} severity="warning" testId="failure" />));
    const notice = host.querySelector('[data-testid="failure"]');
    expect(notice?.getAttribute("data-process-failure")).toBe("warning");
    expect(notice?.querySelector('[role="alert"]')).toBeNull();
    expect(notice?.querySelector("details")?.open).toBe(false);
  });

  it("DB の原文（原因）とコードから引ける平易な説明を別の段落で出す", () => {
    act(() =>
      root.render(
        <ProcessFailureNotice
          failure={{ summary: "この文の実行に失敗しました。", cause: "ORA-00904: invalid identifier", explanation: "列名が存在しません。" }}
          testId="failure"
        />
      )
    );
    expect(host.querySelector("[data-process-failure-cause]")?.textContent).toBe("ORA-00904: invalid identifier");
    expect(host.querySelector("[data-process-failure-explanation]")?.textContent).toBe("列名が存在しません。");
  });

  it("原因が 1 文目と同じなら重ねて出さない", () => {
    const html = renderToStaticMarkup(
      <ProcessFailureNotice failure={{ summary: "回答できませんでした。", cause: "回答できませんでした。" }} />
    );
    expect(html.split("回答できませんでした。").length - 1).toBe(1);
  });

  it("詳細と技術的な詳細の事実は同じ項目・同じ名前（値の無い項目は除く）", () => {
    const details = processFailureDetails(FAILURE, { formatDateTime: () => "2026/10/18 15:30:41" });
    expect(details.map((item) => item.label)).toEqual(["止まった段", "エラーの種類", "エラーコード", "トレース ID", "時刻"]);
    const facts = processFailureFacts(FAILURE, { formatDateTime: () => "2026/10/18 15:30:41" });
    expect(facts.map((fact) => [fact.label, fact.value])).toEqual(details.map((item) => [item.label, item.value]));
    expect(facts.find((fact) => fact.id === "failure-errorCode")?.mono).toBe(true);
  });
});

describe("失敗の後の未実行の段（#2342）", () => {
  it("実行の詳細の処理の経過は、失敗した段を ✗ と「失敗」、実行しなかった段を「未実行」と文字で出す", () => {
    act(() =>
      root.render(
        <RunTimeline
          steps={[
            { id: "a", label: "回答の生成", status: "done", durationMs: 1000 },
            { id: "b", label: "回答の記録の保存", status: "failed", durationMs: 10 },
            { id: "c", label: "会話への保存", status: "pending" },
          ]}
        />
      )
    );
    const rows = [...host.querySelectorAll("[data-run-timeline-step]")];
    expect(rows.map((row) => row.getAttribute("data-status"))).toEqual(["done", "failed", "pending"]);
    expect(rows[1].textContent).toContain("失敗");
    expect(rows[2].textContent).toContain("未実行");
  });

  it("チャットの処理の経過は、終わった後の待機中の段を「未実行」と出す（待機中と出さない）", () => {
    act(() =>
      root.render(
        <ChatProgress
          testId="p"
          active={false}
          steps={[
            { id: "a", label: "回答の生成", status: "done", durationMs: 1000 },
            { id: "b", label: "回答の記録の保存", status: "failed", durationMs: 10 },
            { id: "c", label: "回答の確認", status: "pending" },
          ]}
        />
      )
    );
    const pending = host.querySelector('[data-testid="p-step-c"]');
    expect(pending?.textContent).toContain("未実行");
    expect(pending?.textContent).not.toContain("待機中");
  });
});
