// #355: 共有 Button の loading（aria-disabled でフォーカスを保つ）・PageHeader のグループの区切り・
// フォーカスの表示（outline に一本化）を実ブラウザで確かめる fixture。
import { Button, PageBody, PageHeader, Switch } from "@production-ready/ui";
import { Copy, Plus, RefreshCw, Save, Trash2, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "../../src/globals.css";

declare global {
  interface Window {
    /** 保存の処理を完了させる（テストから呼ぶ。時間に依存しないため）。 */
    __finishSave?: () => void;
  }
}

function ButtonLoadingFocus() {
  const [submits, setSubmits] = useState(0);
  const [clicks, setClicks] = useState(0);
  const [pending, setPending] = useState(false);
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    window.__finishSave = () => setPending(false);
  }, []);
  const noop = () => {};
  return (
    <>
      <PageHeader
        title="loading とフォーカス"
        actions={[
          { id: "create", kind: "primary", label: "新規作成", icon: Plus, onClick: noop },
          { id: "import", kind: "secondary", label: "取込", icon: Upload, onClick: noop },
          { id: "refresh", kind: "utility", label: "最新情報を取得", icon: RefreshCw, onClick: noop },
          { id: "purge", kind: "danger", label: "すべて削除", icon: Trash2, onClick: noop },
        ]}
      />
      <PageBody>
        <form
          aria-label="保存フォーム"
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            setSubmits((count) => count + 1);
            setPending(true);
          }}
        >
          <label className="grid gap-1 text-sm">
            名前
            <input name="name" className="h-10 rounded-md border border-border-control bg-surface px-3" />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="lg" icon={Save} loading={pending} data-testid="save">
              保存
            </Button>
            <Button
              type="button"
              size="lg"
              variant="secondary"
              icon={Copy}
              loading={pending}
              data-testid="copy"
              onClick={() => setClicks((count) => count + 1)}
            >
              コピー
            </Button>
            <Button type="button" size="lg" icon={Plus} disabled data-testid="disabled">
              保存不可
            </Button>
            <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="通知を受け取る" data-testid="switch" />
          </div>
        </form>
        <p className="mt-4 text-sm">
          送信回数: <output data-testid="submits">{submits}</output> / クリック回数:{" "}
          <output data-testid="clicks">{clicks}</output>
        </p>
      </PageBody>
    </>
  );
}

createRoot(document.getElementById("root")!).render(<ButtonLoadingFocus />);
