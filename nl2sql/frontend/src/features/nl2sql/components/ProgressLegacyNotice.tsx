import { useTraceDetailsLabels } from "@production-ready/ui";

/**
 * span の無い古い形式の記録の処理の経過（#3226）。処理の経過の位置に共通の案内の文
 * （「この実行は古い形式の記録のため、処理の経過を表示できません。」）を出し、空白にしない。
 */
export function ProgressLegacyNotice({ testId }: { testId?: string }) {
  const labels = useTraceDetailsLabels();
  return (
    <p className="text-xs leading-5 text-fg-muted" data-testid={testId}>
      {labels.legacy}
    </p>
  );
}
