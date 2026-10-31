import { StatusBadge } from "@production-ready/ui";

import type { SupportGuideIssue } from "@/lib/api";
import { t } from "@/lib/i18n";
import { issuePathLabel } from "@/lib/support-guide-form";

/** 業務ガイドの検証の結果の一覧（問題 / 注意を文字でも示し、位置を利用者の言葉で添える）。 */
export function SupportGuideIssueList({
  issues,
  testId,
}: {
  issues: readonly SupportGuideIssue[];
  testId?: string;
}) {
  if (issues.length === 0) return null;
  return (
    <ul className="space-y-1.5" data-testid={testId}>
      {issues.map((issue, index) => {
        const where = issuePathLabel(issue.path);
        return (
          <li
            key={`${issue.path}-${issue.code}-${index}`}
            className="flex min-w-0 flex-wrap items-start gap-x-2 gap-y-1 text-sm"
            data-severity={issue.severity}
          >
            <StatusBadge
              variant={issue.severity === "error" ? "danger" : "warning"}
              label={t(
                issue.severity === "error"
                  ? "supportGuides.issue.severity.error"
                  : "supportGuides.issue.severity.warning",
              )}
            />
            <span className="min-w-0 flex-1 break-words text-fg">
              {where ? <span className="font-medium">{where}: </span> : null}
              {issue.message}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
