# 参照実装（新規・変更コンポーネント）

**そのまま出荷しないでください。** これは Claude Design で作成した参照実装（React + インラインスタイル）です。
`packages/ui` の既存の作法（`.tsx` + Tailwind v4）に合わせて**書き直してください**。
写すべきは「構造・寸法・トークン名・状態の扱い・ARIA」であって、インラインスタイルの書き方ではありません。

対応する仕様は `README.md` の §4（柱 C）を参照。

> **アイコンの読み替え（packages/ui の決定）:** 下の JSX は Lucide 名の文字列と `<Icon name="…" />` で書かれていますが、`packages/ui` ではアイコン props を **`LucideIcon`（`lucide-react` のコンポーネントそのもの）** で受けます。`icon="Upload"` → `icon={Upload}`、`<Icon name="Upload" size={16} />` → `<Upload size={16} aria-hidden />` と読み替えてください。文字列から引く方式は全アイコンをバンドルに含めるため採用しません（RAG 実測で JS +38%）。サイズは 14 / 16 / 20 / 24 の4値のみで、Sidebar の `size={18}` は `20` と読み替えます。

---

## Tabs.jsx — **新規**

```jsx
import React from "react";
import { Icon } from "../core/Icon.jsx";

/**
 * ビュー切替。**同じ対象の別の見方**に切り替えるときだけ使います。
 * データを絞り込むだけなら ToggleChip、別の画面に移るなら Sidebar です。
 *
 * 下線スタイル固定（管理コンソールの標準）。PageHeader の `tabs` に渡すと
 * ヘッダー下端に吸い付き、単体でもカード内で使えます。
 *
 * items: [{ id, label, icon?, count?, disabled? }]
 * キーボード: ← → で移動、Home / End で端へ（WAI-ARIA Tabs パターン）。
 */
export function Tabs({ items = [], value, onChange, ariaLabel = "ビュー切替", style }) {
  const refs = React.useRef({});
  const enabled = items.filter((item) => !item.disabled);

  const move = (delta) => {
    if (enabled.length === 0) return;
    const current = enabled.findIndex((item) => item.id === value);
    const next = enabled[(current + delta + enabled.length) % enabled.length];
    if (!next) return;
    onChange && onChange(next.id);
    const node = refs.current[next.id];
    if (node) node.focus();
  };

  const jump = (index) => {
    const target = enabled[index];
    if (!target) return;
    onChange && onChange(target.id);
    const node = refs.current[target.id];
    if (node) node.focus();
  };

  const onKeyDown = (event) => {
    if (event.key === "ArrowRight") { event.preventDefault(); move(1); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); move(-1); }
    else if (event.key === "Home") { event.preventDefault(); jump(0); }
    else if (event.key === "End") { event.preventDefault(); jump(enabled.length - 1); }
  };

  return (
    <div role="tablist" aria-label={ariaLabel} className="pr-tablist" onKeyDown={onKeyDown} style={style}>
      {items.map((item) => {
        const selected = item.id === value;
        return (
          <button
            key={item.id}
            ref={(node) => { refs.current[item.id] = node; }}
            type="button"
            role="tab"
            id={`pr-tab-${item.id}`}
            className="pr-tab"
            aria-selected={selected}
            aria-controls={`pr-tabpanel-${item.id}`}
            tabIndex={selected ? 0 : -1}
            disabled={item.disabled}
            onClick={() => onChange && onChange(item.id)}
          >
            {item.icon ? <Icon name={item.icon} size={16} /> : null}
            <span>{item.label}</span>
            {item.count == null ? null : <span className="pr-tab-count">{item.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

/** 対応する中身。`id` は Tabs の item.id と一致させます。 */
export function TabPanel({ id, value, children, style }) {
  if (id !== value) return null;
  return (
    <div role="tabpanel" id={`pr-tabpanel-${id}`} aria-labelledby={`pr-tab-${id}`} tabIndex={0} style={{ minWidth: 0, ...style }}>
      {children}
    </div>
  );
}
```

### Tabs の props

```ts
import * as React from "react";
import type { LucideIcon } from "lucide-react";

export interface TabsProps {
  items: { id: string; label: string; icon?: LucideIcon; count?: number; disabled?: boolean }[];
  value: string;
  onChange?: (id: string) => void;
  ariaLabel?: string;
  style?: React.CSSProperties;
}

/** @dsComponent */
export declare function Tabs(props: TabsProps): JSX.Element;

export interface TabPanelProps {
  id: string; value: string; children?: React.ReactNode; style?: React.CSSProperties;
}

/** @dsComponent */
export declare function TabPanel(props: TabPanelProps): JSX.Element;
```

---

## PageBody.jsx — **新規**

```jsx
import React from "react";

/**
 * PageHeader の直後に置く本文コンテナ。readme が規定していた
 * 「2rem の左右ガター / セクション間 1.5rem」の唯一の実装です。
 * これが無かったため 3 アプリがそれぞれ手書きしていました。
 *
 * `--content-max-width` (1440px) で計測幅を止めます。<main> が無制限に伸びると
 * 10 列の表が 2400px に広がり、目が行を追えません（視線移動限界は約 1000〜1200px）。
 * 表を画面幅いっぱいに出したい画面だけ `wide` を使います。
 */
export function PageBody({ wide = false, children, style }) {
  return (
    <div
      style={{
        display: "grid",
        gap: "var(--gap-stack)",
        alignContent: "start",
        width: "100%",
        maxWidth: wide ? "none" : "var(--content-max-width)",
        marginInline: wide ? 0 : "auto",
        boxSizing: "border-box",
        padding: "var(--page-gutter-y) var(--page-gutter-x)",
        minWidth: 0,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

/**
 * 見出し付きのひとまとまり。カードを複数含む領域や、カードに入れるほどでもない
 * 領域に使います。見出しは --text-section-title（16px / 600）で、
 * ページタイトル 20px とカード見出し 14px の間の段を埋めます。
 */
export function Section({ title, description, actions, children, style }) {
  return (
    <section style={{ display: "grid", gap: "var(--space-3)", minWidth: 0, ...style }}>
      {title || actions ? (
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "var(--space-4)", minWidth: 0 }}>
          <div style={{ minWidth: 0 }}>
            {title ? <h2 style={{ margin: 0, font: "var(--text-section-title)", color: "var(--color-fg)" }}>{title}</h2> : null}
            {description ? <p style={{ margin: "var(--space-1) 0 0", font: "var(--text-body)", color: "var(--color-fg-muted)" }}>{description}</p> : null}
          </div>
          {actions ? <div style={{ display: "flex", flexShrink: 0, alignItems: "center", gap: "var(--gap-action)" }}>{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}
```

### PageBody の props

```ts
import * as React from "react";

export interface PageBodyProps {
  /** 表を画面幅いっぱいに出す画面だけ true。既定は --content-max-width で止める。 */
  wide?: boolean;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}

/** @dsComponent */
export declare function PageBody(props: PageBodyProps): JSX.Element;

export interface SectionProps {
  title?: string; description?: string; actions?: React.ReactNode;
  children?: React.ReactNode; style?: React.CSSProperties;
}

/** @dsComponent */
export declare function Section(props: SectionProps): JSX.Element;
```

---

## PageHeader.jsx — 変更

```jsx
import React from "react";
import { Button } from "../core/Button.jsx";
import { Icon } from "../core/Icon.jsx";

/* アクションの並び順。右寄せグループなので、右端（＝最も押しやすい位置）に primary が来ます。
   旧構成は primary が左端で danger が右端＝最も破壊的な操作が最も押しやすい位置でした。
   danger は本来オーバーフローメニューに入れるべきものです（DropdownMenu 実装後に移行）。 */
const ORDER = { danger: 0, utility: 1, secondary: 2, primary: 3 };
const VARIANT = { primary: "primary", secondary: "secondary", utility: "secondary", danger: "danger" };
/** 操作のグループ。境界に区切り線を置く（buttons.md §5）。 */
const GROUP = { danger: "danger", utility: "utility", secondary: "work", primary: "work" };

/** Location trail for 3+ level flows. The last item is the current page. */
export function Breadcrumbs({ items = [], onNavigate }) {
  if (items.length === 0) return null;
  return (
    <nav aria-label="パンくず" style={{ display: "flex", alignItems: "center", font: "var(--text-meta)", color: "var(--color-fg-muted)" }}>
      <ol style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "var(--space-1)", margin: 0, padding: 0, listStyle: "none" }}>
        {items.map((item, index) => {
          const last = index === items.length - 1;
          return (
            <React.Fragment key={`${item.label}-${index}`}>
              <li>
                {item.href && !last ? (
                  <a href={item.href} onClick={onNavigate ? (event) => { event.preventDefault(); onNavigate(item.href); } : undefined} style={{ color: "inherit", textDecoration: "none" }}>{item.label}</a>
                ) : (
                  <span aria-current={last ? "page" : undefined} style={last ? { fontWeight: 500, color: "var(--color-fg)" } : undefined}>{item.label}</span>
                )}
              </li>
              {last ? null : <li aria-hidden="true" style={{ display: "flex", color: "var(--color-fg-subtle)" }}><Icon name="ChevronRight" size={14} /></li>}
            </React.Fragment>
          );
        })}
      </ol>
    </nav>
  );
}

/**
 * Every screen opens with this header (px-8 py-5 on --color-surface over a hairline).
 * 長い表でもタイトルと主要操作に手が届くよう `position: sticky` で上端に貼り付きます。
 * `tabs` に <Tabs> を渡すとヘッダー下端に吸い付きます（ビュー切替の唯一の置き場所）。
 * actions: [{ id, kind: "primary"|"secondary"|"utility"|"danger", label, icon?, onClick?, loading?, disabled? }]
 * 並びは danger → utility → secondary → primary（右端が primary）。
 * グループ（danger / utility / secondary + primary）の境界に区切り線を置く（buttons.md §5）。
 */
export function PageHeader({ title, subtitle, status, breadcrumbs, actions = [], tabs, wide = false, onNavigate }) {
  const ordered = actions.map((action, index) => ({ action, index })).sort((a, b) => ORDER[a.action.kind] - ORDER[b.action.kind] || a.index - b.index).map(({ action }) => action);
  /* ※ <header> は full-bleed（背景と罫線を画面幅いっぴいに伸ばす）、
     中身は PageBody と同じ計測コンテナに入れる。
     これがないと 1920px モニタでタイトルとカードの左端が 240px ずれます。
     wide は PageBody の wide と必ず揃えること。 */
  const measure = {
    width: "100%",
    maxWidth: wide ? "none" : "var(--content-max-width)",
    marginInline: wide ? 0 : "auto",
    boxSizing: "border-box",
    paddingInline: "var(--page-gutter-x)",
    minWidth: 0,
  };
  return (
    <header
      style={{
        position: "sticky",
        top: 0,
        zIndex: "var(--z-sticky)",
        display: "flex",
        flexDirection: "column",
        gap: tabs ? "var(--space-4)" : 0,
        padding: tabs ? "var(--page-header-py) 0 0" : "var(--page-header-py) 0",
        borderBottom: "1px solid var(--color-border)",
        background: "var(--color-surface)",
      }}
    >
      <div style={{ ...measure, display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: "var(--space-4)" }}>
        <div style={{ minWidth: 0, flex: "1 1 auto" }}>
          {breadcrumbs && breadcrumbs.length ? <div style={{ marginBottom: "var(--space-1-5)" }}><Breadcrumbs items={breadcrumbs} onNavigate={onNavigate} /></div> : null}
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "var(--space-2-5)" }}>
            <h1 style={{ margin: 0, font: "var(--text-page-title)", color: "var(--color-fg)" }}>{title}</h1>
            {status || null}
          </div>
          {subtitle ? <p style={{ margin: "var(--space-1) 0 0", font: "var(--text-body)", color: "var(--color-fg-muted)" }}>{subtitle}</p> : null}
        </div>
        {ordered.length ? (
          <div role="group" aria-label="ページ操作" style={{ display: "flex", flexShrink: 0, alignItems: "center", gap: "var(--gap-action)" }}>
            {ordered.map((action, index) => (
              <React.Fragment key={action.id}>
              {index > 0 && GROUP[ordered[index - 1].kind] !== GROUP[action.kind] ? (
                <span aria-hidden className="pr-page-header__separator" />
              ) : null}
              <Button
                variant={VARIANT[action.kind]}
                icon={action.icon}
                loading={action.loading}
                disabled={action.disabled}
                onClick={action.onClick}
                iconOnly={!action.label}
                aria-label={action.label ? undefined : action.ariaLabel}
              >
                {action.label ? <span>{action.label}</span> : null}
              </Button>
              </React.Fragment>
            ))}
          </div>
        ) : null}
      </div>
      {tabs ? <div style={measure}>{tabs}</div> : null}
    </header>
  );
}
```

### PageHeader の props

```ts
import * as React from "react";
import type { LucideIcon } from "lucide-react";

export interface PageHeaderProps {
  title: string; subtitle?: string; status?: React.ReactNode;
  breadcrumbs?: { label: string; href?: string }[];
  /**
   * 詳細・作成・編集の画面の「一覧へ戻る」（#618）。タイトルの上の左端に ghost の sm ボタン（ArrowLeft）で出す。
   * actions に id: "back" の操作を入れない。2 階層のパンくずの代わり（3 階層以上だけ breadcrumbs）。
   */
  back?: { label: string; onClick: () => void; ariaLabel?: string; disabled?: boolean; testId?: string };
  /** 並びは danger → utility → secondary → primary（右端が primary）。詳細・作成・編集の画面は 変更を破棄（secondary）→ 保存（primary）。 */
  actions?: { id: string; kind: "primary" | "secondary" | "utility" | "danger"; label?: string; ariaLabel?: string; icon?: LucideIcon; onClick?: () => void; loading?: boolean; disabled?: boolean }[];
  /** <Tabs> を渡すとヘッダー下端に吸い付く。ビュー切替の唯一の置き場所。 */
  tabs?: React.ReactNode;
  /** PageBody の wide と必ず同じ値にする。ずらすと本文と左端が揃わない。 */
  wide?: boolean;
  onNavigate?: (href: string) => void;
}

/** @dsComponent */
export declare function PageHeader(props: PageHeaderProps): JSX.Element;

export interface BreadcrumbsProps {
  items: { label: string; href?: string }[]; onNavigate?: (href: string) => void;
}

/** @dsComponent */
export declare function Breadcrumbs(props: BreadcrumbsProps): JSX.Element;
```

---

## Button.jsx — 変更

```jsx
import React from "react";
import { Spinner } from "./Spinner.jsx";
import { Icon } from "./Icon.jsx";

/**
 * RAG / NL2SQL / Agent 共通の唯一のボタン。4 バリアント × 3 サイズ（32 / 36 / 40px）。
 *
 * ── アイコンの規約 ────────────────────────────────────────────────────────
 * `icon` に Lucide 名を渡します（子要素に <Icon> を直接書かない）。
 * 先頭スロットは常に 16px。`trailingIcon` は方向・開閉・外部リンクのみ
 * （chevron / ExternalLink）。アイコンを 2 つ持たせないこと。
 *
 * ── ローディングの規約 ──────────────────────────────────────────────────
 * `loading` 中は **先頭アイコンがスピナーに置き換わります**。
 *   - ラベルは変えません（「実行中…」に差し替えない）
 *   - したがって幅が変わらず、レイアウトが跳ねません
 *   - `aria-busy="true"` と `aria-disabled="true"` が自動で付きます。ネイティブの `disabled` は付けません
 *     （フォーカス中のボタンに disabled を付けるとフォーカスが body へ外れるため。#355）。
 *     クリック・Enter / Space・form の submit（暗黙の送信を含む）は止め、見た目は disabled と同じにします
 *   - `disabled` prop はネイティブの `disabled` のまま（loading と重なったら disabled を優先）
 * ★ 非同期の操作を起こすボタンは必ず `icon` を持たせてください（adherence の lint が検出します）。
 *   アイコンが無いとスピナーの分だけ幅が広がります。
 * ★ `variant="danger"` と `tone="danger"` は同時に指定できません（型で禁止。赤地に赤文字になる）。
 * 1 秒を超えて領域全体が待ちになる処理は、ボタンではなく領域側で
 * LoadingState を出します。
 */
export function Button({
  variant = "primary",
  size = "md",
  icon,
  trailingIcon,
  iconOnly = false,
  touchTarget = false,
  tone = "default",
  loading = false,
  disabled = false,
  pressed,
  type = "button",
  className = "",
  children,
  onClick,
  ...rest
}) {
  // disabled が優先。loading だけのときはフォーカスを保つため aria-disabled にしてクリックを止める。
  const busy = loading && !disabled;
  const classes = [
    "pr-button",
    `pr-button--${variant}`,
    size !== "md" && `pr-button--${size}`,
    iconOnly && "pr-button--icon",
    touchTarget && "pr-button--touch",
    tone === "danger" && "pr-button--danger-tone",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  // 先頭スロット: loading 中はスピナーがアイコンを置き換える（幅不変）。
  // icon が無いまま loading にするとスピナーの分だけ幅が広がる（規約違反）。
  // 寸法は sm / md / lg とも 16px。スピナーは fg-muted（disabled の地の上で 3:1 以上。README §4「Spinner」、#395）。
  const leading = loading ? <Spinner size={16} className="text-fg-muted" /> : icon ? <Icon name={icon} size={16} /> : null;

  return (
    <button
      type={type}
      className={classes}
      aria-busy={loading || undefined}
      aria-pressed={pressed}
      {...rest}
      disabled={disabled || undefined}
      aria-disabled={busy || undefined}
      onClick={busy ? (event) => { event.preventDefault(); event.stopPropagation(); } : onClick}
    >
      {leading}
      {children}
      {trailingIcon && !loading ? <Icon name={trailingIcon} size={16} /> : null}
    </button>
  );
}
```

### Button の props

```ts
import * as React from "react";
import type { LucideIcon } from "lucide-react";

/** variant="danger"（赤塗り）と tone="danger"（赤文字）の同時指定は禁止（赤地に赤文字になる）。 */
export type ButtonVariantToneProps =
  | { variant: "danger"; tone?: "default" }
  | { variant?: "primary" | "secondary" | "ghost"; tone?: "default" | "danger" };

export type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & ButtonVariantToneProps & {
  size?: "sm" | "md" | "lg";
  /** 先頭アイコン（lucide-react のコンポーネント。例: icon={Upload}）。子要素にアイコンを直接書かない。 */
  icon?: LucideIcon;
  /** 方向・開閉・外部リンクのみ（ChevronRight / ChevronDown / ExternalLink）。 */
  trailingIcon?: LucideIcon;
  /** アイコンだけのボタン。aria-label 必須。既定で aria-label と同じ文言の Tooltip を出す（#372）。 */
  iconOnly?: boolean;
  /** ホバーとキーボードのフォーカスで出す説明。iconOnly の既定は aria-label、false で出さない。出すときは title を無視する。 */
  tooltip?: string | false;
  tooltipPlacement?: "top" | "bottom";
  /** マウス環境でも 44px にする。製品では使わない（adherence の lint が検出する。#613）。packages/ui の閉じる・メニューのボタンだけ。 */
  touchTarget?: boolean;
  /** true で先頭アイコンがスピナーに置き換わる。ラベルは変えない。aria-disabled でフォーカスを保つ。 */
  loading?: boolean;
  pressed?: boolean;
};

/** @dsComponent */
export declare function Button(props: ButtonProps): JSX.Element;
```

### ButtonLink（#800）

画面を移るだけの操作を、`Button` と同じ見た目のリンクで出す（README §4「Button」の「画面を移るだけの操作は `ButtonLink`」）。

```ts
/** react-router の Link をそのまま渡せる（packages/ui はルーターに依存しない）。 */
export type ButtonLinkComponent = React.ComponentType<{
  to: string;
  className?: string;
  children: React.ReactNode;
  "aria-label"?: string;
  "data-testid"?: string;
}>;

export type ButtonLinkProps = ButtonVariantToneProps & {
  to: string;
  /** 省略すると <a href>。 */
  linkComponent?: ButtonLinkComponent;
  size?: "sm" | "md" | "lg";
  icon?: LucideIcon;
  trailingIcon?: LucideIcon;
  children: React.ReactNode;
  className?: string;
  "aria-label"?: string;
  testId?: string;
};

/** 既定の variant は secondary。loading / disabled は持たない。 */
export declare function ButtonLink(props: ButtonLinkProps): JSX.Element;
```

```tsx
<ButtonLink to={`/settings/security/permissions?role=${id}`} linkComponent={Link} size="sm" icon={LockKeyhole}>
  権限管理で設定
</ButtonLink>
```

---

## StatusBadge.jsx — 変更

```jsx
import React from "react";
import { Icon } from "./Icon.jsx";

const VARIANTS = {
  neutral: ["var(--color-border-control)", "var(--color-surface)", "var(--color-fg-muted)"],
  info:    ["var(--color-info-border)",    "var(--color-info-subtle)",    "var(--color-info-fg)"],
  success: ["var(--color-success-border)", "var(--color-success-subtle)", "var(--color-success-fg)"],
  warning: ["var(--color-warning-border)", "var(--color-warning-subtle)", "var(--color-warning-fg)"],
  danger:  ["var(--color-danger-border)",  "var(--color-danger-subtle)",  "var(--color-danger-fg)"],
};
/** @deprecated 旧 pending は warning と完全同値だった。warning を使うこと。 */
VARIANTS.pending = VARIANTS.warning;

/* 状態は色だけで表しません。success #047857 と danger #b91c1c は輝度がほぼ同じ
   （L=0.139 / 0.110）で、1型・2型色覚では見分けられないため、形（アイコン）で
   冗長に符号化します。強制カラーモードでも意味が残ります。 */
const VARIANT_ICON = {
  neutral: "Minus",
  info: "Info",
  success: "CircleCheck",
  warning: "TriangleAlert",
  danger: "CircleAlert",
};
VARIANT_ICON.pending = "History";

/**
 * State pill. Token + 1px border version (promoted from NL2SQL) so it follows the dark theme.
 * Apps map their domain enum (FileStatus, RunStatus, …) to a variant and pass a translated label.
 */
export function StatusBadge({ variant = "neutral", label, icon = true, style }) {
  const [border, bg, fg] = VARIANTS[variant] || VARIANTS.neutral;
  const glyph = icon === true ? VARIANT_ICON[variant] || VARIANT_ICON.neutral : icon || null;
  return (
    <span
      data-status-variant={variant}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        whiteSpace: "nowrap",
        borderRadius: "var(--radius-pill)",
        border: `1px solid ${border}`,
        background: bg,
        color: fg,
        gap: "var(--space-1)",
        padding: "var(--space-0-5) var(--space-2-5)",
        font: "var(--font-weight-medium) var(--font-size-xs) / var(--line-height-xs) var(--font-sans)",
        ...style,
      }}
    >
      {glyph ? <Icon name={glyph} size={14} aria-hidden="true" /> : null}
      {label}
    </span>
  );
}
```

### StatusBadge の props

```ts
import * as React from "react";
import type { LucideIcon } from "lucide-react";

export interface StatusBadgeProps {
  variant: "neutral" | "info" | "success" | "warning" | "danger" | /** @deprecated warning と同値 */ "pending";
  label: string;
  /** 既定 true（バリアント既定のアイコン）。LucideIcon で上書き、false で非表示。 */
  icon?: boolean | LucideIcon;
  style?: React.CSSProperties;
}

/** @dsComponent */
export declare function StatusBadge(props: StatusBadgeProps): JSX.Element;
```

---

## Pagination.jsx — 変更

```jsx
import React from "react";
import { Button } from "../core/Button.jsx";

/** Paging under a DataTable. Hidden when there is a single page. Default page size 10. */
export function Pagination({ page = 1, totalPages = 1, onPageChange, summary, pageIndicator, prevLabel = "前へ", nextLabel = "次へ" }) {
  if (totalPages <= 1) return null;
  return (
    <nav aria-label={pageIndicator || summary} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "var(--space-2)", font: "var(--text-meta)", color: "var(--color-fg-muted)" }}>
      <span className="tnum">{summary}</span>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "var(--gap-action)" }}>
        <Button variant="secondary" size="sm" icon="ChevronLeft" disabled={page <= 1} onClick={() => onPageChange && onPageChange(page - 1)}>
          <span>{prevLabel}</span>
        </Button>
        {pageIndicator ? (
          <span className="tnum" style={{ display: "inline-flex", alignItems: "center", minHeight: 32, padding: "0 var(--space-3)", borderRadius: "var(--button-radius)", border: "1px solid var(--color-border-control)", color: "var(--color-fg)" }}>{pageIndicator}</span>
        ) : null}
        <Button variant="secondary" size="sm" trailingIcon="ChevronRight" disabled={page >= totalPages} onClick={() => onPageChange && onPageChange(page + 1)}>
          <span>{nextLabel}</span>
        </Button>
      </div>
    </nav>
  );
}
```

---

> **packages/ui の追加（#265）。** 上の参照実装の props はそのまま。`usePagination(items, pageSize?, options?)` の `options` に
> `page` / `onPageChange`（制御式。ページ番号を作業状態に保持する。再取得で items が変わってもページを戻さない）と
> `resetKey`（この値が変わったときだけ 1 ページ目へ戻す）を足した。省略時は従来どおり items が変わると 1 ページ目へ戻る。
> サーバー側のページング（offset / limit / total）は `offsetPagination({ offset, limit, total, count })` で
> `page` / `totalPages` / `range` に直し、ページの移動は `offsetForPage(page, limit)` で offset に戻す。既定は `DEFAULT_PAGE_SIZE`（25。#1952 で 10 から変えた）。

> **packages/ui の変更（#1891。advanced pagination）。** 上の参照実装（前へ・「N / M ページ」・次へ）は、次の形に置き換えた。
> 既存の props の `summary` / `prevLabel` / `nextLabel` / `pageIndicator`（翻訳済みの文字列）は**削除**し、文言は `labels`
> （`Partial<PaginationLabels>`。製品は `paginationLabels()`）と `range` だけで渡す。
>
> ```text
> [1–25 / 123 件] [表示件数 25 件▾]            [«] [‹ 前へ] [ 1 ] / 5 ページ [次へ ›] [»]
>                                                      1〜5 のページ番号を入力してください（範囲の外のときだけ）
> ```
>
> ```ts
> <Pagination
>   page totalPages onPageChange          // 1-based
>   range?                                 // 左の「1–25 / 123 件」（区切りは en dash、桁区切り）
>   pageSize? pageSizeOptions? onPageSizeChange?  // 渡したときだけ表示件数の選択（選択肢 10 / 25 / 50 / 100・既定 25）
>   mode?: "advanced" | "cursor"           // 既定 advanced。cursor は前へ・「N / M ページ」か「N ページ目」・次へ
>   disabled? labels? ariaLabel? testId? className? />
> <OffsetPagination offset limit total count onPageChange onPageSizeChange? pageSizeOptions? disabled? … />
> <CursorPagination depth limit? total? count nextCursor onPrevious onNext disabled? … />  // total を省くと「N ページ目」
> <PagedDataTable … pageSize? pageSizeOptions? onPageSizeChange? />  // 表示件数を変えると 1 ページ目へ
> ```
>
> - `PaginationLabels` に `pageCount`（「/ N ページ」）・`cursorPage`（「N ページ目」）・`pageInput`（「ページ番号」）・
>   `pageOutOfRange`・`first`（「最初のページ」）・`last`（「最後のページ」）・`pageSize`（「表示件数」）・`pageSizeOption`
>   を足した。`pageIndicator`（「N / M ページ」）と `ariaLabel` は必須になった（既定は `DEFAULT_PAGINATION_LABELS`）。
> - ページ番号の入力: `inputMode="numeric"`（全角の数字も受け付ける。`parsePageInput`）。Enter か欄を離れたときに移る。
>   範囲の外・数でない値は直前のページに戻し、理由を入力欄の下に出す（`aria-describedby`）。Escape は入力を戻す。
>   IME の変換を確定する Enter では移らない。
> - 読み上げ: `nav`（`aria-label`）。今のページ（「2 / 5 ページ」）と範囲の外の理由は、1 つの `role="status"` で読む。
> - 置き場所の幅（container query）が 28rem 未満では最初へ・最後へを隠す（375px・会話の履歴のパネル）。
> - 20rem 未満（`ListDetailLayout` の一覧の列の下端）では前へ・次への文字を `sr-only` にしてアイコンだけで出す（#2282）。
> - 1 ページ以下はページ送りを出さない。表示件数の選択があり、件数が最も少ない段より多いときだけ範囲と選択を出す
>   （`shouldShowPageSize`）。件数そのものは `ListToolbar` の `count`（`ListCount`）が出す。
> - `PAGE_SIZE_OPTIONS` は 10 / 25 / 50 / 100（以前は 10 / 50 / 100）。既定の表示件数 `DEFAULT_PAGE_SIZE` は 25（#1952）。`PageSizeSelect` の既定のラベルは「表示件数」、
>   幅は `xs`（以前は「1 ページの件数」・`sm`）。

---

## DataTable.jsx — 変更

```jsx
import React from "react";
import { Icon } from "../core/Icon.jsx";

/**
 * Every list and result set. text-xs body, bg-background header, px-3 py-2 cells
 * (dense: py-1.5), divide-border/70 rows. Replaces the hand-written <table>s in all three apps.
 * columns: [{ key, header, align?: "left"|"right", mono?, sortable?, render?(row) }]
 */
export function DataTable({ columns = [], rows = [], rowKey = "id", dense = false, sort, onSortChange, onRowClick, emptyText = "データがありません", loading = false, ariaLabel }) {
  const padY = dense ? "var(--space-1-5)" : "var(--space-2)";
  const cell = (column) => ({ padding: `${padY} var(--space-3)`, textAlign: column.align === "right" ? "right" : "left" });
  return (
    <div style={{ overflowX: "auto", borderRadius: "var(--radius-md)", border: "1px solid var(--color-border)", background: "var(--color-surface)" }}>
      <table aria-label={ariaLabel} style={{ width: "100%", borderCollapse: "collapse", font: "var(--text-meta)", color: "var(--color-fg)" }}>
        <thead style={{ background: "var(--color-surface-sunken)", color: "var(--color-fg-muted)" }}>
          <tr>
            {columns.map((column) => {
              const active = sort && sort.key === column.key;
              return (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={active ? (sort.direction === "asc" ? "ascending" : "descending") : undefined}
                  style={{ ...cell(column), fontWeight: 600, whiteSpace: "nowrap" }}
                >
                  {column.sortable ? (
                    <button
                      type="button"
                      className="pr-sort-header"
                      data-align={column.align === "right" ? "right" : undefined}
                      onClick={() => onSortChange && onSortChange({ key: column.key, direction: active && sort.direction === "asc" ? "desc" : "asc" })}
                      style={{ color: active ? "var(--color-fg)" : "var(--color-fg-muted)" }}
                    >
                      {column.header}
                      <Icon name={active ? (sort.direction === "asc" ? "ArrowUp" : "ArrowDown") : "ArrowUpDown"} size={14} />
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {loading
            ? [0, 1, 2].map((index) => (
                <tr key={index} style={{ borderTop: "1px solid color-mix(in srgb, var(--color-border) 70%, transparent)" }}>
                  {columns.map((column) => (
                    <td key={column.key} style={cell(column)}>
                      <span className="pr-pulse" style={{ display: "block", height: "1rem", borderRadius: "var(--radius-sm)", background: "color-mix(in srgb, var(--color-fg-muted) 40%, transparent)" }} />
                    </td>
                  ))}
                </tr>
              ))
            : rows.map((row, index) => (
                <tr
                  key={row[rowKey] ?? index}
                  className={onRowClick ? "pr-row-hover" : undefined}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  style={{ borderTop: "1px solid color-mix(in srgb, var(--color-border) 70%, transparent)", cursor: onRowClick ? "pointer" : undefined }}
                >
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={column.align === "right" ? "tnum" : undefined}
                      style={{ ...cell(column), overflowWrap: "normal", fontFamily: column.mono ? "var(--font-mono)" : undefined }}
                    >
                      {column.render ? column.render(row) : row[column.key]}
                    </td>
                  ))}
                </tr>
              ))}
          {!loading && rows.length === 0 ? (
            <tr>
              <td colSpan={Math.max(columns.length, 1)} style={{ padding: "var(--space-6) var(--space-3)", textAlign: "center", color: "var(--color-fg-muted)" }}>{emptyText}</td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}
```

> **packages/ui の追加 props（一覧用、platform #56）。** 上の参照実装に無い、業務一覧の手書き `<table>` を置き換えるための props です。すべて optional で、渡さなければ出力は変わりません。
>
> | prop | 役割 |
> |---|---|
> | `stickyHeader` | `thead` をスクロール領域の上端に固定する。罫線は th の内側（inset shadow）に持たせ、スクロールしても消えない |
> | `visibleRows` / `fillVisibleRows` | `number` か `{ base, md }`（md = 48rem 以上）。表頭 + 先頭 N 行の**実測高さ**でスクロール領域の `max-height`（fill では `height`）を決める。2 行セルで行高が変わっても N 行ちょうどが見える |
> | `scrollAriaLabel` / `scrollTestId` | スクロール領域を `role="region"` + `tabIndex=0` + フォーカスリングにする（WCAG 2.1.1 キーボードでスクロール） |
> | `selectedRowKey` | master-detail で表示中の行。`aria-current="true"` + `bg-accent-subtle` + 先頭セルの左バー（0.25rem、`--color-accent-fg`。選択を色の差だけで示さない = WCAG 1.4.1）+ `data-surface-tint="accent"`（行内の `fg-muted` / `accent-fg` を淡青面用に深くする = 4.5:1）。全行に `data-selected` |
> | `isRowSelected` | チェックボックスの複数選択。背景・左バー・`data-surface-tint` だけ付け、状態はチェックボックスが伝える |
> | `onRowClick` | マウス操作の補助。行内の button / a / input / label 等のクリックでは発火しない。キーボード用に行内の button も置く |
> | `rowProps` | 行の `className` / `aria-label` / `data-testid` |
> | `renderRowDetail` | 行の直後に全幅の補足行（分析結果など）。`visibleRows` の計測では直前の行に含める |
> | `columns[].rowHeader` | `<th scope="row">` で描画する |
> | `tableClassName` / `loadingRows` | `table-fixed` や `min-w-*`、スケルトン行数 |
>
> 並べ替えヘッダーは折り返さず（`white-space: nowrap`）、当たり判定の高さは `--button-height-sm`（タッチ端末では 44px）です。

---

## Sidebar.jsx — 変更

```jsx
import React from "react";
import { Icon } from "../core/Icon.jsx";

const reveal = (collapsed) => ({ opacity: collapsed ? 0 : 1, transition: "opacity var(--duration-reveal) var(--ease-out)" });

/**
 * Collapsible dark navigation shared by all products. Only `product` (second line of the
 * wordmark), `sections` and the account in the footer differ between RAG / NL2SQL / Agent.
 * sections: [{ key, title, collapsed?, items: [{ href, label, sidebarLabel?, icon }] }]
 * account: { name, roles } — renders SidebarAccountFooter (PROPOSED shared footer).
 */
export function Sidebar({ product, sections = [], currentPath, collapsed = false, onToggleCollapsed, onToggleSection, onNavigate, account, theme = "light", onToggleTheme, onLogout }) {
  const isActive = (href) => currentPath === href || (currentPath || "").startsWith(href + "/");
  return (
    <aside
      aria-label="サイドナビゲーション"
      data-surface="inverted"
      data-state={collapsed ? "collapsed" : "expanded"}
      style={{ display: "flex", flexDirection: "column", flexShrink: 0, height: "100%", width: collapsed ? "var(--sidebar-width-collapsed)" : "var(--sidebar-width)", overflow: "hidden", background: "var(--color-surface)", color: "var(--color-fg-muted)", fontFamily: "var(--font-sans)", transition: "width var(--duration-enter) var(--ease-out)" }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: collapsed ? "center" : "space-between", height: "var(--sidebar-header-height)", flexShrink: 0, padding: collapsed ? "0 var(--space-2)" : "0 var(--space-3)", borderBottom: "1px solid var(--color-border)" }}>
        {collapsed ? null : (
          <div title={`Production Ready ${product}`} style={{ minWidth: 0, flex: 1, padding: "0 var(--space-2)", color: "var(--color-fg)", ...reveal(collapsed) }}>
            <span style={{ display: "block", whiteSpace: "nowrap", fontSize: "var(--font-size-base)", lineHeight: "1.25rem", fontWeight: 700 }}>Production Ready</span>
            <span style={{ display: "block", whiteSpace: "nowrap", fontSize: "var(--font-size-xs)", lineHeight: "var(--line-height-xs)", fontWeight: 600, color: "var(--color-fg-muted)" }}>{product}</span>
          </div>
        )}
        <button type="button" className="pr-nav-row" onClick={onToggleCollapsed} aria-label={collapsed ? "サイドバーを展開" : "サイドバーを折りたたむ"} aria-expanded={!collapsed} style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "2.75rem", height: "2.75rem", flexShrink: 0, border: 0, borderRadius: "var(--radius-md)", background: "transparent", color: "var(--color-fg-muted)", cursor: "pointer" }}>
          <Icon name={collapsed ? "PanelLeftOpen" : "PanelLeftClose"} size={18} />
        </button>
      </div>

      <nav style={{ flex: 1, minHeight: 0, overflowX: "hidden", overflowY: "auto", padding: collapsed ? "var(--space-3) var(--space-2)" : "var(--space-3)" }}>
        {sections.map((section) => {
          const expanded = collapsed || !section.collapsed;
          const containsActive = section.items.some((item) => isActive(item.href));
          return (
            <div key={section.key} style={{ marginBottom: collapsed ? "var(--space-3)" : "var(--space-4)" }}>
              {collapsed ? null : (
                <button type="button" className="pr-nav-row" aria-expanded={expanded} onClick={() => onToggleSection && onToggleSection(section.key)} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "var(--space-2)", width: "100%", padding: "var(--space-1) var(--space-3)", border: 0, borderRadius: "var(--radius-md)", background: "transparent", color: "var(--color-fg-subtle)", font: "var(--font-weight-semibold) var(--font-size-xs) / var(--line-height-xs) var(--font-sans)", letterSpacing: "0.025em", cursor: "pointer" }}>
                  <span style={{ display: "flex", alignItems: "center", gap: "var(--space-1-5)", minWidth: 0 }}>
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{section.title}</span>
                    {!expanded && containsActive ? <span aria-hidden="true" style={{ width: 6, height: 6, borderRadius: "var(--radius-pill)", background: "var(--color-accent-emphasis)" }} /> : null}
                  </span>
                  <Icon name="ChevronDown" size={14} style={{ transform: expanded ? "none" : "rotate(-90deg)", transition: "transform var(--duration-enter) var(--ease-out)" }} />
                </button>
              )}
              {expanded ? (
                <ul style={{ margin: 0, padding: collapsed ? 0 : "var(--space-1) 0 0", listStyle: "none", display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
                  {section.items.map((item) => {
                    const active = isActive(item.href);
                    return (
                      <li key={item.href}>
                        <a
                          href={item.href}
                          className="pr-nav-row"
                          aria-current={active ? "page" : undefined}
                          aria-label={collapsed ? item.label : undefined}
                          title={item.label}
                          onClick={onNavigate ? (event) => { event.preventDefault(); onNavigate(item.href); } : undefined}
                          style={{ position: "relative", display: "flex", alignItems: "center", justifyContent: collapsed ? "center" : "flex-start", gap: "var(--space-2-5)", height: "var(--nav-item-height)", padding: collapsed ? 0 : "0 var(--space-3)", overflow: "hidden", borderRadius: "var(--radius-md)", color: "inherit", textDecoration: "none", font: "var(--text-body)" }}
                        >
                          {active ? <span aria-hidden="true" style={{ position: "absolute", left: 0, top: "50%", width: "0.25rem", height: "1.25rem", transform: "translateY(-50%)", borderRadius: "0 9999px 9999px 0", background: "var(--color-fg)" }} /> : null}
                          <Icon name={item.icon} size={18} />
                          {collapsed ? null : <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.sidebarLabel || item.label}</span>}
                        </a>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </div>
          );
        })}
      </nav>

      {account ? <SidebarAccountFooter account={account} collapsed={collapsed} theme={theme} onToggleTheme={onToggleTheme} onLogout={onLogout} /> : null}
    </aside>
  );
}

/** PROPOSED shared footer: user + roles, logout, theme toggle. Replaces three app-specific footers. */
export function SidebarAccountFooter({ account, collapsed = false, theme = "light", onToggleTheme, onLogout }) {
  const row = { display: "flex", alignItems: "center", gap: "var(--space-2-5)", height: "var(--nav-item-height)", border: 0, borderRadius: "var(--radius-md)", background: "transparent", color: "inherit", font: "var(--text-body)", cursor: "pointer" };
  return (
    <div style={{ borderTop: "1px solid var(--color-border)", padding: collapsed ? "var(--space-3) var(--space-2)" : "var(--space-3)" }}>
      {collapsed ? null : (
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2-5)", minHeight: "var(--nav-item-height)", padding: "var(--space-2) var(--space-3)" }}>
          <Icon name="UserRound" size={18} />
          {/* 1 行目はログイン ID（local の DEBUG は表示名。#1696）、2 行目はロールの表示名（code ではない）。各行は 1 行で省略記号で切り、全文は各行の title（#1644）。
              容れ物は flex: 1 + minWidth: 0、外側の grid の列は minmax(0, 1fr)（auto の列は名前の全幅まで広がり、省略記号が出ずに切れる）。 */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div title={account.name} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", font: "var(--text-label)", color: "var(--color-fg)" }}>{account.name}</div>
            {account.roles ? <div title={account.rolesLabel ?? account.roles} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", font: "var(--text-meta)", color: "var(--color-fg-subtle)" }}>{account.roles}</div> : null}
          </div>
        </div>
      )}
      <div style={{ display: "flex", flexDirection: collapsed ? "column" : "row", gap: "var(--space-1)" }}>
        <button type="button" className="pr-nav-row" onClick={onLogout} aria-label="ログアウト" style={{ ...row, flex: 1, justifyContent: collapsed ? "center" : "flex-start", padding: collapsed ? 0 : "0 var(--space-3)" }}>
          <Icon name="LogOut" size={18} />
          {collapsed ? null : <span>ログアウト</span>}
        </button>
        <button type="button" className="pr-nav-row" onClick={onToggleTheme} aria-label={theme === "dark" ? "ライトテーマに切り替え" : "ダークテーマに切り替え"} style={{ ...row, justifyContent: "center", width: collapsed ? "100%" : "var(--nav-item-height)", padding: 0, border: "1px solid var(--color-border)" }}>
          <Icon name={theme === "dark" ? "Sun" : "Moon"} size={18} />
        </button>
      </div>
    </div>
  );
}
```

---

## AppShell.jsx — 変更

```jsx
import React from "react";

/** Full-height shell: Sidebar slot on the left, one scrolling main region. Required in every app. */
export function AppShell({ sidebar, children, style }) {
  return (
    <div style={{ display: "flex", width: "100%", height: "100vh", overflow: "hidden", position: "relative", background: "var(--color-canvas)", color: "var(--color-fg)", fontFamily: "var(--font-sans)", ...style }}>
      <a className="pr-skip-link" href="#pr-main">本文へスキップ</a>
      {sidebar}
      <main id="pr-main" tabIndex={-1} style={{ display: "flex", minWidth: 0, flex: 1, flexDirection: "column", overflowY: "auto" }}>{children}</main>
    </div>
  );
}
```

### md 未満のナビのドロワー（#367。案 A に決定）

上の参照実装は md 以上の形です。`packages/ui` の `AppShell` は、md 未満（`NAV_DRAWER_QUERY` = `(max-width: 767px)`）で次の形に切り替えます。製品のコードは変えません（`sidebar` スロットの `Sidebar` をそのまま渡す）。

```tsx
<div data-nav-mode="drawer" className="flex h-screen flex-col">          {/* md 未満だけ縦並び */}
  <a className="pr-skip-link" href="#pr-main" inert={open}>本文へスキップ</a>
  <div data-surface="inverted" data-testid="nav-drawer-bar">              {/* 本文のスクロールの外。landmark にしない */}
    <Button variant="ghost" iconOnly touchTarget icon={Menu}
      aria-label="メニュー" aria-expanded={open} aria-controls={drawerId} />
    {/* 製品名（Sidebar の title を context で受け取る） */}
  </div>
  <main id="pr-main" tabIndex={-1} inert={open}>{children}</main>
  <div aria-hidden className="fixed inset-0 z-[var(--z-scrim)] bg-[var(--scrim)]" onClick={close} />
  <div id={drawerId} role="dialog" aria-modal="true" aria-label="メニュー" inert={!open}
    className="fixed inset-y-0 left-0 z-[var(--z-dialog)] …">                {/* Escape / Tab の閉じ込め / リンクのクリックで閉じる */}
    {sidebar}                                                             {/* 展開して描き、折りたたみボタンの代わりに「メニューを閉じる」 */}
  </div>
</div>
```

| 振る舞い | 内容 |
|---|---|
| 開く | 「メニュー」ボタン。ドロワーの閉じるボタンへフォーカス |
| 閉じる | 閉じるボタン・Escape・scrim のタップ・ナビのリンクの選択（今のページでも）・`currentPath` の変化。閉じたら「メニュー」ボタンへフォーカスを戻す（画面を移ったときは製品の画面遷移のフォーカスが本文へ移す）。離脱の確認をキャンセルしたときは開いたまま |
| フォーカス | Tab / Shift+Tab はドロワーの中で回る。背面は `inert`（本文へスキップ・上端のバー・`<main>`） |
| スクロール | 開いている間は `<main>` のスクロールを止め、scrim は `touch-action: none`、ドロワーのナビは `overscroll-behavior: contain` |
| モーション | transform 200ms ease-out。`prefers-reduced-motion` では動かさない |
| 状態 | ドロワーの開閉は保持しない（画面を開いた時は閉じている）。ui-store の `sidebarCollapsed`（md 以上の選好）は変えない |

| props / export | 型 | 説明 |
|---|---|---|
| `navDrawerLabels` | `{ menu: string; close: string }` | 既定 `DEFAULT_NAV_DRAWER_LABELS`（「メニュー」「メニューを閉じる」）。翻訳済みの文言で上書きする |
| `useSidebarCollapsed(collapsed)` | `(boolean) => boolean` | サイドバーの `footer` に置く部品が使う。ドロワーの中では `false`（展開）。`SidebarAccountFooter` と `SidebarAccountSection` は対応済み |
| `NAV_DRAWER_QUERY` | `string` | ドロワーにする幅のメディアクエリ |

Playwright では、`data-testid` の `nav-drawer-bar` / `nav-drawer-trigger` / `nav-drawer` / `nav-drawer-close` / `nav-drawer-scrim` か、role（`button` の「メニュー」、`dialog` の「メニュー」）で操作します。md 未満でサイドナビのリンクを押す spec は、先に「メニュー」を開きます（各製品の e2e の `openSidebarNav(page)`。RAG は `e2e/_helpers.ts`、NL2SQL は `tests/e2e/_helpers/sidebar-nav.ts`、Agent は `e2e/fixtures/nav.ts`）。

---

## SecretField — **新規**（#296）

API key・パスワード・token など、**保存済みの値を画面に出さない** secret の入力欄です。システム設定のモデル設定（API key）・データベース設定（DB パスワード / Wallet パスワード）・RAG の HuggingFace 設定（token）が別々に持っていた実装を `packages/ui` にまとめました。製品で secret の入力欄を再実装しないでください。

| 決めたこと | 理由 |
|---|---|
| ラベルの行の右端に「保存済み / 未設定」を `StatusBadge`（`success` / `neutral`、アイコン付き）で出す | 保存済みかどうかは値を見せずに伝える必要がある。色だけに頼らない（`StatusBadge` の規約） |
| 入力欄は `TextField` と同じ高さ（`--field-height`）・枠線・フォーカス表示。既定はマスク（`type="password"`）、`autoComplete="off"`・`spellCheck={false}` | 2 列の grid で隣の `TextField` と上端・高さをそろえる。secret をブラウザの補完・スペルチェックへ渡さない |
| 表示の切り替えは入力欄の直後の共有 `Button`（`ghost` + `iconOnly`、`Eye` / `EyeOff`）。`aria-label` は「〜を表示 / 〜を隠す」 | アイコンだけのボタンには `aria-label` が要る。`#id + button` で引ける構造を 3 製品の E2E が使う |
| 保存済みの値をサーバーから取り出す間（`revealPending`）は、切り替えが `loading`（共有 `Spinner`）になり押せない | ボタンの loading の規約と同じ。取得中の `aria-label`（例:「DB パスワードを取得中」）は `revealPendingLabel` で渡す |
| 保存済みの値の削除は `clearOption` のチェックボックスで、**入力欄の直下**に出す（保存済みのときだけ）。指定中は入力欄と切り替えを無効にする | 削除の指定と対象の入力欄を近くに置く。削除と新しい値の入力を同時に受け付けない |
| 削除の指定・取り出し・保存は呼び出し側が持つ | `packages/ui` は API と業務語彙を知らない。文言はすべて翻訳済みの文字列で渡す |

```tsx
<SecretField
  id="enterprise-api-key"
  label="API key"
  value={draft.api_key}
  onValueChange={(value) => update("api_key", value)}
  visible={apiKeyVisible}
  onVisibleChange={setApiKeyVisible}
  hasSavedSecret={draft.has_api_key}
  savedLabel="保存済み"
  notSetLabel="未設定"
  showLabel="API key を表示"
  hideLabel="API key を隠す"
  helper="OpenAI-compatible gateway の Bearer 認証で使います。"
  clearOption={{ label: "保存済み API key を削除する", checked: draft.clear_api_key, onCheckedChange: updateClear }}
/>
```

### SecretField の props

```ts
import type { ReactNode, Ref } from "react";

export interface SecretFieldClearOption {
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}

export interface SecretFieldProps {
  id: string;
  label: string;
  value: string;
  onValueChange: (value: string) => void;
  hasSavedSecret: boolean;
  savedLabel: string;
  notSetLabel: string;
  showLabel: string;
  hideLabel: string;
  /** 省略すると内部の状態で切り替える。 */
  visible?: boolean;
  onVisibleChange?: (visible: boolean) => void;
  revealPending?: boolean;
  revealPendingLabel?: string;
  revealError?: string | null;
  helper?: ReactNode;
  error?: string;
  placeholder?: string;
  /** aria-required と RequiredBadge。required だけで「必須」を出す（requiredLabel は条件付きの必須の文言だけ上書き。#531）。 */
  required?: boolean;
  requiredLabel?: string;
  disabled?: boolean;
  /** hasSavedSecret のときだけ表示する。 */
  clearOption?: SecretFieldClearOption;
  autoComplete?: string;
  className?: string;
  ref?: Ref<HTMLInputElement>;
}

/** @dsComponent */
export declare function SecretField(props: SecretFieldProps): JSX.Element;
```

- 補足・エラー・取り出しの失敗の id は `${id}-hint` / `${id}-error` / `${id}-reveal-error`、削除のチェックボックスは `${id}-clear` です。
- 保存の操作行は `FormActionBar`（UX 契約 buttons §5.2.1）に置きます。`<form>` の中の保存は、`FormActionDescriptor` の `type: "submit"` で form の送信（Enter による暗黙の送信と `onSubmit` の検証）をそのまま使えます（既定は `type="button"`）。

---

## Toaster / toast / Banner — 変更（#351 / #411 / #1728）

一時通知（`<Toaster/>` + `toast`）の表示時間と一時停止、Banner の閉じるボタンを UX 契約 messaging §3.1 にそろえました。製品で表示時間を補ったり、`toast.error` のラッパーを作ったりしないでください。

| 決めたこと | 理由 |
|---|---|
| 既定の表示時間は success / info / warning が 4 秒、danger（`toast.error`）が 8 秒。ホバー・フォーカス中は止まる。`duration` を渡せば上書きできる | messaging §3.1。#351 で danger を閉じるまで残したが、画面を移っても古いエラーが残り続けたため 8 秒に戻した（#2470）。対処が続く失敗は固定の面に出す |
| `action` 付きの通知は自動では消えない（従来どおり） | 「元に戻す」を押す前に消さない |
| いずれかの通知にポインタが乗っている間、または通知の中にフォーカスがある間は、**すべての**通知の自動消滅を止め、離れたら残り時間から再開する | 読んでいる・押そうとしている途中で消さない（WCAG 2.2.1 の考え方、Radix / Sonner の通例）。通知ごとに止めると、下の通知が消えて読んでいる通知の位置がずれる |
| 残り時間は store（`useToastStore`）が持つ。`pause()` / `resume()` と `paused` を公開する | 表示の部品を作り直しても残り時間が失われない。単体テストでタイマーを検証できる |
| ホバー・フォーカス中の通知が閉じられて DOM から外れたときは、その通知の一時停止を解除する | 外れた要素からは `pointerleave` / `blur` が届かず、止まったままになるのを防ぐ |
| Banner の閉じる × は Toast と同じ共有 `Button`（`ghost`・`iconOnly`・`touchTarget`・`X` 16px）。色はトーンの文字色（`text-current`）、ホバーは `fg` の薄い重ね | 手書きのボタンをやめ、フォーカスリング・当たり判定（44px）・強制カラーの輪郭を共有部品にそろえる |
| Banner の `progress` は `action` の直下（同じ Banner の中）に出す、その操作で始めた処理の進行（`ProcessingIndicator` など。#2031） | 案内の中のボタン（「DB 構造を再取得」など）で始めた処理の経過時間を、押したボタンから離れたページの先頭・末尾に出さない（messaging §10.1「起点の直下」）。ボタンは送信の間だけ `loading`、durable job の間は `disabled` にし、スピナーは進行の表示が 1 つだけ出す（§3.7） |

```tsx
// 製品のアプリの最上位で一度だけ
<Toaster dismissLabel={t("common.close")} regionLabel={t("common.notifications")} />

toast.success(t("profiles.message.saved"));                       // 4 秒
toast.error(error instanceof ApiError ? error.message : t("…"));  // 8 秒で消える
toast.success(t("…deleted"), { action: { label: t("common.undo"), onClick: undo } }); // 自動では消えない
```

### 置き場所（#1728。#411 を置き換え）

`<Toaster/>` は画面幅ごとに決まった 1 か所に通知を出し、ページの内容では動かしません。製品・画面で位置を変えないでください（`placement` プロップは無い）。規則の正本は UX 契約 messaging §3.1、見た目の決定は README §4「Toaster」。

| 決めたこと | 理由 |
|---|---|
| md 以上は画面の右下（右端から 1rem、下端から `--toast-offset-bottom` = 8rem）。新しい通知は下に足し、古い通知が上へ押し上げられる。幅は内容に合わせて `--toast-width-min` 22rem〜`--toast-width-max` 32rem（#899） | 位置を 1 か所に固定する（Sonner・Fluent 2 の既定と同じ）。以前は `PageHeader` の操作の位置で見出しの中ほどか右端かが変わった。8rem はページの末尾の操作（約 7rem まで）の上。下端に固定されたチャットの入力欄（3.5 行で下端から約 10rem）がある画面は、`ChatLayout` が測った入力欄の上端から 1rem 上（`--toast-clear-bottom`。#2103） |
| md 未満は上端の全幅。上端のバーに重ねて上端から 0.5rem、左はメニューのボタンの後ろ（`calc(1rem + var(--control-height-touch))`） | 375px の下端には全幅の末尾の操作・チャットの入力欄・ソフトウェアキーボードが来る。上端のバーにはメニューのボタンのほかに操作が無い |
| 位置は画面幅（`useNavDrawerMode`）だけで決め、`PageHeader` の位置・スクロールは読まない（`resolveToastPlacement({ narrow })`） | 画面ごとの位置の計算をやめる（#1728） |
| 通知の領域に `data-toast-placement`（`bottom-right` / `top-bar`） | E2E と目視で、どの規則で置いたかを確かめられる |
| 下端の通知は下から、上端の通知は上から現れる（`toast-in` の向きは領域の `--toast-enter-y`） | 置き場所の側から現れる |
| 下端からの距離は共通の定数 `--toast-offset-bottom` だけで決め、画面ごとの余白・位置の上書きをしない | 主操作と重なる画面が出たら、定数か画面の作りを直す（製品で通知の位置を変えない） |

E2E は 3 製品の同じ内容の helper（RAG `e2e/_toast-placement.ts`・Agent `e2e/fixtures/toast-placement.ts`・NL2SQL `tests/e2e/_helpers/toast.ts`）で確かめます。`expectToastPlacement` は規則どおりの位置（desktop は右下、375px は上端のバー）を、`expectMainActionsClearOfToast` は通知の範囲（最も広い通知・本文と補足の 1 件）が主操作（`PageHeader` の操作・`FormActionBar`・`ObjectActionBar`・チャットの入力欄と送信・ページの末尾の操作）と重ならないことを確かめます（3 製品の `a11y.nightly.spec.ts` がサイドナビの全項目で実行）。通知を閉じてから押す回避（`dismissToasts`）は要りません。

メッセージの部品（`Toast`・`Banner`・`FormStatus`・`ProcessingIndicator`・`BlockedPageNotice` の面と `MessageText`）は、共有の CSS の `.pr-message-text`（`word-break: auto-phrase`・`overflow-wrap: anywhere`・`text-wrap: pretty`）で日本語を文節で折り返します（#899。README §4「メッセージの本文の折り返し」）。製品でメッセージに `word-break` などを書かないでください。語の途中で折り返さないことは NL2SQL の `tests/e2e/message-wrapping.spec.ts`（文字ごとの描画位置から折り返しの位置を求め、`Intl.Segmenter` の語の境界と比べる）で確かめます。

### toast の API

```ts
export interface ToastOptions {
  description?: string;
  action?: { label: string; onClick: () => void };
  /** 自動消滅までの ms。0 で自動消滅しない。未指定はトーンの既定（danger は 0、他は 4000）。 */
  duration?: number;
}

export declare const toast: {
  success(message: string, options?: ToastOptions): string;
  info(message: string, options?: ToastOptions): string;
  warning(message: string, options?: ToastOptions): string;
  error(message: string, options?: ToastOptions): string; // danger
  dismiss(id: string): void;
};

// useToastStore.getState(): { toasts, paused, push, dismiss, clear, pause, resume }
```

---

## BlockedPageNotice — **新規**（#325）

ページ全体が使えない（ブロック状態）ときに、業務画面の代わりに主領域の中央へ出す案内カードです。RAG と NL2SQL の DB ゲートが別々に手書きしていた見た目を `packages/ui` にまとめました（3製品の DB ゲートは `@production-ready/system-settings` の `DatabaseGate` がこれを使います）。製品で全画面の案内カードを再実装しないでください。

| 決めたこと | 理由 |
|---|---|
| カードは `section` + `aria-labelledby`（見出しの `h1` の id）。見出しの id は呼び出し側が固定値で渡す | 支援技術がカードを見出しの名前で読み上げる。E2E が `aria-labelledby` でカードを引く |
| 見出しの上に 24px のアイコンを丸（トーンの `*-subtle` の面 + `*-fg` の色）で出し、`aria-hidden` にする。既定のトーンは `warning` | 色だけで意味を伝えない（状況は見出しと本文で書く）。エラー画面（danger）にせず、落ち着いた案内にする |
| 本文 → 補足（診断コードなど）→ 操作 → 区切り線の下の補足の順。渡したものだけを描く | 原因 + 次の行動を上に置く（UX 契約 messaging §0 / §9）。空の面を描かない |
| 操作の行は中央ぞろえで折り返す。主操作（設定を開くリンク等）を先頭に置く | 375px でも横にはみ出さない。Tab 順が見た目の順と同じ |
| 文言はすべて翻訳済みの文字列で渡す | `packages/ui` は i18n を持たない |

```tsx
<BlockedPageNotice
  title="データベースに接続できません"
  titleId="database-unavailable-title"
  icon={Unplug}
  message="データベースにネットワーク経由で到達できないか、接続設定が正しくありません。データベース設定で接続ユーザー・パスワード・サービス名（DSN）と Wallet を確認してから、再試行してください。"
  details={<p role="status">診断コード: wallet_not_found</p>}
  actions={
    <>
      <ButtonLink to="/settings/database" linkComponent={Link} variant="primary" icon={Settings} trailingIcon={ArrowRight}>データベース設定を開く</ButtonLink>
      <Button variant="secondary" icon={RefreshCw} onClick={retry}>再試行</Button>
    </>
  }
  footer="アップロード保存先・モデル・データベース・外観と証明書の各設定ページは引き続き利用できます。"
/>
```

### BlockedPageNotice の props

```ts
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

export interface BlockedPageNoticeProps {
  title: string;
  titleId: string;
  icon: LucideIcon;
  /** 既定は "warning"。 */
  tone?: "success" | "info" | "warning" | "danger";
  message?: ReactNode;
  details?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  testId?: string;
  className?: string;
}

/** @dsComponent */
export declare function BlockedPageNotice(props: BlockedPageNoticeProps): JSX.Element;
```

---

## SelectField — 変更（#352）

選択肢の一覧を **body へ Portal で描く**ようにしました。親の `overflow: hidden / auto`（`DataTable` のセル・モーダル・カード・スクロール枠）で切れず、画面の下端では上に反転します。位置の計算は `FloatingActionMenu` と同じ `useFloatingMenuPosition`（`floating-menu.tsx`）を共有します。props・id・aria は変えていません（`id` / `${id}-option-${index}` / `aria-controls` / `aria-activedescendant` / `aria-labelledby`）。

| 決めたこと | 理由 |
|---|---|
| 一覧は `position: fixed` で body に出し、トリガーと同じ幅・左端にそろえる。開いている間はスクロール（どの祖先でも）とリサイズに追従する | 親の overflow に切られない。375px でも画面の外にはみ出さない（左右 8px の余白で止める） |
| 下に 16rem（`max-h-64` と同じ）が入らず、上のほうが広ければ上に反転する。どちらにも入らなければ広い側に出し、空きの高さまで縮めて内部スクロールにする。反転の向きは `data-floating-menu-placement`（`top` / `bottom`） | 画面の下端のフィールドでも選択肢が見える。E2E が向きを確かめられる |
| モーダル（`aria-modal="true"` / `<dialog open>`）の中で開いたときは、body ではなくそのモーダルの中に描く | モーダルの外に出すと、支援技術がモーダルの外として読まない（aria-modal）・`<dialog>` の top layer の下に隠れる |
| z-index を持つ層（モーダル・固定ヘッダーなど）の中では、その層より 1 段上に出す。層の外は `--z-dropdown` のまま | Portal で描くと、`--z-dropdown` のままではモーダルの暗幕や層の下に隠れる |
| 外側クリックの判定は、フィールド本体と一覧の両方を内側とみなす。一覧を押してもフォーカスはボタンに残す | 一覧は DOM 上フィールドの外にある。スクロールバーや余白を押しても閉じない |
| 選択は `click` で確定する（以前は `pointerdown`） | タッチで一覧をスクロールしただけで選ばれないようにする。Portal でも React のイベントは祖先に伝わるため、一覧のクリックは表の行などへ伝えない |
| 矢印キー・Home / End・PageUp / PageDown（10 件）・typeahead で強調を動かすと、強調中の選択肢を一覧の中で見せる（`scrollIntoView({ block: "nearest" })` と同じ動き）。マウスの移動ではスクロールしない | 7 件目以降が枠の外に出たままにならない。`scrollIntoView` は一覧の外（ページ本体）まで動かすので、一覧の `scrollTop` だけを動かす |
| typeahead（W3C APG の select-only combobox）: 文字を打つと、その文字で始まる選択肢を強調する（閉じていれば開く）。続けて打つと前方一致で絞り込み、同じ文字の繰り返しは順に巡る。500ms 入力がなければ入力をリセットする。大文字小文字・全角半角は区別しない | キーボードで長い一覧（リージョン・評価指標など）を選べる。Space は従来どおり選択・開閉に使う |
| 一覧が開いているときの Esc は一覧だけを閉じ、囲むモーダルには伝えない | Esc 1 回でモーダルごと閉じない（APG: Esc は一覧を閉じる） |

- E2E で一覧を引くときは、フィールドを囲む要素ではなく `page.getByRole("listbox", { name: "<ラベル>" })` で引きます（一覧は body の直下、モーダルの中ならモーダルの直下にあります）。
- 一覧は `position: fixed` です。`transform` / `filter` / `contain` を持つ要素（固定の containing block）をモーダルの外枠にすると位置がずれるため、モーダルの中央寄せは flex で行います（`ConfirmDialog` と同じ）。
- 純粋関数 `computeFloatingMenuLayout`（位置と反転）・`floatingLayerZIndex`（重なり順）・`selectPortalContainer`（描く先）・`findTypeaheadIndex` / `typeaheadStartIndex`（typeahead）・`nearestScrollTop`（強調中の選択肢のスクロール）・`isInsideAny`（外側クリック）は `packages/ui/tests/select-field.test.tsx` が確かめます（操作メニューの左右の反転は `packages/ui/tests/floating-menu.test.ts`）。パッケージのルートからは export しません。

### SelectField — `disabled` / `labelHidden` / `data-testid`（#631）

製品のネイティブの `<select>` を置き換えるために、次の props を足しました（既存の props・id・aria は変えていません）。

```tsx
<SelectField
  id="relation-source-key"
  label={t("expression.sourceKey")}
  value={key}
  options={columnOptions}
  onValueChange={setKey}
  disabled={relationSource !== "MANUAL"}   // 無効
  labelHidden                              // 表の行などで、ラベルを読み上げだけにする
  data-testid="relation-source-key"        // ボタン（role=combobox）に付く
  size="sm"
/>
```

| 決めたこと | 理由 |
|---|---|
| `disabled` はボタンのネイティブの `disabled`（`aria-disabled` ではない）。押しても・矢印キー・Enter / Space・typeahead でも開かない。開いているときに無効になったら一覧を閉じる | 無効な選択欄は操作できないので Tab で止めない（ネイティブの select と同じ）。`Button` の `loading` と違い、フォーカス中に無効になる流れ（押した直後の処理中）が無い |
| 無効の地・文字は `--color-surface-disabled` / `--color-fg-disabled`、シェブロンも `--color-fg-disabled`、カーソルは `not-allowed`。ホバーの地は有効なときだけ（`enabled:hover:`）。枠線は残す | `TextField` の無効と同じ見た目にし、押せそうに見せない。欄の形は残して、並んだ欄と位置がそろう |
| `labelHidden` はラベルを `sr-only` にし、`aria-labelledby` で読み上げ名は残す | 表の行・一覧のツールバーのように周りで目的が分かる所だけに使う（`TextField` の `labelHidden` と同じ）。フォームの欄では使わない |
| ボタンと選択肢（`role=option`）に値の `data-value` を出す | e2e が値で選び（`selectOption` の代わり）、選択中の値を確かめられる（`toHaveValue` の代わり）。表示には使わない |
| `SearchableSelectField` の無効も同じ見た目・動き（ホバーは有効なときだけ、シェブロンと文字の色、開いているときに無効になったら閉じる）にそろえた | 2 つの選択欄で無効の見え方が違わないように |

- ネイティブの `<select>` は adherence の lint が検出します。`<optgroup>` や選べない選択肢（`<option disabled>`）が要るなど `SelectField` で表せない所だけ、理由を添えて局所的に除外し、`fieldControlClassName({ size, width })` で見た目をそろえます。
- e2e は combobox を押して一覧の選択肢を選びます（NL2SQL `tests/e2e/_helpers/select-field.ts`・Agent `e2e/fixtures/select-field.ts` の `chooseSelectFieldOption(combobox, "値" | { label })` / `expectSelectFieldValue`）。
- 単体テストは `packages/ui/tests/select-field-disabled.test.tsx`。

### SelectField — `emptyOptionLabel` / `describedBy`（#647）

任意の選択欄を未選択へ戻す選択肢と、欄の外の説明への `aria-describedby` を足しました（既存の props・id・aria は変えていません）。

```tsx
<SelectField
  id="scope-filter-column"
  label={t("scopeFilterColumn")}
  value={columnName}                         // 未選択は ""（値の型が "" を含むときだけ emptyOptionLabel を渡せる）
  placeholder={t("scopeFilterColumnPlaceholder")}   // 未選択のときのボタンの文言（例:「列を選択」）
  emptyOptionLabel={t("unselected")}         // 一覧の先頭の「未選択」。選ぶと onValueChange("")
  options={columnOptions}
  onValueChange={setColumnName}
/>

<SelectField id="dataset" label={t("dataset")} value={dataset} options={datasets}
  describedBy="dataset-description"          // 右に置いた説明の id（helper・error の id の後ろに足す）
  onValueChange={setDataset} />
<p id="dataset-description">{t(`dataset.${dataset}.description`)}</p>
```

| 決めたこと | 理由 |
|---|---|
| 未選択へ戻す手段は、消去のボタンではなく**一覧の先頭の空の値の選択肢**（`emptyOptionLabel`） | W3C APG の select-only combobox は、ボタンの中に別の操作（消去のボタン）を入れない（Tab の止まり・読み上げ名が増える）。ネイティブの select の `<option value="">` と同じ操作で、↑↓・Home / End・typeahead・Enter・読み上げがそのまま効く。`SearchableSelectField` には消去の手段が無く、そろえる既存の形は無かった。値の選択肢と区別できるよう文字は `--color-fg-muted`（選択中・強調中は他と同じ） |
| 未選択のあいだ、ボタンは `placeholder`（無ければ `emptyOptionLabel`）を控えめの色で出す。`data-value` は `""` | 「列を選択」のように何を選ぶ欄かを見せ続ける。e2e は `chooseSelectFieldOption(combobox, "")` で未選択を選べる |
| `required` の欄と、`options` に空の値が既にある欄（「すべて」「既定の Binding」など、空の値が意味を持つ選択肢）には出さない | 必須の欄を未選択へ戻しても送信の検証で止まるだけ（誤りは別の値を選び直す）。空の値を 2 つ並べない |
| 値の選択肢が 0 件のあいだは開かない（「未選択」だけの一覧を出さない） | 候補の取得中に開いて空の一覧に見えるのを防ぐ（既存の動きのまま） |
| `emptyOptionLabel` の型は `"" extends T ? string : never` | 空の値を取れない列挙（`SelectField<"hr" \| "sales">` など）に渡すと型エラーにし、`onValueChange` に想定外の `""` が届かないようにする |
| `describedBy` は `helper`・`error` の id の後ろに足して `aria-describedby` に渡す（`SearchableSelectField` も同じ） | `TextField` の `aria-describedby` と同じ順。欄の横・下に置いた説明（NL2SQL のサンプルデータの種類の説明）を読み上げる |

- 使っている所: NL2SQL の Deep Data Security（`ScopeExpressionEditor` の「列」・関連テーブル条件の「候補を選ぶ Profile」「関連テーブル」「対象テーブルの列」「関連テーブルの列」）、`describedBy` は NL2SQL のサンプルデータの種類。Agent の Run の「実行先 Binding」は既定が無いとき必須なので出さない。
- 単体テストは `packages/ui/tests/select-field-empty-option.test.tsx`。

### SelectField — 選べない選択肢（`disabled` / `disabledReason` / `badge`）と `RoadmapBadge`（#3374）

準備中（ロードマップ）の機能など、まだ選べない選択肢を隠さずに灰色で出す形を足しました（既存の props・id・aria は変えていません）。

```tsx
import { DEFAULT_ROADMAP_LABEL, RoadmapBadge, SelectField } from "@production-ready/ui";

<SelectField id="engine" label={t("engine")} value={engine} onValueChange={setEngine}
  options={[
    { value: "docling", label: "Docling" },
    { value: "planned", label: "Engine B", disabled: true,
      badge: DEFAULT_ROADMAP_LABEL, disabledReason: t("engine.planned.reason") },
  ]} />

<RoadmapBadge />   {/* カード・一覧の行で同じ印を出す（既定の文言は DEFAULT_ROADMAP_LABEL） */}
```

| 決めたこと | 理由 |
|---|---|
| 選べない選択肢は一覧から消さず、灰色の文字（`--color-fg-disabled`）と禁止のカーソルで出し、`aria-disabled="true"` を付ける | 何が無いのかを伝える（隠すと「無い」のか「探せない」のか分からない）。W3C APG の listbox の無効の選択肢と同じく、↑↓ で強調して読み上げられるが、クリック・Enter・Space では選ばれない（一覧も閉じない） |
| 理由（`disabledReason`）は選択肢の下の行に、読める濃さ（`--color-fg-muted`）で折り返して出す | 一覧の中では Tooltip を出せない（`Tabs` の `disabledReason` は `title`。ここでは常に見せる）。灰色の文字だけでは 4.5:1 にならないため、理由の行は灰色にしない |
| `badge` は中立色の `StatusBadge`（アイコンなし）。カード・一覧の行の印は `RoadmapBadge`（中立色・`CalendarClock`） | 状態を色だけで表さない（文字で「準備中（ロードマップ）」）。3 製品で同じ語にそろえるため、既定の文言 `DEFAULT_ROADMAP_LABEL` を `packages/ui` が持つ（`DEFAULT_TAB_INVALID_LABEL` と同じ） |
| 保存済みの値が選べない選択肢でも、閉じたボタンはそのラベルを出す | 以前に保存した値を消さずに見せ、別の値を選び直させる |

- 使っている所: RAG の処理レシピの「文書解析」（OCI Document Understanding）。`RoadmapBadge` は RAG の文書解析の使用エンジンのカードとサービス管理の行。
- 単体テストは `packages/ui/tests/select-field.test.tsx`、動きは RAG の e2e（`document-processing-config.spec.ts`）。

---

## PageHeader の「その他の操作」メニュー — 変更（#363）

`lg` 未満で主操作以外をまとめる「その他の操作」メニューを、ヘッダーの中の `absolute right-0` から **`FloatingActionMenu`（body へ Portal・`position: fixed`）** に変えました。375px では操作がタイトルの下に折り返して「その他の操作」が左端に来るため、右端揃えのままだとメニューが画面の左外に切れていました。props・`data-testid="page-actions-more"`・aria（`aria-haspopup` / `aria-expanded` / `aria-controls` / `role="menu"` / `aria-label`）は変えていません。

| 決めたこと | 理由 |
|---|---|
| 左右はボタンの右端にそろえる（従来の位置）。右端揃えで画面（左右 8px の余白の内側）に入らず、左端揃えなら入るときは左端にそろえる。どちらも入らなければ画面の内側にずらす。そろえた端は `data-floating-menu-align`（`start` / `end`） | ボタンがどこにあっても全項目が見えて押せる。E2E が向きを確かめられる |
| 上下は `SelectField` と同じ（下に入らなければ上に反転、どちらも入らなければ広い側で内部スクロール） | 位置の計算は `computeFloatingMenuLayout` 1 か所にまとめる |
| 左右の反転は `FloatingActionMenu` を使うすべてのメニュー（`RowActionMenu`・`ObjectActionBar`・`FormActionBar`）に効く。`SelectField`（`stretch`）は反転しない | 同じ問題を同じ仕組みで直す。一覧はトリガーと同じ幅なので反転の必要がない |
| 外側クリックの判定は、ボタン側とメニューの両方を内側とみなす | メニューは DOM 上ヘッダーの外にある |
| Tab はメニューを閉じてボタンの次の要素へ、Shift+Tab はボタンへ戻す。開く・矢印 / Home / End・Escape（ボタンへ戻る）は従来どおり | メニューは body の末尾にあるため、既定の Tab では文書の末尾へ抜けてしまう |

- E2E でメニューを引くときは、ヘッダーの中ではなく `page.getByRole("menu", { name: "その他の操作" })` で引きます（メニューは body の直下にあります）。

---

## ToggleChip / Switch — 変更（#364）

タッチ端末（`pointer: coarse`）でだけ、当たり判定を 44px（`--control-height-touch`）以上に広げました。見た目の大きさ・props・aria は変えていません。仕様は README §4「タッチ端末の当たり判定」。

| 決めたこと | 理由 |
|---|---|
| 両部品の class に `pr-touch-target relative` を付け、`structure/touch-target.css` の `@media (pointer: coarse)` の中だけで擬似要素を作る | Button（`--button-height-*`）と同じく、画面幅ではなく入力方式で判定する。マウス環境は変えない（#338 のレビューで見送り） |
| `::before` を `inset: min(0px, (100% - 44px) / 2)` で見た目の中心から広げる（縦は高さ、横は幅に対する %）。44px 以上の辺は広げない | チップ（約 26px）は縦に約 9px ずつ、スイッチ（44 × 24px）は縦に 10px ずつ広がる。「ON」のような短いチップだけ横にも数 px 広がる |
| `::after`（`inset: -1px`・`z-index: 1`）で自分の見た目の範囲を、隣の部品の `::before` より上に置く | 後ろの部品の `::before` が前の部品の見た目を覆うと、見た目の端を押したのに隣が切り替わる。部品自体は重なり順の文脈を作らないので、`::after` はスティッキーな表頭（z-10）やメニュー（`--z-dropdown` 以上）より下にある |
| 擬似要素は背景・枠・影を持たない | 見た目も強制カラーモードも変わらない |

```tsx
// 製品は何も書かない。これまでどおり使えば、タッチ端末で当たり判定が広がる。
<div className="flex flex-wrap gap-1" role="group" aria-label="再ランキング">
  <ToggleChip selected={value === null} onClick={() => onChange(null)}>継承</ToggleChip>
  <ToggleChip selected={value === true} onClick={() => onChange(true)}>ON</ToggleChip>
  <ToggleChip selected={value === false} onClick={() => onChange(false)}>OFF</ToggleChip>
</div>
<Switch checked={enabled} onCheckedChange={setEnabled} aria-labelledby="history-label" />
```

- E2E で当たり判定の実寸を測るときは、`hasTouch` のプロジェクト（Pixel 5 など。Chromium では `(pointer: coarse)` が一致する）で `document.elementFromPoint` を見た目の外側に向けて探るか、`page.touchscreen.tap` で見た目の外側を押します（NL2SQL の `tests/e2e/touch-targets-tabs.spec.ts`）。

---

## Tabs — 変更（#364）

入りきらずに横へスクロールするとき、スクロールできる方向の端だけをフェードし、選んだタブをフェードの外まで見せるようにしました。props・id・aria・キー操作は変えていません。

| 決めたこと | 理由 |
|---|---|
| タブの列に `pr-tabs-scroll`（`structure/tabs.css`）を付け、続きがある側に `data-scroll-start` / `data-scroll-end` を付ける。付いているときだけ `mask-image` の線形グラデーションで端の `--tab-fade-width`（2rem）を透かす | 色を重ねる方式は、ヘッダー・カード・面スコープごとに背景色を合わせ直す必要がある。mask はどの背景でも同じに見える。入りきるときは何も付けない（見た目は変わらない） |
| 出し分けはスクロールのイベント・`ResizeObserver`（タブの列と各タブ）・`items` の変更で判定する（純粋関数 `tabsScrollEdges`、1px 未満の誤差は無視） | 画面の resize・サイドバーの開閉・件数バッジの変化でも正しい側だけにフェードが出る |
| 選択中のタブ（キーボード・クリック・呼び出し側の変更、初回の表示）は、タブの列の `scrollLeft` だけを動かしてフェードの外まで見せる（純粋関数 `revealTabScrollLeft`。余白は `scroll-padding-inline` = フェードの幅）。キーボードでは `focus({ preventScroll: true })` にする | ブラウザの既定のスクロールでは、タブがフェードの下に残ったり、ページ全体まで動いたりする |
| スクロールバーを隠す指定を utility から `pr-tabs-scroll` に移した。強制カラーモードでは mask を外し、`scrollbar-width: thin` でスクロールバーを出す | 強制カラーモードで文字を透かさない。utilities レイヤーの `[scrollbar-width:none]` は components レイヤーの上書きに勝つため |
| 動き（transition / animation）を持たない | 出し分けは即時。`prefers-reduced-motion` でも同じ |

- タブの列は `relative` になりました（`offsetLeft` をスクロールの内容の左端からの距離にするため）。
- 純粋関数 `tabsScrollEdges` / `revealTabScrollLeft` は `packages/ui/tests/touch-target-tabs.test.tsx` が確かめます。パッケージのルートからは export しません。

---

## Tooltip — **新規**（#372）

アイコンだけのボタンの説明に使う吹き出しです。WAI-ARIA APG の Tooltip パターンに従います（振る舞いの表は README §4「`Tooltip`」）。**`iconOnly` の `Button` は既定で `aria-label` と同じ文言を出す**ので、通常は `Tooltip` を直接書きません。

```tsx
import { Button, Tooltip } from "@production-ready/ui";
import { ChevronLeft, X } from "lucide-react";

// 既定: aria-label と同じ文言を出す（aria-describedby は付けない。二重に読み上げない）
<Button variant="ghost" iconOnly icon={X} aria-label={t("common.dismiss")} />

// 文言を変える: ショートカットキーを添える（aria-label と違うので aria-describedby で説明になる）
<Button variant="ghost" iconOnly icon={ChevronLeft} aria-label={t("preview.viewer.previousPage")}
  tooltip={`${t("preview.viewer.previousPage")} (PageUp)`} />

// 出さない（同じ場所に名前が見えている等）
<Button variant="ghost" iconOnly icon={X} aria-label={t("common.dismiss")} tooltip={false} />

// 見た目を変えずに title を置き換える（iconOnly でない、実質アイコンだけのボタン）
<Button size="sm" variant="ghost" icon={Plus} aria-label={t("graph.zoomIn")} tooltip={t("graph.zoomIn")} />

// Button 以外のフォーカスできる要素 1 つに説明を足す
<Tooltip content={t("legend.toggleHint")}>
  <button type="button" aria-pressed={enabled}>…</button>
</Tooltip>
```

### Tooltip の props

```ts
export interface TooltipProps {
  /** 翻訳済みの短い文。操作できる要素や書式は入れない。 */
  content: string;
  /** トリガー（フォーカスできる要素 1 つ）。ref とイベントを受け取れること。 */
  children: React.ReactElement;
  /** 空きがあれば出す側（既定は上）。入らなければ反転する。 */
  placement?: "top" | "bottom";
  /** true で出さず、aria-describedby の結び付けも外す。 */
  disabled?: boolean;
  /** false で説明として結び付けない（吹き出しは aria-hidden）。読み上げ名に同じ内容が既に入っているとき（#421 の RowTitleButton）。 */
  describe?: boolean;
}
```

| 決めたこと | 理由 |
|---|---|
| 吹き出しは `role="tooltip"`。文言がトリガーの `aria-label` と違うときだけ `aria-describedby` で結び付け、閉じている間も `hidden` で置いておく。同じときは結び付けず `aria-hidden` | APG は説明（description）として結び付ける。名前と同じ文言を説明にすると二重に読み上げる。フォーカスした時点で説明が読めるように、説明の要素は先に置く |
| 開閉は `createTooltipController`（タイマーだけを使う状態機械）、位置は `computeTooltipLayout`（純粋関数）に分ける | 遅延・Escape・hoverable・タッチ端末・反転を DOM なしの単体テストで確かめる |
| Portal の描く先は `SelectField` と同じ（モーダルの中ならモーダル、それ以外は body）。重なり順は `resolveFloatingLayerZIndex(trigger, "--z-popover")`（操作メニューと共有） | `<dialog>` の top layer・`aria-modal` の外に出さない。固定ヘッダー・モーダルの中で隠れない |
| 開いている間の Escape は `window` の capture で受けて握りつぶす | 1 回目の Escape で囲むモーダル（`ConfirmDialog` は document で Escape を受ける）まで閉じない |
| ボタンを押したら閉じ、ポインタが離れるまで出さない。フォーカスで開いた吹き出しは `pointer-events: none` | 押した結果（メニュー・ダイアログ）を隠さない。キーボードで開いた吹き出しが、隣の要素のマウス操作を塞がない |

- E2E で吹き出しを引くときは、`page.locator('[role="tooltip"]:not([hidden])')` で引きます（説明用の吹き出しは閉じている間も `hidden` で body にあり、名前と同じ文言の吹き出しは `aria-hidden` なので `getByRole("tooltip")` では引けません）。説明は `toHaveAccessibleDescription` で確かめます。
- 単体テストは `packages/ui/tests/tooltip.test.tsx`、実ブラウザは NL2SQL の `tests/e2e/tooltip.spec.ts`（desktop のホバー・キーボード・Escape・反転、mobile-375 のタッチ端末）。

---

## InfoTip — **新規**（#901）

操作・欄の補足の説明を常設せず、ラベル（または操作）の横の info アイコン（lucide `Info`、16px）から吹き出しで出します。振る舞いの表・業界の指針・常設の hint との使い分けは README §4「`InfoTip`」。作業に欠かせない情報（入力の条件・押せない理由・エラー・結果）には使いません。

```tsx
import { InfoTip, SelectField } from "@production-ready/ui";

// ラベルの横に置く（ラベルとアイコンは inline-flex でまとめ、行が折り返しても離さない）
<span className="inline-flex items-center gap-0.5">
  <span className="text-xs font-medium text-fg-muted">{t("chat.compare.label")}</span>
  <InfoTip
    label={t("chat.compare.infoLabel")}          // 「回答するモデルの説明」
    content={t("chat.compare.default")}
    contentTestId="chat-default-model"
  />
</span>

// 選択欄にも同じ説明を結び付ける（選んだ値ごとに説明が変わるとき）
const descriptionId = useId();
<InfoTip label={t("chat.engine.infoLabel")} content={engineOption.description} contentId={descriptionId} />
<SelectField id="sql-chat-engine" label={t("chat.engine")} labelHidden describedBy={descriptionId} … />
```

### InfoTip の props

```ts
export interface InfoTipProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children" | "content" | "aria-label" | "type"> {
  /** 説明の文（翻訳済み。1〜3 文。リンク・ボタンなど操作できる要素は入れない）。 */
  content: string;
  /** アイコンのボタンの読み上げ名（翻訳済み）。「<ラベル>の説明」など、何の説明か分かる名前。 */
  label: string;
  /** 空きがあれば出す側（既定は上）。入らなければ反転する。 */
  placement?: "top" | "bottom";
  /** 吹き出しの id。説明の対象の欄の aria-describedby（SelectField の describedBy）からも結び付けるときに渡す。 */
  contentId?: string;
  /** 吹き出しの data-testid（E2E 用）。ボタンの data-testid はそのまま渡す。 */
  contentTestId?: string;
}
```

| 決めたこと | 理由 |
|---|---|
| 開閉は `Tooltip` と同じ `createTooltipController` に `press`（押して開いたまま固定。固定中はもう一度で閉じる）と `dismiss`（外側を押した）を足して使う。開いたきっかけは `data-tooltip-reason="press"` | 遅延・hoverable・Escape・「画面に 1 つだけ」（#655）を `Tooltip` と共有し、DOM なしの単体テストで確かめる |
| 吹き出しの描画（Portal・位置・重なり順・Escape）は `Tooltip` と同じ `TooltipBubble`。`InfoTip` は幅（20rem）・余白・太さ（400）・行間だけを足す | 見た目と位置の規則を 1 か所に保つ |
| 押して開いている間だけ、`document` の `pointerdown`（capture）で外側を押したら閉じる | クリックでフォーカスが移らないブラウザ（Safari）とタッチ端末でも閉じる |
| ボタンの `data-state` は `open` / `closed`（見た目のホバーの色に使う）。`aria-expanded` は付けない | 説明は `aria-describedby` で閉じていても読めるので、開閉は目で見るためのもの |

- E2E: 閉じている間は `toBeHidden()`、説明は `toHaveAccessibleDescription`、開いた吹き出しは `contentTestId`。タッチ端末の project ではホバーの代わりに `tap()`（ホバー・フォーカスでは開かない）。info アイコンの名前は「<ラベル>の説明」なので、同じ行のボタンは `exact: true` で引く。
- 単体テストは `packages/ui/tests/info-tip.test.tsx`（`press` / `dismiss`・タッチ端末・Escape・外側・読み上げの結び付け）。実ブラウザは RAG `e2e/search-answers.spec.ts`・NL2SQL `tests/e2e/sql-chat.spec.ts` / `nl2sql-ontology.spec.ts`・Agent `e2e/agent-versions.spec.ts` / `run-stream-auth.spec.ts`（desktop / 375px のタップ・キーボード・Escape）。

## 読み込み中と一覧の表示密度 — **新規**（#265）

3 製品の一覧・読み込み中の表示を NL2SQL の基準にそろえるための定数と形のある Skeleton です。値は製品で書かず、ここから import します。

```ts
// packages/ui/src/lib/list-density.ts（NL2SQL の src/lib/list-density.ts から移した。値は同じ）
INFORMATION_TABLE_VISIBLE_ROWS       // { base: 5, md: 8 } — DataTable の visibleRows
INFORMATION_TABLE_FIXED_VISIBLE_ROWS // 5 — 幅によらず 5 行の一覧
INFORMATION_TABLE_ROW_CLASS          // "h-[3.5rem]" — DataTable の rowProps の className（行の最小高さ）
INFORMATION_LIST_ROW_CLASS           // "min-h-[3.5rem]" — 表ではない行リストの行
INFORMATION_LIST_SCROLL_CLASS        // "max-h-[17.5rem] overflow-auto md:max-h-[28rem]" — 表ではない行リストの 5 / 8 行
INFORMATION_COMPACT_LIST_FIVE_ROW_SCROLL_CLASS // "h-56 max-h-56 overflow-auto"
INFORMATION_TABLE_FOCUS_CLASS        // Tab で到達できるスクロール領域のフォーカスの表示（outline）
```

```tsx
/** 見た目は 1 つ: 地は --color-surface-hover、prefers-reduced-motion では点滅しない。 */
export const SKELETON_CLASS = "animate-pulse rounded-md bg-surface-hover motion-reduce:animate-none";
export function Skeleton({ className, testId }: { className?: string; testId?: string });

/** 表の形。DataTable と同じ枠・表頭（surface-sunken、2.5rem）・行（3.5rem）に列ごとの棒。
 *  rows の既定は { base: 5, md: 8 }。md 以上だけの行は CSS（hidden md:flex）で出し分け、最初の描画で高さが決まる。 */
export function TableSkeleton({ rows, columns = 4, className, testId }: TableSkeletonProps);

/** 表ではない行リスト（カードの行・履歴）の形。角丸の塊を gap-2 で並べる。 */
export function ListSkeleton({ rows, rowClassName = "h-[3.5rem]", className, testId }: ListSkeletonProps);

/** 設定カード・エディタの形。見出し → ラベル + 入力欄（--button-height-md）× fields → 右寄せの操作行。 */
export function FormSkeleton({ fields = 4, title = true, actions = true, className, testId }: FormSkeletonProps);

/** チャットの会話の形（#1153）。右寄せの質問の吹き出し（h-12・最大 85%）と左の回答の塊（h-32）を turns 組。
 *  3 製品のチャットの会話の欄で、前提の読み込み中に空の状態の代わりに出す（UX 契約 messaging.md §11.7）。 */
export function ChatSkeleton({ turns = 2, className, testId }: ChatSkeletonProps);
```

- 形のある Skeleton は `aria-hidden` です。**読み込み中の文言と経過時間は `TimedLoadingState` が出す**ので、必ずその子に置きます（UX 契約 messaging.md §3.6 / §3.7）。
- `DataTable` の `loading` の行・`LoadingState` の棒も同じ `SKELETON_CLASS` を使います。`DataTable` の `loading` は、`loadingRows` を省略すると `visibleRows` の行数（無ければ 3 行）の Skeleton で高さを予約します。
- 同じ取得の経過時間は 1 か所だけに出します。2 つの領域が同じ取得を待つときは、片方は形だけ（`TableSkeleton` を `TimedLoadingState` で包まない）にします。
- **位置と幅は全画面で 1 つ**です（#2510）。影響を受ける領域の先頭に、領域と同じ幅の行で出し、その下を内容の形の Skeleton で覆います。画面の中央に浮かせる・内容の幅のカードにする・ページの上に重ねる・見出しの文言を差し替える・右寄せのバッジにする、はしません。
  - ページ全体が使えない初期処理（認証の確認・DB の状態の確認・画面のコードの読み込み）は `PageLoadingState`（全幅の `PageBody` の先頭の行 + 本文の形の Skeleton。画面と同じく常に全幅で、`wide` は渡さない。#3311。`AppShell` の外では `<main className="min-h-screen bg-canvas">` で包む）。
  - 汎用の行の形は `LoadingState`（`label` 必須。行 + 棒の Skeleton）。検索つきの選択欄の取得中は、一覧の先頭に同じ形の行を出します（部品が持つ）。
  - スクロールする表示領域（`PreviewViewer` のページ）は、高さ 0 の `sticky` の中に全幅の行を置き、スクロールしても先頭に残し、内容の位置と高さを動かしません。

標準の組み合わせ:

```tsx
{query.isPending ? (
  <TimedLoadingState label={t("xxx.loading")} operationKey="xxx-load" testId="xxx-loading">
    <TableSkeleton columns={5} />
  </TimedLoadingState>
) : (
  <div className="grid gap-2">
    <DataTable
      rows={pageItems}
      rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
      stickyHeader
      visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
      scrollAriaLabel={t("xxx.scrollLabel")}
      …
    />
    <Pagination page={page} totalPages={totalPages} range={range} onPageChange={setPage} labels={paginationLabels()} />
  </div>
)}
```

`PagedDataTable` の `viewport="bounded"` は、長いセルで 1 行が高くなる表を共通のカード一覧の上限（md 未満 17.5rem、md 以上 28rem）で止めます。表頭固定・ページ送り・縦横スクロールは維持します。既定の `list` は実測の 5 / 8 行、`page` は親でスクロールする全高の表です。

クライアント側で全件を持つ一覧は、上の組み合わせを 1 つにした `PagedDataTable` を使います（#265。RAG・Agent が使う）。

```tsx
// packages/ui/src/components/data/pagination.tsx（`PagedDataTable` と `Pagination` の `labels` が受け取る）
export interface PaginationLabels {
  summary: (range: PaginationRange) => string;               // 「1 - 10 / 42 件」
  pageIndicator?: (page: number, totalPages: number) => string; // 「1 / 5 ページ」
  prev: string;
  next: string;
  ariaLabel?: string;
}

<PagedDataTable
  rows={rows}                 // 全件。表示する 25 件（既定）はこの部品が切り出す
  columns={columns}
  getRowKey={(row) => row.id}
  paginationLabels={labels}   // 任意。製品の i18n で作る（省くと共通の既定の日本語）
  scrollAriaLabel={t("xxx.scrollLabel")}
  resetKey={query}            // 任意。検索語・絞り込みが変わったときだけ 1 ページ目へ戻す
  page={page} onPageChange={setPage} // 任意。ページ番号を作業状態に残すとき
/>
```

- `stickyHeader`・`visibleRows`（`INFORMATION_TABLE_VISIBLE_ROWS`）・行の最小高さ（`INFORMATION_TABLE_ROW_CLASS`。`rowProps` の className と合わせる）はこの部品が付けます。
- `resetKey` を省くと、再取得で行が変わってもページを戻しません（Agent の Run・承認の 5 秒ごとの再取得など）。行が減って範囲外になったら表示だけ末尾のページに寄せます。
- `paginationLabels` は任意です（#1266。省くと共通の既定 `DEFAULT_PAGINATION_LABELS`）。製品は i18n の文字列を返す `paginationLabels()` の 1 関数（RAG `lib/pagination-labels.ts`、Agent `components/ListViews.tsx` の `agentPaginationLabels`、NL2SQL `lib/pagination-labels.ts`）だけを持ち、部品を包み直しません（Agent はページ番号を `sessionStorage` の作業状態に残す `pageKey` のラッパーだけ残す）。
- サーバー側のページング（offset / limit / total）は `DataTable` + `OffsetPagination`（#1266）。今のページが 0 件で返ったら（削除・期間の変更・保存していたページが無くなった）最後のページへ寄せて `onPageChange(page, offset)` を呼ぶので、画面で書き写しません（Agent のフィードバック・評価のジョブ・監査、RAG の文書・ナレッジベース・検索・回答プロファイル・チャットの会話一覧、NL2SQL の実行履歴・アプリのフィードバック・質問の学習の候補・SQL生成評価・チャットの会話一覧。#1891）。

```tsx
const [offset, setOffset] = useWorkspaceState(…);       // ページは作業状態に残す（offset でも page でもよい）
<OffsetPagination offset={offset} limit={limit} total={page.total} count={page.items.length}
  onPageChange={(_page, nextOffset) => setOffset(nextOffset)} labels={paginationLabels()} ariaLabel={…} testId={…} />
```

- カーソル（`next_cursor`）を返す API を「前へ / 次へ」で送る一覧は、`useCursorPages`（前へ戻るカーソルの履歴。`resetKey` で先頭へ）と `CursorPagination`（`cursorPagination` で `page` / `totalPages` / `range` を作る）を使います（#1266）。総数を返せる一覧は offset 型にし、ページ番号を入れて移れるようにします（#1891。NL2SQL の実行履歴・フィードバック・学習の候補・SQL生成評価はカーソル型から移した）。

```tsx
const pages = useCursorPages({ resetKey: filters });     // pages.cursor を API に送る（最初のページは null）
<CursorPagination depth={pages.depth} total={data.total ?? 0} count={data.items.length} nextCursor={data.next_cursor}
  onPrevious={pages.prev} onNext={pages.next} labels={paginationLabels()} ariaLabel={…} testId={…} />
```

- 1 ページの件数を選べる一覧は `PageSizeSelect`（`value` / `onValueChange` / `options`（既定 `PAGE_SIZE_OPTIONS` = 10 / 50 / 100）/ `labels`（既定「1 ページの件数」「{n} 件」）。幅は `sm`、`ResultTable` は `xs`）。件数の範囲の文言は `paginationRange(page, pageSize, count, total)`、「さらに読み込む」の失敗の文言は `loadMoreErrorMessage(error, { timeoutMessage, fallback })`。
- 表ではない行リスト（カードの行・選択と連動する一覧）は `INFORMATION_LIST_SCROLL_CLASS` で 5 / 8 行の高さにし、中をスクロールします。行が表より高くても手書きの `max-h-[…]` にしません（NL2SQL の実行履歴、RAG の chunk・抽出セグメント、#403）。
- 基準から外す一覧（選択と連動する一覧・カーソル型の「さらに読み込む」・分析の一覧の件数の切り替え）と理由は、UX 契約 `page-archetypes.md` の「一覧の型と、基準から外す例外」にあります。

---

## ExecutionConfirmationField — **新規**（#379）

破壊的な操作の **実行確認語（type-to-confirm）** の入力欄です。NL2SQL の `ExecutionConfirmationField`（製品のコード、14 か所で使用）と、system-settings のシステムテーブルの確認語欄（#325 で作ったコピー）を `packages/ui` に一本化しました。Agent の実行時スナップショットの置換の確認語もこれに置き換えました。**製品で確認語の入力欄を再実装しないでください。**

| 決めたこと | 理由 |
|---|---|
| 面は中立（`--color-surface-sunken` + `--color-border`）。入力前は danger 色を使わない | 操作前から赤いと、エラーが起きているように見える（README §4「確認語欄（実行確認語）の色」、UX 契約 messaging §3.5） |
| 見出しの行に「入力条件: `{phrase}`」と状態のバッジ（未入力 / 不一致 / 確認済み）。バッジは `aria-live="polite"` | 状態を色だけで示さない。入力の結果を読み上げで知らせる |
| 確認語は等幅・太字で、`.` / `_` / `$` / `#` の直後で折り返す（`<wbr>`） | 識別子（`ADMIN_EXECUTE`・`OWNER.OBJECT`）を読み違えない。375px でも横にはみ出さない |
| 一致しない語を入れたときだけ `aria-invalid="true"`、説明文とバッジを danger 色にする | 空白だけの入力は未入力として扱う |
| 入力欄は下の操作行（実行・取消）と同じ lg（`--control-height-lg` の 40px、タッチ端末は 44px。#613 で 44px 固定から変更）、`required` / `aria-required`、説明を `aria-describedby`。自動補完・自動修正・スペルチェックをしない。フォーカス中は枠線を `--color-danger-fg` にする（ring は重ねない） | 操作行と同じ高さの段にそろえる（README §4「操作部品の高さと幅」）。確認語は手で入力させる |
| 実行 / キャンセル等の操作は `actions` で渡し、区切り線（`border-t`）の下に置く。640px 未満は縦に並べる | 確認語と実行を 1 つの区画にまとめる（README §4「カード内の操作行」） |
| **一致の判定は呼び出し側**（`confirmed`）。前後の空白を許すかどうか・確認語の値は製品が決める | backend の確認と同じ規則で判定するため。`packages/ui` は業務の語を知らない |
| 文言は `labels` で差し替える。未指定の項目は既定（`DEFAULT_EXECUTION_CONFIRMATION_LABELS`：実行確認語 / 必須 / 入力条件: {phrase} / 未入力 / 不一致 / 確認済み） | 3 製品で同じ語をそろえる。製品固有の見出し（Agent の「確認入力」）や system-settings の上書き可能な文言は `labels` で渡す |

```tsx
<ExecutionConfirmationField
  value={confirmation}
  onChange={setConfirmation}
  confirmed={confirmation.trim() === "ADMIN_EXECUTE"}
  expectedLabel="ADMIN_EXECUTE"
  helper="ADMIN_EXECUTE を入力すると実行できます。"
  disabled={running}
  actions={
    <>
      <Button variant="danger" size="lg" className="w-full sm:w-auto" icon={Play} loading={running} disabled={!confirmed}>
        実行
      </Button>
      <ClearActionButton label="入力をリセット" size="lg" className="w-full sm:w-auto" onClick={clear} />
    </>
  }
/>
```

### ExecutionConfirmationField の props

```ts
export interface ExecutionConfirmationLabels {
  label: string;     // 見出し。既定「実行確認語」
  required: string;  // 必須のタグ。既定「必須」
  expected: string;  // `{phrase}` の位置に確認語を差し込む。既定「入力条件: {phrase}」
  pending: string;   // 既定「未入力」
  mismatch: string;  // 既定「不一致」
  confirmed: string; // 既定「確認済み」
}

export interface ExecutionConfirmationFieldProps {
  value: string;
  onChange: (value: string) => void;
  /** 一致の判定は呼び出し側。 */
  confirmed: boolean;
  expectedLabel: string;
  /** 既定は expectedLabel。 */
  placeholder?: string;
  helper: ReactNode;
  disabled?: boolean;
  /** 既定は useId。E2E やページ内リンクで引くときだけ渡す。説明の id は `${id}-helper`。 */
  id?: string;
  labels?: Partial<ExecutionConfirmationLabels>;
  actions?: ReactNode;
  className?: string;
}

/** 表示の区分（空白だけは pending）。 */
export declare function executionConfirmationStatus(value: string, confirmed: boolean): "pending" | "mismatch" | "confirmed";

/** @dsComponent */
export declare function ExecutionConfirmationField(props: ExecutionConfirmationFieldProps): JSX.Element;
```

- ルート要素は `data-testid="execution-confirmation-field"` と `data-confirmation-status`（`pending` / `mismatch` / `confirmed`）を持ちます。3 製品の E2E はこの testid で確認語欄を引きます。
- `actions` の実行ボタンを一致するまで無効にし、使えない理由として説明（`helper`）を読み上げるときは、`id` を渡し、ボタンの `aria-describedby` に `${id}-helper` を一致しない間だけ指定します（例: Agent の実行時スナップショットの置換。#426）。
- 確認語はページを離れる・戻るとき、対象や入力が変わったときに呼び出し側で空に戻します（UX 契約 workspace-state.md）。

## TextField — 変更（#384）

先頭アイコン（`leadingIcon`）・後置スロット（`trailing`）・クリア（`onClear`）を足しました。検索欄は製品で手書きせず、これで作ります（決めたことの表は README §4「`TextField` の先頭アイコン・後置スロット」）。既存の props・id・aria は変えていません。

```tsx
import { TextField } from "@production-ready/ui";
import { Search } from "lucide-react";

// 一覧の絞り込みの検索欄は TextField ではなく SearchField で作る（下の「SearchField」、#535）。

// 重い検索の質問欄（明示的に実行する）: Enter の判定は IME 対応の isSubmitEnter
<TextField
  id="search-test-query"
  label={t("knowledgeBases.searchTest.title")}
  labelHidden
  value={query}
  onValueChange={setQuery}
  onKeyDown={(e) => { if (isSubmitEnter(e)) void submit(); }}
  leadingIcon={Search}
/>

// lg の Button と同じ行に並べる質問欄
<div className="flex flex-col gap-2 sm:flex-row">
  <TextField id="search-query" label={t("nav.search")} labelHidden size="lg" leadingIcon={Search}
    value={query} onValueChange={setQuery} className="min-w-0 flex-1" />
  <Button size="lg" icon={Search} loading={busy}>{t("search.button")}</Button>
</div>

// 単位・件数などの後置
<TextField id="limit" label="取得件数上限" inputMode="numeric" value={limit} onValueChange={setLimit}
  trailing={<span className="px-3 text-sm text-fg-muted">件</span>} />
```

### TextField の props（追加分）

```ts
export type TextFieldSize = ControlSize; // "sm" | "md" | "lg"（#613）

export type TextFieldProps = {
  /** label を sr-only にする（検索欄だけ。フォームの入力欄では使わない）。 */
  labelHidden?: boolean;
  /** 高さ。sm 32px / md 36px（既定）/ lg 40px（--control-height-*）。タッチ端末は 44px。同じ行の Button と同じ size にする。 */
  size?: TextFieldSize;
  /** 幅（xs / sm / md / lg / full。既定は親の幅いっぱい）。sm 未満は全幅（#613）。 */
  width?: FieldWidth;
  /** 先頭の 16px のアイコン（lucide-react）。aria-hidden・pointer-events: none。 */
  leadingIcon?: LucideIcon;
  /** 末尾の任意の要素（単位・件数・ボタン）。実際の幅だけ文字の右の余白を空ける。 */
  trailing?: ReactNode;
  /** 値を消す。値があるときだけクリアボタンを出し、Escape でも消す。value を制御して使う。 */
  onClear?: () => void;
  /** クリアボタンの読み上げ名と Tooltip（翻訳済み）。onClear と一緒に渡す。 */
  clearLabel?: string;
  // …既存の id / label / helper / error / required / requiredLabel / className / inputClassName / onValueChange / ref
} & Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "required" | "size">;
```

| 決めたこと | 理由 |
|---|---|
| 入力欄を常に `div.relative` で包み、先頭アイコンは入力欄の**後ろ**（DOM 上）に置いて `peer-disabled:` で色を変える | クリアボタンの出し入れで入力欄が作り直されない（フォーカスと IME の変換を失わない）。`peer` は前の兄弟にしか効かない |
| 後置スロットは枠線まで含めた右端（`inset-y-0 right-0`。`SecretField` の表示切替と同じ）。幅は `ResizeObserver` で測り、入力欄の `padding-right` にする。測る前（SSR・初回）は `pr-[var(--field-height)]`（四角のボタン 1 つ分） | 幅の決まらない要素でも文字と重ならない。スロットのボタン（クリア・`SearchableMultiSelect` の一覧の開閉）が入力欄と同じ高さ（md 36px・タッチ端末 44px）になる。以前の枠線の内側（`inset-y-px right-px`）では 2px 低かった（#1132） |
| スロットのボタンは `TEXT_FIELD_TRAILING_BUTTON_CLASS`（`h-full` の正方形・外側の角だけ `rounded-r-control`・`bg-clip-padding`） | 枠線は透明のまま地を枠線の内側だけに塗るので、ホバーの地が入力欄の枠線に重ならず、見た目は枠線の内側に収まる |
| クリアボタンは `aria-controls` で入力欄を指し、`mousedown` を止める。押したら `onClear()` の後に入力欄へ `focus()` | ボタンが消えてもフォーカスが body に落ちない。blur で確定する検索欄が消す前の値を確定しない |
| 強制カラーモードでは、スロットのボタンの輪郭の上・右・下が入力欄の枠線とちょうど重なり、左の区切りだけが増える（クリアの左に並ぶ一覧の開閉は、右を `Canvas` にする） | Button は強制カラーモードで輪郭を出すが、入力欄の枠線と二重の線にしない |
| `type="search"` の `::-webkit-search-cancel-button` / `::-webkit-search-decoration` を `appearance: none` | 共有のクリアと二重にしない（README §7 #33） |

- 純粋関数 `hasTextValue` / `shouldClearOnEscape` / `clearTextField` と class の組み立ては `packages/ui/tests/text-field-slots.test.tsx` が確かめます（パッケージのルートからは export しません）。実ブラウザは RAG の `e2e/feedback.spec.ts`（desktop / 375px の高さ・角丸・アイコン・クリア・Tab 順・Escape、強制カラーモードのタブ）。
- 検索欄の E2E は `getByRole("searchbox" | "textbox", { name })` で引きます。先頭アイコンは入力欄の親（`xpath=..`）の `[data-text-field-slot="leading"]`、後置は `[data-text-field-slot="trailing"]` です。

## TextField — 候補（`suggestions`、#547）

候補から選べて、候補に無い値もそのまま入力できる入力欄（editable combobox）。RAG の文書の分類（大・中・小分類）で、保存済みの値を候補に出して表記の揺れを防ぐために足しました。既存の props・id・aria は変えていません。

```tsx
<TextField
  id="document-classification-large_category"
  label={t("documents.classification.large_category")}
  value={value}
  onValueChange={setValue}
  suggestions={options?.large_categories}   // 翻訳しないデータの値。空・未指定なら普通の入力欄
/>
```

| 決めたこと | 理由 |
|---|---|
| 候補の一覧は独自の listbox ではなく、ネイティブの `<datalist>`（入力欄の `list`） | キー操作（↓ で開く・Enter で選ぶ）・読み上げ（role=combobox）・モバイルの候補表示・`color-scheme` によるダークテーマを OS / ブラウザに任せる。`type="date"` の日付選択と同じ扱いで、選んだ値は普通の入力と同じく `onChange` / `onValueChange` に届く |
| 候補があるときだけ `list` と `autoComplete="off"`（呼び出し側の `autoComplete` が優先） | ブラウザの入力履歴を候補に混ぜない。候補が無いときは従来と同じ入力欄 |
| `list` 属性は props で受け取らない（`TextFieldProps` から除く） | datalist の id は `useId` で作り、ほかの入力欄の候補と取り違えない |

- 候補の一覧の見た目（ポップアップ）はブラウザが描くため、トークンの色・角丸にはなりません（日付選択と同じ）。候補を絞り込む・複数選ぶ・候補に無い値を拒む入力は、この部品ではなく `SelectField` や `SearchableSelectField` / `SearchableMultiSelect`（#578）の型です。
- テストは `packages/ui/tests/text-field-slots.test.tsx`（datalist との結び付き・`autocomplete`）、実ブラウザは RAG の `e2e/structure-explainability.spec.ts`（文書詳細の分類、desktop / 375px、light / dark）。


## TextareaField — 新規（#584）

複数行の入力欄。3 製品が `<textarea>` を手書きしていて、地（`bg-surface` / `bg-surface-sunken`）・角丸（`rounded-md`）・余白・フォーカスの枠線・disabled の見た目（`opacity-50` など）・ラベルと必須とエラーの付け方・文字数の表示が画面ごとに違いました。`TextField` と同じ見た目と API の部品にそろえます。

```tsx
import { TextareaField } from "@production-ready/ui";

<TextareaField
  id="search-answer-profile-system-prompt"
  label={t("searchAnswerProfiles.field.systemPrompt")}
  helper={t("searchAnswerProfiles.field.systemPromptHelper")}
  value={prompt}
  onValueChange={setPrompt}
  rows={3}
/>

// 必須・エラー・文字数（上限があれば「12 / 1,000」。関数で翻訳済みの文言にできる）
<TextareaField id="faq-answer" label={t("faq.answer")} required error={errors.answer} maxLength={20000}
  showCount={(count) => t("faq.answerCount", { count })} value={answer} onValueChange={setAnswer} />

// SQL・JSON・プロンプトの雛形は等幅。読み取り専用のプレビューは resize="none"
<TextareaField id="preview" label={title} labelHidden readOnly monospace resize="none" value={json} />
```

| 決めたこと | 理由 |
|---|---|
| 枠線・角丸・地・フォーカス・disabled・read-only は `TextField` と同じ class（`fieldControlClass`）。上下の余白は `py-2`、行間は `leading-relaxed` | 1 行の入力欄と並べても同じ部品に見える。read-only は `bg-surface-sunken`（プレビュー）、disabled は `bg-surface-disabled`（`opacity` で薄めない） |
| 既定 `rows={3}`（半端な値〔3.5 など〕は高さを行の高さ × 行数にする。#2008）・縦だけ伸ばせる（`resize-y`）。`resize="none"` で固定 | 横に伸ばすと列の幅を崩す。高さは `rows` か `textareaClassName`（`min-h-*`）で決める |
| `monospace` は `--font-mono` の 12px | SQL・JSON・プロンプトの雛形を画面ごとに `font-mono text-xs` で書いていた |
| `surface="code"` は暗いコードの面（`data-surface="code"`）・等幅で、read-only でもコードの地のまま | 生成した SQL などの読み取り専用の表示。`bg-surface-sunken` はコードの面で定義し直されず、明るい地に明るい文字になるため |
| `showCount` は `maxLength` があれば「現在 / 上限」、無ければ「n 文字」。右下に `tabular-nums` で置き、補足と同じく `aria-describedby` で結ぶ（`aria-live` にしない） | 打鍵ごとに読み上げると入力の邪魔になる。欄に入ったときに上限と一緒に読める |
| 必須は `required`（`aria-required` と「必須」のタグ）、エラーは `error`（`aria-invalid`・枠線の色・欄の直下の `FieldError`） | `TextField` と同じ（README §4「必須の表示」、UX 契約 messaging.md §3.2.1） |
| 条件付きの必須は `requiredAnnouncedByControl={false}`（`aria-required` を付けず、`requiredLabel` のタグをラベルの一部として読ませる） | 「「違う」のとき必須」のように常に必須ではない欄で、条件を読み上げに残す（`FieldLabel` の同名の prop と同じ） |
| 自動で高さを伸ばす機能は持たない | ブラウザの対応（`field-sizing: content`）がそろうまで、`rows` と `resize-y` で足りる |

- adherence の lint が製品の JSX の `<textarea>` を検出します（`design-system/restricted-syntax`）。例外は元の文書の表を再現して編集するグリッドのセルなど、`TextareaField` で表せない所だけで、理由を添えて `eslint-disable-next-line` / `oxlint-disable-next-line` で局所的に除外します。
- 単体テストは `packages/ui/tests/textarea-field.test.tsx`、lint の規則は RAG の `src/design-system-adherence.test.ts`。

## Tabs — 変更（#374）

強制カラーモードで、選ばれていないタブの下線を `Canvas`（`forced-colors:border-b-[Canvas]`）、選んだタブの下線を `Highlight`（`forced-colors:aria-selected:border-b-[Highlight]`）にしました。props・id・aria・キー操作は変えていません。

- 強制カラーモードでは `border-color: transparent` も `CanvasText` に置き換わります（Chromium で確認）。system color を明示すればそのまま使われます。
- 単体テストは `packages/ui/tests/text-field-slots.test.tsx`（class）、実ブラウザは RAG の `e2e/feedback.spec.ts`（`page.emulateMedia({ forcedColors: "active" })`）。

---

## Tabs — 変更（#396）: ペインの中の見方の切り替え

ページより小さい単位（ペイン・カードの中）で同じ対象の見方を切り替えるときも `Tabs` + `TabPanel` を使います。枠の中にボタンを並べたセグメントコントロールは作りません（理由は README §4「`Tabs`」）。`TabItem` に `disabledReason` を足しました。ほかの props・id・aria・キー操作は変えていません。

```tsx
<div className="mb-2 flex flex-wrap items-center justify-between gap-2">
  <h3 className="text-sm font-semibold text-fg">{t("flow.preview")}</h3>
  {/* 選んだ見方に効く操作（ダウンロード等）は見出しの行に置く */}
</div>
<Tabs
  idPrefix="preview"                       // 同じ画面の別の Tabs と分ける
  ariaLabel={t("flow.preview")}
  className="mb-2 shrink-0"
  value={variant}
  onChange={(id) => setVariant(id === "prepared" ? "prepared" : "original")}
  items={[
    { id: "original", label: t("flow.preview.before") },
    {
      id: "prepared",
      label: t("flow.preview.after"),
      disabled: !hasPrepared,
      disabledReason: hasPrepared ? undefined : t("flow.preview.preparedUnavailable"),
    },
  ]}
/>
<TabPanel id={variant} value={variant} idPrefix="preview" className="flex min-h-0 flex-1 flex-col">
  <DocumentPreview … />
</TabPanel>
```

| 決めたこと | 理由 |
|---|---|
| 役割は `tablist` / `tab`（`aria-selected`）/ `tabpanel`。`radiogroup` / `aria-pressed` にしない | WAI-ARIA APG に segmented control のパターンは無い。見方の切り替えは Tabs パターン。`radiogroup` はフォームの値の選択、`aria-pressed` はオン / オフのトグルで、中身が入れ替わることを伝えない |
| 表示中のパネルが 1 つでも `TabPanel` で包む（`id` と `value` に選択中の id を渡す） | `aria-controls` の参照先と、パネルの名前（`aria-labelledby` = 選んだタブ）を持たせる。中身の要素は同じ位置のままなので、切り替えで作り直されない |
| `disabledReason` は無効のタブにだけ `title` として付ける | 無効のタブはフォーカスを受けず Tooltip を出せない。ホバーと読み上げ（説明）で理由を伝える。有効なタブには付けない |
| 入れ子（右ペインのタブの中の形式のタブ）でもよい。`idPrefix` を分ける | 入れ子の tablist は、外側の tablist の外（外側の `tabpanel` の中）にあるので、矢印キーが干渉しない |

- E2E: 1 画面に `tabpanel` が複数になる。`page.getByRole("tabpanel")` で引いていたテストは、ペイン（`getByTestId("document-inspector-pane")`）やパネルの名前（`{ name: "抽出エクスポート" }`）で絞る。
- 単体テストは `packages/ui/tests/components.test.tsx`（役割・選択・無効の理由・キー操作）、実ブラウザは RAG の `e2e/document-workspace-file-processing.spec.ts`（「処理前/処理後と抽出エクスポートの形式は共有の Tabs で…」）。

## Tabs — 変更（#542）: エラーのあるタブ

フォームを複数のタブに分けたとき（モデル設定のプライマリ接続 / セカンダリ接続）、選んでいないタブの欄のエラーを見落とさないように、`TabItem` に `invalid` を足しました。ほかの props・id・キー操作は変えていません。

```tsx
<Tabs
  idPrefix="enterprise-connection"
  ariaLabel="OCI Enterprise AI の接続"
  value={tab}
  onChange={setTab}
  items={[
    { id: "primary", label: "プライマリ接続" },
    // 保存で止めたときは、呼び出し側が最初のエラーのタブへ切り替えて、最初のエラーの欄へフォーカスする。
    { id: "secondary", label: "セカンダリ接続", badge: unsaved ? "未保存" : undefined, invalid: hasErrors },
  ]}
/>
```

```ts
export interface TabItem {
  // …
  /** タブの中に入力のエラーがある。ラベルの後ろに danger 色の CircleAlert、読み上げは invalidLabel。 */
  invalid?: boolean;
  /** invalid のときの読み上げの説明（翻訳済み）。既定 DEFAULT_TAB_INVALID_LABEL（「入力にエラーがあります」）。 */
  invalidLabel?: string;
}
export declare const DEFAULT_TAB_INVALID_LABEL: string;
```

| 決めたこと | 理由 |
|---|---|
| 印はラベル（とバッジ）の後ろの `CircleAlert`（14px、`text-danger-fg`、`aria-hidden`） | エラーは利用者の対応が要る状態なので状態色を使う。アイコンの形でも伝え、色だけに頼らない（WCAG 1.4.1）。選択中のタブの下線・文字色は変えない |
| 読み上げは `sr-only` の説明を `aria-describedby` で結ぶ（タブの名前は変えない） | タブの名前（「セカンダリ接続」）を保ったまま、フォーカスしたときに「入力にエラーがあります」と伝える。件数バッジと同じ結び方 |
| `data-invalid` を付ける | E2E・単体テストで状態を引ける（見た目の class に依存しない） |
| タブの切り替えとフォーカスは呼び出し側が行う | どの欄が最初のエラーかは画面が知っている。Tabs は表示と読み上げだけを持つ |

- 単体テストは `packages/ui/tests/components.test.tsx`（「Tabs のエラーの表示（#542）」）。実ブラウザは RAG の `e2e/model-settings-switch.spec.ts`（「接続はプライマリ接続とセカンダリ接続のタブで切り替え…」）。

## Disclosure — **新規** / DisclosureChevron — 変更（#397）

開閉できる領域の標準形です。ネイティブの `<details>` / `<summary>` を包み、見出しの行全体を押せる領域にし、開閉の状態を右端（`plain` は見出しの直後）の `DisclosureChevron` で示します。**製品で `<details>` / `<summary>` を手書きしないでください**（adherence の lint が JSX の `<details>` を検出します）。見た目と振る舞いの決定は README §4「`Disclosure`」。

```
┌───────────────────────────────────────────────┐  card（既定）
│ [icon] 見出し                    [meta]  ›    │  ← summary（行全体を押せる。hover で地が付く）
├───────────────────────────────────────────────┤  ← 開いているときだけ区切り線
│ 内容（p-3）                                    │
└───────────────────────────────────────────────┘
見出し ›                                             plain（枠なし。Chevron は見出しの直後）
```

```tsx
import { Disclosure } from "@production-ready/ui";
import { Wrench } from "lucide-react";

// 受控: 開閉を URL や作業状態に残すとき
<Disclosure
  summary={t("flow.inspector.details")}
  icon={Wrench}
  open={diagnosticsOpen}
  onOpenChange={setDiagnosticsOpen}
  data-testid="document-diagnostics"
  contentClassName="space-y-5"
>
  <SourceProfilePanel … />
</Disclosure>

// 非受控 + 件数（meta）+ 面の上に重ねる（sunken）
<Disclosure summary="構造化要素" meta={<span className="tnum text-xs text-fg-muted">{count}</span>} surface="sunken">…</Disclosure>

// 状態が変わったら開き直す（失敗したら開く）: key を状態で変え、defaultOpen を渡す
<Disclosure key={failed ? "failed" : "active"} defaultOpen={failed} tone="warning" summary={`警告 (${n})`}>…</Disclosure>

// 表のセルの中・回答の補足: plain + sm。summary に data-testid を付けるときは summaryProps
<Disclosure variant="plain" size="sm" summary="分析" summaryProps={{ "data-testid": "analysis-toggle" }}>…</Disclosure>
```

### Disclosure の props

```ts
export type DisclosureVariant = "card" | "plain";
export type DisclosureSurface = "surface" | "sunken";
export type DisclosureTone = "neutral" | "warning" | "danger";
export type DisclosureSize = "md" | "sm";

export interface DisclosureProps
  extends Omit<DetailsHTMLAttributes<HTMLDetailsElement>, "open" | "onToggle" | "title" | "children"> {
  summary: ReactNode;            // 見出し。操作できる要素（ボタン・リンク）を入れない
  icon?: LucideIcon;             // 先頭のアイコン（md 16px / sm 14px、--color-fg-muted）
  description?: ReactNode;       // 見出しの下の補足（card のみ、12px）
  meta?: ReactNode;              // 見出しと Chevron の間（件数・StatusBadge）
  summaryFooter?: ReactNode;     // 見出しの行の下に、見出しの行の全幅で出す短い補足（押せる見出しの中。開閉にかかわらず見える。ChatProgress の遅延・再接続の案内。#2426）
  open?: boolean;                // 受控。渡すときは onOpenChange も渡す
  defaultOpen?: boolean;         // 非受控の初期状態
  onOpenChange?: (open: boolean) => void;
  variant?: DisclosureVariant;   // 既定 "card"
  surface?: DisclosureSurface;   // card の地。既定 "surface"
  tone?: DisclosureTone;         // card の状態色。既定 "neutral"
  size?: DisclosureSize;         // 見出しの文字。md 14px / sm 12px
  summaryProps?: HTMLAttributes<HTMLElement>;  // data-testid など
  summaryClassName?: string;     // 見出しの局所的な調整（sticky・余白など）
  contentClassName?: string;     // 内容の領域。card の既定は border-t + p-3、plain は pt-2
  children?: ReactNode;
}
```

- ルートの `<details>` は `data-state="open" | "closed"`、Chevron は `data-state="expanded" | "collapsed"` を持ちます。E2E は `summary` を押して、`details` の `open` 属性か Chevron の `data-state` で確かめます（Chevron に独自の testid を付けない）。
- 見出しの高さは `--button-height-lg`（card、タッチ端末 44px）/ `--button-height-sm`（plain）。hover の地は `--color-surface-hover`（`tone` のときは文字色の 5%）。フォーカスは outline（card は枠の内側）。
- summary のクリックは部品が状態を切り替えます（ブラウザの切り替えを止める）。`summaryProps.onClick` で `preventDefault()` すると開閉しません。
- 内容は閉じている間もマウントされたままです（ページ内検索で見つかる）。重い内容を開いたときだけ取得するなら、`onOpenChange` で状態を持ち、取得の `enabled` に渡します。

### DisclosureChevron の props（変更）

```ts
export interface DisclosureChevronProps extends Omit<LucideProps, "aria-hidden"> {
  /** boolean（推奨）か、`group/disclosure` を付けた <details> に CSS で追従する "group"（入れ子に弱いので新規コードでは使わない）。 */
  expanded: boolean | "group";
}
```

- 折りたたみ = `-rotate-90`（右向き）、展開 = `rotate-0`（下向き）。`transition-transform duration-200 ease-out`、`motion-reduce:transition-none`。`aria-hidden` / `focusable="false"` で読み上げない（状態は `<details>` か `aria-expanded` が伝える）。
- **button + region の開閉**（`Disclosure` で表せないもの）: `aria-expanded` と `aria-controls` を付けたボタンの中に `<DisclosureChevron expanded={open} size={16} />` を置きます（`Button` なら子の末尾。`icon` / `trailingIcon` は回らないので使わない）。共有 `Sidebar` のセクション、`FormActionBar` / `ObjectActionBar` の「その他の操作」も同じ部品です。

## RowTitleButton — **新規**（#421）

一覧の行の先頭セル（または 375px のカード）に置く対象名のボタンです。仕様と判断の理由は README §4「`RowTitleButton`」。

```tsx
import { DataTable, RowTitleButton } from "@production-ready/ui";

<DataTable<Feedback>
  columns={[
    {
      key: "question",
      header: t("feedback.table.question"),
      rowHeader: true,
      render: (item) => (
        <RowTitleButton
          title={item.question}
          subtitle={<QuestionMeta item={item} />}
          current={item.id === selectedId}
          aria-label={t("feedback.list.selectNamed", { name: item.question })}
          onClick={() => onSelect(item.id)}
        />
      ),
    },
    // …
  ]}
  rows={items}
  getRowKey={(item) => item.id}
  selectedRowKey={selectedId}
  onRowClick={(item) => onSelect(item.id)}
/>

// 長い内容は行数で切り詰め、切り詰めたときだけ全文の Tooltip を出す
<RowTitleButton title={entry.content} maxLines={2} current={entry.id === selectedId} onClick={() => select(entry.id)} />

// 識別子の表示部品を題名に渡す（色は部品の既定の --color-fg にそろえる）
<RowTitleButton title={<DbObjectName value={name} size="xs" />} subtitle={<DbObjectCommentText id={commentId} comment={comment} />}
  aria-describedby={commentId} current={name === selectedName} onClick={() => onSelect(name)} />
```

### RowTitleButton の props

```ts
export interface RowTitleButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "title" | "children" | "type" | "aria-current"> {
  /** 対象名。文字列のほか、製品の表示部品も渡せる。 */
  title: React.ReactNode;
  /** 対象名の下の補足（12px、--color-fg-muted）。 */
  subtitle?: React.ReactNode;
  /** 詳細に表示中の対象か。aria-current="true" を付ける。 */
  current?: boolean;
  /** 切り詰める最大行数（1〜3）。既定は切り詰めずに折り返す。 */
  maxLines?: 1 | 2 | 3;
  /** 切り詰めたときの Tooltip の全文。title が文字列なら省略できる。 */
  fullTitle?: string;
  ref?: React.Ref<HTMLButtonElement>;
}
```

| 決めたこと | 理由 |
|---|---|
| `type="button"` の文字だけのボタン（共有 `Button` は使わない）。`aria-label`・`aria-describedby`・`data-*`・`disabled`・`ref` はそのまま渡す | 情報一覧の構造コントロールで、アクションボタンの枠・高さを持たせない。詳細を閉じたときのフォーカスの戻り先を `data-*` や `ref` で探せる |
| 選択の状態は `aria-current="true"` だけ。見た目は行（`DataTable` の `selectedRowKey`）やカードが持つ | 行とボタンの両方が同じ意味の「現在の項目」を持つ（行は背景と左バー、ボタンは Tab で届いたときの読み上げ）。`DataTable` は行の中のボタンのクリックで `onRowClick` を重ねて呼ばない |
| `maxLines` の切り詰めは `ResizeObserver` で `scrollHeight > clientHeight` を測り、切り詰めているときだけ `Tooltip`（`describe={false}`）を有効にする。文言は `rowTitleTooltipText`（先頭 120 文字 + 「…」） | 切り詰めていない題名に同じ文言の吹き出しを出さない。読み上げ名は全文なので説明として二重に結び付けない。長い全文の吹き出しで一覧を覆わない |
| class は `pr-touch-target relative`（タッチ端末の当たり判定）と、題名の `group-hover/row-title:underline` | Button の `--button-height-*` と同じく入力方式で判定し、見た目は変えない |

- 製品の置き換え（#421）: RAG（フィードバック・検索・回答プロファイルの管理・検索・回答プロファイルの用語 / ルール）、Agent（エージェント・承認・ツール・メモリ・MCP サーバー・Skill・Plugin・マーケットプレイス・Run）、NL2SQL（プロファイル・DB 管理のオブジェクト一覧・データ管理の対象の選択・フィードバック管理のエントリ）、system-settings（ユーザー・ロール・ロール権限。`SecurityIdentityRowTitleButton`）。
- 置き換えないもの: 一覧から別ページへ移るリンク（RAG のナレッジベース・ファイル一覧）、カード全体が 1 つのボタンの履歴（NL2SQL のフィードバック履歴）、listbox の選択肢（NL2SQL の DeepSec の対象）、チェックボックスのラベル。
- 単体テストは `packages/ui/tests/row-title-button.test.tsx`。実ブラウザは RAG `e2e/feedback.spec.ts`・Agent `e2e/entity-archetypes.spec.ts`・NL2SQL `tests/e2e/profile-archive-reset.spec.ts`（desktop / 375px、Tab・Enter・Space・`aria-current`・タッチ端末の当たり判定・切り詰めの Tooltip）。

---

## 必須の表示: RequiredBadge — 変更 / FieldLabel・FieldLegend・Fieldset — **新規**（#531）

3 製品と system-settings のフォームで、必須の欄の見せ方と読み上げを 1 通りにします。仕様と判断の理由は README §4「必須の表示」、画面の振る舞い（未入力のエラー・フォーカス）は UX 契約 `messaging.md` §3.2.1。

```tsx
import { FieldLabel, FieldLegend, Fieldset, SelectField, TextField } from "@production-ready/ui";

// 1 行の入力・選択・secret: required だけで「必須」のタグと aria-required が付く（requiredLabel の既定は「必須」）
<TextField id="profile-name" label={t("profiles.field.name")} required value={name} onValueChange={setName}
  error={nameError ?? undefined} />
<SelectField id="region" label={t("settings.oci.region")} required value={region} options={regions} onValueChange={setRegion} />
// 条件付きの必須だけ文言を上書きする（別の見た目を作らない）
<TextField id="compartment" label={t("settings.model.compartment")} required requiredLabel={t("settings.model.requiredInOci")} />

// textarea・ファイル選択・独自の入力: FieldLabel + 入力の aria-required（タグは読み上げない）
<FieldLabel htmlFor="direct-sql" label={t("directSql.field.sql")} required />
<textarea id="direct-sql" aria-required="true" aria-invalid={Boolean(sqlError)} aria-describedby={sqlError ? "direct-sql-error" : undefined} />
<FieldError id="direct-sql-error" message={sqlError} />

// aria-required を持てない独自の入力（グリッドの選択など）: タグを読み上げ対象に残す
<FieldLabel htmlFor="scope-grid" label={t("search.scope")} required requiredAnnouncedByControl={false} />

// チェックボックスの群・複数選択: Fieldset（role=group）。必須は legend の中のタグで伝える
<Fieldset id="user-roles" legend={t("security.users.roles")} required helper={t("security.users.rolesHelper")}
  error={rolesError}>
  {roles.map((role) => <label key={role.code}><input type="checkbox" … /> {role.name}</label>)}
</Fieldset>

// ラジオ: role="radiogroup" で aria-required を群に付ける（タグは読み上げない）
<Fieldset id="scope-mode" legend={t("deepsec.scope.mode")} role="radiogroup" required>…</Fieldset>

// 選択肢のレイアウトを自分で組む fieldset は FieldLegend だけを使ってよい
<fieldset><FieldLegend required>{t("deepsec.targets")}</FieldLegend>…</fieldset>
```

### props

```ts
/** 既定の文言「必須」。packages/ui が持ち、3 製品で同じ語にそろえる。 */
export const DEFAULT_REQUIRED_LABEL = "必須";

export function RequiredBadge(props: {
  /** 既定「必須」。 */
  label?: string;
  className?: string;
  /** 入力側の aria-required が必須を伝えるときは true（二重読み上げを避ける）。 */
  "aria-hidden"?: boolean;
}): JSX.Element;

type RequiredProps = {
  required?: boolean;
  /** 既定「必須」。条件付きの必須（「OCI 運用時必須」）だけ上書きする。 */
  requiredLabel?: string;
  /** 入力・群が aria-required で伝えるなら true（タグは aria-hidden）。FieldLabel の既定 true、FieldLegend の既定 false。 */
  requiredAnnouncedByControl?: boolean;
};

export function FieldLabel(props: {
  id?: string;
  htmlFor: string;
  label: ReactNode;
  className?: string;
  /** タグの後ろに置く要素。 */
  children?: ReactNode;
} & RequiredProps): JSX.Element;

export function FieldLegend(props: { id?: string; children: ReactNode; className?: string } & RequiredProps): JSX.Element;

export type FieldsetProps = {
  id?: string;
  legend: ReactNode;
  /** legend の直下の補足。fieldset の aria-describedby に結ぶ。 */
  helper?: ReactNode;
  /** 群の直下の FieldError（「〇〇を選択してください。」）。aria-describedby に結ぶ。 */
  error?: string | null;
  /** "radiogroup" のときだけ fieldset に aria-required / aria-invalid を付け、タグは読み上げない。 */
  role?: "radiogroup";
  className?: string;
  legendClassName?: string;
  children: ReactNode;
  required?: boolean;
  requiredLabel?: string;
};
```

| 決めたこと | 理由 |
|---|---|
| `requiredLabel` の既定を「必須」にし、`TextField` / `SelectField` / `SecretField` は `required` だけでタグを出す | 呼び出し側が毎回文言を渡す作りでは、渡し忘れた欄が見た目で必須と分からない（WCAG 3.3.2）。`packages/ui` は日本語の既定文言を持つ（「閉じる」「本文へスキップ」と同じ扱い） |
| `FieldLabel` / `FieldLegend` を `packages/ui` に置く（NL2SQL と system-settings が同じ実装を別々に持っていた） | TextField 以外の入力（textarea・ファイル選択・チェックボックスの群・ラジオ・独自の入力）でも、タグの位置・余白・読み上げの扱いを 1 か所で決める |
| `FieldLabel` のタグは既定で `aria-hidden`、`FieldLegend` のタグは既定で読み上げる | label に結ばれた入力は `aria-required` で「必須」と読まれるので、タグも読むと「必須、必須」になる。fieldset（role=group）は `aria-required` を持てない（WAI-ARIA 1.2）ので、legend の中の文字で伝える |
| `Fieldset` の `role="radiogroup"` では fieldset に `aria-required` を付け、タグを読み上げない | radiogroup は `aria-required` を持てるロール。群に入ったときに 1 回だけ伝える |
| 任意の欄には何も付けない。placeholder・ラベルの「(任意)」は lint で検出する | 必須だけを示せば、印の無い欄は任意と分かる（凡例が要らない）。placeholder は入力を始めると消える |

- 製品で `RequiredBadge` を直接ラベルに並べない（adherence の lint が検出する）。ラベルでない所（カードの見出しに付ける条件付きの必須など）に置く必要があるときだけ、理由を添えて局所的に lint を外す。
- 単体テストは `packages/ui/tests/required-field.test.tsx`（既定の文言・`aria-required`・タグの読み上げの扱い・任意の欄に何も付かないこと・`Fieldset` の `aria-describedby`）。lint の検出と許容の例は RAG `frontend/src/design-system-adherence.test.ts`。

---

## SearchField — **新規**（#535）

一覧の絞り込み（画面上の一覧・表を名前などで絞る）の検索欄。入力に合わせて絞り込み（debounce 300ms、Enter はすぐ）、IME の変換中は絞り込まず、消去と件数の読み上げを持ちます。検索ボタンは置きません。決めたことの表は README §4「`SearchField`」、どの検索に使うか（重い検索は明示実行）は UX 契約 [page-archetypes.md「一覧の絞り込みの検索」](../ux-contracts/page-archetypes.md#一覧の絞り込みの検索535)。

```tsx
import { ClearActionButton, EmptyState, SearchField } from "@production-ready/ui";

// サーバー側で絞り込む一覧（RAG の検索・回答プロファイル）。q は作業状態に保存した適用中の検索語。
const [view, setView] = useWorkspaceState("searchAnswerProfiles.view", INITIAL_VIEW, isView);
const query = useSearchAnswerProfiles({ q: view.q || undefined, limit, offset: view.offset });
// ↑ queryKey に q を入れ、placeholderData: keepPreviousData（古い応答で上書きしない・前の一覧を出したまま）

<SearchField
  id="search-answer-profile-search"
  label={t("searchAnswerProfiles.search.placeholder")}
  labelHidden
  value={view.q}
  onSearch={(next) => setView((current) => ({ ...current, q: next, offset: 0 }))} // 変わったら 1 ページ目
  clearLabel={t("common.clearSearch")}
  resultCountLabel={query.data ? t("common.searchResultCount", { count: query.data.total }) : ""}
  placeholder={t("searchAnswerProfiles.search.placeholder")}
  className="w-full sm:w-64"
/>

// 0 件: 空の状態と「検索語をクリア」
{view.q ? (
  <EmptyState
    title={t("searchAnswerProfiles.search.noResultsTitle")}
    hint={t("searchAnswerProfiles.search.noResultsHint")}
    action={<ClearActionButton label={t("common.clearSearch")} onClick={() => applySearch("")} />}
  />
) : null}

// 所有者名のように、入力中も大文字で見せ、確定した値も大文字にする（NL2SQL の DbOwnerPrefixFilterField）
<SearchField id="owner" label="所有者" value={owner} onSearch={setOwner} clearLabel="入力をクリア"
  formatInput={(v) => v.toUpperCase()} normalize={(v) => v.trim().toUpperCase()} />
```

### SearchField の props

```ts
export const SEARCH_FIELD_DEBOUNCE_MS = 300;

export type SearchFieldProps = Omit<
  TextFieldProps,
  "type" | "value" | "defaultValue" | "onChange" | "onValueChange" | "onClear" | "leadingIcon" | "required" | "requiredLabel"
> & {
  /** 適用中の検索語（作業状態・URL に保存している値）。外から変わったときだけ入力欄を合わせる。 */
  value: string;
  /** 確定した検索語（正規化済み）。入力が止まって debounceMs・Enter・消去・入力欄が外れるときに呼ぶ。IME の変換中は呼ばない。前回と同じなら呼ばない。 */
  onSearch: (value: string) => void;
  /** 消去ボタンの読み上げ名と Tooltip（翻訳済み）。 */
  clearLabel: string;
  /** 既定 300。0 なら確定した入力のたびにすぐ呼ぶ。 */
  debounceMs?: number;
  /** onSearch に渡す前の正規化（既定は trim）。 */
  normalize?: (value: string) => string;
  /** 入力中の文字の見せ方（例: 大文字）。IME の変換中は適用しない。 */
  formatInput?: (value: string) => string;
  /** 件数の文言。検索語があるときだけ role="status"（aria-live="polite"、sr-only）で読み上げる。 */
  resultCountLabel?: string;
  /** 入力中の文字（下書き）が変わったとき。絞り込みには onSearch を使う。 */
  onDraftChange?: (draft: string) => void;
};

// IME 対応の Enter の判定（重い検索の明示実行・条件フォーム・追加の入力欄で使う）
export function isImeComposing(event: KeyboardEventLike): boolean; // isComposing または keyCode 229（Safari）
export function isSubmitEnter(event: KeyboardEventLike): boolean;  // key === "Enter" かつ変換中でない
```

| 決めたこと | 理由 |
|---|---|
| 入力中の文字（下書き）は部品が持ち、`value` は適用中の値だけにする。最後に `onSearch` へ渡した値を覚え、`value` がそれと違う値に変わったときだけ外からの変更として入力欄を合わせる | 親が `trim` した値・大文字にした値を返しても、入力中の空白・文字を書き換えない |
| debounce のタイマーは部品の中の 1 つ。親で `useDebouncedValue` などを重ねない（NL2SQL の 5 ページの 250ms を外した） | 反映が 550ms に遅れない。debounce と IME の判定を 1 か所にする |
| `compositionstart` で待っている分を取り消す。`compositionend` で確定した値を予約する（ブラウザにより確定後の `input` が来ないため） | 変換の途中の読みで絞り込まない。確定した値を取りこぼさない |
| Enter は `preventDefault` して囲む form を送信しない。変換を確定する Enter は既定の動作も止めない | 一覧の絞り込みは form の送信ではない。IME の確定を妨げない |
| 入力欄が外れるときは待っている分を確定する。部品の外の状態（作業状態）が同じ画面に残るとき、消した・入力した検索語が戻らない。画面ごと外れる（別のページへ移る）ときは親の状態も消えるので、残したい e2e は Enter で確定してから移る | 一覧 ⇄ 作成の切り替えで、消したはずの検索語が一覧に戻っていた |

- 単体テストは `packages/ui/tests/search-field.test.tsx`（debounce・Enter・trim・正規化・`formatInput`・IME の `compositionstart` 〜 `compositionend` と確定の Enter・消去・外からの変更・外れるときの確定・件数の読み上げ。fake timers）。
- 実ブラウザは RAG `e2e/list-search.spec.ts`（検索・回答プロファイル・ナレッジベース。ボタンなし・入力に合わせた問い合わせ・IME・0 件の「検索語をクリア」、desktop / 375px、ライト / ダーク）、Agent `e2e/list-search.spec.ts`（メモリ）、NL2SQL `tests/e2e/nl2sql-workflows.spec.ts`（学習候補・アプリ内フィードバック）。IME は `compositionstart` → `isComposing` の `input` → `compositionend` の DOM event を出して確かめる（Playwright の keyboard は IME を通さない）。
- 製品の置き換え: RAG（ナレッジベース・検索・回答プロファイル・文書・フィードバック・ナレッジベース詳細の追加する文書）、NL2SQL（`DbManagementSearchField` / `DbOwnerPrefixFilterField` を使う全一覧・DB 管理のオブジェクト一覧・スキーマ参照・アプリ内フィードバック・学習候補）、Agent（メモリ）、system-settings（`SecuritySearchField`: ユーザー・ロール・権限管理・権限の対象。NL2SQL の Deep Data Security も使う）。

## SearchableSelectField / SearchableMultiSelect — **新規**（#578）

数十〜数百件の選択肢から検索して選ぶ部品（単一・複数）。十数件までの固定の選択肢は `SelectField`、大量の候補から一覧で見比べて選ぶもの（表・ビューの選択など）は別の一覧型の部品にする。見た目の変更は README §7 の 50。

```tsx
import { SearchableMultiSelect, SearchableSelectField } from "@production-ready/ui";

// 複数選択: 検索欄（combobox）＋ 候補の一覧（開いている間だけ）＋ 選択済みの chip
<SearchableMultiSelect
  id="kb-scope"
  label="参照するナレッジベース"
  required
  options={options}                 // { value, label, description?, meta?, badge?, searchText?, hideable? }[]
  value={selectedIds}
  onValueChange={setSelectedIds}
  selectedOptions={chips}           // 候補のページに無い選択済みの名前・状態（任意）
  onQueryChange={setQ}              // 確定した検索語（300ms・Enter・IME の確定後）
  remote={many ? { total, hasMore, loadingMore, searching, onLoadMore } : undefined}
  labels={{ searchPlaceholder: "ナレッジベースを検索して追加…" /* ほか */ }}
/>

// 単一選択: ボタン（選択中の名前を折り返して全体を出す）→ 検索欄と候補の一覧を重ねて開く
<SearchableSelectField
  id="kb-filter"
  label="ナレッジベース"
  value={value}
  options={options}
  selectedOption={current}          // 候補のページに無い選択中の名前（任意）
  onValueChange={setValue}
  onQueryChange={setQ}
  remote={many ? { total, hasMore, loadingMore, searching, onLoadMore } : undefined}
/>
```

| 決めたこと | 理由 |
|---|---|
| 検索欄は `SearchField`（debounce 300ms・Enter はすぐ・IME の変換中は絞らない・× と Esc で消す）を role=combobox で使う。親で遅延させない | #535 の規則を 1 か所で守る。変換の途中の読みで問い合わせない |
| `remote` を渡さなければ `options` を画面側で絞る（全角半角・大文字小文字を区別しない部分一致、空白区切りはすべて含む）。渡すと絞らず、`onQueryChange` の検索語で呼び出し側が問い合わせる | 件数の少ないときは 1 文字ごとに問い合わせない。多いときは全件を読まない（切り替えの基準は製品が決める。RAG の KB は 201 件以上） |
| ↑↓ で強調（`aria-activedescendant`）、Enter で選ぶ。IME の変換中の矢印・Enter は候補の操作に使わない。複数選択は選んでも閉じず、「完了」・Esc で閉じて検索欄へ戻る。閉じた後の Esc で検索語を消す | APG の Combobox。続けて複数を選べる。Esc 1 回で入力まで消えない |
| 単一選択の開いた層は `role="dialog"`（非モーダル）。開くと検索欄へ、選ぶ・Esc・Tab で閉じてボタンへ戻る（Shift+Tab はボタンに止まる）。ボタンの名前はラベルと選択中の名前（`aria-labelledby`） | Portal の層に Tab で入れない問題を避け、フォーカスの行き先を 1 つにする。読み上げで今の値が分かる |
| 続きのページは、複数選択は「さらに表示」、単一選択は一覧の下端までのスクロールか最後の候補からの ↓ | 単一選択の層の中に Tab で届くボタンを置かない |
| 選択済みは chip（`<ul aria-label="選択中の…">`）で、名前を省略せず折り返す。`badge` で状態（アーカイブ済み・見つかりません）を文字で添える | 長い名前を最後まで読める。色だけに頼らない |
| 単一選択の一覧の高さは一覧側（`bounded-scroll-area`）で決め、層全体は実寸で測る | 層に max-height を掛けると実寸の測り直しで高さが揺れる |
| **複数選択の開いた一覧（パネル）は欄の幅以上**（#2091）。欄の左端から右へ、読みやすい幅（`SEARCHABLE_PANEL_MIN_WIDTH_REM` 36rem）まで、いちばん近い枠のある箱（Card など）の内容の端の中で広げる。下部の行は左に「N 件選択中」（折り返さない）、右に操作の組（「表示中をすべて選択」「クリア」「完了」。`sm`）を 1 行。右に入らないときだけ、画面・スクロールの箱の余白（1rem）の内側まで右へ広げて左へずらし（375px は画面の幅）、それでも入らなければ件数の文と操作の組を 2 段にする（操作の組は分けない）。隣に並ぶ要素（grid の隣の欄・サイドナビ）の手前で止める。幅は開いたときと大きさの変わったときに測る（`computeSearchablePanelLayout`） | 欄の幅（`lg` 28rem）に縛ると、下部の件数の文が 2 行に折り返し「完了」が次の行へ落ちた。Radix の Popover・MUI Autocomplete の一覧と同じく「トリガーの幅以上・中身に要る幅」にする。一覧は通常の流れの中にある（後ろのカードの下に隠れない）ため、隣の欄・カードの外へは広げない（#1794） |
| **単一選択の閉じたボタンに虫眼鏡（`Search`）を付けない。** ボタンは選んだ値（未選択なら placeholder）と開閉の印（`ChevronDown`）だけを出し、虫眼鏡は開いた層の中の検索欄（`SearchField`）だけが持つ（#1771） | ボタンで開き、層の中で検索する選択欄（shadcn/ui の Combobox・Radix の Popover + Command・MUI の Select）の一般的な形。ボタンは文字を打てる欄ではなく、虫眼鏡を付けると検索欄に見え、開くと虫眼鏡が 2 つ縦に並ぶ。複数選択（`SearchableMultiSelect`）は欄そのものが検索欄（文字を打てる autocomplete）なので、先頭の虫眼鏡はそのまま |

- 開いた一覧の幅の計算は `packages/ui/tests/searchable-panel-layout.test.ts`、実ブラウザの下部の行と位置は共通の e2e の検査 `measureSearchablePanels`（`packages/ui/e2e/layout-checks.ts`。RAG の `expectSearchablePanelFits`・Agent の `agent-data-scope.spec.ts`）（#2091）。
- 単体テストは `packages/ui/tests/searchable-select.test.tsx`（300 件の画面側の絞り込み・`remote`・↑↓/Enter/Esc/Tab・IME の `compositionstart`〜`compositionend` と確定の Enter・chip・hideable・件数の読み上げ・ボタンの名前）。
- 実ブラウザは RAG `e2e/knowledge-base-searchable-select.spec.ts`（モックで 300 件と 120 件。評価・文書インデックス・アップロード・検索・回答プロファイル、desktop / 375px、ライト / ダーク）。
- 製品の置き換え: RAG（文書インデックスの絞り込み、アップロードの登録先、検索・回答プロファイルの参照 KB、品質評価、文書詳細の所属先、RAG 検索・チャットの対象の検索・回答プロファイル（#635 で単一選択の `SearchableSelectField` に統一））。RAG 固有の `MultiSelectCombobox` は削除した。
- `leadingIcon`（任意。#635、#1771 で用途を限った）: 単一選択のボタンの先頭に 16px のアイコン（読み上げない）を出す。**対象の種類を表すアイコン（`Database`・`Bot` など）にだけ使い、虫眼鏡（`Search` 系）は渡さない**（adherence の lint が検出する。上の表）。#635 の RAG の対象の検索・回答プロファイルと NL2SQL のチャットの業務プロファイルの `leadingIcon={Search}` は #1771 で外し、3 製品の単一選択はどれもボタンにアイコンを持たない。

---

## ApiErrorBanner / ApiErrorState / presentApiError — **新規**（#900 / #906）

API の失敗を、**利用者向けの要約（何が起きたか）・次の操作・「詳細」（技術的な情報）**に分けて出す部品と関数（UX 契約 messaging.md §10.3.1）。NL2SQL の #900 の仕組みを platform に移し、3 製品で使う。

```tsx
// 失敗の面（Banner）。要約・次の操作・開いた「詳細」（要求・待ち時間の上限・エラー種別・元の文・HTTP ステータス・request ID）
<ApiErrorBanner error={query.error} fallback={t("…loadError")} testId="…-error" />

// 領域の取得の失敗（ErrorState + 「詳細」。再試行付き）
<ApiErrorState error={query.error} fallback={t("…loadError")} onRetry={() => void query.refetch()} retryLabel={t("common.retry")} />

// 1 つの文しか出せない所（Toast・FormStatus・SaveErrorBanner）
toast.error(apiErrorMessage(error, t("…failed")));
```

| 関数・型 | 役割 |
|---|---|
| `ApiTransportError` / `toApiTransportError(cause, request)` | fetch・本文の読み取りが投げた timeout（`TimeoutError`）・通信断（`TypeError`）を、日本語の要約 + 次の操作の例外にする。英語の元の文は `causeMessage`。`AbortError` は `null`（変換しない） |
| `transportErrorOf(error)` | `ApiTransportError` か、製品の `ApiError` が `cause` に包んだものを取り出す（RAG） |
| `ApiErrorPresentable` / `httpApiErrorPresentation` | 製品の `ApiError` が `toApiErrorPresentation()` を実装し、backend の文を要約に、HTTP ステータス・エラーコード・request ID を詳細に分ける |
| `presentApiError(error, fallback)` | 要約・次の操作・詳細を返す。組み込みの例外（英語の文）は既定の文にし、元の文は詳細へ |
| `apiErrorMessage(error, fallback)` | 1 つの文。timeout・通信断は要約 + 次の操作、組み込みの例外は既定の文、それ以外は `message` のまま |

- 既定の文言（`DEFAULT_API_TRANSPORT_MESSAGES` / `DEFAULT_API_ERROR_DETAIL_LABELS`）は日本語。`ApiErrorDetailList` は「詳細」の折りたたみ（`Disclosure`）だけを出す。`ErrorState` は `details` で同じ折りたたみを本文の下に置ける。
- 単体テストは `packages/ui/tests/api-error.test.tsx`。製品の確認は各製品の API のラッパーのテストと e2e（`route.abort` で通信断）。

---

## OpenConversationAction — **新規**（#2343）

実行の詳細の概要の見出しの右に置く「会話を開く」（UX 契約 run-history.md「詳細」）。3 製品で同じ部品・同じ文言にする。

```tsx
<OpenConversationAction
  href={conversationHref}            // 無ければ描かない（チャットでない実行・会話を見られない人）
  linkComponent={Link}
  labels={{ label: t("…openChat"), ariaLabel: t("…openChatLabel") }}
  testId="…-open-chat"
/>
```

- 見た目は `ButtonLink`（secondary）+ `MessagesSquare`。既定の文言は「会話を開く」「この実行の会話をチャットで開く」。
- 出すかどうか（URL）は製品が決める: RAG `RunHistoryClient`・NL2SQL `historyConversationHref`・Agent `runConversationHref`。
- 単体テストは `packages/ui/tests/open-conversation-action.test.tsx`。

---

## OpenRunInputAction — **新規**（#3309）

実行の詳細の概要の見出しの右に置く「〜で開く」（UX 契約 run-history.md「詳細」）。会話の無い 1 回ごとの実行（RAG 検索・
NL2SQL の SQL 生成）を、元の画面に質問と条件を入れて開く。開いた画面は実行しない。

```tsx
<OpenRunInputAction
  href={inputHref}                   // 無ければ描かない（本人の実行でない・画面の権限が無い）
  linkComponent={Link}
  labels={{ label: t("…openSearch"), ariaLabel: t("…openSearchLabel") }}  // 文言は製品が渡す（必須）
  testId="…-open-search"
/>
```

- 見た目は `ButtonLink`（secondary）+ `SquarePen`。`OpenConversationAction` と同じ位置・同じ寸法で、1 つの実行にはどちらか 1 つだけを出す。
- 出すかどうか（URL）は製品が決める: RAG `runSearchHref`（`run-history-view.ts`）・NL2SQL `historySqlGenerationHref`（`historyManagementState.ts`）。URL の初期値は RAG `search-prefill.ts`・NL2SQL `queryPrefillState.ts`。
- 単体テストは `packages/ui/tests/open-run-input-action.test.tsx`。

---

## ProcessFailureNotice / processFailureDetails / processFailureFacts / failedStepName — **新規**（#2342）

処理（チャットの回答・SQL の生成と実行・Run・文の実行）の失敗を、3 製品で同じ形に出す失敗のカード（UX 契約
messaging.md §9.1）。製品は自分の失敗の情報を `ProcessFailure` に写すだけにし、並べ方・「詳細」の項目は部品が持つ。

```tsx
<ProcessFailureNotice
  failure={{
    summary: "「文書の検索」で処理が止まりました。",   // 何が起きたか（止まった段を含む 1 文目）
    cause: "検索処理中にエラーが発生しました。",       // 原因（backend の利用者向けの文・DB の原文）
    explanation: undefined,                            // コードから引ける平易な説明（ORA-00904 → 列名が存在しない）
    nextAction: "もう一度送信してください。続くときは、詳細のトレース ID を添えて管理者に連絡してください。",
    failedStep: "文書の検索",                          // 「詳細」: 止まった段
    errorType: "DatabaseError", errorCode: "ORA-03146", traceId, occurredAt,
    extraDetails: [{ label: "元のメッセージ", value: raw }],
  }}
  labels={processFailureLabels()}                      // 製品の i18n（トレース ID / ジョブ ID / 実行 ID など）
  action={<Button icon={RotateCcw} …>もう一度送信</Button>}
  testId="chat-answer-failure"
/>
```

| 部品・関数 | 役割 |
|---|---|
| `ProcessFailureNotice` | `severity="danger"`（既定。結果が無い失敗。`role="alert"`、「詳細」は開く）/ `"warning"`（結果はあるが一部が欠けた。例: 作った回答を保存できなかった。「詳細」は閉じる）。原因が 1 文目と同じなら重ねない。値の無い「詳細」の項目は並べない |
| `processFailureDetails(failure, labels)` | 「詳細」の並び（止まった段・エラーの種類・エラーコード・トレース ID・時刻・製品の項目） |
| `processFailureFacts(failure, labels)` | 実行の詳細の技術的な詳細（`RunFactList`）に並べる事実。失敗のカードの「詳細」と同じ項目・同じ名前 |
| `failedStepName(steps)` | 処理の経過の最後の失敗した段の名前。名前を状態で変える段階（「SQL を生成できませんでした」）は名詞の名前 `ChatProgressStep.name`（「SQL の生成」。`labelChatProgressSteps` が段階の定義の未実行の名前から付ける）を使う |

- 処理の経過（`RunTimeline` / `ChatProgress`）は失敗した段を ✗ と「失敗」、失敗・停止の後に実行しなかった段を「未実行」と文字で出す（`ChatProgressLabels.notRun`）。
- 単体テストは `packages/ui/tests/process-failure.test.tsx`。製品の写し方は RAG `lib/process-failure.ts`・NL2SQL `features/nl2sql/processFailure.ts`・Agent `lib/process-failure.ts`。

---

## PreconditionBanner — **新規**（#1688）

**操作の前提（設定・資源）が足りないので、その操作ができない**ことを、操作の区画の先頭に常設で示す warning の Banner。UX 契約 messaging.md §3.4.2 の実装。ページ全体が使えないときは `BlockedPageNotice`（§3.4）、DB の不通は `DatabaseGate` / `DatabaseUnavailableNotice` を使う。

```tsx
<PreconditionBanner
  id="save-oci-precondition"
  title="OCI 認証が設定されていません"
  message="この操作には OCI 認証の設定が必要です。設定するまで保存できません。システム設定の「OCI 認証」で設定してから、再試行してください。"
  settingsLink={canOpen ? { to: "/settings/oci", label: "OCI 認証を開く", icon: KeyRound, linkComponent: Link } : undefined}
  onRetry={recheck}
  retrying={rechecking}
/>
<Button disabled={blocked} aria-describedby={blocked ? "save-oci-precondition" : undefined}>保存</Button>
```

### PreconditionBanner の props

```ts
type PreconditionBannerLink = {
  to: string;
  /** 「〇〇を開く」（〇〇はサイドナビの表示名）。 */
  label: string;
  /** 移動先のナビのアイコン。 */
  icon: LucideIcon;
  linkComponent?: ButtonLinkComponent;
  testId?: string;
};

type PreconditionBannerProps = {
  /** 押せなくした操作の aria-describedby に渡す id。 */
  id?: string;
  title: string;
  message: ReactNode;
  /** 前提を満たす画面を開ける利用者のときだけ渡す。 */
  settingsLink?: PreconditionBannerLink;
  onRetry?: () => void;
  /** 既定「再試行」。 */
  retryLabel?: string;
  retrying?: boolean;
  retryTestId?: string;
  testId?: string;
  className?: string;
};
```

| 決めたこと | 理由 |
|---|---|
| warning の `Banner`（`role="status"`・アイコン付き）。閉じる × は付けない | 失敗ではなく状況（messaging §3.4）。前提を満たすまで残す |
| 見出しは何が足りないか、本文は「何に要るか → 作るまで何ができないか → 運用設定の「〇〇」で…してから、再試行してください。」 | DB ゲート（§3.4.1）と同じ文の形。押せない理由を常設で示す（README §4「`disabled` のボタンはフォーカスを受けないので、使えない理由は画面の文言で示す」） |
| 導線は `ButtonLink`（secondary・sm・移動先のナビのアイコン・`ArrowRight`）、続けて「再試行」（secondary・sm・`RefreshCw`） | DB の縮退の Banner と同じ部品と寸法。導線を再試行より前に置く（§3.4.1。Tab 順） |
| 導線は前提を満たす画面を開ける利用者だけに出す。開けない利用者には本文で「システム管理者に連絡して、…を依頼してください。」 | 押しても開けない導線を出さない（§3.4.1） |
| 押せなくした操作は `disabled` にし、`id` を `aria-describedby` で結ぶ | 読み上げでも押せない理由が分かる |
| 「再試行」の `loading` は押したときだけ | 画面のフォーカスでの取り直しで回さない（buttons.md §8） |

- 単体テストは `packages/ui/tests/precondition-banner.test.tsx`。
- 使う画面: 今は無い（最初に使った NL2SQL の事前条件の画面は、その機能とともに削除した。#7）。

---

## FormActionBar の確定の行 — 変更（#1786。#2715 で `sticky` を削除）

詳細・作成・編集の画面の確定（作成・保存と変更を破棄）の行。フォームの末尾に置き、画面の下端に貼り付けない（内容と一緒にスクロールする）。規則は README §4「詳細・作成・編集の画面の操作」。

```tsx
<form ref={formRef} onSubmit={handleSubmit} noValidate className="grid gap-4">
  {/* 欄 … */}
  <SaveErrorBanner message={saveError} attemptKey={mutation.submittedAt} testId="…-save-error" />
  <FormActionBar
    ariaLabel={t("editor.actions.formLabel")}   // 「確定の操作」
    primaryActions={[{ id: "save", label: initial ? t("…save") : t("…create"), icon: Save, type: "submit", loading: pending }]}
    secondaryActions={[{ id: "discard", label: t("editor.actions.discard"), icon: RotateCcw, disabled: !dirty || pending, onClick: discard }]}
  />
</form>
```

| 決めたこと | 理由 |
|---|---|
| 1 枚のカードのフォームはカードの中のフォームの**最後の子**、複数のカードを 1 つの保存で確定する画面（Agent のエディタなど）は保存の対象の節を包む `div` の最後の子（最後のカードの下）。設定のカードの末尾の行（RAG の言語資源など）と同じ形 | 行が保存の対象のフォームの末尾にだけあり、節ごとに保存する下の節（検索・回答プロファイルの「プロファイルの知識」など）の近くにページの保存が出ない |
| `position` も重なり順（z-index）も持たない（#2715 で `sticky` を削除） | #1786 の `sticky bottom-0`（`--z-sticky`）は貼り付く範囲が親のカードの中だけで、カードの末尾を過ぎると行が上へ流れ、lg 以上の `PageHeader`（同じ `--z-sticky`）の上に DOM の順で描かれた。今は `PageHeader` の下へ隠れる |
| sm 未満は primary と変更を破棄を全幅で縦に積み、sm 以上は横に並べる | フォームの末尾のほかの行（設定のカード）と同じ |
| 直置きのボタンは mousedown でフォーカスを動かさず、click でボタンへフォーカスを移してから操作する | 押した瞬間の入力欄の blur の検証でエラーが出て行が動き、クリックが外れるのを防ぐ。キーボード・押した後のフォーカス（loading の間もボタンに残る。#835）は変わらない |
| 行の目印は `data-form-action-bar` | e2e の `findFloatingFormActionBars`（`packages/ui/e2e/layout-checks.ts`）が、本文を上から下までスクロールして行が画面に貼り付かず、`PageHeader` の上に描かれないことを確かめる |

- 単体テストは `packages/ui/tests/action-components.test.tsx`（sticky / fixed / z を持たない・区切り線・並び）。
- E2E: RAG `security-management.spec.ts`（ユーザー・ロールの作成と編集の確定の位置・順・左端と、貼り付き・重なりが無いこと）、RAG `search-answer-profiles.spec.ts`（長いフォームの末尾に置き、「プロファイルの知識」まで進んでも貼り付かず `PageHeader` に重ならない）、Agent `entity-archetypes.spec.ts`（最後のカードの下の行と失敗の Banner の位置）。

---

## CopyButton / copyTextToClipboard — **新規**（#2099。RAG の `CopyTextButton`（#2086）・NL2SQL と system-settings の `copyTextToClipboard` を移した）

文字をクリップボードへコピーするボタン（SQL・DDL・ID・URL・ログ・コマンド・本文・一時パスワード）。3 製品と共有の画面のコピーのボタンはすべてこの部品で、製品で `navigator.clipboard` を呼ばない（adherence の lint が検出する）。

```tsx
<ContentActionBar ariaLabel={t("history.detail.sqlActions")}>
  <CopyButton
    size="sm"
    text={sql}
    label={t("history.action.copySql")}          // 「SQL をコピー」
    copiedMessage={t("history.copied")}           // 「SQL をコピーしました。」（読み上げ）
    failedMessage={t("history.copyFailed")}       // 「SQL をコピーできませんでした。」（Toast）
  />
</ContentActionBar>

// 押してから取得する内容（取得の間 loading）。失敗は行の直下に出す
<CopyButton
  size="sm"
  text={() => api.exportExtraction(id, format).then((result) => result.content)}
  label="コピー"
  aria-label={`${label} をコピー`}
  copiedMessage={`${label} をコピーしました`}
  failedMessage={`${label} をコピーできませんでした。もう一度お試しください。`}
  onCopied={() => setFailed(null)}
  onCopyError={() => setFailed(format)}
/>
```

| 決めたこと | 理由 |
|---|---|
| アイコンは `Copy`。コピーできたら `COPY_FEEDBACK_MS`（2 秒）の間 `Check` にし、`copiedMessage` を `role="status"`（`aria-live="polite"`）で読み上げる。ラベルは変えない | ラベルを「コピーしました」に変えると幅が変わり、同じ行の操作が動く。成功を色だけでなく形（アイコン）と読み上げで伝える |
| 読み上げの文（`role="status"`）は `sr-only` を `fixed` にして画面の左上に置く | absolute のままだと行の右端のボタンの後ろで本文に横スクロールを作り、左上に寄せると表・一覧のスクロール領域から抜け出す（#2064 と同じ型。e2e の `findEscapedAbsoluteElements`） |
| 成功の Toast は出さない | コピーは押した場所で結果が分かる局所の操作。Toast は離れた場所に出て、続けて押すと積み上がる（messaging.md §4.2） |
| 失敗は既定で Toast（error）。固定面・秘密の欄（`OneTimeSecret`・一時パスワード・設定のプレビュー・抽出エクスポート）は `onCopyError` を渡して同じ場所の `FormStatus` に出し、`onCopied` で消す | 失敗は見落とさせない。秘密の欄は文字を選んで手でコピーできるように欄の近くに出す |
| `text` に関数を渡すと押したときに求める。Promise を返すと取得の間 `loading` にし、`ClipboardItem` が使えるときは取得を待つ前に Promise のまま渡す | Safari はクリックの処理の中で clipboard を呼ばないと書き込みを拒否する（#561） |
| secure context でない（http の配備）・Clipboard API が無いときは隠した textarea と `execCommand("copy")` で書き、フォーカスを戻す（`copyTextToClipboard`） | 自作の CA を入れる前の http の確認でもコピーできる |
| 見た目は共有の `Button`（既定 `variant="secondary"`、文字のすぐ横の小さな操作は `ghost`）。`size` は同じ行の部品と同じ値を渡す（内容のコピーは `sm`、`FieldActionRow` の中は欄と同じ `md`） | README §4「操作部品の高さと幅」。`type="button"` 固定でフォームを送信しない |

- props: `text`（`string` / `() => string | Promise<string>`）・`label`・`copiedMessage`・`failedMessage`・`onCopyError?(message)`・`onCopied?`・`testId?`・`variant?`（`secondary` / `ghost`）と `Button` の `size` / `iconOnly` / `disabled` / `aria-label` / `className` など（`icon` / `loading` / `onClick` / `children` は部品が持つ）。
- 状態は `data-copy-state="idle" | "copied"` でも見える（e2e はこれと `role="status"` の文言で確かめる）。
- 単体テスト: `platform/packages/ui/tests/copy-button.test.tsx`・`tests/clipboard.test.ts`。

## OneTimeSecret — **新規**（#1805。Agent の #790 から共有へ移した）

発行・作成の直後に **1 回だけ** 見せる秘密（API キーなど）。起点の操作（「発行」の `FormActionBar`）の直下・カードの全幅に、success の `Banner` の中で読み取り専用の入力欄 + コピー + 「保管しました」を出す（messaging.md §10.1）。

```tsx
{created ? (
  <OneTimeSecret
    key={created.token}
    id="api-key-created-token"
    title="API キーを発行しました"
    description="このキーは今だけ表示します。…"
    label="API キー"
    value={created.token}
    copyLabel="コピー"
    copiedMessage="API キーをコピーしました"
    copyFailedMessage="コピーできませんでした。欄の文字を選択してコピーしてください。"
    doneLabel="保管しました"
    onDone={() => setCreated(null)}
  />
) : null}
```

| 決めたこと | 理由 |
|---|---|
| 秘密は読み取り専用の入力欄だけに出す。Toast・console・Banner の本文に入れない | Toast はスクリーンショット・通知の履歴・読み上げに残りやすい |
| コピーの失敗は秘密を含まない中立の文言を欄の直下に出し、欄の文字を選択する | 手でコピーできるようにする（クリップボードが使えない環境） |
| 「保管しました」で閉じる。閉じたら二度と出さない（秘密は保存していない） | 発行と保管の確認を 1 か所にする |

## SaveErrorBanner — **新規**（#585。#1786 で置き場所を変更）

詳細・作成・編集の画面（全画面のエディタ）で、**欄に結び付かない保存の失敗**を 1 か所に出す部品。UX 契約 messaging.md §3.3.1 の実装で、**確定の行（フォームの末尾の `FormActionBar`）の直上**に置く（#1786。#585 では `PageBody` の最初の子だった）。欄に結び付く失敗は欄の直下（`FieldError`）に出し、この部品にも Toast にも重ねない。

```tsx
<form …>
  {/* 欄 … */}
  <SaveErrorBanner
    message={mutation.isError ? (mutation.error instanceof ApiError ? mutation.error.message : t("…error.save")) : null}
    attemptKey={mutation.submittedAt}
    testId="search-answer-profile-save-error"
  />
  <FormActionBar … />
</form>
```

### SaveErrorBanner の props

```ts
type SaveErrorBannerProps = {
  /** 失敗の文言（原因 + 次の行動）。空・null なら何も描かない。 */
  message?: string | null;
  /** 保存を試みるたびに変わる値（mutation.submittedAt など）。同じ文言の失敗でも画面に入れ直す。 */
  attemptKey?: string | number;
  title?: string;
  testId?: string;
  className?: string;
};
```

| 決めたこと | 理由 |
|---|---|
| 見た目は danger の `Banner`（アイコン付き・`role="alert"`）。閉じる × は付けない | 状態を色だけで示さない。次の保存まで残し、失敗を消して保存し直したように見せない |
| 失敗が出たら `scrollIntoView({ block: "center" })` で画面に入れる。フォーカスは動かさない | 長いフォームの途中から（入力欄の Enter などで）保存すると、フォームの末尾の Banner は画面の外にある。中央へ寄せると Banner とその直下の確定の行が並んで見え、上端に貼り付く `PageHeader` にも隠れない |
| Toast・フォームの下の `FormStatus` と併用しない | 同じ失敗が 2 か所に出ていた（検索・回答プロファイルなど） |

- 単体テストは `packages/ui/tests/save-error-banner.test.tsx`（空のときは描かない・`role="alert"`・失敗と再試行のときだけ画面に入れる）。
- 使う画面: system-settings（ユーザー・ロール・権限管理）、RAG（検索・回答プロファイル・ナレッジベースのエディタ）、Agent（Agent・Skill・MCP 接続・評価セット・自動実行）。NL2SQL の業務プロファイルは保存ボタンが確認語の欄と並ぶので、ボタンの直下の `FormStatus`（§3.3）。

---

## ListToolbar / ListPicker / LoadMoreFooter — **新規**（#600）

一覧の上のツールバー（検索欄の位置）と、数千〜数万件の候補から一覧で複数を選ぶ部品。数十〜数百件を選択欄で選ぶものは `SearchableSelectField` / `SearchableMultiSelect`（#578）。規則は UX 契約 page-archetypes.md「一覧のツールバー」「大量の候補から選ぶ」、見た目の変更は README §7 の 53。

```tsx
import { ListPicker, ListToolbar, SearchField, FormActionBar } from "@production-ready/ui";

// 一覧のツールバー: 左に検索（先頭・lg 28rem）→ 絞り込み、右に件数 → 一覧への操作。検索欄に幅の class を付けない。
<ListToolbar
  search={<SearchField id="kb-docs-search" label="所属文書を検索" labelHidden value={q} onSearch={setQ} clearLabel="検索語をクリア" />}
  filters={<FilterSelect id="kb-docs-kind" label="種類" … />}   // 絞り込みは FilterSelect / FilterChipGroup（#1933。下の「一覧の絞り込み」）
  count={{ total: 25 }}                  // 「全 25 件」（#1891。以前は summary="25 件"）
  actions={<Button variant="secondary" icon={FilePlus2} aria-expanded={open}>文書を追加</Button>}
/>

// 大量の候補から選ぶ: サーバーの検索（q）と追加読み込み（total / hasMore / loadingMore / onLoadMore は #578 の remote と同じ名前）
<ListPicker
  id="kb-add-documents"
  title="追加する文書を選ぶ"
  label="追加する文書の候補"            // listbox の名前
  items={items}                          // { key, label, textValue, description?, meta?, disabled?, disabledReason?, groupKey? }[]
  groups={groups}                        // 任意: { key, label, textValue, countLabel?, onSelectAll?, onClearAll?, … }[]
  selectedKeys={selectedKeys}            // ReadonlySet<string>
  onToggle={(item, selected) => …}
  onSelectMany={(items) => …}            // 任意:「表示中をすべて選択」
  onClearSelection={() => …}             // 任意:「選択をすべて解除」
  selectedItems={selectedItems}          // 任意:「選択中だけ表示（K）」
  search={{ label: "追加する文書を検索", value: q, onSearch: setQ }}
  total={total} hasMore={hasMore} loadingMore={loadingMore} loadMoreError={error} onLoadMore={loadMore}
  loading={isPending} refreshing={isPlaceholderData}
  disabled={saving}                      // 任意: 保存中は切り替えない（<fieldset disabled> の中も同じ）
  fixedHeight                            // 任意: 並べた 2 つの一覧の高さをそろえる
  labels={…}                             // 翻訳済みの文言（既定は日本語。DEFAULT_LIST_PICKER_LABELS）
  actions={<FormActionBar primaryActions={[{ id: "add", label: "選択した 3 件を追加", icon: FilePlus2 }]} … />}
/>
```

| 決めたこと | 理由 |
|---|---|
| 候補の一覧は listbox（`aria-multiselectable`・`aria-checked`）。Tab で listbox に入り、↑↓ / Home / End / PageUp / PageDown で移り、Space（Enter。IME の変換中は無視）で切り替える。グループの端の ↑↓ で隣のグループの listbox へフォーカスを移す | APG の multi-select listbox。候補の数だけ Tab を押させない。仮想スクロールで外れたグループにも矢印で届く |
| グループは見出し（件数・「すべて選択」「選択をすべて解除」。非同期でよく、実行中は同じピッカーの一括操作を止める）と listbox を分ける | listbox の子は選択肢だけ（ボタンを入れない） |
| 行の名前は `aria-labelledby`、補足・選べない理由は `aria-describedby` | Playwright の `getByLabel` / `getByRole("option", { name })`、読み上げで名前が短く伝わる |
| 100 行を超えたら見えている行と、キーボードの位置の行だけを描く（行の高さは描いた行から測る。`lib/list-window.ts`） | 数千件でも重くならない。スクロールしてもフォーカスが body に落ちない |
| 検索語・「選択中だけ表示」を変えたら一覧の先頭へ戻す。「選択中だけ表示」は `selectedItems` を検索語で画面側で絞る | 読み込みの範囲の外にある選択も確かめられる |
| `LoadMoreFooter` は失敗中は「さらに読み込む」を出さず、Banner の「再試行」だけにする | 同じ処理のスピナー・ボタンを 1 つにする（messaging §3.7） |

- 単体テストは `packages/ui/tests/list-picker.test.tsx`（読み上げの属性・↑↓ / Home / End / Space / Enter・IME の確定の Enter・グループ間の移動・3,000 件の仮想スクロール・検索欄の IME と件数の読み上げ・0 件・追加読み込みと再試行・一括選択と「選択中だけ表示」・無効・`list-window` の計算）。
- 実ブラウザは RAG `e2e/knowledge-bases.spec.ts`（モックで 3,000 件から検索して複数を追加、desktop / 375px、ライト / ダーク、キーボード。所属文書の検索とページング）と NL2SQL `tests/e2e/profile-allowed-objects.spec.ts`（許可する表・ビュー）。
- 製品の置き換え: RAG（ナレッジベースの「文書を追加」、所属文書・ナレッジベース・検索・回答プロファイル・文書の一覧のツールバー）、NL2SQL（業務プロファイルの許可する表・ビュー、`DbObjectSelectorFooter`）、system-settings（権限管理の「利用できる対象」。`RolePermissionTargetOption` は削除）。

## ColumnBrowser — **新規**（#2141）

段の決まった階層の一覧を列で見る（Miller columns）。規則と理由は README §4「`ColumnBrowser`」、見た目の変更は README §7 の 136。

```tsx
import { ColumnBrowser, RowActionMenu, Button } from "@production-ready/ui";

<ColumnBrowser
  ariaLabel={t("documentCategories.browser.label")}       // role="group" の名前
  columns={[
    {
      id: "level-1",
      title: "大分類",                                       // 2 段目からは「10_業務A の中分類」のように上の段の名前を入れる
      count: "2 件",
      headerAction: <Button size="sm" variant="secondary" icon={Plus}>大分類を追加</Button>,
      items: nodes.map((node) => ({ id: node.id, label: node.name, meta: "中分類 1 · 文書 2 件", hasChildren: true,
        actions: <RowActionMenu actions={…} ariaLabel={`${node.name} の操作`} /> })),
      selectedId: large?.id ?? null,
      onSelect: (id) => select({ large: id }),               // 選択は製品が URL などに持つ
      before: adding ? <NameForm … /> : undefined,           // 任意: 一覧の上の入力行（追加）
      renderItem: (item) => (renaming === item.id ? <NameForm … /> : undefined), // 任意: 行の代わり（名前の変更）
      empty: <EmptyState title="大分類がまだありません" />,
    },
    { id: "level-2", title: "中分類", items: [], placeholder: "大分類を選ぶと、その下の中分類を表示します。" }, // 上の段が未選択
    …
  ]}
  activeColumnId={opened ? `level-${opened.level}` : undefined} // 任意: 狭い置き場所で出す列（検索から開いた項目の列。#2143）
  backLabel={(index) => `${levels[index - 1]}に戻る`}          // 狭い置き場所の戻るボタン（index は表示中の列）
  onBack={(index) => clearSelectionFrom(index - 1)}
/>
```

- 列の中の行の一覧は `INFORMATION_LIST_SCROLL_CLASS`（5 / 8 行）・行は `INFORMATION_LIST_ROW_CLASS`。製品で高さを書かない。
- 狭い置き場所（コンテナ 48rem 未満）は `placeholder` の無い一番右の列だけを出す（`data-active="true"`）。`activeColumnId` を渡すとその列を出す（案内の出ている列・知らない ID は使わず、既定に戻る）。列の行を選んだ・戻ったら、製品は `activeColumnId` を外す。
- 単体テストは `packages/ui/tests/column-browser.test.tsx`（選択・案内・狭い置き場所の列・↑↓ / → / ←・戻る）。実ブラウザは RAG `e2e/document-categories.spec.ts`（desktop 3 列 / 375px 1 列ずつ、URL の選択、検索、列の中のスクロール、ダーク）。

## 一覧の絞り込み: FilterSelect / FilterChipGroup / ListToolbar の条件のクリアとシート — **新規**（#1933）

一覧のツールバーの絞り込みの部品。規則（揃え・選択とチップの使い分け・条件のクリア・狭い画面・期間・作業状態）は UX 契約 page-archetypes.md「一覧の絞り込みの部品」、見た目の変更は README §7 の 103。

```tsx
import { FilterChipGroup, FilterSelect, isFilterActive, ListToolbar, SearchField } from "@production-ready/ui";

const activeFilterCount = [isFilterActive(kind, "all"), isFilterActive(status, "all")].filter(Boolean).length;

<ListToolbar
  testId="run-history-toolbar"
  search={<SearchField id="run-q" label={t("history.search")} labelHidden value={q} onSearch={setQ} clearLabel={t("search.clear")} />}
  filters={
    <>
      {/* ボタンの中に「種類: すべて」。上のラベルは出さない。読み上げ名は「種類」 */}
      <FilterSelect id="run-kind" label={t("history.kind")} value={kind} options={kindOptions} onValueChange={setKind} />
      {/* 「すべて」を含めて 5 つまで。件数は任意 */}
      <FilterChipGroup
        label={t("history.statusFilter")}           // group の名前（画面には出さない）
        value={status}
        options={[{ value: "all", label: t("all"), count: total }, { value: "failed", label: t("failed"), count: failed }]}
        onValueChange={setStatus}
      />
    </>
  }
  activeFilterCount={activeFilterCount}            // 既定と違う絞り込みの数（検索語は数えない）
  searchActive={q !== ""}
  onClearFilters={resetView}                       // 検索語・絞り込みを既定に戻し、1 ページ目へ
  filtersLayout="wrap"                             // 絞り込みが 3 つ以上なら "sheet"（28rem 未満で「絞り込み（N）」→ シート）
  count={activeFilterCount || q ? { total, matched } : { total }}
  labels={listToolbarLabels()}                     // 製品の i18n（既定は日本語。DEFAULT_LIST_TOOLBAR_LABELS）
/>
```

### FilterSelect の props

| prop | 型 | 説明 |
|---|---|---|
| `id` / `label` / `value` / `options` / `onValueChange` | `SelectField` と同じ | `label` はボタンの中の「〈label〉: 」と読み上げ名 |
| `defaultValue` | `T` | 絞り込まない値（「すべて」）。既定は `options` の先頭の値。これ以外で `data-filter-active="true"` |
| `width` | `"xs" \| "sm" \| "md" \| "lg"` | 既定 `sm`。「ラベル: 値」の長さで選ぶ（`full` は無い） |
| `size` / `disabled` / `data-testid` / `className` | | `SelectField` と同じ |

- 数百件から選ぶ対象は `SearchableSelectField` に `labelPlacement="inline"` と `active={isFilterActive(value, "ALL")}` を渡す（「ナレッジベース: すべて」。選んだ名前は 1 行で切り、全文は `title`。`data-filter-select-label` / `data-filter-select-value` は `FilterSelect` と同じ。#1933 段 2）。

### FilterChipGroup の props

| prop | 型 | 説明 |
|---|---|---|
| `label` | `string` | `role="group"` の `aria-label`（例「状態で絞り込み」） |
| `value` / `onValueChange` | `T` / `(value: T) => void` | 1 つを選ぶ。選んでいるチップを押しても呼ばない |
| `options` | `{ value, label, count? }[]` | 5 つまで（`FILTER_CHIP_MAX_OPTIONS`）。`count` は桁区切りの等幅数字で、読み上げは説明（`aria-describedby`） |
| `size` | `ControlSize` | 既定 md（検索欄と同じ） |
| `disabled` / `data-testid` / `className` | | |

### ListToolbar の追加の props（#1933）

| prop | 型 | 説明 |
|---|---|---|
| `activeFilterCount` | `number` | 既定と違う絞り込みの数。「絞り込み（N）」と「条件をクリア」を出す条件 |
| `searchActive` | `boolean` | 検索語がある（「条件をクリア」を出す条件） |
| `onClearFilters` | `() => void` | 渡すと、条件があるあいだ絞り込みの後ろに「条件をクリア」（`data-list-toolbar-clear`） |
| `filtersLayout` | `"wrap" \| "sheet"` | 既定 `wrap`。`sheet` はツールバーが 28rem（`LIST_TOOLBAR_NARROW_REM`）未満のとき、絞り込みを `SideSheet` に入れる（`data-list-toolbar-filters-layout="sheet"`、シートの `data-testid` は `${testId}-filters-sheet`） |
| `labels` | `Partial<ListToolbarLabels>` | `clearFilters` / `filtersButton(n)` / `filtersSheetTitle` / `filtersSheetClose` / `showResults(matched)` |

- 単体テストは `packages/ui/tests/list-filters.test.tsx`（inline のラベルと読み上げ名・絞り込み中の強調・`defaultValue`・上のラベルの目印・チップの `aria-pressed` と高さと件数の説明・条件のクリアの出し分けと位置・狭い置き場所のシートと「N 件を表示」・文言の上書き）。
- ツールバーの中の欄の幅の段は container（ツールバーの幅 28rem）で切り替わる（`ListToolbar` が `FieldWidthScopeProvider scope="container"` で包む）。container query で並べ方を決める自前の区画で同じことをするときは `<FieldWidthScopeProvider scope="container">` で包み、ネイティブの欄は `fieldControlClassName({ width, scope: useFieldWidthScope() })`。
- e2e は `findToolbarFieldWidthMismatch(page)`（縦に積んだツールバーで、検索欄と幅のそろわない欄）も使う。
- e2e は `platform/packages/ui/e2e/layout-checks.ts` の `findToolbarMisalignment(page)`（ツールバーの同じ行の部品の高さと縦の中心のずれ。`expect(await findToolbarMisalignment(page)).toEqual([])`）。移行前の画面（上のラベルの欄・`ToggleChip`）では、ずれた行が返る。
- 製品の置き換え（#1933 の段 2〜4）: `SelectField`（上のラベル）→ `FilterSelect`、`role="group"` の `ToggleChip` の並び（Agent の局所の `FilterChipGroup` を含む）→ 共通の `FilterChipGroup`、空の状態だけにあった条件のクリア → `onClearFilters`、`summary` の件数 → `count`。3 製品に適用済みで、`adherence.oxlintrc.json` の lint が `ListToolbar` の `filters` の中の `SelectField`・`ToggleChip`・`labelPlacement="inline"` の無い `SearchableSelectField` を検出する。

## 操作部品の高さと幅: `size` / `width` / `FieldActionRow` / `fieldControlClassName` — **新規**（#613）

入力欄・選択欄・ボタンの高さと、入力欄・選択欄の幅を部品の prop で決めます（規則は README §4「操作部品の高さと幅」）。製品は `h-*` / `min-h-*` / `w-*` を書かず、`touchTarget` を使いません（adherence の lint が検出する）。

```tsx
import { FieldActionRow, SelectField, TextField, TextareaField, fieldControlClassName } from "@production-ready/ui";

// 高さ: 同じ行の部品に同じ size（既定 md）。幅: 値の長さで選ぶ（grid のセルに置く欄は指定しない）
<SelectField id="retention" label="保存期間" value={v} options={o} onValueChange={set} size="lg" width="md" />
<Button size="lg" icon={Save}>保存期間を保存</Button>

// 入力欄と、その値への操作（送信・実行・取得）の行。操作は入力欄の下端にそろい、375px では下に全幅
<FieldActionRow actions={<RunStopButton size="lg" … />}>
  <TextareaField id="chat-composer" label="メッセージ" labelHidden rows={3.5} … />
</FieldActionRow>

// ネイティブの select / input を例外として残す画面（<optgroup> など。#631 で理由付きの lint の除外が要る）は、同じ見た目・高さ・幅のクラスを使う
<select className={fieldControlClassName({ size: "lg", width: "sm" })}>…</select>
```

```ts
export type ControlSize = "sm" | "md" | "lg";                 // 32 / 36 / 40px。タッチ端末は 44px
export type FieldWidth = "xs" | "sm" | "md" | "lg" | "full";  // 8 / 12 / 20 / 28rem / 100%。sm 未満は全幅

// TextField / SearchField / SecretField / SelectField / SearchableSelectField に追加
size?: ControlSize;   // 既定 md（SearchableSelectField は最小の高さ。長い名前は折り返して高くなる）
width?: FieldWidth;   // 既定なし（親の幅いっぱい）。入力欄・選択欄のボタンに付き、外枠はラベルの 1 行の幅まで広がる（#1746）

export function FieldActionRow(props: {
  children: ReactNode;   // 入力欄 1 つ（行の残りを埋める）
  actions: ReactNode;    // 操作（入力欄と同じ size の Button）。null / false なら操作の列を描かない（#631）
  footer?: ReactNode;    // 補足・エラー（欄の helper / error の代わり）
  fit?: boolean;         // 欄に xs〜lg の width を渡したとき true。欄はその幅のまま、操作を欄の直後に並べる（#1891）
  className?: string;
  "data-testid"?: string;
}): JSX.Element;

export function fieldControlClassName(options?: { size?: ControlSize; width?: FieldWidth; className?: string }): string;
export const CONTROL_HEIGHT_CLASS: Record<ControlSize, string>;      // h-[var(--control-height-*)]
export const CONTROL_MIN_HEIGHT_CLASS: Record<ControlSize, string>;  // min-h-[var(--control-height-*)]
export const FIELD_WIDTH_CLASS: Record<FieldWidth, string>;          // w-full sm:w-[var(--field-width-*)] sm:max-w-full（ラベルの無い欄・fieldControlClassName）
export function fieldWidthClass(width?: FieldWidth): string | undefined;
// ラベルを持つ欄（#1746）: 外枠 / 入力欄・選択欄のボタン / 補足・エラーの文
export const FIELD_BOX_WIDTH_CLASS: Record<FieldWidth, string>;                // w-full sm:w-fit sm:max-w-full（full は w-full）
export const FIELD_CONTROL_WIDTH_CLASS: Record<FieldWidth, string | undefined>; // sm:w-[var(--field-width-*)] sm:max-w-full（full は無し）
export const FIELD_TEXT_CONTAIN_CLASS: string;                                 // sm:[contain:inline-size] sm:max-w-[var(--form-max-width)]（外枠を広げない・1 行は 60rem まで。#2063）
export const FIELD_BOX_TEXT_WIDTH_CLASS: string;                               // w-full sm:w-auto sm:max-w-full（補足・エラーのある外枠は置いた枠の幅。#2063）
export const FIELD_BOX_TEXT_WIDTH_CONTAINER_CLASS: string;                     // w-full @md:w-auto @md:max-w-full（ツールバーの中の欄。scope="container"）
export const FIELD_BOX_TEXT_MIN_WIDTH_CLASS: string;                           // sm:min-w-[min(var(--field-width-md),100%)]（補足・エラーのある外枠の最小。横に並べる行で効く。#1891 段 5）
export const FIELD_BOX_TEXT_MIN_WIDTH_CONTAINER_CLASS: string;                 // @md:min-w-[min(var(--field-width-md),100%)]（ツールバーの中の欄。scope="container"）
export const FIELD_TEXT_MIN_WIDTH_REM: number;                                 // 20（補足・エラーを折り返す最小の幅）
export function labeledFieldWidthClasses(width?: FieldWidth, scope?: FieldWidthScope, hasText?: boolean): { box?: string; control?: string; text?: string };
```

| 決めたこと | 理由 |
|---|---|
| 高さのトークンを `--control-height-sm` / `md` / `lg`（32 / 36 / 40px）に一本化し、`--button-height-*` と `--field-height` はその別名にした | ボタンと入力欄が同じ値を参照するので、同じ `size` なら必ず同じ高さになる |
| タッチ端末（`pointer: coarse`）では 3 段とも 44px。**入力欄・選択欄も 44px**（以前は Button だけ） | タッチ端末で入力欄 36px・ボタン 44px とずれていた。Apple HIG 44pt / WCAG 2.5.5。画面幅ではなく入力方式で決める（マウスで狭いウィンドウを使うときは密度を保つ） |
| `touchTarget`（マウス環境でも 44px）は製品で使わない。`TextField` の `touchTarget` は削除 | 「入力欄の横の操作は 44px」の規則で、同じ画面の入力欄 36px と 44px が混ざっていた |
| 幅（段）は入力欄・選択欄のボタンに付け、外枠は sm 以上で `w-fit`（段の幅とラベルの 1 行の幅の広いほう）、sm 未満は `w-full`。外枠も入力欄も `max-w-full`。補足・エラーは `contain: inline-size` で外枠を広げない（#1746。以前は外枠に段の幅を付けていた） | 外枠に段の幅を付けると、`xs`（8rem）の「取得件数上限」のようにラベルと必須の印が段の幅で 2 行に折り返した。入力欄の幅は値の長さのまま、ラベルは 1 行で読める。補足・エラーの文は外枠の幅で折り返す。狭い親の中で親より広くならない。375px では全幅（片手で押しやすい） |
| 補足・エラー（`TextareaField` の文字数・`SecretField` の取り出しの失敗を含む）のある段の欄は、外枠を sm 以上で `width: auto` にし、置いた枠（grid のセル・`FormGrid` の段・カード）の幅まで広げる（`labeledFieldWidthClasses(width, scope, true)`。#2063）。横に並べる行（flex）の中では中身の幅で、最小の幅は `min(20rem, 親の幅)`（#1891 段 5）。文の 1 行は 60rem まで。入力欄の幅は段のまま、文は `aria-describedby` で結んだまま | `xs`（8rem）・`sm`（12rem）の欄の補足が欄の幅で 3〜6 行に折り返した（#1891 段 5）。20rem に広げた後も、1 文の補足（約 23 文字超）が右に余白のある列で 2 行になった（#2063）。外枠は置いた枠（セル）の幅なので、隣の欄には重ならず、狭いセル・375px で横にはみ出さない。flex の行では `auto` が中身の幅になり並びを変えない。文の無い欄は外枠を広げない。代わりの案（画面ごとに補足の幅を指定する prop・欄の段を上げる・`InfoTip` に移す）は、3 製品の同じ型の欄に効かない・値の手がかりが崩れる・入力の条件を隠すので採らない |
| `FieldActionRow` は `items-end`。欄の helper / error は `footer` に出す | 上に見えるラベルがある欄でも、複数行の入力欄でも、ボタンの下端が入力欄の下端に合う。欄の下に文があると下端でそろえた操作が文の下端に合ってしまう |
| `ClearActionButton` の `matchButtonHeight` を削除。既定は `sm`（空の状態の「検索語をクリア」）で、行に置くときは行と同じ `size` を渡す | 以前はマウス環境でも 44px が既定で、`matchButtonHeight` を付け忘れた行がずれた |
| `fieldControlClassName` は TextField と同じ見た目（枠線 `--color-border-control`・`--radius-control`・地・フォーカス・disabled・read-only）に、高さ・幅・`aria-invalid` の枠線を足す | ネイティブの `<select>` / `<input>` を残す画面も、手書きの `h-10` / `h-11` / `min-h-[44px]`（35 / 38.5 / 44px）をやめて同じ段にする |

- 単体テストは `packages/ui/tests/control-size.test.tsx`（段のクラス・各部品の `size` / `width`・`fieldControlClassName`・`FieldActionRow`）と `tokens-css.test.ts`（トークンと `pointer: coarse` の 44px）。
- 実ブラウザの高さは、e2e の helper `expectedControlHeight(page, size)`（RAG `e2e/_helpers.ts`、NL2SQL `tests/e2e/_helpers/control-height.ts`）で入力方式から期待値を出して確かめる（タッチ端末 44px、それ以外は 32 / 36 / 40px）。

### 幅の基準・`FormGrid`・placeholder の切れ（#1891）

```tsx
import { FormGrid, ListCount, ListToolbar, SearchField, TextareaField, TextField } from "@production-ready/ui";

// フォームの本文: 最大 60rem（--form-max-width）で左寄せ、置き場所が 57.5rem（lg の欄 2 つ + 列の間）以上で 2 列。欄は値の種類の width
<FormGrid>
  <TextField id="name" label="名前" width="md" required />
  <SelectField id="region" label="リージョン" width="sm" … />
  <TextField id="url" label="URL" className="col-span-full" />          // 長い値は full のまま 2 列にまたがる
  <TextareaField id="prompt" label="プロンプト" className="col-span-full" />
</FormGrid>

// 一覧のツールバー: 検索欄は lg（28rem）で固定し、件数は count（ListCount）
<ListToolbar
  search={<SearchField id="q" label="名前で検索" labelHidden value={q} onSearch={setQ} clearLabel="検索語をクリア" />}
  filters={<SelectField id="state" label="状態" labelHidden width="sm" … />}
  count={{ total, matched: q ? matched : undefined, labels: listCountLabels() }}   // 「全 123 件」「12 件（全 123 件）」
  actions={…}
/>
```

```ts
// 追加・変更
export const FIELD_WIDTH_REM: { xs: 8; sm: 12; md: 20; lg: 28 };       // e2e の「幅が段のどれか」の検査
export function FormGrid(props: ComponentProps<"div"> & { columns?: 1 | 2 }): JSX.Element;  // 既定 2（置き場所 57.5rem 以上で 2 列）
export const FORM_GRID_TWO_COLUMN_MIN_REM: number;                       // 57.5 = lg 28rem × 2 + 列の間 1.5rem（#1891 段 5）
TextareaField: width?: FieldWidth;            // 既定は親の幅いっぱい（full）
SearchableMultiSelect: width?: FieldWidth;    // 部品全体（検索欄・候補・chip）の幅
ListToolbarProps.count?: ListCountProps;      // 右側の件数（summary は件数以外の状態の文言だけに使う）
export function ListCount(props: { total: number; matched?: number; labels?: Partial<ListCountLabels>; live?: boolean; className?: string; testId?: string }): JSX.Element;
export function listCountText(props: { total: number; matched?: number; labels?: Partial<ListCountLabels> }): string;
// placeholder の切れ
export function placeholderOverflows(input: PlaceholderFitInput): boolean;            // 判定（純粋な関数）
export function measurePlaceholderClip(element: HTMLInputElement | HTMLTextAreaElement): boolean;
export function usePlaceholderFit(ref, placeholder, title): { ref; clipped: boolean; title?: string };
```

| 決めたこと | 理由 |
|---|---|
| 幅は値の**種類**で選ぶ 5 段（`xs` 数値・短いコード・表示件数 / `sm` 日付・期間・短い選択・有効期限・リージョン / `md` 名前・ID・メール・一般の選択 / `lg` 長い名前・対象の選択・一覧の検索欄 / `full` URL・OCID・パス・文章・SQL・プロンプト・`TextareaField`） | 欄の長さが入る値の手がかりになる（Baymard・GOV.UK の文字数の幅）。以前は「grid のセルには渡さない」規則で、名前の欄が 2 列の grid のセル（1920px で約 770px）まで伸びた |
| grid のセルでも種類の `width` を渡す（段の幅が上限。セルが狭ければセルの幅） | 部品の段の幅は `max-w-full` 付きなので、セルに置いても崩れない |
| `FormGrid` だけがフォームの本文を 60rem（840px）で止める。カード・`Section` は 100% のまま | 1920px で 1 つの欄が 1,500px を超えない。2 列のセル（約 29rem）に `lg` の欄が入る |
| `FormGrid` の 2 列は置き場所 57.5rem（`lg` × 2 + 列の間）以上だけ。それより狭いと 1 列（#1891 段 5。推奨で確定） | 以前の 42rem の境では、42〜57.5rem の間のセル（約 20〜28rem）に `lg` の欄が入らずセルの幅で切られた。代わりの案: (a) 42rem のまま欄をセルで止める（`lg` が `md` と区別できず、長い名前の選択が切れる。e2e は止められた欄を例外にする必要がある）、(b) その間だけ `lg` の欄を 2 列にまたがせる（意味のペアが割れ、行の並びが欄の段で変わる）、(c) 中身（`:has(lg の欄)`）で境を変える（同じカードの 2 つの `FormGrid` で列の数がずれる）。1 列は Baymard の単一列のフォームの推奨で、1280px・1920px（置き場所 60rem）と 375px は変わらない |
| 一覧の検索欄は `ListToolbar` が `lg`（28rem）で固定し、行を埋めない。置き場所が 28rem 未満なら全幅 | 複数の語が見え（約 27 文字以上）、隣の絞り込み・件数とつり合う（Visma / Spiris のツールバーの検索欄 200〜510px） |
| 件数は `ListCount` を `ListToolbar` の右に 1 か所（「全 N 件」、絞り込み中は「N 件（全 M 件）」）。1 ページでも出す。見出しのバッジと二重にしない | 製品ごとに「N 件」「a / b 件」「件数: N」「条件一致 N 件」とばらばらで、RAG は 1 ページだと件数が無かった |
| `TextField`・`SearchField`・`TextareaField` は placeholder が欄に収まらないとき `data-placeholder-clipped` と `title`（全文）を付ける。`SelectField` の未選択の表示は `data-field-placeholder` | 切れた placeholder を e2e と開発者が見つけられる。直すのは文言（短い例 + helper）で、`title` は直すまでの補い |

- e2e の共通の検査は `platform/packages/ui/e2e/layout-checks.ts`（`measureHorizontalOverflow`・`findClippedPlaceholders`・`findFieldsOffWidthScale`・`findNarrowFieldTexts`・`jumpToPage`・`setFormGridMaxWidth`）。`findFieldsOffWidthScale` は `FormGrid` のセルからはみ出した欄（`reason: "exceeds-cell"`）も返し、製品の画面固有の grid の `lg` より狭い列で止められた欄は返さない（`FormGrid` の列で止められた欄は返す）。`findNarrowFieldTexts` は補足・エラーの文が置き場所の幅（block・grid のセルの欄はセルの幅、60rem まで。横に並べる行の欄は `min(20rem, 外枠の親の幅)`）より狭い欄を返す（#2063）。`measureShellHiddenOverflow` は `AppShell` の外枠の隠れたあふれを、`findEscapedAbsoluteElements` は本文のスクロール領域から抜け出した absolute の要素（`sr-only` など）を返す（#2064）。RAG `form-field-layout.spec.ts` と Agent `list-field-standards.spec.ts` は、1280px で `setFormGridMaxWidth` により置き場所を 42 / 50 / 57 / 58rem に狭めても同じ検査を通す（#1891 段 5）。`@playwright/test` の型を import しないので、製品の spec から相対パスで読む。
- 単体テストは `packages/ui/tests/pagination-jump.test.tsx`（ページ番号の入力・最初へ / 最後へ・表示件数・`ListCount`）と `field-width-placeholder.test.tsx`（`placeholderOverflows`・`TextareaField` / `SearchableMultiSelect` の `width`・`FormGrid`）。

---

## SideSheet — **新規**（#664）

画面の中の補助的な一覧・詳細（RAG のチャットの会話の履歴など）を、狭い画面で本文の上に重ねて出すモーダルの side sheet です。振る舞いの表は README §4「`SideSheet`」。広い画面では製品が同じ中身を本文の横にインラインで置き、狭い画面だけ `SideSheet` で開きます（どちらか一方だけを描く）。

```tsx
import { Button, SideSheet } from "@production-ready/ui";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";

const inline = useMediaQuery("(min-width: 1024px)"); // 製品が決める
const [sheetOpen, setSheetOpen] = useState(false);
const toggleRef = useRef<HTMLButtonElement>(null);
const historyId = useId();

<Button ref={toggleRef} variant="ghost" size="sm" iconOnly
  icon={open ? PanelLeftClose : PanelLeftOpen} aria-label={t("chat.sessions.title")}
  aria-expanded={open} aria-controls={historyId} onClick={toggle} />

{inline ? (
  <aside id={historyId} className={open ? "flex" : "hidden"}>{history}</aside>
) : (
  <SideSheet open={sheetOpen} onClose={() => setSheetOpen(false)}
    title={t("chat.sessions.title")} closeLabel={t("chat.sessions.close")}
    id={historyId} returnFocusRef={toggleRef} data-testid="chat-history">
    {history}   {/* 項目を選んだら製品が setSheetOpen(false) */}
  </SideSheet>
)}
```

### SideSheet の props

```ts
export interface SideSheetProps {
  open: boolean;
  /** 閉じる要求（閉じるボタン・Escape・scrim のタップ）。製品が open を false にする。 */
  onClose: () => void;
  /** 見出しとダイアログの名前（翻訳済み）。 */
  title: string;
  /** 閉じるボタンの名前（翻訳済み。Tooltip にも出る）。 */
  closeLabel: string;
  /** 出す側（既定は左）。 */
  side?: "left" | "right";
  /**
   * 幅（#1154 / #2281）。default は 22rem（短い一覧・絞り込み）、md は 40rem（一覧の 1 件の詳細。文章を読む内容）、
   * wide は sm 以上 64rem（表）・sm 未満は全画面。いずれも画面幅 − 3.5rem まで。
   */
  size?: "default" | "md" | "wide";
  /** シートの要素の id（開くボタンの aria-controls に渡す）。 */
  id?: string;
  /** 閉じたときにフォーカスを戻す先。省略時は開く前にフォーカスがあった要素。 */
  returnFocusRef?: React.RefObject<HTMLElement | null>;
  /** 見出しの行の右（閉じるボタンの左）に置く操作。 */
  headerActions?: React.ReactNode;
  /** 見出しの行の「前へ / 次へ」（ListDetailPager の props。#2281）。headerActions の左に置く。 */
  pager?: ListDetailPagerProps;
  /**
   * 開いたときのフォーカスの先（#2360）。first（既定）は先頭の操作（閉じるボタン）、dialog はシートそのもの
   * （読むための内容のシート。キーボードで開いた直後の Escape 1 回で閉じる。最初の Tab で先頭の操作へ）。
   */
  initialFocus?: "first" | "dialog";
  children: React.ReactNode;
  className?: string;
  /** 本文（flex の縦並び・中でスクロール・p-3）への追加のクラス。 */
  bodyClassName?: string;
  /** シートの data-testid。scrim は `<testId>-scrim`。 */
  "data-testid"?: string;
}
```

- 閉じている間も描いたまま（`inert`・`data-state="closed"`）。開いている間は `data-state="open"`。
- キー操作はナビのドロワーと同じ helper（`focusableIn` / `navDrawerKeyAction`）。閉じるボタンの Tooltip が出ている間の 1 回目の Escape は吹き出しだけを閉じる（README §4「`Tooltip`」）。
- 単体テストは `packages/ui/tests/side-sheet.test.tsx`、実ブラウザは RAG の `e2e/chat.spec.ts`（375px の開閉・Esc・外側・フォーカスの戻り）。

---

## ListDetailLayout / useUrlSelection / ListDetailPager — **新規**（#2281）

B 型（マスタ詳細の閲覧）の一覧 + 詳細。規則は UX 契約 [page-archetypes.md §1 B](../ux-contracts/page-archetypes.md)、見た目と振る舞いの表は README §4「`ListDetailLayout`」。

```tsx
import { useSearchParams } from "react-router-dom";
import {
  EmptyState, ListDetailLayout, ListDetailPager, PageHeader, adjacentListItems,
  useListDetailSplit, useUrlSelection,
} from "@production-ready/ui";

const [searchParams, setSearchParams] = useSearchParams();
const split = useListDetailSplit(); // xl（1280px）以上か
const selection = useUrlSelection({ param: "table", searchParams, setSearchParams, singlePane: !split });
const { previous, next } = adjacentListItems(items, selection.selectedId, (item) => item.id);

<PageHeader
  title={t("tables.title")}
  // 1 ペインで詳細を出している間だけ「一覧へ戻る」
  back={!split && selection.selectedId ? { label: t("common.backToList"), onClick: selection.clear } : undefined}
/>
<ListDetailLayout
  id="table-management"                 // 幅・開閉の保存 key と data-testid（list-detail-<id>…）
  storagePrefix="production-ready-nl2sql.listDetail"
  listLabel={t("tables.list")}          // 一覧の列の名前・開閉ボタンの名前
  detailLabel={t("tables.detail")}
  listHeader={<ListToolbar … />}        // スクロールしない
  list={<DataTable className="min-h-0 rounded-none border-0" stickyHeader … />}  // 列の中でスクロール
  listFooter={<LoadMoreFooter … />}
  hasSelection={selection.selectedId !== null}
  detail={<TableDetail id={selection.selectedId} />}
  emptyDetail={<EmptyState title={t("tables.emptyTitle")} hint={t("tables.emptyHint")} />}
  detailToolbar={
    <ListDetailPager
      onPrevious={previous ? () => selection.select(previous.id) : null}
      onNext={next ? () => selection.select(next.id) : null}
    />
  }
/>
```

### props

| prop | 型 | 説明 |
|---|---|---|
| `id` | `string` | `<feature>-<view>`。保存 key（`storagePrefix` + `.` + `id`。値は `{ listWidthRem, collapsed }`）と `data-testid`（`list-detail-<id>`・`-list`・`-detail`・`-resizer`・`-toggle`） |
| `listLabel` / `detailLabel` | `string` | 列の読み上げ名（翻訳済み）。`listLabel` は開閉ボタンの名前・Tooltip にも使う |
| `listHeader` / `list` / `listFooter` | `ReactNode` | 一覧の列の上（固定）・本体（列の中でスクロール。`data-list-detail-body`）・下（固定。中身が何も描かない〔1 ページの `Pagination` など〕ときは帯を出さない。#2283） |
| `hasSelection` | `boolean` | 選んでいるか。1 ペインでは一覧と詳細のどちらを出すかを決める |
| `detail` / `emptyDetail` | `ReactNode` | 選んだ対象の詳細 / 2 ペインで未選択のときの空の状態 |
| `detailToolbar` | `ReactNode` | 詳細の上端の行の右（`ListDetailPager`）。2 ペインでは行の左に開閉ボタンが出る |
| `itemSelector` | `string` | ↑ / ↓ でフォーカスを移す項目（既定 `[data-row-title-button]` = `RowTitleButton`） |
| `storagePrefix` | `string` | 既定 `production-ready.listDetail`。製品ごとに分ける |
| `labels` | `Partial<ListDetailLayoutLabels>` | 分割線の名前・案内・値の読み上げ（既定は日本語） |

- 状態は `data-list-detail-layout="split" \| "single"`・`data-list-detail-view="both" \| "list" \| "detail"`・`data-list-collapsed`。列の上・下の帯は `data-list-detail-header` / `data-list-detail-footer`。
- **一覧の列の高さ（2 ペイン。#2330）**: 列の高さ = 本文のスクロール領域（`AppShell` の main）の見える高さ − 列の上端（本文の中の位置。スクロールに依らない）− 置き場所の下の余白（祖先の `padding-bottom`・`border-bottom`。`PageBody` の 1.5rem）。最小 `LIST_DETAIL_MIN_LIST_HEIGHT_REM`（20rem）。貼り付く位置は上端に貼り付く `PageHeader`（`[data-page-header]`）の下 + `LIST_DETAIL_STICKY_GAP_REM`（1rem）。最初の描画の前（layout effect）と、祖先・見出し・本文の大きさが変わったとき（`ResizeObserver`・window の resize）に測り、`--list-detail-list-height` / `--list-detail-list-top`（rem）で渡す。本文のスクロールでは測り直さない（詳細が長くても列は伸びない）。計算は純粋関数 `fitListColumn`（`lib/list-detail.ts`）。詳細が短ければ本文に縦のスクロールは出ず、列の下の帯は本文をスクロールせずに見える。**置き場所の末尾に置く**（後ろに兄弟の要素を置くと、その分だけ本文がスクロールする）。1 ペインは `top: 1rem`・高さは画面 − 2rem（md 未満は − 5.5rem）のまま。
- `useUrlSelection({ param, searchParams, setSearchParams, singlePane })` は `{ selectedId, select, clear, replace }`。`select` は 1 ペインで未選択から選ぶときだけ push、それ以外は replace。`clear` はこの画面で push して入ったなら `history.back()`、URL から直接開いたなら検索パラメータを消して replace。`replace(null)` は削除の後に選択を外す。`packages/ui` は router に依存しないので、`searchParams` / `setSearchParams` は製品が `useSearchParams()` から渡す。
- `ListDetailPager`（`onPrevious` / `onNext` は無ければ `null`、`testId` で `-previous` / `-next`）と `adjacentListItems(items, selectedKey, getKey)` は、詳細の上端の行と `SideSheet` の `pager` で共有する。
- 共通のユーザー・ロール・権限管理（`@production-ready/system-settings`）は `SecurityListDetail`（`users-roles/shared.tsx`）がこの部品に一覧の列（見出し・`ListToolbar`・`DataTable`・`Pagination`）と詳細を渡し、選択は `useSecurityUrlSelection`（`?user=` / `?role=`。「一覧へ戻る」の履歴の戻りは router の `navigate(-1)`）。製品は `listDetailStoragePrefix` を渡す（#2282）。
- 単体テストは `packages/ui/tests/list-detail-layout.test.tsx`・`url-selection.test.tsx`、実ブラウザは NL2SQL の `tests/e2e/table-management-list-detail.spec.ts`（2 ペイン・幅・開閉・↑ / ↓・1 ペインの戻るとフォーカス・1920px）と、一覧の列の高さ（1280px / 1920px で本文がスクロールしない・詳細が長いと列の高さが変わらず見出しの下に貼り付く。`data-management-list-detail.spec.ts` も。判定は `tests/e2e/_helpers/list-detail-fit.ts`。#2330）。

---

## RunStopButton — **新規**（#805。#413 で RAG に作った部品を移した）

その場で結果を待つ操作（検索・チャットの送信・検索テスト）の「実行」と「停止」を 1 つのボタンで出す部品。規則は UX 契約 buttons.md §3.1「その場の実行と停止」。

```tsx
import { FieldActionRow, RunStopButton, TextareaField, isSubmitEnter } from "@production-ready/ui";
import { SendHorizontal } from "lucide-react";

<FieldActionRow
  actions={
    <RunStopButton
      running={sending}                 // true の間は同じボタンが「停止」
      onRun={send}
      onStop={stop}                     // RAG: AbortController で止める / Agent: Run の中止の API
      runLabel={t("chat.send")}         // 翻訳済み
      stopLabel={t("chat.stop")}
      runIcon={SendHorizontal}
      runDisabled={!draft.trim()}       // aria-disabled（フォーカスを受ける）。停止には効かない
      size="lg"                         // 並べる入力欄と同じ size（既定 lg）
      testId="chat-send"
    />
  }
>
  <TextareaField
    id="chat-composer" label={t("chat.composer.label")} labelHidden rows={3.5} value={draft}
    onKeyDown={(event) => {
      // 入力欄の Enter は送信だけ（実行中の Enter で停止しない）。IME の確定の Enter では送らない。
      if (isSubmitEnter(event) && !event.shiftKey) { event.preventDefault(); send(); }
    }}
    …
  />
</FieldActionRow>
```

### RunStopButton の props

```ts
export interface RunStopButtonProps {
  running: boolean;
  onRun: () => void;
  onStop: () => void;
  runLabel: string;            // 翻訳済み
  stopLabel: string;           // 翻訳済み
  runIcon: LucideIcon;         // 停止のアイコンは Square に固定
  runDisabled?: boolean;       // 実行できない間。aria-disabled（ネイティブの disabled にしない）
  size?: "sm" | "md" | "lg";   // 既定 lg
  className?: string;
  testId?: string;             // data-testid。data-state は "idle" / "running"
}

// DOM に依存しない判定（単体テスト用にも export）
export function runStopClickAction(options: { running: boolean; runDisabled?: boolean; clickCount: number }): "run" | "stop" | "ignore";
export function isRepeatedActivationKey(event: { key: string; repeat?: boolean }): boolean;
```

| 決めたこと | 理由 |
|---|---|
| 同じ `<button>` のまま、実行中は `secondary` の「停止」（`Square`）。`loading` は使わない | 要素が替わらないのでフォーカスが残る。処理中のスピナーと経過時間は結果の領域の `ProcessingIndicator` が出す（動くスピナーは 1 つ。messaging §3.7） |
| 2 つのラベルを同じセルに重ね、見えない方は `invisible` | 「検索テスト」→「停止」で幅が縮まず、位置もずれない。見えない方は読み上げの名前に入らない |
| ダブルクリックの 2 回目（`detail >= 2`）と押し続けた Enter / Space の繰り返しは無視する | 実行で「停止」に変わった直後に、続けて停止しない |
| 停止は赤塗り・赤文字にしない | 取り消せる停止で、途中までの内容は残る（buttons.md §3） |

- 単体テストは `packages/ui/tests/run-stop-button.test.tsx`（判定・要素が替わらない・`aria-disabled`・ダブルクリック・Enter の繰り返し）。
- 実ブラウザは RAG `e2e/chat.spec.ts` / `e2e/search-review.spec.ts` / `e2e/knowledge-base-search-test.spec.ts` / `e2e/answer-progress.spec.ts`、Agent `e2e/chat.spec.ts`（実行中の停止 → `POST /api/runs/{id}/cancel` → 送信に戻る。desktop / 375px、ライト / ダーク）。
- 製品の置き換え: RAG の `components/RunStopButton.tsx` と `lib/run-stop.ts` は削除した。Agent のチャットの送信（`loading` / `disabled` の `Button`）を置き換えた。

---

## FeedbackControls — **新規**（#805）

回答・引用への評価（役に立った / 役に立たなかった）の部品。RAG の回答・引用の評価と、Agent のチャットの回答の評価・管理者の評価（#774）が同じ部品を使う。保存の API・payload・権限は製品が `onSubmit` で持ち、部品は業務の文言を持たない（`labels`）。

```tsx
import { FeedbackControls, toast } from "@production-ready/ui";

<FeedbackControls<FeedbackReason>
  value={current ? { rating: current.rating, reason: current.reason, comment: current.comment } : null}
  reasons={REASONS.map((value) => ({ value, label: t(`feedback.reason.${value}`) }))}
  labels={{
    question: t("…question"), helpful: t("…helpful"), notHelpful: t("…notHelpful"),
    savedInline: t("…savedInline"), reasonLegend: t("…reasonLegend"),
    commentLabel: t("…commentLabel"), commentPlaceholder: t("…commentPlaceholder"),
    commentCount: (count, max) => t("…commentCount", { count, max }),
    save: t("…save"), cancel: t("common.cancel"), retry: t("common.retry"), saveError: t("…saveError"),
  }}
  getErrorMessage={(error) => (error instanceof ApiError ? error.message : null)}
  onSubmit={async (submission) => {   // { rating, reason, comment, correctedAnswer }（空は null）
    await api.saveFeedback(toPayload(submission));
    toast.success(t("…saved"));        // 成功の Toast は製品が出す
  }}
  correctedAnswer                      // 任意:「修正した回答」の欄（RAG の回答の評価）
  compact                              // 任意: 引用のカードの中の小さな形
  disabled={loadingCurrent}            // 任意
  readOnly                             // 任意: 保存済みの評価を見せるだけ
  commentId="feedback-comment-answer"  // 任意: 欄の id（e2e で使うとき）
  data-testid="chat-feedback-run-1"
/>
```

### FeedbackControls の props

```ts
export type FeedbackRating = "helpful" | "not_helpful";
export interface FeedbackControlsValue<R extends string = string> {
  rating: FeedbackRating; reason?: R | null; comment?: string | null; correctedAnswer?: string | null;
}
export interface FeedbackControlsSubmission<R extends string = string> {
  rating: FeedbackRating; reason: R | null; comment: string | null; correctedAnswer: string | null;
}
export interface FeedbackControlsProps<R extends string = string> {
  value: FeedbackControlsValue<R> | null | undefined;
  reasons: readonly { value: R; label: string }[];
  onSubmit: (submission: FeedbackControlsSubmission<R>) => Promise<unknown>; // reject で失敗の表示
  labels: FeedbackControlsLabels;               // すべて翻訳済み（既定の日本語は持たない）
  getErrorMessage?: (error: unknown) => string | null | undefined; // 無ければ labels.saveError
  commentMaxLength?: number;                    // 既定 1000
  correctedAnswer?: boolean;                    // 「修正した回答」の欄
  correctedAnswerMaxLength?: number;            // 既定 20000
  compact?: boolean;
  disabled?: boolean;
  readOnly?: boolean;
  commentId?: string;
  correctedAnswerId?: string;
  className?: string;
  "data-testid"?: string;
}
export function isSameFeedback(value, submission): boolean; // 空白を除いて比べ、空文字と null を同じとみなす
```

| 決めたこと | 理由 |
|---|---|
| 「役に立った」はすぐ保存、「役に立たなかった」は理由（必須。`FieldLegend required`）・コメント（任意）・修正した回答（任意）をその場で開く | 良い評価は 1 回の操作で済ませ、悪い評価は改善に使える理由を必ず残す |
| ボタンは `ghost` / 評価済みは `secondary` + `aria-pressed`、`iconOnly`（Tooltip は `aria-label` と同じ文言）。評価済みの 👍 は `text-success-fg`、👎 は `text-danger-fg` | 色だけに頼らず押された状態を `aria-pressed` と枠で示す |
| 保存済みと同じ内容は送り直さず閉じる | 二重の保存・Toast を出さない |
| 保存中は押したボタンだけ `loading`（ほかは `disabled`） | 動くスピナーは操作 1 つに 1 つ（messaging §3.7） |
| 失敗は部品の下に `role="alert"` の文言と「再試行」（同じ内容を送り直す）。次の保存まで残す | 結果は起点の操作の直下に出す（messaging §10）。成功は Toast（製品） |
| 問い・ボタン・保存済みの表示は 1 行。`compact` は問いを読み上げだけにし、右寄せ・保存済みの表示なし・上の区切り線なし | 引用のカードの中に収める |

- 単体テストは `packages/ui/tests/feedback-controls.test.tsx`（すぐ保存・理由の必須・コメントの整形・修正した回答・同じ評価・失敗と再試行・保存中・`compact`・`readOnly`）。
- 実ブラウザは RAG `e2e/chat.spec.ts` / `e2e/search-review.spec.ts` / `e2e/feedback.spec.ts` / `e2e/citation-variant-badge.spec.ts`、Agent `e2e/feedback.spec.ts`（チャットの回答の評価・管理者の評価。既存の spec のまま）。
- 製品の置き換え: RAG の `components/feedback/FeedbackControls.tsx` と Agent の `components/chat/AnswerFeedback.tsx` は、API と文言をつなぐ薄いラッパーになった（見た目・振る舞いは変えない）。

## useActionPending — **新規**（#819）

`Button` の `loading` は押したボタンだけが持つ（UX 契約 buttons.md §8）。押したボタンが始めた処理の間だけ `true` になる状態を作る hook。

```ts
import { useActionPending } from "@production-ready/ui";

export interface ActionPending {
  pending: boolean;                                        // track に渡した処理のどれかが終わっていない間だけ true
  track: <T>(work: () => Promise<T>) => Promise<T>;        // 処理を実行し、結果・例外はそのまま返す
}
export function useActionPending(): ActionPending;

// 「最新情報を取得」: 押した取り直しの間だけ回す
const manualRefresh = useActionPending();
<PageHeader actions={[{ id: "refresh", kind: "utility", label, icon: RefreshCw,
  loading: manualRefresh.pending, onClick: () => void manualRefresh.track(() => query.refetch()) }]} />
```

| 決めたこと | 理由 |
|---|---|
| 再取得のボタンに query の `isFetching` をそのまま渡さず、押した取り直しを `track` で包む | `isFetching` は定期の取り直し（`refetchInterval`）・他の操作の後の invalidate・絞り込みやページの切り替え（`keepPreviousData`）・フォーカスでも true になり、押していないボタンが回るため |
| 重なった `track` はすべて終わるまで `pending`。unmount 後は state を更新しない | 連続で押したとき・画面を離れたときに状態がずれない |
| 1 つの mutation を複数のボタンが使うときは、この hook ではなく `activeOperation` の state・`mutation.variables`（`isPending` のときだけ読む）・行の id で押したボタンを区別する | `mutation.isPending` だけでは押したボタンが分からない |

- 単体テストは `packages/ui/tests/action-pending.test.tsx`。
- 使う所: system-settings の `SystemTablesCard`・RAG のサービスのログ・Agent（Runtime・実行履歴・監査・Snapshot・フィードバック・利用状況）・NL2SQL の一覧の「最新情報を取得」（文言は [buttons.md §7](../ux-contracts/buttons.md#7-命名規則naming)「表示を取り直す操作」）と、Agent の監査の「フィルター適用」など。

## ChatUserMessage / createOptimisticChatMessage — **新規**（#907）

チャットの利用者のメッセージ（右寄せの吹き出し）と、送った質問を楽観的に出すための仮のメッセージの形。振る舞いの規則は UX 契約 messaging.md §11「チャットの送信」。送信・置き換えの API は製品ごとに違う（RAG は SSE、NL2SQL はジョブ、Agent は Run）ので、部品は形と状態だけを持つ。

```tsx
import {
  ChatUserMessage,
  createOptimisticChatMessage,
  withOptimisticChatStatus,
  type OptimisticChatMessage,
} from "@production-ready/ui";

const [pending, setPending] = useState<OptimisticChatMessage | null>(null);

function submit() {
  setPending(createOptimisticChatMessage(draft.trim()));   // status: "sending"、localId は画面の中だけの ID
  setDraft("");
  send.mutate(draft.trim(), {
    onSuccess: (created) => { putIntoConversationCache(created); setPending(null); },   // 確定したものに置き換える
    onError: () => setPending((p) => (p ? withOptimisticChatStatus(p, "failed") : p)),  // 残して再送信を出す
  });
}

{pending ? (
  <>
    <ChatUserMessage status={pending.status} failedLabel={t("chat.sendFailed")} testId="chat-pending">
      {pending.content}
    </ChatUserMessage>
    {pending.status === "failed" ? (
      <Banner severity="danger" action={<Button variant="secondary" size="sm" icon={RotateCcw} onClick={resend}>{t("chat.resend")}</Button>}>
        {reason}
      </Banner>
    ) : (
      <ProcessingIndicator active operationKey={pending.localId} startedAt={pending.sentAtMs} label={t("chat.answering")} />
    )}
  </>
) : null}
```

| 決めたこと | 理由 |
|---|---|
| 送信中（`sending`）も吹き出しの見た目は送信後と同じ（薄くしない・スピナーを付けない） | ChatGPT・Claude・Gemini と同じ。待ちの表示は回答の場所の 1 つだけにする（messaging.md §3.7 の単一スピナー） |
| 失敗（`failed`）は吹き出しの下に `failedLabel` を `AlertCircle` 付きで出す。原因と「再送信」は製品が Banner で出す | 質問の状態（送れていない）と原因・対処を分ける（messaging.md §9 P1 / P2）。色だけに頼らない |
| 状態は `data-status`（`sending` / `sent` / `failed` / `stopped`）に出す | e2e で状態を確かめる |
| 仮の ID は `crypto.randomUUID` を使わず時刻と連番で作る | http の IP 直打ちなど安全でない文脈で `randomUUID` が無い |
| `withOptimisticChatStatus(m, "sending")` は送信の時刻を数え直す。失敗・停止は送信の時刻を保つ | 再送信の経過時間を 0 から出す |
| 会話の持ち主以外（#3244 より前の system_admin。#1928）が送った質問は `senderLabel`（`ChatTurn` は `questionSender`）に「〈名前〉 が送信」を渡し、吹き出しの上に `UserRound`（14px）付きの `text-xs text-fg-muted` の文で出す（`data-chat-sender`）。持ち主が送った質問には渡さない | 同じ会話に別の人の質問が混ざることを、色ではなく文で分かるようにする。持ち主だけの会話は今までと同じ見た目 |

- 単体テストは `packages/ui/tests/chat-message.test.tsx`。
- 実ブラウザは RAG `e2e/chat.spec.ts`・NL2SQL `tests/e2e/sql-chat.spec.ts`・Agent `e2e/chat.spec.ts` の「#907」のテスト（応答を遅らせて、送信の直後に質問と作成中の表示が出ること、新しい会話・続きの会話、失敗 → 再送信、停止。desktop / 375px、ライト / ダーク）。
- 使う所: RAG の `components/chat/ChatClient.tsx`、NL2SQL の `features/nl2sql/SqlChatPage.tsx`、Agent の `pages/ChatPage.tsx`（保存済みの質問の吹き出しも同じ部品）。

## ChatProgress — **新規**（#1145）

チャットの回答の場所（アシスタントの吹き出しの中、回答の上）に置く、backend の処理の段階の控えめな表示。3 製品（RAG・NL2SQL・Agent）のチャットで同じ部品・同じ段階の形を使う。SQL 生成の画面の工程の表示（NL2SQL の `WorkflowProgressStrip`）より情報を絞る。

### 段階の形（3 製品共通の契約）

AG-UI の `STEP_STARTED` / `STEP_FINISHED` / `TOOL_CALL_*` / `RUN_ERROR` に倣う。backend は製品の既存の配信（polling / SSE）で、この形の一覧を画面へ渡す（画面が backend の値からこの形を作ってもよい。NL2SQL はジョブの `steps` から作る）。

```ts
import type { ChatProgressStep } from "@production-ready/ui";

type ChatProgressStep = {
  id: string;                 // 段階の識別子（例: "classify" / "schema" / "generate_sql" / "execute" / "summarize" / "tool:<name>"）
  label: string;              // 利用者向けの日本語（翻訳済み。例:「SQL を生成しています」）
  status: "pending" | "running" | "done" | "failed" | "skipped";
  startedAt?: string;         // ISO 8601
  finishedAt?: string;        // ISO 8601
  detail?: string;            // 任意の短い補足（対象の表・ツール名・件数）。SQL 全文・ORA コード等の技術的な詳細は入れない
  durationMs?: number | null; // 所要時間（時刻を持たない記録。渡すと startedAt / finishedAt より優先。#2096）
  meta?: string;              // 短い計測値（例:「LLM 1 回」。#2096）。一覧では名前の後ろの括弧に detail と並べる（#2265）
  level?: number;             // 入れ子の深さ（0 が最上位。1 段 1rem。深さを丸めず、深い一覧は内部で横スクロール。#3790）
};
```

- `label` は状態に合わせて製品が言い換えてよい（実行中「〜しています」、完了「〜しました」、失敗「〜できませんでした」、未実行は名詞）。
- 失敗の原因・対処は段階に入れず、回答の場所の danger の `Banner`（`ApiErrorBanner`）で出す（messaging.md §9 / §10）。

### 使い方

```tsx
import { ChatProgress } from "@production-ready/ui";

<Card>
  <CardContent className="space-y-3">
    <ChatProgress
      steps={steps}                 // ChatProgressStep[]
      active={inFlight}             // 省略時は段階から決める（running がある / 失敗が無く pending が残る）
      elapsedMs={totalMs}           // 完了後の全体の所要時間。省略時は段階の最初の開始から最後の終了まで
      startedAt={job.created_at}    // 実行中の経過時間の起点（処理全体の開始）。省略時は段階の最初の開始
      labels={{ status: { ...DEFAULT_CHAT_PROGRESS_LABELS.status, skipped: "未実行" } }}
      testId="chat-progress"
    />
    {answer}
  </CardContent>
</Card>
```

### ChatProgress の props

| prop | 型 | 既定 | 説明 |
|---|---|---|---|
| `steps` | `ChatProgressStep[]` | — | 段階の一覧（表示の順）。空なら何も出さない |
| `active` | `boolean` | 段階から決める | 処理中か。回答の本文の受信中など、段階の外で処理が続くときは明示する |
| `elapsedMs` | `number \| null` | 段階の時刻から | 完了後の全体の所要時間（時刻の無い記録は最上位の段階の `durationMs` の和） |
| `startedAt` | `string \| number \| null` | 段階の最初の開始 | 実行中の経過時間の起点（処理全体の開始。#1176）。どちらも無いときは表示が処理中になった時刻から数える |
| `slowAfterMs` | `number` | `10000` | 今の段階がこの時間を超えたら、実行中の見出し「処理中（N ステップ完了）」の直下に遅延の案内（`labels.slow`「通常より時間がかかっています。」。段階の名前は添えない）を出す（全体の経過時間では判断しない。#1176）。一度出たら処理が終わる（`progressKey` が変わる）まで消さない（#2426）。有限でない値（承認待ちの `Number.POSITIVE_INFINITY`）の間は出さず、出ていた案内も忘れる |
| `reconnecting` | `boolean` | `false` | 更新が途絶え、状態を取り直している（#1160）。見出しの直下の予約した行に、遅延の案内の代わりに「接続を確認しています。」を出す（#2426）。`useChatProgressStream` の `progressProps` で渡す |
| `labels` | `Partial<ChatProgressLabels>` | `DEFAULT_CHAT_PROGRESS_LABELS` | 文言（`running(completed, current)` / `summary(count, duration)` / `steps` / `elapsed` / `slow` / `working` / `reconnecting` / `status` / `formatDuration(ms)`） |
| `footer` | `ReactNode` | — | 開いた中身の末尾（タイムラインの後ろ。実行中は今の段階の行の後ろ）に出す内容（#2325）。一覧と一緒に開閉し、閉じているときは出ない。段階が 0 件のときは出さない（製品が単独で出す）。処理の経過に属する技術的な詳細（RAG の「処理の詳細」）に使う |
| `testId` | `string` | — | 根に `data-testid`。`-current`（今の段階。`data-step-id`（段階の外で処理が続くときは空）/ `data-slow` / `data-reconnecting`）・`-timer`（見出しの右の全体の経過時間）・`-notice`（見出しの直下の案内の行。`Disclosure` の `summaryFooter` で見出しの行の全幅に置き、左端を見出しの文字にそろえる。実行中は常にあり、高さを予約する。`data-slow` / `data-reconnecting`）・`-slow`・`-reconnecting`（`-notice` の中の案内の文言）・`-running`（実行中の見出し）・`-summary`（完了後の 1 行）・`-timeline`（一覧の `RunTimeline`。`data-run-timeline-bars`）・`-step-<id>`（一覧の行。`data-status` / `data-level`）を付ける・`-step-<id>-toggle`（詳細のある段の開閉のボタン）・`-step-<id>-details`（開いた詳細。#2294） |

根の要素は `data-chat-progress-state`（`running` / `done` / `failed`）と `aria-busy` を持つ。

| 決めたこと | 理由 |
|---|---|
| 実行中は見出しの行に全体の状態（「処理中（N ステップ完了）」と処理全体の経過時間。閉じているときは今の段階の名前も）だけを出し、開いた中身は 1 本のタイムライン（始まった段階を開始順に並べ、今の段階をその位置に出す。既定で開く。#2096 / #2309 / #2840）にする | 何をしたか・どこで時間がかかったかを最初から見せる（ユーザーの指示。Perplexity の検索の過程・GitHub Actions の steps と同じく、終わった工程が順に積み上がる）。閉じれば今の 1 行だけになる |
| 入れ子の段階は `level` で字下げし、最後に始まった実行中の末端の段階（子の段階を持たない段階）を今の段階にする。子の段階を持つ実行中の段階（親の工程）は回さず、子が途切れた間は親の 1 段下に「処理を続けています」を出す（#3436）。`meta`（LLM の回数など）は `detail` と並べて名前の後ろの括弧、右の列は失敗とスキップの文字・所要時間（#2096 / #2265） | RAG の回答フローの工程は入れ子（根拠の検索と回答の作成の中の文書の検索など）。計測値の置き場所を実行の詳細（`RunTimeline`）にそろえ、右の列を所要時間の 1 列にして時間の線と並べる |
| 実行中の経過時間は**処理全体**（`startedAt`、省略時は最初の段階の開始）から数え、実行中の見出しの行の右の 1 か所に出し続ける。今の段階の行の右には今の段階の経過時間（秒の単位）を出す。段階が変わっても 0 に戻さない。段階ごとの所要時間は完了した段階の行に出す（#1176） | 段階ごとに 0 から数え直すと、利用者には待った時間がリセットされたように見える（#1176 の指摘）。待った時間は全体の 1 つ、段階の内訳は一覧、と役割を分ける |
| 遅延の案内は**今の段階**の経過時間（`slowAfterMs`、既定 10 秒）で判断し、実行中の見出し「処理中（N ステップ完了）」の直下（`-running` の中の `-notice`）に「通常より時間がかかっています。」とだけ出す（段階の名前は添えない）。一度出たら処理が終わるまで消さない（`useLatchedSlow`）。取り直している間は同じ場所に「接続を確認しています。」（遅延の案内より優先）。今の段階の行（`-current`）には判断の結果（`data-slow` / `data-reconnecting`）だけを残す（#1176 / #2426） | LLM を使う処理は全体が 10 秒を超えるのが普通で、全体で判断すると毎回出て意味を失う。案内は見出し（全体の状態）に属する情報で、一覧を閉じていても見える見出しの中に置く。段階で判断したまま消すと、段階が変わるたびに出たり消えたりする（#2426 のユーザーの指示）。どの段階かは一覧の最後の行（今の段階と経過時間）で分かる |
| 終端でない（`active`）のに実行中・待機中の段階が無いときも、今の行（スピナー・「処理を続けています」・全体の経過時間）を出す（#1176） | 「N ステップ完了」だけが出て、止まったのか続いているのか分からない状態を作らない。製品は backend の段階を写し漏らさない（NL2SQL はジョブの全段階を写す）。これは写し漏れの最後の備え |
| 遅延の案内の行（見出しの直下）は実行中の最初から高さを予約する（`ProcessingIndicator` の #902 と同じ） | 10 秒後に行が足されて見出し・一覧・スピナーの行が動かない |
| 実行中の段階はタイムラインの最後の行に置き、段階が終わると同じ位置で完了の行に変わる（#2309。今の段階を一覧の外の先頭に置く形をやめた） | 時間の順に上から下へ読める（ChatGPT / Claude の思考とツールの経過・Perplexity の検索の過程・GitHub Actions の steps と同じ形）。先頭に置くと新しい段が上・古い段が下の逆順になり、段が終わるたびに上の行から下の一覧へ跳んでいた |
| 段階は始まった時点で開始順の位置に出し、その場で状態だけを変える。今の段階（最後に始まった実行中の末端の段階）の行はその位置に置き、今の段階を包む実行中の親の工程も開始順の位置に回さない実行中の印（`RunTimeline` の `spinRunning={false}`）で出す（#2840） | 終わった段階と今の段階だけを出すと、入れ子の工程で親の行が出ないまま子だけが出て、親が終わったときに子の上へ差し込まれ、一覧が入れ替わって見えた。一度出た行は消さず、動かさない（GitHub Actions の steps・AG-UI の STEP_STARTED / STEP_FINISHED と同じ）。動くスピナーは今の段階の 1 つだけ（messaging.md §3.7） |
| 実行中は開き、完了したら「処理の経過（N ステップ・M 秒）」の 1 行に閉じる（会話の履歴から開いた往復も閉じる。#2118 / #2121 / #2265）。製品は開閉を選べない（`collapseWhenDone` / `defaultOpen` は削除） | 実行中は何をしているかを見せ、回答ができた後は回答の本文を主にする（16〜17 行の工程の一覧が回答の上を占めない。ユーザーの指示）。失敗はどこで止まったかを見せるため開いたまま。置き場所は 3 製品とも回答の上（作成中の「N ステップ完了」の場所のまま。#2115）。開閉を prop にすると製品ごとにずれる（NL2SQL だけ開いたままだった） |
| 一覧は実行の詳細と同じ `RunTimeline`（`chatProgressTimelineStep` で写す）で描く。完了後は時間の線つき（置き場所が 36rem 以上）、実行中の完了した段の一覧は線なし。一覧の中に「N ステップ・合計 M 秒」の行は出さない（#2265） | 同じ工程をチャットと実行の詳細で 1 つの形にする（3 製品で同じ部品）。実行中は全体の長さが伸び続け、線が毎回伸び縮みするので出さない。件数と合計は開閉の見出しが持つ |
| 段階の詳細（`ChatProgressStep.details`。backend の段のイベントの `details` を `labelChatProgressSteps` がそのまま写す）は、一覧の行を開いて見る。詳細のある段階だけに開閉のボタンを付け、既定で閉じる（一覧の開閉とは別。#2294） | Agent の実行の詳細と同じく段ごとに何をしたか確かめられ、回答を読む妨げにならない。3 製品で 1 つの形（`RunStepDetails`） |
| 状態はアイコン（`CheckCircle2` / `XCircle` / `MinusCircle` / `Circle` / `Spinner`）と文字（失敗・スキップは見える文字、完了は読み上げの文字）で示す | 色だけに頼らない |
| 段階の切り替わりだけを `role="status"` で読み上げる。経過時間は `role="timer"` + `aria-live="off"` | 毎秒の更新を読み上げない。完了後は読み上げの文を空にする（回答は会話の欄の `role="log"` が知らせる） |
| 動くスピナーは今の段階の 1 つだけ。スピナーは reduced-motion でも回す（#440） | 同じ処理のスピナーは 1 つ（messaging.md §3.7）。開閉の Chevron は `Disclosure` が reduced-motion で回転を止める |
| 所要時間の表記は「0.4 秒」（10 秒未満は 0.1 秒単位。0.1 秒に満たなければ「0.1 秒未満」）・「12 秒」・「1 分 5 秒」（`formatChatProgressDuration`） | 1 秒未満の段階が多く、「0 秒」「0.0 秒」と出すと情報が無い。ms と秒を混ぜない |
| 処理の経過に属する技術的な詳細（回答の記録の詳細など）は `footer` で開いた中身の末尾に置き、処理の経過の外（回答の本文との間）に独立して置かない（#2325） | 処理の経過と回答の本文の間に別の開閉を置くと、回答の補足と読み違える（ユーザーの指摘）。処理の経過を閉じれば一緒に隠れ、回答の本文を主にする |
| `ProcessingIndicator` / `WorkflowProgressStrip` を拡張せず、新しい部品にした | `ProcessingIndicator` は 1 つの処理の 1 行（段階を持たない）で、設定・一覧の読み込みなど多くの所で使う。`WorkflowProgressStrip` は NL2SQL の SQL 生成の画面だけの工程の帯（工程ごとの説明・結果の中身・中止を持つ）。チャットの段階は両者の間の密度で、3 製品が使うため `packages/ui` に置く。経過時間の計算は `useOperationTiming`、開閉は `Disclosure`、スピナーは `Spinner` を使い回す |

- 単体テストは `packages/ui/tests/chat-progress.test.tsx`。
- 実ブラウザは NL2SQL `tests/e2e/sql-chat.spec.ts` の「#1145」のテスト（応答を遅らせて、送信・開始待ち・実行中・完了・失敗の段階を desktop / 375px・light / dark で確かめる）。
- 使う所: NL2SQL の `features/nl2sql/SqlChatPage.tsx`、RAG の `components/chat/ChatClient.tsx`、Agent の `pages/ChatPage.tsx`。段階は 3 製品とも `useChatProgressStream`（下の節）で backend の段階のイベントから作り、名前は製品の段階の定義（NL2SQL `features/nl2sql/chatProgress.ts`・RAG `lib/chat-progress.ts`・Agent `lib/chat-progress.ts`）で付ける（#1359）。

### useChatProgressTracker — 処理の経過の状態を追う（#1160）

3 製品のチャットが「処理の経過」の状態を追う処理を 1 つにした hook。表示は `ChatProgress`、終端の判定と、配信が途絶えたときの取り直しはこの hook が持つ。製品は自分の配信を hook の入力に合わせる薄いアダプタだけを持つ。

```tsx
import { ChatProgress, useChatProgressTracker } from "@production-ready/ui";

const progress = useChatProgressTracker({
  key: job.id,                      // 追う対象（ジョブ ID・Run ID・保存した質問の ID）。変わったら数え直す
  steps,                            // 今の段階（ChatProgressStep[]）
  active: inFlight(job),            // 省略時は段階から（isChatProgressActive）。false で取り直しをやめる
  elapsedMs,                        // 完了後の全体の所要時間
  receivedAt: query.dataUpdatedAt, // polling: 最後に取得できた時刻。push の配信は progress.touch() を呼ぶ
  refresh: () => query.refetch({ cancelRefetch: true, throwOnError: false }),
  staleAfterMs: 10_000,             // 既定 15 秒
});

<ChatProgress {...progress.progressProps} labels={labels} testId="chat-progress" />;
```

| 入力 / 戻り値 | 説明 |
|---|---|
| `refresh(signal)` | 状態を取り直す。配信が `staleAfterMs` 届かなければ呼び、`refreshTimeoutMs`（既定 15 秒）で打ち切る（signal を見ない関数・終わらない promise も打ち切る）。失敗・時間切れは 1 秒・2 秒・4 秒 … `maxBackoffMs`（既定 30 秒）の間隔で続け、終端（`active` が false）か対象が変わるまで止めない |
| `enabled` | false の間は追わない（keep-alive で画面が隠れている・承認待ちなど取り直さない状態） |
| `touch()` | push の配信（SSE の event・heartbeat、WebSocket のメッセージ）を受け取ったときに呼ぶ。取り直している間に届いたときだけ描画する |
| `refreshNow()` | 待たずに取り直す（配信が終端の前に終わった・切れたと分かったとき）。次に配信が届くまで、途絶えの時間に関係なく backoff して取り直し続ける |
| `reconnecting` | 取り直しを始めた後に配信が届いていない。`progressProps.reconnecting` で `ChatProgress` に「接続を確認しています。」を出す |

| 決めたこと | 理由 |
|---|---|
| 「途絶え」は段階が進まないことではなく、配信（取得の成功・SSE のバイト）が届かないこと | 準備・生成が数分かかるのは正常（#1155）。応答が返らない取得・切れた接続だけを取り直す |
| 取り直しは backoff して終端まで続け、あきらめない（あきらめる条件は製品のアダプタが持つ） | NL2SQL のチャットで、応答の返らない取得を待ったまま 12 分更新が止まった（#1160）。終わった結果が画面に出ないまま止まる経路を残さない |
| タブが非表示の間は取り直さず、表示に戻ったら待たずに確かめる | polling もタブが非表示の間は止まる。戻ったときに古い状態を見せ続けない |
| 案内は遅延の案内と同じ予約した行に出す | 案内が出ても今の段階の行・経過時間は動かない |

- 単体テストは `packages/ui/tests/chat-progress-tracker.test.tsx`。
- 実ブラウザは NL2SQL `tests/e2e/sql-chat-progress-refresh.spec.ts`・RAG `e2e/chat-progress-refresh.spec.ts`・Agent `e2e/chat-progress-refresh.spec.ts`。
- 製品はこの hook を直接使わず、配信を含めた `useChatProgressStream`（次の節）を使う（#1359）。

### useChatProgressStream — 処理の段階のイベントを受け取り、段階の一覧にまとめる（#1359）

3 製品のチャットの処理の段階は、段階の一覧の snapshot ではなく**追記型のイベント**で配る（契約の正本は `platform/contracts/chat-progress/chat-progress-events.json`、backend は `pr_backend_core.chat_progress`。[backend-standard.md](../backend-standard.md)「チャットの処理の段階の配信」）。この hook は配信（SSE を優先し polling に縮退）・途絶えの取り直し（中で `useChatProgressTracker` を使う）・イベントの組み立て（`reduceChatProgressEvents`）・名前付け（製品の段階の定義）を 1 つにしたもの。製品は段階の定義（`kind` → i18n の名前）と API の URL だけを渡す。

```tsx
import { ChatProgress, useChatProgressStream, type ChatProgressStepDefinitions } from "@production-ready/ui";

const definitions: ChatProgressStepDefinitions = {
  generate_sql: { label: (status) => t(`chat.progress.generate_sql.${chatProgressLabelState(status)}`) },
  tool: { label: (status, params) => t(`chat.progress.tool.${chatProgressLabelState(status)}`, { tool: String(params.tool) }) },
};

const progress = useChatProgressStream({
  key: job.job_id,                                        // 対象（target_id）。変わったら作り直す
  definitions,
  events: job.progress_events,                            // 保存済みの記録（会話の取得に入っているもの）
  streamUrl: inFlight ? `/api/jobs/${id}/progress/stream` : null, // SSE（hook が since を付ける）
  fetchEvents: (since, signal) => getProgress(id, since, signal), // polling・取り直し（since より後）
  active: inFlight,                                       // 終端のイベントが届いたら関係なく終わり
  onTerminal: () => conversation.refetch(),               // 結果の取り直し
});

<ChatProgress {...progress.progressProps} labels={labels} testId="chat-progress" />;
```

| 入力 / 戻り値 | 説明 |
|---|---|
| `streamUrl` | SSE。`EventSource` で `?since=<cursor>` を付けて開く。切れたらブラウザが `Last-Event-ID` で続きから張り直す。応答が SSE でない（4xx / 5xx・終わった対象の 204）か、続けて 3 回失敗したら `fetchEvents` の polling に縮退する |
| `fetchEvents(since, signal)` | `since` より後のイベント（`ChatProgressPage`）。polling（`pollIntervalMs`、既定 2 秒）・途絶えの取り直し・番号の飛びの穴埋めに使う。応答は `since` の後をすべて含むものとして扱う |
| `events` | 製品が受け取ったイベント（保存済みの記録、RAG の回答の配信の `chat_progress`）。増えるたびに積む（重複は捨てる） |
| `pollEvents` | 既定 `true`（SSE が無いとき `fetchEvents` で polling する）。`false` は製品の自前の配信（RAG の作成中の回答の SSE）が段階を運ぶとき: `fetchEvents` は途絶えの取り直しと番号の飛びの穴埋めにだけ使い、取り直しの成功を配信の再開に数えない |
| `touch()` / `onStalled` | 製品の自前の配信（RAG の回答の SSE）のバイトを受け取ったら `touch()`。途絶えたら `onStalled` で製品の配信を張り直す |
| `transport` | `sse` / `polling` / `push`（`events` だけ）/ `idle`（終わった・追わない） |
| `progressProps` | `ChatProgress` へ渡す。`progressKey` は `<対象>#<試行>`（試行が変わったら作り直す） |

| 決めたこと | 理由 |
|---|---|
| イベントは `seq` の順に、前の番号の次から続けて適用する。古い・重複は捨て、番号が飛んだら取り直しで埋める | 古い snapshot・取り直しの応答・再接続・複数 worker で、新しい表示が古い更新で上書きされない（#1359。snapshot の置き換えでは順序を見分けられなかった） |
| 段階は `step_id` で結び、最初に出た順に並べる。状態は進む方へだけ変える（#1358 の規則をイベントに当てはめたもの）。試行（`attempt`）と対象が変わったら作り直す | 処理中に一度出した段階が消えない・戻らない。引き継いだ実行は段階を初めからにする |
| 名前は契約に入れず、製品の段階の定義（`kind` と `params`）で付ける | 利用者向けの文言は製品の i18n が正本。backend は id・状態・値だけを記録する |
| SSE を優先し、polling に縮退する | 段階はすぐ届き、中継・ブラウザが SSE を通さない環境でも止まらない |

- 製品の対応: RAG（回答のメッセージ。作成中の回は回答の配信の SSE の `chat_progress` が運び、保存済みの作成中の回答は `GET /api/chat/conversations/{id}/messages/{回答の id}/progress[/stream]`）、NL2SQL（ジョブ。`GET /api/nl2sql/jobs/{job_id}/progress[/stream]`）、Agent（Run。`GET /api/runs/{run_id}/progress[/stream]`）。NL2SQL・Agent は終端で `onTerminal` から結果（会話・Run）を取り直す。
- 単体テストは `packages/ui/tests/chat-progress-events.test.ts`（契約との一致・古い / 重複 / 順序の入れ替わり・試行・終端・対象の切り替え）と `tests/chat-progress-stream.test.tsx`（SSE・縮退・途絶えと張り直し・穴埋め・終端）。

## ResultTable — **新規**（#1154 / #1178。旧名 ChatResultTable）

データの結果（読み取りだけの行と列）を出す部品です（3 製品で共通）。チャットの回答の吹き出しの中の SQL・ツールの実行の結果と、画面のクエリの結果・テーブルのデータの表示・取り込みのサンプル行に使います（#1178。合う画面の基準は UX 契約 `page-archetypes.md`「データの結果の型」）。振る舞いの表は README §4「`ResultTable`」。旧名 `ChatResultTable` などは別名として残しています（非推奨）。製品は列・取得した行・打ち切りの有無を渡すだけで、要約・プレビュー・打ち切りの明示・すべての行・CSV・NULL・数値の右寄せは部品が持ちます。実行中（`ProcessingIndicator`）・失敗（danger の `Banner`）・実行の操作（`Button`）は製品が部品の外に置きます。

```tsx
import { ResultTable, toast } from "@production-ready/ui";

<ResultTable
  columns={[{ name: "CATEGORY" }, { name: "AMOUNT", type: "number" }]}
  rows={[["家電", 1200], ["食品", null]]}      // 列の順の値の配列。NULL は null
  truncated={result.has_more}                  // 取得の上限で打ち切った
  rowLimit={1000}                              // 打ち切りの案内に出す上限
  elapsedMs={800}
  csvFilename="nl2sql-chat-result-20261005-140312.csv"
  onCsvDownloaded={() => toast.success(t("common.action.downloaded"))}
  fullResult={{                                 // 上限を超える全件の導線（製品の画面）
    href: "/direct-sql",
    label: "SELECT SQL を実行で開く",
    linkComponent: DirectSqlLink,               // react-router の Link（state で SQL を渡す等）
    hint: "すべての行が必要なときは、…で実行してください。",
  }}
  testId="sql-chat-result"
/>
```

### ResultTable の props

```ts
export interface ResultTableColumn { name: string; type?: string }   // type が number 等なら右寄せ
export interface ResultTableProps {
  columns: readonly ResultTableColumn[];
  rows: readonly (readonly unknown[])[];       // 取得した行（列の順）。NULL は null
  truncated?: boolean;                         // 上限（行数・応答の大きさ）で打ち切った
  totalRowCount?: number | null;               // 総件数が分かるときだけ（「全 N 行」）
  rowLimit?: number | null;                    // 1 回の取得の上限（案内に出す）
  cellsTruncated?: boolean;                    // セルの文字数の上限で値を切った
  maxCellChars?: number | null;
  elapsedMs?: number | null;
  previewRows?: number;                        // プレビューに描く行（既定 50 = RESULT_PREVIEW_ROWS）
  pageSizeOptions?: readonly number[];         // すべての行の 1 ページの行数（既定 25 / 50 / 100）
  csvFilename?: string;                        // 既定 result.csv
  onDownloadCsv?: (csv: string, filename: string) => void;  // 省略時は部品がダウンロード
  onCsvDownloaded?: () => void;
  fullResult?: { href?: string; label?: string; linkComponent?: ButtonLinkComponent; hint?: string };
  actions?: React.ReactNode;                   // 要約の行の右に足す製品の操作
  meta?: React.ReactNode;                      // 要約の文の右の画面固有の補足（取得上限・接続の StatusBadge。#1178）
  labels?: Partial<ResultTableLabels>;     // 既定は DEFAULT_RESULT_TABLE_LABELS（日本語）
  className?: string;
  testId?: string;  // <testId>-summary / -table / -scroll / -preview-note / -truncated / -cells-truncated / -view-all / -csv / -sheet / -all-csv / -all-table / -all-scroll / -all-pagination
}
```

- あわせて export: `ResultCell`（セルの表示。結果の画面の表も同じ表示にする）、`isNumericResultColumn`、`isNullResultValue`、`resultValueText`、`resultRowsToCsv`（BOM・CRLF・RFC 4180・式の無害化）、`resultSummaryText`、`RESULT_PREVIEW_ROWS`、`RESULT_PAGE_SIZES`、`DEFAULT_RESULT_TABLE_LABELS`。
- 画面での使い方（#1178）: NL2SQL は API の形（列名をキーにした行）を `features/nl2sql/queryResultTable.ts` の `toResultTableData` で変換し、`components/QueryResultTable.tsx`（表の名前・取得上限と接続の `meta`・CSV のファイル名を渡す薄いラッパー）から出す。製品で `DataTable` + `Pagination` の結果の表を書かない。
- 単体テストは `packages/ui/tests/result-table.test.tsx`、実ブラウザは NL2SQL の `tests/e2e/sql-chat-execution.spec.ts`（チャット。desktop / 375px、プレビューの表の中の縦横のスクロール、打ち切り、シートのページ送り、CSV）と `tests/e2e/nl2sql-workflows.spec.ts`（SQL 生成・SELECT SQL・管理 SQL・データの表示）。

### 表の形の判定（`toTabularData` / `splitMarkdownTables`。#1158）

ツールの結果・成果物の JSON と、回答の本文の Markdown の表を `ResultTable` の列と行にします。製品に依存しない規則なので、製品で判定を書かず、この関数を通します（使う所: Agent のチャットと実行履歴の詳細）。

```tsx
import { MessageText, ResultTable, splitMarkdownTables, toTabularData } from "@production-ready/ui";

// JSON: 表の形なら列と行、そうでなければ null（今の JSON の表示のまま）。
const table = toTabularData(step.tool_result.output);
{table ? (
  <ResultTable
    columns={table.columns}
    rows={table.rows}
    truncated={table.truncated}        // truncated / has_more
    totalRowCount={table.totalRowCount} // total / total_row_count / row_count
    elapsedMs={table.elapsedMs}         // elapsed_ms
  />
) : (
  <JsonPreview value={step.tool_result.output} />
)}

// 回答の本文: 表とそれ以外の文に分ける（コードブロックの中の表は表にしない）。
splitMarkdownTables(answer).map((segment) =>
  segment.kind === "table" ? <ResultTable {...segment.data} /> : <MessageText text={segment.text} />
);
```

- 表とみなす形: `{ columns, rows }`（`columns` は列名の文字列か `{ name, label?, type? }`、`rows` はオブジェクトか配列の配列。列の指定があれば 0 行も表）、`{ rows }`（オブジェクトの配列。1 行以上）、オブジェクトの配列（1 行以上。key の和集合を列にする）。行に無い値は `null`。
- セルは文字列・数値・真偽値・null だけ。入れ子のオブジェクト・配列を持つ値、列の分からない 0 行、`columns: []` の 0 行（実行中のジョブ）は表にしない（`null`）。
- Markdown の表は GFM の表頭・区切りの行・本文の行。セルの足りない行は空の文字列、`\|` は `|`、`**` と `` ` `` は除く。値がすべて数（桁区切り・小数・%）の列は `type: "number"`（右寄せ）。
- 単体テストは `packages/ui/tests/tabular-data.test.ts`、実ブラウザは Agent の `e2e/run-result-tables.spec.ts`（チャットと実行履歴の詳細、desktop / 375px、light / dark、60 行・0 行・打ち切り・表でない JSON）。

## ChatLayout / useChatHistoryPanel — **新規**（#1161）

チャットの骨格（3 製品共通）。会話の履歴（lg 以上は本文の横の `<aside>`、未満はモーダルの `SideSheet`）と、会話の領域（上端の行・`role="log"` の会話の欄・入力欄の領域）を 1 つの部品で描く。画面の型は UX 契約 [page-archetypes.md §6](../ux-contracts/page-archetypes.md#6-チャット会話の画面1161)。製品は履歴の中身・往復の表示・入力欄・文言を渡す。

```tsx
import { ChatLayout, useChatHistoryPanel } from "@production-ready/ui";

// lg 以上のインラインの開閉は製品の作業状態に残す（lg 未満のシートは残さない）。
const [historyOpen, setHistoryOpen] = useWorkspaceState("chat.historyOpen", false);
const history = useChatHistoryPanel({ inlineOpen: historyOpen, onInlineOpenChange: setHistoryOpen });

<ChatLayout
  history={history}
  historyTitle={t("chat.history")}            // 「会話の履歴」
  historyCloseLabel={t("chat.closeHistory")}  // 「会話の履歴を閉じる」
  historyContent={<ConversationList onSelect={(id) => { history.closeSheet(); open(id); }} />}
  label={t("chat.title")}
  conversationTitle={conversation?.title}
  conversationTitleLoading={Boolean(conversationId) && conversationQuery.isPending}
  newConversation={{ label: t("chat.new"), onClick: startNew }}
  logLabel={t("chat.messages")}               // 「会話」
  logRef={logRef}                             // 自動スクロールはこの要素の scrollTo
  composer={<FieldActionRow actions={<RunStopButton … />}><TextareaField … /></FieldActionRow>}
  testIdPrefix="chat"
>
  {turns}
</ChatLayout>
```

### ChatLayout の props

| prop | 型 | 既定 | 説明 |
|---|---|---|---|
| `history` | `ChatHistoryPanel` | — | `useChatHistoryPanel` の戻り値 |
| `historyTitle` / `historyCloseLabel` | `string` | — | 履歴の見出し・名前（開閉ボタンの名前にも使う）と、シートを閉じるボタンの名前 |
| `historyCaption` | `string \| null` | — | 一覧を絞った期間の補足（「最近 90 日」。lg 以上は見出しの行の右端、lg 未満はシートの本文の先頭。パネルの `aria-describedby`）。一覧の応答の `history_days` から `chatHistoryWindowLabels(days)?.caption` で作り、0 件の文は同じ関数の `empty`（#2340） |
| `historyContent` | `ReactNode` | — | 履歴の中身（一覧・読み込み中・失敗・空・ページング） |
| `label` | `string` | — | 会話の領域（`<section>`）の名前 |
| `conversationTitle` / `conversationTitleLoading` | `string \| null` / `boolean` | — / `false` | 上端の行の会話の名前。名前が無く読み込み中なら `Skeleton`、どちらでもなければ何も出さない |
| `newConversation` | `{ label, onClick, disabled? }` | — | 上端の行の右端の「新しい会話」（`MessageSquarePlus`） |
| `historyToggleDisabled` | `boolean` | `false` | 履歴の開閉を押せない間（対象の一覧の読み込み中など。#1153） |
| `logLabel` / `logRef` | `string` / `Ref<HTMLDivElement>` | — | 会話の欄（`role="log"`）の名前と要素 |
| `children` | `ReactNode` | — | 会話の欄の中身 |
| `composer` | `ReactNode` | — | 入力欄の領域の中身（設定の行・入力欄と送信・通知） |
| `embedded` | `boolean` | `false` | スクロールするページの中に置く（業務 Agent の編集画面の「試す」。#2515）。どの幅でも全体の高さを 70dvh（最小 28rem）に固定し、残りの高さに伸ばさない |
| `testIdPrefix` / `testIds` | `string` / `ChatLayoutTestIds` | `"chat"` | testid。既定は `<prefix>-history`・`-history-toggle`・`-panel`・`-conversation-title`・`-conversation`・`-composer-region`。シートの scrim は `<history>-scrim` |

### useChatHistoryPanel

`useChatHistoryPanel({ inlineOpen, onInlineOpenChange, id? })` → `{ inline, open, inlineOpen, sheetOpen, toggle, closeSheet, id, toggleRef }`。幅の判定は `useMediaQuery(CHAT_HISTORY_INLINE_QUERY)`（`(min-width: 1024px)`）。

| 決めたこと | 理由 |
|---|---|
| 履歴は既定で閉じ、lg 以上は開くと 20rem の列で会話の左に並べる。閉じている間も `<aside hidden>` を描く | 多くの利用者は履歴を使わないので会話に面積を渡す（#664）。開閉ボタンの `aria-controls` の先を常に保つ |
| lg 未満はモーダルの `SideSheet`。開閉は作業状態に残さず、幅が lg を越えたら閉じる | 戻ったとき・再読込でモーダルが画面を塞がない（workspace-state.md）。広げたときにモーダルが残らない |
| 会話の領域は lg 未満で高さ 70dvh（最小 28rem）、lg 以上で残りの高さ。会話の欄だけがスクロールする | 長い会話でページを伸ばさず、入力欄を常に会話の領域の下に置く（README §4「AppShell」） |
| 上端の行に履歴の開閉（左端）・会話の名前・「新しい会話」（右端）の 3 つだけを置く | 履歴を閉じていても会話の切り替え・新しい会話に届く。「新しい会話」は 1 か所（#889） |
| 文言は翻訳済みを受け、往復の表示・入力欄・履歴の中身は持たない | 業務の語彙と API（SSE・ジョブ・Run）は製品ごとに違う（page-archetypes.md §6.2） |

- 単体テストは `packages/ui/tests/chat-layout.test.tsx`。
- 実ブラウザは RAG `e2e/chat.spec.ts`（#664 の履歴の開閉・375px のシート）、NL2SQL `tests/e2e/sql-chat.spec.ts`、Agent `e2e/chat.spec.ts`。
- 使う所: RAG `components/chat/ChatClient.tsx`、NL2SQL `features/nl2sql/SqlChatPage.tsx`、Agent `pages/ChatPage.tsx`。

## ChatComposer / ChatComposerOption — **新規**（#1161）

チャットの入力欄の領域（3 製品共通）。`ChatLayout` の `composer` に渡す。入力欄（`TextareaField`、3.5 行。#2008）と送信 / 停止（`RunStopButton`、lg）を `FieldActionRow` で並べ、上に設定の行（`ChatComposerOption`）、下に通知を置く。

```tsx
import { ChatComposer, ChatComposerOption } from "@production-ready/ui";

<ChatComposer
  id="chat-composer"
  textareaRef={composerRef}
  value={draft}                       // 下書きは製品の作業状態
  onValueChange={setDraft}
  onSubmit={submit}
  onStop={stop}
  running={sending}                   // 送信の要求中・回答の作成中（同じボタンが「停止」）
  submitBlocked={!target || waitingChoice || conversationLoading}
  disabled={targetsLoading}            // 対象の一覧の読み込み中は書けない（#1153。書いた文字は残す）。
                                      // 会話の内容の読み込み中は書けて、送信だけ止める（submitBlocked。#1188）
  label={t("chat.composer.label")}    // 「質問」（読み上げだけ）
  placeholder={t("chat.composer.placeholder")}  // 「質問を入力（Enter で送信、Shift+Enter で改行）」
  sendLabel={t("chat.send")}
  stopLabel={t("chat.stop")}
  sendTestId="chat-send"
  options={
    <ChatComposerOption
      label={t("chat.engine")}        // 「生成方法」
      labelDecorative                 // 中の SelectField が同じ名前を labelHidden で持つ
      info={{ label: t("chat.engine.infoLabel"), content: description, contentId }}
    >
      <SelectField id="engine" label={t("chat.engine")} labelHidden size="sm" width="sm" … />
    </ChatComposerOption>
  }
  footer={stopError ? <ApiErrorBanner error={stopError} fallback={t("chat.stopFailed")} /> : null}
/>
```

| 決めたこと | 理由 |
|---|---|
| Enter で送信、Shift+Enter で改行、IME の変換を確定する Enter では送らない（`isSubmitEnter`） | 日本語の入力で変換の確定が送信にならない（#459） |
| 実行中・送れない間（入力が空・`submitBlocked`）の Enter は何もしない。停止はボタンだけ | Enter の押し間違いで回答を止めない（buttons.md §3.1） |
| 入力欄は回答の作成中も書ける。書けないのは前提（対象の一覧・会話の内容）の読み込み中（`disabled`）だけ。送れない間の送信のボタンは `aria-disabled`（フォーカスを受ける） | 次の質問を書ける（messaging.md §11.1）。送信の後に入力欄が空になってもフォーカスが外れない（#355） |
| 入力欄の名前は「質問」に 3 製品でそろえ、画面には出さない（placeholder で目的を示す） | 製品ごとに「メッセージを入力…」「クエリ」「質問」と違っていた。目的は会話の領域から分かる |
| 設定の行は「名前・info アイコン・選択」。説明は常設しない | 入力欄の上を説明の文で埋めない（#901） |
| 停止の失敗などの通知は `footer`（入力欄の下）に出す | 起点の操作（送信 / 停止のボタン）の直下（messaging.md §10） |
| 閲覧だけの会話（system_admin が開いた他の利用者の会話）では、この部品を置かず `ChatReadOnlyNotice` を置く（#3244。#1954 の送る前の案内 `notice` は外した） | 他の利用者の会話には送れない（backend も 403）。送れない入力欄を無効にして残さない |

- 単体テストは `packages/ui/tests/chat-composer.test.tsx`。
- 使う所: RAG（設定の行は「回答するモデル」の `ToggleChip`）、NL2SQL（「生成方法」の `SelectField`、上限 10,000 文字）、Agent（設定の行なし）。

## ChatReadOnlyNotice — **新規**（#3244）

閲覧だけの会話の、入力欄の代わりの案内（3 製品共通）。system_admin が他の利用者の会話を開いたとき、`ChatLayout` の `composer` に `ChatComposer` の代わりに渡す。

```tsx
import { ChatReadOnlyNotice } from "@production-ready/ui";

composer={
  readOnly ? (
    <ChatReadOnlyNotice
      onStop={running ? stop : undefined}   // 回答の作成中・処理中・承認待ちだけ「停止」を出す
      stopping={cancel.isPending}
      footer={cancel.error ? <ApiErrorBanner error={cancel.error} fallback={t("chat.stopFailed")} /> : null}
      testIdPrefix="chat-read-only"          // 既定。案内は `-notice`、停止は `-stop`（NL2SQL は `sql-chat-read-only` など）
    />
  ) : (
    <ChatComposer … />
  )
}
```

| 決めたこと | 理由 |
|---|---|
| info の `Banner` に見出し「他の利用者の会話です（閲覧のみ）」と本文（`OTHERS_CONTENT_FORBIDDEN_MESSAGE` と同じ文。`DEFAULT_CHAT_READ_ONLY_NOTICE_LABELS`）。3 製品は既定の文を使う（`labels` で上書きできる） | 3 製品で同じ機能の文と見た目をそろえる。画面の案内と API の 403 の文を同じ意味にする |
| 入力欄・送信を無効にして残さず、案内に置き換える | 読み取り専用を無効の入力欄で表さない（送れそうに見える・読み上げが「無効」になる） |
| 停止は運用の操作なので、作成中の回答・処理中の実行があるときだけ案内の中に `secondary`・sm の「停止」（`Square`。要求中は `loading`）を置く。停止の失敗は `footer` | 起点の操作の直下に結果を出す（messaging.md §10）。停止できる物が無いのにボタンを出さない |
| 削除・承認の判断は案内に置かない（会話の履歴の行・往復の中のまま） | 運用の操作は今の場所で行う。名前の変更は `useChatHistoryActions` の `canRename` で行ごとに隠す |

- 単体テストは `packages/ui/tests/chat-read-only-notice.test.tsx`。e2e は RAG `e2e/chat-visibility.spec.ts`・NL2SQL `tests/e2e/sql-chat-visibility.spec.ts` / `db-admin-chat.spec.ts`・Agent `e2e/ai-use-visibility.spec.ts`（desktop と 375px）。

## ChatHistoryList — **新規**（#1161）

チャットの会話の履歴の一覧（3 製品共通）。`ChatLayout` の `historyContent` に渡す。製品は会話・スレッドの型を `ChatHistoryItem`（`{ id, title, meta?, badge? }`）に写す。

```tsx
<ChatHistoryList
  items={conversations.map((c) => ({ id: c.id, title: c.title, meta: formatMeta(c), badge: statusBadge(c) }))}
  currentId={conversationId}
  onSelect={(item) => { history.closeSheet(); open(item.id); }}
  waiting={targetsLoading}                 // 対象の一覧の読み込み中は形だけ（経過時間は対象のカード。#1153）
  loading={query.isLoading}
  error={query.isError ? query.error : null}
  onRetry={() => void query.refetch()}
  labels={{ list: "会話の履歴", loading: "会話を読み込んでいます", error: "会話の履歴を読み込めませんでした。", retry: "再試行", empty: "まだ会話がありません" }}
  renderActions={historyActions.renderActions}   // 名前の変更・削除（useChatHistoryActions。3 製品。#1817）
  renderEditor={historyActions.renderEditor}
  footer={<Pagination … />}                                  // offset の API は Pagination、カーソルは「さらに読み込む」
/>
```

| 決めたこと | 理由 |
|---|---|
| 行は名前（1 行で省略し `title` で全文）・補足・状態のバッジ。高さは `INFORMATION_LIST_ROW_CLASS`（3.5rem 以上）。開いている会話は `bg-accent-subtle` と `aria-current="true"` | 3 製品で行の形・選択の示し方が違っていた（RAG は 1 行の名前と補足、NL2SQL は ghost のボタン、Agent は 2 行の名前） |
| 行の操作（名前の変更・削除）は sm 以上ではホバー・フォーカスの間と開いている会話で出し、sm 未満は常に出す | 一覧を読みやすくし、タッチ端末ではホバーが無い |
| 読み込み中は `TimedLoadingState` + 行の形の `ListSkeleton`、失敗は `ApiErrorBanner`（要約は `labels.error`）と再試行、0 件は短い文 | 読めていない一覧を 0 件と出さない（messaging.md §3.6）。失敗の詳細は「詳細」に畳む（§10.3） |
| 「新しい会話」は一覧に置かない | 会話の領域の上端の行の 1 か所（#889） |
| ページングは `footer` で製品が選ぶ | API に合わせる（page-archetypes.md「一覧の型と、基準から外す例外」） |

- 単体テストは `packages/ui/tests/chat-history-list.test.tsx`。
- 使う所: 3 製品のチャット（名前の変更・削除・`Pagination`）。Agent は状態のバッジも出す。

## useChatHistoryActions — **新規**（#1817）

会話の履歴の行の「名前の変更」「削除」（3 製品共通）。返す `renderActions` / `renderEditor` を `ChatHistoryList` に渡す。製品は API の呼び出しと、削除の前後の画面の後始末だけを書く。

```tsx
const historyActions = useChatHistoryActions({
  onRename: (id, title) => api.renameConversation(id, title),   // 前後の空白を除いた 1〜80 文字
  onDelete: (id) => api.deleteConversation(id),
  onBeforeDelete: (item) => { if (item.id === activeId) stopSendingAndClearSelection(); }, // RAG（送信中でも消せる）
  onDeleted: (item) => { if (item.id === activeId) startNewConversation(); },             // Agent・NL2SQL（作成中は 409）
  disabled: busy,
  canRename: (item) => !othersIds.has(item.id),   // 他の利用者の会話（system_admin が見る）は名前を変えられない（#3244）
});
```

| 決めたこと | 理由 |
|---|---|
| `canRename` が false の行は「名前の変更」を出さず（欄も開かない）、「削除」だけを出す。3 製品とも、他の利用者の会話（`owned=false`）の行で false を返す（#3244） | 名前の変更は持ち主だけ（backend も 403）。削除は運用の操作として system_admin もできる |
| 名前の変更は行をその場の `TextField`（`maxLength` 80・`required`・ラベルは隠す）にし、Enter で保存・Esc で取り消し。空の名前は保存せず欄の下に「会話名を入力してください。」、保存の失敗も欄の下に API の文を出して欄へフォーカスを戻す | 一覧の位置を保ったまま直せる。入力の誤りは欄の直下（messaging.md §10） |
| 削除は確認のダイアログ（danger・scrim では閉じない）を通し、結果は Toast。失敗は API の文を Toast | 取り消せない操作（buttons.md §5.1）。会話の一覧は残るので Toast で足りる |
| 文言は `DEFAULT_CHAT_HISTORY_ACTION_LABELS`（RAG の文を正本にした）。確認の説明は「「{title}」を会話の履歴から削除します。削除した会話は開くことも続けることもできません。この操作は取り消せません。」 | 3 製品で同じ機能の文をそろえる（AGENTS.md「言語」）。Agent の Run・NL2SQL のジョブは監査のため残るので、「メッセージをすべて削除」とは書かない |
| 削除の失敗の文は既定で共通の `apiErrorMessage`（RAG は `ApiError.messages` を「 / 」でつなぐ `errorMessage` を渡す） | 製品の `ApiError` の形に合わせる |

- 単体テストは `packages/ui/tests/chat-history-actions.test.tsx`。e2e は RAG `e2e/chat.spec.ts`・Agent `e2e/chat-history-actions.spec.ts`・NL2SQL `tests/e2e/sql-chat-history-actions.spec.ts`（desktop と 375px）。

## useChatAutoScroll — **新規**（#1161）

チャットの会話の欄の自動スクロールと「最新へ」（3 製品共通）。`ChatLayout` の `logRef` と `latest` につなぐ。

```tsx
const autoScroll = useChatAutoScroll({
  contentKey: `${turns.length}:${lastStatus}:${pending?.localId ?? ""}`,  // 内容が変わったことを表す値
  resetKey: `${targetId}:${conversationId}`,                              // 会話が変わったら末尾から
  enabled: active,                                                         // keep-alive で隠れている間は止める
});

function submit() {
  setPending(createOptimisticChatMessage(draft));
  autoScroll.scrollToLatest();          // 送信の瞬間は、上を読んでいても末尾へ（messaging.md §11.1）
}

<ChatLayout
  logRef={autoScroll.logRef}
  latest={{ visible: autoScroll.showLatest, label: t("chat.latest"), onClick: () => autoScroll.scrollToLatest("smooth") }}
  …
/>
```

| 決めたこと | 理由 |
|---|---|
| 末尾（48px 以内）を見ている間だけ、新しい内容に合わせて末尾へ追う | 3 製品とも内容が変わるたびに末尾へ動かしていて、回答の受信中・処理の段階の更新で、上の回答を読んでいる利用者が引き戻されていた |
| 上を読んでいる間に新しい内容が届いたら、会話の欄の下端の中央に「最新のメッセージへ」（`ArrowDown`、secondary・sm）を重ねる。末尾まで戻ると消す | ChatGPT・Claude・Slack と同じ。新しい内容に気付けて、読む位置は利用者が決める |
| ボタンは `role="log"` の外に置く | 会話の読み上げにボタンの文言を混ぜない |
| 会話を開いた・変えた（`resetKey`）・画面に戻ったときと送信の瞬間は、上を読んでいても末尾へ | 新しい会話は最新から読む。送った質問がすぐ見える（messaging.md §11.1） |
| 動かすのは会話の欄の `scrollTo` だけ | 祖先（ページ）を動かさない（README §4「AppShell」） |

- 単体テストは `packages/ui/tests/chat-auto-scroll.test.tsx`。実ブラウザは NL2SQL `tests/e2e/sql-chat.spec.ts` の「#1161」。
- 会話の欄の中の特定の位置へ動かすとき（RAG の `#message-{id}` の回答へ移る）は、`logElementRef` の `scrollTo` を使う。

## ChatTurn / ChatAnswer / ChatPendingTurn — **新規**（#1161）

チャットの 1 往復の入れ物（3 製品共通）。質問の吹き出し（`ChatUserMessage`）と回答の枠を `<article>` にまとめる。回答の中身（処理の段階 `ChatProgress`・本文・引用・SQL・ツール・承認・評価）は製品が子要素で渡す。

```tsx
import { ChatAnswer, ChatPendingTurn, ChatTurn } from "@production-ready/ui";

// 確定した往復
<ChatTurn question={turn.question} testId="sql-chat-turn">
  <ChatAnswer live>                       {/* 回答が後から届く製品は live（polite） */}
    <ChatProgress … />
    <AnswerBody … />
  </ChatAnswer>
</ChatTurn>

// 送った質問（サーバーの応答の前・送れなかったとき。messaging.md §11）
<ChatPendingTurn
  message={pending}                       // createOptimisticChatMessage の仮のメッセージ
  failedLabel={t("chat.sendFailed")}      // 「送信できませんでした」
  progress={<ChatProgress steps={chatSubmitProgressSteps(pending.sentAtMs)} … />}
  failure={<ApiErrorBanner error={sendError} fallback={…} action={<Button icon={RotateCcw}>再送信</Button>} />}
  testId="chat-pending-turn"
/>
```

| 決めたこと | 理由 |
|---|---|
| 1 往復は `<article>`（質問と回答の間 0.5rem）。往復の間は会話の欄（`ChatLayout`）が 1.25rem を空ける | 3 製品で `div` / `article` と間隔が違っていた。往復のまとまりを読み上げでも区切る |
| 回答の枠は `rounded-md`・`border`・`bg-surface`・`p-3`、中は縦に 0.75rem 間隔（`ChatAnswer`） | NL2SQL は `Card`（影・角丸 lg）、RAG・Agent は枠線の箱で、余白も違っていた。会話の欄の中で影を重ねない |
| 送信中は回答の枠に処理の段階だけを出し、送れなかったら枠を出さずに原因と「再送信」を出す（`ChatPendingTurn`） | 送れなかった質問を残し、入力欄に戻さない（messaging.md §11.3）。動くスピナーは回答の場所の 1 つだけ |
| 複数モデルの比較（RAG）は `ChatAnswer` を列にして並べる（`className` で `h-full`） | 比較の列も同じ枠にする |

- 単体テストは `packages/ui/tests/chat-turn.test.tsx`。
- 使う所: RAG `MessageTurn` / `AssistantColumn`、NL2SQL `SqlChatTurn` と送信中の往復、Agent `RunChatTurn` / `PendingTurn`。
- 回答の本文は `ChatAnswerBody`（#3433）で包み、本文の前に見出し「回答」（既定 h3。`labels.heading`・`headingLevel` で変えられる）を出す。見出しは本文の中の節の見出しと同じ部品 `AnswerHeading`（`text-sm font-semibold text-fg`。`AnswerSections` の節の見出しも同じ）。回答の本文を描くときだけ使い、処理の経過だけの間・失敗・停止だけの枠・回答前の確認のカードには使わない。実行の詳細は `Section` の見出し「回答」を持つので使わない。使う所: RAG `AssistantColumn`、Agent `RunChatTurn`（NL2SQL の回答は SQL・説明・結果で、本文の節の見出しを持たないので使わない）。単体テストは `packages/ui/tests/answer-heading.test.tsx`。
- 利用者の側が質問の文ではない往復（回答前の確認のカードで答えた往復。#1839）は、`userContent` に `ClarificationRecord`（`variant="bubble"`）を渡す。質問の吹き出しの代わりに出る。

---

## MarkdownText — **新規**（#1808）

LLM の回答など Markdown の本文を、記号のまま出さずに要素で描く。決めたことと理由は README §4「`MarkdownText`」。

```tsx
import { MarkdownText, ResultTable, splitMarkdownTables } from "@production-ready/ui";

// 表は先に取り出して共通の結果の表で出し、残りを MarkdownText で描く（Agent の AnswerBody）。
{splitMarkdownTables(answer).map((segment, index) =>
  segment.kind === "text" ? (
    <MarkdownText key={index} text={segment.text} headingLevel={3} />
  ) : (
    <ResultTable key={index} columns={segment.data.columns} rows={segment.data.rows} … />
  ),
)}
```

### MarkdownText の props

| prop | 型 | 既定 | 説明 |
|---|---|---|---|
| `text` | `string` | — | Markdown の本文。空なら何も描かない |
| `headingLevel` | `2 \| 3 \| 4 \| 5 \| 6` | `3` | 本文の最上位の見出しの HTML の段。置く場所の見出しの下の段を渡す（カードの h2 の下なら 3、h1 だけの画面なら 2） |
| `labels` | `Partial<MarkdownTextLabels>` | `{ newTab: "（新しいタブで開きます）" }` | 新しいタブで開くリンクの読み上げの補足 |
| `lang` | `string \| null` | 本文から判定 | 外側の `div` の `lang`（#3335。下の「内容の言語」。リンクの読み上げの補足は画面の言語。#3349） |
| `className` / `testId` | `string` | — | 外側の `div`（`data-markdown-text`）に付ける |

- 描くもの: 見出し（`#`〜`######`）・段落（中の改行は `<br>`）・`-` / `*` / `+` / `1.` の箇条書き（字下げで入れ子）・` ``` ` / `~~~` のコードブロック（`data-language`）・`>` の引用・`---` の区切り線、行の中の `` `code` ``・`**強調**`・`*斜体*`・`[文字](https://…)`・`<https://…>`・裸の URL、`\*` の取り消し。
- 描かないもの: 生の HTML（文字のまま）・表（段落の文字のまま。`splitMarkdownTables` で先に取り出す）・画像・`_斜体_`（識別子の `_` を斜体にしない）・`http(s)` / `mailto` 以外のリンク。

---

## ClarificationCard / ClarificationRecord — **新規**（#1839）

回答前の確認のカードと、利用者の答えを会話に残す表示（3 製品共通）。決めたことと理由は README §4「`ClarificationCard` / `ClarificationRecord`」、振る舞い（いつ聞くか・操作の結果・往復の上限・履歴）は UX 契約 [confirm-before-answer.md](../ux-contracts/confirm-before-answer.md)。

```tsx
import {
  ApiErrorBanner,
  ChatAnswer,
  ClarificationCard,
  ClarificationRecord,
  clarificationLabels,
} from "@production-ready/ui";

// NL2SQL は名詞だけを渡す（RAG・Agent は既定の「回答」のまま labels を渡さない）。
const labels = clarificationLabels({ stage: "SQL の生成", action: "SQL を生成" });

<ChatAnswer>
  <ClarificationCard
    variant="plain"                                   // 回答の枠の中なら枠を重ねない
    labels={labels}
    items={[{
      id: item.id,
      prompt: item.question_ja,                       // 確認事項
      reason: item.reason_ja,                         // 確認する理由
      selection: item.answer_kind === "multi_select" ? "multiple" : "single",
      meta: <StatusBadge icon={false} variant="neutral" label="FROM" />,
      options: item.candidates.map((c) => ({
        id: c.id,
        label: c.label_ja,
        description: c.description_ja,
        detail: <span className="break-all font-mono">{c.refs_ja}</span>,   // 製品固有の中身
      })),
    }]}
    progress={{ current: answered + 1, total: answered + unresolved }}   // 1 件ずつ聞くとき
    round={{ current: reviewRound, max: maxReviewRounds }}
    busyAction={busy}                                  // "confirm"（フォームを記録と経過の行に置き換え）| "skip" | null
    error={error ? <ApiErrorBanner error={error} fallback={t("…")} /> : null}
    onConfirm={(answers) => submit(answers)}           // [{ itemId, optionIds, otherText }]
    onSkip={() => skipAndAnswerWithAssumptions()}      // 確認を終える唯一の操作（「確認をやめる」は置かない）
    onSkipWhileConfirming={() => skipWithoutWaiting()} // 任意: 確定の後の経過の行に出す
  >
    <RequirementItemsList controller={controller} />  {/* 製品の中身（任意） */}
  </ClarificationCard>
</ChatAnswer>

// 確定・スキップの後は会話の利用者の側に残す
<ClarificationRecord
  entries={[{ id: "period", prompt: "どの期間の売上を見ますか？", selected: ["今月"], otherText: "" }]}
  outcome="answered"                                   // スキップは "skipped"
  labels={labels}
/>
```

### ClarificationCard の props

| prop | 型 | 既定 | 説明 |
|---|---|---|---|
| `items` | `ClarificationItem[]` | — | 確認事項（ふつうは 1 件）。`{ id, prompt, reason?, selection?: "single" \| "multiple", options: ClarificationOption[], meta?, optional? }`。`optional` は答えなくても確定できる確認事項（NL2SQL の確認のカードの表の選び直し・直しの自由入力） |
| `items[].options[]` | `ClarificationOption` | — | `{ id, label, description?, detail?, disabled? }`。`detail` は製品固有の中身（物理名・根拠・章節）。1 回 8 個までを目安にする |
| `onConfirm` | `(answers: ClarificationAnswer[]) => void` | — | 「この内容で〈回答〉」。`{ itemId, optionIds, otherText }`（自由入力は前後の空白を除く）。すべての確認事項に答えるまで押せない |
| `onSkip` | `() => void` | — | 「確認せずに〈回答〉」。確認を終える唯一の操作で、質問は取り消さない（質問を取り消すのは入力欄の「停止」。#2311）。渡さなければ出さない（3 製品とも渡す） |
| `onSkipWhileConfirming` | `() => void` | — | 確定の後の経過の行に出す「確認せずに〈回答〉」（待たずに進める）。送った答えの処理を取りやめられる製品（NL2SQL の AI の再検査）だけが渡す |
| `dismissAction` | `{ label, onClick }` | — | 入力の取りやめ（NL2SQL の「直すのをやめる」「追加をやめる」。前の確認事項に戻るだけで、質問・確認は取り消さない）。操作の行の右端の ghost。「確認をやめる」には使わない |
| `progress` | `{ current, total }` | — | 1 件ずつ聞くときの位置「確認事項 1 / 3」。無くて 2 件以上なら「確認事項 2 件」 |
| `round` | `{ current, max }` | — | 「確認 1 回目（上限 2 回）」。`current >= max` で「これが最後の確認です。…」を添える |
| `busyAction` | `"confirm" \| "skip" \| null` | `null` | `confirm` はフォーム（選択肢・自由入力・操作の行・押せない理由・`children`）を、答えた内容の記録（`-answered-entry`）と経過の行 1 つ（`ProcessingIndicator`。`-progress`、文言は `labels.confirming`）に置き換える（確定のボタンは `loading` にしない）。`null` に戻すとフォームと入力が戻る。`skip` は押したボタンだけ `loading`、ほかの操作と入力は `disabled` |
| `disabled` | `boolean` | `false` | カード全体を操作できない（古い会話の確認） |
| `value` / `onValueChange` | `ClarificationDraft` | — | 受控の答え（作業状態に残すとき）。`Record<itemId, { optionIds, otherText }>` |
| `defaultValue` | `ClarificationDraft` | `{}` | 非受控の初期の答え。確認事項（`items` の ID）が変わると空に戻る |
| `blockedReason` | `string \| null` | `null` | 製品が決める押せない理由（NL2SQL の未送信の直し）。文があれば確定を押せず、確定の `aria-describedby` にする |
| `children` | `ReactNode` | — | 確認事項の下・操作の行の上に置く製品の中身 |
| `error` | `ReactNode` | — | 操作の失敗（`ApiErrorBanner`）。操作の行の直上 |
| `actions` | `ReactNode` | — | 確定の右に置く製品の操作（NL2SQL の「直しを伝える」。同じ `size` の `Button`）。多用しない |
| `labels` | `Partial<ClarificationLabels>` | `DEFAULT_CLARIFICATION_LABELS` | 文言。ふつうは `clarificationLabels({ stage, action })` を渡す。`skipHint` を空にすると補足を出さない。`confirming` は確定の後の経過の行の文（既定「答えを反映しています」） |
| `size` | `"sm" \| "md" \| "lg"` | `"md"` | ボタンの高さ（チャットの回答の中は md、画面のパネルは lg） |
| `headingLevel` | `2 \| 3 \| 4` | `3` | 題の見出しの段（確認事項の文はその 1 段下） |
| `autoFocus` | `boolean` | `false` | 最初に出たときに最初の確認事項の文（題の 1 段下の見出し）へフォーカスを移す（確認事項が変わったときは常に移す） |
| `variant` | `"card" \| "plain"` | `"card"` | `plain` は枠なし（回答の枠の中） |
| `idPrefix` / `className` / `testId` | `string` | `testId="clarification-card"` | `testId` から `-confirm` / `-skip` / `-dismiss` / `-confirming` / `-answered-entry`（`data-entry-id`）/ `-progress` / `-blocked` / `-meta` / `-last-round` / `-item`（`data-item-id`）/ `-option`（`data-option-id`）/ `-other` を作る |

- helper: `clarificationAnswers(items, draft)`（確定した答えを組む）、`isClarificationEntryAnswered(entry)`。

### ClarificationRecord の props

| prop | 型 | 既定 | 説明 |
|---|---|---|---|
| `entries` | `ClarificationRecordEntry[]` | — | `{ id, prompt, selected: string[], otherText? }`（選んだ選択肢の名前と自由入力） |
| `outcome` | `"answered" \| "skipped"` | `"answered"` | `skipped` は「確認せずに回答しました（前提を示して回答）」と確認事項だけを出す |
| `variant` | `"bubble" \| "disclosure"` | `"bubble"` | `bubble` は会話の利用者の側の吹き出し、`disclosure` は回答の枠の中の折りたたみ |
| `defaultOpen` | `boolean` | `false` | `disclosure` の初期の開閉 |
| `labels` | `Partial<ClarificationLabels>` | `DEFAULT_CLARIFICATION_LABELS` | 題「回答前に確認した内容」・「その他: …」・「（答えなし）」 |
| `className` / `testId` | `string` | `testId="clarification-record"` | 外側に `data-outcome` を付ける。`-entry`（`data-entry-id`）/ `-skipped` |

- helper: `clarificationRecordAnswerText(entry, labels)`（「東日本、西日本、その他: 沖縄」）。
- 単体テストは `packages/ui/tests/clarification-card.test.tsx`。

---

## RunHistoryTable / RunHistoryToolbar / RunStatusBadge / RunSummary / RunTimeline / RunDetailLayout — **新規**（#1955）

3 製品の「実行履歴」の一覧と詳細。決めたことと理由は README §4「実行履歴」、画面の規則は UX 契約 [run-history.md](../ux-contracts/run-history.md)。製品はデータの取得・行の写し（`toItem`）・状態の写し・i18n・操作の定義と、節の中身（回答・根拠・SQL・ツール）だけを持つ。

```tsx
import {
  OffsetPagination, RunDetailLayout, RunDetailSection, RunHistoryTable, RunHistoryToolbar,
  RunSummary, RunTimeline, SearchField, runPeriodSince,
} from "@production-ready/ui";

// 一覧
<RunHistoryToolbar
  search={<SearchField … />}
  searchActive={query !== ""}
  status={{ value: status, options: ["succeeded", "blocked", "failed"], onChange: setStatus }}
  kind={{ value: kind, options: kindOptions, onChange: setKind }}       // 先頭が「すべて」
  period={{ value: period, onChange: setPeriod }}                         // backend へは runPeriodSince(period)
  executor={canSeeOthers ? { value: executor, onChange: setExecutor } : undefined}
  onClearFilters={clearAll}
  count={{ total, matched }}
/>
<RunHistoryTable rows={rows} toItem={toRunItem} onOpen={openRun} showExecutor={canSeeOthers}
  actionsFor={rowActions} selectedRunId={lastOpenedId} labels={{ columns: { title: t("…") } }} />
<OffsetPagination … />

// 詳細
<RunDetailLayout
  summary={<RunSummary status="failed" question={run.question} startedAt={run.started_at} runId={run.id} … />}
  answer={<RunDetailSection title="回答">…</RunDetailSection>}
  timeline={
    <RunDetailSection title="処理の経過">
      <RunTimeline steps={steps} />
      <RunProcessDetails title="処理の詳細" summaryTestId="…">…</RunProcessDetails>
    </RunDetailSection>
  }
  evidence={<RunDetailSection title="SQL と結果">…</RunDetailSection>}
  technical={<RunDetailSection title="監査ログ" collapsible>…</RunDetailSection>}
/>
```

| 部品 | 主な props | 説明 |
|---|---|---|
| `RunStatusBadge` | `status: RunStatus`・`label?`・`labels?` | 7 つの状態（`RUN_STATUSES`）。`data-run-status`。`isRunActive` は待機中・実行中・応答待ち |
| `RunHistoryTable<T>` | `rows`・`toItem(row) → RunHistoryItem`・`onOpen`・`showKind`（既定 true）・`showExecutor`・`showUsage`・`extraColumns`（2 つまで）・`actionsFor`・`selectedRunId`・`labels`・`loading`・`empty` | `RunHistoryItem` は `id`・`title`・`target?`・`href`・`status`・`statusLabel?`・`kind?`・`executor?`・`startedAt`・`durationMs?`・`usage?`（`tokens`・`note`〔計測の状態の注記。#3466〕・`cost`） |
| `RunHistoryToolbar` | `search`・`searchActive`・`status`（`options` は製品が写せる状態）・`kind`・`period`・`executor`・`onClearFilters`・`count`・`labels`・`idPrefix` | 無い絞り込みは飛ばす。3 つ以上で 375px はシート |
| `RunSummary` | `status`・`question`・`startedAt`・`finishedAt?`・`durationMs?`・`kind?`・`executor?`・`owner?`・`target?`・`model?`・`usage?`・`runId`・`extraFacts?`・`badges?`・`actions?`・`alert?` | 概要のカード（`data-run-summary`） |
| `RunDetailSection` | `title`・`description?`・`meta?`・`actions?`・`collapsible?`・`open?` / `onOpenChange?` | 節のカード。`collapsible` は既定で閉じた `Disclosure`（開いたときだけ中身を読む節は `open` / `onOpenChange` で受控にする。Agent の監査ログ） |
| `RunTimeline` | `steps: RunTimelineStep[]`・`totalDurationMs?`・`defaultExpanded?`・`showSummary?`（既定 true）・`showBars?`（既定 true）・`spinRunning?`（既定 true。false は実行中の段を回さない印 `CirclePlay` で出す。#2840）・`size?`（`sm` / `md`）・`formatDuration?`・`stepTestIdPrefix?` | 段は `id`・`label`・`status`・`startedAt?`・`finishedAt?`・`durationMs?`・`detail?`（名前の後ろの括弧）・`meta?`・`details?`（`RunStepDetails`: `fields`〔見出しと値・`mono`〕・`lines`・`code`〔`label`・`language`（`json` / `sql` / `text`）・`text`〕・`truncated`。`RunStepDetailsView` で描く。#2294）・`children?`（`details` の下に出す自由な中身）・`level?`（0 以上。字下げは 1 段 1rem で実際の深さを保持。6 段より深い木は名前と時間の幅を確保し、一覧内だけで横スクロールする。フォーカス可能な region に一覧名を付け、ページは横にはみ出さない。`parentId?` は記録の親 ID を保持し、親不在でも隣接行から推測しない。#3790）・`product?`（別の製品の境目の行の製品名。名前の後ろの枠つきのタグ。#3272 / #3447）。線の位置は `runTimelineBars`（時刻の無い段は所要時間を積み、入れ子の段は親の段の始まりから積む）。チャットの `ChatProgress` も一覧をこの部品で描く（#2265）。詳細か `children` のある段だけに開閉のボタン（印の `DisclosureChevron` は行の文字の大きさ: `md` 14px / `sm` 12px〔`RUN_TIMELINE_CHEVRON_SIZE`。#2338〕、`stepTestIdPrefix` があれば `<…>-step-<id>-toggle` と `<…>-step-<id>-details`。属性 `data-run-timeline-toggle`）。コードの箱は高さ 16rem で中をスクロールし、focus を受ける |
| `RunProcessDetails` | `title?`（既定「処理の詳細」）・`description?`・`defaultOpen?`・`testId?`（`<details>`）・`summaryTestId?` | 処理の経過に属する技術的な詳細（回答の記録の詳細・識別子・生成した SQL・イベントの記録）を、処理の経過のタイムラインの後ろに既定で閉じて置く `Disclosure` の plain・sm（`data-run-process-details`。#2325）。実行の詳細の「処理の経過」の節と、チャット・RAG 検索の `ChatProgress` の `footer` で同じ部品・名前・位置にする |
| `RunDetailLayout` | `summary`・`answer?`・`timeline?`・`evidence?`・`feedback?`・`technical?` | 節の順を固定（`data-run-detail`）。`technical` は処理の経過に属さない技術的な節（Agent の監査ログ）だけ。読み込み中は `RunDetailSkeleton` |

- 書式: `formatRunDateTime(iso, { seconds })`・`formatRunDuration(ms)`・`formatRunTokens(n)`。モデルの利用量（#3419・#3543）: `runUsageView(usage, { fallbackModel, labels, sourceLabel })` が詳細の概要の `model`・`usage`・「モデルの呼び出し」・「未計測の呼び出し」を、`runListUsage(totalTokens, measurement, labels)` が一覧の `RunHistoryItem.usage` を返す（記録なし・計測できない呼び出しだけは「未計測」、一部は「一部計測」、LLM を呼ばなかった実行は実測の 0。形は `RunModelUsageRecord`、文言は `DEFAULT_RUN_USAGE_LABELS`）。期間: `RUN_PERIODS`・`DEFAULT_RUN_PERIOD`（既定の過去 7 日。初期値・「条件をクリア」・既定と違う条件の判定に使う。#3386）・`runPeriodSince(period)`。期間だけで絞って 0 件でそれより前の実行があるときは `RunHistoryPeriodEmptyState`（`period`・`onShowAll`・`labels`・`testId`）。実行者: `RUN_EXECUTOR_FILTERS`。
- 単体テストは `packages/ui/tests/run-history.test.tsx`。

---

## RunEventLog — **新規**（#2386）

実行のイベントの記録（モデルが呼んだツール・承認の判断・成果物の保存など）を、時間の順に 1 イベント = 1 行で並べる。実行の詳細の「処理の詳細」（`RunProcessDetails`）の中に置く。製品はイベントを `RunEventLogItem` に写すだけで、描き方は部品が持つ。

| prop | 型 | 既定 | 説明 |
|---|---|---|---|
| `items` | `RunEventLogItem[]` | — | `id`・`title`（要約）・`summary?`（淡い補足）・`at?`（ISO）・`category?`（`{ id, label }`。絞り込み）・`icon?`・`status?`（`{ variant, label }`）・`durationMs?`・`details?`（`RunStepDetails`）・`children?`（警告など）・`defaultOpen?` |
| `labels` | `Partial<RunEventLogLabels>` | 日本語の既定 | `title`（領域・一覧の読み上げ名）・`filter`・`all`・`first`・`latest`・`empty`・`count`（`ListCountLabels`）・`detailsTruncated` |
| `formatDateTime` | `(iso) => string` | `formatRunDateTime(iso, { seconds: true })` | 時刻の `title` の表記 |
| `testId` | `string` | — | 行 `<testId>-event-<id>`・開閉 `…-toggle`・詳細 `…-details`・スクロール領域 `<testId>-scroll`（`data-overflowing`）・件数 `-count`・絞り込み `-filter`・`-first` / `-latest` |

| 決定 | 理由 |
|---|---|
| 行は「開閉の印・種類のアイコン・時刻（時:分:秒）・要約・状態のバッジ・処理時間」だけ。種類のコード・ID・payload・警告は行を開く（`button[aria-expanded][aria-controls]`。Enter / Space） | 監査・イベントのタイムライン（GitHub Actions・Datadog / Sentry・Temporal）と同じく、時刻・種類・状態を縦に読み比べる。開く行は要約を 375px で 2 行・36rem 以上で 1 行に切り詰め、全文は開いた詳細で読む |
| 一覧は `INFORMATION_LIST_SCROLL_CLASS`（md 未満 17.5rem・md 以上 28rem）の `role="region"` の中で縦にスクロールし、あふれたときだけ `tabIndex=0` | 件数でページを伸ばさない（表ではない行リストの標準）。あふれないときに空の Tab 止まりを作らない |
| 種類が 2 つ以上なら件数つきの絞り込み（4 つまで `FilterChipGroup`・それ以上 `FilterSelect`）、件数は `ListCount`（「全 N 件」/「N 件（全 M 件）」。読み上げは止める）、あふれたときだけ「最初へ」「最新へ」（端では無効） | 長い記録の中の目的のイベントにすぐ届く。実行中の追加のたびに件数を読み上げない |
| 末尾を見ている間に増えたイベントは末尾に付いて行き、途中を読んでいる間は動かさない | ログの追尾（tail）。読んでいる位置を奪わない |
| 失敗・警告のイベントは `defaultOpen` で最初から開く | `RunTimeline` の失敗した段と同じ |

## 回答の根拠と本文の節: AnswerEvidence / EvidenceList / EvidenceCard / AnswerSections — **新規**（#2328。RAG の `AnswerCitations` / `CitationCard` / `AnswerText` から移した）

3 製品のチャット・RAG 検索・実行の詳細で、回答の根拠と回答の本文の節を**同じ部品**で出す（見た目が似た製品ごとの写しを作らない）。製品は自分の根拠の形を下のデータの契約に写して渡すだけで、文言は `evidenceLabels()`（3 製品で同じ書き方。上書きは `evidenceLabels({ … })`）。

```tsx
import { AnswerEvidence, AnswerSections, evidenceLabels, type EvidenceListItem } from "@production-ready/ui";

// 回答の本文の下・回答の評価の前（チャット・実行の詳細で同じ位置）
<AnswerEvidence
  items={items}                       // EvidenceListItem[]
  idPrefix={`message-${id}`}          // 開くボタンの test id（`<idPrefix>-source-<番号>`）
  labels={evidenceLabels()}
  onOpenSource={source.open}          // 渡すと各カードに「根拠を表示」（部品が描く。3 製品で同じ名前）
  renderActions={(item, index) => …}  // 操作の行の右の組（根拠の評価）
/>

// 回答の本文: 要約の段落 →「資料により確認できた事項」（各項目の下に「根拠：文書 p.N」）→「資料から確認できなかった事項」
<AnswerSections blocks={[
  { kind: "paragraph", key: "p", content: "…" },
  { kind: "section", key: "s", title: "資料により確認できた事項",
    entries: [{ key: "e", content: "…", citations: [{ key: "c", label: "根拠：文書 A.pdf p.3", ariaLabel: "…", onOpen }] }] },
  { kind: "content", key: "t", content: <表など>, citations: [...] },
]} />
```

| 部品 | 主な props | 説明 |
|---|---|---|
| `AnswerEvidence` | `items`・`idPrefix?`・`labels?`・`onOpenSource?`・`renderActions?`・`renderExtra?`・`listTestId?`・`open?` / `onOpenChange?`・`className?`・`testId?`（既定 `answer-citations`） | 見出し「AI に渡した N 件 ／ 回答で引用 M 件 ／ うち該当確認済み X 件 ／ 参考 Y 件」の plain の `Disclosure`（**既定は閉じる**）。中身は `EvidenceList`。根拠が 0 件なら何も出さない。まとまりの表示・見方の切り替え（タブ）は持たない（#2388。文書名と節はカードのチップにある） |
| `EvidenceList` | `items`・`labels?`・`onOpenSource?`・`renderActions?`・`renderExtra?`・`idPrefix?`・`testId?` | カードの一覧。`bounded-scroll-area-lg`（**一定の高さの中で縦にスクロール**し、回答の評価を押し下げない）。`onOpenSource` があれば各カードの操作の行の左の組に `EvidenceSourceButton`、`renderActions` は右の組。回答を作らない検索の結果（RAG 検索・実行の詳細）は、画面の見出し「検索結果（N）」の下にこれだけを置く |
| `EvidenceSourceButton` | `fileName`・`onOpen`・`labels?`・`data-testid?` | 根拠を右のサイドパネルで開くボタン「根拠を表示」（`secondary`・`LocateFixed`・`aria-haspopup="dialog"`・読み上げ「〈文書名〉の根拠を表示」。3 製品で同じ。#2388）。一覧の外の 1 件のカード（RAG の `CitationCard`）で使う |
| `EvidenceCard` | `item`・`index`・`labels?`・`leadingActions?`・`actions?`・`children?` | カード: 番号・文書名・旧版・引用の印（`EvidenceAnswerUseBadge`）・経路のバッジ・本文 3 行・場所のチップ（`dl`）・補足のタグ・原典のリンク・スコアの面（取得スコアと Rerank のメーター）・操作の行（左の組 `leadingActions` は「根拠を表示」、右の組 `actions` は根拠の評価など。どちらも無ければ区切り線の行を出さない） |
| `EvidenceSourceSheet` | `open`・`onClose`・`item: EvidenceListItem \| null`・`index`・`labels?`・`kind?`（`answer` 既定 / `search`）・`children?`（原文のプレビュー）・`returnFocusRef?`・`data-testid?`（既定 `evidence-source-sheet`） | 根拠 1 件を**右のサイドパネル**で開く（#2360）。枠は `SideSheet`（右・`wide`。sm 未満は全画面。×・Escape・scrim で閉じ、開いた操作へフォーカスを戻す）。見出しは「回答の根拠」（`kind="search"`。回答を作らない検索の結果の箇所は「検索で見つかった箇所」）。上部の要約（`evidence-source-summary`）はカードと同じ描画（番号・文書名・引用の印・経路・抜粋・チップ・スコア。操作の行は持たない。#2462）、下に `children`（`evidence-source-preview`。パネルの残りの高さ、最小 24rem / sm 以上 32rem）。`children` があれば抜粋の全文と頁・章節のチップを「抜粋と頁・章節」の `Disclosure`（plain・sm。`evidence-excerpt-details`）に入れて既定で閉じ（開くたび・根拠を替えるたびに閉じた状態から。#2502）、無ければ全文。本文の根拠の行と、一覧の「根拠を表示」は、どちらもこのパネルを開く（`children` は RAG・Agent とも共通の `DocumentPreview`。#2389） |
| `useEvidenceSource<T>()` | — | パネルで開く根拠の状態（`selected`・`shown`・`mounted`・`open(value)`・`close()`）。`shown` は閉じた後も前の根拠を覚え（滑って消える間も中身を出す）、`mounted` は一度開くまで false（閉じた後もパネルを描いたままにし、フォーカスを戻す） |
| `EvidenceAnswerUseBadge` | `use`・`labels?` | 該当確認済み =「回答で引用」（success）、参考 =「参考」（neutral・`BookOpen`）、使われ方が不明 =「回答で引用」（info） |
| `evidenceCountLabel` | `(items, labels?)` | 見出しの文。引用したかが分からない（`usedInAnswer: null`。検索だけ）なら件数だけ、0 件の項目は出さない |
| `AnswerSections` / `AnswerCitationLinks` | `blocks`・`live?`・`children?`（シート・ダイアログ）・`testId?`（既定 `answer-text`）・`passages?`・`passageLabels?` | 段落（`paragraph`）・段落にできない塊（`content`。表など）・見出しつきの箇条書き（`section`。`muted` の項目は印なしの補足）と、各段落・項目の下の根拠の行（`onOpen` があれば強調色の押せる行、無ければ文字だけ）。`passages`（#2738）を渡すと、段落・項目の原文（`passageText`。無ければ文字列の `content`）と印の `text` を照合して印を付け、本文の上に回答全体の注意、後ろに「根拠と矛盾し除いた内容」「確認が必要な点」の節を足す |
| `parseAnswerPassageRecords` / `matchAnswerPassageMarks` / `answerPassageCounts` | `(source)` / `(passages, marks)` / `(records)` | 回答の記録の `passage_marks`・`removed_passages`・`answer_warnings` を読む（3 つとも無い過去の記録は null）。照合は印の原文と同じ段落 → 含む段落 → 含まれる段落の順（箇条書きの印・強調・空白・「根拠：」の行を除いて比べる。契約は `platform/contracts/answer-passages/passage-marks.json`） |
| `AnswerPassageMarked` / `AnswerPassageWarnings` / `AnswerPassageBadges` | `group`・`anchorId`・`render(marker)` / `warnings` / `records`・`labels?`・`testId?` | 印の段落（左の線・末尾の印・開く理由）、回答全体の注意（warning の `Banner`）、印の数のバッジ（0 件は出さない）。文言は `AnswerPassageLabels`（製品が i18n で渡す。RAG `answerPassageLabels()`・Agent `answerPassageLabels()`） |
| `MarkdownInlineText` | `text`・`labels?` | 段落・項目の中の太字・斜体・コード・リンクだけを描く（`<p>` / `<li>` の中に置く） |

**データの契約（`EvidenceListItem`）**: `key`・`fileName`・`text`・`usedInAnswer: boolean | null`・`answerUse: "confirmed" | "reference" | null`・`superseded?`・`routeBadges?`（翻訳済み）・`score: number | null`・`rerankScore: number | null`・`chips?: { id, label, value }[]`（`label` は読み上げ）・`tags?: { id, value, icon?, tone? }[]`・`sourceUrl?`（https）。

- RAG: `RetrievedChunk` → `citationEvidenceItem`（`rag/frontend/src/components/search/citation-evidence.ts`）。根拠のパネルは `CitationSourceSheet`（`EvidenceSourceSheet` に原文のプレビュー `DocumentPreview` を渡す）。
- Agent: 成果物 `answer_evidence`（backend の `answer_evidence.py`）→ `answerEvidenceData`（`agent/frontend/src/lib/answer-evidence.ts`）。根拠のパネルは `EvidenceSourceSheet` に RAG と同じ `DocumentPreview`（`RagSourcePreview`）を渡す。原文は RAG の画面用の Cookie の API ではなく、Agent の backend（`GET /api/runs/{run_id}/source-preview`）が閲覧者として RAG に作らせた短命の署名つきの URL で読む（#2389）。本文の節は `structureAnswer`（`answer-structure.ts`。本文の文は書き換えず、段落ごとの根拠・根拠の ID の印・最終の検証の節から組み立てる。結び付けられなければ Markdown のまま）。
- 単体テストは `packages/ui/tests/answer-evidence.test.tsx`・`answer-sections.test.tsx`・`answer-passage-marks.test.tsx`（段落の印の契約の事例を読む）・`evidence-figure.test.tsx`。

---

## 原文のプレビュー: DocumentPreview / PreviewViewer / evidencePreviewFocus — **新規**（#2389。RAG の `DocumentPreview` / `PreviewViewer` / `lib/bbox.ts` / `lib/preview-viewer.ts` から移した）

RAG の文書の詳細・根拠のパネルと、Agent の根拠のパネルで、原文のプレビュー（ページの画像・ページ送り・拡大縮小・幅に合わせる・全体表示・回転・強調の位置へ移動・「p.N の M か所を強調しています」・bbox の強調）を**同じ部品**で出す。部品は製品の API を知らず、データの取り方は props で渡す。

```tsx
import { DocumentPreview, documentPreviewLabels, evidencePreviewFocus } from "@production-ready/ui";

const focus = evidencePreviewFocus(chunkMetadata);   // 最初のページ・bbox・要素ごとの強調（RAG と Agent で同じ計算）
<DocumentPreview
  kind="pdf"                                          // pdf | image | text | unsupported（製品が決める）
  fileName={fileName}
  pages={{ status: "ready", pages: [{ pageNumber: 1, points: { width, height }, imageUrl: (dpi) => url }] }}
  iframeUrl={pdfUrl}                                  // ページの画像を描けないとき（pages の status が fallback）
  imageUrl={imageUrl}                                 // kind=image
  loadText={(signal) => fetchText(url, signal)}       // kind=text（失敗は throw → 理由と再試行）
  textKey={url}
  downloadUrl={downloadUrl}                           // kind=unsupported のダウンロード（無ければボタンなし）
  unsupportedMessage={message}                        // 製品だけの案内（Agent の根拠のダウンロードの案内など）
  {...focus}
  labels={documentPreviewLabels({ retry: t("common.retry") })}
  sizing="fill"                                       // fill（根拠のパネル）/ page（文書の詳細。1 ページ全体が入る高さ）
/>
```

| 部品 | 主な props | 説明 |
|---|---|---|
| `DocumentPreview` | `kind`・`fileName`・`pages?`（`loading` / `ready` / `fallback` / `error`〔`error`・`onRetry`〕）・`iframeUrl?`・`imageUrl?`・`loadText?` / `textKey?`・`downloadUrl?`・`unsupportedMessage?`・`focus*`・`highlights?`・`sizing?`・`labels?` | 読み込み中は `TimedLoadingState` と内容の形の `Skeleton`（`preview-loading`）、理由のある失敗は `ApiErrorState`（理由・詳細・再試行）、PDF・画像は `PreviewViewer`、本文は中でスクロールする `pre`、表示できない形式は案内とダウンロード |
| `PreviewViewer` | `pages`・`fileName`・`kind`（`pdf` / `image`）・`focusPage?`・`highlights?`・`sizing?`・`labels?` | ツールバー（ページ送り・縮小 / 拡大・横幅に合わせる・全体を表示・回転・強調した位置へ移動）、強調の状態の行、Ctrl + ホイール・キーボード・ドラッグのパン。ページの画像は `referrerPolicy="no-referrer"` で読む（署名つきの URL） |
| `evidencePreviewFocus` | `(metadata)` | 根拠の metadata（頁・bbox・座標の解釈・要素の表示領域）から `focusPage` / `focusBbox` / `focusBboxMode` / `focusBboxUnit` / `focusPageSize` / `highlights` を作る |
| `documentPreviewLabels` | `(overrides?)` | 文言の既定（3 製品で同じ書き方） |
| bbox の計算（`bboxFromMetadata` ほか）・`previewLayout` ほか | — | 旧 RAG の `lib/bbox.ts` / `lib/preview-viewer.ts`（RAG の文書の詳細の強調の計算も使う） |

- RAG: `rag/frontend/src/components/documents/DocumentPreview.tsx` が RAG の文書の API（原本・処理後のファイル・preview-pages・ページの画像・本文の文字コードの判定）をつなぐ。根拠のパネル（`CitationSourceSheet`）と文書の詳細（`DocumentWorkspace`）が使う。
- Agent: `agent/frontend/src/components/evidence/RagSourcePreview.tsx` が `GET /api/runs/{run_id}/source-preview`（RAG の `rag_read_source` の `include_preview` で、閲覧者に縛った 5 分の署名つきの URL）をつなぎ、開いている間は期限の前に取り直す。
- 単体テストは `packages/ui/tests/document-preview.test.tsx`・`bbox.test.ts`・`bbox-highlights.test.ts`・`preview-viewer.test.ts`。

---

## 回答の中の SQL: ChatSqlBlock / SqlSafetyBadge — **新規**（#2346。NL2SQL の `ChatScaffold` の `ChatSqlBlock` から移した）

NL2SQL のチャット・DB 管理チャットと、Agent のチャット・実行の詳細の業務データの照会（NL2SQL の MCP）の結果で、生成した SQL を同じ部品で出す。結果の表は共通の `ResultTable`（旧名 `ChatResultTable`。要約・すべての行・CSV）。並びは NL2SQL のチャットと同じく SQL の塊 → 結果の表。

| 部品 | 主な props | 説明 |
|---|---|---|
| `ChatSqlBlock` | `sql`・`badges`（左の状態）・`actions?`（コピーの右。吹き出しの中なので secondary の sm）・`labels?`・`onCopied?` / `onCopyError?`・`testId?`（`pre` に付く） | 上の行に状態のバッジ・「SQL をコピー」・操作、下に SQL の本文（枠の中で横スクロール） |
| `SqlSafetyBadge` | `safe`・`executed`・`labels?` | 「安全検査済み・未実行」「安全検査済み・実行済み」（success）・「安全検査でブロック」（danger）。アイコンつき |
| `sqlBlockLabels(overrides?)` | — | 3 製品で同じ文言（NL2SQL は辞書の値を渡す） |

- Agent: `lib/run-tables.ts` の `toolResultSql`（`executable_sql` を優先し、無ければ `generated_sql`。`is_safe`・`status`）。
- 単体テストは `packages/ui/tests/chat-sql-block.test.tsx`。

---

## 内容の言語: resolveContentLang / detectContentLang / normalizeContentLang — **新規**（#3335）

利用者の内容（回答・根拠の抜粋・質問・文書のプレビューの本文）の要素に内容の言語の `lang` を付け、共有の tokens（`tokens/fonts.css`）の `:lang()` で漢字の字形を切り替える（`zh-Hans` は Noto Sans SC、`zh-Hant` は TC、`zh-HK` は HK を先頭にした並び。画面の文言は `<html lang="ja">` のまま）。決めたことと理由は README §2「内容の言語の字形」。

| 部品・関数 | `lang` の渡し方 | 付ける要素 |
|---|---|---|
| `AnswerSections` | `lang?`（無ければ段落・項目の `passageText` / 文字列の `content` から判定）。段落・項目の `lang?`（`null` は製品の決まった文言。#3349） | 段落・項目の文・表やコードの塊・引用だけ。節の見出しは見出しの文字から判定。入れ物・根拠の行・ボタン・段落の印・`extra` には付けない（#3349） |
| `AnswerPlainText`（新規） | `lang?`（無ければ `text` から） | 節に分けない回答（生成中・書式に合わない本文）の `p`。`children`（カーソル）を後ろに置くときは本文だけを包み、`children` は外（#3349） |
| `MarkdownText` | `lang?` | 外側の `div`（リンクの読み上げの補足は `uiTextLang()`） |
| `ExcerptContent` | `lang?` | 段落・表の題・セル（表の読み上げの名前 `tableLabel` は外。#3349） |
| `EvidenceCard` などの根拠の一覧 | `EvidenceListItem.lang?` | 抜粋（`citation-text`） |
| `ChatUserMessage` / `ChatTurn` | `lang?` / `questionLang?`（無ければ文字列の質問から） | 質問の吹き出し |
| `DocumentPreview`（`kind="text"`） | `textLang?` | 本文の `pre` の中の本文（`preview-text-body`。領域の読み上げの名前は外。#3349） |
| `uiTextLang()` | — | 画面の文言の言語（`<html lang>`、無ければ `ja`）。内容の要素の中に置くしかない部品の文言にだけ付ける（#3349） |
| `resolveContentLang(lang, text)` | 値（BCP 47）ならそれ（`normalizeContentLang` でそろえる）、`null` なら付けない、`undefined` なら `detectContentLang(text)` | 製品が内容を自前の要素で出すとき |

- 取込などで言語が分かっていれば値を渡す（`zh-CN` → `zh-Hans`、`zh-TW` → `zh-Hant`、`zh-HK` / `zh-Hant-HK` → `zh-HK`、`ja-JP` → `ja`）。今は 3 製品とも取込で言語を持たないので、本文からの判定で付く。
- `detectContentLang` は決められなければ `undefined`（`lang` を付けない）。`zh-HK` は判定しない。
- 単体テストは `packages/ui/tests/content-lang.test.tsx`。e2e は `font-checks.ts` の `renderedLangFonts`（3 製品の書体の spec）と、RAG の `chat.spec.ts` の中国語の内容の会話（Chromium が実際に使った書体を CDP で確かめる）。


### 文書プレビューの現在ページの通知（#3610）

`DocumentPreview` / `PreviewViewer` は任意の `onPageChange(page: number)` を受けます。同じページへの再移動には `focusRequestKey` を変えて通知します。ページ画像の初期表示・ページ送り・出典への移動で、表示中の実ページ番号を通知します。本文だけの表示や iframe の内部ページは通知しません。呼び出し側は表示の絞り込みに使い、出典の強調の focusPage とは別に保持します。

抽出項目の定義のように複数の欄を横に並べる表形式の編集行は、通常の単一入力フォームの FormGrid の最大幅で止めず全幅を使用します（#3608）。名前・型の幅の段は維持し、説明に残りの幅を割り当てます。


`SelectField` の `wrapOptions`（既定 false）は、展開した選択肢のラベルを省略せず折り返します。番号例などが判断に必要な長い選択肢で使います。欄の幅は従来の width の段で指定します（#3613）。

### `ViewportScrollRegion`

カードの主一覧を標準の 5 / 8 行相当の上限で縦スクロールにする。desktop では主一覧の `DataTable.growToViewport` と同様に、スクロール位置を差し引いた表示領域の下余白まで上限を伸ばす。小さな画面では標準密度を維持する。`RunHistoryTable.growToViewport` は同じ指定を内側の `DataTable` に渡す。

### TraceExplorer

`RunTimeline` の「軌跡を調べる」から開く、3 製品共通の呼び出し調査。時間の概観・検索・種類/状態・階層/開始時刻順・選択連動の詳細を提供する。広い置き場所は 2 列、狭い場所は一覧/詳細を切り替える。`RunTimelineStep.operationName` に記録された操作名を渡す。文言は `explorerLabels`（`DEFAULT_TRACE_EXPLORER_LABELS`）で上書きできる。振る舞いの正本は [run-history.md](../ux-contracts/run-history.md) の「軌跡の調査」。
