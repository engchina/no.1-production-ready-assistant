import { FileSpreadsheet, Upload } from "lucide-react";
import { useRef, useState } from "react";

import {
  Button,
  FieldLabel,
  FormStatus,
  ProcessingIndicator,
  SelectField,
  useConfirm,
  resolveContentLang,
} from "@production-ready/ui";

import {
  api,
  ApiError,
  type DomainKeywordImportMode,
  type DomainKeywordImportPreviewData,
} from "@/lib/api";
import { t } from "@/lib/i18n";

/** Approved FAQ と同じファイル選択・プレビュー・取込の構成。 */
export function DomainKeywordExcelImport({
  searchAnswerProfileId, disabled, dirty, pending, onImport,
}: {
  searchAnswerProfileId: string;
  disabled: boolean;
  dirty: boolean;
  pending: boolean;
  onImport: (file: File, mode: DomainKeywordImportMode) => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<DomainKeywordImportMode>("INSERT");
  const [preview, setPreview] = useState<DomainKeywordImportPreviewData | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [error, setError] = useState("");
  const requestSequence = useRef(0);
  const confirm = useConfirm();
  const selectFile = async (next: File | null) => {
    const sequence = ++requestSequence.current;
    setFile(next);
    setPreview(null);
    setError("");
    setPreviewing(Boolean(next));
    if (!next) return;
    try {
      const data = await api.previewDomainKeywordImport(searchAnswerProfileId, next);
      if (sequence === requestSequence.current) setPreview(data);
    } catch (cause) {
      if (sequence === requestSequence.current) {
        setError(cause instanceof ApiError ? cause.message : t("searchAnswerProfiles.domainKeywords.previewError"));
      }
    } finally {
      if (sequence === requestSequence.current) setPreviewing(false);
    }
  };
  const handleImport = async () => {
    if (!file || !preview || previewing || pending || disabled) return;
    if (mode === "REPLACE" && !(await confirm({
      title: t("searchAnswerProfiles.domainKeywords.replaceTitle"),
      description: t("searchAnswerProfiles.domainKeywords.replaceHelp"),
      confirmLabel: t("searchAnswerProfiles.faq.import"),
      tone: "danger",
      dismissOnOverlay: false,
    }))) return;
    setError("");
    try {
      await onImport(file, mode);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("searchAnswerProfiles.domainKeywords.importError"));
    }
  };

  return (
    <section className="min-w-0 space-y-3 rounded-md border border-border p-3">
      <h4 className="flex items-center gap-1.5 text-sm font-semibold text-fg">
        <FileSpreadsheet size={16} aria-hidden />
        {t("searchAnswerProfiles.faq.importTitle")}
      </h4>
      <p className="text-xs leading-relaxed text-fg-muted">
        {t("searchAnswerProfiles.domainKeywords.importHelp")}
      </p>
      <FieldLabel htmlFor="domain-keywords-import-file" label={t("searchAnswerProfiles.faq.importFile")} required className="block" />
      <input
        id="domain-keywords-import-file"
        type="file"
        accept=".xlsx,.xls"
        required
        disabled={pending}
        onChange={(event) => void selectFile(event.target.files?.[0] ?? null)}
        className="block w-full text-sm text-fg file:mr-3 file:rounded-md file:border file:border-border-control file:bg-surface file:px-3 file:py-1.5 file:text-sm"
      />
      <SelectField
        id="domain-keywords-import-mode"
        label={t("searchAnswerProfiles.faq.importModeLabel")}
        helper={t(mode === "REPLACE" ? "searchAnswerProfiles.domainKeywords.replaceHelp" : "searchAnswerProfiles.domainKeywords.insertHelp")}
        value={mode}
        options={[
          { value: "INSERT", label: t("searchAnswerProfiles.domainKeywords.importMode.INSERT") },
          { value: "REPLACE", label: t("searchAnswerProfiles.domainKeywords.importMode.REPLACE") },
        ]}
        onValueChange={(value) => value && setMode(value)}
        width="full"
        disabled={pending}
      />
      {previewing ? <ProcessingIndicator active label={t("searchAnswerProfiles.domainKeywords.previewing")} operationKey="domain-keywords-preview" placement="action" activityIcon="none" /> : null}
      {preview ? (
        <div className="space-y-1 text-xs text-fg-muted" aria-live="polite">
          <p>{t("searchAnswerProfiles.domainKeywords.previewCount", { count: preview.total })}</p>
          <ul className="space-y-1">
            {preview.rows.map((row) => <li key={row.row} lang={resolveContentLang(undefined, row.keyword)} className="break-words">{row.keyword}</li>)}
          </ul>
        </div>
      ) : null}
      {dirty ? <FormStatus tone="warning" message={t("searchAnswerProfiles.domainKeywords.dirtyImportHelp")} /> : null}
      <Button size="sm" variant="secondary" icon={Upload} loading={pending} disabled={disabled || previewing || !file || !preview} onClick={() => void handleImport()}>
        {t("searchAnswerProfiles.faq.import")}
      </Button>
      {pending ? <ProcessingIndicator active label={t("searchAnswerProfiles.domainKeywords.importing")} operationKey="domain-keywords-import" placement="action" activityIcon="none" /> : null}
      {error ? <FormStatus tone="danger" message={error} /> : null}
    </section>
  );
}
