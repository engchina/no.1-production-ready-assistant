import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { MoreHorizontal } from "lucide-react";

import { restoreMenuTriggerFocus } from "../../lib/menu-focus";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { DisclosureChevron } from "../ui/disclosure-chevron";
import { contextualMenuLabel, FloatingActionMenu } from "../ui/floating-menu";
import { splitObjectActions, visibleEntityActions, type EntityAction } from "./object-actions-core";

export {
  splitObjectActions,
  visibleEntityActions,
  type EntityAction,
  type EntityActionTone,
} from "./object-actions-core";

function useActionMenu() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;

    const closeOnOutsideClick = (event: globalThis.MouseEvent) => {
      const target = event.target as Node;
      if (containerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeOnOutsideClick);
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const firstEnabled = menuRef.current?.querySelector<HTMLButtonElement>(
      '[role="menuitem"]:not(:disabled)'
    );
    firstEnabled?.focus({ preventScroll: true });
  }, [open]);

  const close = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) restoreMenuTriggerFocus(triggerRef, menuRef);
  };

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close(true);
      return;
    }

    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? []
    );
    if (items.length === 0) return;

    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length;
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex === null) return;

    event.preventDefault();
    items[nextIndex]?.focus({ preventScroll: true });
  };

  return { open, setOpen, close, containerRef, triggerRef, menuRef, handleMenuKeyDown };
}

function MenuItems({
  actions,
  onClose,
  onActionClick,
}: {
  actions: readonly EntityAction[];
  onClose: (restoreFocus?: boolean) => void;
  onActionClick?: (event: MouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <>
      {actions.map((action, index) => {
        const Icon = action.icon;
        const danger = action.tone === "danger";
        return (
          <div
            key={action.id}
            role="none"
            className={cn(
              danger &&
                (index === 0 || actions[index - 1]?.tone !== "danger") &&
                "mt-1 border-t border-border pt-1"
            )}
          >
            <Button
              type="button"
              role="menuitem"
              tone={danger ? "danger" : "default"}
              size="sm"
              variant="ghost"
              icon={Icon}
              loading={action.loading}
              disabled={action.disabled}
              aria-label={action.ariaLabel}
              data-testid={action.testId}
              data-entity-action-id={action.id}
              data-entity-action-tone={action.tone ?? "default"}
              className="w-full justify-start text-left"
              onClick={(event) => {
                event.stopPropagation();
                onActionClick?.(event);
                onClose(false);
                void action.onSelect();
              }}
            >
              <span>{action.label}</span>
            </Button>
          </div>
        );
      })}
    </>
  );
}

export function RowActionMenu({
  actions,
  ariaLabel,
  disabled = false,
  loading = false,
  testId,
}: {
  actions: readonly EntityAction[];
  ariaLabel: string;
  disabled?: boolean;
  loading?: boolean;
  testId?: string;
}) {
  const visible = useMemo(() => visibleEntityActions(actions), [actions]);
  const menuId = useId();
  const { open, setOpen, close, containerRef, triggerRef, menuRef, handleMenuKeyDown } = useActionMenu();

  if (visible.length === 0) return null;

  return (
    <div
      ref={containerRef}
      className="relative inline-flex"
      role="group"
      aria-label={ariaLabel}
      data-testid={testId}
      onClick={(event) => event.stopPropagation()}
    >
      <Button
        ref={triggerRef}
        type="button"
        size="sm"
        variant="ghost"
        iconOnly
        loading={loading}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={menuId}
        aria-haspopup="menu"
        data-testid={testId ? `${testId}-trigger` : undefined}
        onClick={(event) => {
          event.stopPropagation();
          setOpen((current) => !current);
        }} icon={MoreHorizontal}>
        </Button>
      {open ? (
        <FloatingActionMenu
          id={menuId}
          open={open}
          triggerRef={triggerRef}
          menuRef={menuRef}
          className="min-w-44"
          onKeyDown={handleMenuKeyDown}
        >
          <MenuItems actions={visible} onClose={close} />
        </FloatingActionMenu>
      ) : null}
    </div>
  );
}

export function ObjectActionBar({
  actions,
  ariaLabel,
  moreLabel = "その他の操作",
  testId,
}: {
  actions: readonly EntityAction[];
  ariaLabel: string;
  /** overflow メニューを開くボタンの文言（製品の i18n で上書きできる）。 */
  moreLabel?: string;
  testId?: string;
}) {
  const { inline, overflow } = useMemo(() => splitObjectActions(actions), [actions]);
  const menuId = useId();
  const { open, setOpen, close, containerRef, triggerRef, menuRef, handleMenuKeyDown } = useActionMenu();

  if (inline.length === 0 && overflow.length === 0) return null;

  return (
    <div
      ref={containerRef}
      className="relative flex flex-col gap-[8px] sm:flex-row sm:flex-wrap xl:justify-end"
      role="group"
      aria-label={ariaLabel}
      data-testid={testId}
    >
      {inline.map((action) => {
        const Icon = action.icon;
        return (
          <Button
            key={action.id}
            type="button"
            variant="secondary"
            size="sm"
            icon={Icon}
              loading={action.loading}
            disabled={action.disabled}
            aria-label={action.ariaLabel}
            data-testid={action.testId}
            data-entity-action-id={action.id}
            data-entity-action-tone={action.tone ?? "default"}
            onClick={() => void action.onSelect()}
          >
            <span>{action.label}</span>
          </Button>
        );
      })}
      {overflow.length > 0 ? (
        <>
          <Button
            ref={triggerRef}
            type="button"
            variant="secondary"
            size="sm"
            aria-expanded={open}
            aria-controls={menuId}
            aria-haspopup="menu"
            // 読み上げ名に対象（ariaLabel）を足し、ページの「その他の操作」と区別する（#582）。
            aria-label={contextualMenuLabel(moreLabel, ariaLabel)}
            data-testid={testId ? `${testId}-more` : undefined}
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
              ariaLabel={contextualMenuLabel(moreLabel, ariaLabel)}
              className="min-w-52"
              onKeyDown={handleMenuKeyDown}
            >
              <MenuItems actions={overflow} onClose={close} />
            </FloatingActionMenu>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
