import { logBrowserDiagnostic, stripBasePath } from "@production-ready/ui";
import { useContext, useEffect, useLayoutEffect, useRef } from "react";
import {
  UNSAFE_DataRouterContext,
  useBlocker,
  useHref,
  useNavigate,
  type BlockerFunction,
} from "react-router-dom";

type ConfirmLeaveRef = { current: () => Promise<boolean> };

/**
 * 未保存の編集を持つ（`enabled` の）ガードの確認関数。登録順に並ぶ。
 * 1 画面に離脱の確認が複数あっても、戻る / 進むの blocker は `UnsavedChangesBlocker` の 1 つだけにし、
 * ここに集めた状態で判定する（#586）。
 */
const activeGuards = new Set<ConfirmLeaveRef>();
let mountedBlockers = 0;
let warnedMissingBlocker = false;

/**
 * 未保存の編集がある画面の確認を通す。未保存が無ければ確認せずに true。
 * 複数のフォームが未保存でも、確認は最初に登録したガードの 1 回だけにする。
 * ログアウトなど `navigate()` で画面を離れる操作は、移動の前にこれを呼ぶ。
 */
export function confirmUnsavedChanges(): Promise<boolean> {
  const [first] = activeGuards;
  return first ? first.current() : Promise.resolve(true);
}

/**
 * 未保存の編集があるとき、画面離脱の前に確認を挟む。
 *
 * - 内部リンクの click を capture 段階で受けて確認ダイアログを挟み、承認された場合のみ `navigate` する。
 * - タブを閉じる・再読込は `beforeunload` が担当する。
 * - ブラウザの戻る/進む（popstate）は、data router（`createBrowserRouter` + `RouterProvider`）に
 *   1 つだけ置いた `UnsavedChangesBlocker` が確認する（#138 / #586）。この hook 自身は `useBlocker` を
 *   呼ばない（React Router は blocker を 1 つしか扱えず、複数あると最後に登録したものだけで判定する）。
 *   画面内のボタンが自分で確認してから `navigate` する流れ（PUSH / REPLACE）は二重に確認しないよう止めない。
 */
export function useUnsavedChangesGuard(
  enabled: boolean,
  confirmLeave: () => Promise<boolean>
): void {
  const navigate = useNavigate();
  // router の basename（製品を /rag/ などの prefix で配信するとき。#1316）。<a> の href は basename を含むので、
  // navigate() に渡す前に外す（外さないと /rag/rag/... に移動する）。basename が無ければ "/"。
  const rootHref = useHref("/");
  const confirmLeaveRef = useRef(confirmLeave);
  confirmLeaveRef.current = confirmLeave;
  const inDataRouter = useContext(UNSAFE_DataRouterContext) !== null;

  useEffect(() => {
    if (!enabled) return;
    activeGuards.add(confirmLeaveRef);
    if (inDataRouter && mountedBlockers === 0 && !warnedMissingBlocker) {
      warnedMissingBlocker = true;
      logBrowserDiagnostic("WARNING", {
        event: "unsaved_changes_blocker_missing", serviceName: "production-ready-platform",
      });
    }
    return () => {
      activeGuards.delete(confirmLeaveRef);
    };
  }, [enabled, inDataRouter]);

  useEffect(() => {
    if (!enabled) return;

    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Safari 等の旧仕様向け。文言はブラウザ側が決めるため i18n 対象外。
      event.returnValue = "";
    };

    const handleClick = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest("a");
      if (!anchor || anchor.hasAttribute("download") || anchor.target === "_blank") return;
      const href = anchor.getAttribute("href");
      if (!href || href.startsWith("#")) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin) return;
      const destination = `${url.pathname}${url.search}${url.hash}`;
      const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (destination === current) return;

      event.preventDefault();
      event.stopPropagation();
      // 未保存のフォームが複数あっても、確認は 1 回だけ（最初の listener が defaultPrevented にする）。
      void confirmLeaveRef.current().then((confirmed) => {
        if (confirmed) navigate(stripBasePath(destination, rootHref));
      });
    };

    window.addEventListener("beforeunload", handleBeforeUnload);
    document.addEventListener("click", handleClick, true);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
      document.removeEventListener("click", handleClick, true);
    };
  }, [enabled, navigate, rootHref]);
}

/** 未保存の画面があるとき、戻る/進むによる別 URL への移動を止める。 */
const shouldBlockHistoryPop: BlockerFunction = ({ historyAction, currentLocation, nextLocation }) =>
  activeGuards.size > 0 &&
  historyAction === "POP" &&
  (currentLocation.pathname !== nextLocation.pathname ||
    currentLocation.search !== nextLocation.search);

/**
 * ブラウザの戻る/進むを確認する、アプリで 1 つだけの blocker（#586）。data router の root
 * （`createBrowserRouter` の route の element）に 1 回だけ置く。`useUnsavedChangesGuard` が未保存と
 * 登録した画面があるときだけ止め、確認は `confirmUnsavedChanges()` と同じ 1 回にする。キャンセルしたら
 * URL を元に戻す。
 */
export function UnsavedChangesBlocker(): null {
  // 同じ commit で有効になったガードの passive effect より先に数える（layout effect は先に走る）。
  useLayoutEffect(() => {
    mountedBlockers += 1;
    return () => {
      mountedBlockers -= 1;
    };
  }, []);
  const blocker = useBlocker(shouldBlockHistoryPop);

  // blocker は router の状態に保持され、止めるたびに新しいオブジェクトになる。同じ履歴の項目へ
  // 続けて戻る/進むしたとき（state が blocked のまま）も確認し直せるよう、オブジェクトごとに扱う。
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    let settled = false;
    void confirmUnsavedChanges().then((confirmed) => {
      if (settled || blocker.state !== "blocked") return;
      settled = true;
      if (confirmed) blocker.proceed();
      else blocker.reset();
    });
    return () => {
      settled = true;
    };
  }, [blocker]);

  return null;
}
