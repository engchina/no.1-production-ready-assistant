import type { ComponentType, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

/**
 * ナビゲーション項目（解決済みラベルを保持）。
 * パッケージは i18n を持たないため、`label` / `sidebarLabel` には各アプリで翻訳済みの文字列を渡す。
 */
export interface NavItem {
  /** 安定した一意キー（多くの場合 href をそのまま使う）。 */
  href: string;
  /** 正式名（ページタイトル / aria 用）。 */
  label: string;
  /** サイドバー表示用の短縮ラベル（未指定時は label）。 */
  sidebarLabel?: string;
  icon: LucideIcon;
}

export interface NavSection {
  /** セクションの安定キー（DOM id / 折りたたみ状態キーに使用）。 */
  key: string;
  /** セクション見出し（解決済み文字列）。 */
  title: string;
  items: NavItem[];
  /**
   * 見出しクリックでセクションを折りたたみ可能にするか（既定 true）。
   * 展開幅サイドバーでのみ作用し、icon-only 幅では無効。
   */
  collapsible?: boolean;
  /**
   * 利用者の開閉の保存値が無いとき、このセクションを閉じて出すか（既定 false = 展開）。
   * 3 製品は業務の入口（「AI 活用」）だけを展開し、ほかのセクションに true を付ける（#1563）。
   * 保存値（ui-store の `collapsedSections`）があればそちらを優先し、今のページを含むセクションは開く。
   */
  defaultCollapsed?: boolean;
}

/**
 * ルーター非依存のリンクコンポーネント型。
 * react-router の `Link`（`to` を取る）をそのまま渡せる。プレーン `<a>` を使うアプリは
 * `to` を `href` に変換する薄いラッパを渡す。
 */
export type NavLinkComponent = ComponentType<{
  to: string;
  className?: string;
  children: ReactNode;
  "aria-current"?: "page" | undefined;
  "aria-label"?: string;
  title?: string;
}>;

/** Sidebar の文言（i18n 済み）。関数は「セクション名 → aria ラベル」を生成する。 */
export interface SidebarLabels {
  /** nav 要素の aria-label。 */
  aria: string;
  expand: string;
  collapse: string;
  sectionContainsActive: string;
  sectionToggleExpand: (section: string) => string;
  sectionToggleCollapse: (section: string) => string;
}
