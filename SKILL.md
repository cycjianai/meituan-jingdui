---
name: meituanjingdui
description: 美团闪购/超市便利竞对门店全量高维数据采集与比价引擎（meituanjingdui）。通过已登录常规浏览器的美团外卖H5端（h5.waimai.meituan.com -> wx-shangou.meituan.com），引导或协助用户精准定位并打开目标竞对超市门店，在防418风控压测安全阈值下高速采集全店SPU/SKU级核心数据（含69条形码upccode、无水印高清主图、美团官方一二三级标准类目、店内分类排序、原价、直接折扣价、神价/券后到手价、满减活动券、月售销量、想买人数、点赞数、实时库存real_stock及多规格全明细），并输出标准Excel竞对总表与本方货盘引流/毛利精算建议。
---

# 美团闪购竞对全量采集与比价引擎（`meituanjingdui`）

## 1. 适用场景与触发入口

当用户提出以下任一需求时，立即激活本 Skill：
- 指定抓取某家美团闪购/超市便利/闪电仓竞对门店（如“爬一下某地的满佳喜超市/鲸速达/京东便利店/某竞对仓”）；
- 需要获取竞对门店的全量商品销量、原价、折扣价、神价到手价、69条形码（`upccode`）、高清主图、实时库存（`real_stock`）、多规格价格明细；
- 需要基于竞对门店热销品补充本方门店货盘、制定压价引流策略并提前核算保本/目标毛利率。

---

## 2. 标准交互与门店定位流程（防爬错店铁律）

### 第一步：连接用户常规 Chrome 浏览器（严禁新启自动化受控实例）
1. 默认从 `9222` 端口检查已开启远程调试的常规 Chrome 实例（`http://127.0.0.1:9222/json/list`）；若未开启则顺延或用临时目录启动常规 Chrome，**绝不启用带 `webdriver` 标识的浏览器**。
2. 连接后立即在 Browser 层执行 `Browser.setDownloadBehavior({behavior: "default"})`。
3. **关键防断点检查**：检查 `/json/list` 是否存在指向 H5 页的 `DevTools` 窗口（`devtools://...`），若有则立即通过 `/json/close/<targetId>` 关闭，防止美团 `H5guard` 触发反调试断点。

### 第二步：引导或自动定位目标竞对门店（双重核验防同名误爬）
1. 在浏览器中打开或复用 `https://h5.waimai.meituan.com/waimai/mindex/home`：
   - 若未登录，将登录页切至前台，请用户扫码或验证码登录。
2. **定位目标地址与进入门店（二选一）**：
   - **方式 A（用户手动定位打开）**：引导用户在 H5 页面顶部切换城市与收货地址（定位到目标门店附近地标），点击进入目标超市门店主页，确认进入后告知 Agent 开始抓取。
   - **方式 B（Agent 协助定位）**：通过 `/waimai/mindex/poipicker` 或页面实例 `getCurrentPages()[0].api.addrSearch({city_name, keyword})` 检索精准地标坐标（如 `大学路6900号摩玛商城`），进入目标门店。
3. **进店后强制身份核验（防止同名分店爬错）**：
   - 读取 `getCurrentPages()[0].data.poiInfo.name`、`poi_id_str` 与 `poiInfo.address`；
   - **必须与用户指定的店名（如区分 `满佳喜超市（长清店）` vs `满佳喜超市（长清大学城店）`）完全一致**，核验通过后方可启动采集。

---

## 3. 美团 H5 闪购（`wx-shangou`）风控机制与防 418 压测铁律

通过实战压力测试查明，美团 H5 闪购内嵌 Web 小程序（`reco_fe_shangchao_wxapp`）存在以下两道核心风控门禁，**必须严格按本规范规避**：

### 3.1 为什么裸调接口或小程序快刷会触发 `HTTP 418`（滑块验证）？
1. **缺少灵犀（Lingxi）分类点击埋点与行为序列异常**：
   - 真人浏览时，点击左侧分类会先上报 `_lxFirstCategoryClick(idx)`（`bid: "b_g16gkfb4"`），随后调用 `spuTagProducts` 拉取该类前 20 个商品与全量排序 ID（`allSortedSpuId`），紧接着在该分类内下滑调用 `smoothRender` 分批渲染剩余商品。
   - 若脚本连续 60 次只调 `spuTagProducts` 而不调 `_lxFirstCategoryClick`、也不在分类内调用 `smoothRender`，会在第 55~60 次请求时直接触发 `418` 滑块拦截。
