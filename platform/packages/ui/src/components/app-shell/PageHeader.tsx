import { ArrowLeft, ChevronDown, type LucideIcon } from "lucide-react";
import {
  Fragment,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { cn } from "../../lib/utils";
import { Button, type ButtonVariantToneProps } from "../ui/button";
import { contextualMenuLabel, FloatingActionMenu } from "../ui/floating-menu";
import { measureClass } from "./PageBody";

export interface PageHeaderAction {
  id: string;
  /** primary = ページの主操作 / secondary / utility = 枠付き secondary / danger = 破壊的操作。 */
  kind: "primary" | "secondary" | "utility" | "danger";
  /** 翻訳済みラベル。省略するとアイコンだけのボタンになる（`ariaLabel` 必須）。 */
  label?: string;
  /** 画面の文脈を含む読み上げ名。ラベルがあっても指定すれば優先する（アイコンだけの場合は必須）。 */
  ariaLabel?: string;
  icon?: LucideIcon;
  onClick?: () => void;
  loading?: boolean;
  disabled?: boolean;
  /** ボタンの data-testid。 */
  testId?: string;
}

/**
 * 詳細・作成・編集の画面の「一覧へ戻る」（#618）。ページの左上（タイトルの上）に、左向きの矢印付きの
 * テキストのボタンで出す。右の操作の列（保存など）には置かない。文言は 3 製品で「一覧へ戻る」にそろえる。
 */
export interface PageHeaderBack {
  /** 翻訳済みの文言（「一覧へ戻る」）。 */
  label: string;
  /** 戻る操作（未保存の変更の確認は呼び出し側の離脱ガードが行う。#586）。 */
  onClick: () => void;
  /** 読み上げ名（戻り先を足すとき。例:「検索・回答プロファイルの一覧へ戻る」）。省略すると label。 */
  ariaLabel?: string;
  disabled?: boolean;
  testId?: string;
}

const ORDER: Record<PageHeaderAction["kind"], number> = { danger: 0, utility: 1, secondary: 2, primary: 3 };
const VARIANT = { primary: "primary", secondary: "secondary", utility: "secondary", danger: "danger" } as const;

/**
 * 操作のグループ（buttons.md §5）。primary / secondary = 作業開始、utility = ページツール、danger = 危険操作。
 * グループの境界に区切り線と余白を置く。
 */
export type PageActionGroup = "danger" | "utility" | "work";

export function actionGroup(kind: PageHeaderAction["kind"]): PageActionGroup {
  return kind === "danger" || kind === "utility" ? kind : "work";
}

/** 並べた順の操作の間で、グループが変わる位置（区切りを置く index。その要素の直前に置く）。 */
export function groupBoundaries(actions: PageHeaderAction[]): Set<number> {
  const boundaries = new Set<number>();
  actions.forEach((action, index) => {
    if (index > 0 && actionGroup(actions[index - 1].kind) !== actionGroup(action.kind)) boundaries.add(index);
  });
  return boundaries;
}

/** 見た目（variant / tone）。メニューの中は ghost で、危険操作だけ文字を danger にする。 */
function actionLook(kind: PageHeaderAction["kind"], menuItem: boolean): ButtonVariantToneProps {
  if (menuItem) return { variant: "ghost", tone: kind === "danger" ? "danger" : "default" };
  return kind === "danger" ? { variant: "danger" } : { variant: VARIANT[kind] };
}

/**
 * アクションの並び順。右寄せグループなので右端（最も押しやすい位置）に primary、左端に danger。
 * 同じ kind の中は渡した順を保つ。
 */
export function orderActions(actions: PageHeaderAction[]): PageHeaderAction[] {
  return actions
    .map((action, index) => ({ action, index }))
    .sort((a, b) => ORDER[a.action.kind] - ORDER[b.action.kind] || a.index - b.index)
    .map(({ action }) => action);
}

/**
 * 狭い画面（lg 未満）で見せる 1 つ。primary → secondary → utility の優先で選び、danger はメニュー側に置く。
 * メニューは上から重要な順（secondary → utility → danger）。破壊的な操作を末尾に置き、誤タップを避ける。
 * メニューに入るのが 1 つだけなら畳まず、広い画面と同じ並びで全部出す（#582。「一覧へ戻る」だけのメニューを
 * 開かせない。ボタン 2 つなら 375px でも 1 行に収まり、メニューのボタンと同じ幅しか使わない）。
 */
export function splitCompactActions(actions: PageHeaderAction[]) {
  // 同じ kind の中は渡した順を保つ（reverse すると同じ kind の順も逆になるため sort で並べる）。
  const byImportance = [...orderActions(actions)].sort((a, b) => ORDER[b.kind] - ORDER[a.kind]);
  if (byImportance.length <= 1) return { visible: byImportance, overflow: [] as PageHeaderAction[] };
  const visible = byImportance.find((action) => action.kind !== "danger");
  const overflow = byImportance.filter((action) => action !== visible);
  if (visible && overflow.length === 1) return { visible: orderActions(actions), overflow: [] as PageHeaderAction[] };
  return { visible: visible ? [visible] : [], overflow };
}

/** メニュー内のキー操作（WAI-ARIA Menu Button）。移動先の index、対象外のキーは null。 */
export function nextMenuIndex(key: string, current: number, count: number): number | null {
  if (count === 0) return null;
  if (key === "ArrowDown") return (current + 1) % count;
  if (key === "ArrowUp") return (current - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

const COMPACT_QUERY = "(max-width: 1023px)";

function useCompact() {
  return useSyncExternalStore(
    (onChange) => {
      const query = window.matchMedia(COMPACT_QUERY);
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    },
    () => window.matchMedia(COMPACT_QUERY).matches,
    () => false
  );
}

function ActionButton({ action, menuItem = false, onInvoked }: { action: PageHeaderAction; menuItem?: boolean; onInvoked?: () => void }) {
  return (
    <Button
      {...actionLook(action.kind, menuItem)}
      role={menuItem ? "menuitem" : undefined}
      icon={action.icon}
      iconOnly={!menuItem && !action.label}
      aria-label={action.ariaLabel}
      data-testid={action.testId}
      loading={action.loading}
      disabled={action.disabled}
      className={menuItem ? "w-full justify-start" : undefined}
      onClick={() => {
        onInvoked?.();
        action.onClick?.();
      }}
    >
      {menuItem || action.label ? <span>{action.label ?? action.ariaLabel}</span> : null}
    </Button>
  );
}

/**
 * 「その他の操作」メニュー（WAI-ARIA Menu Button）。開くと先頭の項目へフォーカスし、
 * ↓ ↑ Home End で移動、Escape で閉じてトリガーへ戻る。外側のクリック・Tab でも閉じる
 * （Tab はトリガーの次へ、Shift+Tab はトリガーへ）。
 * メニューは FloatingActionMenu（body へ Portal・fixed）で画面内に置く。トリガーが左寄り（375px で操作が
 * 折り返したとき）なら左端、右寄りなら右端にそろえ、どちらも入らなければ画面の内側にずらす（#363）。
 */
function OverflowMenu({ actions, label, context }: { actions: PageHeaderAction[]; label: string; context: string }) {
  // 読み上げ名に対象（アクション群の名前）を足し、カードの「その他の操作」と区別する（#582）。
  const accessibleName = contextualMenuLabel(label, context);
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const items = () => Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? []);

  useEffect(() => {
    if (!open) return;
    items()[0]?.focus({ preventScroll: true });
    const closeOnOutside = (event: MouseEvent) => {
      const target = event.target as Node;
      // メニューは body へ Portal で描くため、トリガー側（rootRef）とメニューの両方を内側として扱う。
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutside);
    return () => document.removeEventListener("mousedown", closeOnOutside);
  }, [open]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
      return;
    }
    if (event.key === "Tab") {
      // メニューは body の末尾にあるため、そのままでは Tab が文書の末尾へ抜ける。
      // トリガーへフォーカスを移してから既定の Tab を進め、トリガーの次の要素へ移す。Shift+Tab はトリガーで止める。
      if (event.shiftKey) event.preventDefault();
      triggerRef.current?.focus({ preventScroll: true });
      setOpen(false);
      return;
    }
    const list = items();
    const next = nextMenuIndex(event.key, list.indexOf(document.activeElement as HTMLButtonElement), list.length);
    if (next === null) return;
    event.preventDefault();
    list[next]?.focus({ preventScroll: true });
  };

  return (
    <div ref={rootRef}>
      <Button
        ref={triggerRef}
        type="button"
        variant="secondary"
        trailingIcon={ChevronDown}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={accessibleName}
        data-testid="page-actions-more"
        onClick={() => setOpen((current) => !current)}
      >
        <span>{label}</span>
      </Button>
      {open ? (
        <FloatingActionMenu
          id={menuId}
          open={open}
          align="end"
          ariaLabel={accessibleName}
          triggerRef={triggerRef}
          menuRef={menuRef}
          onKeyDown={onKeyDown}
          className="min-w-56 gap-0.5"
        >
          {actions.map((action, index) => (
            <Fragment key={action.id}>
              {/* 危険操作は区切り線の下に置く（buttons.md §5.1）。 */}
              {action.kind === "danger" && index > 0 && actions[index - 1].kind !== "danger" ? (
                <div role="separator" className="my-1 h-px bg-border" data-testid="page-actions-menu-separator" />
              ) : null}
              <ActionButton action={action} menuItem onInvoked={() => setOpen(false)} />
            </Fragment>
          ))}
        </FloatingActionMenu>
      ) : null}
    </div>
  );
}

/**
 * 画面共通ヘッダー。lg 以上ではスクロールしても上端に貼り付き（sticky）、タイトルと主要操作に常に手が届く。
 * `<header>` は画面幅いっぱい（背景と罫線）、中身は PageBody と同じ計測コンテナに入れる。
 * `wide` は PageBody と必ず同じ値にする（ずらすと 1920px でタイトルと本文の左端がずれる）。
 */
export function PageHeader({
  title,
  subtitle,
  meta,
  status,
  breadcrumbs,
  back,
  actions,
  actionsLabel = "ページ操作",
  actionsTestId,
  moreActionsLabel = "その他の操作",
  tabs,
  wide = false,
  className,
}: {
  title: string;
  subtitle?: string;
  /** 副題の下の補足（最終更新・件数など）。 */
  meta?: ReactNode;
  /** タイトル横の状態表示（StatusBadge 等）。 */
  status?: ReactNode;
  /** タイトル上のパンくず（`<Breadcrumbs>`）。3 階層以上の深い導線だけに使う（2 階層の「一覧 › 対象」は `back`）。 */
  breadcrumbs?: ReactNode;
  /**
   * 詳細・作成・編集の画面の「一覧へ戻る」（#618）。タイトルの上の左端に出す（Tab の順の先頭）。
   * `actions` に戻る操作を入れない。
   */
  back?: PageHeaderBack;
  /**
   * danger → utility → secondary → primary の順に並べ替えて描画する（buttons.md §5）。
   * JSX（`<Button>` 等）は受けない（3 製品の移行が済んだので後方互換の ReactNode を外した。#823）。
   */
  actions?: PageHeaderAction[];
  /** アクション群の aria-label（翻訳済み）。 */
  actionsLabel?: string;
  /** アクション群の data-testid。 */
  actionsTestId?: string;
  /**
   * 狭い画面（lg 未満）で primary 以外をまとめるメニューのラベル（翻訳済み）。読み上げ名は
   * 「<moreActionsLabel>（<actionsLabel>）」になる（#582。カードの「その他の操作」と区別する）。
   */
  moreActionsLabel?: string;
  /** `<Tabs>` を渡すとヘッダー下端に吸い付く（ビュー切替の唯一の置き場所）。 */
  tabs?: ReactNode;
  wide?: boolean;
  className?: string;
}) {
  const compact = useCompact();
  let actionNodes: ReactNode = null;
  if (actions && actions.length > 0) {
    const list = actions;
    // 狭い画面では主操作 1 つ +「その他の操作」にまとめ、sticky ヘッダーが本文を覆わない高さに保つ。
    const { visible, overflow } = compact ? splitCompactActions(list) : { visible: orderActions(list), overflow: [] };
    const boundaries = groupBoundaries(visible);
    actionNodes =
      list.length > 0 ? (
        <>
          {overflow.length > 0 ? <OverflowMenu actions={overflow} label={moreActionsLabel} context={actionsLabel} /> : null}
          {visible.map((action, index) => (
            <Fragment key={action.id}>
              {/* グループの境界に軽い区切りと余白（buttons.md §5）。gap-2 と合わせて左右 1rem 空く。 */}
              {boundaries.has(index) ? (
                <span aria-hidden className="mx-1 h-5 w-px self-center bg-border" data-testid="page-actions-separator" />
              ) : null}
              <ActionButton action={action} />
            </Fragment>
          ))}
        </>
      ) : null;
  }

  return (
    <header
      // e2e がページの見出しの面を見分ける目印（通知は右下に固定し、この面を覆わない。#1728）。
      data-page-header=""
      className={cn(
        // 貼り付くのは lg 以上だけ。狭い画面では文字の折り返しでヘッダーが高くなり、貼り付くと本文の表示領域を常に削るため。
        "flex flex-col border-b border-border bg-surface lg:sticky lg:top-0 lg:z-[var(--z-sticky)]",
        tabs ? "gap-4 pt-5" : "py-5",
        className
      )}
    >
      <div className={cn(measureClass(wide), "flex flex-wrap items-start justify-between gap-4")}>
        <div className="min-w-0 flex-auto">
          {back ? (
            <div className="mb-1.5">
              {/* 左の余白をボタンの内側の余白の分だけ戻し、矢印の左端をタイトルの左端にそろえる。 */}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                icon={ArrowLeft}
                onClick={back.onClick}
                disabled={back.disabled}
                aria-label={back.ariaLabel}
                tooltip={false}
                data-testid={back.testId}
                data-page-header-back=""
                className="-ml-[var(--button-padding-sm)] text-fg-muted hover:enabled:text-fg"
              >
                <span>{back.label}</span>
              </Button>
            </div>
          ) : null}
          {breadcrumbs ? <div className="mb-1.5">{breadcrumbs}</div> : null}
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-xl font-bold text-fg">{title}</h1>
            {status}
          </div>
          {subtitle ? <p className="mt-1 text-sm text-fg-muted">{subtitle}</p> : null}
          {meta ? <div className="mt-1 text-xs text-fg-muted">{meta}</div> : null}
        </div>
        {actionNodes ? (
          <div
            role="group"
            aria-label={actionsLabel}
            data-testid={actionsTestId}
            // e2e がページの操作の並びを見分ける目印（通知がページの操作を覆わないことの確認に使う。#1728）。
            data-page-header-actions=""
            className="flex min-w-0 flex-wrap items-center gap-2"
          >
            {actionNodes}
          </div>
        ) : null}
      </div>
      {tabs ? <div className={measureClass(wide)}>{tabs}</div> : null}
    </header>
  );
}
