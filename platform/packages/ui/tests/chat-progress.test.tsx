// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatProgress, formatChatProgressDuration, type ChatProgressStep } from "../src";

// #1145: チャットの回答の処理の段階（3 製品共通の契約）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const T0 = "2026-10-04T00:00:00.000Z";
const at = (seconds: number) => new Date(Date.parse(T0) + seconds * 1000).toISOString();

const runningSteps: ChatProgressStep[] = [
  { id: "queue", label: "処理を開始しました", status: "done", startedAt: at(0), finishedAt: at(0.4) },
  { id: "schema", label: "対象の表を調べました", status: "done", startedAt: at(0.4), finishedAt: at(2) },
  { id: "generate_sql", label: "SQL を生成しています", status: "running", startedAt: at(2), detail: "Enterprise AI" },
  { id: "safety", label: "安全性を確認します", status: "pending" },
];

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
  vi.useRealTimers();
});

function render(node: React.ReactNode) {
  act(() => root.render(node));
}

const q = (testId: string) => host.querySelector(`[data-testid="${testId}"]`) as HTMLElement | null;

describe("formatChatProgressDuration", () => {
  it("10 秒未満は 0.1 秒単位（0.1 秒に満たなければ「0.1 秒未満」）、1 分未満は秒、それ以上は分と秒・時間と分", () => {
    expect(formatChatProgressDuration(0)).toBe("0.0 秒");
    expect(formatChatProgressDuration(42)).toBe("0.1 秒未満");
    expect(formatChatProgressDuration(100)).toBe("0.1 秒");
    expect(formatChatProgressDuration(420)).toBe("0.4 秒");
    expect(formatChatProgressDuration(9_960)).toBe("9.9 秒");
    expect(formatChatProgressDuration(12_400)).toBe("12 秒");
    expect(formatChatProgressDuration(65_000)).toBe("1 分 5 秒");
    expect(formatChatProgressDuration(3_723_000)).toBe("1 時間 2 分");
    expect(formatChatProgressDuration(-5)).toBe("0.0 秒");
  });
});

