/**
 * フォームの操作行（#226。NL2SQL の components/FormActionBar.tsx → system-settings を経て packages/ui へ移設）。
 *
 * - primary（保存など）と secondary（キャンセルなど）は直置きし、danger（削除など）は
 *   「その他の操作」メニューへまとめる（赤いボタンを主操作の隣に直置きしない）。
 * - `status` は操作行の左側に出す状態（`FormStatus` など）。
 * - 一覧・詳細の `EntityAction` は `entityActionToFormAction` でそのまま渡せる。
 * - 詳細・作成・編集の画面の確定（「作成」「保存」と「変更を破棄」）も、フォームの末尾のこの行に置く（#1786）。
 *   行は画面の下端に貼り付けず、内容と一緒にスクロールする（#2715。README §4「詳細・作成・編集の画面の操作」）。
 */
import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { type LucideIcon } from "lucide-react";

import type { EntityAction } from "../data/object-actions-core";
import { restoreMenuTriggerFocus } from "../../lib/menu-focus";
import { cn } from "../../lib/utils";
import { Button, buttonVariants } from "./button";
import { DisclosureChevron } from "./disclosure-chevron";
import { contextualMenuLabel, FloatingActionMenu } from "./floating-menu";


export interface FormActionDescriptor {
  id: string;
  label: string;
  icon?: LucideIcon;
  onClick?: () => void | Promise<void>;
  href?: string;
  loading?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
  testId?: string;
  /**
   * 直置きのボタンの type（既定 "button"）。`<form>` の中で保存を "submit" にすると、
   * 入力欄での Enter による送信（暗黙の送信）と `onSubmit` の検証をそのまま使える。
   * `href` を持つ操作と「その他の操作」メニューの項目では使わない。
   */
  type?: "button" | "submit";
}

export interface FormActionBarProps {
  primaryActions?: readonly FormActionDescriptor[];
  secondaryActions?: readonly FormActionDescriptor[];
  dangerActions?: readonly FormActionDescriptor[];
  status?: ReactNode;
  ariaLabel: string;
  testId?: string;
  /** 破壊的操作をまとめるメニューの見出し（既定は「その他の操作」）。 */
  moreLabel?: string;
}

/** 一覧・詳細と同じ EntityAction をフォーム操作へ投影する。 */
export function entityActionToFormAction(action: EntityAction): FormActionDescriptor {
  return {
    id: action.id,
    label: action.label,
    icon: action.icon,
    onClick: action.onSelect,
    loading: action.loading,
    disabled: action.disabled,
    ariaLabel: action.ariaLabel,
    testId: action.testId,
  };
}

function actionEnabled(action: FormActionDescriptor) {
  return !action.disabled && !action.loading;
}

function ActionContent({ action, iconSize = 16 }: { action: FormActionDescriptor; iconSize?: number }) {
  const Icon = action.icon;
  return (
    <>
      {Icon ? <Icon size={iconSize} aria-hidden="true" /> : null}
      <span>{action.label}</span>
    </>
  );
}

function VisibleAction({
  action,
  variant,
}: {
  action: FormActionDescriptor;
  variant: "primary" | "secondary";
}) {
  const className = "w-full whitespace-nowrap sm:w-auto";

  if (action.href) {
    const enabled = actionEnabled(action);
    return (
      <a
        href={enabled ? action.href : undefined}
        aria-disabled={!enabled || undefined}
        aria-label={action.ariaLabel}
        data-testid={action.testId}
        data-form-action-id={action.id}
        data-form-action-kind={variant}
        className={cn(
          buttonVariants({ variant, size: "lg" }),
          className,
          !enabled && "pointer-events-none opacity-50"
        )}
        onClick={(event) => {
          if (!enabled) event.preventDefault();
        }}
      >
        <ActionContent action={action} />
      </a>
    );
  }

  return (
    <Button
      type={action.type ?? "button"}
      variant={variant}
      size="lg"
      className={className}
      icon={action.icon}
      loading={action.loading}
      disabled={action.disabled}
      aria-label={action.ariaLabel}
      data-testid={action.testId}
      data-form-action-id={action.id}
      data-form-action-kind={variant}
      // フォームの末尾の確定（#1786）: 押した瞬間（mousedown）に入力欄の blur の検証が走って欄の下にエラーが出ると、
      // 下にあるこの行が押している間に動き、クリックが外れて送信されない（375px で欄が縦に積まれるとき）。
      // mousedown ではフォーカスを動かさず、click でボタンへフォーカスを移してから操作する（blur の検証は click の後）。
      onMouseDown={(event: ReactMouseEvent<HTMLButtonElement>) => event.preventDefault()}
      onClick={(event: ReactMouseEvent<HTMLButtonElement>) => {
        event.currentTarget.focus({ preventScroll: true });
        void action.onClick?.();
      }}
    >
      <span>{action.label}</span>
    </Button>
  );
}

