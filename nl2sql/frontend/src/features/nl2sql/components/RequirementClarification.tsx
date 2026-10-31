// 回答前の確認の共通の部品（#1439 / #1442）。SQL 生成の画面のパネル（GuidedClarificationPanel）と、チャットの
// 会話の中の確認（ChatRequirementTurn）が同じ部品・同じ API で、1 問ずつの質問・回答・確認済みの一覧からの直し・
// 要件のまとめと SQL の骨組みを出す。session の作り方（業務プロファイルの推薦・会話の履歴）と、反映の後の操作
// （質問の欄に入れる・チャットで SQL を生成する）だけを画面ごとに持つ。
// 「SQL の要件（AI の理解）」の一覧（#1718）も両方の画面で同じ部品・同じ振る舞いにする（違いは置き場所と幅だけ）。
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Check, MessageSquareText, Pencil, Plus, RotateCcw } from "lucide-react";

import {
  ApiErrorBanner,
  Banner,
  Button,
  ClarificationCard,
  clarificationLabels,
  Disclosure,
  DisclosureChevron,
  ProcessingIndicator,
  StatusBadge,
  type ClarificationBusyAction,
  type ClarificationItem,
  type ClarificationLabels,
  type ClarificationOption,
} from "@production-ready/ui";

import { isAbortError } from "@/lib/api";
import { useValuesChanged } from "@/lib/render-sync";
import { t } from "@/lib/i18n";

import {
  answerQuerySessionClarification,
  applyQuerySessionRequirement,
  cancelQuerySession,
  getQuerySession,
  QuerySessionVersionConflictError,
} from "../ontology/api";
import type {
  QuerySession,
  RequirementCandidate,
  RequirementItem,
  RequirementSection,
  RequirementState,
  SqlRequirementDraft,
} from "../ontology/types";
import {
  ADD_ITEM_PREFIX,
  KIND_BADGE_VARIANT,
  addConditionItem,
  canAnswerWithCandidate,
  confirmCorrection,
  confirmCorrectionKind,
  confirmTargetItem,
  currentCandidateId,
  requirementKindLabel,
  requirementOverview,
  sectionPartLabel,
} from "../requirementView";
import { isRequirementCheckUnavailable, requirementNextStep } from "../requirementCheck";

export type RequirementBusyAction = "start" | "answer" | "apply" | "cancel" | "";
type ButtonSize = "sm" | "md" | "lg";

export function isSessionOpen(session: QuerySession | null): session is QuerySession {
  return Boolean(session && session.status !== "done" && session.status !== "cancelled");
}

export function requirementPhaseBadge(state: RequirementState): {
  variant: "success" | "warning" | "danger";
  label: string;
} {
  if (state.status === "unanswerable") {
    return { variant: "danger", label: t("nl2sql.clarification.status.unanswerable") };
  }
  if (state.status === "ready") {
    return { variant: "success", label: t("nl2sql.clarification.status.ready") };
  }
  return {
    variant: "warning",
    label: t("nl2sql.clarification.status.needsAnswer", { count: state.unresolved_items.length }),
  };
}

/** 回答前の確認の session の状態と、回答・直し・反映・中止の操作（SQL 生成の画面とチャットで共通）。 */
export interface RequirementSessionController {
  session: QuerySession | null;
  setSession: (session: QuerySession | null) => void;
  busyAction: RequirementBusyAction;
  setBusyAction: (action: RequirementBusyAction) => void;
  /**
   * 回答・反映・中止の失敗（画面の文、または API の失敗。API の失敗は「詳細」付きで出す。#1715）。
   * 出すのは `RequirementErrorBanner`。
   */
  error: unknown;
  setError: (error: unknown) => void;
  /** 中止・期限切れなどで終わった（質問と回答の欄を出さない）。 */
  sessionEnded: boolean;
  state: RequirementState | null;
  draft: SqlRequirementDraft | null;
  items: RequirementItem[];
  /** 今の質問（確認済みの一覧から直している項目があればそれ）。 */
  activeItem: RequirementItem | null;
  correctingItem: RequirementItem | null;
  /** 確認済みの一覧から直す項目を選ぶ（null で直すのをやめる。条件の追加もやめる）。 */
  setCorrectingItemId: (id: string | null) => void;
  /** 「条件を追加」で、値の無い部分に条件を足す入力を出す（#1718）。 */
  startAdding: (section: RequirementSection) => void;
  /** 一覧の要確認の行で、候補を押して答える（#1718）。 */
  answerWithCandidate: (item: RequirementItem, candidateId: string) => Promise<void>;
  /** 候補を押して送っている候補の ID（その候補のボタンだけを loading にする）。 */
  answeringCandidateId: string;
  selectedCandidateIds: string[];
  freeText: string;
  answerReady: boolean;
  selectCandidate: (candidateId: string) => void;
  changeFreeText: (value: string) => void;
  /**
   * 共通のカード（`ClarificationCard`）の答えを受け取る。選択肢と自由入力はどちらか一方にする（backend の契約。
   * 選び直したら自由入力を空に、書いたら選択を外す）。
   */
  setAnswerDraft: (optionIds: string[], otherText: string) => void;
  submitAnswer: () => Promise<void>;
  /**
   * 確認のカード（確認の質問が無い・全部に答えた後）で選び直せる対象の表（#1761）。AI が決めた表に、業務
   * プロファイルのほかの表が候補として添えてあるときだけ。
   */
  confirmTarget: RequirementItem | null;
  /** 確認のカードの直し（対象の表の選び直し・「その他（自由入力）」）が入力されている。 */
  correctionPending: boolean;
  /** 確認のカードの直しの種類（押せない理由の文を選ぶ。#1776）。直しが無ければ null。 */
  correctionKind: "free_text" | "table" | null;
  /** 確認のカードの直しを回答として送る（AI の再検査が要件を直す。#1761）。 */
  submitCorrection: () => Promise<void>;
  /** 反映して session を閉じる。反映した session（要件の本文を含む）を返す。失敗は null。 */
  applyRequirement: () => Promise<QuerySession | null>;
  /** 開いている session をサーバーで中止する。中止できた（または開いていない）ら true。 */
  cancelSession: () => Promise<boolean>;
  /**
   * 送っている回答（AI の再検査を待っている要求）を取りやめる。応答は待たずに捨て、失敗も出さない
   * （チャットの「確認せずに SQL を生成」で、再検査を待たずに SQL の生成へ進むとき）。
   */
  abortAnswer: () => void;
  /** 回答・反映・中止のどれかが要件確認を進められる状態か（SELECT / FROM が明確で、直している途中でない）。 */
  showApply: boolean;
  unresolvedCount: number;
  answeredCount: number;
  showFinalSummary: boolean;
}

