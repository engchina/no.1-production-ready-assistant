import type { ReactNode } from "react";
import {
  Banner,
  Disclosure,
  MessageText,
  StatusBadge,
} from "@production-ready/ui";

import { t } from "./messages";
import { cn } from "@production-ready/ui";

export type SettingsTestResultTone = "success" | "warning" | "danger";
export type SettingsTestResultValue = string | number | boolean;

export interface SettingsTestResultDetail {
  label: string;
  value: SettingsTestResultValue;
}

export interface SettingsTestResultPanelProps {
  tone: SettingsTestResultTone;
  message: string;
  elapsedMs?: number;
  checkedAt?: string;
  details?: readonly SettingsTestResultDetail[];
  troubleshooting?: readonly string[];
  errorType?: string | null;
  /** backend が秘密情報を伏せた、利用者に提示できる診断本文。 */
  rawError?: string | null;
  className?: string;
  testId?: string;
  /** timing の直後、details の前に置く追加内容（段階ごとの結果など）。 */
  children?: ReactNode;
}

/**
 * システム設定の接続・モデルテスト結果を同じ情報階層で表示する（UX 契約 messaging.md §10。#705）。
 *
 * 要約（何が起きたか）と所要時間・確認ポイントは常に出し、技術的な詳細（key/value・エラー種別）は
 * 「詳細」に畳む。失敗のときだけ開いて出す。
 */
export function SettingsTestResultPanel({
  tone,
  message,
  elapsedMs,
  checkedAt,
  details = [],
  troubleshooting = [],
  errorType,
  rawError,
  className,
  testId,
  children,
}: SettingsTestResultPanelProps) {
  const errorDetail = tone === "danger" ? rawError?.trim() : undefined;
  const hasTiming = elapsedMs !== undefined || Boolean(checkedAt);

  return (
    <div
      data-settings-test-result=""
      data-tone={tone}
      data-testid={testId}
      className={cn("min-w-0", className)}
    >
      <Banner severity={tone} title={message}>
        <div className="min-w-0 space-y-2">
          {hasTiming ? (
            <p className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-fg-muted">
              {elapsedMs !== undefined ? (
                <span className="tnum">
                  {t("settings.testResult.elapsed")}: {elapsedMs} ms
                </span>
              ) : null}
              {checkedAt ? (
                <span>
                  {t("settings.testResult.checkedAt")}: {checkedAt}
                </span>
              ) : null}
            </p>
          ) : null}

          {children}

          {tone !== "success" && troubleshooting.length > 0 ? (
            <div className="space-y-1">
              <p className="text-xs font-semibold text-fg">
                {t("settings.testResult.troubleshooting")}
              </p>
              <ul className="list-disc space-y-1 pl-5 text-xs leading-relaxed text-fg/90">
                {troubleshooting.map((item) => (
                  <li key={item} className="min-w-0 break-words">
                    <MessageText text={item} />
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {details.length > 0 || (tone === "danger" && errorType) || errorDetail ? (
            <Disclosure
              variant="plain"
              size="sm"
              summary={t("settings.testResult.details")}
              defaultOpen={tone === "danger"}
            >
              <div className="space-y-2">
                {details.length > 0 ? (
                  <dl className="grid min-w-0 gap-x-4 gap-y-1 text-xs text-fg-muted sm:grid-cols-2">
                    {details.map((detail) => (
                      <div key={detail.label} className="min-w-0">
                        <dt className="break-words font-medium text-fg">{detail.label}</dt>
                        <dd className="break-words tnum">{String(detail.value)}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
                {tone === "danger" && errorType ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold text-fg">
                      {t("settings.testResult.errorType")}
                    </span>
                    <StatusBadge variant="danger" label={errorType} />
                  </div>
                ) : null}
                {errorDetail ? (
                  <div className="min-w-0 space-y-1 text-xs">
                    <p className="font-semibold text-fg">{t("settings.testResult.errorDetail")}</p>
                    <p className="break-words whitespace-pre-wrap text-fg-muted">{errorDetail}</p>
                  </div>
                ) : null}
              </div>
            </Disclosure>
          ) : null}
        </div>
      </Banner>
    </div>
  );
}

export function toSettingsTestResultDetails(
  values: Readonly<Record<string, SettingsTestResultValue | null | undefined>>
): SettingsTestResultDetail[] {
  return Object.entries(values).flatMap(([label, value]) =>
    value === null || value === undefined || value === "" ? [] : [{ label, value }]
  );
}
