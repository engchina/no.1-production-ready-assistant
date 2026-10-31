import { FileJson, ListChecks, Upload, X } from "lucide-react";
import { useState } from "react";

import {
  Banner,
  Button,
  FieldLabel,
  FormGrid,
  FormStatus,
  StatusBadge,
  TabPanel,
  Tabs,
  TextareaField,
  toast,
} from "@production-ready/ui";

import {
  api,
  ApiError,
  type SupportGuideImportDiff,
  type SupportGuideImportPreviewData,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useImportSupportGuides } from "@/lib/queries";
import {
  changeFieldLabels,
  changeSectionLabel,
  countChanges,
  groupChangesBySection,
  parseSupportGuideImport,
  SUPPORT_GUIDE_CHANGE_VARIANT,
} from "@/lib/support-guide-form";

import { SupportGuideIssueList } from "./SupportGuideIssueList";
import { SupportGuideProcessImport } from "./SupportGuideProcessImport";

type ImportTab = "json" | "process";

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/**
 * 業務ガイドの取り込み。JSON（書き出した形）と、回答の過程を書いた表（Excel・テキスト。#2397）のタブを持つ。
 * どちらも取り込んだガイドは下書きになる（公開は検証してから行う）。
 */
export function SupportGuideImportPanel({
  searchAnswerProfileId,
  onClose,
}: {
  searchAnswerProfileId: string;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<ImportTab>("json");
  return (
    <section
      className="min-w-0 space-y-3 rounded-md border border-border p-3"
      aria-labelledby="support-guide-import-title"
      data-testid="support-guide-import"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <h4 id="support-guide-import-title" className="flex items-center gap-1.5 text-sm font-semibold text-fg">
          <FileJson size={16} aria-hidden />
          {t("supportGuides.importPanel.title")}
        </h4>
        <Button size="sm" variant="ghost" icon={X} className="ml-auto" onClick={onClose}>
          {t("supportGuides.importPanel.close")}
        </Button>
      </div>
      <Tabs
        idPrefix="support-guide-import"
        ariaLabel={t("supportGuides.importPanel.tabsLabel")}
        value={tab}
        onChange={(value) => setTab(value as ImportTab)}
        items={(["json", "process"] as const).map((value) => ({
          id: value,
          label: t(`supportGuides.importPanel.tab.${value}`),
        }))}
      />
      <TabPanel idPrefix="support-guide-import" id="json" value={tab}>
        <SupportGuideJsonImport searchAnswerProfileId={searchAnswerProfileId} onDone={onClose} />
      </TabPanel>
      <TabPanel idPrefix="support-guide-import" id="process" value={tab}>
        <SupportGuideProcessImport searchAnswerProfileId={searchAnswerProfileId} onDone={onClose} />
      </TabPanel>
    </section>
  );
}

/**
 * JSON からの取り込み。「確認」で各ガイドを検証して示し、全件が取り込めるときだけ「取り込む」を押せる
 * （backend は 1 件でも不正なら何も作らない）。取り込んだガイドは下書きになる。
 */
function SupportGuideJsonImport({
  searchAnswerProfileId,
  onDone,
}: {
  searchAnswerProfileId: string;
  onDone: () => void;
}) {
  const [text, setText] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const [preview, setPreview] = useState<SupportGuideImportPreviewData | null>(null);
  // 確認した時点の JSON。変えたらもう一度確認させる。
  const [previewedText, setPreviewedText] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const importGuides = useImportSupportGuides(searchAnswerProfileId);
  useLeaveGuard(Boolean(text.trim()), importGuides.isPending);

  const stale = preview !== null && previewedText !== text;
  const allValid = preview !== null && preview.importable_count === preview.items.length;

  const runPreview = async () => {
    setRequestError(null);
    setPreview(null);
    const parsed = parseSupportGuideImport(text);
    if ("error" in parsed) {
      setInputError(t(parsed.error));
      document.getElementById("support-guide-import-text")?.focus();
      return;
    }
    setInputError(null);
    setPreviewing(true);
    try {
      setPreview(await api.previewSupportGuideImport(searchAnswerProfileId, parsed.guides));
      setPreviewedText(text);
    } catch (error) {
      setRequestError(errorMessage(error, t("supportGuides.importPanel.previewError")));
    } finally {
      setPreviewing(false);
    }
  };

  const runImport = () => {
    const parsed = parseSupportGuideImport(text);
    if ("error" in parsed) return;
    setRequestError(null);
    importGuides.mutate(parsed.guides, {
      onSuccess: (data) => {
        toast.success(t("supportGuides.importPanel.imported", { count: data.created.length }));
        setText("");
        setPreview(null);
        setPreviewedText(null);
        onDone();
      },
      onError: (error) => setRequestError(errorMessage(error, t("supportGuides.importPanel.importError"))),
    });
  };

  const readFile = async (file: File | null) => {
    if (!file) return;
    try {
      setText(await file.text());
      setInputError(null);
      setPreview(null);
    } catch {
      setInputError(t("supportGuides.importPanel.fileError"));
    }
  };

  return (
    <div className="min-w-0 space-y-3" data-testid="support-guide-json-import">
      <p className="text-xs leading-relaxed text-fg-muted">{t("supportGuides.importPanel.hint")}</p>
      {/* ファイルと JSON の欄はフォームの最大幅まで（JSON は長い値なので full。#1891）。 */}
      <FormGrid columns={1}>
        <div className="space-y-1.5">
          <FieldLabel
            htmlFor="support-guide-import-file"
            label={t("supportGuides.importPanel.file")}
            className="block"
          />
          <input
            id="support-guide-import-file"
            type="file"
            accept=".json,application/json"
            onChange={(event) => void readFile(event.target.files?.[0] ?? null)}
            className="block w-full text-sm text-fg file:mr-3 file:rounded-md file:border file:border-border-control file:bg-surface file:px-3 file:py-1.5 file:text-sm"
          />
        </div>
        <TextareaField
          id="support-guide-import-text"
          width="full"
          label={t("supportGuides.importPanel.text")}
          value={text}
          rows={8}
          required
          error={inputError ?? undefined}
          onChange={(event) => {
            setText(event.target.value);
            setInputError(null);
          }}
        />
      </FormGrid>
      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
        <Button
          size="sm"
          variant="secondary"
          icon={ListChecks}
          className="w-full sm:w-auto"
          loading={previewing}
          disabled={importGuides.isPending}
          onClick={() => void runPreview()}
        >
          {t("supportGuides.importPanel.preview")}
        </Button>
        <Button
          size="sm"
          icon={Upload}
          className="w-full sm:w-auto"
          loading={importGuides.isPending}
          disabled={!allValid || stale || previewing}
          onClick={runImport}
        >
          {t("supportGuides.importPanel.run")}
        </Button>
      </div>
      <div aria-live="polite" className="space-y-3" data-testid="support-guide-import-result">
        {requestError ? <FormStatus tone="danger" message={requestError} /> : null}
        {stale ? <FormStatus tone="warning" message={t("supportGuides.importPanel.changed")} /> : null}
        {preview ? (
          <div className="space-y-3">
            <p className="text-sm text-fg">
              {t("supportGuides.importPanel.summary", {
                total: preview.items.length,
                count: preview.importable_count,
              })}
            </p>
            {!allValid ? <Banner severity="danger">{t("supportGuides.importPanel.blocked")}</Banner> : null}
            <ul className="space-y-2">
              {preview.items.map((item) => (
                <li
                  key={item.index}
                  className="min-w-0 space-y-2 rounded-md border border-border p-2"
                  data-testid={`support-guide-import-item-${item.index}`}
                >
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <StatusBadge
                      variant={item.valid ? "success" : "danger"}
                      label={t(
                        item.valid
                          ? "supportGuides.importPanel.valid"
                          : "supportGuides.importPanel.invalid",
                      )}
                    />
                    <span className="min-w-0 break-words text-sm font-medium text-fg">
                      {t("supportGuides.importPanel.itemTitle", {
                        n: item.index + 1,
                        title: item.title || t("supportGuides.importPanel.untitled"),
                      })}
                    </span>
                  </div>
                  <SupportGuideIssueList issues={item.issues} />
                  {item.existing ? (
                    <SupportGuideImportDiffView diff={item.existing} index={item.index} />
                  ) : item.valid ? (
                    <p className="text-xs text-fg-muted" data-testid={`support-guide-import-new-${item.index}`}>
                      {t("supportGuides.importPanel.diff.new")}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/**
 * 取り込むガイドと、同じ ID・名前の既存のガイドとの違い（#1288）。節ごとに追加・削除・変更を並べる。
 * 種類は色だけでなくラベル付きの StatusBadge で示す。
 */
function SupportGuideImportDiffView({ diff, index }: { diff: SupportGuideImportDiff; index: number }) {
  const counts = countChanges(diff.changes);
  const titleId = `support-guide-import-diff-title-${index}`;
  return (
    <section
      className="min-w-0 space-y-2 rounded-md border border-border bg-surface-sunken p-2"
      aria-labelledby={titleId}
      data-testid={`support-guide-import-diff-${index}`}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <h5 id={titleId} className="min-w-0 break-words text-sm font-medium text-fg">
          {t("supportGuides.importPanel.diff.title", { title: diff.title })}
        </h5>
        <StatusBadge
          variant="neutral"
          label={t(
            diff.matched_by === "id"
              ? "supportGuides.importPanel.diff.matchedById"
              : "supportGuides.importPanel.diff.matchedByTitle",
          )}
        />
        {diff.status === "archived" ? (
          <StatusBadge variant="neutral" label={t("supportGuides.importPanel.diff.archived")} />
        ) : null}
      </div>
      <p className="text-xs leading-relaxed text-fg-muted">
        {t(
          diff.base === "published"
            ? "supportGuides.importPanel.diff.basePublished"
            : "supportGuides.importPanel.diff.baseDraft",
          { revision: diff.revision },
        )}
        {t("supportGuides.path.separator")}
        {t("supportGuides.importPanel.diff.note")}
      </p>
      {diff.changes.length === 0 ? (
        <p className="text-sm text-fg">{t("supportGuides.importPanel.diff.same")}</p>
      ) : (
        <>
          <p className="tnum text-xs text-fg-muted">
            {t("supportGuides.importPanel.diff.counts", {
              added: counts.added,
              removed: counts.removed,
              changed: counts.changed,
            })}
          </p>
          <ul className="space-y-2">
            {groupChangesBySection(diff.changes).map((group) => (
              <li key={group.section} className="min-w-0 space-y-1">
                <p className="text-xs font-semibold text-fg">{changeSectionLabel(group.section)}</p>
                <ul className="space-y-1">
                  {group.changes.map((change) => {
                    const fields = changeFieldLabels(change);
                    const name = change.label || change.key;
                    return (
                      <li
                        key={`${change.kind}-${change.key}-${change.label}`}
                        className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-fg"
                        data-change-kind={change.kind}
                      >
                        <StatusBadge
                          variant={SUPPORT_GUIDE_CHANGE_VARIANT[change.kind]}
                          label={t(`supportGuides.change.${change.kind}`)}
                        />
                        {name ? <span className="min-w-0 break-words">{name}</span> : null}
                        {change.key && change.key !== name ? (
                          <span className="font-mono text-xs text-fg-muted">{change.key}</span>
                        ) : null}
                        {fields.length > 0 ? (
                          <span className="min-w-0 break-words text-xs text-fg-muted">
                            {t("supportGuides.change.fields", {
                              fields: fields.join(t("supportGuides.change.fieldSeparator")),
                            })}
                          </span>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