export function useRequirementSession(
  initialBusy: RequirementBusyAction = ""
): RequirementSessionController {
  const [session, setSession] = useState<QuerySession | null>(null);
  const [selectedCandidateIds, setSelectedCandidateIds] = useState<string[]>([]);
  const [freeText, setFreeText] = useState("");
  // 確認済みの一覧から「直す」で選んだ項目（質問の欄の代わりに、この項目の入力を出す）。
  const [correctingItemId, setCorrectingItemIdState] = useState<string | null>(null);
  // 「条件を追加」で足す部分（質問の欄の代わりに、その部分の自由入力を出す。#1718）。
  const [addingSection, setAddingSection] = useState<RequirementSection | null>(null);
  const [answeringCandidateId, setAnsweringCandidateId] = useState("");
  const [error, setError] = useState<unknown>("");
  const [busyAction, setBusyAction] = useState<RequirementBusyAction>(initialBusy);
  const mountedRef = useRef(false);
  // 送っている回答の要求（取りやめるときに中断する）。
  const answerAbortRef = useRef<AbortController | null>(null);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 中止・期限切れなどで終わった確認は、質問と回答の欄を出さず、理由と「閉じる」だけを出す（#1274）。
  const sessionEnded = session?.status === "cancelled" || session?.status === "error";
  const state = sessionEnded ? null : session?.requirement_state ?? null;
  const draft = sessionEnded ? null : session?.requirement ?? null;
  const items = draft?.items ?? [];
  const correctingItem = addingSection
    ? addConditionItem(addingSection)
    : correctingItemId
      ? items.find((item) => item.id === correctingItemId) ?? null
      : null;
  const setCorrectingItemId = (id: string | null) => {
    setAddingSection(null);
    setCorrectingItemIdState(id);
  };
  const startAdding = (section: RequirementSection) => {
    setCorrectingItemIdState(null);
    setAddingSection(section);
  };
  const activeItem = correctingItem ?? state?.current_item ?? null;

  // 質問（または直す項目）が変わったレンダーで回答欄を空にする（effect で setState しない）。
  const activeChanged = useValuesChanged([activeItem?.id, session?.current_intent_version]);
  if (activeChanged) {
    setSelectedCandidateIds([]);
    setFreeText("");
  }

  const answerReady = selectedCandidateIds.length > 0 || Boolean(freeText.trim());

  const selectCandidate = (candidateId: string) => {
    setFreeText("");
    if (activeItem?.answer_kind === "multi_select") {
      setSelectedCandidateIds((current) =>
        current.includes(candidateId)
          ? current.filter((item) => item !== candidateId)
          : [...current, candidateId]
      );
      return;
    }
    setSelectedCandidateIds([candidateId]);
  };

  const changeFreeText = (value: string) => {
    setFreeText(value);
    if (value) setSelectedCandidateIds([]);
  };

  const setAnswerDraft = (optionIds: string[], otherText: string) => {
    const sameIds =
      optionIds.length === selectedCandidateIds.length &&
      optionIds.every((id, index) => id === selectedCandidateIds[index]);
    if (!sameIds && (optionIds.length > 0 || otherText === freeText)) {
      setSelectedCandidateIds(optionIds);
      setFreeText("");
      return;
    }
    changeFreeText(otherText);
  };

  const reloadAfterConflict = async (cause: unknown, current: QuerySession) => {
    if (!(cause instanceof QuerySessionVersionConflictError)) return;
    try {
      setSession(await getQuerySession(current.id));
    } catch {
      if (cause.session) setSession(cause.session);
    }
  };

  const sendAnswer = async (
    itemId: string,
    answer: { selected_candidate_ids: string[]; free_text: string }
  ) => {
    if (!session || busyAction) return;
    const abort = new AbortController();
    answerAbortRef.current = abort;
    setBusyAction("answer");
    setError("");
    try {
      const updated = await answerQuerySessionClarification(
        session.id,
        {
          base_version: session.current_intent_version ?? 1,
          item_id: itemId,
          ...answer,
        },
        { signal: abort.signal }
      );
      if (abort.signal.aborted) return;
      setSession(updated);
      setCorrectingItemId(null);
    } catch (cause) {
      // 取りやめた回答（確認せずに SQL の生成へ進んだ）の失敗は出さない。
      if (abort.signal.aborted) return;
      await reloadAfterConflict(cause, session);
      setError(cause instanceof Error ? cause : t("nl2sql.clarification.error.answer"));
    } finally {
      if (answerAbortRef.current === abort) answerAbortRef.current = null;
      if (mountedRef.current) {
        setBusyAction("");
        setAnsweringCandidateId("");
      }
    }
  };

  const abortAnswer = () => {
    answerAbortRef.current?.abort();
    answerAbortRef.current = null;
  };

  const submitAnswer = async () => {
    if (!activeItem || !answerReady) return;
    await sendAnswer(activeItem.id, {
      selected_candidate_ids: selectedCandidateIds,
      free_text: freeText.trim(),
    });
  };

  const confirmTarget = state && !state.current_item ? confirmTargetItem(items) : null;
  // 確認のカードの直しは、質問の欄（今の質問・直す項目）を出していないときだけ。
  const correction = activeItem ? null : confirmCorrection(confirmTarget, selectedCandidateIds, freeText);
  const submitCorrection = async () => {
    if (!correction) return;
    await sendAnswer(correction.item_id, {
      selected_candidate_ids: correction.selected_candidate_ids,
      free_text: correction.free_text,
    });
  };

  const answerWithCandidate = async (item: RequirementItem, candidateId: string) => {
    if (busyAction) return;
    setAnsweringCandidateId(candidateId);
    await sendAnswer(item.id, { selected_candidate_ids: [candidateId], free_text: "" });
  };

  const applyRequirement = async (): Promise<QuerySession | null> => {
    if (!session || busyAction) return null;
    // 反映済み（session は閉じた）で画面への反映だけが失敗したときは、API を呼び直さない。
    if (!session.requirement_applied_at && !state?.can_apply) return null;
    setBusyAction("apply");
    setError("");
    try {
      const applied = session.requirement_applied_at
        ? session
        : await applyQuerySessionRequirement(session.id, {
            base_version: session.current_intent_version ?? 1,
          });
      if (applied !== session) {
        // 状態の表示は反映前のまま保つ（閉じた session の state は使わない）。
        // 閉じた session は画面を離れても中止しない（status を反映後の値にする）。
        setSession({
          ...session,
          status: applied.status ?? "done",
          requirement_applied_at: applied.requirement_applied_at ?? "applied",
          requirement: applied.requirement ?? session.requirement,
        });
      }
      if (!applied.requirement?.clarified_question_ja?.trim()) {
        setError(t("nl2sql.clarification.error.apply"));
        return null;
      }
      return applied;
    } catch (cause) {
      await reloadAfterConflict(cause, session);
      setError(cause instanceof Error ? cause : t("nl2sql.clarification.error.apply"));
      return null;
    } finally {
      if (mountedRef.current) setBusyAction("");
    }
  };

  const cancelSession = async (): Promise<boolean> => {
    if (busyAction) return false;
    if (!isSessionOpen(session)) return true;
    setBusyAction("cancel");
    setError("");
    try {
      await cancelQuerySession(session.id);
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause : t("nl2sql.clarification.error.cancel"));
      return false;
    } finally {
      if (mountedRef.current) setBusyAction("");
    }
  };

  const unresolvedCount = state?.unresolved_items.length ?? 0;
  return {
    session,
    setSession,
    busyAction,
    setBusyAction,
    error,
    setError,
    sessionEnded,
    state,
    draft,
    items,
    activeItem,
    correctingItem,
    setCorrectingItemId,
    startAdding,
    answerWithCandidate,
    answeringCandidateId,
    selectedCandidateIds,
    freeText,
    answerReady,
    selectCandidate,
    changeFreeText,
    setAnswerDraft,
    submitAnswer,
    confirmTarget,
    correctionPending: Boolean(correction),
    correctionKind: confirmCorrectionKind(correction),
    submitCorrection,
    applyRequirement,
    cancelSession,
    abortAnswer,
    showApply: Boolean(state?.can_apply) && !correctingItem,
    unresolvedCount,
    answeredCount: state?.answered_count ?? 0,
    showFinalSummary: Boolean(state && (state.status === "ready" || state.can_apply)),
  };
}

