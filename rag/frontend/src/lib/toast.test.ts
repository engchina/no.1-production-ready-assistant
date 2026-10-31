import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toast, useToastStore } from "./toast";

beforeEach(() => {
  vi.useFakeTimers();
  useToastStore.getState().clear();
});

afterEach(() => {
  useToastStore.getState().clear();
  vi.useRealTimers();
});

describe("toast store", () => {
  it("toast.success で success トーンの通知を積む", () => {
    toast.success("保存しました");
    const toasts = useToastStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].tone).toBe("success");
    expect(toasts[0].message).toBe("保存しました");
  });

  it("toast.error は danger トーンにマップされる", () => {
    toast.error("失敗しました");
    expect(useToastStore.getState().toasts[0].tone).toBe("danger");
  });

  it("既定時間が経過すると自動消滅する", () => {
    toast.success("ok");
    expect(useToastStore.getState().toasts).toHaveLength(1);
    vi.advanceTimersByTime(4000);
    expect(useToastStore.getState().toasts).toHaveLength(0);
  });

  it("action 付きは自動消滅しない", () => {
    toast.success("削除しました", { action: { label: "元に戻す", onClick: () => {} } });
    vi.advanceTimersByTime(60_000);
    expect(useToastStore.getState().toasts).toHaveLength(1);
  });

  it("duration 0 は自動消滅しない", () => {
    toast.info("常駐", { duration: 0 });
    vi.advanceTimersByTime(60_000);
    expect(useToastStore.getState().toasts).toHaveLength(1);
  });

  it("dismiss で指定 ID を取り除く", () => {
    const id = toast.warning("注意");
    expect(useToastStore.getState().toasts).toHaveLength(1);
    toast.dismiss(id);
    expect(useToastStore.getState().toasts).toHaveLength(0);
  });

  it("複数積んだ順序を保持する", () => {
    toast.info("1");
    toast.info("2");
    const messages = useToastStore.getState().toasts.map((item) => item.message);
    expect(messages).toEqual(["1", "2"]);
  });
});
