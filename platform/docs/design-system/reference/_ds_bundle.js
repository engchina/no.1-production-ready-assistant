/* @ds-bundle: {"format":4,"namespace":"ProductionReadyPlatformDesignSystem_6437ce","components":[{"name":"TONE_ICON","sourcePath":"components/core/Banner.jsx"},{"name":"Banner","sourcePath":"components/core/Banner.jsx"},{"name":"Button","sourcePath":"components/core/Button.jsx"},{"name":"Card","sourcePath":"components/core/Card.jsx"},{"name":"CardHeader","sourcePath":"components/core/Card.jsx"},{"name":"CardTitle","sourcePath":"components/core/Card.jsx"},{"name":"CardDescription","sourcePath":"components/core/Card.jsx"},{"name":"CardContent","sourcePath":"components/core/Card.jsx"},{"name":"Icon","sourcePath":"components/core/Icon.jsx"},{"name":"Skeleton","sourcePath":"components/core/Skeleton.jsx"},{"name":"Spinner","sourcePath":"components/core/Spinner.jsx"},{"name":"StatusBadge","sourcePath":"components/core/StatusBadge.jsx"},{"name":"Switch","sourcePath":"components/core/Switch.jsx"},{"name":"ToggleChip","sourcePath":"components/core/ToggleChip.jsx"},{"name":"DataTable","sourcePath":"components/data/DataTable.jsx"},{"name":"Pagination","sourcePath":"components/data/Pagination.jsx"},{"name":"ConfirmDialog","sourcePath":"components/feedback/ConfirmDialog.jsx"},{"name":"LoadingState","sourcePath":"components/feedback/StateViews.jsx"},{"name":"ErrorState","sourcePath":"components/feedback/StateViews.jsx"},{"name":"EmptyState","sourcePath":"components/feedback/StateViews.jsx"},{"name":"StateViews","sourcePath":"components/feedback/StateViews.jsx"},{"name":"ToastRegion","sourcePath":"components/feedback/Toast.jsx"},{"name":"Toast","sourcePath":"components/feedback/Toast.jsx"},{"name":"FieldError","sourcePath":"components/forms/FieldError.jsx"},{"name":"FormStatus","sourcePath":"components/forms/FieldError.jsx"},{"name":"SelectField","sourcePath":"components/forms/SelectField.jsx"},{"name":"TextField","sourcePath":"components/forms/TextField.jsx"},{"name":"AppShell","sourcePath":"components/layout/AppShell.jsx"},{"name":"PageBody","sourcePath":"components/layout/PageBody.jsx"},{"name":"Section","sourcePath":"components/layout/PageBody.jsx"},{"name":"Breadcrumbs","sourcePath":"components/layout/PageHeader.jsx"},{"name":"PageHeader","sourcePath":"components/layout/PageHeader.jsx"},{"name":"Sidebar","sourcePath":"components/layout/Sidebar.jsx"},{"name":"SidebarAccountFooter","sourcePath":"components/layout/Sidebar.jsx"},{"name":"Tabs","sourcePath":"components/layout/Tabs.jsx"},{"name":"TabPanel","sourcePath":"components/layout/Tabs.jsx"}],"sourceHashes":{"components/core/Banner.jsx":"23293b992002","components/core/Button.jsx":"7b0f77f778d1","components/core/Card.jsx":"62cc55356f9c","components/core/Icon.jsx":"956806fc405a","components/core/Skeleton.jsx":"3f5cfd86c087","components/core/Spinner.jsx":"fe075ebc6868","components/core/StatusBadge.jsx":"b6f2c31c6e19","components/core/Switch.jsx":"540816b25abe","components/core/ToggleChip.jsx":"513156466418","components/data/DataTable.jsx":"c21557bb7720","components/data/Pagination.jsx":"2437c5c4da0c","components/feedback/ConfirmDialog.jsx":"06c3a8b5676d","components/feedback/StateViews.jsx":"cc97eb1b0a19","components/feedback/Toast.jsx":"b0d3e2b64b02","components/forms/FieldError.jsx":"05d03274c06c","components/forms/SelectField.jsx":"8d6d1d2b4367","components/forms/TextField.jsx":"6df73807c3dd","components/layout/AppShell.jsx":"d55f1ffd0162","components/layout/PageBody.jsx":"eb7780dc1ac1","components/layout/PageHeader.jsx":"d21917057c2a","components/layout/Sidebar.jsx":"0299bc37a686","components/layout/Tabs.jsx":"fdbfbafddfff","ui_kits/product-shells/screens.jsx":"fb856c24137e"},"inlinedExternals":[],"unexposedExports":[]} */

(() => {

const __ds_ns = (window.ProductionReadyPlatformDesignSystem_6437ce = window.ProductionReadyPlatformDesignSystem_6437ce || {});

const __ds_scope = {};

(__ds_ns.__errors = __ds_ns.__errors || []);

// components/core/Card.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** Bordered surface with shadow-sm and 0.5rem radius — the container for every unit of work. */
function Card({
  style,
  children,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      borderRadius: "var(--card-radius)",
      border: "1px solid var(--color-border)",
      background: "var(--color-surface)",
      boxShadow: "var(--shadow-card)",
      minWidth: 0,
      ...style
    }
  }, rest), children);
}

/** p-5 pb-3. Put CardTitle + CardDescription inside; pass `actions` for a right-aligned slot. */
function CardHeader({
  actions,
  style,
  children
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "flex-start",
      justifyContent: "space-between",
      gap: "var(--space-4)",
      padding: "var(--card-padding) var(--card-padding) var(--space-3)",
      ...style
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-1)",
      minWidth: 0
    }
  }, children), actions ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexShrink: 0,
      alignItems: "center",
      gap: "var(--gap-action)"
    }
  }, actions) : null);
}
function CardTitle({
  style,
  children
}) {
  return /*#__PURE__*/React.createElement("h2", {
    style: {
      margin: 0,
      font: "var(--text-card-title)",
      color: "var(--color-fg)",
      ...style
    }
  }, children);
}
function CardDescription({
  style,
  children
}) {
  return /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      font: "var(--text-meta)",
      color: "var(--color-fg-muted)",
      ...style
    }
  }, children);
}
function CardContent({
  style,
  children
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "0 var(--card-padding) var(--card-padding)",
      ...style
    }
  }, children);
}
Object.assign(__ds_scope, { Card, CardHeader, CardTitle, CardDescription, CardContent });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Card.jsx", error: String((e && e.message) || e) }); }

// components/core/Icon.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Lucide icon. The apps import from lucide-react@0.468; here the identical glyph data
 * comes from the Lucide UMD build — load once per page:
 * <script src="https://unpkg.com/lucide@0.468.0/dist/umd/lucide.js"></script>
 * `name` is the PascalCase Lucide name (e.g. "FileSearch", "PanelLeftClose").
 */
function Icon({
  name,
  size = 16,
  strokeWidth = 2,
  style,
  ...rest
}) {
  const set = typeof window !== "undefined" && window.lucide && (window.lucide.icons || window.lucide) || null;
  const node = set ? set[name] : null;
  if (!node) {
    return /*#__PURE__*/React.createElement("span", {
      "aria-hidden": "true",
      style: {
        display: "inline-block",
        width: size,
        height: size,
        flexShrink: 0,
        ...style
      }
    });
  }
  const children = (Array.isArray(node) ? node[2] : node.children) || [];
  return /*#__PURE__*/React.createElement("svg", _extends({
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: strokeWidth,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": "true",
    focusable: "false",
    style: {
      display: "block",
      flexShrink: 0,
      ...style
    }
  }, rest), children.map(([tag, attrs], index) => React.createElement(tag, {
    key: index,
    ...attrs
  })));
}
Object.assign(__ds_scope, { Icon });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Icon.jsx", error: String((e && e.message) || e) }); }

// components/core/Banner.jsx
try { (() => {
const TONE_ICON = {
  success: "CircleCheck",
  info: "Info",
  warning: "TriangleAlert",
  danger: "CircleAlert"
};

/**
 * Persistent in-page situation notice (setup incomplete, degraded mode, failed operation).
 * Transient success goes to Toast instead. danger → role="alert", others → role="status".
 */
function Banner({
  severity = "info",
  title,
  children,
  action,
  onDismiss,
  dismissLabel = "閉じる",
  style
}) {
  const c = `var(--color-${severity}-fg)`;
  return /*#__PURE__*/React.createElement("div", {
    role: severity === "danger" ? "alert" : "status",
    style: {
      display: "flex",
      alignItems: "flex-start",
      gap: "var(--space-2-5)",
      borderRadius: "var(--radius-lg)",
      border: `1px solid var(--color-${severity}-border)`,
      background: `var(--color-${severity}-subtle)`,
      color: c,
      padding: "var(--space-3) var(--space-3-5)",
      font: "var(--font-weight-regular) var(--font-size-sm) / var(--line-height-relaxed) var(--font-sans)",
      ...style
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: TONE_ICON[severity],
    size: 16,
    style: {
      marginTop: "0.125rem"
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0,
      flex: 1
    }
  }, title ? /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      fontWeight: 500
    }
  }, title) : null, children ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: title ? "var(--space-0-5)" : 0,
      color: "color-mix(in srgb, var(--color-fg) 90%, transparent)"
    }
  }, children) : null, action ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: "var(--space-2)",
      display: "flex",
      flexWrap: "wrap",
      gap: "var(--gap-action)"
    }
  }, action) : null), onDismiss ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onDismiss,
    "aria-label": dismissLabel,
    className: "pr-icon-button",
    style: {
      margin: "-0.5rem -0.5rem 0 0",
      width: 44,
      height: 44,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      border: 0,
      borderRadius: "var(--radius-md)",
      background: "transparent",
      color: "inherit",
      cursor: "pointer"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "X",
    size: 14
  })) : null);
}
Object.assign(__ds_scope, { TONE_ICON, Banner });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Banner.jsx", error: String((e && e.message) || e) }); }

