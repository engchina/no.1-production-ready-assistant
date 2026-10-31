import { Info, MessageCircleQuestion, Send, SkipForward, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode, type Ref } from "react";

import { cn } from "../../lib/utils";
import { ProcessingIndicator } from "../feedback/processing-state";
import { Button } from "../ui/button";
import type { ControlSize } from "../ui/control-size";
import { TextareaField } from "../ui/textarea-field";
import {
  DEFAULT_CLARIFICATION_LABELS,
  type ClarificationAnswerKind,
  type ClarificationLabels,
} from "./clarification-labels";
import { clarificationRecordAnswerText } from "./clarification-record";

/** 確認事項の選択肢。 */
export interface ClarificationOption {
  id: string;
  /** 翻訳済みの短い名前（1 行で読める長さ）。 */
  label: string;
  /** 翻訳済みの説明（任意）。 */
  description?: ReactNode;
  /** 製品固有の中身（NL2SQL の物理名・根拠、RAG の章節など）。説明の下に出す。 */
  detail?: ReactNode;
  disabled?: boolean;
}

/** 1 件の確認事項。 */
export interface ClarificationItem {
  id: string;
  /** 確認事項の文（例:「どの期間の売上を見ますか？」）。 */
  prompt: string;
  /** 確認する理由（例:「「売上」が受注と出荷のどちらの日付か、質問から決められません。」）。 */
  reason?: ReactNode;
  /** `single`（既定）はラジオ、`multiple` はチェックボックス。 */
  selection?: "single" | "multiple";
  /** 選択肢（1 回 8 個までを目安にする）。0 件なら自由入力だけ。 */
  options: ClarificationOption[];
  /** 確認事項の文の横に置く短い補足（NL2SQL の SQL の部分の `StatusBadge` など）。操作できる要素は置かない。 */
  meta?: ReactNode;
  /**
   * 答えなくても確定できる確認事項（NL2SQL の確認のカードの「対象の表の選び直し」「その他（自由入力）」の直しなど、
   * 今の内容のままでよいもの）。押せない理由の数に入れない。
   */
  optional?: boolean;
}

/** 1 件の確認事項の入力中の答え。 */
export interface ClarificationDraftEntry {
  optionIds: string[];
  otherText: string;
}

/** 入力中の答え（確認事項の ID ごと）。 */
export type ClarificationDraft = Record<string, ClarificationDraftEntry>;

/** 確定した答え（`otherText` は前後の空白を除く）。 */
export interface ClarificationAnswer {
  itemId: string;
  optionIds: string[];
  otherText: string;
}

/**
 * 処理中の操作。`confirm` はフォームを答えた内容の記録と経過の行 1 つに置き換え、`skip` は押したボタンだけを
 * `loading` にしてほかを押せなくする。
 */
export type ClarificationBusyAction = "confirm" | "skip";

/**
 * 入力の取りやめ（NL2SQL の「直すのをやめる」など、開いた入力を閉じて前の確認事項に戻る操作）。質問も確認も
 * 取り消さない。確認を終える操作は「確認せずに〈回答〉」（`onSkip`）だけで、「確認をやめる」には使わない。
 */
export interface ClarificationDismissAction {
  label: string;
  onClick: () => void;
}

