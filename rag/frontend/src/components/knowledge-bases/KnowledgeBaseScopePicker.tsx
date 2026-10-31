"use client";

import { useId, useMemo, useState } from "react";

import { KnowledgeBaseMultiSelect } from "@/components/knowledge-bases/KnowledgeBaseMultiSelect";
import { Banner, type FieldWidth, TimedLoadingState, Skeleton } from "@production-ready/ui";
import { ApiError } from "@/lib/api";
import { t } from "@/lib/i18n";
import {
  type KnowledgeBaseRefLike,
  type KnowledgeBaseSelectionHealth,
  resolveKnowledgeBaseSelection,
} from "@/lib/knowledge-base-refs";
import { useKnowledgeBaseChoices, useKnowledgeBasesByIds } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * 選択済みの KB を ID で引き、名前・状態（アーカイブ済み / 見つからない）を解決する（#302）。
 * `known` は画面が既に持つ参照（検索・回答プロファイル詳細の参照 KB など。利用者の KB 範囲の外も含む）。
 */
export function useKnowledgeBaseSelectionHealth(
  ids: string[],
  {
    known,
    knownMissingIds,
  }: { known?: readonly KnowledgeBaseRefLike[]; knownMissingIds?: readonly string[] } = {}
): KnowledgeBaseSelectionHealth {
  const lookup = useKnowledgeBasesByIds(ids);
  const found = lookup.data?.items;
  // 別の ID 集合の結果を出している間（placeholder）と失敗時は、見つからないと判定しない。
  const lookupSettled = lookup.isSuccess && !lookup.isPlaceholderData;
  return useMemo(
    () =>
      resolveKnowledgeBaseSelection({
        ids,
        found,
        known,
        knownMissingIds,
        lookupSettled,
        missingName: (id) => t("knowledgeBasePicker.missingName", { id }),
      }),
    [ids, found, known, knownMissingIds, lookupSettled]
  );
}

/**
 * 検索・評価・検索・回答プロファイル・文書詳細で使うナレッジベースの複数選択スコープ（#578）。
 * 候補は `useKnowledgeBaseChoices`: 200 件以下は全件を手元で絞り込み、超えるとサーバー側で名前・説明を検索し、
 * 「さらに表示」で次のページを取る（全件を読まない。#302）。
 */
export function KnowledgeBaseScopePicker({
  selectedIds,
  onChange,
  disabled = false,
  label = t("knowledgeBaseScope.label"),
  helper = t("knowledgeBaseScope.helper"),
  emptySelectionText = t("knowledgeBaseScope.all"),
  className,
  knownKnowledgeBases,
  knownMissingIds,
  required = false,
  errorId,
  width,
}: {
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
  label?: string;
  helper?: string;
  emptySelectionText?: string;
  className?: string;
  /** 画面が既に持つ選択済みの参照（範囲外の KB の名前もチップに出すため）。 */
  knownKnowledgeBases?: readonly KnowledgeBaseRefLike[];
  knownMissingIds?: readonly string[];
  /** 1 件以上の選択が必須（ラベルに「必須」、入力欄に aria-required。#531）。 */
  required?: boolean;
  /** 欄の下に出しているエラーの id。渡すと入力欄を aria-invalid にして結ぶ。 */
  errorId?: string;
  /** 選択欄の幅（対象の選択なので通常は lg。省くと置き場所の幅いっぱい。#1891）。 */
  width?: FieldWidth;
}) {
  const inputId = useId();
  // 検索語は SearchField が確定した値（300ms・Enter・IME の確定後）。親では遅延させない（#535）。
  const [q, setQ] = useState("");
  const choices = useKnowledgeBaseChoices({ status: "ACTIVE", q });
  const selection = useKnowledgeBaseSelectionHealth(selectedIds, {
    known: knownKnowledgeBases,
    knownMissingIds,
  });
  // 有効な KB が 1 件も無いときだけ「有効なナレッジベースがありません」（検索の 0 件は一覧内で示す）。
  const noKnowledgeBases = !choices.isPending && !choices.isError && choices.total === 0;

  return (
    <div className={cn("space-y-2", className)} data-testid="knowledge-base-scope-picker">
      {choices.isError ? (
        <Banner severity="warning" title={t("knowledgeBaseScope.loadWarning")}>
          <p>
            {choices.error instanceof ApiError
              ? choices.error.message
              : t("knowledgeBaseScope.loadWarningHint")}
          </p>
        </Banner>
      ) : choices.isPending ? (
        <TimedLoadingState
          label={t("knowledgeBaseScope.loading")}
          operationKey="knowledge-base-scope-load"
          framed={false}
          testId="knowledge-base-scope-loading"
        >
          <Skeleton className="h-[var(--field-height)] w-full" />
        </TimedLoadingState>
      ) : noKnowledgeBases && selectedIds.length === 0 ? (
        <p className="rounded-md border border-border bg-surface-sunken px-3 py-2 text-xs text-fg-muted">
          {t("knowledgeBaseScope.empty")}
        </p>
      ) : (
        <>
          <KnowledgeBaseMultiSelect
            id={inputId}
            label={label}
            helper={helper}
            required={required}
            invalid={Boolean(errorId)}
            describedBy={errorId}
            choices={choices}
            onQueryChange={setQ}
            selectedIds={selectedIds}
            onChange={onChange}
            selectedItems={selection.items}
            disabled={disabled}
            width={width}
          />
          <p className="text-xs text-fg-muted">
            {selectedIds.length > 0
              ? t("knowledgeBaseScope.selected", { count: selectedIds.length })
              : emptySelectionText}
          </p>
        </>
      )}
    </div>
  );
}
