import { X } from "lucide-react";
import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

import { cn } from "../../lib/utils";
import { ListDetailPager, type ListDetailPagerProps } from "../app-shell/list-detail-layout";
import { focusableIn, navDrawerKeyAction } from "../app-shell/nav-drawer";

import { Button } from "./button";

/**
 * SideSheet（モーダルの side sheet。#664）。
 *
 * 画面の中の補助的な一覧・詳細（チャットの会話の履歴など）を、狭い画面で本文の上に重ねて出す。
 * 広い画面では製品が同じ中身を本文の横にインラインで置き、狭い画面だけこの部品で開く想定
 * （Material 3 の standard / modal side sheet の使い分け）。
 *
 * ナビのドロワー（`AppShell`、#367）と同じ型: 画面の端から滑り出し、scrim・`role="dialog"` + `aria-modal`、
 * 開いたら閉じるボタンへフォーカスし、Tab / Shift+Tab を中で回す。閉じ方は閉じるボタン・Escape・scrim のタップ。
 * 閉じたら開く前にフォーカスがあった要素（または `returnFocusRef`）へ戻す。
 * 閉じている間も描いたままにし（`inert`・`visibility: hidden`）、開閉を transform で動かす。
 * 中の SelectField の一覧・Tooltip は `aria-modal` の中へ Portal で描かれる。
 */
export interface SideSheetProps {
  open: boolean;
  /** 閉じる要求（閉じるボタン・Escape・scrim のタップ）。製品が `open` を false にする。 */
  onClose: () => void;
  /** シートの見出しとダイアログの名前（翻訳済み）。 */
  title: string;
  /** 閉じるボタンの名前（翻訳済み。Tooltip にも出る）。 */
  closeLabel: string;
  /** 出す側（既定は左）。 */
  side?: "left" | "right";
  /**
   * 幅（#1154 / #2281）。`default` は 22rem（画面幅 − 3.5rem まで。会話の履歴・絞り込みなどの短い一覧）。
   * `md` は 40rem（画面幅 − 3.5rem まで）で、一覧の 1 件の詳細（評価のケース・回答・コメントなど文章を読む内容）。
   * `wide` は表など横に広い内容を見る大きなパネルで、sm 以上は 64rem（画面幅 − 3.5rem まで）、sm 未満は全画面。
   */
  size?: "default" | "md" | "wide";
  /** シートの要素の id（開くボタンの `aria-controls` に渡す）。 */
  id?: string;
  /** 閉じたときにフォーカスを戻す先。省略時は開く前にフォーカスがあった要素。 */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** 見出しの行の右（閉じるボタンの左）に置く操作。 */
  headerActions?: ReactNode;
  /**
   * 見出しの行の「前へ / 次へ」（#2281。一覧の前後の項目の詳細へシートを開いたまま移る）。
   * 見出しの右・`headerActions` の左に置く。
   */
  pager?: ListDetailPagerProps;
  /**
   * 開いたときのフォーカスの先（#2360）。`first`（既定）は先頭の操作（閉じるボタン）、`dialog` はシートそのもの
   * （名前が読み上げられ、最初の Tab で先頭の操作へ移る）。読むための内容のシートは `dialog` にすると、キーボードで
   * 開いた直後の Escape 1 回で閉じられる（閉じるボタンにフォーカスすると、1 回目の Escape は Tooltip だけを閉じる）。
   */
  initialFocus?: "first" | "dialog";
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  "data-testid"?: string;
}

