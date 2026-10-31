import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { cn } from "../../lib/utils";

import { Button } from "./button";
import { toneIcon, type FeedbackTone } from "./feedback-tone";
import { MessageText } from "./message-text";

/**
 * ConfirmDialog（確認ダイアログ）。
 * 破壊的・不可逆操作の確認ゲート。`useConfirm()` で Promise<boolean> を await する。
 * フォーカストラップ / Esc キャンセル / トリガーへフォーカス復帰に対応。
 * メニュー（`role="menu"`）の項目から開いた場合は、閉じたあとメニューのトリガーへ戻す（WAI-ARIA Menu Button）。
 *
 * 既定のボタン文言は日本語。アプリ側の i18n を使うときは <ConfirmProvider labels={...}> で注入する。
 */

export interface ConfirmOptions {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** confirm ボタンのトーン。破壊的操作は "danger"。 */
  tone?: Extract<FeedbackTone, "danger" | "warning" | "info">;
  /** オーバーレイクリックでキャンセルを許可（既定 true）。誤操作防止で false にできる。 */
  dismissOnOverlay?: boolean;
}

/** Provider レベルの既定文言（i18n 注入用）。 */
export interface ConfirmDefaultLabels {
  confirm: string;
  cancel: string;
}

const DEFAULT_LABELS: ConfirmDefaultLabels = { confirm: "実行", cancel: "キャンセル" };

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

/** 確認ダイアログを開いて結果を待つ。Provider 配下でのみ利用可能。 */
export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) {
    throw new Error("useConfirm は <ConfirmProvider> の配下で使用してください。");
  }
  return ctx;
}

interface DialogState {
  options: ConfirmOptions;
  resolve: (value: boolean) => void;
  returnFocus: HTMLElement | null;
}

/**
 * 閉じたあとのフォーカスの戻り先。`confirm()` の呼び出し時点で決める
 * （メニュー項目はダイアログが開く前に unmount されるため、マウント時の activeElement では body になる）。
 * メニューの中から開いた場合は、`aria-controls` でそのメニューを指すトリガーへ戻す。
 */
function confirmReturnFocus(active: Element | null): HTMLElement | null {
  if (!(active instanceof HTMLElement)) return null;
  const menu = active.closest<HTMLElement>('[role="menu"]');
  if (menu?.id) {
    const trigger = Array.from(document.querySelectorAll<HTMLElement>("[aria-controls]")).find(
      (element) => element.getAttribute("aria-controls") === menu.id
    );
    if (trigger) return trigger;
  }
  return active;
}

export function ConfirmProvider({
  children,
  labels = DEFAULT_LABELS,
  navigationKey,
}: {
  children: ReactNode;
  labels?: ConfirmDefaultLabels;
  /**
   * 画面遷移の識別子。値が変わると開いている確認をキャンセル（`false`）で閉じる。遷移先の画面に
   * 前の画面の確認が残らないようにする。React Router のアプリは `@production-ready/system-settings`
   * の `useConfirmNavigationKey()` を渡す（同じパスで URL を書き戻すだけの置き換えでは閉じない。#833）。
   */
  navigationKey?: unknown;
}) {
  const [state, setState] = useState<DialogState | null>(null);

  const confirm = useCallback<ConfirmFn>((options) => {
    return new Promise<boolean>((resolve) => {
      setState({ options, resolve, returnFocus: confirmReturnFocus(document.activeElement) });
    });
  }, []);

  const settle = useCallback((value: boolean) => {
    setState((current) => {
      current?.resolve(value);
      return null;
    });
  }, []);

  useEffect(() => {
    settle(false);
  }, [navigationKey, settle]);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {state ? (
        <ConfirmDialog
          options={state.options}
          returnFocus={state.returnFocus}
          labels={labels}
          onCancel={() => settle(false)}
          onConfirm={() => settle(true)}
        />
      ) : null}
    </ConfirmContext.Provider>
  );
}

function ConfirmDialog({
  options,
  returnFocus,
  labels,
  onCancel,
  onConfirm,
}: {
  options: ConfirmOptions;
  returnFocus: HTMLElement | null;
  labels: ConfirmDefaultLabels;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { title, description, tone = "danger", dismissOnOverlay = true } = options;
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const Icon = toneIcon[tone];

  // 開いたら確認ボタンへフォーカス、閉じたらトリガーへ復帰。ダイアログは fixed なので本文をスクロールさせない。
  useEffect(() => {
    confirmRef.current?.focus({ preventScroll: true });
    return () => returnFocus?.focus({ preventScroll: true });
  }, [returnFocus]);

  // Esc でキャンセル + 簡易フォーカストラップ。
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCancel();
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = panelRef.current.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus({ preventScroll: true });
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus({ preventScroll: true });
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  const confirmVariant = tone === "danger" ? "danger" : "primary";

  return createPortal(
    <div
      className="animate-overlay-in fixed inset-0 z-[var(--z-dialog)] flex items-center justify-center bg-[var(--scrim)] p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && dismissOnOverlay) onCancel();
      }}
    >
      <div
        ref={panelRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        className="animate-dialog-in max-h-[90dvh] w-full max-w-md overflow-auto [scrollbar-gutter:stable] rounded-xl border border-border bg-surface-overlay p-5 shadow-[var(--shadow-dialog)]"
      >
        <div className="flex items-start gap-3">
          <span
            data-surface-tint={tone}
            className={cn(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-full",
              tone === "danger" && "bg-danger-subtle text-danger-fg",
              tone === "warning" && "bg-warning-subtle text-warning-fg",
              tone === "info" && "bg-info-subtle text-info-fg"
            )}
          >
            <Icon size={20} aria-hidden />
          </span>
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-base font-semibold text-fg">
              <MessageText text={title} />
            </h2>
            {description ? (
              <p id={descriptionId} className="mt-1 text-sm leading-relaxed text-fg-muted">
                <MessageText text={description} />
              </p>
            ) : null}
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onCancel}>
            {options.cancelLabel ?? labels.cancel}
          </Button>
          <Button type="button" ref={confirmRef} variant={confirmVariant} size="sm" onClick={onConfirm}>
            {options.confirmLabel ?? labels.confirm}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  );
}
