import {
  BookOpen,
  CircleAlert,
  Hash,
  Layers3,
  ImageIcon,
  ListChecks,
  Table2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useRef, type ReactNode, type Ref } from "react";

import { Banner, CopyButton, Disclosure, excerptHasTable, excerptPlainText, INFORMATION_LIST_SCROLL_CLASS, INFORMATION_TABLE_FOCUS_CLASS, cn } from "@production-ready/ui";
import type {
  DocumentElement,
  DocumentNavigationNode,
  ExtractionAsset,
  ExtractionField,
  ExtractionTable,
  ExtractionTableCell,
} from "@/lib/api";
import {
  parseStructuredExtraction,
  summarizeDocumentElements,
} from "@/lib/extraction";
import {
  layoutVisionDetails,
  elementCropUrl,
  elementVision,
} from "@/lib/layout-element";
import { extractionWarningView } from "@/lib/excel-warnings";
import { scrollFocusedControlIntoView } from "@/lib/focus-scroll";
import { formatNumber } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { tableCellKey, tableCellRef } from "@/lib/table-cell-focus";

import { DocumentSectionsPanel } from "./DocumentSectionsPanel";
import { ExtractedText, InfoChip } from "./extraction-bits";

/**
 * 要素の本文の表示。表の HTML を含むときだけ、根拠の抜粋と同じ共通の関数で「セル | セル」の行にする（#2387）。
 * 表でない本文はそのまま渡す（埋め込みの画像の data URI を `ExtractedText` がサムネイルにするため）。
 */
function elementDisplayText(text: string): string {
  return excerptHasTable(text) ? excerptPlainText(text) : text;
}

const KIND_LABELS: Record<string, Parameters<typeof t>[0]> = {
  title: "flow.extraction.kind.title",
  text: "flow.extraction.kind.text",
  list: "flow.extraction.kind.list",
  table: "flow.extraction.kind.table",
  figure: "flow.extraction.kind.figure",
  figure_caption: "flow.extraction.kind.figureCaption",
  table_caption: "flow.extraction.kind.tableCaption",
  header: "flow.extraction.kind.header",
  footer: "flow.extraction.kind.footer",
  code: "flow.extraction.kind.code",
  equation: "flow.extraction.kind.equation",
  other: "flow.extraction.kind.other",
};

/** RAG 取込で得た本文テキストを表示する。 */
export function DocumentRawText({ extraction }: { extraction: Record<string, unknown> }) {
  const { displayText } = parseStructuredExtraction(extraction);
  if (!displayText) {
    return <p className="text-sm text-fg-muted">{t("flow.extraction.noRawText")}</p>;
  }

  return (
    <section className="rounded-lg border border-border bg-surface-sunken p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold text-fg">
          {t("flow.extraction.rawText")}
        </h4>
        <CopyRawTextButton text={displayText} />
      </div>
      <ExtractedText text={displayText} />
    </section>
  );
}

