import { expect, type Page } from "@playwright/test";

/**
 * `page.clock.install()` した画面で、時計を `stepMs` ずつ進めながら値を待つ（#1593）。
 *
 * 画面の時間の経過を実時間で待たない: 定期取得（実行中の job は 1〜2 秒）、経過時間の表示（1 秒）、
 * 通知の自動の消去（4 秒）、TanStack Query の失敗の再試行（既定の 3 回。1・2・4 秒）。
 * `page.clock.install()` は `page.goto` の前に呼ぶ。時計は止めないので、進めない間も時間は普通に流れる。
 */
export function pollWithClock<T>(page: Page, read: () => Promise<T>, stepMs: number) {
  return expect.poll(async () => {
    await page.clock.runFor(stepMs);
    return read();
  });
}
