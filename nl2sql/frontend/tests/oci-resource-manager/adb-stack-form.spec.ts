import { expect, test } from "@playwright/test";

const formUrl = process.env.OCI_RESOURCE_MANAGER_ADB_FORM_URL;

const EVERYWHERE_ACCESS = "すべての場所からのセキュア・アクセス";
const ALLOWED_ACCESS = "許可されたIPおよびVCN限定のセキュア・アクセス";
const PRIVATE_ACCESS = "プライベート・エンドポイント・アクセスのみ";
const IP_OR_CIDR = "IPアドレスまたはCIDRブロック";
const CREATE_NEW_MODE = "新規 Autonomous AI Database の作成";
const USE_EXISTING_MODE = "既存の Autonomous AI Database を選択";
const SSH_KEY_TITLE = "SSHキーの追加";
const SSH_KEY_AUTO_GENERATE = "キー・ペアを自動で生成";
const DOWNLOAD_PRIVATE_KEY = "秘密キーのダウンロード";
const DOWNLOAD_PUBLIC_KEY = "公開キーのダウンロード";
const NETWORK_ACCESS_DESCRIPTION =
  "Autonomous AI Databaseのネットワーク・アクセスを選択します。すべての場所からのセキュア・アクセスはパブリック・エンドポイントを使用し、許可されたIPおよびVCN限定のセキュア・アクセスはACLで接続元を制限し、プライベート・エンドポイント・アクセスのみは指定したVCN内のプライベート・エンドポイントを使用します。";
const PRIVATE_ENDPOINT_VCN_COMPARTMENT_DESCRIPTION =
  "プライベート・エンドポイントで使用するVCNが存在するコンパートメントを選択します。";
const PRIVATE_ENDPOINT_VCN_DESCRIPTION =
  "Autonomous AI Databaseのプライベート・エンドポイントで使用するVCNを選択します。";
const PRIVATE_ENDPOINT_SUBNET_COMPARTMENT_DESCRIPTION =
  "Autonomous AI Databaseをアタッチするサブネットが存在するコンパートメントを選択します。";
const PRIVATE_ENDPOINT_SUBNET_DESCRIPTION =
  "Autonomous AI Databaseのプライベート・エンドポイントをアタッチするサブネットを選択します。";
const ACL_NOTATION_DESCRIPTION =
  "ACLで許可する接続元を、VCNまたはIPアドレス/CIDRブロックから選択します。";
const ACL_VCN_DESCRIPTION =
  "Service Gateway経由でAutonomous AI Databaseへのアクセスを許可するVCNを選択します。";
const ACL_CIDR_DESCRIPTION =
  "パブリック・インターネットからの接続を許可するクライアントのパブリックIPアドレスまたはパブリックCIDRブロックをカンマ区切りで入力します。";

