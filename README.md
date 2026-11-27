# 美团闪购竞对采集与比价（meituanjingdui · 合规精简版）

通过本机已登录常规 Chrome 的美团外卖 H5 门店页，在单账号低频礼貌配速下读取门店商品与 SKU 数据，并按锁定的 27 列单表规范导出 `{MMDD}{店铺名称}.xlsx` 比价表。

## 快速开始

1. 在常规 Chrome（调试端口 `9222`）手动登录美团外卖 H5 并打开目标门店页，核验门店身份：

```bash
python3 -m pip install websockets openpyxl
python3 scripts/crawl_h5_store.py --port 9222 --inspect-only
```

2. 启动单账号保守慢速采集（默认 `68~88s` 间隔、`≤38次/小时`、每满 30 次休眠 `10 分钟`，单日软上限 `92 / 85`）：

```bash
python3 scripts/crawl_h5_store.py \
  --port 9222 \
  --poi-id-str "<门店 ID>" \
  --poi-name "<门店名称>" \
  --out-dir "./crawl_data/store" \
  --excel-out-dir "."
```

## 脚本清单

- `scripts/crawl_h5_store.py`：核验门店、纯休眠慢速读取分类与商品、保存断点并增量导出 27 列 Excel（遇验证页面退 `42`、遇 `403` 退 `43`、到单日软上限退 `44` 立即停机）。
- `scripts/export_competitor_excel.py`：从 `spus.jsonl` 和 `categories.json` 导出锁定的 27 列单表比价工作簿（含 `unify_price` v5 真实折扣价与到手价解析）。
- `scripts/fetch_spu_details.py`：按指定 SPU ID 或月销排名低频提取轮播副图、详情长图与标准属性表。
