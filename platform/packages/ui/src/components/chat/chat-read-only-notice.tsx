import { Square } from "lucide-react";
import type { ReactNode } from "react";

import { Banner } from "../ui/banner";
import { Button } from "../ui/button";

export interface ChatReadOnlyNoticeLabels {
  /** 案内の見出し（例:「他の利用者の会話です（閲覧のみ）」）。 */
  title: string;
  /** 案内の本文（できないこととできること）。 */
  message: string;
  /** 停止のボタン。 */
  stop: string;
}

/**
 * 閲覧だけの会話の案内の文言（3 製品で同じ。#3244）。本文は backend の `OTHERS_CONTENT_FORBIDDEN_MESSAGE`
 * （`pr_system_settings.auth.visibility`。他の利用者の会話に足す操作を 403 にしたときの文）と同じ文にする。
 * 製品は i18n で上書きできるが、3 製品で同じ機能の文は同じにする（AGENTS.md「言語」）。
 */
export const DEFAULT_CHAT_READ_ONLY_NOTICE_LABELS: ChatReadOnlyNoticeLabels = {
  title: "他の利用者の会話です（閲覧のみ）",
  message:
    "他の利用者の会話・実行には、続きの質問・実行・回答・評価を追加できません。閲覧・停止・削除はできます。調べ直すときは自分の会話で質問してください。",
  stop: "停止",
};

export interface ChatReadOnlyNoticeProps {
  /**
   * 停止（回答の作成中・処理中・承認待ちのときだけ渡す）。停止は運用の操作なので、閲覧だけの会話でも出す。
   * 渡さなければボタンを出さない。
   */
  onStop?: () => void;
  /** 停止の要求中（ボタンの `loading`）。 */
  stopping?: boolean;
  /** 案内の下に出すもの（停止の失敗の `ApiErrorBanner` など）。 */
  footer?: ReactNode;
  labels?: Partial<ChatReadOnlyNoticeLabels>;
  /** testid の接頭辞（既定 `chat-read-only`。案内は `-notice`、停止は `-stop`）。 */
  testIdPrefix?: string;
}

/**
 * 閲覧だけの会話の、入力欄の代わりの案内（3 製品共通。UX 契約 page-archetypes.md §6、#3244）。
 *
 * system_admin が他の利用者の会話を開いたときに `ChatLayout` の `composer` へ `ChatComposer` の代わりに渡す。
 * 続きの質問・実行・確認への回答・評価・名前の変更は持ち主だけなので、入力欄を無効にして残さず（読み取り専用を
 * 無効の入力欄で表さない）、info の `Banner` で「できないこととできること」を出す。停止は運用の操作として残し、
 * 作成中の回答・処理中の実行があるときだけ案内の中に置く。
 */
export function ChatReadOnlyNotice({
  onStop,
  stopping = false,
  footer,
  labels: labelOverrides,
  testIdPrefix = "chat-read-only",
}: ChatReadOnlyNoticeProps) {
  const labels = { ...DEFAULT_CHAT_READ_ONLY_NOTICE_LABELS, ...labelOverrides };
  return (
    <div className="space-y-2" data-testid={`${testIdPrefix}-notice`} data-chat-read-only-notice="">
      <Banner
        severity="info"
        title={labels.title}
        action={
          onStop ? (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              icon={Square}
              loading={stopping}
              onClick={onStop}
              data-testid={`${testIdPrefix}-stop`}
            >
              {labels.stop}
            </Button>
          ) : undefined
        }
      >
        {labels.message}
      </Banner>
      {footer}
    </div>
  );
}