// components/core/Skeleton.jsx
try { (() => {
/** Loading placeholder that reserves the final size (no layout shift). */
function Skeleton({
  width = "100%",
  height = "1.25rem",
  style
}) {
  return /*#__PURE__*/React.createElement("div", {
    "aria-hidden": "true",
    className: "pr-pulse",
    style: {
      width,
      height,
      borderRadius: "var(--radius-md)",
      background: "color-mix(in srgb, var(--color-border) 60%, transparent)",
      ...style
    }
  });
}
Object.assign(__ds_scope, { Skeleton });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Skeleton.jsx", error: String((e && e.message) || e) }); }

// components/core/Spinner.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Loading spinner: a full 360° track plus a 270° arc, so the silhouette never changes
 * while rotating (a bare partial arc visibly wobbles). Matches packages/ui Spinner.
 */
function Spinner({
  size = 16,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("svg", _extends({
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    "aria-hidden": "true",
    className: "pr-spin",
    style: {
      display: "block",
      flexShrink: 0,
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("circle", {
    cx: "12",
    cy: "12",
    r: "9",
    opacity: "0.25"
  }), /*#__PURE__*/React.createElement("path", {
    d: "M21 12a9 9 0 1 0-9 9",
    strokeLinecap: "round"
  }));
}
Object.assign(__ds_scope, { Spinner });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Spinner.jsx", error: String((e && e.message) || e) }); }

// components/core/Button.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
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
 *   - `aria-busy="true"` と `disabled` が自動で付きます
 * ★ 非同期の操作を起こすボタンは必ず `icon` を持たせてください。
 *   アイコンが無いとスピナーの分だけ幅が広がります。
 * 1 秒を超えて領域全体が待ちになる処理は、ボタンではなく領域側で
 * LoadingState を出します。
 */
function Button({
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
  ...rest
}) {
  const classes = ["pr-button", `pr-button--${variant}`, size !== "md" && `pr-button--${size}`, iconOnly && "pr-button--icon", touchTarget && "pr-button--touch", tone === "danger" && "pr-button--danger-tone", className].filter(Boolean).join(" ");

  // 先頭スロット: loading 中はスピナーがアイコンを置き換える（幅不変）。
  // icon が無いまま loading にするとスピナーの分だけ幅が広がる（規約違反）。
  const leading = loading ? /*#__PURE__*/React.createElement(__ds_scope.Spinner, {
    size: 16
  }) : icon ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: 16
  }) : null;
  return /*#__PURE__*/React.createElement("button", _extends({
    type: type,
    className: classes,
    disabled: disabled || loading,
    "aria-busy": loading || undefined,
    "aria-pressed": pressed
  }, rest), leading, children, trailingIcon && !loading ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: trailingIcon,
    size: 16
  }) : null);
}
Object.assign(__ds_scope, { Button });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Button.jsx", error: String((e && e.message) || e) }); }

// components/core/StatusBadge.jsx
try { (() => {
const VARIANTS = {
  neutral: ["var(--color-border-control)", "var(--color-surface)", "var(--color-fg-muted)"],
  info: ["var(--color-info-border)", "var(--color-info-subtle)", "var(--color-info-fg)"],
  success: ["var(--color-success-border)", "var(--color-success-subtle)", "var(--color-success-fg)"],
  warning: ["var(--color-warning-border)", "var(--color-warning-subtle)", "var(--color-warning-fg)"],
  danger: ["var(--color-danger-border)", "var(--color-danger-subtle)", "var(--color-danger-fg)"]
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
  danger: "CircleAlert"
};
VARIANT_ICON.pending = "History";

/**
 * State pill. Token + 1px border version (promoted from NL2SQL) so it follows the dark theme.
 * Apps map their domain enum (FileStatus, RunStatus, …) to a variant and pass a translated label.
 */
function StatusBadge({
  variant = "neutral",
  label,
  icon = true,
  style
}) {
  const [border, bg, fg] = VARIANTS[variant] || VARIANTS.neutral;
  const glyph = icon === true ? VARIANT_ICON[variant] || VARIANT_ICON.neutral : icon || null;
  return /*#__PURE__*/React.createElement("span", {
    "data-status-variant": variant,
    style: {
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
      ...style
    }
  }, glyph ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: glyph,
    size: 14,
    "aria-hidden": "true"
  }) : null, label);
}
Object.assign(__ds_scope, { StatusBadge });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/StatusBadge.jsx", error: String((e && e.message) || e) }); }

// components/core/Switch.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** Immediate-apply boolean. 44×24 track, 20px thumb; role="switch" + aria-checked. */
function Switch({
  checked = false,
  onChange,
  disabled = false,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("button", _extends({
    type: "button",
    role: "switch",
    "aria-checked": checked,
    disabled: disabled,
    onClick: () => onChange && onChange(!checked),
    style: {
      position: "relative",
      display: "inline-flex",
      flexShrink: 0,
      width: 44,
      height: 24,
      padding: 0,
      border: "1px solid transparent",
      borderRadius: "var(--radius-pill)",
      background: checked ? "var(--color-accent-fg)" : "var(--color-border)",
      cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.5 : 1,
      transition: "background-color var(--duration-enter) var(--ease-out)",
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("span", {
    "aria-hidden": "true",
    style: {
      position: "absolute",
      left: 2,
      top: "50%",
      width: 20,
      height: 20,
      borderRadius: "var(--radius-pill)",
      background: "var(--color-fg-on-accent)",
      boxShadow: "var(--shadow-sm)",
      transform: `translate(${checked ? 18 : 0}px, -50%)`,
      transition: "transform var(--duration-enter) var(--ease-out)"
    }
  }));
}
Object.assign(__ds_scope, { Switch });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Switch.jsx", error: String((e && e.message) || e) }); }

// components/core/ToggleChip.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** Filter / mode chip. Wrap a set in role="group" + aria-label with gap 0.25rem. */
function ToggleChip({
  selected = false,
  children,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("button", _extends({
    type: "button",
    className: "pr-chip",
    "aria-pressed": selected
  }, rest), children);
}
Object.assign(__ds_scope, { ToggleChip });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/ToggleChip.jsx", error: String((e && e.message) || e) }); }

// components/data/DataTable.jsx
try { (() => {
/**
 * Every list and result set. text-xs body, bg-background header, px-3 py-2 cells
 * (dense: py-1.5), divide-border/70 rows. Replaces the hand-written <table>s in all three apps.
 * columns: [{ key, header, align?: "left"|"right", mono?, sortable?, render?(row) }]
 */
function DataTable({
  columns = [],
  rows = [],
  rowKey = "id",
  dense = false,
  sort,
  onSortChange,
  onRowClick,
  emptyText = "データがありません",
  loading = false,
  ariaLabel
}) {
  const padY = dense ? "var(--space-1-5)" : "var(--space-2)";
  const cell = column => ({
    padding: `${padY} var(--space-3)`,
    textAlign: column.align === "right" ? "right" : "left"
  });
  return /*#__PURE__*/React.createElement("div", {
    style: {
      overflowX: "auto",
      borderRadius: "var(--radius-md)",
      border: "1px solid var(--color-border)",
      background: "var(--color-surface)"
    }
  }, /*#__PURE__*/React.createElement("table", {
    "aria-label": ariaLabel,
    style: {
      width: "100%",
      borderCollapse: "collapse",
      font: "var(--text-meta)",
      color: "var(--color-fg)"
    }
  }, /*#__PURE__*/React.createElement("thead", {
    style: {
      background: "var(--color-surface-sunken)",
      color: "var(--color-fg-muted)"
    }
  }, /*#__PURE__*/React.createElement("tr", null, columns.map(column => {
    const active = sort && sort.key === column.key;
    return /*#__PURE__*/React.createElement("th", {
      key: column.key,
      scope: "col",
      "aria-sort": active ? sort.direction === "asc" ? "ascending" : "descending" : undefined,
      style: {
        ...cell(column),
        fontWeight: 600,
        whiteSpace: "nowrap"
      }
    }, column.sortable ? /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "pr-sort-header",
      "data-align": column.align === "right" ? "right" : undefined,
      onClick: () => onSortChange && onSortChange({
        key: column.key,
        direction: active && sort.direction === "asc" ? "desc" : "asc"
      }),
      style: {
        color: active ? "var(--color-fg)" : "var(--color-fg-muted)"
      }
    }, column.header, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: active ? sort.direction === "asc" ? "ArrowUp" : "ArrowDown" : "ArrowUpDown",
      size: 14
    })) : column.header);
  }))), /*#__PURE__*/React.createElement("tbody", null, loading ? [0, 1, 2].map(index => /*#__PURE__*/React.createElement("tr", {
    key: index,
    style: {
      borderTop: "1px solid color-mix(in srgb, var(--color-border) 70%, transparent)"
    }
  }, columns.map(column => /*#__PURE__*/React.createElement("td", {
    key: column.key,
    style: cell(column)
  }, /*#__PURE__*/React.createElement("span", {
    className: "pr-pulse",
    style: {
      display: "block",
      height: "1rem",
      borderRadius: "var(--radius-sm)",
      background: "color-mix(in srgb, var(--color-fg-muted) 40%, transparent)"
    }
  }))))) : rows.map((row, index) => /*#__PURE__*/React.createElement("tr", {
    key: row[rowKey] ?? index,
    className: onRowClick ? "pr-row-hover" : undefined,
    onClick: onRowClick ? () => onRowClick(row) : undefined,
    style: {
      borderTop: "1px solid color-mix(in srgb, var(--color-border) 70%, transparent)",
      cursor: onRowClick ? "pointer" : undefined
    }
  }, columns.map(column => /*#__PURE__*/React.createElement("td", {
    key: column.key,
    className: column.align === "right" ? "tnum" : undefined,
    style: {
      ...cell(column),
      overflowWrap: "normal",
      fontFamily: column.mono ? "var(--font-mono)" : undefined
    }
  }, column.render ? column.render(row) : row[column.key])))), !loading && rows.length === 0 ? /*#__PURE__*/React.createElement("tr", null, /*#__PURE__*/React.createElement("td", {
    colSpan: Math.max(columns.length, 1),
    style: {
      padding: "var(--space-6) var(--space-3)",
      textAlign: "center",
      color: "var(--color-fg-muted)"
    }
  }, emptyText)) : null)));
}
Object.assign(__ds_scope, { DataTable });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/DataTable.jsx", error: String((e && e.message) || e) }); }

// components/data/Pagination.jsx
try { (() => {
/** Paging under a DataTable. Hidden when there is a single page. Default page size 10. */
function Pagination({
  page = 1,
  totalPages = 1,
  onPageChange,
  summary,
  pageIndicator,
  prevLabel = "前へ",
  nextLabel = "次へ"
}) {
  if (totalPages <= 1) return null;
  return /*#__PURE__*/React.createElement("nav", {
    "aria-label": pageIndicator || summary,
    style: {
      display: "flex",
      flexWrap: "wrap",
      alignItems: "center",
      justifyContent: "space-between",
      gap: "var(--space-2)",
      font: "var(--text-meta)",
      color: "var(--color-fg-muted)"
    }
  }, /*#__PURE__*/React.createElement("span", {
    className: "tnum"
  }, summary), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexWrap: "wrap",
      alignItems: "center",
      gap: "var(--gap-action)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: "secondary",
    size: "sm",
    icon: "ChevronLeft",
    disabled: page <= 1,
    onClick: () => onPageChange && onPageChange(page - 1)
  }, /*#__PURE__*/React.createElement("span", null, prevLabel)), pageIndicator ? /*#__PURE__*/React.createElement("span", {
    className: "tnum",
    style: {
      display: "inline-flex",
      alignItems: "center",
      minHeight: 32,
      padding: "0 var(--space-3)",
      borderRadius: "var(--button-radius)",
      border: "1px solid var(--color-border-control)",
      color: "var(--color-fg)"
    }
  }, pageIndicator) : null, /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: "secondary",
    size: "sm",
    trailingIcon: "ChevronRight",
    disabled: page >= totalPages,
    onClick: () => onPageChange && onPageChange(page + 1)
  }, /*#__PURE__*/React.createElement("span", null, nextLabel))));
}
Object.assign(__ds_scope, { Pagination });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/Pagination.jsx", error: String((e && e.message) || e) }); }

// components/feedback/ConfirmDialog.jsx
try { (() => {
const TONE = {
  danger: "TriangleAlert",
  warning: "TriangleAlert",
  info: "Info"
};

/**
 * The only modal: destructive / irreversible actions and discarding unsaved work.
 * max-w-md, rounded-xl, p-5, shadow-xl over a 50% scrim. `inline` renders without the scrim for mocks.
 */
function ConfirmDialog({
  open = true,
  tone = "danger",
  title,
  description,
  confirmLabel = "実行",
  cancelLabel = "キャンセル",
  onConfirm,
  onCancel,
  inline = false
}) {
  if (!open) return null;
  const panel = /*#__PURE__*/React.createElement("div", {
    role: "alertdialog",
    "aria-modal": inline ? undefined : true,
    "aria-labelledby": "pr-confirm-title",
    className: "pr-anim-dialog",
    style: {
      width: "100%",
      maxWidth: "28rem",
      boxSizing: "border-box",
      borderRadius: "var(--dialog-radius)",
      border: "1px solid var(--color-border)",
      background: "var(--color-surface-overlay)",
      padding: "var(--space-5)",
      boxShadow: "var(--shadow-dialog)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "flex-start",
      gap: "var(--space-3)"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      flexShrink: 0,
      width: "2.25rem",
      height: "2.25rem",
      borderRadius: "var(--radius-pill)",
      background: `var(--color-${tone}-subtle)`,
      color: `var(--color-${tone}-fg)`
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: TONE[tone],
    size: 18
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0,
      flex: 1
    }
  }, /*#__PURE__*/React.createElement("h2", {
    id: "pr-confirm-title",
    style: {
      margin: 0,
      font: "var(--font-weight-semibold) var(--font-size-base) / var(--line-height-base) var(--font-sans)",
      color: "var(--color-fg)"
    }
  }, title), description ? /*#__PURE__*/React.createElement("p", {
    style: {
      margin: "var(--space-1) 0 0",
      font: "var(--font-weight-regular) var(--font-size-sm) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-fg-muted)"
    }
  }, description) : null)), /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: "var(--space-5)",
      display: "flex",
      justifyContent: "flex-end",
      gap: "var(--gap-action)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: "secondary",
    size: "sm",
    onClick: onCancel
  }, cancelLabel), /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: tone === "danger" ? "danger" : "primary",
    size: "sm",
    onClick: onConfirm
  }, confirmLabel)));
  if (inline) return panel;
  return /*#__PURE__*/React.createElement("div", {
    className: "pr-anim-overlay",
    style: {
      position: "fixed",
      inset: 0,
      zIndex: "var(--z-dialog)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      padding: "var(--space-4)",
      background: "var(--scrim)"
    }
  }, panel);
}
Object.assign(__ds_scope, { ConfirmDialog });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/ConfirmDialog.jsx", error: String((e && e.message) || e) }); }

// components/feedback/StateViews.jsx
try { (() => {
/** Skeleton rows reserving the region's height. Use instead of a blocking spinner for >1s loads. */
function LoadingState({
  rows = 3,
  label = "読み込み中"
}) {
  return /*#__PURE__*/React.createElement("div", {
    role: "status",
    "aria-busy": "true",
    "aria-label": label,
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-2)",
      padding: "var(--space-2) 0"
    }
  }, Array.from({
    length: rows
  }).map((_, index) => /*#__PURE__*/React.createElement(__ds_scope.Skeleton, {
    key: index
  })));
}

/** Failed region with a retry. Say what happened and what to do. */
function ErrorState({
  message,
  onRetry,
  retryLabel = "再試行"
}) {
  return /*#__PURE__*/React.createElement("div", {
    role: "alert",
    style: {
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      gap: "var(--space-3)",
      padding: "var(--space-8)",
      textAlign: "center",
      borderRadius: "var(--radius-lg)",
      border: "1px solid var(--color-danger-border)",
      background: "var(--color-danger-subtle)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "CircleAlert",
    size: 24,
    style: {
      color: "var(--color-danger-fg)"
    }
  }), /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      font: "var(--font-weight-regular) var(--font-size-sm) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-fg)"
    }
  }, message), onRetry ? /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: "secondary",
    size: "sm",
    icon: "RefreshCw",
    onClick: onRetry
  }, /*#__PURE__*/React.createElement("span", null, retryLabel)) : null);
}