export interface ClarificationCardProps {
  /** 確認事項（ふつうは 1 件。1 件ずつ聞くときは `progress` で位置を出す）。 */
  items: ClarificationItem[];
  /** 確定（「この内容で〈回答〉」）。すべての確認事項に答えがあるときだけ押せる。 */
  onConfirm: (answers: ClarificationAnswer[]) => void;
  /**
   * スキップ（「確認せずに〈回答〉」。前提を示して回答する）。確認を終える唯一の操作で、質問は取り消さない
   * （質問を取り消すのは入力欄の「停止」だけ）。渡さなければボタンを出さない。
   */
  onSkip?: () => void;
  /**
   * 確定の後の経過の行に出す「確認せずに〈回答〉」（待たずに進める）。送った答えの処理を取りやめられる製品
   * （NL2SQL の AI の再検査）だけが渡す。
   */
  onSkipWhileConfirming?: () => void;
  /** 入力の取りやめ（操作の行の右端の ghost）。質問・確認は取り消さない。 */
  dismissAction?: ClarificationDismissAction;
  /** 1 件ずつ聞くときの位置（「確認事項 1 / 3」）。 */
  progress?: { current: number; total: number };
  /** 確認の往復（「確認 1 回目（上限 2 回）」）。上限の回では最後の確認であることを添える。 */
  round?: { current: number; max: number };
  /**
   * 処理中の操作。`confirm` の間はフォーム（選択肢・自由入力・操作の行・押せない理由・製品の中身）を出さず、
   * 答えた内容の記録と経過の行 1 つ（スピナー・経過時間・遅いときの案内）に置き換える。失敗したら `null` に
   * 戻すとフォームと入力が戻り、`error` を操作の行の直上に出す。
   */
  busyAction?: ClarificationBusyAction | null;
  /** カード全体を操作できない（古い会話の確認など）。 */
  disabled?: boolean;
  /** 受控の答え。渡すときは `onValueChange` も渡す（作業状態に残すとき）。 */
  value?: ClarificationDraft;
  /** 非受控の初期の答え。 */
  defaultValue?: ClarificationDraft;
  onValueChange?: (value: ClarificationDraft) => void;
  /**
   * 確定を押せない理由を製品が決めるとき（NL2SQL の未送信の直しなど）。文があれば確定を押せず、文を確定の
   * ボタンの説明（aria-describedby）にする。`null` で部品の既定の判定（未回答の確認事項）に戻す。
   */
  blockedReason?: string | null;
  /** 確認事項の下・操作の行の上に置く製品の中身（NL2SQL の要件の一覧など）。 */
  children?: ReactNode;
  /** 操作の失敗（`ApiErrorBanner` など）。操作の行の直上に出す。 */
  error?: ReactNode;
  /**
   * 確定の右に置く製品の操作（NL2SQL の「直しを伝える」など。`Button` を同じ `size` で渡す）。多用しない
   * （操作の行は 確定 → 製品の操作 → スキップ、反対の端に入力の取りやめ）。
   */
  actions?: ReactNode;
  labels?: Partial<ClarificationLabels>;
  /** 操作部品の高さ（既定 md。チャットの回答の中は md、画面のパネルは lg）。 */
  size?: ControlSize;
  /** 題の見出しの段（既定 3。回答の枠の中）。 */
  headingLevel?: 2 | 3 | 4;
  /** 出たときに最初の確認事項の文へフォーカスを移す（確認事項が変わったときは常に移す）。 */
  autoFocus?: boolean;
  /** `card`（既定）は枠線つきの面、`plain` は枠なし（回答の枠の中に重ねるとき）。 */
  variant?: "card" | "plain";
  /** id の接頭辞（同じ画面に 2 つ置くとき）。 */
  idPrefix?: string;
  className?: string;
  testId?: string;
}

const EMPTY_ENTRY: ClarificationDraftEntry = { optionIds: [], otherText: "" };

function entryOf(draft: ClarificationDraft, itemId: string): ClarificationDraftEntry {
  return draft[itemId] ?? EMPTY_ENTRY;
}

/** 確認事項に答えがあるか（選択肢を選んだか、空白でない自由入力）。 */
export function isClarificationEntryAnswered(entry: ClarificationDraftEntry | undefined): boolean {
  return Boolean(entry && (entry.optionIds.length > 0 || entry.otherText.trim()));
}

/** 入力中の答えを確定した答えにする（確認事項の順。自由入力の前後の空白を除く）。 */
export function clarificationAnswers(items: ClarificationItem[], draft: ClarificationDraft): ClarificationAnswer[] {
  return items.map((item) => {
    const entry = entryOf(draft, item.id);
    return { itemId: item.id, optionIds: entry.optionIds, otherText: entry.otherText.trim() };
  });
}

