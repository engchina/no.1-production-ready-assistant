import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

export function ContentActionBar({
  actionsClassName,
  ariaLabel,
  children,
  className,
  description,
  leading,
  meta,
  testId,
  title,
}: {
  actionsClassName?: string;
  ariaLabel: string;
  children: ReactNode;
  className?: string;
  description?: ReactNode;
  leading?: ReactNode;
  meta?: ReactNode;
  testId?: string;
  title?: ReactNode;
}) {
  const hasInfo = leading || title || description || meta;
  const infoClassName = "min-w-0 max-w-full flex-1 basis-64";

  return (
    <div
      className={cn(
        "flex min-w-0 max-w-full flex-wrap items-start justify-between gap-[8px]",
        !hasInfo && "items-center justify-end",
        className
      )}
      data-testid={testId}
    >
      {leading ? (
        <div className={infoClassName}>{leading}</div>
      ) : hasInfo ? (
        <div className={cn(infoClassName, "space-y-1")}>
          {title ? <div className="text-sm font-semibold text-fg">{title}</div> : null}
          {description ? (
            <div className="text-sm leading-6 text-fg-muted">{description}</div>
          ) : null}
          {meta ? <div className="text-xs leading-5 text-fg-muted">{meta}</div> : null}
        </div>
      ) : null}
      <div
        role="group"
        aria-label={ariaLabel}
        className={cn(
          "flex min-w-0 max-w-full shrink-0 flex-wrap items-center justify-end gap-[8px]",
          hasInfo && "ml-auto",
          !hasInfo && "w-full",
          actionsClassName
        )}
      >
        {children}
      </div>
    </div>
  );
}
