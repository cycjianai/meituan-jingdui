---
name: meituanjingdui
description: 通过已登录的 Chrome 美团外卖 H5 门店页采集闪购、超市便利或闪电仓的商品与 SKU 数据，按固定 27 列导出竞对比价表。适用于核对价格、销量、库存、条码和多规格商品，也支持按需提取单品图片与属性。
---

# 美团闪购竞对采集与比价

## 打开目标门店

在技能目录安装 Python 依赖。Windows 上可将命令中的 `python3` 替换为 `python`。在 Windows CMD 中，将下文以反斜杠续行的命令合并为一行执行。

```bash
python3 -m pip install websockets openpyxl
```

脚本连接本机 Chrome 的远程调试端口，默认为 `9222`。如果已有可连接的 Chrome，直接在该窗口打开门店。需要启动窗口时，按操作系统执行对应命令。

**macOS**

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome_meituan_9222" &
```

**Windows（CMD）**

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\.chrome_meituan_9222"
```

**Linux**

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome_meituan_9222" &
```

在该窗口打开[美团外卖 H5 首页](https://h5.waimai.meituan.com/waimai/mindex/home)，通过页面登录，选择城市并进入目标分店。运行以下命令，核对返回的 `poi_name`、`address` 和 `poi_id_str`。

```bash
python3 scripts/crawl_h5_store.py --port 9222 --inspect-only
```

## 采集门店商品

每家分店使用独立的数据目录。将示例中的门店 ID 和名称替换为上一步核对的值。

```bash
python3 scripts/crawl_h5_store.py \
  --port 9222 \
  --poi-id-str "<门店 ID>" \
  --poi-name "<门店名称>" \
  --out-dir "./crawl_data/store_a" \
  --excel-out-dir "."
```

采集脚本按 SPU ID 去重，将商品、分类和进度分别写入 `spus.jsonl`、`categories.json` 和 `progress.json`。采集中会更新工作簿。页面请求出错时，脚本结束并保存已取得的商品。使用同一门店和数据目录重新运行可继续采集。

## 导出 27 列比价表

已有采集文件时，可直接运行导出脚本。

```bash
python3 scripts/export_competitor_excel.py \
  --spus-jsonl "./crawl_data/store_a/spus.jsonl" \
  --cat-json "./crawl_data/store_a/categories.json" \
  --store-name "<门店名称>" \
  --out-dir "."