/** Nothing yet. Explain the prerequisite; optionally offer the creating action. */
function EmptyState({
  title,
  hint,
  action
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      gap: "var(--space-1)",
      padding: "var(--space-10) 0",
      textAlign: "center"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "Inbox",
    size: 24,
    style: {
      color: "var(--color-fg-muted)"
    }
  }), /*#__PURE__*/React.createElement("p", {
    style: {
      margin: "var(--space-1) 0 0",
      font: "var(--font-weight-regular) var(--font-size-sm) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-fg)"
    }
  }, title), hint ? /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      maxWidth: "28rem",
      font: "var(--font-weight-regular) var(--font-size-xs) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-fg-muted)"
    }
  }, hint) : null, action ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: "var(--space-3)"
    }
  }, action) : null);
}

/** 3 状態をまとめて参照するための名前空間。どれを使うかは feedback.prompt.md の規定に従う。 */
const StateViews = {
  LoadingState,
  ErrorState,
  EmptyState
};
Object.assign(__ds_scope, { LoadingState, ErrorState, EmptyState, StateViews });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/StateViews.jsx", error: String((e && e.message) || e) }); }

// components/feedback/Toast.jsx
try { (() => {
/** Bottom-right stack, 22rem wide. Position `fixed` in the app; `static` for inline mocks. */
function ToastRegion({
  static: isStatic = false,
  children
}) {
  return /*#__PURE__*/React.createElement("div", {
    "aria-live": "polite",
    style: {
      position: isStatic ? "static" : "fixed",
      right: "1rem",
      bottom: "1rem",
      zIndex: "var(--z-toast)",
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-2)",
      width: "min(92vw, 22rem)"
    }
  }, children);
}

/** Transient confirmation of something that already happened. Persistent issues use Banner. */
function Toast({
  tone = "success",
  title,
  description,
  actionLabel,
  onAction,
  onDismiss,
  dismissLabel = "閉じる"
}) {
  return /*#__PURE__*/React.createElement("div", {
    role: tone === "danger" ? "alert" : "status",
    className: "pr-anim-toast",
    style: {
      display: "flex",
      alignItems: "flex-start",
      gap: "var(--space-2-5)",
      borderRadius: "var(--radius-lg)",
      border: "1px solid var(--color-border)",
      background: "var(--color-surface-raised)",
      padding: "var(--space-3) var(--space-3-5)",
      boxShadow: "var(--shadow-toast)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: __ds_scope.TONE_ICON[tone],
    size: 16,
    style: {
      marginTop: "0.125rem",
      color: `var(--color-${tone}-fg)`
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0,
      flex: 1
    }
  }, /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      font: "var(--font-weight-medium) var(--font-size-sm) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-fg)"
    }
  }, title), description ? /*#__PURE__*/React.createElement("p", {
    style: {
      margin: "0.125rem 0 0",
      font: "var(--font-weight-regular) var(--font-size-xs) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-fg-muted)"
    }
  }, description) : null, actionLabel ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onAction,
    style: {
      marginTop: "var(--space-1-5)",
      border: 0,
      padding: 0,
      background: "transparent",
      font: "var(--font-weight-medium) var(--font-size-xs) / var(--line-height-xs) var(--font-sans)",
      color: "var(--color-accent-fg)",
      cursor: "pointer"
    }
  }, actionLabel) : null), onDismiss ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onDismiss,
    "aria-label": dismissLabel,
    style: {
      margin: "-0.5rem -0.5rem 0 0",
      width: 44,
      height: 44,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      border: 0,
      borderRadius: "var(--radius-md)",
      background: "transparent",
      color: "var(--color-fg-muted)",
      cursor: "pointer"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "X",
    size: 14
  })) : null);
}
Object.assign(__ds_scope, { ToastRegion, Toast });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/Toast.jsx", error: String((e && e.message) || e) }); }