2. **缺少 DOM 生物行为探针数据（`bio/info/report`）**：
   - `msp.meituan.com/fingerprint/v1/notapp/bio/info/report` 持续监听页面内的 `mousemove`、`mousedown`、`mouseup`、`click`、`scroll` 事件；连续无交互请求会被判定为脚本。
   - **注意**：在 macOS 上不要使用 CDP `Input.dispatchMouseEvent(type="mouseWheel")`，当浏览器处于后台时会被 macOS 窗口合成器挂起导致超时；**必须在 `Runtime.evaluate` 内通过 `document.dispatchEvent(new MouseEvent(...))` 和 `window.dispatchEvent(new Event('scroll'))` 注入原生 DOM 事件**。
3. **2 分钟滑动窗口请求频次上限（~28–30 次请求门禁）与 `smoothRender` 30品批次约束**：
   - 进店首屏自带约 4~5 次底层请求，此后连续请求（即使穿插 10 秒短停顿）累计达到第 **29~30 次**时会触发滑动窗口门禁返回 `418`。
   - 同时，`/quickbuy/v1/poi/product/smooth/render` 服务端硬上限为 **单次返回 30 个 SPU**（虽然前端 JS 写 `slice(0, 50)`，但服务端只返回前 30 个）。必须按 **30 个 SPU/批** 动态取未采集差集 `missing_ids[:30]`，绝不能按 50 步进导致漏采和 ID 跳跃异常。

### 3.2 极速且零封控的标准采集架构（支持一天稳定连爬多家门店）
1. **分类遍历顺序优化（先爬实体分类 `idx >= 7`，最后补齐活动分类 `0..6`）**：
   - 门店前 7 个分类通常为 `推荐`、`神价`、`活动`、`疯狂折扣` 等聚合活动区（99% 商品与实体分类重复）。
   - **先遍历 `idx >= 7` 的实体一级分类**：既能确保每个商品落入真实的店内一级分类（如 `饮料专区`、`生活用水`、`五金家装`），又能在最后扫描活动分类时通过全局 `seen_spus` 自动跳过已抓商品，减少约 25% 冗余请求。
2. **单分类“全部”视图一次拿齐 ID + 30 品/批动态差集拉取**：
   - 对每个一级分类 `idx`，取其 `全部` 子标签（`sub_idx = 0 if p._ifHasSubTag(idx) else -1`），调用 1 次 `requestSpusAndSortedIds` 即可拿到该一级分类下**全部子分类的 100% 排序商品 ID（`allSortedSpuId`）**及前 20 个商品详情，无需逐个点击二级子分类。
   - 对该分类剩余未采集的 `allSortedSpuId`，动态计算 `missing_ids = [x for x in all_ids if x not in seen_spus]`，每次取前 **30 个 SPU** 调用 `p.requestSmoothSpus(batchOf30, idx, sub_idx, c.tag, c.type, c.extra, true)`，确保 100% 连续无遗漏。
3. **自适应呼吸限速（20 请求滑动窗口清零机制）**：
   - 每次请求前注入 6 步 DOM 鼠标轨迹与滚动事件，基础间隔 `3.5 ~ 5.2 秒`；
   - 每满 **8 次请求** 小憩 `9 ~ 13 秒`；
   - 第 **18 次请求**及此后每满 **20 次请求**（远低于 29~30 次触发阈值）执行一次 `38 ~ 48 秒` 的滑动窗口清零休息；
   - 全程增量写入 `*.jsonl` 断点文件，若偶发点选/滑块验证（`verify.meituan.com`），自动暂停等待验证通过后从当前批次无缝续传。

---

## 4. 单表全规格采集字段规范（Excel 导出标准）

使用 `scripts/crawl_h5_store.py` 采集完成后，通过 `scripts/export_competitor_excel.py` 导出**单张工作表（`全量商品竞对明细`）**，包含所有商品与所有子规格（多规格按子规格逐行展开，同名同 SPU 紧邻排列）：