/** 答えられない質問・最低の基準（SELECT / FROM）・再検査の上限の知らせ。 */
/**
 * 要件の検査（回答前の確認の session の作成・読み込み）の状態。SQL 生成の画面（ボタンで開く）とチャット（送信の
 * ときに自動）で同じ hook を使い、検査中・失敗・OCI Enterprise AI が無いときの扱いを 1 か所で決める（#1715）。
 */
export interface RequirementCheckRunner {
  /** 検査の最中（session の作成の応答を待っている）。 */
  checking: boolean;
  /** 検査の失敗（`RequirementCheckFailure` で、理由・「詳細」・もう一度確認・確認せずに生成・中止を出す）。 */
  checkError: unknown;
  /**
   * 検査を行う。成功したら結果を返し、失敗は `checkError` に入れて null を返す。`isCurrent` が false になった
   * （中止した・別の送信に移った）ときは状態を変えずに null を返す。OCI Enterprise AI が無い
   * （`REQUIREMENT_AI_NOT_CONFIGURED`）ときは `onUnavailable` を呼ぶ（確認せずに SQL を作る）。
   */
  run: <T>(
    task: () => Promise<T>,
    options?: { isCurrent?: () => boolean; onUnavailable?: () => void }
  ) => Promise<T | null>;
  /** 検査の表示を外す（中止・別の送信に移ったとき）。 */
  reset: () => void;
}

