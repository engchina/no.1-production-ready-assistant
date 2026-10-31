// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  PageLoadingState,
  ProcessingIndicator,
  TimedLoadingState,
  elapsedMsBetween,
  formatElapsedClock,
  operationTimestampMs,
} from "../src";

describe("operation timing", () => {
  it("1 時間未満は mm:ss、以上は h:mm:ss に固定桁で整形する", () => {
    expect(formatElapsedClock(59_000)).toBe("00:59");
    expect(formatElapsedClock(60_000)).toBe("01:00");
    expect(formatElapsedClock(3_723_000)).toBe("1:02:03");
    expect(formatElapsedClock(-1)).toBe("00:00");
  });

  it("サーバーの時刻から経過時間を求め、読めない時刻は null にする", () => {
    expect(operationTimestampMs("invalid")).toBeNull();
    expect(operationTimestampMs(Number.NaN)).toBeNull();
    expect(elapsedMsBetween("2026-07-29T00:00:00.000Z", "2026-07-29T00:00:01.250Z")).toBe(1250);
    expect(elapsedMsBetween(null, null)).toBeNull();
  });
});

describe("ProcessingIndicator", () => {
  it("実行中は既定の日本語の見出しで経過時間を出し、timer を読み上げない", () => {
    const html = renderToStaticMarkup(<ProcessingIndicator active label="取得しています" testId="p" />);
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('data-processing-placement="panel"');
    expect(html).toContain('role="timer"');
    expect(html).toContain('aria-live="off"');
    expect(html).toContain("経過時間");
    expect(html).toContain("取得しています");
  });

  it("完了後は所要時間と完了の文言を出し、labels で文言を差し替えられる", () => {
    const html = renderToStaticMarkup(
      <ProcessingIndicator
        active={false}
        label="取得しています"
        elapsedMs={65_000}
        labels={{ duration: "Duration", completed: "Done" }}
      />
    );
    expect(html).toContain("Duration");
    expect(html).toContain("Done");
    expect(html).toContain("01:05");
    expect(html).not.toContain('aria-busy="true"');
  });

  it("遅延の案内の行は最初から高さを予約し、文言は遅延してから中身に出す（#902）", () => {
    const before = renderToStaticMarkup(<ProcessingIndicator active label="x" testId="p" />);
    expect(before).toContain('data-processing-slow="false"');
    // 予約の行は status にしない（処理中の見出しの status だけ。getByRole("status") が 1 つに決まる）。
    expect(before.match(/role="status"/g)).toHaveLength(1);
    // 予約の文言は ::before の content（属性）だけにあり、textContent・読み上げには入らない。
    expect(before).toContain('data-placeholder="通常より時間がかかっています。"');
    expect(before.replace(/data-placeholder="[^"]*"/g, "")).not.toContain("通常より時間がかかっています。");
    expect(before).not.toContain('data-testid="p-slow"');

    const startedAt = new Date(Date.now() - 11_000).toISOString();
    const slow = renderToStaticMarkup(<ProcessingIndicator active label="x" testId="p" startedAt={startedAt} />);
    expect(slow).toContain('data-processing-slow="true"');
    expect(slow).toContain('data-testid="p-slow"');
    expect(slow.match(/role="status"/g)).toHaveLength(2);
    expect(slow).toContain(">通常より時間がかかっています。<");
  });

  it("遅延の案内は処理の名前の直下に出し、経過時間の列には置かない（#2426）", () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    const startedAt = new Date(Date.now() - 11_000).toISOString();
    act(() => root.render(<ProcessingIndicator active label="取得しています" testId="p" startedAt={startedAt} />));
    const slow = host.querySelector('[data-testid="p-slow"]')!;
    const timer = host.querySelector('[data-testid="p-timer"]')!;
    expect(slow.textContent).toBe("通常より時間がかかっています。");
    // 処理の名前と同じ列（名前の次の行）。
    expect(slow.previousElementSibling?.textContent).toBe("取得しています");
    expect(timer.parentElement!.contains(slow)).toBe(false);
    // 一度出たら、同じ処理の間は時刻の取り直しで経過時間が閾値を下回っても消さない。
    act(() =>
      root.render(
        <ProcessingIndicator active label="取得しています" testId="p" startedAt={new Date().toISOString()} />
      )
    );
    expect(host.querySelector('[data-testid="p-slow"]')?.textContent).toBe("通常より時間がかかっています。");
    // 別の処理（operationKey が変わる）・処理の終わりでは忘れる。
    act(() =>
      root.render(
        <ProcessingIndicator
          active
          label="取得しています"
          testId="p"
          operationKey="next"
          startedAt={new Date().toISOString()}
        />
      )
    );
    expect(host.querySelector('[data-testid="p-slow"]')).toBeNull();
    expect(host.querySelector('[data-processing-slow="false"]')).not.toBeNull();
    act(() => root.unmount());
    host.remove();
  });

  it("showSlowMessage=false と完了後は遅延の案内の行を置かない", () => {
    expect(renderToStaticMarkup(<ProcessingIndicator active label="x" showSlowMessage={false} />)).not.toContain(
      "data-processing-slow"
    );
    expect(renderToStaticMarkup(<ProcessingIndicator active={false} label="x" />)).not.toContain(
      "data-processing-slow"
    );
  });

  it("activityIcon=none では動くスピナーを出さない", () => {
    const html = renderToStaticMarkup(<ProcessingIndicator active label="x" activityIcon="none" />);
    expect(html).toContain('data-processing-activity-icon="none"');
    expect(html).not.toContain("animate-spin");
  });
});

describe("TimedLoadingState", () => {
  it("result の配置では既定でスピナーを出さず、子要素で寸法を保つ", () => {
    const html = renderToStaticMarkup(
      <TimedLoadingState label="生成しています" placement="result" testId="t">
        <div data-skeleton="" />
      </TimedLoadingState>
    );
    expect(html).toContain('aria-label="生成しています"');
    expect(html).toContain('data-processing-activity-icon="none"');
    expect(html).toContain("data-skeleton");
  });

  it("panel の配置では既定でスピナーを出す", () => {
    const html = renderToStaticMarkup(<TimedLoadingState label="読み込んでいます" />);
    expect(html).toContain('data-processing-activity-icon="spinner"');
    expect(html).toContain("animate-spin");
  });
});

describe("PageLoadingState", () => {
  it("3 製品の画面と同じく全幅で出し、本文の最大幅で絞らない（#3311）", () => {
    const html = renderToStaticMarkup(<PageLoadingState label="読み込んでいます" testId="page-loading" />);
    expect(html).toContain("max-w-none");
    expect(html).not.toContain("max-w-[var(--content-max-width)]");
  });
});