/**
 * 回答前の確認のカード（#1839。3 製品共通）。曖昧な質問に答える前に、確認事項・確認する理由・選択肢（ラジオ /
 * チェックボックス）と、常に「その他（自由入力）」を出し、「この内容で〈回答〉」「確認せずに〈回答〉」を並べる。
 * 確認を終えるのは「確認せずに〈回答〉」だけで、カードに「確認をやめる」は置かない（質問を取り消すのは入力欄の
 * 「停止」だけ。#2311）。文言は `clarificationLabels({ stage, action })` で製品の名詞だけを変える。
 *
 * - 1 つだけ選ぶ確認事項では、選択肢と自由入力はどちらか一方（選ぶと自由入力を空に、書くと選択を外す）。
 *   複数選ぶ確認事項では併せて答えられる。
 * - 確定を押せない間は、理由を操作の行の下に出し、確定のボタンの aria-describedby で結ぶ（#1772）。
 * - 確定を押したら、フォームを答えた内容の記録と経過の行 1 つに置き換える（1 つの処理にスピナー 1 つ。
 *   messaging.md §3.7）。スキップの処理中は押したボタンだけを `loading` にし、ほかを押せなくする（buttons.md §8）。
 * - 確認事項の文は題の 1 段下の見出し。確認事項が変わったら答えを空にし、最初の確認事項の文へフォーカスを移す
 *   （読み上げで新しい確認事項をそのまま聞ける）。
 * - 業務の語彙は知らない。製品固有の中身は選択肢の `detail`・確認事項の `meta`・`children` で渡す。
 */
