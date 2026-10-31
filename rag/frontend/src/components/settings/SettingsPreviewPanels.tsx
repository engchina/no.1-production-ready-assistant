"use client";

import { type LucideIcon } from "lucide-react";
import { useId, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  CopyButton,
  FormStatus,
  TextareaField,
} from "@production-ready/ui";
import { t } from "@/lib/i18n";

interface PreviewCardProps {
  title: string;
  description: string;
  value: string;
  ariaLabel?: string;
  copyLabel: string;
  icon: LucideIcon;
  /** プレビューの行数（高さは rows で決める。textareaClassName の h-* は使わない。#613 / #800）。 */
  previewRows: number;
}

export function SettingsPreviewCard({
  title,
  description,
  value,
  ariaLabel,
  copyLabel,
  icon,
  previewRows,
}: PreviewCardProps) {
  const [copyFailed, setCopyFailed] = useState(false);
  const previewId = useId();

  return (
    <Card>
      <CardHeader>
        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
          <SettingsCardHeader icon={icon} title={title} description={description} />
          <CopyButton
            // 内容のコピーは局所の操作（sm。README §4「Button」の表）。
            size="sm"
            className="w-full shrink-0 whitespace-nowrap sm:w-auto"
            text={value}
            label={copyLabel}
            copiedMessage={t("settings.preview.copy.copied")}
            failedMessage={t("settings.preview.copy.failed")}
            onCopied={() => setCopyFailed(false)}
            onCopyError={() => setCopyFailed(true)}
          />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <TextareaField
          id={previewId}
          label={ariaLabel ?? title}
          labelHidden
          // 設定の内容の表示（読み取り専用。フォームではない）なのでカードの幅いっぱい（#1891）。
          width="full"
          readOnly
          value={value}
          monospace
          resize="none"
          rows={previewRows}
        />
        {copyFailed ? (
          <FormStatus
            tone="danger"
            className="text-xs"
            message={t("settings.preview.copy.failed")}
          />
        ) : null}
      </CardContent>
    </Card>
  );
}

function SettingsCardHeader({
  icon: Icon,
  title,
  description,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  return (
    <div className="flex min-w-0 items-start gap-3">
      <span data-surface-tint="info" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
        <Icon size={20} aria-hidden />
      </span>
      <span className="min-w-0 space-y-1">
        <CardTitle>{title}</CardTitle>
        <CardDescription className="leading-relaxed">{description}</CardDescription>
      </span>
    </div>
  );
}
