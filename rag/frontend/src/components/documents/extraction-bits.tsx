import { FileImage, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { splitExtractedText, type ExtractedMedia } from "@/lib/extracted-text";
import { formatBytes } from "@/lib/format";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** メディア帯に並べる最大件数。超過分は `+N` に畳む。 */
const MAX_MEDIA = 3;

/**
 * 抽出本文 / chunk テキストを表示する。埋め込み base64 はサムネイル(ラスタ画像)
 * または軽量チップ(svg/バイナリ/断片)へ畳み、本文だけを読みやすく見せる。
 */
export function ExtractedText({ text, clamp = false }: { text: string; clamp?: boolean }) {
  const { text: body, media } = splitExtractedText(text);

  if (!body && media.length === 0) {
    return <span className="text-sm text-fg-muted">—</span>;
  }

  const shown = media.slice(0, MAX_MEDIA);
  const overflow = media.length - shown.length;

  return (
    <div className="space-y-2">
      {media.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          {shown.map((item, index) => (
            <MediaItem key={index} media={item} />
          ))}
          {overflow > 0 ? (
            <span className="tnum rounded-full border border-border bg-surface-sunken px-2.5 py-1 text-xs text-fg-muted">
              {t("flow.extraction.embeddedMore", { count: overflow })}
            </span>
          ) : null}
        </div>
      ) : null}
      {body ? (
        <p
          className={cn(
            "whitespace-pre-wrap break-words text-sm leading-relaxed text-fg/90",
            clamp && "line-clamp-4"
          )}
        >
          {body}
        </p>
      ) : null}
    </div>
  );
}

function MediaItem({ media }: { media: ExtractedMedia }) {
  if (media.kind === "image" && media.src) {
    return (
      <img
        src={media.src}
        loading="lazy"
        alt={t("flow.extraction.embeddedImageAlt")}
        className="max-h-32 w-auto rounded-md border border-border bg-surface object-contain"
      />
    );
  }
  const label = media.fragment
    ? t("flow.extraction.embeddedAssetFragment")
    : t("flow.extraction.embeddedAsset", { size: formatBytes(media.approxBytes) });
  return <InfoChip icon={FileImage} label={label} />;
}

/** ラベル(+任意アイコン)の丸チップ。CitationCard の MetadataChip と同じ視覚。 */
export function InfoChip({
  icon: Icon,
  label,
  title,
}: {
  icon?: LucideIcon;
  label: string;
  title?: string;
}) {
  return (
    <span
      className="inline-flex min-w-0 max-w-full items-center gap-1 rounded-full border border-border bg-surface-sunken px-2.5 py-1 text-xs text-fg-muted"
      title={title ?? label}
    >
      {Icon ? <Icon size={14} className="shrink-0" aria-hidden /> : null}
      <span className="truncate">{label}</span>
    </span>
  );
}

/** 連番(#N など)を表す小さな丸バッジ。 */
export function IndexBadge({ children }: { children: ReactNode }) {
  return (
    <span data-surface-tint="accent" className="tnum flex h-6 min-w-6 shrink-0 items-center justify-center rounded-full bg-accent-subtle px-1.5 text-xs font-semibold text-accent-fg">
      {children}
    </span>
  );
}
