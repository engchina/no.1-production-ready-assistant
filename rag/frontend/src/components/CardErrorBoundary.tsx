import { Component, type ReactNode } from "react";
import { RefreshCw } from "lucide-react";

import {
  Banner,
  logBrowserDiagnostic,
  Button,
} from "@production-ready/ui";
import { t } from "@/lib/i18n";

interface CardErrorBoundaryProps {
  /** 隔離対象。描画中に throw してもこの範囲だけがエラー表示へ差し替わる。 */
  children: ReactNode;
  /** エラー表示に出すカード名（例: 「システムテーブル」）。省略時は汎用文言。 */
  label?: string;
}

interface CardErrorBoundaryState {
  error: Error | null;
}

/**
 * カード単位の error boundary。
 *
 * React は boundary が無いと、throw したコンポーネントの祖先をルートまで unmount する。
 * 設定ページは独立した API と責務を持つカードを兄弟として並べているため、boundary が無いと
 * 1 枚の描画例外でページ全体が空になる（実例: SystemTablesCard の例外で ADB 管理・Wallet が
 * まとめて消えた #63）。カードごとに失敗を閉じ込め、他のカードの描画を継続させる。
 *
 * 対象は **描画中の例外**。API エラーは各カードが従来どおり自前のエラー表示で扱う。
 */
export class CardErrorBoundary extends Component<
  CardErrorBoundaryProps,
  CardErrorBoundaryState
> {
  state: CardErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): CardErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error): void {
    logBrowserDiagnostic("ERROR", { event: "card_render_failed", serviceName: "production-ready-rag", error });
  }

  private handleReset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    const { label } = this.props;
    return (
      <Banner
        severity="danger"
        title={
          label
            ? t("common.cardError.titleWithLabel", { label })
            : t("common.cardError.title")
        }
        action={
          <Button variant="secondary" size="sm" onClick={this.handleReset} icon={RefreshCw}>
            {t("common.retry")}
          </Button>
        }
      >
        {t("common.cardError.description")}
      </Banner>
    );
  }
}
