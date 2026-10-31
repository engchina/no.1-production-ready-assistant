# 依存関係セキュリティ例外

現在、`pip-audit` で無視している脆弱性はありません。

例外を追加する場合は、対象 advisory・対象バージョン・解消できない理由・影響・対策・解除条件をこのファイルに記録し、`scripts/check-all.sh` と、毎晩の全件の検査（`.github/workflows/dependency-audit-nightly.yml` の Agent の `args`）の `--ignore-vuln` と対応させてください。

## 解除済み

- **GHSA-537c-gmf6-5ccf**（cryptography wheel 内 OpenSSL）: OCI Python SDK 2.179.0 が `cryptography<47` を要求していたため例外としていた。OCI SDK 2.185 系が修正版を許可したため、`cryptography 50.0.1` へ更新して解除（#4）。
