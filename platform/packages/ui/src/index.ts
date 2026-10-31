// @production-ready/ui — 公開 API バレル
//
// デザイントークン CSS は別 export（"@production-ready/ui/tokens.css"）で取り込む。

// --- lib ---
export { cn } from "./lib/utils";
export { surfaceTintFor, type SurfaceTint } from "./lib/surface-tint";
export {
  INFORMATION_LIST_VISIBLE_ROWS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  INFORMATION_TABLE_FIXED_VISIBLE_ROWS,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_LIST_ROW_CLASS,
  INFORMATION_LIST_SCROLL_CLASS,
  INFORMATION_COMPACT_LIST_FIVE_ROW_SCROLL_CLASS,
  INFORMATION_TABLE_FOCUS_CLASS,
} from "./lib/list-density";
export { isImeComposing, isSubmitEnter, type KeyboardEventLike } from "./lib/keyboard";
export { normalizeBasePath, routerBasename, stripBasePath, withBasePath } from "./lib/base-path";
export { compareNames, type NameSortDirection } from "./lib/name-collation";
export {
  DEFAULT_DISPLAY_TIME_ZONE,
  DISPLAY_DATE_TIME_SECONDS,
  DISPLAY_TIME_SECONDS,
  displayDateTimeFormat,
  displayDateTimeParts,
  displayFileTimestamp,
  displayTimeZone,
  formatDisplayCalendarDate,
  formatDisplayDateTime,
  isSupportedTimeZone,
  setDisplayTimeZone,
  type DisplayDateTimeFormat,
  type DisplayDateTimeParts,
} from "./lib/date-time";
export { isRepeatedActivationKey, runStopClickAction, type RunStopAction } from "./lib/run-stop";
export { useActionPending, type ActionPending } from "./lib/action-pending";
export {
  createOptimisticChatMessage,
  withOptimisticChatStatus,
  type OptimisticChatMessage,
  type OptimisticChatStatus,
} from "./lib/chat-optimistic";
export {
  splitMarkdownTables,
  toTabularData,
  type MarkdownTableSegment,
  type TabularCellValue,
  type TabularColumn,
  type TabularData,
} from "./lib/tabular-data";
export {
  excerptHasTable,
  excerptPlainText,
  parseExcerpt,
  type ExcerptBlock,
  type ExcerptTableCell,
} from "./lib/excerpt-text";
export { ExcerptContent, type ExcerptContentProps } from "./components/data/excerpt-content";
export {
  CONTENT_LANG_SCAN_LIMIT,
  detectContentLang,
  normalizeContentLang,
  resolveContentLang,
  uiTextLang,
  type ContentLang,
} from "./lib/content-lang";
export {
  parseMarkdownBlocks,
  parseMarkdownInline,
  safeMarkdownHref,
  type MarkdownBlock,
  type MarkdownInline,
} from "./lib/markdown";