export function useRequirementCheck(initialChecking = false): RequirementCheckRunner {
  const [checking, setChecking] = useState(initialChecking);
  const [checkError, setCheckError] = useState<unknown>(null);
  const run = useCallback(
    async <T,>(
      task: () => Promise<T>,
      options: { isCurrent?: () => boolean; onUnavailable?: () => void } = {}
    ): Promise<T | null> => {
      const isCurrent = options.isCurrent ?? (() => true);
      setCheckError(null);
      setChecking(true);
      try {
        const result = await task();
        if (!isCurrent()) return null;
        setChecking(false);
        return result;
      } catch (cause) {
        if (!isCurrent()) return null;
        setChecking(false);
        if (isAbortError(cause)) return null;
        if (options.onUnavailable && isRequirementCheckUnavailable(cause)) {
          options.onUnavailable();
          return null;
        }
        setCheckError(cause);
        return null;
      }
    },
    []
  );
  const reset = useCallback(() => {
    setChecking(false);
    setCheckError(null);
  }, []);
  return { checking, checkError, run, reset };
}

/**
 * 検査の後の次の扱い（チャットと SQL 生成の画面で共通。#1442 / #1715 / #1761）。反映済み（`applied`。反映の後、
 * 次へ進む前に画面を離れた）なら `onApplied` で次へ進む。それ以外は、確認の質問の件数にかかわらず session を
 * 出し、利用者の確認（「この内容で SQL を生成して実行」）を待つ。聞くことが無くても自動では反映しない（#1761）。
 */
export function useRequirementProceed(controller: RequirementSessionController) {
  const { setSession } = controller;
  const proceed = useCallback(
    (loaded: QuerySession, onApplied: (applied: QuerySession) => void) => {
      if (requirementNextStep(loaded) === "applied") {
        onApplied(loaded);
        return;
      }
      setSession(loaded);
    },
    [setSession]
  );
  return { proceed };
}

/**
 * 要件の検査ができなかったときの表示（チャットと SQL 生成の画面で共通。#1442 / #1715）。理由（backend の 1 行）と
 * 「詳細」（失敗の種類・HTTP ステータス・モデル・所要時間・request ID）を `ApiErrorBanner` で出し、
 * 「もう一度確認」「確認せずに SQL を生成」を並べる（確認を終える操作は「確認せずに SQL を生成」だけ。#2311）。
 */
export function RequirementCheckFailure({
  error,
  onRetry,
  onSkip,
  size = "sm",
  testId,
}: {
  error: unknown;
  onRetry: () => void;
  onSkip: () => void;
  size?: ButtonSize;
  testId?: string;
}) {
  return (
    <ApiErrorBanner
      error={error}
      fallback={t("chat.requirement.failed")}
      nextAction={t("chat.requirement.failed.action")}
      testId={testId}
      action={
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="secondary" size={size} icon={RotateCcw} onClick={onRetry}>
            {t("chat.requirement.retry")}
          </Button>
          <Button type="button" variant="secondary" size={size} icon={MessageSquareText} onClick={onSkip}>
            {t("chat.requirement.skip")}
          </Button>
        </div>
      }
    />
  );
}

/** 回答・反映・中止の失敗（画面の文は Banner、API の失敗は「詳細」付きの `ApiErrorBanner`。#1715）。 */
export function RequirementErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  if (typeof error === "string") return <Banner severity="danger">{error}</Banner>;
  return <ApiErrorBanner error={error} fallback={t("nl2sql.requirementFailure.fallback")} />;
}

export function RequirementStateBanners({ state, unresolvedCount }: { state: RequirementState | null; unresolvedCount: number }) {
  if (!state) return null;
  return (
    <>
      {state.status === "unanswerable" ? <Banner severity="warning">{state.message_ja}</Banner> : null}
      {state.minimum_missing_ja.length > 0 && state.status !== "unanswerable" ? (
        <Banner severity="warning">{t("nl2sql.clarification.minimumMissing")}</Banner>
      ) : null}
      {state.review_limit_reached && unresolvedCount > 0 ? (
        <Banner severity="warning">
          {t("nl2sql.clarification.limitReached", {
            max: state.max_review_rounds,
            items: state.unresolved_items.map((item) => item.label_ja).join("、"),
          })}
        </Banner>
      ) : null}
    </>
  );
}

/**
 * 確認のカードの外から送った回答（確認済みの一覧の候補・確かめて生成するカードの「直しを伝える」）を反映している
 * 間の経過（回答の後に LLM の再検査が走ると数秒以上かかる）。経過時間はこの回答を送った時から数える（前の段階・
 * 利用者が答えていた時間を含めない）。`onSkip` を渡すと、待たずに「確認せずに SQL を生成」へ進む操作を経過の行に
 * 出す。確認のカードの「この内容で回答」の経過は、カードがフォームを置き換えて出す（`requirementCardConfirming`）。
 */
export function RequirementAnswerProgress({ active, onSkip }: { active: boolean; onSkip?: () => void }) {
  if (!active) return null;
  // スピナーは回答のボタンが担う（messaging.md §3.7）。
  return (
    <ProcessingIndicator
      active
      operationKey="guided-clarification-answer"
      label={t("nl2sql.clarification.loading.answer")}
      placement="panel"
      activityIcon="none"
      className="rounded-md border border-border bg-surface-sunken px-3 py-2"
      testId="nl2sql-guided-answer-progress"
      onCancel={onSkip}
      labels={{ cancel: t("chat.requirement.skip") }}
    />
  );
}

/** 回答前の確認のカードの文言（#1839）。文型は共通の `clarificationLabels`、NL2SQL は名詞だけを i18n から渡す。 */
export function requirementClarificationLabels(): ClarificationLabels {
  return {
    ...clarificationLabels({
      stage: t("nl2sql.clarification.stage"),
      action: t("nl2sql.clarification.action"),
    }),
    other: t("nl2sql.clarification.other"),
    otherPlaceholder: t("nl2sql.clarification.otherPlaceholder"),
    freeText: t("nl2sql.clarification.freeText"),
    freeTextPlaceholder: t("nl2sql.clarification.freeTextPlaceholder"),
    confirming: t("nl2sql.clarification.loading.answer"),
  };
}