test.describe("OCI Resource Manager ADB作成フォーム", () => {
  test.skip(
    !formUrl,
    "OCI_RESOURCE_MANAGER_ADB_FORM_URLを指定した認証済みテスト・テナンシで実行します。",
  );

  test("ADBの選択順、現在のコンパートメント既定値、ネットワーク項目の段階表示が契約に一致する", async ({
    page,
  }) => {
    await page.goto(formUrl!, { waitUntil: "domcontentloaded" });

    for (const hiddenGitField of [
      "Application Git URL",
      "Application Git ref",
      "Platform Git URL",
      "Platform Git ref",
    ]) {
      await expect(page.getByLabel(hiddenGitField, { exact: true })).toBeHidden();
    }
    await expect(page.getByLabel("Application port", { exact: true })).toBeVisible();

    const deploymentCompartment = page.getByLabel("Create in compartment", { exact: true });
    const adbCompartment = page.getByLabel("ADBのコンパートメント", { exact: true });
    const deploymentMode = page.getByLabel("ADBの利用方法", { exact: true });
    await expect(deploymentCompartment).toBeVisible();
    await expect(adbCompartment).toBeVisible();
    await expect(adbCompartment).toBeEnabled();
    await expect(deploymentMode).toBeVisible();
    await expect(deploymentCompartment).toHaveValue(/\S+/);
    await expect(adbCompartment).toHaveValue(await deploymentCompartment.inputValue());
    await expect(deploymentMode).toHaveValue(CREATE_NEW_MODE);

    const [compartmentBox, deploymentModeBox] = await Promise.all([
      adbCompartment.boundingBox(),
      deploymentMode.boundingBox(),
    ]);
    expect(compartmentBox).not.toBeNull();
    expect(deploymentModeBox).not.toBeNull();
    expect(compartmentBox!.y).toBeLessThan(deploymentModeBox!.y);

    const networkSection = page.getByText("ネットワーク・アクセス", { exact: true });
    // 統合 stack（#217）では NL2SQL の入力グループは NL2SQL を配備するときだけ表示される（既定は配備する）。
    const deepsecSection = page.getByText("NL2SQL Deep Data Security", { exact: true });
    const computeSection = page.getByText("Compute", { exact: true });
    await expect(networkSection).toBeVisible();
    await expect(deepsecSection).toBeVisible();
    await expect(computeSection).toBeVisible();
    await expect(page.getByText(SSH_KEY_TITLE, { exact: true })).toBeVisible();
    await expect(page.getByText(SSH_KEY_AUTO_GENERATE, { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: DOWNLOAD_PRIVATE_KEY })).toBeVisible();
    await expect(page.getByRole("button", { name: DOWNLOAD_PUBLIC_KEY })).toBeVisible();

    const [networkSectionBox, deepsecSectionBox, computeSectionBox] = await Promise.all([
      networkSection.boundingBox(),
      deepsecSection.boundingBox(),
      computeSection.boundingBox(),
    ]);
    expect(networkSectionBox).not.toBeNull();
    expect(deepsecSectionBox).not.toBeNull();
    expect(computeSectionBox).not.toBeNull();
    expect(networkSectionBox!.y).toBeLessThan(deepsecSectionBox!.y);
    expect(deepsecSectionBox!.y).toBeLessThan(computeSectionBox!.y);

    // ADB は3製品で共有するため、既定の workload は OLTP。
    const workload = page.getByLabel("Workload type", { exact: true });
    await expect(workload).toHaveValue("OLTP");
    await expect(workload.locator("option")).toHaveText(["OLTP", "AJD", "APEX", "LH"]);

    const accessType = page.getByLabel("アクセス・タイプ", { exact: true });
    await expect(accessType).toHaveValue(PRIVATE_ACCESS);
    await expect(accessType.locator("option")).toHaveText([
      EVERYWHERE_ACCESS,
      ALLOWED_ACCESS,
      PRIVATE_ACCESS,
    ]);
    await expect(page.getByText(NETWORK_ACCESS_DESCRIPTION, { exact: true })).toBeVisible();

    await expect(page.getByLabel("VCNのコンパートメント", { exact: true }).first()).toBeVisible();
    await expect(page.getByLabel("仮想クラウド・ネットワーク", { exact: true })).toBeVisible();
    await expect(page.getByLabel("サブネットのコンパートメント", { exact: true }).first()).toBeVisible();
    await expect(page.getByLabel("サブネット", { exact: true })).toBeVisible();
    await expect(
      page.getByText(PRIVATE_ENDPOINT_VCN_COMPARTMENT_DESCRIPTION, { exact: true }),
    ).toBeVisible();
    await expect(page.getByText(PRIVATE_ENDPOINT_VCN_DESCRIPTION, { exact: true })).toBeVisible();
    await expect(
      page.getByText(PRIVATE_ENDPOINT_SUBNET_COMPARTMENT_DESCRIPTION, { exact: true }),
    ).toBeVisible();
    await expect(page.getByText(PRIVATE_ENDPOINT_SUBNET_DESCRIPTION, { exact: true })).toBeVisible();
    await expect(page.getByLabel("相互TLS (mTLS)認証が必要", { exact: true })).toBeHidden();

    await deploymentMode.selectOption({ label: USE_EXISTING_MODE });
    await expect(page.getByLabel("既存のAutonomous AI Database", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Existing DB wallet password", { exact: true })).toBeHidden();
    await expect(workload).toBeHidden();
    await expect(networkSection).toBeHidden();

    await deploymentMode.focus();
    await deploymentMode.press("Home");
    await expect(deploymentMode).toHaveValue(CREATE_NEW_MODE);
    await expect(workload).toBeVisible();
    await expect(networkSection).toBeVisible();

    await accessType.selectOption({ label: EVERYWHERE_ACCESS });
    await expect(page.getByLabel("仮想クラウド・ネットワーク", { exact: true })).toBeHidden();
    await expect(page.getByLabel("IP表記法タイプ", { exact: true })).toBeHidden();

    await accessType.selectOption({ label: ALLOWED_ACCESS });
    const notationType = page.getByLabel("IP表記法タイプ", { exact: true });
    await expect(notationType).toBeVisible();
    await expect(notationType).toHaveValue("VCN");
    await expect(page.getByText(ACL_NOTATION_DESCRIPTION, { exact: true })).toBeVisible();
    await expect(page.getByLabel("許可する仮想クラウド・ネットワーク", { exact: true })).toBeVisible();
    await expect(page.getByText(ACL_VCN_DESCRIPTION, { exact: true })).toBeVisible();

    await notationType.selectOption({ label: IP_OR_CIDR });
    await expect(
      page.getByLabel("許可するIPアドレスまたはCIDRブロック", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText(ACL_CIDR_DESCRIPTION, { exact: true })).toBeVisible();
    await expect(
      page.getByLabel("許可する仮想クラウド・ネットワーク", { exact: true }),
    ).toBeHidden();

    await accessType.focus();
    await accessType.press("End");
    await expect(accessType).toHaveValue(PRIVATE_ACCESS);
    await expect(page.getByLabel("サブネット", { exact: true })).toBeVisible();

    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(hasHorizontalOverflow).toBe(false);
  });
});
