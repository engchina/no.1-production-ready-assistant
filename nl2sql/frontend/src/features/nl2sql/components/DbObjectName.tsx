import { IdentifierText } from "@/components/IdentifierText";

import { formatDbObjectName, type DbObjectNameSource } from "../dbObjectIdentity";

const SIZE_CLASS = {
  xs: "text-xs",
  sm: "text-sm",
  base: "text-base",
} as const;

export type DbObjectNameSize = keyof typeof SIZE_CLASS;

type DbObjectNameProps = {
  /** 一覧・バッジは xs、文中は sm、見出しは base。 */
  size?: DbObjectNameSize;
  /**
   * 押せる要素（選択肢・チェックボックスのラベル・リンク）の中に置くときだけ true にする。
   * accent 色は「押せる」ことを示すため、見出し・バッジ・確認ダイアログ・結果表示では使わない。
   * 一覧の行の題名（共有の `RowTitleButton`）の中では使わない（3 製品で題名の色をそろえる。#421）。
   */
  interactive?: boolean;
  /**
   * 1 行密度のリスト（スキーマ参照・許可オブジェクト選択）では折り返さず末尾を省略する。
   * 省略時も全文は title で確認できる。`<wbr>` は nowrap でも改行位置になるため、この場合は使わない。
   */
  truncate?: boolean;
  className?: string;
  "data-testid"?: string;
} & (
  | { object: DbObjectNameSource; value?: never }
  | { /** `formatDbObjectName` 済みの文字列。 */ value: string; object?: never }
);

/**
 * 表・ビュー名を `OWNER.OBJECT` 形式で表示する。等幅・semibold で、`.` と `_` の後で折り返す。
 * 文字列が必要な場面（トースト・aria-label・確認語）では `formatDbObjectName` を使う。
 */
export function DbObjectName({
  object,
  value,
  size = "sm",
  interactive = false,
  truncate = false,
  className = "",
  "data-testid": testId,
}: DbObjectNameProps) {
  const text = object ? formatDbObjectName(object) : (value ?? "");
  const classes = [
    "font-mono font-semibold",
    SIZE_CLASS[size],
    interactive ? "text-accent-fg" : "text-fg",
    truncate ? "truncate" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  if (truncate) {
    return (
      <span className={classes} title={text} data-testid={testId}>
        {text}
      </span>
    );
  }
  return <IdentifierText value={text} data-testid={testId} className={classes} />;
}