// components/forms/FieldError.jsx
try { (() => {
/** Validation message directly under a control; wire the control's aria-describedby to `id`. */
function FieldError({
  id,
  message
}) {
  if (!message) return null;
  return /*#__PURE__*/React.createElement("p", {
    id: id,
    role: "alert",
    style: {
      margin: 0,
      font: "var(--font-weight-regular) var(--font-size-xs) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-danger-fg)"
    }
  }, message);
}

/** One-line result next to the button that produced it (保存しました / 接続に失敗しました). */
function FormStatus({
  tone = "success",
  message
}) {
  if (!message) return null;
  return /*#__PURE__*/React.createElement("p", {
    role: tone === "danger" ? "alert" : "status",
    style: {
      margin: 0,
      display: "inline-flex",
      alignItems: "flex-start",
      gap: "var(--space-1-5)",
      minWidth: 0,
      font: "var(--font-weight-medium) var(--font-size-sm) / var(--line-height-relaxed) var(--font-sans)",
      color: `var(--color-${tone}-fg)`
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: __ds_scope.TONE_ICON[tone],
    size: 15,
    style: {
      marginTop: "0.125rem"
    }
  }), /*#__PURE__*/React.createElement("span", null, message));
}
Object.assign(__ds_scope, { FieldError, FormStatus });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/FieldError.jsx", error: String((e && e.message) || e) }); }

// components/forms/SelectField.jsx
try { (() => {
/**
 * The only dropdown: custom listbox with optional per-option description.
 * Replaces raw <select> (Agent) and the NL2SQL-local SelectField.
 */
function SelectField({
  id,
  label,
  value,
  options = [],
  onChange,
  placeholder = "選択してください",
  hint,
  defaultOpen = false
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  const selected = options.find(option => option.value === value);
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--gap-field)"
    }
  }, /*#__PURE__*/React.createElement("label", {
    id: `${id}-label`,
    htmlFor: id,
    style: {
      font: "var(--text-label)",
      color: "var(--color-fg)"
    }
  }, label), /*#__PURE__*/React.createElement("div", {
    style: {
      position: "relative"
    }
  }, /*#__PURE__*/React.createElement("button", {
    id: id,
    type: "button",
    className: "pr-input",
    "aria-haspopup": "listbox",
    "aria-expanded": open,
    "aria-labelledby": `${id}-label ${id}`,
    onClick: () => setOpen(!open),
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      gap: "var(--space-2)",
      textAlign: "left",
      cursor: "pointer",
      borderColor: open ? "var(--color-focus-ring)" : undefined,
      boxShadow: open ? "inset 0 0 0 1px var(--color-focus-ring)" : undefined
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      minWidth: 0,
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap",
      color: selected ? "var(--color-fg)" : "color-mix(in srgb, var(--color-fg-muted) 70%, transparent)"
    }
  }, selected ? selected.label : placeholder), /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "ChevronDown",
    size: 16,
    style: {
      color: "var(--color-fg-muted)",
      transform: open ? "rotate(180deg)" : "none",
      transition: "transform var(--duration-enter) var(--ease-out)"
    }
  })), open ? /*#__PURE__*/React.createElement("ul", {
    role: "listbox",
    "aria-labelledby": `${id}-label`,
    style: {
      position: "absolute",
      left: 0,
      right: 0,
      top: "calc(100% + 0.25rem)",
      zIndex: "var(--z-dropdown)",
      margin: 0,
      padding: "var(--space-1)",
      listStyle: "none",
      maxHeight: "16rem",
      overflow: "auto",
      borderRadius: "var(--radius-md)",
      border: "1px solid var(--color-border)",
      background: "var(--color-surface)",
      boxShadow: "var(--shadow-popover)",
      background: "var(--color-surface-raised)"
    }
  }, options.map(option => {
    const isSelected = option.value === value;
    return /*#__PURE__*/React.createElement("li", {
      key: option.value,
      role: "option",
      "aria-selected": isSelected,
      className: "pr-option",
      onClick: () => {
        onChange && onChange(option.value);
        setOpen(false);
      },
      style: {
        display: "flex",
        gap: "var(--space-2)",
        padding: "var(--space-1-5) var(--space-2)",
        borderRadius: "var(--radius-sm)",
        font: "var(--text-body)",
        cursor: "pointer"
      }
    }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: "Check",
      size: 14,
      style: {
        marginTop: "0.125rem",
        color: "var(--color-accent-fg)",
        opacity: isSelected ? 1 : 0
      }
    }), /*#__PURE__*/React.createElement("span", {
      style: {
        minWidth: 0
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        display: "block"
      }
    }, option.label), option.description ? /*#__PURE__*/React.createElement("span", {
      style: {
        display: "block",
        marginTop: "0.125rem",
        font: "var(--text-meta)",
        color: "var(--color-fg-muted)"
      }
    }, option.description) : null));
  })) : null), hint ? /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      font: "var(--font-weight-regular) var(--font-size-xs) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-fg-muted)"
    }
  }, hint) : null);
}
Object.assign(__ds_scope, { SelectField });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/SelectField.jsx", error: String((e && e.message) || e) }); }

// components/forms/TextField.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Labelled single-line input or textarea (PROPOSED shared component — replaces the
 * TextField / SecretField copies in Agent and RAG settings clients).
 */
function TextField({
  id,
  label,
  required = false,
  hint,
  error,
  multiline = false,
  rows = 4,
  mono = false,
  value,
  onChange,
  ...rest
}) {
  const Tag = multiline ? "textarea" : "input";
  const describedBy = [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--gap-field)"
    }
  }, /*#__PURE__*/React.createElement("label", {
    htmlFor: id,
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-2)",
      font: "var(--text-label)",
      color: "var(--color-fg)"
    }
  }, label, required ? /*#__PURE__*/React.createElement("span", {
    style: {
      borderRadius: "var(--radius-pill)",
      background: "var(--color-warning-subtle)",
      color: "var(--color-warning-fg)",
      padding: "0.125rem var(--space-2)",
      fontSize: 11,
      lineHeight: "14px",
      fontWeight: 600
    }
  }, "\u5FC5\u9808") : null), /*#__PURE__*/React.createElement(Tag, _extends({
    id: id,
    className: "pr-input",
    rows: multiline ? rows : undefined,
    "aria-required": required || undefined,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": describedBy,
    value: value,
    onChange: onChange ? event => onChange(event.target.value) : undefined,
    style: mono ? {
      fontFamily: "var(--font-mono)"
    } : undefined
  }, rest)), hint ? /*#__PURE__*/React.createElement("p", {
    id: `${id}-hint`,
    style: {
      margin: 0,
      font: "var(--font-weight-regular) var(--font-size-xs) / var(--line-height-relaxed) var(--font-sans)",
      color: "var(--color-fg-muted)"
    }
  }, hint) : null, /*#__PURE__*/React.createElement(__ds_scope.FieldError, {
    id: `${id}-error`,
    message: error
  }));
}
Object.assign(__ds_scope, { TextField });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/TextField.jsx", error: String((e && e.message) || e) }); }

// components/layout/AppShell.jsx
try { (() => {
/** Full-height shell: Sidebar slot on the left, one scrolling main region. Required in every app. */
function AppShell({
  sidebar,
  children,
  style
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      width: "100%",
      height: "100vh",
      overflow: "hidden",
      position: "relative",
      background: "var(--color-canvas)",
      color: "var(--color-fg)",
      fontFamily: "var(--font-sans)",
      ...style
    }
  }, /*#__PURE__*/React.createElement("a", {
    className: "pr-skip-link",
    href: "#pr-main"
  }, "\u672C\u6587\u3078\u30B9\u30AD\u30C3\u30D7"), sidebar, /*#__PURE__*/React.createElement("main", {
    id: "pr-main",
    tabIndex: -1,
    style: {
      display: "flex",
      minWidth: 0,
      flex: 1,
      flexDirection: "column",
      overflowY: "auto"
    }
  }, children));
}
Object.assign(__ds_scope, { AppShell });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/layout/AppShell.jsx", error: String((e && e.message) || e) }); }

// components/layout/PageBody.jsx
try { (() => {
/**
 * PageHeader の直後に置く本文コンテナ。readme が規定していた
 * 「2rem の左右ガター / セクション間 1.5rem」の唯一の実装です。
 * これが無かったため 3 アプリがそれぞれ手書きしていました。
 *
 * `--content-max-width` (1440px) で計測幅を止めます。<main> が無制限に伸びると
 * 10 列の表が 2400px に広がり、目が行を追えません（視線移動限界は約 1000〜1200px）。
 * 表を画面幅いっぱいに出したい画面だけ `wide` を使います。
 */
function PageBody({
  wide = false,
  children,
  style
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gap: "var(--gap-stack)",
      alignContent: "start",
      width: "100%",
      maxWidth: wide ? "none" : "var(--content-max-width)",
      marginInline: wide ? 0 : "auto",
      boxSizing: "border-box",
      padding: "var(--page-gutter-y) var(--page-gutter-x)",
      minWidth: 0,
      ...style
    }
  }, children);
}

/**
 * 見出し付きのひとまとまり。カードを複数含む領域や、カードに入れるほどでもない
 * 領域に使います。見出しは --text-section-title（16px / 600）で、
 * ページタイトル 20px とカード見出し 14px の間の段を埋めます。
 */
function Section({
  title,
  description,
  actions,
  children,
  style
}) {
  return /*#__PURE__*/React.createElement("section", {
    style: {
      display: "grid",
      gap: "var(--space-3)",
      minWidth: 0,
      ...style
    }
  }, title || actions ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "flex-start",
      justifyContent: "space-between",
      gap: "var(--space-4)",
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0
    }
  }, title ? /*#__PURE__*/React.createElement("h2", {
    style: {
      margin: 0,
      font: "var(--text-section-title)",
      color: "var(--color-fg)"
    }
  }, title) : null, description ? /*#__PURE__*/React.createElement("p", {
    style: {
      margin: "var(--space-1) 0 0",
      font: "var(--text-body)",
      color: "var(--color-fg-muted)"
    }
  }, description) : null), actions ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexShrink: 0,
      alignItems: "center",
      gap: "var(--gap-action)"
    }
  }, actions) : null) : null, children);
}
Object.assign(__ds_scope, { PageBody, Section });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/layout/PageBody.jsx", error: String((e && e.message) || e) }); }

