/** 外観と証明書の画面の既定の文言（日本語）。製品側は `messages` prop で一部だけ上書きできる。 */
export const APPEARANCE_MESSAGES = {
  // 画面は配色テーマと、HTTPS の接続に使う証明書を扱う（#1316 で「外観」から改名）。
  title: "外観と証明書",
  subtitle: "配色テーマ（ライト / ダーク）を切り替え、HTTPS の接続に使う証明書を取得します。既定の配色はライトです。",
  themeLabel: "配色テーマ",
  themeHint: "画面全体の配色を切り替えます。「自動」は OS の設定に追従します。",
  themeLight: "ライト",
  themeDark: "ダーク",
  themeSystem: "自動（OS 設定）",
  caCertificateTitle: "HTTPS の証明書",
  caCertificateDescription:
    "この環境の HTTPS は、配備したサーバーの中で作った認証局（ルート CA）が発行した証明書を使います。CA の証明書を端末に「信頼されたルート証明機関」として取り込むと、ブラウザの警告が出なくなります。",
  caCertificateChecking: "HTTPS の証明書を確認しています",
  caCertificateDownload: "CA の証明書をダウンロード",
  caCertificateFileHint: "production-ready-root-ca.crt（CA の証明書だけで、秘密鍵は含みません）",
  caCertificateStepsSummary: "端末への取り込み方",
  caCertificateStepsWindowsLabel: "Windows（Edge・Chrome）",
  caCertificateStepsWindows:
    "ダウンロードしたファイルを開き、「証明書のインストール」→「ローカル コンピューター」→「証明書をすべて次のストアに配置する」で「信頼されたルート証明機関」を選びます。",
  caCertificateStepsMacLabel: "macOS（Safari・Chrome・Edge）",
  caCertificateStepsMac:
    "ファイルを開いてキーチェーンアクセスの「システム」に追加し、追加した証明書を開いて「信頼」の「この証明書を使用するとき」を「常に信頼」にします。",
  caCertificateStepsIosLabel: "iPhone / iPad（すべてのブラウザ）",
  caCertificateStepsIos:
    "Safari でダウンロードし、「設定 > 一般 > VPN とデバイス管理」でプロファイルをインストールしてから、「設定 > 一般 > 情報 > 証明書信頼設定」で有効にします。",
  caCertificateStepsAndroidLabel: "Android（Chrome）",
  caCertificateStepsAndroid:
    "「設定 > セキュリティ > 暗号化と認証情報 > 証明書のインストール > CA 証明書」からファイルを選びます（項目の名前は機種によって異なります）。",
  // ブラウザの項（#1662）: どの OS の証明書ストアを使うかを書く。確かめられた組み合わせだけを書く（出典は PR #1662 の本文）。
  caCertificateStepsEdgeLabel: "Edge",
  caCertificateStepsEdge: "Windows・macOS・iPhone / iPad では、上の OS への取り込みだけで使えます（ブラウザでの操作は不要です）。",
  caCertificateStepsChromeLabel: "Chrome",
  caCertificateStepsChrome:
    "Windows・macOS・iPhone / iPad・Android では、上の OS への取り込みだけで使えます（ブラウザでの操作は不要です）。Linux では、Chrome の「証明書の管理」（chrome://certificate-manager）で取り込みます。",
  caCertificateStepsFirefoxLabel: "Firefox",
  caCertificateStepsFirefox:
    "Windows・macOS・Android では、バージョン 120 以降は上の OS への取り込みを既定で使います。Linux のときや、OS の証明書を使わない設定のときは、「設定 > プライバシーとセキュリティ > 証明書を表示 > 認証局証明書 > インポート」で取り込み、「この認証局によるウェブサイトの識別を信頼する」を選びます。",
  caCertificateStepsAfter:
    "取り込んだ後は、ブラウザを開き直してください。取り込むのは、この環境の管理者から受け取った証明書であることを確かめてからにしてください。",
  caCertificateUnavailable: "この環境では HTTPS の自己署名の証明書を使っていません。",
  caCertificateUnavailableHint:
    "HTTPS を有効にして配備した環境（OCI Resource Manager の stack）では、ここから CA の証明書をダウンロードできます。",
};

export type AppearanceMessages = typeof APPEARANCE_MESSAGES;
