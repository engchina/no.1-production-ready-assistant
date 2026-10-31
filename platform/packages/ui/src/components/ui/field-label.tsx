import { type ReactNode, useId } from "react";

import { cn } from "../../lib/utils";

import { FieldError } from "./field-error";
import { DEFAULT_REQUIRED_LABEL, RequiredBadge } from "./required-badge";

/**
 * TextField / SelectField / SecretField で表せない入力（textarea・ファイル選択・独自の入力・チェックボックスの群・
 * ラジオ・複数選択）のラベルと必須表示（#531）。
 *
 * 必須の表示は 3 製品で次の 1 通りにそろえる（docs/design-system/README.md §4「必須の表示」）。
 * - 必須の欄だけ、ラベルの後ろに中立色のテキストタグ「必須」（RequiredBadge）を付ける。任意の欄には何も付けない。
 * - 支援技術には入力の aria-required（またはネイティブの required）で伝え、タグは二重に読み上げない。
 *   aria-required を持てない群（チェックボックスの群・fieldset）では、legend の中のタグを読み上げる。
 */

type RequiredProps = {
  /** 必須の欄。ラベルの後ろに RequiredBadge を出す。 */
  required?: boolean;
  /** タグの文言。既定「必須」。条件付きの必須（例:「OCI 運用時必須」）だけ上書きする。 */
  requiredLabel?: string;
  /**
   * 入力側（またはグループ）が aria-required / required で必須を伝えているか。
   * true のときタグを aria-hidden にして「必須、必須」の二重読み上げを避ける。
   * FieldLabel の既定は true（入力に aria-required を付ける前提）、FieldLegend の既定は false（fieldset は aria-required を持てない）。
   */
  requiredAnnouncedByControl?: boolean;
};

/**
 * 入力のラベル（`<label htmlFor>`）。必須のときは RequiredBadge を添える。
 * 対応する入力には `aria-required`（または `required`）を付ける。付けられない独自の入力（グリッド選択など）では
 * `requiredAnnouncedByControl={false}` でタグを読み上げ対象に残す。
 */
export function FieldLabel({
  id,
  htmlFor,
  label,
  required = false,
  requiredLabel = DEFAULT_REQUIRED_LABEL,
  requiredAnnouncedByControl = true,
  className,
  children,
}: {
  id?: string;
  htmlFor: string;
  /** 翻訳済みのラベル。 */
  label: ReactNode;
  className?: string;
  /** タグの後ろに置く要素（補足のリンクなど）。 */
  children?: ReactNode;
} & RequiredProps) {
  return (
    <label id={id} htmlFor={htmlFor} className={cn("text-sm font-medium text-fg", className)}>
      {label}
      {required ? (
        <RequiredBadge
          label={requiredLabel}
          aria-hidden={requiredAnnouncedByControl}
          className="ml-2 align-middle"
        />
      ) : null}
      {children}
    </label>
  );
}

/**
 * fieldset の見出し（`<legend>`）。必須のときは RequiredBadge を添える。
 * legend はグループの名前として読まれ、fieldset（role=group）は aria-required を持てないので、既定ではタグを読み上げる。
 * `role="radiogroup"` の fieldset に aria-required を付けた場合だけ `requiredAnnouncedByControl` を渡す（Fieldset は自動で渡す）。
 */
export function FieldLegend({
  id,
  children,
  required = false,
  requiredLabel = DEFAULT_REQUIRED_LABEL,
  requiredAnnouncedByControl = false,
  className,
}: {
  id?: string;
  /** 翻訳済みの見出し。 */
  children: ReactNode;
  className?: string;
} & RequiredProps) {
  return (
    <legend id={id} className={cn("text-sm font-semibold text-fg", className)}>
      {children}
      {required ? (
        <RequiredBadge
          label={requiredLabel}
          aria-hidden={requiredAnnouncedByControl}
          className="ml-2 align-middle"
        />
      ) : null}
    </legend>
  );
}

export type FieldsetProps = {
  id?: string;
  /** 翻訳済みの見出し（legend）。 */
  legend: ReactNode;
  /** 翻訳済みの補足。legend の直下に出し、fieldset の aria-describedby と結ぶ。 */
  helper?: ReactNode;
  /** 翻訳済みのエラー（例:「〇〇を選択してください。」）。群の直下に FieldError で出す。 */
  error?: string | null;
  /**
   * `radiogroup` を渡すと fieldset を role=radiogroup にし、必須を aria-required で伝える（タグは読み上げない）。
   * 省略時は role=group（チェックボックスの群・複合入力）で、必須は legend の中のタグで伝える。
   */
  role?: "radiogroup";
  className?: string;
  legendClassName?: string;
  children: ReactNode;
} & Omit<RequiredProps, "requiredAnnouncedByControl">;

/**
 * チェックボックスの群・ラジオ・複数選択・複合入力を囲む fieldset。見出し・必須の表示・補足・エラーを 1 か所で組む。
 * 選択肢のレイアウト（縦並び・グリッド・チップ）は children で自由に組む。
 */
export function Fieldset({
  id,
  legend,
  helper,
  error,
  required = false,
  requiredLabel = DEFAULT_REQUIRED_LABEL,
  role,
  className,
  legendClassName,
  children,
}: FieldsetProps) {
  const reactId = useId();
  const baseId = id ?? `fieldset-${reactId}`;
  const hintId = `${baseId}-hint`;
  const errorId = `${baseId}-error`;
  const describedBy = [helper ? hintId : "", error ? errorId : ""].filter(Boolean).join(" ") || undefined;
  const announcedByGroup = role === "radiogroup";

  return (
    <fieldset
      id={id}
      role={role}
      aria-required={announcedByGroup && required ? true : undefined}
      aria-invalid={announcedByGroup && error ? true : undefined}
      aria-describedby={describedBy}
      className={cn("min-w-0 space-y-2", className)}
    >
      <FieldLegend
        required={required}
        requiredLabel={requiredLabel}
        requiredAnnouncedByControl={announcedByGroup}
        className={legendClassName}
      >
        {legend}
      </FieldLegend>
      {helper ? (
        <p id={hintId} className="text-xs leading-relaxed text-fg-muted">
          {helper}
        </p>
      ) : null}
      {children}
      <FieldError id={errorId} message={error} />
    </fieldset>
  );
}
