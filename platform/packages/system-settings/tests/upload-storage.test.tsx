import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { UploadStorageSettingsPage, validateUploadStorageForm } from "../src";

describe("validateUploadStorageForm", () => {
  it("保存ディレクトリを必須にする", () => {
    expect(validateUploadStorageForm({ localStorageDir: " " })).toEqual({
      localStorageDir: "ローカル保存ディレクトリを入力してください。",
    });
    expect(validateUploadStorageForm({ localStorageDir: "/x" })).toEqual({});
  });
});

describe("UploadStorageSettingsPage", () => {
  it("読み込み中は経過時間の表示とフォームの形の Skeleton を出す（#1047）", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient()}>
          <ConfirmProvider>
            <UploadStorageSettingsPage
              api={{
                get: () => new Promise(() => undefined),
                update: async () => Promise.reject(),
              }}
            />
          </ConfirmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    expect(html).toContain('data-testid="settings-upload-storage-loading"');
    expect(html).toContain("アップロード保存先設定を読み込んでいます。");
    expect(html).toContain('data-skeleton="form"');
    expect(html).toContain('data-processing-placement="panel"');
    // OCI Object Storage の選択肢は無い（#11）
    expect(html).not.toContain("Object Storage");
  });
});
