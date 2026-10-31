// #372: 共有の Tooltip（アイコンだけの Button の説明）を実ブラウザで確かめる fixture。
import { Button, PageBody, PageHeader, Section } from "@production-ready/ui";
import { ChevronLeft, CircleHelp, Download, Plus, Settings, X } from "lucide-react";
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "../../src/globals.css";

function TooltipFixture() {
  const [escapes, setEscapes] = useState(0);
  const [clicks, setClicks] = useState(0);
  // 囲むモーダルの代わり: document に届いた Escape を数える（Tooltip が開いている間は届かない）。
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setEscapes((count) => count + 1);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
  const noop = () => {};
  return (
    <>
      <PageHeader
        title="Tooltip"
        actions={[
          { id: "help", kind: "utility", ariaLabel: "ヘルプ", icon: CircleHelp, onClick: noop, testId: "help" },
          { id: "create", kind: "primary", label: "新規作成", icon: Plus, onClick: noop },
        ]}
      />
      <PageBody>
        <Section title="ツールバー">
          <div className="flex flex-wrap items-center gap-2 pt-8">
            <input aria-label="検索語" className="h-8 rounded-md border border-border-control bg-surface px-2" />
            <Button
              type="button"
              size="sm"
              variant="ghost"
              iconOnly
              icon={ChevronLeft}
              aria-label="前のページ"
              tooltip="前のページ（PageUp）"
              data-testid="prev"
            />
            <Button
              type="button"
              size="sm"
              variant="ghost"
              iconOnly
              icon={X}
              aria-label="閉じる"
              data-testid="close"
              onClick={() => setClicks((count) => count + 1)}
            />
            <Button
              type="button"
              size="sm"
              variant="ghost"
              iconOnly
              icon={Settings}
              aria-label="表示設定"
              tooltip={false}
              data-testid="no-tooltip"
            />
            <Button
              type="button"
              size="sm"
              variant="secondary"
              iconOnly
              icon={Download}
              aria-label="CSV をダウンロード"
              className="ml-auto"
              data-testid="edge"
            />
          </div>
          <p className="mt-4 text-sm">
            Escape: <output data-testid="escapes">{escapes}</output> / クリック:{" "}
            <output data-testid="clicks">{clicks}</output>
          </p>
        </Section>
      </PageBody>
    </>
  );
}

createRoot(document.getElementById("root")!).render(<TooltipFixture />);