export function SideSheet({
  open,
  onClose,
  title,
  closeLabel,
  side = "left",
  size = "default",
  id,
  returnFocusRef,
  headerActions,
  pager,
  initialFocus = "first",
  children,
  className,
  bodyClassName,
  "data-testid": testId,
}: SideSheetProps) {
  const generatedId = useId();
  const sheetId = id ?? generatedId;
  const titleId = `${sheetId}-title`;
  const panelRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);

  // 開いたら閉じるボタン（先頭の操作）へ、閉じたら開く前の要素へフォーカスを移す。
  useEffect(() => {
    if (open && !wasOpen.current) {
      const active = typeof document === "undefined" ? null : document.activeElement;
      openerRef.current = active instanceof HTMLElement ? active : null;
      const panel = panelRef.current;
      if (panel) ((initialFocus === "dialog" ? null : focusableIn(panel)[0]) ?? panel).focus({ preventScroll: true });
    } else if (!open && wasOpen.current) {
      const target = returnFocusRef?.current ?? openerRef.current;
      if (target?.isConnected) target.focus({ preventScroll: true });
      openerRef.current = null;
    }
    wasOpen.current = open;
  }, [open, returnFocusRef, initialFocus]);

  const onPanelKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // 中の部品が自分で処理した Escape（名前の編集の取消・一覧を閉じるなど）では閉じない。
    if (event.defaultPrevented) return;
    const panel = panelRef.current;
    if (!panel) return;
    const items = focusableIn(panel);
    const action = navDrawerKeyAction(
      event.key,
      event.shiftKey,
      items.indexOf(document.activeElement as HTMLElement),
      items.length
    );
    if (!action) return;
    event.preventDefault();
    if (action.type === "close") onClose();
    else items[action.index]?.focus({ preventScroll: true });
  };

  if (typeof document === "undefined") return null;

  const left = side === "left";
  return createPortal(
    <>
      {/* scrim。タップで閉じる。読み上げとキーボードには出さない（閉じるボタンと Escape がある）。 */}
      <div
        aria-hidden
        data-testid={testId ? `${testId}-scrim` : undefined}
        className={cn(
          "fixed inset-0 z-[var(--z-scrim)] touch-none bg-[var(--scrim)] transition-opacity duration-200 ease-out motion-reduce:transition-none",
          open ? "opacity-100" : "pointer-events-none opacity-0"
        )}
        onClick={onClose}
      />
      <div
        ref={panelRef}
        id={sheetId}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        inert={open ? undefined : true}
        data-state={open ? "open" : "closed"}
        data-testid={testId}
        className={cn(
          "fixed inset-y-0 z-[var(--z-dialog)] flex flex-col bg-surface-overlay text-fg shadow-[var(--shadow-dialog)] outline-none",
          size === "wide"
            ? "w-full max-w-full sm:w-[64rem] sm:max-w-[calc(100vw-3.5rem)]"
            : size === "md"
              ? "w-[40rem] max-w-[calc(100vw-3.5rem)]"
              : "w-[22rem] max-w-[calc(100vw-3.5rem)]",
          left ? "left-0 border-r border-border" : "right-0 border-l border-border",
          "duration-200 ease-out motion-reduce:transition-none",
          // 開くときは visibility を即座に visible にする（遷移させると開いた瞬間にフォーカスを移せない）。
          // 閉じるときは slide が終わってから hidden にする。
          // 開いている間は translate を none にする（`translate-x-0` の `translate: 0 0` でも fixed の子の
          // 基準がシートになり、シートの中の SelectField の一覧・Tooltip がシートの左端の分だけずれて
          // 画面の外に出ていた。#810）。
          open
            ? "visible translate-none transition-transform"
            : cn("invisible transition-[transform,visibility]", left ? "-translate-x-full" : "translate-x-full"),
          className
        )}
        onKeyDown={onPanelKeyDown}
      >
        <div className="flex shrink-0 items-center gap-2 border-b border-border py-2 pl-4 pr-2">
          <h2 id={titleId} className="min-w-0 flex-1 truncate text-sm font-semibold text-fg">
            {title}
          </h2>
          {pager ? <ListDetailPager {...pager} /> : null}
          {headerActions}
          <Button type="button" variant="ghost" size="sm" iconOnly icon={X} aria-label={closeLabel} onClick={onClose} />
        </div>
        <div className={cn("flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain [scrollbar-gutter:stable] p-3", bodyClassName)}>
          {children}
        </div>
      </div>
    </>,
    document.body
  );
}