describe("ChatProgress", () => {
  it("一部だけの記録は、開いた中身の段の一覧の後ろに案内を出す（#3226）", () => {
    const steps: ChatProgressStep[] = [{ id: "s1", label: "合成の工程", status: "done", startedAt: "2026-01-01T00:00:00Z", finishedAt: "2026-01-01T00:00:01Z" }];
    render(<ChatProgress steps={steps} partial testId="progress" />);
    act(() => q("progress-summary")?.click());
    expect(q("progress-partial")?.textContent).toBe("一部の処理だけを記録しています。");
    render(<ChatProgress steps={steps} testId="progress" />);
    expect(q("progress-partial")).toBeNull();
  });

  it("段階が無いときは何も出さない", () => {
    expect(renderToStaticMarkup(<ChatProgress steps={[]} />)).toBe("");
  });

  it("実行中で段（span）がまだ無いときは「処理を開始しています」の行を出し、段が届いたら消す（#3226）", () => {
    render(<ChatProgress steps={[]} active startedAt={T0} testId="p" />);
    expect(host.textContent).toContain("処理を開始しています");
    render(<ChatProgress steps={[{ id: "s", label: "段 A", status: "running", startedAt: at(1) }]} active startedAt={T0} testId="p" />);
    expect(host.textContent).not.toContain("処理を開始しています");
    expect(host.textContent).toContain("段 A");
  });

  it("実行中は見出しに全体の状態と経過時間、開いた 1 本のタイムラインの最後の行に今の段階を出す（#2309）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(5)));
    render(<ChatProgress steps={runningSteps} testId="p" />);
    const rootEl = q("p")!;
    expect(rootEl.dataset.chatProgressState).toBe("running");
    expect(rootEl.getAttribute("aria-busy")).toBe("true");
    const current = q("p-current")!;
    expect(current.dataset.stepId).toBe("generate_sql");
    expect(current.textContent).toContain("SQL を生成しています");
    expect(current.textContent).toContain("（Enterprise AI）");
    // 経過時間は処理全体（最初の段階の開始 0 秒）から数える（#1176。段階ごとに 0 へ戻さない）。
    expect(q("p-timer")!.getAttribute("aria-label")).toBe("経過時間 00:05");
    expect(q("p-timer")!.getAttribute("aria-live")).toBe("off");
    // 動くスピナーは今の段階の 1 つだけ。
    expect(host.querySelectorAll("svg.animate-spin")).toHaveLength(1);
    // 一覧は既定で開く（#2096。行が増えていく）。見出しは全体の状態だけ（開いているときは今の段階を重ねない）。
    expect(q("p-running")!.textContent).toContain("処理中（2 ステップ完了）");
    expect(q("p-running")!.textContent).not.toContain("SQL を生成しています");
    expect(q("p-running")!.contains(q("p-timer"))).toBe(true);
    expect(host.querySelector("details")!.open).toBe(true);
    // 今の段階は完了した段階の後ろ（タイムラインの最後の行）。今の段階の経過時間は秒の単位で出す。
    const order = [q("p-step-queue")!, q("p-step-schema")!, current];
    expect(order[0].compareDocumentPosition(order[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(order[1].compareDocumentPosition(order[2]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(q("p-current-elapsed")!.textContent).toBe("3.0 秒");
    // 段階の切り替わりの読み上げは今の段階の名前だけ。
    const status = host.querySelector('[role="status"]')!;
    expect(status.textContent).toBe("SQL を生成しています");
  });

  it("完了した段階の一覧に、段階ごとの状態（読み上げの語を含む）と所要時間を出し、閉じられる", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(5)));
    render(<ChatProgress steps={runningSteps} testId="p" />);
    expect(host.querySelector("details")!.open).toBe(true);
    const schema = q("p-step-schema")!;
    expect(schema.dataset.status).toBe("done");
    expect(schema.textContent).toContain("対象の表を調べました");
    expect(schema.textContent).toContain("完了");
    expect(schema.textContent).toContain("1.6 秒");
    // 実行中の段階は完了の行ではなく、最後の今の段階の行に出す。
    expect(q("p-current")?.contains(q("p-step-generate_sql"))).toBe(true);
    act(() => {
      q("p-running")!.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    });
    expect(host.querySelector("details")!.open).toBe(false);
    // 閉じたら、見出しに今の段階の名前を添える（閉じても何をしているかが分かる）。
    expect(q("p-running")!.textContent).toContain("処理中（2 ステップ完了）・SQL を生成しています");
    // 閉じた状態は完了後も保つ（同じ表示の間）。
    render(
      <ChatProgress
        steps={runningSteps.map((step) => ({ ...step, status: "done" as const, finishedAt: step.finishedAt ?? at(5) }))}
        testId="p"
      />
    );
    expect(q("p")!.dataset.chatProgressState).toBe("done");
    expect(host.querySelector("details")!.open).toBe(false);
  });

  it("遅延の案内は実行中の見出しの直下に、段階の名前を添えずに出し、行は最初から高さを予約する（#2426）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(5)));
    render(<ChatProgress steps={runningSteps} testId="p" />);
    expect(q("p-current")!.dataset.slow).toBe("false");
    expect(q("p-slow")).toBeNull();
    const notice = q("p-notice")!;
    // 見出し（押せる行）の中の、見出しの文字の直下。右の経過時間の列・今の段階の行には置かない。
    expect(q("p-running")!.contains(notice)).toBe(true);
    expect(q("p-timer")!.contains(notice)).toBe(false);
    expect(q("p-current")!.contains(notice)).toBe(false);
    // 予約の文言は ::before の content（属性）だけにあり、textContent・読み上げには入らない。
    expect(notice.querySelector('[data-placeholder="通常より時間がかかっています。"]')).not.toBeNull();
    expect(notice.textContent).toBe("");
    act(() => {
      vi.setSystemTime(Date.parse(at(13)));
      vi.advanceTimersByTime(1000);
    });
    expect(q("p-current")!.dataset.slow).toBe("true");
    expect(notice.dataset.slow).toBe("true");
    expect(q("p-slow")!.textContent).toBe("通常より時間がかかっています。");
    expect(notice.contains(q("p-slow"))).toBe(true);
    // 段階の切り替わりの読み上げは今の段階の名前だけ（遅延の案内を重ねない）。
    expect(host.querySelector('[role="status"]')!.textContent).toBe("SQL を生成しています");
  });

  it("一度出た遅延の案内は、段階が変わっても処理が終わるまで消さない（#2426）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(13)));
    render(<ChatProgress steps={runningSteps} progressKey="turn-1" testId="p" />);
    expect(q("p-slow")!.textContent).toBe("通常より時間がかかっています。");
    // 次の段階へ進む（今の段階の経過時間は 0 に戻るが、案内は消えない）。
    const nextSteps: ChatProgressStep[] = [
      ...runningSteps.slice(0, 2),
      { ...runningSteps[2]!, status: "done", label: "SQL を生成しました", finishedAt: at(13) },
      { id: "safety", label: "安全性を確認しています", status: "running", startedAt: at(13) },
    ];
    act(() => {
      vi.setSystemTime(Date.parse(at(14)));
      vi.advanceTimersByTime(1000);
    });
    render(<ChatProgress steps={nextSteps} progressKey="turn-1" testId="p" />);
    expect(q("p-current")!.dataset.stepId).toBe("safety");
    // 今の段階は始まったばかり（閾値の 10 秒より短い）。
    expect(q("p-current-elapsed")!.textContent).toBe("2.0 秒");
    expect(q("p-slow")!.textContent).toBe("通常より時間がかかっています。");
    expect(q("p-current")!.dataset.slow).toBe("true");
    // 取り直している間は同じ場所に「接続を確認しています」を出し、戻ったら遅延の案内に戻す。
    render(<ChatProgress steps={nextSteps} progressKey="turn-1" reconnecting testId="p" />);
    expect(q("p-slow")).toBeNull();
    expect(q("p-reconnecting")!.textContent).toBe("接続を確認しています。");
    render(<ChatProgress steps={nextSteps} progressKey="turn-1" testId="p" />);
    expect(q("p-slow")!.textContent).toBe("通常より時間がかかっています。");
    // 処理が終わったら消える。次の処理（別の progressKey）は最初から判断する。
    render(
      <ChatProgress
        steps={nextSteps.map((step) => ({ ...step, status: "done" as const, finishedAt: step.finishedAt ?? at(14) }))}
        progressKey="turn-1"
        testId="p"
      />
    );
    expect(q("p-slow")).toBeNull();
    expect(q("p-notice")).toBeNull();
    render(
      <ChatProgress
        steps={[{ id: "queue", label: "開始を待っています", status: "running", startedAt: at(14) }]}
        progressKey="turn-2"
        testId="p"
      />
    );
    expect(q("p-notice")!.dataset.slow).toBe("false");
    expect(q("p-slow")).toBeNull();
  });

  it("閾値が有限でない間（承認待ちなど）は遅延の案内を出さず、出ていた案内も忘れる（#2426）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(13)));
    render(<ChatProgress steps={runningSteps} progressKey="run-1" testId="p" />);
    expect(q("p-slow")).not.toBeNull();
    render(
      <ChatProgress steps={runningSteps} progressKey="run-1" slowAfterMs={Number.POSITIVE_INFINITY} testId="p" />
    );
    expect(q("p-slow")).toBeNull();
    // 行の高さの予約は残す（見出しが動かない）。
    expect(q("p-notice")).not.toBeNull();
  });

  it("取り直している間は、遅延の案内の場所（見出しの直下）に「接続を確認しています」を出す（#1160 / #2426）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(13)));
    render(<ChatProgress steps={runningSteps} reconnecting testId="p" />);
    expect(q("p-slow")).toBeNull();
    expect(q("p-reconnecting")!.textContent).toBe("接続を確認しています。");
    expect(q("p-notice")!.contains(q("p-reconnecting"))).toBe(true);
    expect(q("p-running")!.contains(q("p-reconnecting"))).toBe(true);
    expect(q("p-current")!.dataset.reconnecting).toBe("true");
  });

  it("段階が変わっても、処理全体の経過時間は 0 に戻らない（#1176）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(5)));
    render(<ChatProgress steps={runningSteps} testId="p" />);
    expect(q("p-timer")!.getAttribute("aria-label")).toBe("経過時間 00:05");
    // 次の段階へ進む（安全性の確認の開始 20 秒・今 21 秒）。
    act(() => {
      vi.setSystemTime(Date.parse(at(20)));
      vi.advanceTimersByTime(1000);
    });
    render(
      <ChatProgress
        steps={[
          ...runningSteps.slice(0, 2),
          { ...runningSteps[2]!, status: "done", label: "SQL を生成しました", finishedAt: at(20) },
          { id: "safety", label: "安全性を確認しています", status: "running", startedAt: at(20) },
        ]}
        testId="p"
      />
    );
    expect(q("p-current")!.dataset.stepId).toBe("safety");
    expect(q("p-timer")!.getAttribute("aria-label")).toBe("経過時間 00:21");
    // 段階ごとの所要時間は、完了した段階の行に出す。
    expect(q("p-step-generate_sql")!.textContent).toContain("18 秒");
  });

  it("開始の時刻を渡すと、その時刻から数える（段階に時刻が無いときも全体で数える）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(70)));
    render(
      <ChatProgress
        startedAt={at(4)}
        steps={[{ id: "queue", label: "開始を待っています", status: "running" }]}
        testId="p"
      />
    );
    expect(q("p-timer")!.getAttribute("aria-label")).toBe("経過時間 01:06");
  });

  it("終端でないのに今の段階が無い（すべて完了）ときも、今の行（スピナー・「処理を続けています」・経過時間）を出す", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(30)));
    render(
      <ChatProgress
        active
        steps={[
          { id: "a", label: "受け付けました", status: "done", startedAt: at(0), finishedAt: at(1) },
          { id: "b", label: "SQL を生成しました", status: "done", startedAt: at(1), finishedAt: at(12) },
        ]}
        testId="p"
      />
    );
    const current = q("p-current")!;
    expect(current.dataset.stepId).toBe("");
    expect(current.textContent).toContain("処理を続けています");
    expect(host.querySelectorAll("svg.animate-spin")).toHaveLength(1);
    expect(q("p-timer")!.getAttribute("aria-label")).toBe("経過時間 00:30");
    expect(q("p-running")!.textContent).toContain("処理中（2 ステップ完了）");
    expect(host.querySelector('[role="status"]')!.textContent).toBe("処理を続けています");
  });

  it("遅延の案内は今の段階の経過時間で決める（全体が長くても、始まったばかりの段階には付けない）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(31)));
    render(
      <ChatProgress
        steps={[
          { id: "a", label: "SQL を生成しました", status: "done", startedAt: at(0), finishedAt: at(30) },
          { id: "b", label: "安全性を確認しています", status: "running", startedAt: at(30) },
        ]}
        testId="p"
      />
    );
    expect(q("p-timer")!.getAttribute("aria-label")).toBe("経過時間 00:31");
    expect(q("p-current")!.dataset.slow).toBe("false");
    act(() => {
      vi.setSystemTime(Date.parse(at(41)));
      vi.advanceTimersByTime(1000);
    });
    expect(q("p-current")!.dataset.slow).toBe("true");
    expect(q("p-slow")!.textContent).toBe("通常より時間がかかっています。");
  });

  it("実行中の段階が無いときは、次の段階を今の段階として出す", () => {
    render(
      <ChatProgress
        steps={[
          { id: "queue", label: "開始を待っています", status: "pending" },
          { id: "generate_sql", label: "SQL を生成します", status: "pending" },
        ]}
        testId="p"
      />
    );
    expect(q("p")!.dataset.chatProgressState).toBe("running");
    expect(q("p-current")!.dataset.stepId).toBe("queue");
    // 完了した段階が無いうちは、見出しは「処理中」だけ（件数を出さない）。
    expect(q("p-running")!.textContent).toContain("処理中");
    expect(q("p-running")!.textContent).not.toContain("ステップ完了");
  });

  it("完了後は「処理の経過（N ステップ・M 秒）」の 1 行に閉じ、押すと時間の線つきの一覧を開く（#2265）", () => {
    render(
      <ChatProgress
        steps={[
          { id: "a", label: "受け付けました", status: "done", startedAt: at(0), finishedAt: at(1) },
          { id: "b", label: "SQL を生成しました", status: "done", startedAt: at(1), finishedAt: at(12.5) },
          { id: "c", label: "実行しませんでした", status: "skipped" },
        ]}
        testId="p"
      />
    );
    expect(q("p")!.dataset.chatProgressState).toBe("done");
    expect(q("p")!.getAttribute("aria-busy")).toBe("false");
    expect(q("p-summary")!.textContent).toBe("処理の経過（3 ステップ・12 秒）");
    expect(host.querySelector("details")!.open).toBe(false);
    act(() => q("p-summary")!.click());
    expect(host.querySelector("details")!.open).toBe(true);
    // 開いた一覧は実行の詳細と同じ RunTimeline（時間の線は飾りで aria-hidden。位置は全体に対する %）。
    const timeline = q("p-timeline")!;
    expect(timeline.dataset.runTimelineBars).toBe("true");
    const bars = host.querySelectorAll<HTMLElement>("[data-run-timeline-bar]");
    expect(bars).toHaveLength(3);
    expect(bars[0]!.getAttribute("aria-hidden")).toBe("true");
    expect(bars[1]!.querySelector("span")!.style.left).toMatch(/^8(\.\d+)?%$/);
    // 時刻の無い段（スキップ）は線を描かない。
    expect(bars[2]!.querySelector("span")).toBeNull();
    // 見出しの行は開閉の見出しが持つので、一覧の中に重ねない。
    expect(timeline.textContent).not.toContain("合計");
    expect(host.querySelectorAll("svg.animate-spin")).toHaveLength(0);
    expect(q("p-current")).toBeNull();
    // スキップは文字でも出す（色だけに頼らない）。
    expect(q("p-step-c")!.textContent).toContain("スキップ");
    // 完了後は読み上げの文を空にする（回答は会話の欄の log が知らせる）。
    expect(host.querySelector('[role="status"]')!.textContent).toBe("");
  });

  it("実行中は開き、完了したら閉じ、利用者が開いたら保ち、次の実行で開き直す（3 製品で同じ。#2118 / #2265）", () => {
    const doneSteps: ChatProgressStep[] = runningSteps.map((step) => ({
      ...step,
      status: "done",
      startedAt: step.startedAt ?? at(2),
      finishedAt: at(5),
    }));
    render(<ChatProgress steps={runningSteps} testId="p" />);
    expect(host.querySelector("details")!.open).toBe(true);
    // 実行中の完了した段の一覧には時間の線を出さない（全体の長さが伸び続ける）。
    expect(q("p-timeline")!.dataset.runTimelineBars).toBe("false");
    expect(host.querySelector("[data-run-timeline-bar]")).toBeNull();
    render(<ChatProgress steps={doneSteps} testId="p" />);
    expect(q("p")!.dataset.chatProgressState).toBe("done");
    expect(host.querySelector("details")!.open).toBe(false);
    act(() => q("p-summary")!.click());
    expect(host.querySelector("details")!.open).toBe(true);
    expect(host.querySelectorAll("[data-run-timeline-bar] span")).toHaveLength(4);
    render(<ChatProgress steps={doneSteps} elapsedMs={5000} testId="p" />);
    expect(host.querySelector("details")!.open).toBe(true);
    render(<ChatProgress steps={runningSteps} testId="p" />);
    expect(host.querySelector("details")!.open).toBe(true);
    // 完了した状態で出したとき（会話の履歴から開いた往復）は閉じて出し、失敗は開いたまま出す。
    act(() => root.unmount());
    root = createRoot(host);
    render(<ChatProgress steps={doneSteps} testId="p" />);
    expect(host.querySelector("details")!.open).toBe(false);
    render(
      <ChatProgress
        steps={[...doneSteps, { id: "x", label: "失敗", status: "failed", startedAt: at(5), finishedAt: at(6) }]}
        testId="p"
      />
    );
    expect(host.querySelector("details")!.open).toBe(true);
  });

  it("footer は開いた中身の末尾（タイムラインの後ろ）に出し、一覧と一緒に開閉する（#2325）", () => {
    const footer = <p data-testid="p-footer">処理の詳細</p>;
    // 実行中: 今の段階の行（最後の行）の後ろ。
    render(<ChatProgress steps={runningSteps} footer={footer} testId="p" />);
    const runningDetails = host.querySelector("details")!;
    expect(runningDetails.open).toBe(true);
    expect(runningDetails.contains(q("p-footer"))).toBe(true);
    expect(q("p-current")!.compareDocumentPosition(q("p-footer")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // 完了後: 閉じた 1 行の中（開くと一覧の後ろに出る）。開閉の見出しの外には出さない。
    act(() => root.unmount());
    root = createRoot(host);
    const doneSteps: ChatProgressStep[] = [
      { id: "a", label: "受け付けました", status: "done", startedAt: at(0), finishedAt: at(1) },
    ];
    render(<ChatProgress steps={doneSteps} footer={footer} testId="p" />);
    const details = host.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.contains(q("p-footer"))).toBe(true);
    act(() => q("p-summary")!.click());
    expect(details.open).toBe(true);
    expect(q("p-timeline")!.compareDocumentPosition(q("p-footer")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // 段階が無いときは footer も出さない（製品が単独で出す）。
    expect(renderToStaticMarkup(<ChatProgress steps={[]} footer={footer} />)).toBe("");
  });

  it("詳細のある段階だけに開閉のボタンを付け、完了後に一覧を開いても詳細は閉じたまま（#2294）", () => {
    render(
      <ChatProgress
        steps={[
          { id: "a", label: "準備しました", status: "done", startedAt: at(0), finishedAt: at(1) },
          {
            id: "b",
            label: "SQL を生成しました",
            status: "done",
            startedAt: at(1),
            finishedAt: at(3),
            details: { code: [{ label: "生成した SQL", language: "sql", text: "SELECT 1 FROM DUAL" }] },
          },
        ]}
        labels={{ detailsTruncated: "省略あり" }}
        testId="p"
      />
    );
    act(() => q("p-summary")!.click());
    expect(q("p-step-a-toggle")).toBeNull();
    const toggle = q("p-step-b-toggle")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(q("p-step-b-details")!.hidden).toBe(true);
    act(() => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(q("p-step-b-details")!.querySelector("pre")?.textContent).toBe("SELECT 1 FROM DUAL");
  });

  it("実行中の段階の一覧でも、届いた詳細で開閉のボタンが付く（同じ状態の詳細の更新を捨てない）", () => {
    const base: ChatProgressStep[] = [
      { id: "a", label: "検索しました", status: "done", startedAt: at(0), finishedAt: at(1) },
      { id: "b", label: "回答しています", status: "running", startedAt: at(1) },
    ];
    render(<ChatProgress steps={base} progressKey="k" testId="p" />);
    expect(q("p-step-a-toggle")).toBeNull();
    render(
      <ChatProgress
        steps={[{ ...base[0], details: { lines: ["検索文: 2 本"] } }, base[1]]}
        progressKey="k"
        testId="p"
      />
    );
    expect(q("p-step-a-toggle")).not.toBeNull();
  });

  it("失敗した段階があれば開いた状態で出し、失敗を文字とアイコンで示す", () => {
    render(
      <ChatProgress
        steps={[
          { id: "a", label: "受け付けました", status: "done", startedAt: at(0), finishedAt: at(1) },
          { id: "b", label: "SQL を生成できませんでした", status: "failed", startedAt: at(1), finishedAt: at(4) },
          { id: "c", label: "安全性の確認", status: "pending" },
        ]}
        testId="p"
      />
    );
    expect(q("p")!.dataset.chatProgressState).toBe("failed");
    expect(host.querySelector("details")!.open).toBe(true);
    const failed = q("p-step-b")!;
    expect(failed.dataset.status).toBe("failed");
    expect(failed.textContent).toContain("失敗");
    expect(failed.textContent).toContain("3.0 秒");
    expect(q("p-summary")!.textContent).toBe("処理の経過（2 ステップ・4.0 秒）");
  });

  it("入れ子の工程は字下げし、計測値は名前の後ろの括弧、右の列に所要時間（durationMs）を出す。今の段階はいちばん内側（#2096 / #2265）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(5)));
    const nested: ChatProgressStep[] = [
      { id: "understand", label: "質問の理解", status: "done", durationMs: 120, meta: "LLM 1 回" },
      { id: "flow", label: "根拠の検索と回答の作成", status: "running", startedAt: at(1) },
      { id: "search", label: "文書の検索", status: "done", durationMs: 1_600, level: 1 },
      { id: "rerank", label: "並べ替え", status: "running", startedAt: at(4), level: 1 },
    ];
    render(<ChatProgress steps={nested} testId="p" />);
    expect(q("p-current")!.dataset.stepId).toBe("rerank");
    expect(host.querySelectorAll("svg.animate-spin")).toHaveLength(1);
    const search = q("p-step-search")!;
    expect(search.dataset.level).toBe("1");
    expect(search.querySelector<HTMLElement>("[data-run-timeline-label]")?.closest("div")?.style.paddingInlineStart).toBe("1rem");
    expect(search.textContent).toContain("1.6 秒");
    expect(q("p-step-understand")!.textContent).toContain("質問の理解");
    expect(q("p-step-understand")!.querySelector("[data-run-result-summary]")?.textContent).toBe("LLM 1 回");
    expect(q("p-step-understand")!.textContent).toContain("0.1 秒");
    expect(q("p-step-understand")!.dataset.level).toBeUndefined();
    // 時刻の無い記録の全体の所要時間は、最上位の段階の和。
    render(
      <ChatProgress
        steps={[
          { id: "a", label: "a", status: "done", durationMs: 1_000 },
          { id: "b", label: "b", status: "done", durationMs: 2_000 },
          { id: "c", label: "c", status: "done", durationMs: 1_500, level: 1 },
        ]}
        progressKey="saved"
        testId="p"
      />
    );
    expect(q("p-summary")!.textContent).toBe("処理の経過（3 ステップ・3.0 秒）");
    // 時刻の無い記録の線は所要時間を積み、入れ子の段は親の段の始まりから積む（子の線が親の線の中に入る）。
    act(() => q("p-summary")!.click());
    const lefts = [...host.querySelectorAll<HTMLElement>("[data-run-timeline-bar] span")].map((bar) => bar.style.left);
    expect(lefts).toEqual(["0%", `${(1000 / 3000) * 100}%`, `${(1000 / 3000) * 100}%`]);
  });

  it("実行中の段階は始まった時点で開始順の位置に出し、親の工程が実行中・完了しても行の順は変わらない（#2840）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(10)));
    // 開始順の連番の id（RAG の段階の記録と同じ）。親（s2）の中で子（s3・s4）が順に動く。
    const order = () =>
      [...host.querySelectorAll<HTMLElement>("[data-run-timeline-step], [data-testid='p-current']")].map(
        (row) => row.dataset.runTimelineStep ?? `current:${row.dataset.stepId}`
      );
    const top = { id: "s1", label: "質問の読み取り", status: "done", startedAt: at(0), finishedAt: at(1) } as const;
    // 1. 子の工程 s3 が今の段階。親 s2 は開始順の位置（子の上）に回さない実行中の印で出る。
    render(
      <ChatProgress
        steps={[
          top,
          { id: "s2", label: "文書の検索", status: "running", startedAt: at(1), level: 1 },
          { id: "s3", label: "候補の選択", status: "running", startedAt: at(2), level: 2 },
        ]}
        progressKey="r"
        testId="p"
      />
    );
    expect(order()).toEqual(["s1", "s2", "current:s3"]);
    const parent = q("p-step-s2")!;
    expect(parent.dataset.status).toBe("running");
    expect(parent.querySelector("svg.animate-spin")).toBeNull();
    expect(parent.textContent).toContain("処理中");
    expect(host.querySelectorAll("svg.animate-spin")).toHaveLength(1);
    // 2. s3 が終わり、次の子 s4 が今の段階。s3 は同じ位置で完了の行に変わる。
    render(
      <ChatProgress
        steps={[
          top,
          { id: "s2", label: "文書の検索", status: "running", startedAt: at(1), level: 1 },
          { id: "s3", label: "候補の選択", status: "done", startedAt: at(2), finishedAt: at(5), level: 2 },
          { id: "s4", label: "並べ替え", status: "running", startedAt: at(5), level: 2 },
        ]}
        progressKey="r"
        testId="p"
      />
    );
    expect(order()).toEqual(["s1", "s2", "s3", "current:s4"]);
    // 3. 子と親が終わり、親の後の工程 s5 が今の段階。親の行は子の上のまま（後から差し込まれない）。
    render(
      <ChatProgress
        steps={[
          top,
          { id: "s2", label: "文書の検索", status: "done", startedAt: at(1), finishedAt: at(7), level: 1 },
          { id: "s3", label: "候補の選択", status: "done", startedAt: at(2), finishedAt: at(5), level: 2 },
          { id: "s4", label: "並べ替え", status: "done", startedAt: at(5), finishedAt: at(7), level: 2 },
          { id: "s5", label: "章節の追加", status: "running", startedAt: at(7), level: 1 },
        ]}
        progressKey="r"
        testId="p"
      />
    );
    expect(order()).toEqual(["s1", "s2", "s3", "s4", "current:s5"]);
    expect(q("p-step-s2")!.dataset.status).toBe("done");
    expect(q("p-running")!.textContent).toContain("処理中（4 ステップ完了）");
  });

  it("子が終わって次の子がまだ無い親の工程は回さず、親の 1 段下に「処理を続けています」を回す（#3436）", () => {
    render(
      <ChatProgress
        steps={[
          { id: "s1", label: "文書の検索", status: "running", startedAt: at(0) },
          { id: "s2", label: "候補の選択", status: "done", startedAt: at(1), finishedAt: at(2), level: 1 },
          { id: "s3", label: "待機中の工程", status: "pending" },
        ]}
        testId="p"
      />
    );
    const rows = [...host.querySelectorAll<HTMLElement>("[data-run-timeline-step], [data-testid='p-current']")];
    expect(rows.map((row) => row.dataset.runTimelineStep ?? `current:${row.dataset.stepId}`)).toEqual([
      "s1",
      "s2",
      "current:",
    ]);
    // 親の工程は回さない実行中の印（CirclePlay）。回るのは親の下の今の行だけ。
    const parent = q("p-step-s1")!;
    expect(parent.dataset.status).toBe("running");
    expect(parent.querySelector("svg.animate-spin")).toBeNull();
    expect(parent.querySelector("svg.lucide-circle-play")).not.toBeNull();
    expect(host.querySelectorAll("svg.animate-spin")).toHaveLength(1);
    const current = q("p-current")!;
    expect(current.querySelector("svg.animate-spin")).not.toBeNull();
    expect(current.textContent).toContain("処理を続けています");
    // 親の 1 段下に字下げする。
    expect(current.querySelector<HTMLElement>("[data-run-timeline-label]")?.closest("div")?.style.paddingInlineStart).toBe("1rem");
    // 待機中の段階は、親の工程が実行中の間は今の段階の代わりにしない。
    expect(q("p-step-s3")).toBeNull();
    // 閉じたときの見出しは親の工程の名前。
    act(() => q("p-running")!.click());
    expect(q("p-running")!.textContent).toContain("文書の検索");
  });

  it("active と elapsedMs と labels で上書きできる", () => {
    render(
      <ChatProgress
        active={false}
        elapsedMs={61_000}
        steps={[{ id: "a", label: "x", status: "running", startedAt: at(0) }]}
        labels={{ summary: (count, duration) => `Steps ${count} / ${duration}` }}
        testId="p"
      />
    );
    expect(q("p")!.dataset.chatProgressState).toBe("done");
    expect(q("p-summary")!.textContent).toBe("Steps 0 / 1 分 1 秒");
  });
});