/**
 * 選択肢の無い欄で、確認済みの要件を直す・条件を足すときの欄の文言（#3366）。書くのは確認事項への答えではなく
 * 直し・足す条件なので、共通の「答え」ではなく何を書くかを名前にする（選択肢があれば「その他（自由入力）」のまま）。
 */
function correctionFreeTextLabels(kind: "correct" | "add"): Partial<ClarificationLabels> {
  return kind === "add"
    ? {
        freeText: t("nl2sql.clarification.addText"),
        freeTextPlaceholder: t("nl2sql.clarification.addPlaceholder"),
        confirmBlockedFreeText: t("nl2sql.clarification.addBlocked"),
      }
    : {
        freeText: t("nl2sql.clarification.correctionText"),
        freeTextPlaceholder: t("nl2sql.clarification.correctionPlaceholder"),
        confirmBlockedFreeText: t("nl2sql.clarification.correctionBlocked"),
      };
}

/**
 * 確認のカードの「この内容で回答」を送っている（カードがフォームを答えた内容と経過の行に置き換えている）か。
 * 確認済みの一覧の候補から答えているときは含めない（カードは押せなくするだけ）。
 */
export function requirementCardConfirming(controller: RequirementSessionController): boolean {
  return controller.busyAction === "answer" && Boolean(controller.activeItem) && !controller.answeringCandidateId;
}

/** 回答の候補を共通のカードの選択肢にする（説明・物理名・根拠は選択肢の `detail` に出す）。 */
function candidateOption(candidate: RequirementCandidate): ClarificationOption {
  const refs = candidate.refs_ja && candidate.refs_ja !== candidate.label_ja ? candidate.refs_ja : "";
  return {
    id: candidate.id,
    label: candidate.label_ja,
    description: candidate.description_ja || undefined,
    detail:
      refs || candidate.evidence_ja ? (
        <>
          {refs ? <span className="block break-all font-mono">{refs}</span> : null}
          {candidate.evidence_ja ? (
            <span className="block text-fg-subtle">
              {t("nl2sql.clarification.evidence", { evidence: candidate.evidence_ja })}
            </span>
          ) : null}
        </>
      ) : undefined,
  };
}

/** カードの処理中の操作（確定はフォームを経過の行に置き換え、スキップは押したボタンだけを loading にする）。 */
function cardBusyAction(
  busyAction: RequirementBusyAction,
  map: Partial<Record<RequirementBusyAction, ClarificationBusyAction>>
): ClarificationBusyAction | null {
  return map[busyAction] ?? null;
}

/**
 * 回答前の確認のカード（#1839。3 製品共通の `ClarificationCard` に載せ替えた。#1439 / #1442 / #1761 の振る舞いは
 * そのまま）。2 つの段階を同じカードで出す。
 *
 * - **確認事項に答える段階**（今の質問・一覧から直している項目がある）: 1 件ずつ、選択肢（説明・物理名・根拠）と
 *   「その他（自由入力）」。確定は「この内容で回答」（回答を送り、AI が要件を検査し直す）。スキップ「確認せずに
 *   SQL を生成」は、要件が SQL の生成に進める（SELECT / FROM が明確）なら残りを AI の推測のまま反映し、進めない
 *   なら確認を使わずに SQL を作る。一覧から直しているときは「直した内容で更新」「直すのをやめる」。
 * - **確かめて生成する段階**（聞くことが無い・全部に答えた）: 対象の表の選び直し（候補があるとき）と
 *   「その他（自由入力）」の直し。確定は「この内容で SQL を生成して実行」。直しを入力している間は確定を押せず、
 *   押せない理由を確定の説明に出し、「直しを伝える」で AI に伝える（#1772 / #1776）。
 *
 * 選択肢と自由入力はどちらか一方（backend がどちらか 1 つで答える契約。複数選択でも同じ）。
 */