```

工作簿命名为 `{MMDD}{店铺名称}.xlsx`，例如 `1124满佳喜超市长清店.xlsx`。唯一工作表为 `全量商品竞对明细`，按 `LOCKED_27_HEADERS` 固定列名与顺序。每个 SKU 占一行，规格类型取 `单规格` 或 `多规格`，同一多规格商品的各行相邻并以浅黄色标出。

### 商品字段映射

导出后核对终端的 `[Self-Check]` 统计。若有折扣商品的门店显示折扣行数为零，可从下表的 `unify_price` 字段核对原始商品记录。原版样本中有 9,065 行折扣商品曾被导成原价，例如实售 3.5 元的鞋刷导成了 4.98 元。[商品字段对账样例](references/product_fields.md)

| 序号 | 陷阱字段 | 常见错误读法（严禁！） | 正确强制映射链（已写死在代码中） |
| :---: | :--- | :--- | :--- |
| 1 | **`直接折扣价(元)`**（第 18 列） | 直接读 `skus[].price`（在 v5 下等于原价！） | `skus[].unify_price.activity_info.item_details[0].act_price` $\rightarrow$ `skus[].unify_price.activity_info.activity_price`（若旧 `skus[].price` 更低取更低值）$\rightarrow$ `skus[].price` |
| 2 | **`原价(元)`**（第 17 列） | 仅读 `skus[].origin_price` | `skus[].unify_price.underlined_price` $\rightarrow$ `activity_info.underline_price` $\rightarrow$ `skus[].origin_price` $\rightarrow$ `spu.min_price` |
| 3 | **`神价/到手价(元)` & 说明**（第 19~20 列） | 只读主商品 `spu.unify_price.actual_price_info`（多规格非最低价子规格全丢） | 优先读子规格独立 `skus[].unify_price.actual_price_info.actual_price`（及 `actual_price_text`）$\rightarrow$ `secondary_activity_price` $\rightarrow$ 单规格/最低价规格兜底 SPU 层 |
| 4 | **`限购份数` & `促销标签`**（第 24、22 列） | 直接读 `skus[].restrict`（全为 `0`）与 `skus[].promotion_info`（全为 `""`） | 限购优先 `restrict` 否则 `activity_info.quota_per_order`；标签为空且 `折扣价 < 原价` 时自动由 `(1 - discount_strength) * 10` 折 + `限N份` 合成（如 `7折 限1份`） |
| 5 | **`月销量(数值)`**（第 13 列） | 直接读 `spu.month_saled`（~95% 商品为 `0`） | `max(解析(spu.month_saled_content), spu.month_saled)`（支持 `"月售9"`、`"月售1.2万"`） |
| 6 | **`点赞数` & `想买人数`**（第 15、14 列） | 只读 `praise_num_new`（`smoothRender` 下常为 `0`）；新品 `want_to_Buy` 为 `None` | 点赞：`max(praise_num_new, praise_num, praise_count, 解析(praise_content))`；想买：`max(want_to_Buy, 解析(want_to_buy_content), 0)`，严禁写空值 |
| 7 | **`子规格名称(spec)`**（第 6 列） | 仅读 `skus[].spec`（量贩装常为空 `""`） | `skus[].spec` $\rightarrow$ `skus[].combine_spec` $\rightarrow$ `skus[].cspu_grouping_value` |
| 8 | **`店内二级分类`**（第 8 列） | 仅用 `spu.tag` 查表（`sub_idx=0` 拉取时 `spu.tag` 会变成一级分类 ID） | `spu.tag` $\rightarrow$ `skus[0].tagBelongTo` $\rightarrow$ `categories.json` 内 `sub_tags[].allSortedSpuId` 反查 $\rightarrow$ 实体一级分类名兜底 |
| 9 | **`满减商品券`、`69码`、`主图`**（第 23、3、27 列） | 只读 SPU 层或保留 `.webp` 尾缀 | 券优先 `skus[].dynamic_act_labels` 再兜底 SPU；69 码优先 `skus[].upccode` 再兜底 `supply_poi_sku.upccode`；主图优先 `skus[].picture` 再兜底 SPU，剥离 `@watermark=...`、尺寸路径与 `.webp` |

### 27 列表头

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

> **多规格数据边界说明**：`SKU_ID`、69 条码、子规格名、原价、直接折扣价、神价到手价、实时库存、限购份数、最小起购数均由批量接口按子规格独立返回；`月销量`、`想买人数`、`点赞数` 美团底层仅在 SPU 维度披露，因此同 SPU 下各子规格行保持一致。

---

## 提取单品图片与属性

从比价表选出待引进的商品后，使用详情脚本按 SPU ID 或月销量排名提取轮播图、详情长图和属性参数，并生成建品明细表。

```bash
# 方式 A：按指定 SPU_ID 列表拉取副图、详情长图与属性表，并下载分类图片包
python3 scripts/fetch_spu_details.py \
  --port 9222 \
  --spu-ids "26963575572,26963143789" \
  --spus-jsonl "./crawl_data/store_a/spus.jsonl" \
  --out-jsonl "./crawl_data/store_a/spu_details.jsonl" \
  --out-xlsx "./新品建品详情与图片组装表.xlsx" \
  --download-images-dir "./新品素材图包"

# 方式 B：按真实解析月销自动筛 Top N 热销品拉取详情与图片
python3 scripts/fetch_spu_details.py \
  --port 9222 \
  --spus-jsonl "./crawl_data/store_a/spus.jsonl" \
  --top-n-sales 30 \
  --out-jsonl "./crawl_data/store_a/spu_details.jsonl" \
  --out-xlsx "./新品建品详情与图片组装表.xlsx"
```
