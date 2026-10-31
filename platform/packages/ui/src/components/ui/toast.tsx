import { X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type FocusEvent } from "react";
import { createPortal } from "react-dom";

import { useNavDrawerMode } from "../app-shell/nav-drawer";
import { resolveToastPlacement } from "../../lib/toast-placement";
import { cn } from "../../lib/utils";
import { useToastStore, type ToastItem } from "../../store/toast-store";

import { Button } from "./button";
import { toneIcon, toneRole, toneText } from "./feedback-tone";
import { MessageText } from "./message-text";
import { useToastTopClearance } from "./use-toast-top-clearance";

export interface ToasterProps {
  dismissLabel?: string;
  regionLabel?: string;
}

/**
 * Toast 表示領域。アプリ最上位で一度だけ描画する。フォーカスを奪わず aria-live で読み上げる（toast-accessibility）。
 *
 * 置き場所（#1728。`lib/toast-placement.ts`）は画面幅ごとに決まった 1 か所で、ページの内容では動かさない。製品では変えない。
 * - md 以上: 画面の右下（右端から 1rem、下端から `--toast-offset-bottom`）。新しい通知は下に足し、古い通知を上へ押し上げる。
 *   下端からの距離は、画面の下端に固定された主操作（チャットの入力欄と送信）とページの末尾の操作の上に出る共通の定数。
 *   下端に固定された入力欄がそれより高い画面（チャット）では、入力欄の上端から 1rem 上に持ち上げる（#2103。`ChatLayout` の
 *   `useToastClearance`）。
 * - md 未満: 上端の全幅。上端のバーに重ね、メニューのボタンは覆わない。バーを超える高さは AppShell が確保し、戻る操作や本文に重ねない。
 * どちらも DOM の順（読み上げ・Tab の順）と見た目の上下の順は同じ（古い通知が上、新しい通知が下）。
 *
 * 幅（#899）: md 以上は内容に合わせて `--toast-width-min`〜`--toast-width-max`（22〜32rem）の間で広がり、
 * 積んだ通知は最も広い通知の幅にそろう。本文は `.pr-message-text` で日本語を文節で折り返す（語の途中で切らない）。
 *
 * 重なり順はモーダルの下（`--z-toast` < `--z-scrim` < `--z-dialog`）。モーダル中はモーダル外を操作できないため、
 * 通知を上に重ねても押せず、確認ダイアログのボタンを覆うだけになる。
 *
 * 閉じるボタンの aria ラベルは `dismissLabel` で注入（既定「閉じる」）。
 *
 * いずれかの通知にポインタが乗っている間、または通知の中にフォーカスがある間は、すべての通知の自動消滅を止め、
 * 離れたら残り時間から再開する（WCAG 2.2.1 の考え方。読んでいる途中・操作しようとしている途中で消さない）。
 * 通知ごとではなく全体を止めるのは、下の通知が消えて読んでいる通知の位置がずれないようにするため。
 */
export function Toaster({ dismissLabel = "閉じる", regionLabel = "通知" }: ToasterProps = {}) {
  const toasts = useToastStore((state) => state.toasts);
  const narrow = useNavDrawerMode();
  const placement = useMemo(() => resolveToastPlacement({ narrow }), [narrow]);
  const pause = useToastStore((state) => state.pause);
  const resume = useToastStore((state) => state.resume);
  const [mounted, setMounted] = useState(false);
  const regionRef = useRef<HTMLDivElement>(null);
  useToastTopClearance(regionRef, mounted && narrow && toasts.length > 0);
  // ホバー・フォーカス中の通知（`hover:<id>` / `focus:<id>`）。1 つでもあれば止める。
  const engaged = useRef(new Set<string>());
  const setEngaged = useCallback(
    (key: string, active: boolean) => {
      if (active) engaged.current.add(key);
      else engaged.current.delete(key);
      if (engaged.current.size > 0) pause();
      else resume();
    },
    [pause, resume]
  );

  useEffect(() => setMounted(true), []);
  if (!mounted) return null;

  return createPortal(
    <div
      ref={regionRef}
      role="region"
      aria-label={regionLabel}
      aria-live="polite"
      aria-relevant="additions"
      data-toast-placement={placement.mode}
      className="pointer-events-none fixed z-[var(--z-toast)] flex flex-col gap-2 overflow-y-auto"
      style={placement.style as CSSProperties}
    >
      {toasts.map((item) => (
        <ToastCard key={item.id} item={item} dismissLabel={dismissLabel} onEngage={setEngaged} />
      ))}
    </div>,
    document.body
  );
}

function ToastCard({
  item,
  dismissLabel,
  onEngage,
}: {
  item: ToastItem;
  dismissLabel: string;
  onEngage: (key: string, active: boolean) => void;
}) {
  const dismiss = useToastStore((state) => state.dismiss);
  const Icon = toneIcon[item.tone];
  const hoverKey = `hover:${item.id}`;
  const focusKey = `focus:${item.id}`;

  // ホバー・フォーカス中に閉じられた（DOM から外れた）通知は、leave / blur が届かないため、外れたときに解除する。
  useEffect(
    () => () => {
      onEngage(hoverKey, false);
      onEngage(focusKey, false);
    },
    [onEngage, hoverKey, focusKey]
  );

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    // 通知の中でフォーカスが移るだけなら止めたままにする。
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
    onEngage(focusKey, false);
  };

  return (
    <div
      role={toneRole(item.tone)}
      onPointerEnter={() => onEngage(hoverKey, true)}
      onPointerLeave={() => onEngage(hoverKey, false)}
      onFocus={() => onEngage(focusKey, true)}
      onBlur={handleBlur}
      className="pr-message-text animate-toast-in pointer-events-auto flex items-start gap-2.5 rounded-lg border border-border bg-surface-raised px-3.5 py-3 shadow-[var(--shadow-toast)]"
    >
      {/* 状態のアイコンは見出しの 1 行目の中心にそろえる（見出しと同じ文字の大きさと行の高さの箱。#2371）。 */}
      <span className={cn("pr-icon-line text-sm leading-relaxed", toneText[item.tone])}>
        <Icon size={16} aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium leading-relaxed text-fg">
          <MessageText text={item.message} />
        </p>
        {item.description ? (
          <p className="mt-0.5 text-xs leading-relaxed text-fg-muted">
            <MessageText text={item.description} />
          </p>
        ) : null}
        {item.action ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              item.action?.onClick();
              dismiss(item.id);
            }}
            className="mt-1.5"
          >
            {item.action.label}
          </Button>
        ) : null}
      </div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        iconOnly
        touchTarget
        icon={X}
        onClick={() => dismiss(item.id)}
        aria-label={dismissLabel}
        className="-mr-2 -mt-2 text-fg-muted hover:enabled:text-fg"
      />
    </div>
  );
}