export function ClarificationCard({
  items,
  onConfirm,
  onSkip,
  onSkipWhileConfirming,
  dismissAction,
  progress,
  round,
  busyAction = null,
  disabled = false,
  value,
  defaultValue,
  onValueChange,
  blockedReason = null,
  children,
  error,
  actions,
  labels: labelOverrides,
  size = "md",
  headingLevel = 3,
  autoFocus = false,
  variant = "card",
  idPrefix,
  className,
  testId = "clarification-card",
}: ClarificationCardProps) {
  const labels = { ...DEFAULT_CLARIFICATION_LABELS, ...labelOverrides };
  const reactId = useId();
  const baseId = idPrefix ?? `clarification-${reactId.replace(/:/g, "")}`;
  const titleId = `${baseId}-title`;
  const blockedId = `${baseId}-blocked`;
  const skipHintId = `${baseId}-skip-hint`;
  const Heading = `h${headingLevel}` as "h2" | "h3" | "h4";

  // 確認事項が変わったレンダーで、非受控の答えを空にする（effect で setState しない）。
  const itemsKey = items.map((item) => item.id).join("\u0000");
  const stepKey = `${itemsKey}\u0001${progress?.current ?? ""}`;
  const [internal, setInternal] = useState<{ key: string; draft: ClarificationDraft }>(() => ({
    key: itemsKey,
    draft: defaultValue ?? {},
  }));
  let internalDraft = internal.draft;
  if (internal.key !== itemsKey) {
    internalDraft = {};
    setInternal({ key: itemsKey, draft: internalDraft });
  }
  const draft = value ?? internalDraft;

  const update = (itemId: string, entry: ClarificationDraftEntry) => {
    const next = { ...draft, [itemId]: entry };
    if (value === undefined) setInternal({ key: itemsKey, draft: next });
    onValueChange?.(next);
  };

  const promptRef = useRef<HTMLHeadingElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const focusedKeyRef = useRef<string | null>(autoFocus ? null : stepKey);
  const confirming = busyAction === "confirm";
  const wasConfirmingRef = useRef(false);
  useEffect(() => {
    const wasConfirming = wasConfirmingRef.current;
    wasConfirmingRef.current = confirming;
    // 確定でボタンが消えるので、フォーカスを題へ移す（経過は経過の行の status が読み上げる）。
    if (confirming) {
      if (!wasConfirming) titleRef.current?.focus({ preventScroll: true });
      return;
    }
    // 確認事項が変わった・確定に失敗してフォームが戻ったら、最初の確認事項の文へ移す。
    if (focusedKeyRef.current === stepKey && !wasConfirming) return;
    focusedKeyRef.current = stepKey;
    promptRef.current?.focus({ preventScroll: true });
  }, [stepKey, confirming]);

  const busy = busyAction !== null;
  const inputsDisabled = disabled || busy;
  const unanswered = items.filter((item) => !item.optional && !isClarificationEntryAnswered(draft[item.id]));
  const remaining = unanswered.length;
  // 押せない理由は、答えていない確認事項の答え方（選択肢の有無）で言い方を変える（#3366）。
  const freeTextCount = unanswered.filter((item) => item.options.length === 0).length;
  const answerKind: ClarificationAnswerKind =
    freeTextCount === 0 ? "choice" : freeTextCount === remaining ? "freeText" : "mixed";
  const blocked =
    blockedReason ??
    (remaining === 0
      ? ""
      : items.length > 1
        ? labels.confirmBlockedMultiple(remaining, answerKind)
        : answerKind === "freeText"
          ? labels.confirmBlockedFreeText
          : labels.confirmBlocked);
  const ready = !blocked;
  const lastRound = round ? round.current >= round.max : false;

  const meta: string[] = [];
  if (progress) meta.push(labels.step(progress.current, progress.total));
  else if (items.length > 1) meta.push(labels.itemCount(items.length));
  if (round) meta.push(labels.round(round.current, round.max));

  return (
    <section
      aria-labelledby={titleId}
      data-testid={testId}
      data-ready={ready ? "true" : "false"}
      data-busy={busyAction ?? undefined}
      className={cn(
        "grid min-w-0 gap-4",
        variant === "card" && "rounded-md border border-border bg-surface p-4",
        className
      )}
    >
      <header className="grid min-w-0 gap-1">
        {meta.length > 0 ? (
          <p className="text-xs font-medium text-fg-muted" data-testid={`${testId}-meta`}>
            {meta.join("・")}
          </p>
        ) : null}
        <Heading
          ref={titleRef}
          id={titleId}
          tabIndex={-1}
          className="pr-clarification-heading flex items-center gap-2 text-sm font-semibold leading-6 text-fg"
        >
          <MessageCircleQuestion size={16} aria-hidden className="shrink-0 text-accent-fg" />
          {labels.title}
        </Heading>
        {lastRound && !confirming ? (
          <p className="flex items-start gap-1.5 text-xs leading-5 text-fg-muted" data-testid={`${testId}-last-round`}>
            <span className="pr-icon-line">
              <Info size={12} aria-hidden />
            </span>
            {labels.lastRound}
          </p>
        ) : null}
      </header>

      {confirming ? (
        <ClarificationConfirming
          items={items}
          draft={draft}
          labels={labels}
          operationKey={`${baseId}-confirm-${stepKey}`}
          onSkip={onSkipWhileConfirming}
          testId={testId}
        />
      ) : (
        <>
        {items.map((item, index) => (
          <ClarificationItemFields
            key={item.id}
            item={item}
            entry={entryOf(draft, item.id)}
            baseId={`${baseId}-item-${index}`}
            promptLevel={Math.min(headingLevel + 1, 6) as 3 | 4 | 5}
            promptRef={index === 0 ? promptRef : undefined}
            labels={labels}
            disabled={inputsDisabled}
            separated={index > 0}
            testId={testId}
            onChange={(entry) => update(item.id, entry)}
          />
        ))}

        {children}
        {error}

        <div className="grid min-w-0 gap-2">
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
            <Button
              type="button"
              variant="primary"
              size={size}
              icon={Send}
              className="w-full sm:w-auto"
              disabled={busy || disabled || !ready}
              aria-describedby={blocked && !busy ? blockedId : undefined}
              onClick={() => onConfirm(clarificationAnswers(items, draft))}
              data-testid={`${testId}-confirm`}
            >
              {labels.confirm}
            </Button>
            {actions}
            {onSkip ? (
              <Button
                type="button"
                variant="secondary"
                size={size}
                icon={SkipForward}
                className="w-full sm:w-auto"
                loading={busyAction === "skip"}
                disabled={busy ? busyAction !== "skip" : disabled}
                aria-describedby={labels.skipHint ? skipHintId : undefined}
                onClick={onSkip}
                data-testid={`${testId}-skip`}
              >
                {labels.skip}
              </Button>
            ) : null}
            {dismissAction ? (
              // 入力の取りやめは確定と反対の端に置く（README §4「カード内の操作行」）。質問は消えないので danger にしない。
              <Button
                type="button"
                variant="ghost"
                size={size}
                icon={X}
                className="w-full sm:ml-auto sm:w-auto"
                disabled={busy || disabled}
                onClick={dismissAction.onClick}
                data-testid={`${testId}-dismiss`}
              >
                {dismissAction.label}
              </Button>
            ) : null}
          </div>
          <div className="grid gap-0.5 text-xs leading-5 text-fg-muted">
            {blocked && !busy && !disabled ? (
              <p id={blockedId} data-testid={`${testId}-blocked`}>
                {blocked}
              </p>
            ) : null}
            {onSkip && labels.skipHint ? (
              <p id={skipHintId}>
                <span className="font-medium">{labels.skip}</span>: {labels.skipHint}
              </p>
            ) : null}
          </div>
        </div>
        </>
      )}
    </section>
  );
}

