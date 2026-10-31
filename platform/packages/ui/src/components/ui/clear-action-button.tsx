import { X } from "lucide-react";

import { cn } from "../../lib/utils";
import { Button, type ButtonProps } from "./button";

export interface ClearActionButtonProps
  extends Omit<ButtonProps, "aria-label" | "children" | "type" | "variant"> {
  ariaLabel?: string;
  dataTestId?: string;
  label: string;
}

/**
 * 検索語・条件を消す「クリア」ボタン（secondary・`X`）。既定は sm（空の状態の「検索語をクリア」など）。入力欄・ボタンと同じ行に置くときは、その行と同じ `size` を渡す。
 * 以前はマウス環境でも 44px にしていたが、同じ行の入力欄とずれるためやめた（#613）。
 */
export function ClearActionButton({
  ariaLabel,
  className,
  dataTestId,
  label,
  size = "sm",
  ...props
}: ClearActionButtonProps) {
  return (
    <Button
      type="button"
      variant="secondary"
      size={size}
      className={cn("whitespace-nowrap", className)}
      aria-label={ariaLabel ?? label}
      data-testid={dataTestId}
      {...props} icon={X}>
      <span>{label}</span>
    </Button>
  );
}
