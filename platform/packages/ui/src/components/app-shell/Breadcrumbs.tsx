import { Fragment } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "../../lib/utils";
import type { NavLinkComponent } from "../../navigation/types";

export interface BreadcrumbItem {
  label: string;
  /** リンク先。最後の項目（現在地）は省略する。 */
  href?: string;
}

/**
 * パンくず（3 階層以上の深い導線で現在地を示す。breadcrumb-web）。
 * リンクはルーター非依存のため `linkComponent` を注入。href の無い末尾は現在地として強調する。
 */
export function Breadcrumbs({
  items,
  linkComponent: Link,
  className,
  ariaLabel = "パンくず",
}: {
  items: BreadcrumbItem[];
  linkComponent: NavLinkComponent;
  className?: string;
  ariaLabel?: string;
}) {
  if (items.length === 0) return null;

  return (
    <nav aria-label={ariaLabel} className={cn("flex items-center text-xs text-fg-muted", className)}>
      <ol className="flex flex-wrap items-center gap-1">
        {items.map((item, index) => {
          const isLast = index === items.length - 1;
          return (
            <Fragment key={`${item.label}-${index}`}>
              <li className="flex items-center">
                {item.href && !isLast ? (
                  <Link to={item.href} className="rounded hover:text-fg hover:underline">
                    {item.label}
                  </Link>
                ) : (
                  <span
                    className={cn(isLast && "font-medium text-fg")}
                    aria-current={isLast ? "page" : undefined}
                  >
                    {item.label}
                  </span>
                )}
              </li>
              {!isLast ? (
                <li aria-hidden className="flex items-center text-fg-subtle">
                  <ChevronRight size={14} />
                </li>
              ) : null}
            </Fragment>
          );
        })}
      </ol>
    </nav>
  );
}