/**
 * 確定の後の表示: 答えた内容の短い記録（確認事項の文と答え）と、経過の行 1 つ（スピナー・その確定から数える
 * 経過時間・遅いときの案内。製品が渡せば「確認せずに〈回答〉」）。答えの無い任意の確認事項は記録に出さない。
 */
function ClarificationConfirming({
  items,
  draft,
  labels,
  operationKey,
  onSkip,
  testId,
}: {
  items: ClarificationItem[];
  draft: ClarificationDraft;
  labels: ClarificationLabels;
  operationKey: string;
  onSkip?: () => void;
  testId: string;
}) {
  const entries = items
    .filter((item) => !item.optional || isClarificationEntryAnswered(draft[item.id]))
    .map((item) => {
      const entry = entryOf(draft, item.id);
      return {
        id: item.id,
        prompt: item.prompt,
        selected: entry.optionIds
          .map((optionId) => item.options.find((option) => option.id === optionId)?.label)
          .filter((label): label is string => Boolean(label)),
        otherText: entry.otherText,
        freeTextOnly: item.options.length === 0,
      };
    });
  return (
    <div className="grid min-w-0 gap-3" data-testid={`${testId}-confirming`}>
      {entries.length > 0 ? (
        <dl className="grid min-w-0 gap-1.5" data-testid={`${testId}-answered`}>
          {entries.map((entry) => (
            <div key={entry.id} className="min-w-0" data-testid={`${testId}-answered-entry`} data-entry-id={entry.id}>
              <dt className="break-words text-xs leading-5 text-fg-muted">{entry.prompt}</dt>
              <dd className="whitespace-pre-wrap break-words text-sm leading-6 text-fg">
                {clarificationRecordAnswerText(entry, labels)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
      <div className="rounded-md border border-border bg-surface-sunken p-4">
        <ProcessingIndicator
          active
          operationKey={operationKey}
          label={labels.confirming}
          placement="panel"
          testId={`${testId}-progress`}
          onCancel={onSkip}
          labels={{ cancel: labels.skip }}
        />
      </div>
    </div>
  );
}

function ClarificationItemFields({
  item,
  entry,
  baseId,
  promptLevel,
  promptRef,
  labels,
  disabled,
  separated,
  testId,
  onChange,
}: {
  item: ClarificationItem;
  entry: ClarificationDraftEntry;
  baseId: string;
  promptLevel: 3 | 4 | 5;
  promptRef?: Ref<HTMLHeadingElement>;
  labels: ClarificationLabels;
  disabled: boolean;
  separated: boolean;
  testId: string;
  onChange: (entry: ClarificationDraftEntry) => void;
}) {
  const multiple = item.selection === "multiple";
  const Prompt = `h${promptLevel}` as "h3" | "h4" | "h5";
  const legendId = `${baseId}-prompt`;
  const reasonId = `${baseId}-reason`;
  const hintId = `${baseId}-hint`;
  const describedBy = [item.reason ? reasonId : "", multiple ? hintId : ""].filter(Boolean).join(" ") || undefined;

  const toggle = (optionId: string) => {
    if (multiple) {
      const optionIds = entry.optionIds.includes(optionId)
        ? entry.optionIds.filter((id) => id !== optionId)
        : [...entry.optionIds, optionId];
      onChange({ ...entry, optionIds });
      return;
    }
    // 1 つだけ選ぶ確認事項では、選択肢と自由入力はどちらか一方。
    onChange({ optionIds: [optionId], otherText: "" });
  };
  const changeOther = (otherText: string) => {
    onChange({ optionIds: multiple || !otherText ? entry.optionIds : [], otherText });
  };

  return (
    <fieldset
      aria-labelledby={legendId}
      aria-describedby={describedBy}
      className={cn("grid min-w-0 gap-3", separated && "border-t border-border pt-4")}
      data-testid={`${testId}-item`}
      data-item-id={item.id}
      data-answered={isClarificationEntryAnswered(entry) ? "true" : "false"}
    >
      <div className="grid min-w-0 gap-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {/* legend は flex の子にできないため、名前は aria-labelledby で付ける。 */}
          <Prompt
            ref={promptRef}
            id={legendId}
            tabIndex={-1}
            className="pr-clarification-heading min-w-0 break-words text-sm font-medium leading-6 text-fg"
          >
            {item.prompt}
          </Prompt>
          {item.meta}
        </div>
        {item.reason ? (
          <p id={reasonId} className="break-words text-xs leading-5 text-fg-muted">
            <span className="font-medium">{labels.reason}</span>: {item.reason}
          </p>
        ) : null}
        {multiple ? (
          <p id={hintId} className="text-xs leading-5 text-fg-muted">
            {labels.multipleHint}
          </p>
        ) : null}
      </div>

      {item.options.length > 0 ? (
        // 列の数は置き場所の幅で決める（回答の枠の中と画面のパネルで同じ部品）。
        <div className="grid min-w-0 grid-cols-[repeat(auto-fill,minmax(min(100%,16rem),1fr))] gap-2">
          {item.options.map((option) => {
            const checked = entry.optionIds.includes(option.id);
            return (
              <label
                key={option.id}
                data-surface-tint="accent"
                data-testid={`${testId}-option`}
                data-option-id={option.id}
                className={cn(
                  "flex min-h-[var(--control-height-touch)] min-w-0 cursor-pointer items-start gap-3 rounded-md border border-border-control bg-surface px-3 py-2 text-sm text-fg transition-colors",
                  "hover:not-has-[:checked]:not-has-[:disabled]:bg-surface-hover has-[:checked]:border-accent-emphasis has-[:checked]:bg-accent-subtle",
                  // 処理中も選んだ内容は読めるよう、色は変えずに押せないことだけを示す。
                  "has-[:disabled]:cursor-not-allowed",
                  "forced-colors:has-[:checked]:border-[Highlight]"
                )}
              >
                {/* 選択肢はネイティブのラジオ・チェックボックス（矢印キーの移動・読み上げをブラウザに任せる）。 */}
                <input
                  type={multiple ? "checkbox" : "radio"}
                  name={`${baseId}-option`}
                  value={option.id}
                  checked={checked}
                  disabled={disabled || option.disabled}
                  onChange={() => toggle(option.id)}
                  className="mt-1 h-4 w-4 shrink-0 accent-accent-emphasis"
                />
                <span className="grid min-w-0 gap-0.5">
                  <span className="break-words font-medium">{option.label}</span>
                  {option.description ? (
                    <span className="break-words text-xs leading-5 text-fg-muted">{option.description}</span>
                  ) : null}
                  {option.detail ? <span className="min-w-0 text-xs leading-5 text-fg-muted">{option.detail}</span> : null}
                </span>
              </label>
            );
          })}
        </div>
      ) : null}

      <TextareaField
        id={`${baseId}-other`}
        // 選択肢の無い確認事項は自由入力だけで答えるので、「その他」「選択肢に無いとき」と言わない（#3366）。
        label={item.options.length > 0 ? labels.other : labels.freeText}
        value={entry.otherText}
        rows={2}
        placeholder={item.options.length > 0 ? labels.otherPlaceholder : labels.freeTextPlaceholder}
        disabled={disabled}
        onValueChange={changeOther}
        data-testid={`${testId}-other`}
      />
    </fieldset>
  );
}
