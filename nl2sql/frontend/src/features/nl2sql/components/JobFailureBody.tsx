import {
  ApiErrorDetailList,
  DEFAULT_API_ERROR_DETAIL_LABELS,
  MessageText,
  apiErrorDetail,
} from "@production-ready/ui";

/**
 * NL2SQL のジョブの失敗の本文（失敗の面の Banner の中に置く）。
 *
 * 1 文目は backend の利用者向けの文（何が起きたか・次の操作）、エラーコードと例外・Oracle のエラーの元の文
 * （`error_detail`）は「詳細」に畳み、失敗なので開いて出す（UX 契約 messaging.md §10.3。#1072）。
 */
export function JobFailureBody({
  message,
  errorCode,
  errorDetail,
}: {
  message: string;
  errorCode?: string | null;
  errorDetail?: string | null;
}) {
  const details = [
    ...apiErrorDetail(DEFAULT_API_ERROR_DETAIL_LABELS.errorCode, errorCode),
    ...apiErrorDetail(DEFAULT_API_ERROR_DETAIL_LABELS.rawMessage, errorDetail),
  ];
  return (
    <div className="min-w-0 space-y-2" data-testid="nl2sql-job-failure">
      <p>
        <MessageText text={message} />
      </p>
      <ApiErrorDetailList details={details} />
    </div>
  );
}