describe("ChatProgress の段階の一覧の安定（#1358）", () => {
  const completedIds = () =>
    Array.from(host.querySelectorAll<HTMLElement>("li[data-status]")).map((item) =>
      item.dataset.testid?.replace("p-step-", "")
    );
  const snapshot = (statuses: Record<string, ChatProgressStep["status"]>): ChatProgressStep[] =>
    Object.entries(statuses).map(([id, status]) => ({ id, label: id, status }));

  it("処理中は、完了した段階が実行中に戻った一覧を受け取っても、完了の一覧から消さない", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(at(5)));
    render(<ChatProgress steps={snapshot({ step1: "done", step2: "done", step3: "running", step4: "pending" })} testId="p" />);
    expect(completedIds()).toEqual(["step1", "step2"]);
    // 段階が実行中に戻る（RAG の検索 → 並べ替え → 根拠確認）・古い snapshot が届いた。
    render(<ChatProgress steps={snapshot({ step1: "done", step2: "running", step3: "done", step4: "pending" })} testId="p" />);
    expect(completedIds()).toEqual(["step1", "step2", "step3"]);
    expect(q("p-current")!.dataset.stepId).toBe("step4");
    render(<ChatProgress steps={snapshot({ step1: "done", step2: "done", step3: "done", step4: "running" })} testId="p" />);
    expect(completedIds()).toEqual(["step1", "step2", "step3"]);
    expect(q("p-current")!.dataset.stepId).toBe("step4");
  });

  it("progressKey が変わったら（対象の切り替え）渡された一覧で作り直す", () => {
    render(<ChatProgress steps={snapshot({ submit: "running" })} progressKey="submit" testId="p" />);
    expect(q("p-current")!.dataset.stepId).toBe("submit");
    render(<ChatProgress steps={snapshot({ plan: "running" })} progressKey="run-1" testId="p" />);
    expect(q("p-current")!.dataset.stepId).toBe("plan");
    expect(q("p-running")!.textContent).not.toContain("ステップ完了");
  });

  it("終端の一覧は渡されたとおりに確定する", () => {
    render(<ChatProgress steps={snapshot({ a: "done", b: "running" })} testId="p" />);
    render(<ChatProgress steps={snapshot({ a: "done", c: "done" })} testId="p" />);
    expect(q("p")!.dataset.chatProgressState).toBe("done");
    expect(q("p-summary")!.textContent).toContain("2 ステップ");
  });
});