// components/layout/PageHeader.jsx
try { (() => {
/* アクションの並び順。右寄せグループなので、右端（＝最も押しやすい位置）に primary が来ます。
   旧構成は primary が左端で danger が右端＝最も破壊的な操作が最も押しやすい位置でした。
   danger は本来オーバーフローメニューに入れるべきものです（DropdownMenu 実装後に移行）。 */
const ORDER = {
  danger: 0,
  utility: 1,
  secondary: 2,
  primary: 3
};
const VARIANT = {
  primary: "primary",
  secondary: "secondary",
  utility: "ghost",
  danger: "danger"
};

/** Location trail for 3+ level flows. The last item is the current page. */
function Breadcrumbs({
  items = [],
  onNavigate
}) {
  if (items.length === 0) return null;
  return /*#__PURE__*/React.createElement("nav", {
    "aria-label": "\u30D1\u30F3\u304F\u305A",
    style: {
      display: "flex",
      alignItems: "center",
      font: "var(--text-meta)",
      color: "var(--color-fg-muted)"
    }
  }, /*#__PURE__*/React.createElement("ol", {
    style: {
      display: "flex",
      flexWrap: "wrap",
      alignItems: "center",
      gap: "var(--space-1)",
      margin: 0,
      padding: 0,
      listStyle: "none"
    }
  }, items.map((item, index) => {
    const last = index === items.length - 1;
    return /*#__PURE__*/React.createElement(React.Fragment, {
      key: `${item.label}-${index}`
    }, /*#__PURE__*/React.createElement("li", null, item.href && !last ? /*#__PURE__*/React.createElement("a", {
      href: item.href,
      onClick: onNavigate ? event => {
        event.preventDefault();
        onNavigate(item.href);
      } : undefined,
      style: {
        color: "inherit",
        textDecoration: "none"
      }
    }, item.label) : /*#__PURE__*/React.createElement("span", {
      "aria-current": last ? "page" : undefined,
      style: last ? {
        fontWeight: 500,
        color: "var(--color-fg)"
      } : undefined
    }, item.label)), last ? null : /*#__PURE__*/React.createElement("li", {
      "aria-hidden": "true",
      style: {
        display: "flex",
        color: "var(--color-fg-subtle)"
      }
    }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: "ChevronRight",
      size: 14
    })));
  })));
}

/**
 * Every screen opens with this header (px-8 py-5 on --color-surface over a hairline).
 * 長い表でもタイトルと主要操作に手が届くよう `position: sticky` で上端に貼り付きます。
 * `tabs` に <Tabs> を渡すとヘッダー下端に吸い付きます（ビュー切替の唯一の置き場所）。
 * actions: [{ id, kind: "primary"|"secondary"|"utility"|"danger", label, icon?, onClick?, loading?, disabled? }]
 * 並びは danger → utility → secondary → primary（右端が primary）。
 */
function PageHeader({
  title,
  subtitle,
  status,
  breadcrumbs,
  actions = [],
  tabs,
  wide = false,
  onNavigate
}) {
  const ordered = actions.map((action, index) => ({
    action,
    index
  })).sort((a, b) => ORDER[a.action.kind] - ORDER[b.action.kind] || a.index - b.index).map(({
    action
  }) => action);
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
    minWidth: 0
  };
  return /*#__PURE__*/React.createElement("header", {
    style: {
      position: "sticky",
      top: 0,
      zIndex: "var(--z-sticky)",
      display: "flex",
      flexDirection: "column",
      gap: tabs ? "var(--space-4)" : 0,
      padding: tabs ? "var(--page-header-py) 0 0" : "var(--page-header-py) 0",
      borderBottom: "1px solid var(--color-border)",
      background: "var(--color-surface)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      ...measure,
      display: "flex",
      flexWrap: "wrap",
      alignItems: "flex-start",
      justifyContent: "space-between",
      gap: "var(--space-4)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0,
      flex: "1 1 auto"
    }
  }, breadcrumbs && breadcrumbs.length ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginBottom: "var(--space-1-5)"
    }
  }, /*#__PURE__*/React.createElement(Breadcrumbs, {
    items: breadcrumbs,
    onNavigate: onNavigate
  })) : null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexWrap: "wrap",
      alignItems: "center",
      gap: "var(--space-2-5)"
    }
  }, /*#__PURE__*/React.createElement("h1", {
    style: {
      margin: 0,
      font: "var(--text-page-title)",
      color: "var(--color-fg)"
    }
  }, title), status || null), subtitle ? /*#__PURE__*/React.createElement("p", {
    style: {
      margin: "var(--space-1) 0 0",
      font: "var(--text-body)",
      color: "var(--color-fg-muted)"
    }
  }, subtitle) : null), ordered.length ? /*#__PURE__*/React.createElement("div", {
    role: "group",
    "aria-label": "\u30DA\u30FC\u30B8\u64CD\u4F5C",
    style: {
      display: "flex",
      flexShrink: 0,
      alignItems: "center",
      gap: "var(--gap-action)"
    }
  }, ordered.map(action => /*#__PURE__*/React.createElement(__ds_scope.Button, {
    key: action.id,
    variant: VARIANT[action.kind],
    icon: action.icon,
    loading: action.loading,
    disabled: action.disabled,
    onClick: action.onClick,
    iconOnly: !action.label,
    "aria-label": action.label ? undefined : action.ariaLabel
  }, action.label ? /*#__PURE__*/React.createElement("span", null, action.label) : null))) : null), tabs ? /*#__PURE__*/React.createElement("div", {
    style: measure
  }, tabs) : null);
}
Object.assign(__ds_scope, { Breadcrumbs, PageHeader });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/layout/PageHeader.jsx", error: String((e && e.message) || e) }); }

// components/layout/Sidebar.jsx
try { (() => {
const reveal = collapsed => ({
  opacity: collapsed ? 0 : 1,
  transition: "opacity var(--duration-reveal) var(--ease-out)"
});

/**
 * Collapsible dark navigation shared by all products. Only `product` (second line of the
 * wordmark), `sections` and the account in the footer differ between RAG / NL2SQL / Agent.
 * sections: [{ key, title, collapsed?, items: [{ href, label, sidebarLabel?, icon }] }]
 * account: { name, roles } — renders SidebarAccountFooter (PROPOSED shared footer).
 */
function Sidebar({
  product,
  sections = [],
  currentPath,
  collapsed = false,
  onToggleCollapsed,
  onToggleSection,
  onNavigate,
  onOpenCommandPalette,
  account,
  theme = "light",
  onToggleTheme,
  onLogout
}) {
  const isActive = href => currentPath === href || (currentPath || "").startsWith(href + "/");
  return /*#__PURE__*/React.createElement("aside", {
    "aria-label": "\u30B5\u30A4\u30C9\u30CA\u30D3\u30B2\u30FC\u30B7\u30E7\u30F3",
    "data-surface": "inverted",
    "data-state": collapsed ? "collapsed" : "expanded",
    style: {
      display: "flex",
      flexDirection: "column",
      flexShrink: 0,
      height: "100%",
      width: collapsed ? "var(--sidebar-width-collapsed)" : "var(--sidebar-width)",
      overflow: "hidden",
      background: "var(--color-surface)",
      color: "var(--color-fg-muted)",
      fontFamily: "var(--font-sans)",
      transition: "width var(--duration-enter) var(--ease-out)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: collapsed ? "center" : "space-between",
      height: "var(--sidebar-header-height)",
      flexShrink: 0,
      padding: collapsed ? "0 var(--space-2)" : "0 var(--space-3)",
      borderBottom: "1px solid var(--color-border)"
    }
  }, collapsed ? null : /*#__PURE__*/React.createElement("div", {
    title: `Production Ready ${product}`,
    style: {
      minWidth: 0,
      flex: 1,
      padding: "0 var(--space-2)",
      color: "var(--color-fg)",
      ...reveal(collapsed)
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      whiteSpace: "nowrap",
      fontSize: "var(--font-size-base)",
      lineHeight: "1.25rem",
      fontWeight: 700
    }
  }, "Production Ready"), /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      whiteSpace: "nowrap",
      fontSize: "var(--font-size-xs)",
      lineHeight: "var(--line-height-xs)",
      fontWeight: 600,
      color: "var(--color-fg-muted)"
    }
  }, product)), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "pr-nav-row",
    onClick: onToggleCollapsed,
    "aria-label": collapsed ? "サイドバーを展開" : "サイドバーを折りたたむ",
    "aria-expanded": !collapsed,
    style: {
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      width: "2.75rem",
      height: "2.75rem",
      flexShrink: 0,
      border: 0,
      borderRadius: "var(--radius-md)",
      background: "transparent",
      color: "var(--color-fg-muted)",
      cursor: "pointer"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: collapsed ? "PanelLeftOpen" : "PanelLeftClose",
    size: 18
  }))), /*#__PURE__*/React.createElement("nav", {
    style: {
      flex: 1,
      minHeight: 0,
      overflowX: "hidden",
      overflowY: "auto",
      padding: collapsed ? "var(--space-3) var(--space-2)" : "var(--space-3)"
    }
  }, onOpenCommandPalette ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "pr-nav-row",
    onClick: onOpenCommandPalette,
    "aria-label": "\u30B3\u30DE\u30F3\u30C9\u30D1\u30EC\u30C3\u30C8\u3092\u958B\u304F",
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: collapsed ? "center" : "flex-start",
      gap: "var(--space-2)",
      width: "100%",
      height: "var(--command-button-height)",
      marginBottom: "var(--space-3)",
      padding: collapsed ? 0 : "0 var(--space-3)",
      border: "1px solid var(--color-border)",
      borderRadius: "var(--radius-md)",
      background: "transparent",
      color: "var(--color-fg-muted)",
      font: "var(--text-body)",
      cursor: "pointer"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "Search",
    size: 16
  }), collapsed ? null : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("span", {
    style: {
      minWidth: 0,
      flex: 1,
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap",
      textAlign: "left"
    }
  }, "\u30B3\u30DE\u30F3\u30C9\u30D1\u30EC\u30C3\u30C8\u3092\u958B\u304F"), /*#__PURE__*/React.createElement("kbd", {
    style: {
      flexShrink: 0,
      borderRadius: "var(--radius-sm)",
      border: "1px solid var(--color-border-strong)",
      padding: "0.125rem 0.375rem",
      fontFamily: "var(--font-sans)",
      fontSize: 10,
      fontWeight: 500,
      color: "var(--color-fg-subtle)"
    }
  }, "\u2318K"))) : null, sections.map(section => {
    const expanded = collapsed || !section.collapsed;
    const containsActive = section.items.some(item => isActive(item.href));
    return /*#__PURE__*/React.createElement("div", {
      key: section.key,
      style: {
        marginBottom: collapsed ? "var(--space-3)" : "var(--space-4)"
      }
    }, collapsed ? null : /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "pr-nav-row",
      "aria-expanded": expanded,
      onClick: () => onToggleSection && onToggleSection(section.key),
      style: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "var(--space-2)",
        width: "100%",
        padding: "var(--space-1) var(--space-3)",
        border: 0,
        borderRadius: "var(--radius-md)",
        background: "transparent",
        color: "var(--color-fg-subtle)",
        font: "var(--font-weight-semibold) var(--font-size-xs) / var(--line-height-xs) var(--font-sans)",
        letterSpacing: "0.025em",
        cursor: "pointer"
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        display: "flex",
        alignItems: "center",
        gap: "var(--space-1-5)",
        minWidth: 0
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap"
      }
    }, section.title), !expanded && containsActive ? /*#__PURE__*/React.createElement("span", {
      "aria-hidden": "true",
      style: {
        width: 6,
        height: 6,
        borderRadius: "var(--radius-pill)",
        background: "var(--color-accent-emphasis)"
      }
    }) : null), /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: "ChevronDown",
      size: 14,
      style: {
        transform: expanded ? "none" : "rotate(-90deg)",
        transition: "transform var(--duration-enter) var(--ease-out)"
      }
    })), expanded ? /*#__PURE__*/React.createElement("ul", {
      style: {
        margin: 0,
        padding: collapsed ? 0 : "var(--space-1) 0 0",
        listStyle: "none",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-1)"
      }
    }, section.items.map(item => {
      const active = isActive(item.href);
      return /*#__PURE__*/React.createElement("li", {
        key: item.href
      }, /*#__PURE__*/React.createElement("a", {
        href: item.href,
        className: "pr-nav-row",
        "aria-current": active ? "page" : undefined,
        "aria-label": collapsed ? item.label : undefined,
        title: item.label,
        onClick: onNavigate ? event => {
          event.preventDefault();
          onNavigate(item.href);
        } : undefined,
        style: {
          position: "relative",
          display: "flex",
          alignItems: "center",
          justifyContent: collapsed ? "center" : "flex-start",
          gap: "var(--space-2-5)",
          height: "var(--nav-item-height)",
          padding: collapsed ? 0 : "0 var(--space-3)",
          overflow: "hidden",
          borderRadius: "var(--radius-md)",
          color: "inherit",
          textDecoration: "none",
          font: "var(--text-body)"
        }
      }, active ? /*#__PURE__*/React.createElement("span", {
        "aria-hidden": "true",
        style: {
          position: "absolute",
          left: 0,
          top: "50%",
          width: "0.25rem",
          height: "1.25rem",
          transform: "translateY(-50%)",
          borderRadius: "0 9999px 9999px 0",
          background: "var(--color-fg)"
        }
      }) : null, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
        name: item.icon,
        size: 18
      }), collapsed ? null : /*#__PURE__*/React.createElement("span", {
        style: {
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap"
        }
      }, item.sidebarLabel || item.label)));
    })) : null);
  })), account ? /*#__PURE__*/React.createElement(SidebarAccountFooter, {
    account: account,
    collapsed: collapsed,
    theme: theme,
    onToggleTheme: onToggleTheme,
    onLogout: onLogout
  }) : null);
}