/** RAG 取込で得た構造化要素・軽量メタデータを表示する。 */
export function DocumentExtraction({
  extraction,
  selectedElementId = null,
  selectedTableCellKey = null,
  focusRequestKey = null,
  focusSelectedElement = false,
  focusSelectedTableCell = false,
  onElementSelect,
  onTableCellSelect,
  documentId = null,
  recipeId = null,
  onPageSelect,
  omitWarning,
}: {
  extraction: Record<string, unknown>;
  /**
   * 同じ画面のほかの場所が出す warning（文書の先頭の Excel の表頭の確認の案内など）。ここでは出さない
   * （同じ状態の案内は 1 か所。UX 契約 messaging §10.2。#2034）。
   */
  omitWarning?: (warning: string) => boolean;
  /** 章節ナビゲーションの元にする処理レシピ（#713）。 */
  recipeId?: string | null;
  /** 章節を押したとき、プレビューをそのページへ移す（#713）。 */
  onPageSelect?: (page: number) => void;
  /** 指定時は図・表の Vision 詳細に切り出し画像(crop API)を表示する。 */
  documentId?: string | null;
  selectedElementId?: string | null;
  selectedTableCellKey?: string | null;
  focusRequestKey?: string | null;
  focusSelectedElement?: boolean;
  focusSelectedTableCell?: boolean;
  onElementSelect?: (elementId: string) => void;
  onTableCellSelect?: (table: ExtractionTable, cell: ExtractionTableCell) => void;
}) {
  const parsed = parseStructuredExtraction(extraction);
  const stats = summarizeDocumentElements(parsed.elements);
  const selectedElementRef = useRef<HTMLButtonElement | null>(null);
  const selectedTableCellRef = useRef<HTMLButtonElement | null>(null);
  const summarizedAssets = parsed.assets.filter((asset) => asset.summary);
  // warning は利用者の言葉の文にし、backend のコードは「詳細」に畳む（messaging §10.3。#2034）。
  const warningView = extractionWarningView(parsed.warnings, omitWarning);
  const hasSummary =
    parsed.documentType ||
    parsed.confidence != null ||
    warningView.messages.length > 0 ||
    parsed.elements.length > 0 ||
    parsed.tables.length > 0 ||
    parsed.navigation.length > 0 ||
    parsed.fields.length > 0 ||
    summarizedAssets.length > 0;

  useEffect(() => {
    if (!focusRequestKey || !selectedElementId || !selectedElementRef.current) return;
    scrollFocusedControlIntoView(selectedElementRef.current, {
      focus: focusSelectedElement,
    });
  }, [focusRequestKey, focusSelectedElement, selectedElementId]);

  useEffect(() => {
    if (!focusRequestKey || !selectedTableCellKey || !selectedTableCellRef.current) return;
    scrollFocusedControlIntoView(selectedTableCellRef.current, {
      focus: focusSelectedTableCell,
    });
  }, [focusRequestKey, focusSelectedTableCell, selectedTableCellKey]);

  if (!hasSummary) {
    return <p className="text-sm text-fg-muted">{t("flow.extraction.empty")}</p>;
  }

  return (
    <div className="space-y-4 rounded-lg border border-border bg-surface-sunken p-4">
      <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
        <MetadataItem
          label={t("flow.extraction.documentType")}
          value={parsed.documentType || "—"}
        />
        <MetadataItem
          label={t("flow.extraction.confidence")}
          value={confidenceText(parsed.confidence)}
        />
      </dl>

      {warningView.messages.length > 0 ? (
        <Banner severity="warning">
          <div className="min-w-0 space-y-2" data-testid="extraction-warnings">
            <ul className="space-y-1">
              {warningView.messages.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
            {warningView.codes.length > 0 ? (
              <Disclosure variant="plain" size="sm" summary={t("flow.error.details")}>
                <p className="break-all text-xs text-fg-muted">
                  {t("flow.extraction.warning.codes", { codes: warningView.codes.join(", ") })}
                </p>
              </Disclosure>
            ) : null}
          </div>
        </Banner>
      ) : null}

      {parsed.elements.length > 0 ? (
        <div
          className="grid grid-cols-2 gap-3 lg:grid-cols-4"
          aria-label={t("flow.extraction.structureStats")}
        >
          <StatTile
            icon={Layers3}
            label={t("flow.extraction.stats.elements")}
            value={formatNumber(stats.elementCount)}
          />
          <StatTile
            icon={BookOpen}
            label={t("flow.extraction.stats.pages")}
            value={formatNumber(stats.pageCount)}
          />
          <StatTile
            icon={Table2}
            label={t("flow.extraction.stats.tables")}
            value={formatNumber(stats.tableCount)}
          />
          <StatTile
            icon={ListChecks}
            label={t("flow.extraction.stats.lists")}
            value={formatNumber(stats.listCount)}
          />
        </div>
      ) : null}

      {/* 章節は要素の一覧より先に出す（#735）。文書の詳細では章節を修正・追加・削除できる（#713）。文書の無い表示は抽出結果の章節だけを出す。 */}
      {documentId ? (
        <DocumentSectionsPanel
          documentId={documentId}
          recipeId={recipeId}
          summaries={
            new Map(
              parsed.navigation.flatMap((node) =>
                node.summary ? [[node.section_id, node.summary] as const] : []
              )
            )
          }
          onPageSelect={onPageSelect}
        />
      ) : parsed.navigation.length > 0 ? (
        <NavigationTreePanel nodes={parsed.navigation} />
      ) : null}

      {parsed.elements.length > 0 ? (
        <section>
          <h4 className="mb-2 flex items-center gap-2 text-sm font-semibold text-fg">
            {t("flow.extraction.elements")}
            <span className="tnum text-xs font-normal text-fg-muted">
              {formatNumber(stats.elementCount)}
            </span>
          </h4>
          <ol className={cn("space-y-3 pr-1", INFORMATION_LIST_SCROLL_CLASS, INFORMATION_TABLE_FOCUS_CLASS)} tabIndex={0} aria-label={t("flow.extraction.elements")} data-testid="structured-elements-scroll">
            {parsed.elements.map((element) => (
              <ElementItem
                key={elementKey(element)}
                element={element}
                selected={elementKey(element) === selectedElementId}
                buttonRef={
                  elementKey(element) === selectedElementId ? selectedElementRef : undefined
                }
                onSelect={onElementSelect}
                visionDetails={layoutVisionDetails(extraction, elementKey(element))}
                cropUrl={documentId ? elementCropUrl(documentId, element) : null}
              />
            ))}
          </ol>
        </section>
      ) : null}

      {parsed.tables.some((table) => table.cells.length > 0) ? (
        <TableCellsPanel
          tables={parsed.tables}
          selectedTableCellKey={selectedTableCellKey}
          selectedTableCellRef={selectedTableCellRef}
          onTableCellSelect={onTableCellSelect}
        />
      ) : null}

      {summarizedAssets.length > 0 ? <AssetSummariesPanel assets={summarizedAssets} /> : null}
      {parsed.fields.length > 0 ? <ExtractionFieldsPanel fields={parsed.fields} /> : null}
    </div>
  );
}

/** 見出し+件数の折りたたみセクション(既存カードのトーンを踏襲)。 */
function CollapsibleSection({
  title,
  count,
  testId,
  children,
}: {
  title: string;
  count: number;
  testId: string;
  children: ReactNode;
}) {
  return (
    <Disclosure
      summary={title}
      meta={<span className="tnum text-xs font-normal text-fg-muted">{formatNumber(count)}</span>}
      data-testid={testId}
    >
      {children}
    </Disclosure>
  );
}

/** 章節 navigation tree(ナビゲーション要約が有効なら各章節の要約付き)。 */
function NavigationTreePanel({ nodes }: { nodes: DocumentNavigationNode[] }) {
  return (
    <CollapsibleSection
      title={t("flow.extraction.navigation.title")}
      count={nodes.length}
      testId="extraction-navigation"
    >
      <ol className="space-y-2">
        {nodes.map((node) => (
          <li
            key={node.section_id}
            style={{ paddingLeft: `calc(var(--space-4) * ${Math.min(node.depth, 6)})` }}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="break-words text-sm font-medium text-fg">
                {node.title}
              </span>
              {node.page_start != null ? (
                <span className="tnum rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
                  {t("flow.extraction.page", { page: node.page_start })}
                </span>
              ) : null}
            </div>
            {node.summary ? <p className="mt-0.5 text-xs text-fg-muted">{node.summary}</p> : null}
          </li>
        ))}
      </ol>
    </CollapsibleSection>
  );
}

/** 図表 VLM 要約(summary 付き asset のみ)。 */
function AssetSummariesPanel({ assets }: { assets: ExtractionAsset[] }) {
  return (
    <CollapsibleSection
      title={t("flow.extraction.assets.title")}
      count={assets.length}
      testId="extraction-asset-summaries"
    >
      <ul className="space-y-3">
        {assets.map((asset) => (
          <li key={asset.asset_id} className="rounded-md border border-border bg-surface-sunken p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span data-surface-tint="accent" className="rounded-full bg-accent-subtle px-2 py-0.5 text-xs font-medium text-accent-fg">
                {elementKindLabel(asset.kind)}
              </span>
              <span className="break-all text-xs font-medium text-fg">
                {asset.alt_text || asset.asset_id}
              </span>
              {typeof asset.page_number === "number" ? (
                <span className="tnum rounded-full bg-surface px-2 py-0.5 text-xs text-fg-muted">
                  {t("flow.extraction.page", { page: asset.page_number })}
                </span>
              ) : null}
            </div>
            <div className="mt-2">
              <ExtractedText text={asset.summary ?? ""} clamp />
            </div>
          </li>
        ))}
      </ul>
    </CollapsibleSection>
  );
}

/** メタデータ/項目抽出の結果(name / value / 型 / confidence)。 */
function ExtractionFieldsPanel({ fields }: { fields: ExtractionField[] }) {
  return (
    <CollapsibleSection
      title={t("flow.extraction.fields.title")}
      count={fields.length}
      testId="extraction-fields"
    >
      <dl className="space-y-2">
        {fields.map((field, index) => {
          const lowConfidence = typeof field.confidence === "number" && field.confidence < 0.65;
          return (
            <div
              key={`${field.name}-${index}`}
              className="flex flex-wrap items-baseline gap-2 rounded-md border border-border bg-surface-sunken p-2.5"
            >
              <dt className="text-xs font-medium text-fg-muted">{field.name}</dt>
              <dd className="min-w-0 break-words text-sm font-medium text-fg">
                {field.value}
              </dd>
              <span className="rounded-full bg-surface px-2 py-0.5 text-xs text-fg-muted">
                {field.value_type}
              </span>
              {typeof field.confidence === "number" ? (
                <span
                  data-surface-tint={lowConfidence ? "warning" : "success"}
                  className={`tnum inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                    lowConfidence ? "bg-warning-subtle text-warning-fg" : "bg-success-subtle text-success-fg"
                  }`}
                >
                  {lowConfidence ? <CircleAlert size={14} aria-hidden /> : null}
                  {confidenceText(field.confidence)}
                </span>
              ) : null}
            </div>
          );
        })}
      </dl>
    </CollapsibleSection>
  );
}

function MetadataItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="tnum mt-0.5 font-medium text-fg">{value}</dd>
    </div>
  );
}

