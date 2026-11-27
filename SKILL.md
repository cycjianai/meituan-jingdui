---
name: meituanjingdui
description: 通过用户本机已登录常规 Chrome 的美团外卖 H5 门店页（h5.waimai.meituan.com -> cactivityapi-sc.waimai.meituan.com），以单账号低频礼貌配速采集闪购/超市便利/闪电仓门店商品与 SKU 数据，并严格按锁定的 27 列单表规范（完整兼容 unify_price v5 真实折扣价、独立神价到手价、69 条码、月销、点赞、实时库存、多规格展开）导出 `{MMDD}{店铺名称}.xlsx`，同时支持按需提取新品副图、详情长图与属性参数表。
---

# 美团闪购竞对采集与比价（合规精简版）

## 核心原则与合规边界

1. **仅依赖用户手动登录的单账号浏览器会话**：用户在常规 Chrome（默认调试端口 `9222`）自行完成登录并进入目标门店页后，再运行采集脚本。
2. **严守平台速率与单号配额**：脚本默认锁定保守慢速等待（单次纯休眠 `68~88 秒`、每小时上限 `≤38 次`、每满 `30 次` 纯休眠 `10 分钟`、单日软上限 `sputag≤92` / `smooth≤85` 提前退出码 `44` 停机），**绝不注入任何模拟触摸/鼠标轨迹、绝不擦除或改写 Cookie/设备指纹、绝不执行多账号轮换**。
3. **受阻立即停机**：
   - 遇到人机验证跳转（`verify.meituan.com` / `418`）：立即落盘当前数据与 Excel 并以退出码 `42` 停止，报告用户，不在后台循环挂起等待或绕过验证；
   - 遇到访问限制（`HTTP 403 / 406`）：立即落盘当前数据与 Excel 并以退出码 `43` 停止，不轮询重试、不换号突破限制。
4. **27 列导出规范焊死不可变**：导出竞对总表必须且只能调用 `scripts/export_competitor_excel.py`。

---

## 1. 启动与门店核验

确认依赖：

```bash
python3 -c "import websockets, openpyxl; print('DEPS_OK')" || python3 -m pip install websockets openpyxl
```

如常规 Chrome 尚未开启远程调试端口 `9222`，启动常规 Chrome 并手动打开 [美团外卖 H5 首页](https://h5.waimai.meituan.com/waimai/mindex/home) 登录、定位并点进目标门店页：

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome_meituan_9222" &
```

核对当前打开的门店名称、地址与 `poi_id_str`：

```bash
python3 scripts/crawl_h5_store.py --port 9222 --inspect-only
```

---

## 2. 单账号低频采集与断点续传

每家门店使用独立的数据目录（默认已锁定保守慢速参数）：

```bash
python3 scripts/crawl_h5_store.py \
  --port 9222 \
  --poi-id-str "<核验后的poi_id_str>" \
  --poi-name "<核验后的目标店名>" \
  --out-dir "./crawl_data/store_a" \
  --excel-out-dir "." \
  --min-delay 68 --max-delay 88 --hourly-cap 38 \
  --wave-size 30 --wave-rest-sec 600 \
  --max-sputag-daily 92 --max-smooth-daily 85
```

- 状态持久化在 `<out-dir>/spus.jsonl`、`categories.json`、`timestamps.json`、`daily_quota.json`、`progress.json`，每 3 次请求自动调用 `scripts/export_competitor_excel.py` 增量刷新 `{MMDD}{店铺名称}.xlsx`（跨零点任务可加 `--date-mmdd MMDD` 固定文件名日期）。
- 退出码说明：`0` 采集完成；`42` 遇到验证页面立即停机；`43` 遇到 `403/406` 访问限制立即停机；`44` 达到单日安全软上限优雅停机。

---

## 3. 27 列硬锁定 Excel 导出规范与 9 大字段防坑映射

独立重导命令：

```bash
python3 scripts/export_competitor_excel.py \
  --spus-jsonl "./crawl_data/store_a/spus.jsonl" \
  --cat-json "./crawl_data/store_a/categories.json" \
  --store-name "<门店名称>" \
  --out-dir "."
```

1. **文件命名**：严格按 `{MMDD}{店铺名称}.xlsx` 自动命名（剥离括号与空格）。
2. **单工作表**：仅允许 1 张工作表 `全量商品竞对明细`，多规格子项逐行展开并浅黄底高亮。
3. **`规格类型` 枚举**：第 5 列仅允许 `单规格` 或 `多规格`。
4. **导出后必核 `[Self-Check]`**：确认 `折扣价<原价`、`神价/到手价`、`69条码`、`月销>0` 正常（详见 [商品字段对账样例](references/product_fields.md)）。

| 列号 | 标准列名 | 业务说明 | 列号 | 标准列名 | 业务说明 |
| :---: | :--- | :--- | :---: | :--- | :--- |
| 1 | `SPU_ID` | 美团主商品 ID | 15 | `点赞数` | SPU 聚合好评点赞数 |
| 2 | `SKU_ID` | 子规格唯一 ID | 16 | `实时库存` | SKU 独立实时库存 |
| 3 | `UPC条形码(69码)` | 跨店 100% 精准同品匹配依据 | 17 | `原价(元)` | SKU 独立划线原价 |
| 4 | `商品名称` | 完整商品标题 | 18 | `直接折扣价(元)` | **SKU 独立真实直接折扣价** |
| 5 | `规格类型` | 仅 `单规格` / `多规格` | 19 | `神价/到手价(元)` | SKU 独立券后/神价到手价 |
| 6 | `子规格名称(spec)` | 区分多规格子项（含 `combine_spec` 兜底） | 20 | `到手价说明` | 到手价标签说明 |
| 7 | `店内一级分类` | 优先归属真实货架实体分类 | 21 | `竞对有效基准价(元)` | 剔除 $\le 0.1$ 元单件霸王餐后的最低有效价 |
| 8 | `店内二级分类` | 还原竞对细分货架分类 | 22 | `促销标签` | 如 `7折 限1份` |
| 9 | `店内分类排名` | 该商品在店内分类的排序位置 | 23 | `满减商品券` | 品类券/满减券标签 |
| 10 | `美团一级类目` | 美团官方标准一级类目 | 24 | `限购份数` | 单笔限购份数（含 `quota_per_order`） |
| 11 | `美团二级类目` | 美团官方标准二级类目 | 25 | `最小起购数` | 最小起购件数 |
| 12 | `美团三级类目名称` | 美团官方标准三级类目 | 26 | `售卖属性(温度/提示)` | 如 `温度:常温,冰镇` |
| 13 | `月销量(数值)` | SPU 聚合月售（解析自文本） | 27 | `高清无水印主图URL` | 去水印/裁剪/`.webp` 的原图 URL |
| 14 | `想买人数` | SPU 聚合想买人数（无空值） | — | — | — |

---

## 4. 二阶段按需能力：单品轮播副图、详情长图与属性参数表

```bash
python3 scripts/fetch_spu_details.py \
  --port 9222 \
  --spus-jsonl "./crawl_data/store_a/spus.jsonl" \
  --top-n-sales 30 \
  --out-jsonl "./crawl_data/store_a/spu_details.jsonl" \
  --out-xlsx "./新品建品详情与图片组装表.xlsx"
```