- **硬性规范**：
  1. **只生成一张工作表（`全量商品竞对明细`）**，包含全部单规格与多规格展开行。
  2. **`规格类型` 字段内容严格锁定为仅两个枚举值**：**`单规格`**、**`多规格`**（严禁出现“多规格同价(2个子规格)”等其他衍生文字；无论包含几个子规格一律归为 `多规格`）。
  3. **多规格行展示规则**：同一个多规格商品的所有子规格在表中上下紧挨排列，`商品名称` 与 `SPU_ID` 完全相同，`子规格名称(spec)`、价格、库存、条码各自独立。
  4. **包含 `店内二级分类` 字段**（通过商品 `tag` 关联门店 `food_spu_tags[].tags` 二级分类字典）。
  5. **已精简剔除冗余列**：不包含 `美团三级类目ID`、`月售文本`、`建议我方引流定价(95折)`、`30%保本毛利允许最高进价`、`包装费(元)`、`多规格全明细`、`组合规格(combine_spec)`、`单位`、`卖点描述`。

| 序号 | 标准列名 | 数据来源字段 | 竞对与选品业务说明 |
| :---: | :--- | :--- | :--- |
| 1 | `SPU_ID` | `id` | 美团商品唯一 SPU 标识 |
| 2 | `SKU_ID` | `skus[].id` | 美团子规格唯一 SKU 标识 |
| 3 | **`UPC条形码(69码)`** | `skus[].upccode` | **核心**：跨平台/跨店 100% 精准同品匹配依据 |
| 4 | `商品名称` | `name` | 完整商品标题（多规格商品各子规格行商品名相同） |
| 5 | **`规格类型`** | 仅 **`单规格`** / **`多规格`** | 严格二选一，不允许出现其他内容 |
| 6 | `子规格名称(spec)` | `skus[].spec` | 商家前台规格名（多规格靠此列区分不同子规格） |
| 7 | `店内一级分类` | `_l1_name` | 还原竞对店内一级货架分类 |
| 8 | **`店内二级分类`** | `tag` -> `sub_tags` | 还原竞对店内二级细分分类（如 `碳酸饮料`、`茶饮系列`） |
| 9 | `店内分类排名` | `allSortedSpuId` 索引 | 该商品在对手店内分类下的排序位置 |
| 10 | `美团一级类目` | `standardCategorys(level=1)` | 美团官方标准一级类目名称 |
| 11 | `美团二级类目` | `standardCategorys(level=2)` | 美团官方标准二级类目名称 |
| 12 | `美团三级类目名称` | `standardCategorys(level=3)` | 美团官方标准三级类目名称 |
| 13 | **`月销量(数值)`** | `month_saled_content` 转数值 | 核心动销排序指标 |
| 14 | `想买人数` | `want_to_Buy` | 潜在需求热度 |
| 15 | `点赞数` | `praise_num_new` | 好评点赞数 |
| 16 | **`实时库存`** | `skus[].real_stock` | 该规格当前实时库存深度 |
| 17 | `原价(元)` | `skus[].origin_price` | 该规格划线原价 |
| 18 | `直接折扣价(元)` | `skus[].price` | 该规格当前直接折扣售价 |
| 19 | **`神价/到手价(元)`** | `unify_price.actual_price` | 竞对神价/券后到手价 |
| 20 | `到手价说明` | `actual_price_text` | 到手价标签说明（如 `神价到手价`） |
| 21 | **`竞对有效基准价(元)`** | 剔除 0.01 霸王餐后最低有效价 | 真实价格竞争基准 |
| 22 | `促销标签` | `promotion_info` | 折扣率与限购说明 |
| 23 | `满减商品券` | `dynamic_act_labels` | 品类券/品牌满减券标签 |
| 24 | `限购份数` | `restrict` | 促销限购份数 |
| 25 | `最小起购数` | `min_order_count` | 最小起购数量 |
| 26 | `售卖属性(温度/提示)` | `attrs` | 前台加购属性（如常温/冰镇） |
| 27 | **`高清无水印主图URL`** | `picture`（去水印原图） | SPU 无水印高清首图 |
| 28 | `子规格图片URL` | `skus[].picture`（去水印原图） | 该行对应子规格的独立无水印高清图 |

---

## 5. 标准执行命令

1. **启动防风控全店采集**：
   ```bash
   python3 scripts/crawl_h5_store.py \
     --poi-id-str "<目标门店poi_id_str>" \
     --poi-name "<目标门店名称>" \
     --out-dir "<输出目录>"
   ```
2. **生成全维度竞对 Excel 报表**：
   ```bash
   python3 scripts/export_competitor_excel.py \
     --spus-jsonl "<输出目录>/spus.jsonl" \
     --cat-json "<输出目录>/categories.json" \
     --out-xlsx "<输出Excel路径>"
   ```