export function RequirementCard({
  controller,
  size,
  applyLabel,
  onApply,
  onSkip,
  onSkipWhileConfirming,
  idPrefix,
}: {
  controller: RequirementSessionController;
  size: ButtonSize;
  /** 確かめて生成する段階の確定の文言（実行の権限で「…して実行」/「…を生成」）。 */
  applyLabel: string;
  /** 確かめた要件で SQL を生成する（反映して次へ進む）。 */
  onApply: () => void;
  /** 確認を使わずに SQL を生成する（要件が生成に進めないときのスキップ。確認を終える唯一の操作。#2311）。 */
  onSkip: () => void;
  /** 「この内容で回答」の経過の行に出す「確認せずに SQL を生成」（AI の再検査を待たずに進む）。 */
  onSkipWhileConfirming?: () => void;
  idPrefix: string;
}) {
  const {
    activeItem,
    correctingItem,
    state,
    busyAction,
    selectedCandidateIds,
    freeText,
    showApply,
    unresolvedCount,
    answeredCount,
    confirmTarget,
    correctionKind,
  } = controller;
  const baseLabels = requirementClarificationLabels();
  if (!state) return null;
  // 確認の往復の何回目か（最初の確認が 1 回目。AI の再検査のたびに 1 回増え、再検査の上限の回が最後の確認）。
  const round = { current: state.review_round + 1, max: state.max_review_rounds + 1 };

  if (activeItem) {
    const adding = Boolean(correctingItem?.id.startsWith(ADD_ITEM_PREFIX));
    const prompt = correctingItem
      ? correctingItem.question_ja || t("nl2sql.clarification.correctLabel", { label: correctingItem.label_ja })
      : activeItem.question_ja || activeItem.label_ja;
    const item: ClarificationItem = {
      id: activeItem.id,
      prompt,
      reason: activeItem.reason_ja || undefined,
      selection: activeItem.answer_kind === "multi_select" ? "multiple" : "single",
      options: activeItem.candidates.map(candidateOption),
      meta: (
        <>
          <StatusBadge icon={false} variant="neutral" label={sectionPartLabel(activeItem.section)} />
          {correctingItem ? (
            <span className="text-xs text-fg-muted">
              {adding ? (
                t("nl2sql.clarification.addTitle", { part: correctingItem.label_ja })
              ) : (
                <>
                  <span>{t("nl2sql.clarification.correctTitle", { label: correctingItem.label_ja })}</span>
                  <span className="ml-2">
                    {t("nl2sql.clarification.correctHint", {
                      value: correctingItem.value_ja || t("nl2sql.clarification.unspecifiedValue"),
                    })}
                  </span>
                </>
              )}
            </span>
          ) : null}
        </>
      ),
    };
    // スキップは、要件が SQL の生成に進めるなら残りを AI の推測のまま反映し、進めないなら確認を使わずに作る。
    const skipApplies = showApply && unresolvedCount > 0;
    const answeringFromList = busyAction === "answer" && Boolean(controller.answeringCandidateId);
    return (
      <div className="min-w-0" data-testid="nl2sql-clarification-question" data-item-key={activeItem.key}>
        <ClarificationCard
          variant="plain"
          size={size}
          headingLevel={4}
          // 確認事項が出たとき（SQL 生成の画面のボタン・チャットの送信の後）は、確認事項の文へフォーカスを移す。
          autoFocus
          idPrefix={`${idPrefix}-clarification`}
          testId="nl2sql-clarification-card"
          items={[item]}
          value={{ [activeItem.id]: { optionIds: selectedCandidateIds, otherText: freeText } }}
          onValueChange={(draft) => {
            const entry = draft[activeItem.id];
            if (entry) controller.setAnswerDraft(entry.optionIds, entry.otherText);
          }}
          progress={
            correctingItem ? undefined : { current: answeredCount + 1, total: answeredCount + unresolvedCount }
          }
          round={round}
          // 確認済みの一覧の候補から答えている間は、カードは押せなくするだけ（経過は一覧の側の行）。
          busyAction={
            answeringFromList ? null : cardBusyAction(busyAction, { answer: "confirm", apply: "skip" })
          }
          disabled={answeringFromList}
          onConfirm={() => void controller.submitAnswer()}
          onSkip={correctingItem ? undefined : skipApplies ? onApply : onSkip}
          onSkipWhileConfirming={correctingItem ? undefined : onSkipWhileConfirming}
          // 直す・足す入力の取りやめ（前の確認事項に戻る。質問・確認は取り消さない）。
          dismissAction={
            correctingItem
              ? {
                  label: adding ? t("nl2sql.clarification.addCancel") : t("nl2sql.clarification.correctCancel"),
                  onClick: () => controller.setCorrectingItemId(null),
                }
              : undefined
          }
          labels={{
            ...baseLabels,
            ...(correctingItem ? correctionFreeTextLabels(adding ? "add" : "correct") : {}),
            confirm: adding
              ? t("nl2sql.clarification.addSubmit")
              : correctingItem
                ? t("nl2sql.clarification.correctSubmit")
                : t("nl2sql.clarification.answer"),
          }}
        />
      </div>
    );
  }

  if (!showApply) return null;
  const currentId = confirmTarget ? currentCandidateId(confirmTarget) : "";
  const checkedId = selectedCandidateIds[0] ?? currentId;
  const confirmItemId = confirmTarget?.id ?? `${idPrefix}-confirm`;
  const pendingHint =
    correctionKind === "free_text"
      ? t("nl2sql.clarification.otherPendingHint")
      : correctionKind === "table"
        ? t("nl2sql.clarification.targetPendingHint")
        : null;
  const item: ClarificationItem = confirmTarget
    ? {
        id: confirmTarget.id,
        prompt: t("nl2sql.clarification.confirmTargetPrompt", { label: confirmTarget.label_ja }),
        options: confirmTarget.candidates.map(candidateOption),
        meta: <StatusBadge icon={false} variant="neutral" label={sectionPartLabel(confirmTarget.section)} />,
        optional: true,
      }
    : { id: confirmItemId, prompt: t("nl2sql.clarification.confirmPrompt"), options: [], optional: true };
  const sendingCorrection = busyAction === "answer";
  return (
    <div className="min-w-0" data-testid="nl2sql-clarification-confirm">
      <ClarificationCard
        variant="plain"
        size={size}
        headingLevel={4}
        idPrefix={`${idPrefix}-confirm`}
        testId="nl2sql-clarification-card"
        items={[item]}
        value={{
          [item.id]: { optionIds: confirmTarget && checkedId ? [checkedId] : [], otherText: freeText },
        }}
        onValueChange={(draft) => {
          const entry = draft[item.id];
          if (entry) controller.setAnswerDraft(entry.optionIds, entry.otherText);
        }}
        round={round}
        busyAction={cardBusyAction(busyAction, { apply: "confirm" })}
        disabled={sendingCorrection}
        blockedReason={pendingHint}
        onConfirm={onApply}
        // 確かめた要件を使わずに、質問のまま SQL を生成する（確認を終える唯一の操作。#2311）。
        onSkip={onSkip}
        labels={{
          ...baseLabels,
          ...correctionFreeTextLabels("correct"),
          confirm: applyLabel,
          confirming: t("nl2sql.clarification.loading.apply"),
        }}
        actions={
          <Button
            type="button"
            variant="secondary"
            size={size}
            icon={MessageSquareText}
            className="w-full sm:w-auto"
            loading={sendingCorrection}
            disabled={!controller.correctionPending || Boolean(busyAction && !sendingCorrection)}
            onClick={() => void controller.submitCorrection()}
            data-testid="nl2sql-clarification-confirm-answer"
          >
            {t("nl2sql.clarification.sendCorrection")}
          </Button>
        }
      />
    </div>
  );
}

