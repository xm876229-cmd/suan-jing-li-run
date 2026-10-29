# SKU→商品 映射主表（累积）

这个目录存的是「SKU ID → 商品ID」的累积映射，由 `scripts/import_mapping.py` 生成，不用手工维护。

| 文件 | 作用 |
| --- | --- |
| `master.csv` | 累积主表，程序算账时优先用它 |
| `history.jsonl` | 变更流水：新增 SKU、SKU 改绑商品都有记录 |

## 为什么映射要累积

平台每次取到的 SKU 列表只反映**当前**的链接状态。链接一编辑，SKU 归属就可能变，一天变一次也正常。如果只用最新那份：

- 某个老 SKU 从列表里消失 → 历史订单里那些行找不到商品ID → 整片归进「未识别商品」，净利凭空少算
- 某个 SKU 改绑到新商品 → 没有任何记录能说明"这个商品的 9 月数据其实属于旧链接"

所以每次取数产生的 `sku_list_*.json` 都会增量合并进主表：新 SKU 追加、商品ID 变了才更新并记一条 `rebind` 流水、老 SKU 永远保留。

主表会和导出目录里的映射表**合并使用**（主表优先），任一方不完整都不会算错。

## 什么时候需要手动跑

正常情况下不用：`src/cli.py` 发现 `_inbox` 里有比主表更新的 `sku_list_*.json` 会自动合并。

想手动合并时：

```bash
python -m scripts.import_mapping                          # 合并全部 sku_list_*.json
python -m scripts.import_mapping "data/exports/_inbox/sku_list_2026-10-01_2026-10-16.json"
```

## 数据去向

这一目录下的文件**不进版本库**（已 gitignore）。
