import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { ChatAnswer, ChatTurn } from "@production-ready/ui";

import { randomUuid } from "@/lib/randomUuid";

import { cancelQuerySession, createQuerySession, getQuerySession } from "../ontology/api";
import {
  chatRequirementIdempotencyKey,
  chatRequirementSessionRequest,
  type ChatRequirementCheck,
} from "../chatRequirement";
import {
  isSessionOpen,
  useRequirementCheck,
  useRequirementProceed,
  useRequirementSession,
  type RequirementCheckRunner,
  type RequirementSessionController,
} from "./RequirementClarification";
import { RequirementClarificationView } from "./RequirementClarificationView";

/**
 * チャットの回答前の確認（#1442）。SQL 生成の画面の回答前の確認（`GuidedClarificationPanel`）を基準にし、同じ hook
 * （`useRequirementSession`・`useRequirementCheck`・`useRequirementProceed`）と同じ部品
 * （`RequirementClarificationView`）・同じ文言を使う（#1715 / #1761）。チャットだけの違いは入口（送信のときに自動で
 * 始め、会話の続きを検査に渡す）だけ。確認の質問が 0 件でも、「この内容で SQL を生成して実行」を押すまで SQL を
 * 作らない。
 */
export interface ChatRequirementFlow {
  check: ChatRequirementCheck | null;
  controller: RequirementSessionController;
  requirementCheck: RequirementCheckRunner;
  /** 要件の検査の最中（入力欄の送信のボタンは「停止」になり、押すと確認を中止する）。 */
  running: boolean;
  /** 会話の欄の末尾へ追う key（確認の質問・まとめが変わったとき）。 */
  scrollKey: string;
  /** もう一度確かめる（新しい session を作る）。 */
  retry: () => void;
  /**
   * 確認を終えて、確認せずに SQL を生成する（質問は取り消さない。#2303）。AI の処理中（要件の下書き・回答の後の
   * 再検査）なら、その応答を待たずに捨てて進む。
   */
  skip: () => void;
  /** 確認を中止する（入力欄の「停止」。質問は入力欄に戻す）。 */
  cancel: () => void;
  /** 確かめた要件で SQL を生成する。 */
  generate: () => void;
  /** 会話・業務プロファイルを変えたときに、確認を外す（サーバーの session は中止する）。 */
  discard: () => void;
}

interface ChatRequirementFlowOptions {
  check: ChatRequirementCheck | null;
  setCheck: (check: ChatRequirementCheck | null) => void;
  /**
   * SQL の生成のジョブを投入する（`sessionId` が無ければ確認せずに作る）。`requirementQuestion` は確かめた
   * 質問の文（回答の「回答前の確認で確かめた質問」を、会話の取り直しを待たずに出す）。
   */
  onSubmit: (
    check: ChatRequirementCheck,
    sessionId: string | null,
    requirementQuestion?: string,
    skipped?: boolean
  ) => void;
  /** 確認を中止した（送った質問を入力欄に戻す）。 */
  onCancelled: (check: ChatRequirementCheck) => void;
  /** 画面が表示されている（keep-alive で隠れている間は検査を始めない）。 */
  enabled: boolean;
}

