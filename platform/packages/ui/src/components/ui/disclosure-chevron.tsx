import { ChevronDown, type LucideProps } from "lucide-react";

import { cn } from "../../lib/utils";

export interface DisclosureChevronProps extends Omit<LucideProps, "aria-hidden"> {
  /**
   * 開閉の状態。`Disclosure` と button + region（`aria-expanded`）の開閉は boolean を渡す。
   * `"group"` は `group/disclosure` を付けた `<details>` の開閉に CSS で追従する旧来の書き方
   * （入れ子の details では外側の open に引きずられるため、新規コードは `Disclosure` を使う）。
   * 展開時は下向き、折りたたみ時は右向きで統一する（#397。Sidebar のセクションとネイティブの ▸ / ▾ と同じ）。
   */
  expanded: boolean | "group";
}

export function DisclosureChevron({
  expanded,
  className,
  ...props
}: DisclosureChevronProps) {
  const stateClass =
    expanded === "group"
      ? "-rotate-90 group-open/disclosure:rotate-0"
      : expanded
        ? "rotate-0"
        : "-rotate-90";

  return (
    <ChevronDown
      {...props}
      className={cn(
        "shrink-0 transition-transform duration-200 ease-out motion-reduce:transition-none",
        stateClass,
        className
      )}
      data-state={
        expanded === "group" ? undefined : expanded ? "expanded" : "collapsed"
      }
      aria-hidden="true"
      focusable="false"
    />
  );
}
