# 美团闪购竞对采集与比价（meituanjingdui）

通过已登录的 Chrome 美团外卖 H5 门店页，采集闪购、超市便利和闪电仓的商品，按 SKU 展开为固定 27 列的 Excel 比价表。支持 macOS、Windows 和 Linux。

## 快速开始

在技能目录运行以下命令安装依赖，再按技能说明启动 Chrome，在美团外卖 H5 页面登录并打开目标门店。[各系统启动方式](SKILL.md#打开目标门店)

```bash
python3 -m pip install websockets openpyxl
python3 scripts/crawl_h5_store.py --port 9222 --inspect-only
```

核对门店名称、地址和 ID 后开始采集。每家分店使用独立的输出目录。

```bash
python3 scripts/crawl_h5_store.py \
  --port 9222 \
  --poi-id-str "<门店 ID>" \
  --poi-name "<门店名称>" \
  --out-dir "./crawl_data/store" \
  --excel-out-dir "."
```

生成的 `{MMDD}{店铺名称}.xlsx` 含一张 `全量商品竞对明细` 工作表。可按条码和规格比对原价、直接折扣价、到手价、销量和实时库存。采集中断后，重新运行同一命令可接着处理已保存的数据。

## 脚本

- [门店采集](scripts/crawl_h5_store.py)核验分店、采集商品与分类、保存进度并更新比价表。
- [Excel 导出](scripts/export_competitor_excel.py)可从已有的 `spus.jsonl` 和 `categories.json` 独立重新生成比价表。
- [单品详情](scripts/fetch_spu_details.py)支持按 SPU ID 或月销排名选择商品，导出图片和商品属性，并可下载图片。

完整命令、字段映射与表头见[技能说明](SKILL.md)。
