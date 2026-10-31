import { expect, type Locator } from "@playwright/test";

/**
 * 共有の Spinner（`span.pr-spinner` の中の `svg.animate-spin`）が、回転しても揺れないことを測る（#1180）。
 *
 * - 回転を角度ごとに止めて（Web Animations の `currentTime`）スピナーの周りを撮り、地との差で重み付けした
 *   **見た目の重心** が角度によらず動かないこと（旧形の 270 度の 1 本のアークは 16px で 1.8px 動いた。
 *   180 度対称の 2 本のアークはアンチエイリアスの誤差の 0.2px 以下）。
 * - 回転しない箱（`span.pr-spinner`）と、それを置いた行（親要素）の位置・高さが角度によらず変わらないこと。
 *
 * 撮った画像は、ブラウザの canvas で画素を読む（依存を増やさない）。終わったら回転を再開する。
 */
export async function expectSpinnerStable(spinner: Locator, { angles = 12, tolerance = 0.5 } = {}) {
  await expect(spinner).toBeVisible();
  await expect(spinner).toHaveClass(/(?:^|\s)pr-spinner(?:\s|$)/);
  const page = spinner.page();
  const frames: Array<{ cx: number; cy: number; box: number[]; row: number[] }> = [];
  for (let step = 0; step < angles; step += 1) {
    const rects = await spinner.evaluate(
      (element, { step, angles }) => {
        const svg = element.querySelector("svg.animate-spin");
        const animations = svg?.getAnimations() ?? [];
        if (animations.length === 0) throw new Error("スピナーが回っていません");
        for (const animation of animations) {
          animation.pause();
          // 1 周（1s）を等分した角度で止める。
          animation.currentTime = (1000 * step) / angles;
        }
        const box = element.getBoundingClientRect();
        const row = (element.parentElement as HTMLElement).getBoundingClientRect();
        return {
          box: [box.x, box.y, box.width, box.height],
          row: [row.x, row.y, row.width, row.height],
        };
      },
      { step, angles },
    );
    const [x, y, width, height] = rects.box;
    const margin = 2;
    const clip = {
      x: Math.floor(x) - margin,
      y: Math.floor(y) - margin,
      width: Math.ceil(width) + margin * 2,
      height: Math.ceil(height) + margin * 2,
    };
    const png = await page.screenshot({ clip });
    const centroid = await page.evaluate(async ({ base64, cssWidth }) => {
      const image = new Image();
      image.src = `data:image/png;base64,${base64}`;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("canvas を使えません");
      context.drawImage(image, 0, 0);
      const { data, width: w, height: h } = context.getImageData(0, 0, canvas.width, canvas.height);
      // 地は左上の画素（スピナーの外接円の外）。
      const [r0, g0, b0] = [data[0], data[1], data[2]];
      let sum = 0;
      let sx = 0;
      let sy = 0;
      for (let py = 0; py < h; py += 1) {
        for (let px = 0; px < w; px += 1) {
          const i = (py * w + px) * 4;
          const weight = Math.abs(data[i] - r0) + Math.abs(data[i + 1] - g0) + Math.abs(data[i + 2] - b0);
          sum += weight;
          sx += weight * (px + 0.5);
          sy += weight * (py + 0.5);
        }
      }
      if (sum === 0) throw new Error("スピナーが写っていません");
      // 画素 → CSS px
      const ratio = w / cssWidth;
      return { cx: sx / sum / ratio, cy: sy / sum / ratio };
    }, { base64: png.toString("base64"), cssWidth: clip.width });
    frames.push({ cx: clip.x + centroid.cx, cy: clip.y + centroid.cy, ...rects });
  }
  await spinner.evaluate((element) => {
    for (const animation of element.querySelector("svg.animate-spin")?.getAnimations() ?? []) animation.play();
  });

  const range = (values: number[]) => Math.max(...values) - Math.min(...values);
  // 見た目の重心が角度によらず動かない（上下・左右とも）。
  expect(range(frames.map((frame) => frame.cy)), "見た目の重心の上下のずれ（px）").toBeLessThan(tolerance);
  expect(range(frames.map((frame) => frame.cx)), "見た目の重心の左右のずれ（px）").toBeLessThan(tolerance);
  // 回転しない箱と、それを置いた行の位置・寸法が変わらない。
  for (let index = 0; index < 4; index += 1) {
    expect(range(frames.map((frame) => frame.box[index])), `箱の寸法・位置 ${index}`).toBeLessThan(0.01);
    expect(range(frames.map((frame) => frame.row[index])), `行の寸法・位置 ${index}`).toBeLessThan(0.01);
  }
  // 箱は正方形のまま（回転した外接矩形にならない）。
  const [, , boxWidth, boxHeight] = frames[0].box;
  expect(boxWidth).toBe(boxHeight);
}
