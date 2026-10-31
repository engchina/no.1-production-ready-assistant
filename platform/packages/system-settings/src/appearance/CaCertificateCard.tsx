import {
  ButtonLink,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Disclosure,
  EmptyState,
  Skeleton,
  TimedLoadingState,
} from "@production-ready/ui";
import { Download } from "lucide-react";
import { useEffect, useState } from "react";

import type { AppearanceMessages } from "./messages";

/**
 * 1 台の Compute の配備（#1316）で Nginx が配る、自作の Root CA の証明書の場所。製品は /rag/ などの prefix の下で
 * 配信されるため、製品の base ではなくサイトの root からの絶対 path にする（platform の共有の内容）。
 */
export const CA_CERTIFICATE_PATH = "/platform/ca.crt";

const CA_CERTIFICATE_CONTENT_TYPES = ["application/x-x509-ca-cert", "application/pkix-cert"];

/**
 * CA の証明書を配っているか（HEAD）。ローカルの開発（Vite）は知らない path にも index.html（text/html）を返すため、
 * status だけでなく Content-Type で判定する。失敗は「配っていない」として扱う（画面の他の設定は止めない）。
 */
export async function probeCaCertificate(
  url: string = CA_CERTIFICATE_PATH,
  fetchImpl: typeof fetch = fetch,
): Promise<boolean> {
  try {
    const response = await fetchImpl(url, { method: "HEAD", cache: "no-store", credentials: "same-origin" });
    if (!response.ok) return false;
    const contentType = (response.headers.get("Content-Type") ?? "").toLowerCase();
    return CA_CERTIFICATE_CONTENT_TYPES.some((type) => contentType.startsWith(type));
  } catch {
    return false;
  }
}

type ProbeState = "checking" | "available" | "unavailable";

export interface CaCertificateCardProps {
  messages: AppearanceMessages;
  /** 証明書の場所（既定は /platform/ca.crt）。 */
  url?: string;
  /** 証明書を配っているかの確認（テスト用に差し替えられる）。 */
  probe?: (url: string) => Promise<boolean>;
}

/** 「HTTPS の証明書」: 自作の Root CA の証明書の取得と、端末への取り込み方（#1316）。 */
export function CaCertificateCard({ messages: m, url = CA_CERTIFICATE_PATH, probe = probeCaCertificate }: CaCertificateCardProps) {
  const [state, setState] = useState<ProbeState>("checking");

  useEffect(() => {
    let active = true;
    setState("checking");
    void probe(url).then((available) => {
      if (active) setState(available ? "available" : "unavailable");
    });
    return () => {
      active = false;
    };
  }, [probe, url]);

  const steps: Array<{ label: string; text: string }> = [
    { label: m.caCertificateStepsWindowsLabel, text: m.caCertificateStepsWindows },
    { label: m.caCertificateStepsMacLabel, text: m.caCertificateStepsMac },
    { label: m.caCertificateStepsIosLabel, text: m.caCertificateStepsIos },
    { label: m.caCertificateStepsAndroidLabel, text: m.caCertificateStepsAndroid },
    { label: m.caCertificateStepsEdgeLabel, text: m.caCertificateStepsEdge },
    { label: m.caCertificateStepsChromeLabel, text: m.caCertificateStepsChrome },
    { label: m.caCertificateStepsFirefoxLabel, text: m.caCertificateStepsFirefox },
  ];

  return (
    <Card data-testid="appearance-ca-certificate">
      <CardHeader>
        <CardTitle>{m.caCertificateTitle}</CardTitle>
        <CardDescription>{m.caCertificateDescription}</CardDescription>
      </CardHeader>
      <CardContent>
        {state === "checking" ? (
          <TimedLoadingState
            label={m.caCertificateChecking}
            operationKey="appearance-ca-certificate"
            placement="panel"
            testId="appearance-ca-certificate-loading"
          >
            <Skeleton className="h-9 w-56 max-w-full" />
          </TimedLoadingState>
        ) : state === "available" ? (
          <div className="grid gap-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <ButtonLink to={url} icon={Download} size="md" testId="appearance-ca-certificate-download">
                {m.caCertificateDownload}
              </ButtonLink>
              <p className="text-sm text-fg-muted">{m.caCertificateFileHint}</p>
            </div>
            <Disclosure summary={m.caCertificateStepsSummary} data-testid="appearance-ca-certificate-steps">
              <dl className="grid gap-3 text-sm">
                {steps.map((step) => (
                  <div key={step.label} className="grid gap-1">
                    <dt className="font-medium text-fg">{step.label}</dt>
                    <dd className="text-fg-muted">{step.text}</dd>
                  </div>
                ))}
              </dl>
              <p className="mt-3 text-sm text-fg-muted">{m.caCertificateStepsAfter}</p>
            </Disclosure>
          </div>
        ) : (
          <div data-testid="appearance-ca-certificate-unavailable">
            <EmptyState title={m.caCertificateUnavailable} hint={m.caCertificateUnavailableHint} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
