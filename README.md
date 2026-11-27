# 美团闪购竞对采集、比价与货盘分析（meituanjingdui · 合规省力版）

通过本机已登录常规 Chrome 的美团外卖 H5 门店页，在单账号低频礼貌配速下读取门店商品与 SKU 数据，自动导出锁定的 27 列 `{MMDD}{店铺名称}.xlsx` 比价表与 `{MMDD}{店铺名称}_数据分析报告.md` 货盘分析报告。

## 核心省力特性

- **一行命令启动**：自动识别 `9222` 当前门店并创建 `./crawl_data/{店铺名称}` 断点目录，无需手填繁琐参数。
- **老号 / 新号双档限速（`--account-tier veteran|conservative`）**：老号默认 `68~88s` / `≤38次/小时` / `30次休10分钟` / 24h 软上限 `92/85`；新号传 `conservative` 自动下调配额防受限。
- **跨零点与重载防坑**：滚动 24 小时配额记账防零点清零超发；固定初始 `MMDD` 防跨零点分裂成两份 Excel；页面重载后核验同店名+同地址自动适配新加密 `poi_id_str`。
- **大店单号智能配额规划**：3,000~4,000 品大店自动先用剩余二级分类 `p=0` 分担大类，保留瀑布流配额收齐无子分类尾数。
- **离线数据分析与 69 码同品对标**：自动产出价格带、折扣锁利、分类贡献与 Top 30 爆品分析报告，支持与本店商品表做离线 69 码比价与缺品选品。

## 快速使用

```bash
python3 -m pip install websockets openpyxl

# 1. 在 9222 常规 Chrome 登录并打开目标门店页后，一键启动采集 + 导出 27 列 Excel + 生成分析报告
python3 scripts/crawl_h5_store.py --port 9222 --excel-out-dir .

# 2. 离线与本店商品表做 69 码同品比价与热销缺品筛选
python3 scripts/analyze_competitor_store.py --excel "./1126快趣逛超市龙文店.xlsx" --our-excel "./本店商品导出.xlsx"
```

## 脚本清单

- `scripts/crawl_h5_store.py`：门店核验、单账号低频礼貌采集、智能配额规划与增量导出（遇验证退 `42`、遇 `403` 退 `43`、到 24h 软上限退 `44` 立即停机）。
- `scripts/export_competitor_excel.py`：锁定 27 列单表规范导出（完整兼容 `unify_price` v5 真实直接折扣价与子规格独立到手价）。
- `scripts/analyze_competitor_store.py`：纯离线生成门店货盘与定价策略 Markdown 报告，以及可选的 69 码跨店比价/缺品选品表。
- `scripts/fetch_spu_details.py`：按 SPU ID 或月销排名低频提取新品副图、详情长图与标准属性参数表。