/** PROPOSED shared footer: user + roles, logout, theme toggle. Replaces three app-specific footers. */
function SidebarAccountFooter({
  account,
  collapsed = false,
  theme = "light",
  onToggleTheme,
  onLogout
}) {
  const row = {
    display: "flex",
    alignItems: "center",
    gap: "var(--space-2-5)",
    height: "var(--nav-item-height)",
    border: 0,
    borderRadius: "var(--radius-md)",
    background: "transparent",
    color: "inherit",
    font: "var(--text-body)",
    cursor: "pointer"
  };
  return /*#__PURE__*/React.createElement("div", {
    style: {
      borderTop: "1px solid var(--color-border)",
      padding: collapsed ? "var(--space-3) var(--space-2)" : "var(--space-3)"
    }
  }, collapsed ? null : /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-2-5)",
      minHeight: "var(--nav-item-height)",
      padding: "var(--space-2) var(--space-3)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "UserRound",
    size: 18
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap",
      font: "var(--text-label)",
      color: "var(--color-fg)"
    }
  }, account.name), account.roles ? /*#__PURE__*/React.createElement("div", {
    style: {
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap",
      font: "var(--text-meta)",
      color: "var(--color-fg-subtle)"
    }
  }, account.roles) : null)), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: collapsed ? "column" : "row",
      gap: "var(--space-1)"
    }
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "pr-nav-row",
    onClick: onLogout,
    "aria-label": "\u30ED\u30B0\u30A2\u30A6\u30C8",
    style: {
      ...row,
      flex: 1,
      justifyContent: collapsed ? "center" : "flex-start",
      padding: collapsed ? 0 : "0 var(--space-3)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "LogOut",
    size: 18
  }), collapsed ? null : /*#__PURE__*/React.createElement("span", null, "\u30ED\u30B0\u30A2\u30A6\u30C8")), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "pr-nav-row",
    onClick: onToggleTheme,
    "aria-label": theme === "dark" ? "ライトテーマに切り替え" : "ダークテーマに切り替え",
    style: {
      ...row,
      justifyContent: "center",
      width: collapsed ? "100%" : "var(--nav-item-height)",
      padding: 0,
      border: "1px solid var(--color-border)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: theme === "dark" ? "Sun" : "Moon",
    size: 18
  }))));
}
Object.assign(__ds_scope, { Sidebar, SidebarAccountFooter });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/layout/Sidebar.jsx", error: String((e && e.message) || e) }); }