// --- UI primitives ---
export { Button, buttonVariants, type ButtonProps, type ButtonVariantToneProps } from "./components/ui/button";
export { ButtonLink, type ButtonLinkComponent, type ButtonLinkProps } from "./components/ui/button-link";
export { Tooltip, type TooltipProps, type TooltipPlacement } from "./components/ui/tooltip";
export { InfoTip, INFO_TIP_SHOW_DELAY_MS, type InfoTipProps } from "./components/ui/info-tip";
export { DEFAULT_TAB_INVALID_LABEL, Tabs, TabPanel, type TabItem, type TabsProps } from "./components/ui/tabs";
export { TextField, type TextFieldProps, type TextFieldSize } from "./components/ui/text-field";
export {
  CONTROL_HEIGHT_CLASS,
  CONTROL_MIN_HEIGHT_CLASS,
  FIELD_BOX_TEXT_MIN_WIDTH_CLASS,
  FIELD_BOX_TEXT_WIDTH_CLASS,
  FIELD_BOX_WIDTH_CLASS,
  FIELD_CONTROL_WIDTH_CLASS,
  FIELD_TEXT_CONTAIN_CLASS,
  FIELD_TEXT_MIN_WIDTH_REM,
  FIELD_WIDTH_CLASS,
  FIELD_WIDTH_REM,
  fieldControlClassName,
  fieldWidthClass,
  labeledFieldWidthClasses,
  FIELD_WIDTH_CONTAINER_CLASS,
  FIELD_BOX_WIDTH_CONTAINER_CLASS,
  FIELD_CONTROL_WIDTH_CONTAINER_CLASS,
  FIELD_TEXT_CONTAIN_CONTAINER_CLASS,
  FIELD_BOX_TEXT_MIN_WIDTH_CONTAINER_CLASS,
  FIELD_BOX_TEXT_WIDTH_CONTAINER_CLASS,
  type FieldWidthScope,
  type ControlSize,
  type FieldControlClassNameOptions,
  type FieldWidth,
} from "./components/ui/control-size";
export { FieldActionRow, type FieldActionRowProps } from "./components/ui/field-action-row";
export { FieldWidthScopeProvider, useFieldWidthScope } from "./components/ui/field-width-scope";
export { RunStopButton, type RunStopButtonProps } from "./components/ui/run-stop-button";
export { TextareaField, defaultTextareaCount, type TextareaFieldProps } from "./components/ui/textarea-field";
export { FORM_GRID_TWO_COLUMN_MIN_REM, FormGrid, type FormGridProps } from "./components/ui/form-grid";
export {
  measurePlaceholderClip,
  placeholderOverflows,
  usePlaceholderFit,
  type PlaceholderFitInput,
} from "./components/ui/placeholder-fit";
export {
  SearchField,
  SEARCH_FIELD_DEBOUNCE_MS,
  trimSearchValue,
  type SearchFieldProps,
} from "./components/ui/search-field";
export {
  SecretField,
  type SecretFieldProps,
  type SecretFieldClearOption,
} from "./components/ui/secret-field";
export { RequiredBadge, DEFAULT_REQUIRED_LABEL } from "./components/ui/required-badge";
export { FieldLabel, FieldLegend, Fieldset, type FieldsetProps } from "./components/ui/field-label";
export {
  ExecutionConfirmationField,
  executionConfirmationStatus,
  DEFAULT_EXECUTION_CONFIRMATION_LABELS,
  type ExecutionConfirmationFieldProps,
  type ExecutionConfirmationLabels,
  type ExecutionConfirmationStatus,
} from "./components/ui/execution-confirmation-field";
export { Spinner, type SpinnerProps } from "./components/ui/spinner";
export {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "./components/ui/card";
export {
  Skeleton,
  TableSkeleton,
  ListSkeleton,
  FormSkeleton,
  ChatSkeleton,
  SKELETON_CLASS,
  type SkeletonRows,
  type TableSkeletonProps,
  type ListSkeletonProps,
  type FormSkeletonProps,
  type ChatSkeletonProps,
} from "./components/ui/skeleton";
export { Switch, type SwitchProps } from "./components/ui/switch";
export { ToggleChip } from "./components/ui/toggle-chip";
export { FieldError } from "./components/ui/field-error";
export { FormStatus } from "./components/ui/form-status";
export {
  SelectField,
  type SelectFieldOption,
} from "./components/ui/select-field";
export {
  SearchableSelectField,
  type SearchableSelectFieldProps,
} from "./components/ui/searchable-select-field";
export {
  SearchableMultiSelect,
  type SearchableMultiSelectProps,
} from "./components/ui/searchable-multi-select";
export {
  DEFAULT_SEARCHABLE_SELECT_LABELS,
  filterSearchableOptions,
  type SearchableSelectOption,
  type SearchableSelectRemote,
  type SearchableSelectLabels,
  type SearchableMultiSelectLabels,
} from "./components/ui/searchable-options";
export { Banner } from "./components/ui/banner";
export {
  ApiErrorBanner,
  ApiErrorDetailList,
  ApiErrorState,
  type ApiErrorBannerProps,
  type ApiErrorDetailListProps,
  type ApiErrorStateProps,
} from "./components/feedback/api-error-banner";
export {
  DEFAULT_PROCESS_FAILURE_LABELS,
  ProcessFailureNotice,
  failedStepName,
  processFailureDetails,
  processFailureFacts,
  type ProcessFailure,
  type ProcessFailureLabels,
  type ProcessFailureNoticeProps,
} from "./components/feedback/process-failure";
export {
  ApiTransportError,
  DEFAULT_API_ERROR_DETAIL_LABELS,
  DEFAULT_API_TRANSPORT_MESSAGES,
  apiErrorDetail,
  apiErrorMessage,
  apiRequestPath,
  httpApiErrorPresentation,
  isAbortError,
  isNetworkFailure,
  isTimeoutError,
  isTransportError,
  presentApiError,
  toApiTransportError,
  transportErrorOf,
  type ApiErrorDetail,
  type ApiErrorDetailLabels,
  type ApiErrorPresentable,
  type ApiErrorPresentation,
  type ApiFieldErrorLike,
  type ApiTransportFailureKind,
  type ApiTransportMessages,
  type ApiTransportRequest,
} from "./lib/api-error";
export { MessageText, type MessageTextProps } from "./components/ui/message-text";
export {
  MarkdownInlineText,
  MarkdownText,
  type MarkdownTextLabels,
  type MarkdownTextProps,
} from "./components/ui/markdown-text";
export {
  ChatUserMessage,
  type ChatUserMessageProps,
  type ChatUserMessageStatus,
} from "./components/ui/chat-message";
export {
  ChatLayout,
  type ChatLayoutLatest,
  type ChatLayoutNewConversation,
  type ChatLayoutProps,
  type ChatLayoutTestIds,
} from "./components/chat/chat-layout";
export {
  ChatComposer,
  ChatComposerOption,
  type ChatComposerOptionProps,
  type ChatComposerProps,
} from "./components/chat/chat-composer";
export {
  ChatReadOnlyNotice,
  DEFAULT_CHAT_READ_ONLY_NOTICE_LABELS,
  type ChatReadOnlyNoticeLabels,
  type ChatReadOnlyNoticeProps,
} from "./components/chat/chat-read-only-notice";
export {
  ChatAttachmentList,
  formatAttachmentSize,
  type ChatAttachmentItem,
  type ChatAttachmentKind,
  type ChatAttachmentListLabels,
  type ChatAttachmentListProps,
  type ChatAttachmentStatus,
  type ChatComposerAttachmentLabels,
  type ChatComposerAttachments,
} from "./components/chat/chat-attachments";
export {
  chatAttachmentItems,
  chatAttachmentUploadResult,
  useChatAttachments,
  useChatComposerAttachments,
  type ChatComposerAttachmentsApi,
  type ChatComposerAttachmentTexts,
  type ChatAttachmentApiItem,
  type ChatAttachmentApiLimits,
  type ChatAttachmentApiReference,
  type ChatAttachmentMessages,
  type ChatAttachmentUploadResult,
  type UseChatAttachmentsOptions,
} from "./components/chat/use-chat-attachments";
export {
  ChatHistoryList,
  type ChatHistoryItem,
  type ChatHistoryListLabels,
  type ChatHistoryPagination,
  type ChatHistoryListProps,
  type ChatHistoryListTestIds,
} from "./components/chat/chat-history-list";
export { chatHistoryWindowLabels, type ChatHistoryWindowLabels } from "./components/chat/chat-history-window";
export {
  CHAT_HISTORY_TITLE_MAX_LENGTH,
  DEFAULT_CHAT_HISTORY_ACTION_LABELS,
  useChatHistoryActions,
  type ChatHistoryActionLabels,
  type ChatHistoryActions,
  type UseChatHistoryActionsOptions,
} from "./components/chat/use-chat-history-actions";
export {
  ClarificationCard,
  clarificationAnswers,
  isClarificationEntryAnswered,
  type ClarificationAnswer,
  type ClarificationBusyAction,
  type ClarificationCardProps,
  type ClarificationDraft,
  type ClarificationDraftEntry,
  type ClarificationItem,
  type ClarificationOption,
} from "./components/chat/clarification-card";
export {
  ClarificationRecord,
  clarificationRecordAnswerText,
  type ClarificationRecordEntry,
  type ClarificationRecordProps,
} from "./components/chat/clarification-record";
export {
  DEFAULT_CLARIFICATION_LABELS,
  clarificationLabels,
  type ClarificationAnswerKind,
  type ClarificationLabels,
  type ClarificationNouns,
} from "./components/chat/clarification-labels";
export {
  ChatSqlBlock,
  DEFAULT_SQL_BLOCK_LABELS,
  SqlSafetyBadge,
  sqlBlockLabels,
  type ChatSqlBlockProps,
  type SqlBlockLabels,
} from "./components/chat/chat-sql-block";
export {
  ANSWER_SECTION_COLLAPSE_AFTER,
  AnswerCitationLinks,
  AnswerPlainText,
  AnswerSections,
  DEFAULT_ANSWER_SECTIONS_LABELS,
  type AnswerCitationRef,
  type AnswerPlainTextProps,
  type AnswerSectionEntry,
  type AnswerSectionsBlock,
  type AnswerSectionsLabels,
  type AnswerSectionsProps,
} from "./components/chat/answer-sections";
// 回答の見出しと、チャットの回答の本文の見出し「回答」（3 製品共通。#3433）
export {
  AnswerHeading,
  ChatAnswerBody,
  DEFAULT_CHAT_ANSWER_BODY_LABELS,
  type AnswerHeadingProps,
  type ChatAnswerBodyLabels,
  type ChatAnswerBodyProps,
} from "./components/chat/answer-heading";
// 回答の段落の印・外した段落・回答全体の注意（RAG・Agent 共通。#2738）
export {
  ANSWER_PASSAGE_STATE_ICON,
  AnswerPassageBadges,
  AnswerPassageMarked,
  AnswerPassageWarnings,
  DEFAULT_ANSWER_PASSAGE_LABELS,
  answerPassageQuote,
  answerPassageReasonText,
  focusAnswerPassage,
  type AnswerPassageLabels,
} from "./components/chat/answer-passage-marks";
export {
  answerPassageCounts,
  answerPassageKey,
  hasAnswerPassageRecords,
  matchAnswerPassageMarks,
  parseAnswerPassageRecords,
  type AnswerPassageCounts,
  type AnswerPassageMark,
  type AnswerPassageMarkGroup,
  type AnswerPassageMarkState,
  type AnswerPassageReason,
  type AnswerPassageRecords,
  type AnswerRemovedPassage,
  type AnswerWarning,
} from "./components/chat/answer-passage-records";
export {
  AnswerEvidence,
  DEFAULT_EVIDENCE_LABELS,
  EVIDENCE_LIST_CLASS,
  EvidenceAnswerUseBadge,
  EvidenceCard,
  EvidenceList,
  EvidenceSourceSheet,
  EvidenceSourceButton,
  EvidenceWithheldNote,
  evidenceCountLabel,
  useEvidenceSource,
  type EvidenceSourceSheetProps,
  type EvidenceSourceState,
  evidenceLabels,
  evidenceScoreMeterPercent,
  formatEvidenceScore,
  type AnswerEvidenceProps,
  type EvidenceAnswerUse,
  type EvidenceCardProps,
  type EvidenceChip,
  type EvidenceLabels,
  type EvidenceListItem,
  type EvidenceListProps,
  type EvidenceTag,
} from "./components/chat/answer-evidence";
// 回答の注意書き（3 製品共通。#2405）
export {
  AnswerNotice,
  answerNoticeSegments,
  type AnswerNoticeProps,
  type AnswerNoticeSegment,
} from "./components/chat/answer-notice";
// チャットの回答のカードの末尾の並び（注意書き → 回答の根拠 → 評価。3 製品共通。#2707）
export { AnswerFooter, type AnswerFooterProps } from "./components/chat/answer-footer";
export {
  ChatAnswer,
  ChatPendingTurn,
  ChatTurn,
  type ChatAnswerProps,
  type ChatPendingTurnProps,
  type ChatTurnProps,
} from "./components/chat/chat-turn";
export {
  CHAT_HISTORY_INLINE_QUERY,
  useChatHistoryPanel,
  useMediaQuery,
  type ChatHistoryPanel,
  type UseChatHistoryPanelOptions,
} from "./components/chat/use-chat-history-panel";
export {
  CHAT_AUTO_SCROLL_THRESHOLD_PX,
  useChatAutoScroll,
  type ChatAutoScroll,
  type UseChatAutoScrollOptions,
} from "./components/chat/use-chat-auto-scroll";
export { Toaster, type ToasterProps } from "./components/ui/toast";
export {
  ConfirmProvider,
  useConfirm,
  type ConfirmOptions,
  type ConfirmDefaultLabels,
} from "./components/ui/confirm-dialog";
export { SideSheet, type SideSheetProps } from "./components/ui/side-sheet";
export { ContentActionBar } from "./components/ui/content-action-bar";
export {
  FormActionBar,
  entityActionToFormAction,
  type FormActionBarProps,
  type FormActionDescriptor,
} from "./components/ui/form-action-bar";
export {
  DisclosureChevron,
  type DisclosureChevronProps,
} from "./components/ui/disclosure-chevron";
export { TEXT_ICON_SIZE, type TextIconSize } from "./components/ui/text-icon";
export {
  Disclosure,
  type DisclosureProps,
  type DisclosureSize,
  type DisclosureSurface,
  type DisclosureTone,
  type DisclosureVariant,
} from "./components/ui/disclosure";
export {
  FloatingActionMenu,
  type FloatingMenuPlacement,
} from "./components/ui/floating-menu";
export { restoreMenuTriggerFocus } from "./lib/menu-focus";
export {
  BulkSelectionActions,
  type BulkSelectionActionsProps,
} from "./components/ui/bulk-selection-actions";
export {
  ClearActionButton,
  type ClearActionButtonProps,
} from "./components/ui/clear-action-button";
export { COPY_FEEDBACK_MS, CopyButton, type CopyButtonProps } from "./components/ui/copy-button";
export { copyTextToClipboard } from "./lib/clipboard";
export {
  toneIcon,
  toneText,
  toneSurface,
  toneRole,
  type FeedbackTone,
} from "./components/ui/feedback-tone";

// --- feedback / state views ---
export {
  LoadingState,
  ErrorState,
  EmptyState,
} from "./components/feedback/state-views";
export {
  ProcessingIndicator,
  TimedLoadingState,
  useOperationTiming,
  DEFAULT_PROCESSING_LABELS,
  type ProcessingIndicatorProps,
  type TimedLoadingStateProps,
  type ProcessingPlacement,
  type ProcessingActivityIcon,
  type ProcessingLabels,
  type UseOperationTimingOptions,
  type OperationTiming,
} from "./components/feedback/processing-state";
export { PageLoadingState, type PageLoadingStateProps } from "./components/feedback/page-loading-state";
export {
  ChatProgress,
  chatProgressTimelineStep,
  DEFAULT_CHAT_PROGRESS_LABELS,
  formatChatProgressDuration,
  isChatProgressActive,
  useChatProgressSteps,
  type ChatProgressLabels,
  type ChatProgressProps,
  type ChatProgressStep,
  type ChatProgressStepStatus,
  type UseChatProgressStepsOptions,
} from "./components/feedback/chat-progress";
export {
  CHAT_PROGRESS_STATUS_RANK,
  mergeChatProgressSteps,
  reduceChatProgressSteps,
  type ChatProgressStepsInput,
  type ChatProgressStepsState,
} from "./components/feedback/chat-progress-steps";
export {
  CHAT_PROGRESS_SCHEMA_VERSION,
  CHAT_PROGRESS_SSE_EVENT,
  CHAT_PROGRESS_SSE_HEARTBEAT_EVENT,
  CHAT_PROGRESS_STEP_STATUSES,
  CHAT_PROGRESS_TERMINAL_STATUSES,
  chatProgressHasSpans,
  chatProgressLabelState,
  chatProgressStepsFromEvents,
  chatProgressTerminalOf,
  chatProgressTraceIdOf,
  createChatProgressEventsState,
  labelChatProgressSteps,
  parseChatProgressEvent,
  parseChatProgressEvents,
  parseChatProgressPage,
  parseChatProgressStepDetails,
  reduceChatProgressEvents,
  type ChatProgressEvent,
  type ChatProgressEventSpan,
  type ChatProgressEventStep,
  type ChatProgressEventsInput,
  type ChatProgressLabelOptions,
  type ChatProgressEventsState,
  type ChatProgressLabelState,
  type ChatProgressPage,
  type ChatProgressParamValue,
  type ChatProgressParams,
  type ChatProgressStepDefinition,
  type ChatProgressStepDefinitions,
  type ChatProgressStepEvent,
  type ChatProgressTerminalEvent,
  type ChatProgressTerminalStatus,
} from "./components/feedback/chat-progress-events";
export {
  useChatProgressStream,
  CHAT_PROGRESS_SSE_MAX_ERRORS,
  DEFAULT_CHAT_PROGRESS_POLL_INTERVAL_MS,
  type ChatProgressStream,
  type ChatProgressStreamOptions,
  type ChatProgressTransport,
} from "./components/feedback/chat-progress-stream";
export {
  useChatProgressTracker,
  chatProgressBackoffMs,
  DEFAULT_CHAT_PROGRESS_STALE_AFTER_MS,
  DEFAULT_CHAT_PROGRESS_REFRESH_TIMEOUT_MS,
  DEFAULT_CHAT_PROGRESS_MAX_BACKOFF_MS,
  type ChatProgressTracker,
  type ChatProgressTrackerOptions,
  type ChatProgressTrackerProps,
} from "./components/feedback/chat-progress-tracker";
export {
  operationTimestampMs,
  elapsedMsSince,
  elapsedMsBetween,
  formatElapsedClock,
  type OperationTimestamp,
} from "./lib/operation-timing";
export {
  BlockedPageNotice,
  type BlockedPageNoticeProps,
} from "./components/feedback/blocked-page-notice";
export {
  ActionResultRegion,
  type ActionResultRegionProps,
} from "./components/feedback/action-result-region";
export {
  SaveErrorBanner,
  type SaveErrorBannerProps,
} from "./components/feedback/save-error-banner";
// 作成・発行の直後に 1 回だけ見せる秘密（API キーなど。#1805）
export { OneTimeSecret, type OneTimeSecretProps } from "./components/feedback/one-time-secret";
export {
  PreconditionBanner,
  type PreconditionBannerLink,
  type PreconditionBannerProps,
} from "./components/feedback/precondition-banner";
export {
  FeedbackControls,
  isSameFeedback,
  type FeedbackControlsLabels,
  type FeedbackControlsProps,
  type FeedbackControlsSubmission,
  type FeedbackControlsValue,
  type FeedbackRating,
  type FeedbackReasonOption,
} from "./components/feedback/feedback-controls";

// --- data ---
export {
  DEFAULT_ROADMAP_LABEL,
  RoadmapBadge,
  StatusBadge,
  type StatusVariant,
} from "./components/data/status-badge";
export {
  Pagination,
  OffsetPagination,
  CursorPagination,
  usePagination,
  useCursorPages,
  offsetPagination,
  offsetForPage,
  lastPageOffset,
  outOfRangeOffset,
  cursorPagination,
  paginationRange,
  formatPaginationNumber,
  parsePageInput,
  shouldShowPageSize,
  DEFAULT_PAGE_SIZE,
  DEFAULT_PAGINATION_LABELS,
  type PaginationMode,
  type PaginationProps,
  type OffsetPaginationProps,
  type CursorPaginationProps,
  type PaginationLabels,
  type PaginationRange,
  type UsePaginationOptions,
} from "./components/data/pagination";
export {
  PageSizeSelect,
  PAGE_SIZE_OPTIONS,
  DEFAULT_PAGE_SIZE_SELECT_LABELS,
  type PageSizeSelectProps,
  type PageSizeSelectLabels,
} from "./components/data/page-size-select";
export { PagedDataTable, type PagedDataTableProps } from "./components/data/paged-data-table";
export {
  publishedVersionColumns,
  type PublishedVersionColumnLabels,
  type PublishedVersionColumnsOptions,
} from "./components/data/published-version-columns";
export {
  ResultTable,
  ResultCell,
  RESULT_PAGE_SIZES,
  RESULT_PREVIEW_ROWS,
  DEFAULT_RESULT_TABLE_LABELS,
  resultSummaryText,
  isNullResultValue,
  isNumericResultColumn,
  resultRowsToCsv,
  resultValueText,
  type ResultFullResultLink,
  type ResultTableColumn,
  type ResultTableLabels,
  type ResultTableProps,
  // 以前の名前（#1154。後方互換の別名。#1178）
  ChatResultTable,
  CHAT_RESULT_PAGE_SIZES,
  CHAT_RESULT_PREVIEW_ROWS,
  DEFAULT_CHAT_RESULT_TABLE_LABELS,
  chatResultSummaryText,
  type ChatResultFullResultLink,
  type ChatResultTableColumn,
  type ChatResultTableLabels,
  type ChatResultTableProps,
} from "./components/data/result-table";
export {
  RowTitleButton,
  type RowTitleButtonProps,
  type RowTitleButtonMaxLines,
} from "./components/data/row-title-button";
export {
  RowActionMenu,
  ObjectActionBar,
  splitObjectActions,
  visibleEntityActions,
  type EntityAction,
  type EntityActionTone,
} from "./components/data/object-actions";
export {
  DataTable,
  type DataTableProps,
  type DataTableColumn,
  type DataTableSort,
  type DataTableRowProps,
  type DataTableVisibleRows,
  type SortDirection,
} from "./components/data/data-table";
export {
  ListToolbar,
  DEFAULT_LIST_TOOLBAR_LABELS,
  LIST_TOOLBAR_NARROW_REM,
  type ListToolbarProps,
  type ListToolbarLabels,
} from "./components/data/list-toolbar";
export {
  FilterSelect,
  FilterChipGroup,
  FILTER_CHIP_MAX_OPTIONS,
  isFilterActive,
  type FilterSelectProps,
  type FilterChipGroupProps,
  type FilterChipOption,
} from "./components/data/list-filters";
export {
  RunStatusBadge,
  RUN_STATUSES,
  DEFAULT_RUN_STATUS_LABELS,
  runStatusVariant,
  runStatusIcon,
  isRunActive,
  type RunStatus,
  type RunStatusLabels,
  type RunStatusBadgeProps,
} from "./components/data/run-status";
export {
  RunHistoryTable,
  RunHistoryToolbar,
  RunHistoryPeriodEmptyState,
  formatRunDuration,
  formatRunDateTime,
  formatRunTokens,
  runUsageView,
  runListUsage,
  DEFAULT_RUN_USAGE_LABELS,
  runPeriodSince,
  resolveRunHistoryLabels,
  RUN_PERIODS,
  DEFAULT_RUN_PERIOD,
  DEFAULT_RUN_PERIOD_EMPTY_LABELS,
  RUN_EXECUTOR_FILTERS,
  DEFAULT_RUN_PERIOD_LABELS,
  DEFAULT_RUN_EXECUTOR_FILTER_LABELS,
  DEFAULT_RUN_FILTER_NAMES,
  DEFAULT_RUN_HISTORY_LABELS,
  type RunPeriod,
  type RunExecutorFilter,
  type RunStatusFilter,
  type RunHistoryFilter,
  type RunHistoryItem,
  type RunModelUsageRecord,
  type RunUsageLabels,
  type RunUsageMeasurement,
  type RunUsageView,
  type RunHistoryLabels,
  type RunHistoryLabelsInput,
  type RunHistoryTableProps,
  type RunHistoryToolbarProps,
  type RunHistoryPeriodEmptyStateProps,
} from "./components/data/run-history";
export {
  RunSummary,
  RunDetailSection,
  RunProcessDetails,
  RunUsedModels,
  RunTimeline,
  RunTimelineSection,
  RunJobProgress,
  RunDetailLayout,
  RunDetailSkeleton,
  RunFactList,
  runTimelineBars,
  runTimelineHasChildren,
  runTimelineCurrentStepIndex,
  RUN_TIMELINE_MAX_VISIBLE_DEPTH,
  resolveRunDetailLabels,
  formatRunCancellation,
  DEFAULT_RUN_DETAIL_LABELS,
  DEFAULT_RUN_CANCELLATION_LABELS,
  type RunCancellation,
  type RunCancellationLabels,
  type RunCancellationVia,
  type RunDetailLabels,
  type RunDetailLabelsInput,
  type RunFact,
  type RunSummaryProps,
  type RunDetailSectionProps,
  type RunProcessDetailsProps,
  type RunProcessMetadataProps,
  type RunTimelineStep,
  type RunTimelineStepStatus,
  type RunTimelineBar,
  type RunTimelineProps,
  type RunTimelineSectionProps,
  type RunJobProgressProps,
  type RunDetailLayoutProps,
} from "./components/data/run-detail";
export {
  DEFAULT_OPEN_CONVERSATION_LABELS,
  OpenConversationAction,
  type OpenConversationActionLabels,
  type OpenConversationActionProps,
} from "./components/data/open-conversation-action";
export {
  OpenRunInputAction,
  type OpenRunInputActionLabels,
  type OpenRunInputActionProps,
} from "./components/data/open-run-input-action";
export {
  RunStepDetailsView,
  hasRunStepDetails,
  sameRunStepDetails,
  runStepDetailsJson,
  RUN_STEP_DETAILS_CODE_MAX_CHARS,
  DEFAULT_RUN_STEP_DETAILS_LABELS,
  type RunStepDetails,
  type RunStepDetailField,
  type RunStepDetailCode,
  type RunStepDetailsLabels,
  type RunStepDetailsViewProps,
} from "./components/data/run-step-details";
export {
  RunEventLog,
  DEFAULT_RUN_EVENT_LOG_LABELS,
  formatRunEventTime,
  runEventCategories,
  type RunEventLogItem,
  type RunEventLogLabels,
  type RunEventLogProps,
} from "./components/data/run-event-log";
export {
  ListCount,
  listCountText,
  DEFAULT_LIST_COUNT_LABELS,
  type ListCountProps,
  type ListCountLabels,
} from "./components/data/list-count";
export { LoadMoreFooter, loadMoreErrorMessage, type LoadMoreFooterProps } from "./components/data/load-more-footer";
export {
  ListPicker,
  DEFAULT_LIST_PICKER_LABELS,
  type ListPickerProps,
  type ListPickerItem,
  type ListPickerGroup,
  type ListPickerLabels,
  type ListPickerSearch,
} from "./components/data/list-picker";
export {
  ColumnBrowser,
  type ColumnBrowserColumn,
  type ColumnBrowserItem,
  type ColumnBrowserProps,
} from "./components/data/column-browser";

// --- app shell / layout ---
export { AppShell } from "./components/app-shell/AppShell";
export {
  useSidebarCollapsed,
  DEFAULT_NAV_DRAWER_LABELS,
  NAV_DRAWER_QUERY,
  type NavDrawerLabels,
} from "./components/app-shell/nav-drawer";
export {
  Sidebar,
  SidebarAccountFooter,
  type SidebarAccountLink,
  type SidebarFooterAction,
  type SidebarProps,
} from "./components/app-shell/Sidebar";
export { PageHeader, type PageHeaderAction, type PageHeaderBack } from "./components/app-shell/PageHeader";
export { PageBody, Section } from "./components/app-shell/PageBody";
export {
  FixedSplitPane,
  DEFAULT_FIXED_SPLIT_PANE_LABELS,
  type FixedSplitPaneProps,
  type FixedSplitPaneLabels,
} from "./components/app-shell/fixed-split-pane";
export * from "./lib/fixed-split-pane";
export {
  ListDetailLayout,
  ListDetailPager,
  adjacentListItems,
  useListDetailSplit,
  DEFAULT_LIST_DETAIL_LAYOUT_LABELS,
  DEFAULT_LIST_DETAIL_PAGER_LABELS,
  LIST_DETAIL_ITEM_SELECTOR,
  type ListDetailLayoutProps,
  type ListDetailLayoutLabels,
  type ListDetailPagerProps,
  type ListDetailPagerLabels,
} from "./components/app-shell/list-detail-layout";
export * from "./lib/list-detail";
export {
  useUrlSelection,
  type UrlSelection,
  type UrlSelectionSetSearchParams,
  type UseUrlSelectionOptions,
} from "./lib/url-selection";
export * from "./lib/measured-size";
export {
  Breadcrumbs,
  type BreadcrumbItem,
} from "./components/app-shell/Breadcrumbs";

// --- navigation types ---
export type {
  NavItem,
  NavSection,
  NavLinkComponent,
  SidebarLabels,
} from "./navigation/types";

// --- stores ---
export {
  createUiStore,
  type UiState,
  type CreateUiStoreOptions,
  type ThemePreference,
} from "./store/ui-store";
export { initTheme, applyTheme, resolveDark, type ThemeStore } from "./theme";
export {
  useToastStore,
  toast,
  type ToastItem,
  type ToastOptions,
  type ToastAction,
} from "./store/toast-store";

export { diagnosticTimestamp, logBrowserDiagnostic, type BrowserDiagnostic } from "./lib/diagnostic-log";
export { loadStylesAfterFirstRender, type DeferredStylesOptions } from "./lib/deferred-styles";

// 原文のプレビュー（ページの画像のビューア・bbox の強調。RAG と Agent の根拠のパネル・RAG の文書の詳細。#2389）
export * from "./lib/bbox";
export * from "./lib/preview-viewer";
export * from "./lib/text-quote";
export {
  DEFAULT_DOCUMENT_PREVIEW_LABELS,
  documentPreviewLabels,
  type DocumentPreviewLabels,
} from "./components/document-preview/labels";
export {
  PreviewViewer,
  bboxOverlayStyle,
  type PreviewSizing,
  type PreviewViewerPage,
} from "./components/document-preview/preview-viewer";
export {
  DocumentPreview,
  type DocumentPreviewFocus,
  type DocumentPreviewKind,
  type DocumentPreviewPagesState,
  type DocumentPreviewProps,
} from "./components/document-preview/document-preview";

export { TraceDetails } from "./components/data/trace-details";
export { traceSpanDisplayName, traceSpanFields, traceTimeline } from "./lib/trace-tree";

export { TRACE_DETAILS_JA, createTraceDetailsLabels, type TraceDetailsLabels, type TraceSpanNames, type TraceSpanStatus, type TraceStageName } from "./lib/trace-labels";
export { TraceDetailsLabelsProvider, useTraceDetailsLabels } from "./components/data/trace-labels-provider";
export {
  TRACE_SNAPSHOT_INTERVAL_MS,
  TRACE_SNAPSHOT_SETTLE_ATTEMPTS,
  TRACE_SNAPSHOT_SETTLE_DELAY_MS,
  TRACE_SNAPSHOT_TIMEOUT_MS,
  useTraceProgressSnapshot,
  type TraceProgressSnapshot,
  type TraceProgressSnapshotOptions,
} from "./components/feedback/trace-progress-snapshot";

export { ViewportScrollRegion } from "./components/data/viewport-scroll-region";

export { TraceExplorer, DEFAULT_TRACE_EXPLORER_LABELS, type TraceExplorerLabels } from "./components/data/trace-explorer";
