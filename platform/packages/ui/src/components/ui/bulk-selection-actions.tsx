import { CheckSquare, X } from "lucide-react";

import { Button, type ButtonProps } from "./button";
import { cn } from "../../lib/utils";

export interface BulkSelectionActionsProps {
  selectLabel: string;
  clearLabel: string;
  onSelectAll: () => void;
  onClearAll: () => void;
  selectAriaLabel?: string;
  clearAriaLabel?: string;
  selectDisabled?: boolean;
  clearDisabled?: boolean;
  busy?: boolean;
  size?: ButtonProps["size"];
  className?: string;
  dataTestId?: string;
}

export function BulkSelectionActions({
  selectLabel,
  clearLabel,
  onSelectAll,
  onClearAll,
  selectAriaLabel,
  clearAriaLabel,
  selectDisabled = false,
  clearDisabled = false,
  busy = false,
  size = "sm",
  className,
  dataTestId,
}: BulkSelectionActionsProps) {
  return (
    <div
      role="group"
      aria-busy={busy || undefined}
      className={cn("flex min-w-0 flex-wrap items-center justify-start gap-[8px]", className)}
      data-testid={dataTestId}
    >
      <Button
        type="button"
        variant="secondary"
        size={size}
        className="whitespace-nowrap"
        aria-label={selectAriaLabel ?? selectLabel}
        disabled={busy || selectDisabled}
        data-testid={dataTestId ? `${dataTestId}-select` : undefined}
        onClick={onSelectAll} icon={CheckSquare}>
        <span>{selectLabel}</span>
      </Button>
      <Button
        type="button"
        variant="ghost"
        size={size}
        className="whitespace-nowrap"
        aria-label={clearAriaLabel ?? clearLabel}
        disabled={busy || clearDisabled}
        data-testid={dataTestId ? `${dataTestId}-clear` : undefined}
        onClick={onClearAll} icon={X}>
        <span>{clearLabel}</span>
      </Button>
    </div>
  );
}
