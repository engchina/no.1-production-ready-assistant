// #364: タッチ端末での ToggleChip / Switch の当たり判定（44px）と、Tabs の横スクロールのフェード・
// キーボードで選んだタブのスクロールを実ブラウザで確かめる fixture。
import { PageBody, PageHeader, Switch, TabPanel, Tabs, ToggleChip } from "@production-ready/ui";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import "../../src/globals.css";

const TAB_ITEMS = [
  { id: "overview", label: "概要" },
  { id: "tables", label: "テーブル", count: 12 },
  { id: "views", label: "ビュー" },
  { id: "columns", label: "列の定義" },
  { id: "indexes", label: "索引" },
  { id: "constraints", label: "制約" },
  { id: "grants", label: "権限" },
  { id: "history", label: "変更履歴" },
];

const FILTERS = ["すべて", "有効", "無効", "アーカイブ済み", "エラー"];

function TouchTargetsTabs() {
  const [tab, setTab] = useState("overview");
  const [value, setValue] = useState<"inherit" | "on" | "off">("inherit");
  const [filter, setFilter] = useState("すべて");
  const [enabled, setEnabled] = useState(false);
  const [notify, setNotify] = useState(true);
  const [sound, setSound] = useState(false);
  return (
    <>
      <PageHeader
        title="当たり判定とタブ"
        tabs={<Tabs items={TAB_ITEMS} value={tab} onChange={setTab} ariaLabel="表示の切り替え" />}
      />
      <PageBody>
        <TabPanel id={tab} value={tab}>
          <div className="grid gap-6">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm text-fg">再ランキング</span>
              <div className="flex flex-wrap gap-1" role="group" aria-label="再ランキング" data-testid="tri-state">
                <ToggleChip selected={value === "inherit"} onClick={() => setValue("inherit")}>
                  継承
                </ToggleChip>
                <ToggleChip selected={value === "on"} onClick={() => setValue("on")}>
                  ON
                </ToggleChip>
                <ToggleChip selected={value === "off"} onClick={() => setValue("off")}>
                  OFF
                </ToggleChip>
              </div>
            </div>
            {/* 狭い枠で折り返させ、上下の行のチップの当たり判定が互いの見た目を覆わないことを確かめる。 */}
            <div className="flex w-40 flex-wrap gap-1" role="group" aria-label="状態で絞り込む" data-testid="wrapped">
              {FILTERS.map((item) => (
                <ToggleChip key={item} selected={filter === item} onClick={() => setFilter(item)}>
                  {item}
                </ToggleChip>
              ))}
            </div>
            <div className="flex items-center justify-between gap-3">
              <span id="switch-history-label" className="text-sm text-fg">
                検索履歴を保存する
              </span>
              <Switch
                checked={enabled}
                onCheckedChange={setEnabled}
                aria-labelledby="switch-history-label"
                data-testid="switch-history"
              />
            </div>
            {/* 間隔の狭い（gap-2）スイッチの並び。当たり判定が互いの見た目を覆わないことを確かめる。 */}
            <div className="grid gap-2">
              <div className="flex items-center justify-between gap-3">
                <span id="switch-notify-label" className="text-sm text-fg">
                  完了を通知する
                </span>
                <Switch
                  checked={notify}
                  onCheckedChange={setNotify}
                  aria-labelledby="switch-notify-label"
                  data-testid="switch-notify"
                />
              </div>
              <div className="flex items-center justify-between gap-3">
                <span id="switch-sound-label" className="text-sm text-fg">
                  音を鳴らす
                </span>
                <Switch
                  checked={sound}
                  onCheckedChange={setSound}
                  aria-labelledby="switch-sound-label"
                  data-testid="switch-sound"
                />
              </div>
            </div>
            <p className="text-sm text-fg-muted">
              選択中のタブ: <output data-testid="selected-tab">{tab}</output>
            </p>
          </div>
        </TabPanel>
      </PageBody>
    </>
  );
}

createRoot(document.getElementById("root")!).render(<TouchTargetsTabs />);