/**
 * 「SQL の要件（AI の理解）」（#1718）。値のある部分だけを 1 行ずつの一覧（項目名・値・状態・「直す」）にし、
 * 値の無い部分は「指定なし: …」の 1 行と「条件を追加」にまとめる。状態は 確定 / AI の推測 / 要確認 の 3 つで、
 * 要確認の行は質問と候補を出し、候補を押して答えられる。要確認が無ければ畳んで要約の 1 行にする。
 * SQL 生成の画面とチャットで同じ部品・同じ振る舞い（画面ごとの分岐を持たない）。
 */
export function RequirementItemsList({ controller }: { controller: RequirementSessionController }) {
  const { state, draft, items, activeItem, busyAction, answeringCandidateId } = controller;
  const overview = requirementOverview(items);
  const needsAnswer = overview.needsAnswerCount > 0;
  // 要確認があれば開き、無ければ畳む。要確認の有無が変わったら、その既定に戻す（利用者は開閉できる）。
  const [open, setOpen] = useState(needsAnswer);
  const [addOpen, setAddOpen] = useState(false);
  const needsAnswerChanged = useValuesChanged([needsAnswer]);
  if (needsAnswerChanged) {
    setOpen(needsAnswer);
  }
  const addGroupId = useId();
  if (!state || !draft) return null;
  const editable = state.status !== "unanswerable";
  const busy = Boolean(busyAction);
  const showUnspecified =
    overview.unspecifiedSections.length > 0 || (editable && overview.addableSections.length > 0);
  return (
    <section
      aria-label={t("nl2sql.clarification.requirements")}
      className="min-w-0"
      data-testid="nl2sql-clarification-requirements"
      data-needs-answer={needsAnswer ? "true" : "false"}
    >
      <Disclosure
        surface="sunken"
        summary={t("nl2sql.clarification.requirements")}
        // 畳んだときは要約の 1 行（要確認が無ければ「（確認の必要なし）」を添える）。状態のバッジは要確認の
        // ときだけ出し、狭い画面で要約の幅を取らない。
        description={
          open
            ? undefined
            : needsAnswer
              ? overview.summary || undefined
              : `${overview.summary}${t("nl2sql.clarification.noAnswerNeededSuffix")}`
        }
        meta={
          needsAnswer ? (
            <StatusBadge
              variant="warning"
              label={t("nl2sql.clarification.needsAnswerCount", { count: overview.needsAnswerCount })}
            />
          ) : undefined
        }
        open={open}
        onOpenChange={setOpen}
        summaryProps={{ "data-testid": "nl2sql-clarification-requirements-toggle" }}
        contentClassName="grid gap-2 p-3"
      >
        <p className="text-xs leading-5 text-fg-muted">{t("nl2sql.clarification.requirementsHint")}</p>
        {overview.listed.length > 0 ? (
          <ul className="grid min-w-0 divide-y divide-border">
            {overview.listed.map(({ item, kind }) => {
              const part = sectionPartLabel(item.section);
              const isCurrent = item.id === activeItem?.id;
              const value = (item.value_ja ?? "").trim() || item.sql_hint || "";
              const showLabel = item.label_ja && item.label_ja !== part && item.label_ja !== value;
              return (
                <li
                  key={item.id}
                  className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3 gap-y-1 py-2 first:pt-0 last:pb-0 sm:grid-cols-[8rem_minmax(0,1fr)_auto]"
                  data-testid="nl2sql-requirement-item"
                  data-item-key={item.key}
                  data-item-kind={kind}
                  data-item-status={item.answered ? "answered" : item.status}
                  data-current={isCurrent ? "true" : undefined}
                >
                  <span className="col-start-1 row-start-1 pt-1 text-xs font-medium text-fg-muted">{part}</span>
                  <div className="col-span-2 row-start-2 grid min-w-0 gap-0.5 sm:col-span-1 sm:col-start-2 sm:row-start-1">
                    {kind === "needs_answer" ? (
                      <>
                        <p className="break-words text-sm font-medium leading-6 text-fg">
                          {item.question_ja || item.label_ja}
                        </p>
                        {item.reason_ja ? (
                          <p className="break-words text-xs leading-5 text-fg-muted">{item.reason_ja}</p>
                        ) : null}
                        {isCurrent ? (
                          // 今の質問は回答欄（候補の説明・根拠・自由入力つき）で答える。同じ候補を 2 か所に出さない。
                          <p className="text-xs leading-5 text-fg-muted">{t("nl2sql.clarification.answeringInForm")}</p>
                        ) : editable && canAnswerWithCandidate(item) ? (
                          <div
                            role="group"
                            aria-label={t("nl2sql.clarification.candidates", { label: item.label_ja })}
                            className="mt-1 flex min-w-0 flex-wrap gap-2"
                          >
                            {item.candidates.map((candidate) => (
                              <Button
                                key={candidate.id}
                                type="button"
                                variant="secondary"
                                size="sm"
                                icon={Check}
                                loading={answeringCandidateId === candidate.id}
                                disabled={busy && answeringCandidateId !== candidate.id}
                                aria-label={t("nl2sql.clarification.answerWithCandidate", {
                                  candidate: candidate.label_ja,
                                })}
                                tooltip={candidate.description_ja || candidate.refs_ja || false}
                                onClick={() => void controller.answerWithCandidate(item, candidate.id)}
                              >
                                {candidate.label_ja}
                              </Button>
                            ))}
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <>
                        <p className="break-words text-sm font-medium leading-6 text-fg">{value}</p>
                        {showLabel ? (
                          <p className="break-words text-xs leading-5 text-fg-muted">{item.label_ja}</p>
                        ) : null}
                        {item.refs_ja && !value.includes(item.refs_ja) ? (
                          <p className="break-all font-mono text-xs leading-5 text-fg-muted">{item.refs_ja}</p>
                        ) : null}
                        {item.period?.range_ja && !value.includes(item.period.range_ja) ? (
                          <p className="text-xs leading-5 text-fg-muted">{item.period.range_ja}</p>
                        ) : null}
                        {item.answered ? (
                          <p className="text-xs leading-5 text-fg-muted">{t("nl2sql.clarification.answeredNote")}</p>
                        ) : null}
                      </>
                    )}
                  </div>
                  <div className="col-start-2 row-start-1 flex items-center justify-end gap-1 sm:col-start-3">
                    <StatusBadge variant={KIND_BADGE_VARIANT[kind]} label={requirementKindLabel(kind)} />
                    {kind !== "needs_answer" && !isCurrent && editable ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        icon={Pencil}
                        disabled={busy}
                        aria-label={t("nl2sql.clarification.correctLabel", { label: item.label_ja })}
                        onClick={() => controller.setCorrectingItemId(item.id)}
                      >
                        {t("nl2sql.clarification.correct")}
                      </Button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        ) : null}
        {showUnspecified ? (
          <div
            className="grid min-w-0 gap-2 border-t border-border pt-2"
            data-testid="nl2sql-requirement-unspecified"
          >
            <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
              <p className="min-w-0 break-words text-xs leading-5 text-fg-muted">
                {overview.unspecifiedSections.length > 0
                  ? t("nl2sql.clarification.unspecified", {
                      parts: overview.unspecifiedSections.map(sectionPartLabel).join("・"),
                    })
                  : null}
              </p>
              {editable && overview.addableSections.length > 0 ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  icon={Plus}
                  disabled={busy}
                  aria-expanded={addOpen}
                  aria-controls={addGroupId}
                  onClick={() => setAddOpen((current) => !current)}
                  data-testid="nl2sql-requirement-add-toggle"
                >
                  <span>{t("nl2sql.clarification.addCondition")}</span>
                  <DisclosureChevron expanded={addOpen} size={16} />
                </Button>
              ) : null}
            </div>
            {editable && addOpen ? (
              <div
                id={addGroupId}
                role="group"
                aria-label={t("nl2sql.clarification.addParts")}
                className="flex min-w-0 flex-wrap gap-2"
              >
                {overview.addableSections.map((section) => (
                  <Button
                    key={section}
                    type="button"
                    variant="secondary"
                    size="sm"
                    icon={Plus}
                    disabled={busy}
                    aria-label={t("nl2sql.clarification.addPartLabel", { part: sectionPartLabel(section) })}
                    onClick={() => {
                      setAddOpen(false);
                      controller.startAdding(section);
                    }}
                  >
                    {sectionPartLabel(section)}
                  </Button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        {draft.warnings_ja.length > 0 ? (
          <Disclosure summary={t("nl2sql.clarification.warnings")} size="sm">
            <ul className="list-disc space-y-1 pl-5 text-xs leading-5 text-fg-muted">
              {draft.warnings_ja.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </Disclosure>
        ) : null}
      </Disclosure>
    </section>
  );
}

/** 要件のまとめ（書き直した質問・SQL の骨組みと注記）。 */
export function RequirementSummary({
  controller,
  idPrefix = "nl2sql",
  className = "grid gap-3 border-t border-border pt-4",
}: {
  controller: RequirementSessionController;
  idPrefix?: string;
  className?: string;
}) {
  const { state, draft, showFinalSummary } = controller;
  if (!showFinalSummary || !state || !draft) return null;
  return (
    <section
      aria-labelledby={`${idPrefix}-requirement-summary-title`}
      className={className}
      data-testid="nl2sql-clarification-summary"
    >
      <h4 id={`${idPrefix}-requirement-summary-title`} className="text-sm font-semibold text-fg">
        {t("nl2sql.clarification.summaryTitle")}
      </h4>
      <div className="grid gap-1">
        <p className="text-xs font-medium text-fg-muted">{t("nl2sql.clarification.clarifiedQuestion")}</p>
        <p
          className="break-words rounded-md border border-border bg-surface-sunken px-3 py-2 text-sm leading-6 text-fg"
          data-testid="nl2sql-clarified-question"
        >
          {draft.clarified_question_ja}
        </p>
      </div>
      <div className="grid gap-1">
        <p className="text-xs font-medium text-fg-muted">{t("nl2sql.clarification.skeleton")}</p>
        <pre
          data-surface="code"
          tabIndex={0}
          role="region"
          aria-label={t("nl2sql.clarification.skeleton")}
          className="max-h-80 overflow-auto rounded-md border border-border p-3 font-mono text-xs leading-5"
          data-testid="nl2sql-clarification-skeleton"
        >
          {state.sql_skeleton}
        </pre>
        <p className="text-xs leading-5 text-fg-muted">{t("nl2sql.clarification.skeletonNote")}</p>
      </div>
    </section>
  );
}
