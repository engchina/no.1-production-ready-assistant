import { useState } from "react";
import { createRoot } from "react-dom/client";
import { Play } from "lucide-react";
import { Button, ClearActionButton, ExecutionConfirmationField } from "@production-ready/ui";
import { t } from "../../src/lib/i18n";
import "../../src/globals.css";

function ConfirmationExample() {
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(false);
  const phrase = new URLSearchParams(location.search).get("phrase") ?? "ADMIN_EXECUTE";
  return (
    <main className="p-3">
      <ExecutionConfirmationField
        value={value}
        onChange={setValue}
        confirmed={value.trim() === phrase}
        placeholder={phrase}
        expectedLabel={phrase}
        helper={t("nl2sql.adminSqlRunner.adminHelper")}
        disabled={loading}
        actions={
          <>
            <Button
              variant="danger"
              size="lg"
              className="w-full sm:w-auto"
              loading={loading}
              disabled={value.trim() !== phrase}
              onClick={() => setLoading(true)}
            >
              <Play aria-hidden="true" />
              {t("dbAdmin.runner.run")}
            </Button>
            <ClearActionButton
              label={t("nl2sql.action.clearSql")}
              size="lg"
              className="w-full sm:w-auto"
              disabled={loading}
              onClick={() => setValue("")}
            />
          </>
        }
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<ConfirmationExample />);
