import { ErrorState } from "@/components/StateViews";
import { DocumentPicker } from "@/components/documents/DocumentPicker";
import { FormSkeleton, TimedLoadingState } from "@production-ready/ui";
import { t } from "@/lib/i18n";
import { initialLoadError, useSearchAnswerProfile } from "@/lib/queries";
import { referenceRowFromDocument, type ReferenceForm } from "@/lib/support-guide-form";

/**
 * 業務ガイドの資料を選ぶ（ナレッジベースの「文書を追加」と同じ `DocumentPicker`。#2749）。
 * 候補はプロファイルのナレッジベースの文書（公開の前の検証と同じ範囲）に絞り、すでに入れた文書は「追加済み」で選べなくする。
 */
export function SupportGuideReferencePicker({
  id,
  testId,
  searchAnswerProfileId,
  references,
  maxSelection,
  onAdd,
  onClose,
}: {
  id: string;
  testId: string;
  searchAnswerProfileId: string;
  /** すでに入れた資料（同じ文書は選べなくする）。 */
  references: readonly ReferenceForm[];
  maxSelection: number;
  onAdd: (references: ReferenceForm[]) => void;
  onClose: () => void;
}) {
  const profile = useSearchAnswerProfile(searchAnswerProfileId);
  if (profile.isPending) {
    return (
      <TimedLoadingState
        label={t("supportGuides.referencePicker.loading")}
        operationKey={`support-guide-reference-picker-${searchAnswerProfileId}`}
        testId={`${testId}-loading`}
      >
        <FormSkeleton fields={2} />
      </TimedLoadingState>
    );
  }
  const loadError = initialLoadError(profile);
  if (loadError || !profile.data) {
    return (
      <ErrorState
        message={t("supportGuides.referencePicker.loadError")}
        onRetry={() => void profile.refetch()}
      />
    );
  }
  const added = new Set(references.map((reference) => reference.documentId));
  return (
    <DocumentPicker
      id={id}
      testId={testId}
      description={t("supportGuides.referencePicker.hint")}
      knowledgeBaseIds={profile.data.config.knowledge_base_ids}
      unavailableReason={(document) => (added.has(document.id) ? t("documentPicker.alreadyAdded") : undefined)}
      maxSelection={maxSelection}
      onSubmit={(documents) => onAdd(documents.map(referenceRowFromDocument))}
      onClose={onClose}
    />
  );
}
