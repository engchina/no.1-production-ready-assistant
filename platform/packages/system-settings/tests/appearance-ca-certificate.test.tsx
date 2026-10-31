// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppearanceSettingsPage } from "../src/appearance/AppearanceSettingsPage";
import { CA_CERTIFICATE_PATH, probeCaCertificate } from "../src/appearance/CaCertificateCard";

// #1316: 1 台の Compute の HTTPS の自作の Root CA の証明書を、外観と証明書の画面から取得する。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

async function render(probe: (url: string) => Promise<boolean>) {
  await act(async () => {
    root.render(<AppearanceSettingsPage theme="light" onThemeChange={() => undefined} probeCaCertificate={probe} />);
  });
  await act(async () => {
    await Promise.resolve();
  });
}

function response(status: number, contentType: string) {
  return new Response(null, { status, headers: { "Content-Type": contentType } });
}

describe("probeCaCertificate", () => {
  it("サイトの root の /platform/ca.crt を HEAD で確かめる（製品の /rag/ などの base を付けない）", async () => {
    const fetchImpl = vi.fn(async () => response(200, "application/x-x509-ca-cert"));
    await expect(probeCaCertificate(undefined, fetchImpl as unknown as typeof fetch)).resolves.toBe(true);
    expect(CA_CERTIFICATE_PATH).toBe("/platform/ca.crt");
    expect(fetchImpl).toHaveBeenCalledWith("/platform/ca.crt", expect.objectContaining({ method: "HEAD" }));
  });

  it("application/pkix-cert も証明書として扱う", async () => {
    const fetchImpl = vi.fn(async () => response(200, "application/pkix-cert"));
    await expect(probeCaCertificate(CA_CERTIFICATE_PATH, fetchImpl as unknown as typeof fetch)).resolves.toBe(true);
  });

  it("ローカルの開発（Vite が index.html を返す）・404・通信の失敗は「配っていない」", async () => {
    for (const fetchImpl of [
      vi.fn(async () => response(200, "text/html; charset=utf-8")),
      vi.fn(async () => response(404, "text/html")),
      vi.fn(async () => {
        throw new TypeError("network");
      }),
    ]) {
      await expect(probeCaCertificate(CA_CERTIFICATE_PATH, fetchImpl as unknown as typeof fetch)).resolves.toBe(false);
    }
  });
});

describe("外観と証明書の HTTPS の証明書", () => {
  it("画面の名前は「外観と証明書」", async () => {
    await render(async () => false);
    expect(host.querySelector("h1")?.textContent).toBe("外観と証明書");
  });

  it("証明書を配っていれば、ダウンロードのリンクと取り込み方を出す", async () => {
    await render(async () => true);
    const card = host.querySelector('[data-testid="appearance-ca-certificate"]');
    expect(card?.textContent).toContain("HTTPS の証明書");
    const link = host.querySelector<HTMLAnchorElement>('[data-testid="appearance-ca-certificate-download"]');
    expect(link?.getAttribute("href")).toBe("/platform/ca.crt");
    expect(link?.textContent).toBe("CA の証明書をダウンロード");
    const steps = host.querySelector('[data-testid="appearance-ca-certificate-steps"]');
    expect(steps?.textContent).toContain("端末への取り込み方");
    // OS の見出しは使うブラウザを添え、ブラウザの項は Edge → Chrome → Firefox の順（#1662）。
    expect(Array.from(steps?.querySelectorAll("dt") ?? [], (dt) => dt.textContent)).toEqual([
      "Windows（Edge・Chrome）",
      "macOS（Safari・Chrome・Edge）",
      "iPhone / iPad（すべてのブラウザ）",
      "Android（Chrome）",
      "Edge",
      "Chrome",
      "Firefox",
    ]);
    expect(host.querySelector('[data-testid="appearance-ca-certificate-unavailable"]')).toBeNull();
  });

  it("配っていなければ、理由を説明してダウンロードを出さない", async () => {
    await render(async () => false);
    expect(host.querySelector('[data-testid="appearance-ca-certificate-unavailable"]')?.textContent).toContain(
      "この環境では HTTPS の自己署名の証明書を使っていません。",
    );
    expect(host.querySelector('[data-testid="appearance-ca-certificate-download"]')).toBeNull();
  });

  it("確かめている間は経過時間付きの読み込み中を出す", async () => {
    await render(() => new Promise<boolean>(() => undefined));
    expect(host.querySelector('[data-testid="appearance-ca-certificate-loading"]')?.getAttribute("aria-label")).toBe(
      "HTTPS の証明書を確認しています",
    );
  });
});
