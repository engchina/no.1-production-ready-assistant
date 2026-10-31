import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { UploadStorageSettingsPage, validateUploadStorageForm } from "../src";

const base = {
  backend: "oci" as const,
  localStorageDir: "",
  objectStorageRegion: "us-chicago-1",
  objectStorageNamespace: "ns",
  objectStorageBucket: "originals",
};

describe("validateUploadStorageForm", () => {
  it("local は保存ディレクトリだけを必須にする", () => {
    expect(validateUploadStorageForm({ ...base, backend: "local" })).toEqual({
      localStorageDir: "ローカル保存ディレクトリを入力してください。",
    });
    expect(validateUploadStorageForm({ ...base, backend: "local", localStorageDir: "/x" })).toEqual({});
  });

  it("OCI は region / namespace / bucket を必須にし、名前の文字種を検査する（backend と同じ規則）", () => {
    expect(validateUploadStorageForm(base)).toEqual({});
    const errors = validateUploadStorageForm({
      ...base,
      objectStorageRegion: " ",
      objectStorageNamespace: "",
      objectStorageBucket: "bad bucket",
    });
    expect(Object.keys(errors).sort()).toEqual([
      "objectStorageBucket",
      "objectStorageNamespace",
      "objectStorageRegion",
    ]);
    expect(errors.objectStorageBucket).toBe("英数字、ハイフン、アンダースコア、ドットで入力してください。");
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
                test: async () => Promise.reject(),
              }}
              onOpenOciSettings={() => undefined}
            />
          </ConfirmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
    expect(html).toContain('data-testid="settings-upload-storage-loading"');
    expect(html).toContain("アップロード保存先設定を読み込んでいます。");
    expect(html).toContain('data-skeleton="form"');
    expect(html).toContain('data-processing-placement="panel"');
  });
});
