import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Banner, toast, useToastStore } from "../src";

const ids = () => useToastStore.getState().toasts.map((item) => item.id);

function reset() {
  const state = useToastStore.getState();
  state.clear();
  // 修正前の実装（resume が無い）でも既定時間のテストを評価できるようにする。
  state.resume?.();
}

beforeEach(() => {
  vi.useFakeTimers();
  reset();
});

afterEach(() => {
  reset();
  vi.useRealTimers();
});

describe("toast の既定の表示時間（messaging.md §3.1）", () => {
  it.each(["success", "info", "warning"] as const)("%s は 4 秒で消える", (tone) => {
    const id = toast[tone]("保存しました");
    vi.advanceTimersByTime(3999);
    expect(ids()).toEqual([id]);
    vi.advanceTimersByTime(1);
    expect(ids()).toEqual([]);
  });

  it("danger（toast.error）は 8 秒で消える（閉じるまで残さない。#2470）", () => {
    const id = toast.error("保存に失敗しました");
    vi.advanceTimersByTime(7999);
    expect(ids()).toEqual([id]);
    vi.advanceTimersByTime(1);
    expect(ids()).toEqual([]);
  });

  it("danger でも duration を明示すればその時間で消える", () => {
    toast.error("一時的なエラー", { duration: 2000 });
    vi.advanceTimersByTime(2000);
    expect(ids()).toEqual([]);
  });

  it("action 付きの通知は自動では消えない", () => {
    const id = toast.success("削除しました", { action: { label: "元に戻す", onClick: () => {} } });
    vi.advanceTimersByTime(60_000);
    expect(ids()).toEqual([id]);
  });
});

describe("toast の一時停止と再開", () => {
  it("止めている間は消えず、再開したら残り時間で消える", () => {
    const id = toast.success("保存しました");
    vi.advanceTimersByTime(3000);

    useToastStore.getState().pause();
    expect(useToastStore.getState().paused).toBe(true);
    vi.advanceTimersByTime(10_000);
    expect(ids()).toEqual([id]);

    useToastStore.getState().resume();
    expect(useToastStore.getState().paused).toBe(false);
    vi.advanceTimersByTime(999);
    expect(ids()).toEqual([id]);
    vi.advanceTimersByTime(1);
    expect(ids()).toEqual([]);
  });

  it("止めている間に届いた通知は、再開したときから数え始める", () => {
    useToastStore.getState().pause();
    const id = toast.info("取り込みが終わりました");
    vi.advanceTimersByTime(10_000);
    expect(ids()).toEqual([id]);

    useToastStore.getState().resume();
    vi.advanceTimersByTime(3999);
    expect(ids()).toEqual([id]);
    vi.advanceTimersByTime(1);
    expect(ids()).toEqual([]);
  });

  it("止めては再開を繰り返しても、表示していた時間の合計で消える", () => {
    toast.warning("接続が不安定です");
    for (let i = 0; i < 3; i += 1) {
      vi.advanceTimersByTime(1000);
      useToastStore.getState().pause();
      vi.advanceTimersByTime(5000);
      useToastStore.getState().resume();
    }
    expect(ids()).toHaveLength(1);
    vi.advanceTimersByTime(1000);
    expect(ids()).toEqual([]);
  });

  it("止めている間に閉じた通知は、再開しても復活しない", () => {
    const id = toast.success("保存しました");
    useToastStore.getState().pause();
    toast.dismiss(id);
    useToastStore.getState().resume();
    vi.advanceTimersByTime(10_000);
    expect(ids()).toEqual([]);
  });
});

describe("Banner の閉じるボタン", () => {
  it("共有 Button（ghost・アイコンのみ・44px）で描き、aria-label を付ける", () => {
    const html = renderToStaticMarkup(
      <Banner severity="info" onDismiss={() => {}} dismissLabel="お知らせを閉じる">
        本文
      </Banner>
    );
    const tag = html.match(/<button\b[^>]*>/)?.[0] ?? "";
    expect(tag).toContain('aria-label="お知らせを閉じる"');
    expect(tag).toContain('type="button"');
    // 共有 Button のクラス（フォーカスリング・アイコンのみ・タッチ高さ）
    expect(tag).toContain("focus-visible:outline-focus-ring");
    expect(tag).toContain("aspect-square");
    expect(tag).toContain("h-[var(--control-height-touch)]");
  });
});
