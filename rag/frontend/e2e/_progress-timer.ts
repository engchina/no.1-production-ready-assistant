import { expect, type Page } from "@playwright/test";

/**
 * チャットの処理の経過（共有の ChatProgress）の右上の経過時間を、段階が進む間 100ms ごとに記録する（#1176）。
 * 経過時間は処理全体の通算で、段階が変わっても 0 に戻らないことを `expectProgressTimerMonotonic` で確かめる。
 * 3 製品（RAG・NL2SQL・Agent）の e2e で同じ確かめ方をする。
 */
export async function startProgressTimerSampler(page: Page, testIdPrefix: string) {
  await page.evaluate((prefix) => {
    const w = window as unknown as { __progressTimerSamples: string[]; __progressTimerSampler?: number };
    w.__progressTimerSamples = [];
    w.__progressTimerSampler = window.setInterval(() => {
      const timer = document.querySelector(`[data-testid="${prefix}-timer"]`);
      const current = document.querySelector(`[data-testid="${prefix}-current"]`);
      if (timer && current)
        w.__progressTimerSamples.push(`${current.getAttribute("data-step-id") ?? ""}|${timer.getAttribute("aria-label") ?? ""}`);
    }, 100);
  }, testIdPrefix);
}

/** 記録を止め、`minSteps` 個以上の段階を通る間に経過時間が減らなかったことを確かめる。 */
export async function expectProgressTimerMonotonic(page: Page, minSteps = 2) {
  const samples = await page.evaluate(() => {
    const w = window as unknown as { __progressTimerSamples: string[]; __progressTimerSampler?: number };
    window.clearInterval(w.__progressTimerSampler);
    return w.__progressTimerSamples;
  });
  const parsed = samples.map((sample) => {
    const [step, label] = sample.split("|");
    const [, hours, minutes, seconds] = label.match(/(?:(\d+):)?(\d+):(\d+)$/) ?? [];
    return { step, seconds: Number(hours ?? 0) * 3600 + Number(minutes) * 60 + Number(seconds) };
  });
  expect(new Set(parsed.map((sample) => sample.step)).size, samples.join(", ")).toBeGreaterThanOrEqual(minSteps);
  for (let index = 1; index < parsed.length; index += 1)
    expect(parsed[index].seconds, samples.join(", ")).toBeGreaterThanOrEqual(parsed[index - 1].seconds);
}
