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
3. **1 分钟滑动窗口请求频次上限（QPM 门禁）**：
   - 单店连续请求速率若超过 `~22 次/分钟` 或连续突发满 `55 次` 无停顿，网关会返回 `418`。

### 3.2 极速且零封控的标准采集架构（单店 8,000+ SKU 约 15~18 分钟，支持一天连爬多店）
1. **分类遍历顺序优化（先爬实体分类 `idx >= 7`，最后补齐活动分类 `0..6`）**：
   - 门店前 7 个分类通常为 `推荐`、`神价`、`活动`、`疯狂折扣` 等聚合活动区（99% 商品与实体分类重复）。
   - **先遍历 `idx >= 7` 的实体一级分类**：既能确保每个商品落入真实的店内一级分类（如 `饮料专区`、`生活用水`、`五金家装`），又能在最后扫描活动分类时通过全局 `seen_spus` 自动跳过已抓商品，减少约 25% 冗余请求。
2. **单分类“全部”视图一次拿齐 ID + 50 品/批满载拉取**：
   - 对每个一级分类 `idx`，取其 `全部` 子标签（`sub_idx = 0 if p._ifHasSubTag(idx) else -1`），调用 1 次 `requestSpusAndSortedIds` 即可拿到该一级分类下**全部子分类的 100% 排序商品 ID（`allSortedSpuId`）**及前 20 个商品详情，无需逐个点击二级子分类。
   - 对该分类剩余未采集的 `allSortedSpuId`，按服务端最大上限 **50 个 SPU/批** 调用 `p.requestSmoothSpus(batchOf50, idx, sub_idx, c.tag, c.type, c.extra, true)`。
3. **自适应呼吸限速（Adaptive Pacing）**：
   - 每次请求前注入 6 步 DOM 鼠标轨迹与滚动事件，基础间隔 `2.8 ~ 4.2 秒`；
   - 每满 **10 次请求** 小憩 `8 ~ 12 秒`；
   - 每满 **32 次请求**（远低于 55 次触发阈值）执行一次 `22 ~ 28 秒` 的滑动窗口清零休息；
   - 全程增量写入 `*.jsonl` 断点文件，若偶发滑块验证（`verify.meituan.com`），自动暂停等待验证通过后从当前批次无缝续传。

---

## 4. 全维度采集字段规范（Excel 导出标准）

使用 `scripts/crawl_h5_store.py` 采集完成后，通过 `scripts/export_competitor_excel.py` 导出包含以下完整维度的 Excel 报表（支持用户按需定制字段）：

| 字段模块 | 标准列名 | 数据来源字段 | 竞对与选品业务价值 |
| :--- | :--- | :--- | :--- |
| **定位与分类** | `店内一级分类` | `_l1_name` | 还原竞对店内货架结构 |
| | `店内分类排序` | `allSortedSpuId` 索引 | 识别该品类在对手店内的主推排位 |
| | `美团一级/二级/三级类目` | `standardCategorys` | 官方标准三级类目 ID 与名称，用于配额均衡选品 |
| **身份与条码** | `SPU_ID` / `SKU_ID` | `id` / `skus[].id` | 美团平台商品与规格唯一标识 |
| | **`UPC条形码(69码)`** | `skus[].upccode` | **核心**：跨平台/跨店 100% 精准同品匹配依据 |
| | `商品名称` | `name` | 完整商品标题 |
| | `规格类型` | 单规格 / 多规格同价 / 多规格不同价 | 识别引流子规格与利润子规格 |
| | `子规格名称` / `组合规格` | `skus[].spec` / `combine_spec` | 精准对齐容量、克重、连包倍数 |
| | `售卖属性` | `attrs`（如常温/冰镇） | 还原前台加购属性选项 |
| **视觉与素材** | **`高清无水印主图URL`** | `picture`（剥离 `@watermark`） | **核心**：无条码百货/散品的视觉多模态比对依据 |
| | `子规格图片URL` | `skus[].picture` | 多规格独立展示图 |
| **全维价格体系** | `原价(元)` | `skus[].origin_price` | 划线原价基准 |
| | `直接折扣价(元)` | `skus[].price` | 列表/弹窗直接展示的折后售价 |
| | **`神价/券后到手价(元)`** | `unify_price.actual_price_info.actual_price` | 竞对真实前台最低到手价（如神价到手价） |
| | **`竞对有效基准价(元)`** | 剔除 0.01 新客限1份霸王餐后的真实竞争价 | 用于本方制定“比竞对低”的目标售价 |
| | `促销活动标签` | `promotion_info` | 折扣率与限购说明（如 `5.78折 限1份`） |
| | `满减商品券标签` | `dynamic_act_labels` | 品类券/品牌券（如 `满10减2`、`满59减15`） |
| | `限购份数` / `起购数` / `包装费` | `restrict` / `min_order_count` / `box_price` | 识别引流限购品与打包费暗扣 |
| **销量与库存** | **`月售文本`** / **`月销量(数值)`** | `month_saled_content` | 核心动销指标（自动将 `800+` 转为数值排序） |
| | `想买人数` / `点赞数` | `want_to_Buy` / `praise_num_new` | 潜在需求热度与好评指标 |
| | **`当前实时库存`** | `skus[].real_stock` | 对手真实备货深度（判断是真爆款还是空挂引流） |

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
