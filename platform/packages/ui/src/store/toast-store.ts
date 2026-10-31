import { create } from "zustand";

import type { FeedbackTone } from "../components/ui/feedback-tone";

/**
 * Toast（一時通知）ストア。
 * コンポーネント外からも `toast.success(...)` 等で呼べるよう Zustand store + 純関数 API で提供する。
 */

export interface ToastAction {
  /** 表示ラベル（i18n 済み文字列）。 */
  label: string;
  onClick: () => void;
}

export interface ToastOptions {
  /** 補足説明（i18n 済み文字列）。 */
  description?: string;
  /** 「元に戻す」等のアクション（undo-support）。 */
  action?: ToastAction;
  /** 自動消滅までの ms。0 で自動消滅しない。未指定はトーン既定値。 */
  duration?: number;
}

export interface ToastItem extends ToastOptions {
  id: string;
  tone: FeedbackTone;
  message: string;
}

interface ToastStore {
  toasts: ToastItem[];
  /** 自動消滅のタイマーを止めているか（通知の領域にホバー・フォーカスがある間）。 */
  paused: boolean;
  push: (item: Omit<ToastItem, "id">) => string;
  dismiss: (id: string) => void;
  clear: () => void;
  /** すべての自動消滅を止める。残り時間は保持する。 */
  pause: () => void;
  /** 止めていた自動消滅を、残り時間から再開する。 */
  resume: () => void;
}

/**
 * トーン別の既定表示時間（ms）。UX 契約 messaging.md §3.1 に従う。
 * success / info / warning は 4 秒（toast-dismiss: 3–5s）。danger は読む時間を見て 8 秒（#2470。
 * 閉じるまで残すと、画面を移っても古いエラーが残り続けた）。ホバー・フォーカス中は止まる。
 */
const DEFAULT_DURATION: Record<FeedbackTone, number> = {
  success: 4000,
  info: 4000,
  warning: 4000,
  danger: 8000,
};

/** 自動消滅のタイマー。`handle` が無い間は止まっている（`remaining` が残り時間）。 */
interface ToastTimer {
  remaining: number;
  startedAt: number;
  handle?: ReturnType<typeof setTimeout>;
}

const timers = new Map<string, ToastTimer>();

function startTimer(id: string, timer: ToastTimer) {
  timer.startedAt = Date.now();
  timer.handle = setTimeout(() => {
    useToastStore.getState().dismiss(id);
  }, timer.remaining);
}

function stopTimer(timer: ToastTimer) {
  if (!timer.handle) return;
  clearTimeout(timer.handle);
  timer.handle = undefined;
  timer.remaining = Math.max(0, timer.remaining - (Date.now() - timer.startedAt));
}

function clearTimer(id: string) {
  const timer = timers.get(id);
  if (timer?.handle) clearTimeout(timer.handle);
  timers.delete(id);
}

function nextId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `toast-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export const useToastStore = create<ToastStore>((set, get) => ({
  toasts: [],
  paused: false,
  push: (item) => {
    const id = nextId();
    set((state) => ({ toasts: [...state.toasts, { ...item, id }] }));

    // action 付き、または duration 明示 0 のときは自動消滅させない。
    const duration =
      item.duration ?? (item.action ? 0 : DEFAULT_DURATION[item.tone]);
    if (duration > 0) {
      const timer: ToastTimer = { remaining: duration, startedAt: Date.now() };
      timers.set(id, timer);
      // 止めている間に届いた通知は、再開したときから数え始める。
      if (!get().paused) startTimer(id, timer);
    }
    return id;
  },
  dismiss: (id) => {
    clearTimer(id);
    set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) }));
  },
  clear: () => {
    timers.forEach((timer) => {
      if (timer.handle) clearTimeout(timer.handle);
    });
    timers.clear();
    set({ toasts: [] });
  },
  pause: () => {
    if (get().paused) return;
    timers.forEach(stopTimer);
    set({ paused: true });
  },
  resume: () => {
    if (!get().paused) return;
    timers.forEach((timer, id) => {
      if (!timer.handle) startTimer(id, timer);
    });
    set({ paused: false });
  },
}));

function show(tone: FeedbackTone, message: string, options?: ToastOptions): string {
  return useToastStore.getState().push({ tone, message, ...options });
}

/**
 * 一時通知 API。message / description / action.label には **i18n 済み文字列**を渡す。
 * 生のリテラル直書きは避ける（アプリ側の i18n 経由の文字列を渡す）。
 */
export const toast = {
  success: (message: string, options?: ToastOptions) => show("success", message, options),
  info: (message: string, options?: ToastOptions) => show("info", message, options),
  warning: (message: string, options?: ToastOptions) => show("warning", message, options),
  /** 失敗通知（danger トーン）。 */
  error: (message: string, options?: ToastOptions) => show("danger", message, options),
  dismiss: (id: string) => useToastStore.getState().dismiss(id),
};