function DangerMenuItem({
  action,
  onInvoked,
}: {
  action: FormActionDescriptor;
  onInvoked: () => void;
}) {
  const className =
    "w-full";

  if (action.href) {
    const enabled = actionEnabled(action);
    return (
      <a
        href={enabled ? action.href : undefined}
        role="menuitem"
        aria-disabled={!enabled || undefined}
        aria-label={action.ariaLabel}
        data-testid={action.testId}
        data-form-action-id={action.id}
        data-form-action-tone="danger"
        className={cn(buttonVariants({ variant: "ghost", size: "sm", tone: "danger" }), className, !enabled && "pointer-events-none opacity-50")}
        onClick={(event) => {
          if (!enabled) {
            event.preventDefault();
            return;
          }
          onInvoked();
        }}
      >
        <ActionContent action={action} />
      </a>
    );
  }

  return (
    <Button
      type="button"
      role="menuitem"
      tone="danger"
      variant="ghost"
      size="sm"
      className={cn(className, "w-full justify-start text-left")}
      icon={action.icon}
      loading={action.loading}
      disabled={action.disabled}
      aria-label={action.ariaLabel}
      data-testid={action.testId}
      data-form-action-id={action.id}
      data-form-action-tone="danger"
      onClick={(event: ReactMouseEvent<HTMLButtonElement>) => {
        event.preventDefault();
        onInvoked();
        void action.onClick?.();
      }}
    >
      <span>{action.label}</span>
    </Button>
  );
}

function DangerActionsMenu({
  actions,
  moreLabel,
  context,
}: {
  actions: readonly FormActionDescriptor[];
  moreLabel: string;
  /** 操作のグループの読み上げ名。メニューのボタンの読み上げ名に足す（#582）。 */
  context: string;
}) {
  const accessibleName = contextualMenuLabel(moreLabel, context);
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;

    const closeOnOutsideClick = (event: MouseEvent) => {
      const target = event.target as Node;
      if (containerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeOnOutsideClick);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    window.requestAnimationFrame(() => {
      const firstEnabled = menuRef.current?.querySelector<HTMLElement>(
        '[role="menuitem"]:not(:disabled):not([aria-disabled="true"])'
      );
      firstEnabled?.focus({ preventScroll: true });
    });
  }, [open]);

  const closeMenu = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) restoreMenuTriggerFocus(triggerRef, menuRef);
  };

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
      return;
    }

    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>(
        '[role="menuitem"]:not(:disabled):not([aria-disabled="true"])'
      ) ?? []
    );
    if (items.length === 0) return;

    const currentIndex = items.indexOf(document.activeElement as HTMLElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length;
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex === null) return;

    event.preventDefault();
    items[nextIndex]?.focus({ preventScroll: true });
  };

  if (actions.length === 0) return null;

  return (
    <div ref={containerRef} className="relative flex w-full sm:w-auto sm:ml-auto">
      <Button
        ref={triggerRef}
        type="button"
        variant="secondary"
        size="lg"
        className="w-full whitespace-nowrap sm:w-auto"
        aria-expanded={open}
        aria-controls={menuId}
        aria-haspopup="menu"
        aria-label={accessibleName}
        data-testid="form-actions-more"
        onClick={() => setOpen((current) => !current)}
      >
        <span>{moreLabel}</span>
        <DisclosureChevron expanded={open} size={16} />
      </Button>
      {open ? (
        <FloatingActionMenu
          id={menuId}
          open={open}
          triggerRef={triggerRef}
          menuRef={menuRef}
          ariaLabel={accessibleName}
          className="min-w-56 max-w-[calc(100vw-1rem)]"
          onKeyDown={handleMenuKeyDown}
        >
          {actions.map((action, index) => (
            <div
              key={action.id}
              role="none"
              data-form-action-group-start={index === 0 ? "true" : undefined}
              className={cn(index === 0 && "border-t border-border pt-1")}
            >
              <DangerMenuItem action={action} onInvoked={() => closeMenu(false)} />
            </div>
          ))}
        </FloatingActionMenu>
      ) : null}
    </div>
  );
}

export function FormActionBar({
  ariaLabel,
  dangerActions = [],
  primaryActions = [],
  secondaryActions = [],
  status,
  testId,
  moreLabel = "その他の操作",
}: FormActionBarProps) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      data-testid={testId}
      // e2e がフォームの確定の行を見分ける目印（画面に貼り付かず、PageHeader に重ならないことを確かめる。#2715）。
      data-form-action-bar=""
      className="grid min-w-0 gap-[8px] border-t border-border pt-4"
    >
      <div className="flex min-w-0 flex-col gap-[8px] sm:flex-row sm:flex-wrap sm:items-center">
        {primaryActions.map((action) => (
          <VisibleAction key={action.id} action={action} variant="primary" />
        ))}
        {secondaryActions.map((action) => (
          <VisibleAction key={action.id} action={action} variant="secondary" />
        ))}
        {status ? <div className="min-w-0 sm:flex-1">{status}</div> : null}
        <DangerActionsMenu actions={dangerActions} moreLabel={moreLabel} context={ariaLabel} />
      </div>
    </div>
  );
}
