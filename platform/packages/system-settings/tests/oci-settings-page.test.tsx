import { ConfirmProvider } from "@production-ready/ui";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { OciSettingsPage, type OciSettingsPageProps } from "../src";

const pending = () => new Promise<never>(() => undefined);

const api: OciSettingsPageProps["api"] = {
  getOciSettings: pending,
  getUploadStorageSettings: pending,
  updateOciSettings: pending,
  updateOciObjectStorageSettings: pending,
  readOciConfig: pending,
  testOciConfig: pending,
  readOciObjectStorageNamespace: pending,
  uploadOciPrivateKey: pending,
};

describe("OciSettingsPage", () => {
  it("読み込み中は経過時間の表示とフォームの形の Skeleton を出す（#1028）", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <ConfirmProvider>
          <OciSettingsPage api={api} />
        </ConfirmProvider>
      </MemoryRouter>,
    );
    expect(html).toContain('data-testid="settings-oci-loading"');
    expect(html).toContain("OCI 設定を読み込んでいます");
    expect(html.match(/data-skeleton="form"/g)).toHaveLength(2);
    // 読み込み中は入力欄を出さない（未設定の空のフォームと誤認させない）。
    expect(html).not.toContain('id="oci-user-ocid"');
  });
});