// components/layout/Tabs.jsx
try { (() => {
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
function Tabs({
  items = [],
  value,
  onChange,
  ariaLabel = "ビュー切替",
  style
}) {
  const refs = React.useRef({});
  const enabled = items.filter(item => !item.disabled);
  const move = delta => {
    if (enabled.length === 0) return;
    const current = enabled.findIndex(item => item.id === value);
    const next = enabled[(current + delta + enabled.length) % enabled.length];
    if (!next) return;
    onChange && onChange(next.id);
    const node = refs.current[next.id];
    if (node) node.focus();
  };
  const jump = index => {
    const target = enabled[index];
    if (!target) return;
    onChange && onChange(target.id);
    const node = refs.current[target.id];
    if (node) node.focus();
  };
  const onKeyDown = event => {
    if (event.key === "ArrowRight") {
      event.preventDefault();
      move(1);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      jump(0);
    } else if (event.key === "End") {
      event.preventDefault();
      jump(enabled.length - 1);
    }
  };
  return /*#__PURE__*/React.createElement("div", {
    role: "tablist",
    "aria-label": ariaLabel,
    className: "pr-tablist",
    onKeyDown: onKeyDown,
    style: style
  }, items.map(item => {
    const selected = item.id === value;
    return /*#__PURE__*/React.createElement("button", {
      key: item.id,
      ref: node => {
        refs.current[item.id] = node;
      },
      type: "button",
      role: "tab",
      id: `pr-tab-${item.id}`,
      className: "pr-tab",
      "aria-selected": selected,
      "aria-controls": `pr-tabpanel-${item.id}`,
      tabIndex: selected ? 0 : -1,
      disabled: item.disabled,
      onClick: () => onChange && onChange(item.id)
    }, item.icon ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: item.icon,
      size: 16
    }) : null, /*#__PURE__*/React.createElement("span", null, item.label), item.count == null ? null : /*#__PURE__*/React.createElement("span", {
      className: "pr-tab-count"
    }, item.count));
  }));
}

/** 対応する中身。`id` は Tabs の item.id と一致させます。 */
function TabPanel({
  id,
  value,
  children,
  style
}) {
  if (id !== value) return null;
  return /*#__PURE__*/React.createElement("div", {
    role: "tabpanel",
    id: `pr-tabpanel-${id}`,
    "aria-labelledby": `pr-tab-${id}`,
    tabIndex: 0,
    style: {
      minWidth: 0,
      ...style
    }
  }, children);
}
Object.assign(__ds_scope, { Tabs, TabPanel });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/layout/Tabs.jsx", error: String((e && e.message) || e) }); }

// ui_kits/product-shells/screens.jsx
try { (() => {
// Representative screens for each product, composed only from the shared components.
// Navigation labels are the real i18n strings of each app; table rows are sample data.
const {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  EmptyState,
  Icon,
  PageBody,
  PageHeader,
  Pagination,
  SelectField,
  StatusBadge,
  TextField,
  ToggleChip
} = window.ProductionReadyPlatformDesignSystem_6437ce;
const badge = value => {
  const [variant, label] = value.split("|");
  return /*#__PURE__*/React.createElement(StatusBadge, {
    variant: variant,
    label: label
  });
};
const muted = text => /*#__PURE__*/React.createElement("span", {
  className: "tnum",
  style: {
    color: "var(--color-fg-muted)"
  }
}, text);
function RagFiles() {
  const [filter, setFilter] = React.useState("all");
  const rows = [["就業規則_2026年度版.pdf", "人事規程", "success|索引済み", "412", "2026-09-12 18:04"], ["経費精算マニュアル.docx", "経理", "success|索引済み", "186", "2026-09-12 17:51"], ["情報セキュリティ基本方針.pdf", "情報システム", "info|解析中", "—", "2026-09-12 17:48"], ["製品仕様書_v3.2.xlsx", "製品開発", "pending|待機中", "—", "2026-09-12 17:40"], ["取引先契約書_雛形.pdf", "法務", "danger|解析失敗", "—", "2026-09-12 16:22"], ["社内FAQ_ヘルプデスク.md", "情報システム", "warning|要再索引", "254", "2026-09-10 09:30"]].map(([name, kb, status, chunks, updated], id) => ({
    id,
    name,
    kb,
    status,
    chunks,
    updated
  }));
  return /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(PageHeader, {
    breadcrumbs: [{
      label: "ナレッジ構築"
    }, {
      label: "文書インデックス"
    }],
    title: "\u6587\u66F8\u30A4\u30F3\u30C7\u30C3\u30AF\u30B9",
    subtitle: "\u30A2\u30C3\u30D7\u30ED\u30FC\u30C9\u6E08\u307F\u6587\u66F8\u306E\u89E3\u6790\u30FB\u7D22\u5F15\u72B6\u614B\u3092\u78BA\u8A8D\u3057\u307E\u3059\u3002",
    actions: [{
      id: "upload",
      kind: "primary",
      label: "文書アップロード",
      icon: "Upload"
    }, {
      id: "reload",
      kind: "secondary",
      label: "最新情報を取得",
      icon: "RefreshCw"
    }]
  }), /*#__PURE__*/React.createElement(PageBody, null, /*#__PURE__*/React.createElement(Banner, {
    severity: "warning",
    title: "2 \u4EF6\u306E\u6587\u66F8\u3067\u89E3\u6790\u306B\u5931\u6557\u3057\u3066\u3044\u307E\u3059\u3002",
    action: /*#__PURE__*/React.createElement(Button, {
      variant: "secondary",
      size: "sm"
    }, "\u5931\u6557\u306E\u307F\u8868\u793A")
  }, "\u6587\u66F8\u89E3\u6790\u306E\u8A2D\u5B9A\u3092\u78BA\u8A8D\u3057\u3066\u304B\u3089\u518D\u5B9F\u884C\u3057\u3066\u304F\u3060\u3055\u3044\u3002"), /*#__PURE__*/React.createElement(Card, null, /*#__PURE__*/React.createElement(CardHeader, {
    actions: /*#__PURE__*/React.createElement("div", {
      role: "group",
      "aria-label": "\u72B6\u614B\u3067\u7D5E\u308A\u8FBC\u307F",
      style: {
        display: "flex",
        gap: "var(--space-1)"
      }
    }, [["all", "すべて"], ["run", "処理中"], ["done", "索引済み"], ["ng", "失敗"]].map(([k, l]) => /*#__PURE__*/React.createElement(ToggleChip, {
      key: k,
      selected: filter === k,
      onClick: () => setFilter(k)
    }, l)))
  }, /*#__PURE__*/React.createElement(CardTitle, null, "\u6587\u66F8\u4E00\u89A7"), /*#__PURE__*/React.createElement(CardDescription, null, "124 \u4EF6\u306E\u6587\u66F8")), /*#__PURE__*/React.createElement(CardContent, {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-3)"
    }
  }, /*#__PURE__*/React.createElement(DataTable, {
    ariaLabel: "\u6587\u66F8\u4E00\u89A7",
    rows: rows,
    onRowClick: () => {},
    columns: [{
      key: "name",
      header: "ファイル名",
      sortable: true,
      render: r => /*#__PURE__*/React.createElement("span", {
        style: {
          fontWeight: 500
        }
      }, r.name)
    }, {
      key: "kb",
      header: "ナレッジベース"
    }, {
      key: "status",
      header: "状態",
      render: r => badge(r.status)
    }, {
      key: "chunks",
      header: "チャンク数",
      align: "right"
    }, {
      key: "updated",
      header: "更新日時",
      render: r => muted(r.updated)
    }]
  }), /*#__PURE__*/React.createElement(Pagination, {
    page: 1,
    totalPages: 21,
    summary: "1\u20136 / 124 \u4EF6",
    pageIndicator: "1 / 21"
  })))));
}
function Nl2sqlQuery() {
  const [profile, setProfile] = React.useState("sales");
  return /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(PageHeader, {
    breadcrumbs: [{
      label: "AI 活用"
    }, {
      label: "SQL 生成"
    }],
    title: "SQL \u751F\u6210",
    status: /*#__PURE__*/React.createElement(StatusBadge, {
      variant: "success",
      label: "DB \u63A5\u7D9A\u4E2D"
    }),
    subtitle: "\u81EA\u7136\u8A00\u8A9E\u306E\u8CEA\u554F\u304B\u3089 SELECT SQL \u3092\u751F\u6210\u3057\u3001\u5B89\u5168\u6027\u3092\u78BA\u8A8D\u3057\u3066\u5B9F\u884C\u3057\u307E\u3059\u3002",
    actions: [{
      id: "history",
      kind: "secondary",
      label: "実行履歴",
      icon: "History"
    }]
  }), /*#__PURE__*/React.createElement(PageBody, null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
      gap: "var(--gap-stack)"
    }
  }, /*#__PURE__*/React.createElement(Card, null, /*#__PURE__*/React.createElement(CardHeader, null, /*#__PURE__*/React.createElement(CardTitle, null, "\u8CEA\u554F"), /*#__PURE__*/React.createElement(CardDescription, null, "\u696D\u52D9\u30D7\u30ED\u30D5\u30A1\u30A4\u30EB\u306B\u6CBF\u3063\u3066 SQL \u3092\u751F\u6210\u3057\u307E\u3059\u3002")), /*#__PURE__*/React.createElement(CardContent, {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-4)"
    }
  }, /*#__PURE__*/React.createElement(SelectField, {
    id: "profile",
    label: "\u696D\u52D9\u30D7\u30ED\u30D5\u30A1\u30A4\u30EB",
    value: profile,
    onChange: setProfile,
    options: [{
      value: "sales",
      label: "営業分析",
      description: "SALES / REGIONS"
    }, {
      value: "hr",
      label: "人事",
      description: "EMPLOYEES"
    }]
  }), /*#__PURE__*/React.createElement(TextField, {
    id: "question",
    label: "\u8CEA\u554F",
    required: true,
    multiline: true,
    rows: 3,
    defaultValue: "2026\u5E74\u5EA6\u4E0A\u671F\u306E\u5730\u57DF\u5225\u58F2\u4E0A\u5408\u8A08\u3092\u591A\u3044\u9806\u306B\u6559\u3048\u3066"
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      justifyContent: "flex-end",
      gap: "var(--gap-action)"
    }
  }, /*#__PURE__*/React.createElement(Button, {
    variant: "secondary",
    icon: "Eraser"
  }, /*#__PURE__*/React.createElement("span", null, "\u30AF\u30EA\u30A2")), /*#__PURE__*/React.createElement(Button, {
    icon: "Sparkles"
  }, /*#__PURE__*/React.createElement("span", null, "SQL \u3092\u751F\u6210"))))), /*#__PURE__*/React.createElement(Card, null, /*#__PURE__*/React.createElement(CardHeader, {
    actions: /*#__PURE__*/React.createElement(StatusBadge, {
      variant: "success",
      label: "\u5B89\u5168"
    })
  }, /*#__PURE__*/React.createElement(CardTitle, null, "\u751F\u6210\u3055\u308C\u305F SQL"), /*#__PURE__*/React.createElement(CardDescription, null, "\u5B9F\u884C\u524D\u306B\u5B89\u5168\u6027\u30C1\u30A7\u30C3\u30AF\u3092\u901A\u904E\u3057\u307E\u3057\u305F\u3002")), /*#__PURE__*/React.createElement(CardContent, {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-3)"
    }
  }, /*#__PURE__*/React.createElement("pre", {
    style: {
      margin: 0,
      padding: "var(--space-3-5) var(--space-4)",
      borderRadius: "var(--radius-md)",
      border: "1px solid var(--color-border)",
      background: "var(--color-code-canvas)",
      color: "var(--slab-code-fg)",
      font: "var(--text-code)",
      overflowX: "auto"
    }
  }, `SELECT r.region_name,\n       SUM(s.amount) AS total_sales\n  FROM sales s\n  JOIN regions r ON r.region_id = s.region_id\n WHERE s.sold_on >= DATE '2026-04-01'\n GROUP BY r.region_name\n ORDER BY total_sales DESC`), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      justifyContent: "flex-end",
      gap: "var(--gap-action)"
    }
  }, /*#__PURE__*/React.createElement(Button, {
    variant: "secondary",
    icon: "Copy"
  }, /*#__PURE__*/React.createElement("span", null, "SQL \u3092\u30B3\u30D4\u30FC")), /*#__PURE__*/React.createElement(Button, {
    icon: "Play"
  }, /*#__PURE__*/React.createElement("span", null, "\u5B9F\u884C")))))), /*#__PURE__*/React.createElement(Card, null, /*#__PURE__*/React.createElement(CardHeader, null, /*#__PURE__*/React.createElement(CardTitle, null, "\u5B9F\u884C\u7D50\u679C\uFF085\u4EF6\uFF09"), /*#__PURE__*/React.createElement(CardDescription, null, "\u51E6\u7406\u6642\u9593 0.42\u79D2")), /*#__PURE__*/React.createElement(CardContent, null, /*#__PURE__*/React.createElement(DataTable, {
    ariaLabel: "\u5B9F\u884C\u7D50\u679C",
    columns: [{
      key: "REGION_NAME",
      header: "REGION_NAME",
      mono: true
    }, {
      key: "TOTAL_SALES",
      header: "TOTAL_SALES",
      align: "right"
    }],
    rows: [["関東", "1,284,500,000"], ["近畿", "742,310,000"], ["中部", "518,940,000"], ["九州", "301,220,000"], ["北海道", "156,780,000"]].map(([a, b], id) => ({
      id,
      REGION_NAME: a,
      TOTAL_SALES: b
    }))
  })))));
}
function AgentDashboard() {
  const metric = (label, value, note) => /*#__PURE__*/React.createElement(Card, null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-1)",
      padding: "var(--card-padding)"
    }
  }, /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      font: "var(--text-meta)",
      color: "var(--color-fg-muted)"
    }
  }, label), /*#__PURE__*/React.createElement("p", {
    className: "tnum",
    style: {
      margin: 0,
      font: "700 1.5rem/2rem var(--font-sans)"
    }
  }, value), /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      font: "var(--text-meta)",
      color: "var(--color-fg-muted)"
    }
  }, note)));
  const runs = [["run-8f21c", "請求書チェック Agent", "success|完了", "2m 14s", "2026-09-13 10:42"], ["run-8f20a", "問い合わせ一次回答", "info|実行中", "—", "2026-09-13 10:40"], ["run-8f1ff", "契約書レビュー", "pending|承認待ち", "—", "2026-09-13 10:31"], ["run-8f1d2", "請求書チェック Agent", "danger|失敗", "11s", "2026-09-13 08:57"]].map(([id, agent, status, duration, started]) => ({
    id,
    agent,
    status,
    duration,
    started
  }));
  return /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(PageHeader, {
    title: "\u30C0\u30C3\u30B7\u30E5\u30DC\u30FC\u30C9",
    subtitle: "Agent\u30FBRuntime\u30FBRun \u306E\u7A3C\u50CD\u72B6\u6CC1\u3068\u627F\u8A8D\u5F85\u3061\u3092\u78BA\u8A8D\u3057\u307E\u3059\u3002",
    actions: [{
      id: "create",
      kind: "primary",
      label: "Agent を作成",
      icon: "Plus"
    }, {
      id: "runs",
      kind: "secondary",
      label: "Run 一覧",
      icon: "Play"
    }]
  }), /*#__PURE__*/React.createElement(PageBody, null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
      gap: "var(--space-4)"
    }
  }, metric("稼働中 Agent", "12", "登録 18 件中"), metric("実行中 Run", "3", "Runtime 2 台"), metric("承認待ち", "1", "最長 11 分"), metric("24h 失敗率", "2.4%", "失敗 3 / 124 件")), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr)",
      gap: "var(--gap-stack)"
    }
  }, /*#__PURE__*/React.createElement(Card, null, /*#__PURE__*/React.createElement(CardHeader, null, /*#__PURE__*/React.createElement(CardTitle, null, "\u6700\u8FD1\u306E Run"), /*#__PURE__*/React.createElement(CardDescription, null, "\u76F4\u8FD1 24 \u6642\u9593")), /*#__PURE__*/React.createElement(CardContent, null, /*#__PURE__*/React.createElement(DataTable, {
    ariaLabel: "\u6700\u8FD1\u306E Run",
    rows: runs,
    onRowClick: () => {},
    columns: [{
      key: "id",
      header: "Run ID",
      mono: true
    }, {
      key: "agent",
      header: "Agent"
    }, {
      key: "status",
      header: "状態",
      render: r => badge(r.status)
    }, {
      key: "duration",
      header: "所要時間",
      align: "right"
    }, {
      key: "started",
      header: "開始日時",
      render: r => muted(r.started)
    }]
  }))), /*#__PURE__*/React.createElement(Card, null, /*#__PURE__*/React.createElement(CardHeader, null, /*#__PURE__*/React.createElement(CardTitle, null, "\u627F\u8A8D\u5F85\u3061"), /*#__PURE__*/React.createElement(CardDescription, null, "\u9AD8\u30EA\u30B9\u30AF\u64CD\u4F5C\u306E\u627F\u8A8D")), /*#__PURE__*/React.createElement(CardContent, null, /*#__PURE__*/React.createElement(EmptyState, {
    title: "\u627F\u8A8D\u5F85\u3061\u306F\u3042\u308A\u307E\u305B\u3093",
    hint: "\u65B0\u3057\u3044\u627F\u8A8D\u4F9D\u983C\u306F\u3053\u3053\u306B\u8868\u793A\u3055\u308C\u307E\u3059\u3002"
  }))))));
}
const PRODUCTS = {
  RAG: {
    home: "/files",
    screen: RagFiles,
    account: {
      name: "山田 花子",
      roles: "管理者"
    },
    sections: [{
      key: "rag",
      title: "検索・回答プロファイル",
      items: [{
        href: "/search",
        label: "RAG 検索",
        icon: "FileSearch"
      }, {
        href: "/chat",
        label: "チャット",
        icon: "MessagesSquare"
      }, {
        href: "/search-answer-profiles",
        label: "検索・回答プロファイル (Search Answer Profile)",
        sidebarLabel: "検索・回答プロファイル",
        icon: "LayoutGrid"
      }, {
        href: "/evaluation",
        label: "品質評価",
        icon: "FlaskConical"
      }, {
        href: "/feedback",
        label: "フィードバック",
        icon: "MessageSquareHeart"
      }]
    }, {
      key: "ingestion",
      title: "ナレッジ構築",
      items: [{
        href: "/dashboard",
        label: "ダッシュボード",
        icon: "LayoutDashboard"
      }, {
        href: "/upload",
        label: "文書アップロード",
        sidebarLabel: "アップロード",
        icon: "Upload"
      }, {
        href: "/files",
        label: "文書インデックス",
        icon: "FileStack"
      }, {
        href: "/knowledge-bases",
        label: "ナレッジベース",
        icon: "Library"
      }]
    }, {
      key: "pipeline",
      title: "検索・回答設定",
      collapsed: true,
      items: [{
        href: "/settings/retrieval",
        label: "検索方法",
        icon: "Search"
      }]
    }, {
      key: "settings",
      title: "システム設定",
      collapsed: true,
      items: [{
        href: "/settings/model",
        label: "モデル設定",
        sidebarLabel: "モデル",
        icon: "BrainCog"
      }]
    }]
  },
  NL2SQL: {
    home: "/query",
    screen: Nl2sqlQuery,
    account: {
      name: "佐藤 健",
      roles: "データ管理者 · 利用者"
    },
    sections: [{
      key: "use",
      title: "AI 活用",
      items: [{
        href: "/query",
        label: "SQL 生成",
        icon: "Sparkles"
      }, {
        href: "/direct-sql",
        label: "SELECT SQL を実行",
        icon: "FileCode2"
      }, {
        href: "/sql-to-question",
        label: "SQL から質問を生成",
        icon: "MessageSquareCode"
      }, {
        href: "/history",
        label: "実行履歴",
        icon: "History"
      }]
    }, {
      key: "prepare",
      title: "データ準備",
      items: [{
        href: "/tables",
        label: "テーブルの管理",
        icon: "Table2"
      }, {
        href: "/views",
        label: "ビューの管理",
        icon: "Eye"
      }, {
        href: "/data",
        label: "データの管理",
        icon: "FileSpreadsheet"
      }]
    }, {
      key: "improve",
      title: "改善・運用",
      collapsed: true,
      items: [{
        href: "/profiles",
        label: "業務プロファイル",
        icon: "UserCog"
      }]
    }, {
      key: "security",
      title: "セキュリティ管理",
      collapsed: true,
      items: [{
        href: "/security/users",
        label: "ユーザー管理",
        icon: "Users"
      }]
    }, {
      key: "settings",
      title: "システム設定",
      collapsed: true,
      items: [{
        href: "/settings/model",
        label: "モデル",
        icon: "BrainCog"
      }]
    }]
  },
  Agent: {
    home: "/dashboard",
    screen: AgentDashboard,
    account: {
      name: "鈴木 一郎",
      roles: "運用管理者"
    },
    sections: [{
      key: "overview",
      title: "概要",
      items: [{
        href: "/dashboard",
        label: "ダッシュボード",
        icon: "LayoutDashboard"
      }]
    }, {
      key: "control",
      title: "Control Plane",
      items: [{
        href: "/agents",
        label: "業務 Agent",
        icon: "Bot"
      }, {
        href: "/skills",
        label: "スキル (Skills)",
        icon: "Sparkles"
      }, {
        href: "/runtimes",
        label: "Runtime",
        icon: "Server"
      }, {
        href: "/runs",
        label: "Run",
        icon: "Play"
      }, {
        href: "/approvals",
        label: "承認・監査",
        icon: "ShieldCheck"
      }]
    }, {
      key: "settings",
      title: "システム設定",
      collapsed: true,
      items: [{
        href: "/settings/connection",
        label: "Agent 接続設定",
        icon: "Plug"
      }]
    }]
  }
};
window.ProductShells = {
  PRODUCTS
};
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/product-shells/screens.jsx", error: String((e && e.message) || e) }); }

