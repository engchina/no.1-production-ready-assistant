# 障害対応 Runbook

OCI Enterprise AI の抽出後に Oracle AI Database の登録件数を確認します。

```sql
select count(*) as chunk_count
from rag_chunks
where document_id = :document_id;
```

$$
recall = \frac{relevant\ hits}{expected\ citations}
$$

この式は評価レポートの recall 確認に使います。
