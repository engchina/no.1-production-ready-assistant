import { ChatProgress, RunProcessDetails, useTraceDetailsLabels, type ChatProgressStep } from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { AnswerDetailsPanel } from "./AnswerDetailsPanel";
import { SavedAnswerRecord } from "./SavedAnswerRecord";

/**
 * 回答（チャット・RAG 検索）の「処理の経過」（#2096）。3 製品共通の `ChatProgress`（工程の一覧。実行中は開いて
 * 行が増え、今の行に Spinner と経過時間）と、その中の末尾の「処理の詳細」（回答の記録の詳細。技術的な詳細は既定で
 * 閉じる。messaging.md §10）を 1 か所にまとめる。
 *
 * 工程の行は回答の trace（span の木）の投影で、共通の span の表の snapshot から作る（#3226 の案 A）。実行中も読み直す
 * たびに行が増え、2 つ目の表示（「トレースの詳細」）は出さない。trace id は「処理の詳細」の先頭に出す（`traceId`）。
 * span の無い古い形式の記録（`legacy`）は、処理の経過の位置に古い形式の案内を出す（空白にしない）。書けなかった span
 * がある記録（`partial`）は、開いた中身の末尾に「一部の処理だけを記録しています。」を出す。
 *
 * 「処理の詳細」は回答の補足ではなく処理の経過に属する技術的な詳細なので、処理の経過を開いたときだけタイムラインの
 * 後ろに出し、閉じると一緒に隠れる（#2325。回答の本文との間に独立して置かない）。段階の記録の無い回答（段階が 0 件）は
 * 処理の経過の行が無いので、「処理の詳細」を単独で出す。
 *
 * チャットと RAG 検索は、この 1 つの部品で同じ位置（回答・検索結果の上。#2115）・同じ開閉・同じ「処理の詳細」を出す
 * （#2118。チャットの形にそろえる）。工程の一覧は実行中は開き、完了したら閉じる（`ChatProgress` の既定。3 製品で同じ。#2265）。
 * 「処理の詳細」は回答の記録の診断（`diagnostics`）があればその詳細、無ければ
 * 保存済みの回答の記録（`savedRecord`。会話を開き直したとき）を読み直して出す。回答の記録の削除はチャット・RAG 検索では
 * 出さない（#2325）。
 */
export function AnswerProcess({
  steps,
  active,
  startedAt,
  elapsedMs,
  reconnecting,
  progressKey,
  diagnostics,
  traceId,
  legacy = false,
  partial = false,
  savedRecord,
  conversationId = null,
  messageId = null,
  testId,
  className,
}: {
  conversationId?: string | null;
  messageId?: string | null;
  steps: ChatProgressStep[];
  /** 処理中か。省略時は段階から決める。 */
  active?: boolean;
  /** 処理全体の開始（実行中の経過時間の起点）。 */
  startedAt?: string | number | null;
  /** 完了後の全体の所要時間。省略時は段階の時刻から。 */
  elapsedMs?: number | null;
  reconnecting?: boolean;
  progressKey?: string | null;
  /** 回答の記録の診断（`diagnostics.answer`）。完了後の「処理の詳細」に出す。実行中は渡さない。 */
  diagnostics?: unknown;
  /** 処理の経過の trace id（span のイベントの `trace_id`。「処理の詳細」に出す）。 */
  traceId?: string;
  /** 処理の経過が span の無い古い形式の記録か（工程の行の代わりに案内を出す）。 */
  legacy?: boolean;
  /** 書けなかった span がある（一部の処理だけを記録している）。 */
  partial?: boolean;
  /** 診断の無い保存済みの回答（会話を開き直したとき）。回答の記録を読み直して「処理の詳細」に出す。 */
  savedRecord?: { traceId: string; searchAnswerProfileId: string } | null;
  /** `ChatProgress` の testId（`-summary` / `-current` / `-step-<id>` など）。囲みは `-process`、「処理の詳細」は `-details`。 */
  testId?: string;
  className?: string;
}) {
  const traceLabels = useTraceDetailsLabels();
  const details = diagnostics ? (
    <AnswerDetailsPanel
      diagnostics={diagnostics}
    />
  ) : savedRecord ? (
    <SavedAnswerRecord
      traceId={savedRecord.traceId}
      searchAnswerProfileId={savedRecord.searchAnswerProfileId}
      showAnswer={false}
      showCitations={false}
    />
  ) : null;
  // 古い形式の記録は工程の行を作らない（段だけのイベントから段を組み立て直さない）。
  const rows = legacy ? [] : steps;
  if (rows.length === 0 && !details && !traceId && !legacy) return null;
  // 実行の詳細の「処理の経過」の節と同じ共有の部品・名前（`RunProcessDetails`。#2325）。
  const detailsDisclosure = details || traceId ? (
    <RunProcessDetails
      traceId={traceId}
      metadata={{ conversationId, messageId }}
      title={t("answer.process.details")}
      summaryTestId={testId ? `${testId}-details` : undefined}
    >
      {details}
    </RunProcessDetails>
  ) : null;
  return (
    <section
      className={className}
      aria-label={t("answer.process.title")}
      data-testid={testId ? `${testId}-process` : undefined}
    >
      {rows.length > 0 ? (
        <ChatProgress
          steps={rows}
          active={active}
          startedAt={startedAt}
          elapsedMs={elapsedMs}
          reconnecting={reconnecting}
          progressKey={progressKey}
          // 表に書けなかった span がある（#3226。3 製品共通の案内を段の一覧の後ろに出す）。
          partial={partial}
          footer={detailsDisclosure}
          testId={testId}
        />
      ) : (
        <>
          {legacy ? (
            <p className="text-sm text-fg-muted" data-testid={testId ? `${testId}-legacy` : undefined}>
              {traceLabels.legacy}
            </p>
          ) : null}
          {detailsDisclosure}
        </>
      )}
    </section>
  );
}
