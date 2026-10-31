import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { copyTextToClipboard } from "../../lib/clipboard";
import { toast } from "../../store/toast-store";
import { Button, type ButtonProps } from "./button";

/** コピーできた印（アイコンの `Check`）を出しておく時間（ms）。 */
export const COPY_FEEDBACK_MS = 2000;

export type CopyButtonProps = Omit<
  ButtonProps,
  "children" | "icon" | "loading" | "onClick" | "pressed" | "tone" | "trailingIcon" | "variant"
> & {
  /** 既定は secondary。文字のすぐ横に置く小さな操作（ID の横など）は ghost。 */
  variant?: "secondary" | "ghost";
  /**
   * コピーする文字。関数なら押したときに求める。Promise を返す（押してから取得する）と、取得の間は `loading` にし、
   * Safari のために取得を待つ前に `ClipboardItem` へ渡す。
   */
  text: string | (() => string | Promise<string>);
  /** ボタンのラベル（`iconOnly` のときは `aria-label` と Tooltip）。コピーの前後で変えない。 */
  label: string;
  /** コピーできたときの読み上げ（`aria-live="polite"`）。画面ではアイコンが一時的に `Check` になる。 */
  copiedMessage: string;
  /** コピーできなかったときの文言（秘密を含めない）。既定は Toast（error）で出す。 */
  failedMessage: string;
  /**
   * 失敗を同じ場所（ボタン・欄の直下など）に出すときに渡す。渡すと Toast を出さない。
   * 呼び出し側は `failedMessage` を `FormStatus` 等で出し、`onCopied` で消す。
   */
  onCopyError?: (message: string) => void;
  /** コピーできたとき（同じ場所に出した失敗を消すなど）。 */
  onCopied?: () => void;
  testId?: string;
};

/**
 * 文字をクリップボードへコピーするボタン（SQL・ID・URL・本文・一時パスワードなど。3 製品と共有の画面で共通。#2099）。
 *
 * - アイコンは `Copy`。コピーできたら `COPY_FEEDBACK_MS` の間 `Check` にし、`copiedMessage` を読み上げる
 *   （ラベルは変えない。幅が変わらず、周りの行が動かない）。成功の Toast は出さない。
 * - コピーできなければ `failedMessage` を Toast（error）で出す。固定の面・秘密の欄のように同じ場所に出すときは
 *   `onCopyError` を渡す。
 * - 見た目は共有の `Button`（既定は `variant="secondary"`。`size` は同じ行の部品と同じ値を渡す）。
 */
export function CopyButton({
  text,
  label,
  copiedMessage,
  failedMessage,
  onCopyError,
  onCopied,
  testId,
  variant = "secondary",
  iconOnly,
  ...props
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const [copying, setCopying] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    []
  );

  async function handleCopy() {
    if (timerRef.current) clearTimeout(timerRef.current);
    setCopied(false);
    // 続けて押したときも読み上げるよう、いったん空にしてから入れる。
    setAnnouncement("");
    const value = typeof text === "function" ? text() : text;
    const pending = typeof value !== "string";
    if (pending) setCopying(true);
    try {
      await copyTextToClipboard(value);
      setCopied(true);
      setAnnouncement(copiedMessage);
      onCopied?.();
      timerRef.current = setTimeout(() => {
        setCopied(false);
        setAnnouncement("");
      }, COPY_FEEDBACK_MS);
    } catch {
      if (onCopyError) onCopyError(failedMessage);
      else toast.error(failedMessage);
    } finally {
      if (pending) setCopying(false);
    }
  }

  return (
    <>
      <Button
        type="button"
        variant={variant}
        iconOnly={iconOnly}
        aria-label={iconOnly ? label : undefined}
        {...props}
        icon={copied ? Check : Copy}
        loading={copying}
        data-copy-state={copied ? "copied" : "idle"}
        data-testid={testId}
        onClick={() => void handleCopy()}
      >
        {iconOnly ? null : <span>{label}</span>}
      </Button>
      {/*
        読み上げ専用の文は画面の左上に固定する（`sr-only` の absolute を fixed にする）。absolute のままだと、
        ボタンが行の右端にあるとき静的な位置が右へはみ出して本文に横スクロールを作り（375px の API キーの
        接続先）、左上に寄せると位置を持つ祖先（main）が包含ブロックになって中のスクロール領域から抜け出す
        （e2e の findEscapedAbsoluteElements）。fixed はどのスクロール領域のあふれにも数えられない。
      */}
      <span role="status" aria-live="polite" aria-atomic="true" className="sr-only fixed! left-0 top-0">
        {announcement}
      </span>
    </>
  );
}
