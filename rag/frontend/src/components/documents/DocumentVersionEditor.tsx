import {
  Button,
  FormStatus,
  SearchableSelectField,
  StatusBadge,
  type SearchableSelectOption,
} from "@production-ready/ui";
import { History, Save, Undo2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { documentVersionOptions, savedVersionOption } from "./DocumentVersionEditor.logic";
import { ApiError, type DocumentDetail } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useDocumentCandidates, useSaveDocumentSupersededBy } from "@/lib/queries";
import { useValuesChanged } from "@/lib/render-sync";
import { APP_ROUTES } from "@/lib/routes";

const DOCUMENT_VERSION_PICKER_ID = "document-version-picker";

/**
 * 文書の版（#1248）。この文書を置き換えた新しい版の文書を選んで保存すると、この文書は旧版になり、
 * 既定では回答の検索の対象から外れる。置き換えの記録は文書の属性で、所属ナレッジベースは変えない。
 */
export function DocumentVersionEditor({ document: doc }: { document: DocumentDetail }) {
  const save = useSaveDocumentSupersededBy();
  const savedId = doc.superseded_by_document_id ?? "";
  const [selectedId, setSelectedId] = useState(savedId);
  const [selectedCandidate, setSelectedCandidate] = useState<SearchableSelectOption | null>(null);
  // 文書詳細は処理中に定期的に再取得される。保存値が実際に変わったときだけ選択を入れ替える。
  if (useValuesChanged([savedId])) setSelectedId(savedId);
  const [lastAction, setLastAction] = useState<"set" | "clear" | null>(null);

  // 候補は欄に触れたときに初めて読む（文書詳細を開くたびに文書の一覧を読まない）。
  const [pickerActive, setPickerActive] = useState(false);
  const [query, setQuery] = useState("");
  const documents = useDocumentCandidates(
    { q: query || undefined },
    { enabled: pickerActive },
  );
  const candidates = [
    ...new Map(
      (documents.data?.pages ?? [])
        .flatMap((page) => page.items)
        .map((item) => [item.id, item]),
    ).values(),
  ];
  const options = documentVersionOptions(candidates, doc.id, {
    superseded: t("documents.version.superseded"),
  });
  const saved = savedVersionOption(savedId, doc.superseded_by_file_name, (id) =>
    t("documents.version.unknownDocument", { id }),
  );
  const selected =
    options.find((option) => option.value === selectedId) ??
    (selectedCandidate?.value === selectedId ? selectedCandidate : null) ??
    (saved && saved.value === selectedId ? saved : null);

  const isDirty = selectedId !== savedId;
  useLeaveGuard(isDirty);
  const superseded = Boolean(doc.superseded_by_document_id);

  const submit = (supersededById: string | null) => {
    setLastAction(supersededById ? "set" : "clear");
    save.mutate({ id: doc.id, payload: { superseded_by_document_id: supersededById } });
  };

  return (
    <section
      className="space-y-3 border-t border-border pt-4"
      aria-labelledby="document-version-title"
      data-testid="document-version"
    >
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="document-version-title" className="text-sm font-semibold text-fg">
            {t("documents.version.title")}
          </h3>
          {superseded ? (
            <StatusBadge
              variant="warning"
              icon={History}
              label={t("documents.version.superseded")}
            />
          ) : null}
        </div>
        <p className="mt-1 text-xs text-fg-muted">{t("documents.version.description")}</p>
      </div>

      {superseded ? (
        <p className="break-words text-sm text-fg" data-testid="document-version-superseded-by">
          {t("documents.version.replacedBy")}{" "}
          {doc.superseded_by_file_name ? (
            <Link
              to={`${APP_ROUTES.documents}/${encodeURIComponent(savedId)}`}
              className="font-medium text-accent-fg hover:underline"
            >
              {doc.superseded_by_file_name}
            </Link>
          ) : (
            <span className="font-medium">
              {t("documents.version.unknownDocument", { id: savedId })}
            </span>
          )}
          {doc.superseded_at ? (
            <span className="tnum text-fg-muted">
              {" "}
              {t("documents.version.replacedAt", { date: formatDateTime(doc.superseded_at) })}
            </span>
          ) : null}
        </p>
      ) : (
        <p className="text-sm text-fg-muted">{t("documents.version.current")}</p>
      )}

      <div
        id={DOCUMENT_VERSION_PICKER_ID}
        onFocusCapture={() => setPickerActive(true)}
        onPointerDownCapture={() => setPickerActive(true)}
      >
        <SearchableSelectField
          id="document-version-superseded-by"
          label={t("documents.version.pickerLabel")}
          placeholder={t("documents.version.pickerPlaceholder")}
          value={selectedId}
          options={options}
          selectedOption={selected}
          onValueChange={(value) => {
            setSelectedId(value);
            setSelectedCandidate(options.find((option) => option.value === value) ?? null);
            setLastAction(null);
            save.reset();
          }}
          onQueryChange={setQuery}
          remote={{
            total: documents.data?.pages.at(-1)?.total ?? options.length,
            hasMore: documents.hasNextPage,
            loadingMore: documents.isFetchingNextPage,
            searching: documents.isFetching && !documents.isFetchingNextPage,
            onLoadMore: () => void documents.fetchNextPage(),
          }}
          disabled={save.isPending}
          width="lg"
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          size="md"
          icon={Save}
          onClick={() => submit(selectedId || null)}
          loading={save.isPending && lastAction === "set"}
          disabled={!isDirty || !selectedId || save.isPending}
        >
          {t("documents.version.save")}
        </Button>
        {superseded ? (
          <Button
            type="button"
            size="md"
            variant="secondary"
            icon={Undo2}
            onClick={() => submit(null)}
            loading={save.isPending && lastAction === "clear"}
            disabled={save.isPending}
          >
            {t("documents.version.clear")}
          </Button>
        ) : null}
        {save.isSuccess && !isDirty && lastAction ? (
          <FormStatus
            tone="success"
            message={
              lastAction === "set" ? t("documents.version.saved") : t("documents.version.cleared")
            }
          />
        ) : null}
        {save.isError ? (
          <FormStatus
            tone="danger"
            message={
              save.error instanceof ApiError
                ? save.error.message
                : t("documents.version.saveError")
            }
          />
        ) : null}
      </div>
    </section>
  );
}
