import { Menu } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";

import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import {
  closesNavDrawer,
  DEFAULT_NAV_DRAWER_LABELS,
  focusableIn,
  navDrawerKeyAction,
  NavDrawerContext,
  useNavDrawerMode,
  type NavDrawerBrand,
  type NavDrawerLabels,
} from "./nav-drawer";

/**
 * アプリ全体のシェル。左に Sidebar、右にメイン領域（スクロール）を配置する。
 * Sidebar は `sidebar` スロットで注入する（ルーター/auth/i18n を持ち込まないため）。
 *
 * md 未満（767px 以下）ではサイドバーを隠し、上端のバーの「メニュー」ボタンで開くドロワーにする（#367）。
 * ドロワーはモーダル（Escape・scrim のタップ・ナビの選択で閉じ、閉じたらボタンへフォーカスを戻す。
 * 開いている間はフォーカスを閉じ込め、背面を inert にしてスクロールさせない）。md 以上は従来どおり。
 */
export function AppShell({
  sidebar,
  children,
  className,
  mainClassName,
  skipLinkLabel = "本文へスキップ",
  navDrawerLabels = DEFAULT_NAV_DRAWER_LABELS,
}: {
  sidebar: ReactNode;
  children: ReactNode;
  className?: string;
  mainClassName?: string;
  /** キーボード利用者が nav を飛ばして本文へ移るリンクのラベル（翻訳済み）。 */
  skipLinkLabel?: string;
  /** md 未満のドロワーの「メニュー」ボタン（とドロワーの名前）と、閉じるボタンのラベル（翻訳済み）。 */
  navDrawerLabels?: NavDrawerLabels;
}) {
  const drawer = useNavDrawerMode();
  const [open, setOpen] = useState(false);
  const [brand, setBrand] = useState<NavDrawerBrand | null>(null);
  const drawerId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const wasOpen = useRef(false);

  const closeDrawer = useCallback(() => setOpen(false), []);

  // md 以上に広げたらドロワーを閉じる（サイドバーは通常の位置に戻る）。
  useEffect(() => {
    if (!drawer) setOpen(false);
  }, [drawer]);

  // 開いたらドロワーの先頭（閉じるボタン）へ、閉じたら「メニュー」ボタンへフォーカスを移す。
  useEffect(() => {
    if (open) {
      const panel = panelRef.current;
      if (panel) (focusableIn(panel)[0] ?? panel).focus({ preventScroll: true });
    } else if (wasOpen.current && drawer) {
      triggerRef.current?.focus({ preventScroll: true });
    }
    wasOpen.current = open;
  }, [open, drawer]);

  const onPanelKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const panel = panelRef.current;
    if (!panel) return;
    const items = focusableIn(panel);
    const action = navDrawerKeyAction(event.key, event.shiftKey, items.indexOf(document.activeElement as HTMLElement), items.length);
    if (!action) return;
    event.preventDefault();
    if (action.type === "close") setOpen(false);
    else items[action.index]?.focus({ preventScroll: true });
  };

  // 本文へスキップ: URL に #pr-main を付けず（履歴を増やさず）、本文へフォーカスを移す。
  // 既定のページ内リンクの移動は履歴の項目を足し、ブラウザの popstate を React Router が POP の画面遷移として
  // 扱う（離脱ガードの blocker が「router の作っていない POP」の警告を出し、戻るで同じ画面に留まる）。
  // Enter でもリンクの click が届くので、キーボードも同じ動きになる。
  const onSkipLinkClick = (event: MouseEvent<HTMLAnchorElement>) => {
    // 新しいタブで開く等（修飾キー・中ボタン）はブラウザに任せる。
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const main = mainRef.current;
    if (!main) return;
    event.preventDefault();
    main.focus();
  };

  const context = useMemo(
    () => ({ drawer, open, closeDrawer, labels: navDrawerLabels, setBrand }),
    [drawer, open, closeDrawer, navDrawerLabels]
  );
  const backgroundInert = drawer && open ? true : undefined;

  return (
    <NavDrawerContext.Provider value={context}>
      <div
        className={cn("fixed inset-0 flex w-full overflow-hidden bg-canvas text-fg", drawer && "flex-col", className)}
        data-nav-mode={drawer ? "drawer" : undefined}
      >
        <a className="pr-skip-link" href="#pr-main" inert={backgroundInert} onClick={onSkipLinkClick}>
          {skipLinkLabel}
        </a>
        {drawer ? (
          // 上端のバー（サイドバーと同じ反転面）。本文のスクロールの外に置き、どこまでスクロールしてもメニューに届く。
          // <header> にしない（md 以上に無い banner の landmark を増やさず、PageHeader の <header> と取り違えない）。
          <>
            <div
              data-nav-drawer-bar=""
              data-surface="inverted"
              data-testid="nav-drawer-bar"
              className="flex h-14 shrink-0 items-center gap-2 border-b border-border bg-surface px-2 text-fg-muted"
              inert={backgroundInert}
            >
              <Button
                ref={triggerRef}
                type="button"
                variant="ghost"
                iconOnly
                touchTarget
                icon={Menu}
                aria-label={navDrawerLabels.menu}
                aria-expanded={open}
                aria-controls={drawerId}
                data-testid="nav-drawer-trigger"
                onClick={() => setOpen(true)}
              />
              {brand ? (
                <div className="min-w-0 text-fg" title={brand.full}>
                  <span className="block truncate whitespace-nowrap text-sm font-bold leading-5">{brand.line1}</span>
                  <span className="block truncate whitespace-nowrap text-xs font-semibold leading-4 text-fg-muted">{brand.line2}</span>
                </div>
              ) : null}
            </div>
            <div aria-hidden className="h-[var(--toast-top-clearance,0px)] shrink-0" />
          </>
        ) : (
          sidebar
        )}
        <main
          ref={mainRef}
          id="pr-main"
          tabIndex={-1}
          inert={backgroundInert}
          className={cn(
            // relative: 本文の中の absolute の要素（読み上げ専用の `sr-only` のライブリージョン・ラベルなど）の包含ブロックを
            // main（本文のスクロール領域）にする。無いと包含ブロックが外枠（fixed）になり、main をスクロールしても
            // 動かずに外枠の下端の外に残る。外枠（overflow: hidden）がその分だけ隠れてスクロールでき、要素を見える位置へ
            // 移す操作（scrollIntoView・ページ内検索・支援技術）で外枠ごと上にずれ、本文の下端が画面の外に切れた（#2064。#1116 と同じ型）。
            "relative flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto [scrollbar-gutter:stable]",
            backgroundInert && "overflow-hidden",
            mainClassName
          )}
        >
          {children}
        </main>
        {drawer ? (
          <>
            {/* scrim。タップで閉じる。読み上げとキーボードには出さない（閉じるボタンと Escape がある）。 */}
            <div
              aria-hidden
              data-testid="nav-drawer-scrim"
              className={cn(
                "fixed inset-0 z-[var(--z-scrim)] touch-none bg-[var(--scrim)] transition-opacity duration-200 ease-out motion-reduce:transition-none",
                open ? "opacity-100" : "pointer-events-none opacity-0"
              )}
              onClick={closeDrawer}
            />
            {/* 閉じている間も描いたままにし（inert・visibility: hidden）、開閉を transform で動かす。 */}
            <div
              ref={panelRef}
              id={drawerId}
              role="dialog"
              aria-modal="true"
              aria-label={navDrawerLabels.menu}
              tabIndex={-1}
              inert={open ? undefined : true}
              data-state={open ? "open" : "closed"}
              data-testid="nav-drawer"
              className={cn(
                "fixed inset-y-0 left-0 z-[var(--z-dialog)] flex max-w-[calc(100vw-3.5rem)] shadow-[var(--shadow-dialog)] outline-none",
                "duration-200 ease-out motion-reduce:transition-none",
                // 開くときは visibility を即座に visible にする（遷移させると開いた瞬間にフォーカスを移せない）。
                // 閉じるときは slide が終わってから hidden にする。
                open ? "visible translate-x-0 transition-transform" : "invisible -translate-x-full transition-[transform,visibility]"
              )}
              onKeyDown={onPanelKeyDown}
              onClick={(event) => {
                if (closesNavDrawer(event.target)) setOpen(false);
              }}
            >
              {sidebar}
            </div>
          </>
        ) : null}
      </div>
    </NavDrawerContext.Provider>
  );
}
