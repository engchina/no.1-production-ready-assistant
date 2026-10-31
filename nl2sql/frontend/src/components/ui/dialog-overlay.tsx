import { type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { cn } from "@production-ready/ui";

interface DialogOverlayPortalProps {
  children: ReactNode;
  className?: string;
  onMouseDown?: (event: MouseEvent<HTMLDivElement>) => void;
  testId?: string;
}

const overlayBaseClass =
  "animate-overlay-in fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-[var(--scrim)]";
const overlayDefaultLayoutClass = "p-3 sm:items-center sm:p-4";

/**
 * App-wide modal overlay.
 * document.body 直下へ出すことで、main の contain/scroll に閉じ込められず viewport 全体を覆う。
 */
export function DialogOverlayPortal({
  children,
  className = overlayDefaultLayoutClass,
  onMouseDown,
  testId = "app-dialog-overlay",
}: DialogOverlayPortalProps) {
  return createPortal(
    <div
      role="presentation"
      data-testid={testId}
      className={cn(overlayBaseClass, className)}
      onMouseDown={onMouseDown}
    >
      {children}
    </div>,
    document.body
  );
}