export function useChatRequirementFlow({
  check,
  setCheck,
  onSubmit,
  onCancelled,
  enabled,
}: ChatRequirementFlowOptions): ChatRequirementFlow {
  const controller = useRequirementSession();
  const { session, setSession, setError } = controller;
  const requirementCheck = useRequirementCheck();
  const { run: runCheck, reset: resetCheck } = requirementCheck;
  const { proceed } = useRequirementProceed(controller);
  // 検査を始めた送信（job ID）。StrictMode・再レンダーで二重に始めない。
  const startedRef = useRef("");
  // 作成の応答の前に中止した送信（応答が届いたら session を中止する）。
  const abandonedRef = useRef(new Set<string>());
  const checkRef = useRef(check);
  const onSubmitRef = useRef(onSubmit);
  useLayoutEffect(() => {
    checkRef.current = check;
    onSubmitRef.current = onSubmit;
  });
  const clientJobId = check?.request.client_job_id ?? "";

  // 送った質問の要件の検査を始める（作業状態から戻したときは、作った session を読み直して続ける）。
  useEffect(() => {
    if (!enabled || !check || startedRef.current === check.request.client_job_id) return;
    const current = check;
    const id = current.request.client_job_id;
    startedRef.current = id;
    const isCurrent = () => !abandonedRef.current.has(id) && checkRef.current?.request.client_job_id === id;
    void runCheck(
      async () => {
        const loaded = current.sessionId
          ? await getQuerySession(current.sessionId)
          : await createQuerySession(chatRequirementSessionRequest(current.request), {
              idempotencyKey: chatRequirementIdempotencyKey(id),
            });
        // 作成の応答の前に中止した・別の送信に移ったときは、作られた確認をサーバーで中止する。
        if (!isCurrent() && isSessionOpen(loaded)) void cancelQuerySession(loaded.id).catch(() => undefined);
        return loaded;
      },
      {
        isCurrent,
        // モデルの API が無い環境では要件の検査を使えないので、確認せずに SQL を作る。
        onUnavailable: current.sessionId ? undefined : () => onSubmitRef.current(current, null),
      }
    ).then(async (loaded) => {
      if (!loaded) return;
      if (!current.sessionId) setCheck({ ...current, sessionId: loaded.id });
      if (!isCurrent()) return;
      // 確認の質問の件数にかかわらず、確認のカードで利用者の確定を待つ（SQL 生成の画面と同じ。#1761）。
      proceed(loaded, (applied) =>
        onSubmitRef.current(current, applied.id, applied.requirement?.clarified_question_ja)
      );
    });
  }, [check, enabled, proceed, runCheck, setCheck]);

  // 別の送信に移ったら、前の送信の確認の状態を外す（render 中に前の値と比べて直す）。
  const [shownJobId, setShownJobId] = useState(clientJobId);
  if (shownJobId !== clientJobId) {
    setShownJobId(clientJobId);
    setSession(null);
    setError("");
    controller.setCorrectingItemId(null);
    resetCheck();
  }

  const running = requirementCheck.checking;

  const retry = () => {
    if (!check) return;
    // 新しい session で確かめ直す（ジョブはまだ作っていないので、job ID と作成の key を新しくする）。
    setCheck({
      ...check,
      request: { ...check.request, client_job_id: randomUuid() },
      sessionId: "",
    });
  };

  const skip = () => {
    if (!check) return;
    // AI の処理中でも待たない（#2303）。作成の応答はこの送信のものでなくなり、届いたらサーバーで中止する
    // （上の検査の effect）。送っている回答は中断し、その応答・失敗は出さない。
    abandonedRef.current.add(check.request.client_job_id);
    controller.abortAnswer();
    resetCheck();
    // 開いている確認はサーバーで中止してから、確認を使わずに作る（会話には「確認せずに…」を残す。#1839）。
    if (isSessionOpen(session)) void cancelQuerySession(session.id).catch(() => undefined);
    onSubmit(check, null, undefined, true);
  };

  const cancel = () => {
    if (!check) return;
    if (running) {
      abandonedRef.current.add(check.request.client_job_id);
      resetCheck();
      onCancelled(check);
      return;
    }
    void controller.cancelSession().then((done) => {
      if (done) onCancelled(check);
    });
  };

  const discard = () => {
    if (!check) return;
    abandonedRef.current.add(check.request.client_job_id);
    if (isSessionOpen(session)) void cancelQuerySession(session.id).catch(() => undefined);
    resetCheck();
    setCheck(null);
  };

  const generate = () => {
    if (!check) return;
    const current = check;
    void controller.applyRequirement().then((applied) => {
      if (applied && checkRef.current?.request.client_job_id === current.request.client_job_id) {
        onSubmit(current, applied.id, applied.requirement?.clarified_question_ja);
      }
    });
  };

  return {
    check,
    controller,
    requirementCheck,
    running,
    scrollKey: [
      running ? "running" : "",
      requirementCheck.checkError ? "failed" : "",
      session?.status ?? "",
      session?.current_intent_version ?? "",
    ].join(":"),
    retry,
    skip,
    cancel,
    generate,
    discard,
  };
}

/** チャットの会話の中の回答前の確認の 1 往復（送った質問の吹き出しと、その下の回答前の確認）。 */
export function ChatRequirementTurn({
  flow,
  canExecuteSql,
}: {
  flow: ChatRequirementFlow;
  canExecuteSql: boolean;
}) {
  const { check } = flow;
  if (!check) return null;
  return (
    <ChatTurn question={check.request.question} testId="sql-chat-requirement-turn">
      <ChatAnswer testId="sql-chat-requirement">
        <RequirementClarificationView
          key={check.request.client_job_id}
          controller={flow.controller}
          check={flow.requirementCheck}
          startPhase="prepare_questions"
          onRetry={flow.retry}
          onSkip={flow.skip}
          onApply={flow.generate}
          // 確認を終えるのは「確認せずに SQL を生成」（flow.skip）だけで、質問を取り消さずに SQL の生成へ進む
          // （#2303 / #2311）。質問を取り消すのは入力欄の「停止」。
          canExecuteSql={canExecuteSql}
          size="sm"
          idPrefix="sql-chat"
        />
      </ChatAnswer>
    </ChatTurn>
  );
}
