import type { LucideIcon } from "lucide-react";
import type { ComponentType, ReactNode } from "react";

import { cn } from "../../lib/utils";
import { BUTTON_ICON_SIZE, buttonVariants, type ButtonVariantToneProps } from "./button";

/**
 * ルーター非依存のリンク部品。react-router の `Link`（`to` を取る）をそのまま渡せる。
 * Breadcrumbs の `NavLinkComponent` と同じ考え方で、`packages/ui` はルーターに依存しない。
 */
export type ButtonLinkComponent = ComponentType<{
  to: string;
  className?: string;
  children: ReactNode;
  "aria-label"?: string;
  "data-testid"?: string;
}>;

export type ButtonLinkProps = ButtonVariantToneProps & {
  /** 移動先のパス（または URL）。 */
  to: string;
  /**
   * 画面の中の移動に使うリンク（react-router の `Link` など）。省略すると `<a href>` を描く
   * （ページの読み込みを伴う移動・外部への移動）。
   */
  linkComponent?: ButtonLinkComponent;
  /** 高さ（Button と同じ sm 32px / md 36px / lg 40px。タッチ端末は 44px）。同じ行の部品と同じ size にする。 */
  size?: "sm" | "md" | "lg";
  /** 先頭アイコン（lucide-react のコンポーネント。例: `icon={LockKeyhole}`）。子要素にアイコンを書かない。 */
  icon?: LucideIcon;
  /** 方向・外部リンクだけ（ArrowRight / ExternalLink）。アイコンを 2 つ持たせない。 */
  trailingIcon?: LucideIcon;
  /** 翻訳済みのラベル。 */
  children: ReactNode;
  className?: string;
  /** 画面の文脈を含む読み上げ名（ラベルだけでは移動先が分からないとき）。 */
  "aria-label"?: string;
  testId?: string;
};

/**
 * ボタンの見た目の「移動」（#800）。画面を移るだけの操作（別の画面で設定する導線など）は、
 * `<button onClick={navigate}>` ではなくリンクにする（新しいタブで開ける・読み上げで「リンク」と分かる）。
 * 見た目・寸法・アイコンの寸法は `Button` と同じで、`<Link className={buttonVariants()}>` の子にアイコンを
 * 手書きしない（アイコンは `icon` / `trailingIcon` で渡す）。loading / disabled は持たない
 * （移動は待ちが無く、移動できないときはリンクを出さない）。
 */
export function ButtonLink({
  to,
  linkComponent: LinkComponent,
  variant = "secondary",
  size,
  tone,
  icon: Icon,
  trailingIcon: TrailingIcon,
  children,
  className,
  "aria-label": ariaLabel,
  testId,
}: ButtonLinkProps) {
  const classes = cn(buttonVariants({ variant, size, tone }), "no-underline", className);
  const content = (
    <>
      {Icon ? <Icon size={BUTTON_ICON_SIZE} aria-hidden /> : null}
      <span>{children}</span>
      {TrailingIcon ? <TrailingIcon size={BUTTON_ICON_SIZE} aria-hidden /> : null}
    </>
  );
  if (LinkComponent) {
    return (
      <LinkComponent to={to} className={classes} aria-label={ariaLabel} data-testid={testId}>
        {content}
      </LinkComponent>
    );
  }
  return (
    <a href={to} className={classes} aria-label={ariaLabel} data-testid={testId}>
      {content}
    </a>
  );
}