function StatTile({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-md border border-border bg-surface p-3">
      <div className="flex items-center gap-2 text-xs font-medium text-fg-muted">
        <Icon size={14} className="text-accent-fg" aria-hidden />
        <span>{label}</span>
      </div>
      <p className="tnum mt-2 text-lg font-semibold text-fg">{value}</p>
    </div>
  );
}

function ElementItem({
  element,
  selected,
  buttonRef,
  onSelect,
  visionDetails = null,
  cropUrl = null,
}: {
  element: DocumentElement;
  selected: boolean;
  buttonRef?: Ref<HTMLButtonElement>;
  onSelect?: (elementId: string) => void;
  visionDetails?: ReturnType<typeof layoutVisionDetails>;
  cropUrl?: string | null;
}) {
  const id = elementKey(element);
  const lowConfidence = typeof element.confidence === "number" && element.confidence < 0.65;
  const vision = elementVision(element);
  return (
    <li>
      <button
        data-surface-tint={selected ? "accent" : undefined}
        ref={buttonRef}
        type="button"
        className={`w-full rounded-md border p-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring ${
          selected ? "border-accent-emphasis bg-accent-subtle" : "border-border bg-surface hover:bg-surface-hover"
        }`}
        aria-pressed={selected}
        onClick={() => onSelect?.(id)}
      >
        <div className="flex flex-wrap items-center gap-2">
          <span data-surface-tint="accent" className="rounded-full bg-accent-subtle px-2 py-0.5 text-xs font-medium text-accent-fg">
            {elementKindLabel(element.kind)}
          </span>
          {element.content_kind ? (
            <span className="rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
              {element.content_kind}
            </span>
          ) : null}
          {typeof element.page_number === "number" ? (
            <span className="tnum rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
              {t("flow.extraction.page", { page: element.page_number })}
            </span>
          ) : null}
          {element.section_path?.length ? (
            <span data-surface-tint="info" className="min-w-0 max-w-full rounded-full bg-info-subtle px-2 py-0.5 text-xs text-info-fg">
              <span className="break-words">{element.section_path.join(" > ")}</span>
            </span>
          ) : null}
          {typeof element.confidence === "number" ? (
            <span
              data-surface-tint={lowConfidence ? "warning" : "success"}
              className={`tnum inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                lowConfidence ? "bg-warning-subtle text-warning-fg" : "bg-success-subtle text-success-fg"
              }`}
            >
              {lowConfidence ? <CircleAlert size={14} aria-hidden /> : null}
              {confidenceText(element.confidence)}
            </span>
          ) : null}
        </div>
        <div className="mt-2">
          <ExtractedText text={elementDisplayText(element.text)} clamp />
        </div>
        {vision?.retrievalText ? (
          <p className="mt-2 line-clamp-3 border-l-2 border-accent-emphasis pl-2 text-xs leading-relaxed text-fg-muted">
            {vision.retrievalText}
          </p>
        ) : null}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {vision?.status ? (
            <InfoChip
              icon={ImageIcon}
              label={t("flow.extraction.vision.status", { status: visionStatusLabel(vision.status) })}
            />
          ) : null}
          {vision?.excluded ? (
            <InfoChip icon={ImageIcon} label={t("flow.extraction.vision.excluded")} />
          ) : null}
          <InfoChip
            icon={Hash}
            label={element.source_parser ? `${id} / ${element.source_parser}` : id}
          />
        </div>
      </button>
      {visionDetails ? <VisionDetails details={visionDetails} cropUrl={cropUrl} /> : null}
    </li>
  );
}

