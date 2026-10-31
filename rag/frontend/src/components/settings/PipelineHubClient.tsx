"use client";

import { PageBody } from "@production-ready/ui";
import { useId } from "react";
import { Link } from "react-router-dom";

import { settingsSubtitleKey, visibleNavSections, type NavItem } from "@/components/layout/nav-config";
import { useAuth } from "@/components/security/AuthProvider";
import { APP_ROUTES } from "@/lib/routes";
import { t } from "@/lib/i18n";

import { PipelineRecipeDefaultsSection } from "./PipelineRecipeDefaultsSection";

// 「ナレッジ構築(取込)」工程の href。これ以外の取込・検索・回答設定工程は「検索・回答」に分類する。
const INGESTION_HREFS = new Set<string>([
  APP_ROUTES.settingsPreprocess,
  APP_ROUTES.settingsParserAdapters,
  APP_ROUTES.settingsChunking,
  APP_ROUTES.settingsVectorIndex,
  APP_ROUTES.settingsEntityIndex,
  APP_ROUTES.settingsGraph,
]);

// 1 つの工程ではなく全工程が使う設定（言語資源。#2181）。番号を付けず、工程の前に出す。
const SHARED_HREFS = new Set<string>([APP_ROUTES.settingsLanguagePack]);

function stageDescription(item: NavItem): string {
  const key = settingsSubtitleKey(item);
  return key ? t(key) : "";
}

/**
 * 取込・検索・回答設定の俯瞰ハブ。サイドバーの「取込・検索・回答設定」セクション(処理順)を
 * ナレッジ構築 / 検索・回答 の 2 フェーズに分け、各工程へのカード導線を 1 画面で提供する。
 * セクション項目を動的に読むため、工程の増減に追従して drift しない。
 * 権限のない工程はサイドナビと同じ判定で出さず、工程が 0 件のフェーズは見出しごと出さない（#214）。
 * 先頭に、取込の流れと全体の既定（工程の自動進行のスイッチを含む。#528）を置く。
 */
export function PipelineHubClient() {
  const { hasPermission } = useAuth();
  const section = visibleNavSections(hasPermission).find(
    (item) => item.titleKey === "nav.section.pipeline"
  );
  const items = (section?.items ?? []).filter((item) => item.href !== APP_ROUTES.settingsPipeline);
  const shared = items.filter((item) => SHARED_HREFS.has(item.href));
  const stages = items.filter((item) => !SHARED_HREFS.has(item.href));
  const ingestion = stages.filter((item) => INGESTION_HREFS.has(item.href));
  const query = stages.filter((item) => !INGESTION_HREFS.has(item.href));

  return (
    <PageBody wide>
      {/* 取込の流れ・工程の自動進行・レシピ 11 項目の全体の既定（#528）。この画面の権限で読み書きする。 */}
      <PipelineRecipeDefaultsSection />
      <PhaseGroup
        title={t("settings.pipeline.phase.shared")}
        hint={t("settings.pipeline.phase.sharedHint")}
        stages={shared}
        startIndex={null}
      />
      <PhaseGroup
        title={t("settings.pipeline.phase.ingestion")}
        hint={t("settings.pipeline.phase.ingestionHint")}
        stages={ingestion}
        startIndex={1}
      />
      <PhaseGroup
        title={t("settings.pipeline.phase.query")}
        hint={t("settings.pipeline.phase.queryHint")}
        stages={query}
        startIndex={ingestion.length + 1}
      />
    </PageBody>
  );
}

function PhaseGroup({
  title,
  hint,
  stages,
  startIndex,
}: {
  title: string;
  hint: string;
  stages: NavItem[];
  /** 工程の番号の始まり。null は番号を付けない（全工程で共通の設定）。 */
  startIndex: number | null;
}) {
  if (stages.length === 0) return null;
  return (
    <section className="space-y-3" aria-label={title}>
      <div>
        <h2 className="text-sm font-semibold text-fg">{title}</h2>
        <p className="mt-0.5 text-xs text-fg-muted">{hint}</p>
      </div>
      <ol className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {stages.map((item, index) => (
          <li key={item.href}>
            <StageCard item={item} step={startIndex === null ? null : startIndex + index} />
          </li>
        ))}
      </ol>
    </section>
  );
}

function StageCard({ item, step }: { item: NavItem; step: number | null }) {
  const cardId = useId();
  const contentId = useId();
  const Icon = item.icon;
  const name = t(item.labelKey);
  return (
    <Link
      to={item.href}
      id={cardId}
      aria-label={t("settings.pipeline.openStage", { name })}
      aria-labelledby={`${contentId} ${cardId}`}
      className="flex h-full gap-3 rounded-lg border border-border bg-surface p-4 transition-colors hover:border-accent-emphasis hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
    >
      <span data-surface-tint="accent" className="flex size-9 shrink-0 items-center justify-center rounded-md bg-accent-subtle text-accent-fg">
        <Icon size={20} aria-hidden />
      </span>
      <span id={contentId} className="min-w-0">
        <span className="block text-sm font-semibold text-fg">
          {step === null ? null : <span className="tnum text-fg-muted">{step}.</span>} {name}
        </span>
        <span className="mt-0.5 block text-xs leading-relaxed text-fg-muted">
          {stageDescription(item)}
        </span>
      </span>
    </Link>
  );
}
