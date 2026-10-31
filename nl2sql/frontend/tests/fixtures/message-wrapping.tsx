// #899: 通知（Toast）と画面の中のメッセージ（Banner・FormStatus・結果パネル）が、語の途中で折り返さないことを
// 実ブラウザで確かめる fixture。業務プロファイルの保存の後の通知（「Oracle Profile の反映が完了しました。」）を、
// 実画面と同じ置き場所（desktop は画面の右下、375px は上端のバー。#1728）に出す。
import {
  AppShell,
  Banner,
  FormStatus,
  PageBody,
  PageHeader,
  ProcessingIndicator,
  Section,
  Toaster,
  toast,
} from "@production-ready/ui";
import { Save } from "lucide-react";
import { createRoot } from "react-dom/client";
import "../../src/globals.css";

const SYNCED = "Oracle Profile の反映が完了しました。";
const LONG_TOAST = "業務プロファイル「売上分析」を保存しました。Oracle Profile の反映はバックグラウンドで続きます。";
const BANNER_TITLE = "システムテーブルの作成に失敗しました。";
const BANNER_BODY =
  "データベースの接続情報とユーザーの権限を確認してから、もう一度実行してください。接続できない場合は、Autonomous Database が起動しているかを確認してください。";
const FORM_STATUS = "保存に失敗しました。業務プロファイルの名前が重複しています。別の名前を入力してください。";

function MessageWrapping() {
  return (
    <AppShell sidebar={null}>
      <PageHeader
        title="業務プロファイルの編集"
        back={{ label: "一覧へ戻る", onClick: () => undefined }}
        actions={[
          {
            id: "save",
            kind: "primary",
            label: "保存",
            icon: Save,
            onClick: () => toast.success(SYNCED),
          },
          {
            id: "save-long",
            kind: "secondary",
            label: "長い通知",
            onClick: () => toast.success(LONG_TOAST),
          },
        ]}
      />
      <PageBody>
        <Section title="メッセージ">
          <div className="grid gap-4">
            <Banner severity="danger" title={BANNER_TITLE}>
              {BANNER_BODY}
            </Banner>
            <FormStatus tone="danger" message={FORM_STATUS} />
            <ProcessingIndicator active label="Oracle Profile の反映を待っています" startedAt={Date.now()} />
          </div>
        </Section>
      </PageBody>
      <Toaster />
    </AppShell>
  );
}

createRoot(document.getElementById("root")!).render(<MessageWrapping />);
