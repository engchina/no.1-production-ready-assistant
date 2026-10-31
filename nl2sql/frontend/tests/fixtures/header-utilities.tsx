import { AppShell, Button, PageBody, PageHeader, Tabs } from "@production-ready/ui";
import { RefreshCw, Upload, Plus } from "lucide-react";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import "../../src/globals.css";

function HeaderUtilities() {
  const [loading, setLoading] = useState(false);
  const [count, setCount] = useState(0);
  const [tab, setTab] = useState("columns");
  const refresh = () => { setLoading(true); setCount(value => value + 1); };
  return <AppShell sidebar={null}>
    <PageHeader wide title="テーブルの管理" actions={[
      { id: "refresh", kind: "utility", label: "最新情報を取得", icon: RefreshCw, loading, onClick: refresh },
      { id: "schema", kind: "utility", label: "DB 構造を再取得", icon: RefreshCw, disabled: loading },
      { id: "import", kind: "secondary", label: "Excel/CSV 取込(新規テーブル)", icon: Upload },
      { id: "create", kind: "primary", label: "テーブル作成", icon: Plus },
    ]} tabs={<Tabs value={tab} onChange={setTab} items={[
      { id: "columns", label: "列情報" }, { id: "ddl", label: "DDL" },
    ]} />} />
    <PageBody wide data-testid="header-body">
      <output>更新回数: {count}</output>
      <Button variant="secondary" onClick={() => setLoading(false)}>更新を完了</Button>
      <PageHeader title="補助操作のみ" actions={[
        { id: "single", kind: "utility", label: "最新情報を取得", icon: RefreshCw },
      ]} />
    </PageBody>
  </AppShell>;
}

createRoot(document.getElementById("root")!).render(<HeaderUtilities />);
