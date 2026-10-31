import { ProcessingIndicator } from "@production-ready/ui";
import { ArrowUpCircle, CheckCircle2, Clock3 } from "lucide-react";

import { t } from "@/lib/i18n";
import {
  formatByteSize,
  uploadFileProgresses,
  uploadProgressPercent,
  type UploadFileProgress,
  type UploadFileSendState,
  type UploadProgress,
} from "@/lib/upload-requests";

/**
 * 送信中の表示（#306）。件数と経過時間（`ProcessingIndicator`）に、送信済み / 合計のバイト数と割合を添える。
 * 送り終えた後は保存先への保存・文書の登録を待っていることを示す（100% のまま止まって見えないように）。
 * 複数のファイルを送るときは、ファイルごとの送信済み / 大きさと状態も示す。
 */
export function UploadSendingState({
  fileCount,
  files = [],
  progress,
}: {
  fileCount: number;
  /** 送るファイル（送る順。上限を超えて送らないファイルは除く）。 */
  files?: readonly Pick<File, "name" | "size">[];
  progress: UploadProgress | null;
}) {
  const percent = progress ? uploadProgressPercent(progress) : 0;
  const hasBytes = progress !== null && progress.totalBytes > 0;
  const summary = hasBytes
    ? t("upload.progress.sent", {
        sent: formatByteSize(progress.sentBytes),
        total: formatByteSize(progress.totalBytes),
        percent,
      })
    : "";
  return (
    <div className="grid min-w-0 gap-2" data-testid="upload-sending">
      <ProcessingIndicator
        active
        placement="action"
        label={t("upload.uploading", { count: fileCount })}
        testId="upload-processing"
        // 大きなファイルの送信は 10 秒を超えるのが普通。進み具合を出すときは遅延の案内を出さない。
        showSlowMessage={!hasBytes}
      />
      {hasBytes ? (
        <div className="grid min-w-0 gap-1.5" data-testid="upload-progress">
          <div
            data-surface-tint="neutral"
            className="h-2 overflow-hidden rounded-full bg-surface-hover"
            role="progressbar"
            aria-label={t("upload.progress.aria")}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            aria-valuetext={summary}
          >
            {/* 割合だけは送信の進み具合で決まるため inline style にする（色・高さはトークン）。
                幅ではなく transform で伸ばし、layout を動かさない。 */}
            <div
              className="h-full w-full origin-left bg-accent-emphasis transition-transform duration-300 motion-reduce:transition-none"
              style={{ transform: `scaleX(${percent / 100})` }}
            />
          </div>
          <p className="tnum text-xs text-fg-muted" data-testid="upload-progress-bytes">
            {summary}
          </p>
          {percent >= 100 ? (
            <p className="text-xs text-fg-muted" data-testid="upload-progress-saving">
              {t("upload.progress.saving")}
            </p>
          ) : null}
          {files.length > 1 ? (
            <UploadFileProgressList items={uploadFileProgresses(files, progress.sentBytes)} />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

const FILE_STATE_LABEL_KEYS = {
  waiting: "upload.progress.state.waiting",
  sending: "upload.progress.state.sending",
  sent: "upload.progress.state.sent",
} as const satisfies Record<UploadFileSendState, string>;

/**
 * ファイルごとの送信状況（#306）。状態は色だけでなくアイコンと文言で示す。件数が多いときは一覧の中で
 * 縦にスクロールし、ページを伸ばさない。進み具合の読み上げは全体の progressbar に任せ、ここは live にしない。
 */
function UploadFileProgressList({ items }: { items: UploadFileProgress[] }) {
  return (
    <ul
      className="bounded-scroll-area mt-1 divide-y divide-border overflow-y-auto rounded-md border border-border bg-surface-sunken"
      aria-label={t("upload.progress.files")}
      data-testid="upload-file-progress"
    >
      {items.map((item, index) => (
        // 同じ名前のファイルを選べるため、送る順の位置を key に含める。
        <li
          key={`${index}-${item.name}`}
          className="flex min-w-0 items-start gap-2 px-3 py-2"
          data-state={item.state}
          data-testid="upload-file-progress-item"
        >
          <span className="mt-0.5 flex shrink-0">
            <FileSendStateIcon state={item.state} />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-0.5 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
            <span className="truncate text-sm text-fg" title={item.name}>
              {item.name}
            </span>
            <span className="tnum shrink-0 text-xs text-fg-muted">
              {t(FILE_STATE_LABEL_KEYS[item.state])}
              {" · "}
              {t("upload.progress.fileBytes", {
                sent: formatByteSize(item.sentBytes),
                total: formatByteSize(item.totalBytes),
              })}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function FileSendStateIcon({ state }: { state: UploadFileSendState }) {
  switch (state) {
    case "sending":
      // 回るスピナーは上の送信全体の表示の 1 つだけにする。送信中のファイルは静止したアイコンと文言で示す
      // （同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
      return <ArrowUpCircle size={16} className="text-accent-fg" aria-hidden />;
    case "sent":
      return <CheckCircle2 size={16} className="text-success-fg" aria-hidden />;
    default:
      return <Clock3 size={16} className="text-fg-muted" aria-hidden />;
  }
}