__ds_ns.TONE_ICON = __ds_scope.TONE_ICON;

__ds_ns.Banner = __ds_scope.Banner;

__ds_ns.Button = __ds_scope.Button;

__ds_ns.Card = __ds_scope.Card;

__ds_ns.CardHeader = __ds_scope.CardHeader;

__ds_ns.CardTitle = __ds_scope.CardTitle;

__ds_ns.CardDescription = __ds_scope.CardDescription;

__ds_ns.CardContent = __ds_scope.CardContent;

__ds_ns.Icon = __ds_scope.Icon;

__ds_ns.Skeleton = __ds_scope.Skeleton;

__ds_ns.Spinner = __ds_scope.Spinner;

__ds_ns.StatusBadge = __ds_scope.StatusBadge;

__ds_ns.Switch = __ds_scope.Switch;

__ds_ns.ToggleChip = __ds_scope.ToggleChip;

__ds_ns.DataTable = __ds_scope.DataTable;

__ds_ns.Pagination = __ds_scope.Pagination;

__ds_ns.ConfirmDialog = __ds_scope.ConfirmDialog;

__ds_ns.LoadingState = __ds_scope.LoadingState;

__ds_ns.ErrorState = __ds_scope.ErrorState;

__ds_ns.EmptyState = __ds_scope.EmptyState;

__ds_ns.StateViews = __ds_scope.StateViews;

__ds_ns.ToastRegion = __ds_scope.ToastRegion;

__ds_ns.Toast = __ds_scope.Toast;

__ds_ns.FieldError = __ds_scope.FieldError;

__ds_ns.FormStatus = __ds_scope.FormStatus;

__ds_ns.SelectField = __ds_scope.SelectField;

__ds_ns.TextField = __ds_scope.TextField;

__ds_ns.AppShell = __ds_scope.AppShell;

__ds_ns.PageBody = __ds_scope.PageBody;

__ds_ns.Section = __ds_scope.Section;

__ds_ns.Breadcrumbs = __ds_scope.Breadcrumbs;

__ds_ns.PageHeader = __ds_scope.PageHeader;

__ds_ns.Sidebar = __ds_scope.Sidebar;

__ds_ns.SidebarAccountFooter = __ds_scope.SidebarAccountFooter;

__ds_ns.Tabs = __ds_scope.Tabs;

__ds_ns.TabPanel = __ds_scope.TabPanel;

})();