function TableCellsPanel({
  tables,
  selectedTableCellKey,
  selectedTableCellRef,
  onTableCellSelect,
}: {
  tables: ExtractionTable[];
  selectedTableCellKey: string | null;
  selectedTableCellRef: Ref<HTMLButtonElement>;
  onTableCellSelect?: (table: ExtractionTable, cell: ExtractionTableCell) => void;
}) {
  return (
    <section>
      <h4 className="mb-2 text-sm font-semibold text-fg">
        {t("flow.extraction.tableCells")}
      </h4>
      <div className="max-h-[30rem] space-y-3 overflow-auto pr-1">
        {tables
          .filter((table) => table.cells.length > 0)
          .map((table) => (
            <div
              key={table.table_id}
              className="rounded-md border border-border bg-surface p-3"
              data-testid="extraction-table-cells"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="break-all text-xs font-medium text-fg">
                  {table.caption || table.table_id}
                </span>
                {typeof table.page_number === "number" ? (
                  <span className="tnum rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
                    {t("flow.extraction.page", { page: table.page_number })}
                  </span>
                ) : null}
              </div>
              <div className="mt-2 overflow-auto">
                <div className="grid min-w-full gap-1" style={tableGridStyle(table)}>
                  {table.cells.map((cell) => {
                    const key = tableCellKey(table.table_id, cell);
                    const selected = key === selectedTableCellKey;
                    const ref = tableCellRef(cell);
                    return (
                      <button
                        key={key}
                        data-surface-tint={selected ? "accent" : undefined}
                        ref={selected ? selectedTableCellRef : undefined}
                        type="button"
                        className={`min-h-11 rounded border px-2 py-1.5 text-left text-xs transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring ${
                          selected
                            ? "border-accent-emphasis bg-accent-subtle text-fg"
                            : "border-border bg-surface-sunken text-fg hover:bg-surface"
                        }`}
                        aria-pressed={selected}
                        aria-label={tableCellAriaLabel(table, cell)}
                        data-testid="extraction-table-cell"
                        onClick={() => onTableCellSelect?.(table, cell)}
                      >
                        <span className="flex flex-wrap items-center gap-1">
                          <span className="tnum rounded bg-surface px-1.5 py-0.5 text-xs text-fg-muted">
                            {ref || t("flow.extraction.tableCellPosition", {
                              row: cell.row + 1,
                              col: cell.col + 1,
                            })}
                          </span>
                          {cell.bbox ? (
                            <span data-surface-tint="success" className="rounded bg-success-subtle px-1.5 py-0.5 text-xs text-success-fg">
                              bbox
                            </span>
                          ) : null}
                        </span>
                        <span className="mt-1 block">
                          <ExtractedText text={cell.text} />
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          ))}
      </div>
    </section>
  );
}

function tableGridStyle(table: ExtractionTable) {
  const columnCount = Math.max(
    1,
    ...table.cells.map((cell) => cell.col + Math.max(1, cell.col_span))
  );
  return {
    gridTemplateColumns: `repeat(${columnCount}, minmax(7rem, 1fr))`,
  };
}

function tableCellAriaLabel(table: ExtractionTable, cell: ExtractionTableCell): string {
  const ref = tableCellRef(cell);
  return t("flow.extraction.tableCellAria", {
    table: table.caption || table.table_id,
    cell: ref || t("flow.extraction.tableCellPosition", {
      row: cell.row + 1,
      col: cell.col + 1,
    }),
    text: cell.text || "—",
  });
}

function CopyRawTextButton({ text }: { text: string }) {
  return (
    <CopyButton
      text={text}
      variant="ghost"
      size="sm"
      label={t("flow.extraction.copyRawText")}
      copiedMessage={t("flow.extraction.copied")}
      failedMessage={t("flow.extraction.copyFailed")}
    />
  );
}

/** rag_poc viewer の VisualDetails 相当: Vision が読み取った構造化項目と除外理由。 */
function VisionDetails({
  details,
  cropUrl,
}: {
  details: NonNullable<ReturnType<typeof layoutVisionDetails>>;
  cropUrl: string | null;
}) {
  return (
    <Disclosure
      summary={t("flow.extraction.vision.details")}
      size="sm"
      className="mt-1"
      contentClassName="space-y-2"
    >
      {cropUrl ? (
        <img
          src={cropUrl}
          alt={t("flow.extraction.vision.cropAlt")}
          loading="lazy"
          className="max-h-48 max-w-full rounded border border-border bg-surface-sunken object-contain"
        />
      ) : null}
      {details.excludedReason ? (
        <p className="text-xs text-fg-muted">
          {t("flow.extraction.vision.excludedReason", { reason: details.excludedReason })}
        </p>
      ) : null}
      {details.lines.length ? (
        <dl className="space-y-1.5 text-xs">
          {details.lines.map((line) => (
            <div key={line.field}>
              <dt className="font-medium text-fg">
                {t(`flow.extraction.vision.field.${line.field}` as I18nKey)}
              </dt>
              <dd className="whitespace-pre-wrap break-words text-fg-muted">{line.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </Disclosure>
  );
}

function visionStatusLabel(status: string): string {
  if (status === "succeeded") return t("flow.extraction.vision.succeeded");
  if (status === "failed") return t("flow.extraction.vision.failed");
  if (status === "skipped") return t("flow.extraction.vision.skipped");
  return status;
}

function elementKindLabel(kind: string): string {
  return t(KIND_LABELS[kind] ?? "flow.extraction.kind.other");
}

function elementKey(element: DocumentElement): string {
  return element.element_id || `el-${String(element.order).padStart(4, "0")}`;
}

function confidenceText(value: number | null): string {
  return value == null ? "—" : `${formatNumber(Math.round(value * 100))}%`;
}
