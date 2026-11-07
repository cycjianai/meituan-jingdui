# 美团 H5 闪购（`wx-shangou`）风控机制剖析与防 418 压测手册

## 一、核心底层架构发现

当用户在 `https://h5.waimai.meituan.com/waimai/mindex/home` 点击进入任何一家闪购/超市便利门店（如满佳喜超市、鲸速达超市、京东便利店）时：
1. 页面会跳转至 `https://cactivityapi-sc.waimai.meituan.com/h5/sub-trade/restaurant/restaurant?poi_id_str=<门店加密ID>`；
2. 该页面在浏览器内运行美团闪购 Web 化小程序框架（`reco_fe_shangchao_wxapp`），其页面实例可直接通过全局 `getCurrentPages()[0]` 访问；
3. 页面数据接口直连 `https://wx-shangou.meituan.com/quickbuy/v1/*`，主要包括：
   - `/quickbuy/v1/poi/food`：进店首包（约 1.3 MB），包含门店信息 `poi_info`、全店一级与二级分类树 `food_spu_tags`（含 70+ 个一级分类及 400+ 个子分类）；
   - `/quickbuy/v1/poi/sputag/products`：按分类 ID 拉取该分类下的**全量排序商品 ID 数组（`allSortedSpuId`）**及前 20 个商品详情；
   - `/quickbuy/v1/poi/product/smooth/render`：按传入的 `spuIds` 数组（**单次最高支持 50 个 SPU ID**）批量返回完整的商品 SPU 与子 SKU 结构化详情。

---

## 二、为什么普通脚本/小程序快刷必中 `HTTP 418`（Yoda 滑块验证）？

在压力测试中我们定位到了触发 `HTTP 418`（跳转 `https://verify.meituan.com/v2/app/general_page`）的 4 个精确原因：

1. **DevTools 窗口开启触发反调试陷阱**
   - 若针对 H5 标签页打开了 Chrome `DevTools` 窗口（`devtools://...`），`H5guard` 会检测到调试器并挂起或标记风险。采集前必须通过 `/json/close/<devtoolsTargetId>` 关闭 DevTools 标签页。
2. **连续调用 `spuTagProducts` 超过 55~60 次且无分类点击埋点与 `smoothRender` 配合**
   - 第一轮压测中，连续 60 次仅调用 `requestSpusAndSortedIds`（速率 ~2.0 秒/次，无鼠标移动、无 `_lxFirstCategoryClick`、无 `smoothRender`），在第 60 个分类时精准触发 `Status code not 200[418]`。
   - **原因**：真人浏览单个分类时，会先触发灵犀埋点 `_lxFirstCategoryClick(idx)`（上报 `bid: "b_g16gkfb4"`），接着拉取首屏 `spuTagProducts`，随后下滑触发 `smoothRender`。只刷分类首屏不刷详情页、且无灵犀点击埋点，属于典型爬虫特征。
3. **缺少 DOM 行为生物探针上报（`msp.meituan.com/fingerprint/v1/notapp/bio/info/report`）**
   - 页面内的 `H5guard` 持续收集 `document` / `window` 上的 `mousemove`、`mousedown`、`mouseup`、`click`、`scroll` 事件并周期性上报 `bio/info/report`。
   - **macOS 踩坑铁律**：在 macOS 下，当 Chrome 窗口处于后台或被遮挡时，调用 CDP 的 `Input.dispatchMouseEvent(type="mouseWheel")` 会因为等待渲染合成器（Compositor）帧确认而永久阻塞 `ws.recv()`。因此**必须通过 `Runtime.evaluate` 在 JS 内部使用 `document.dispatchEvent(new MouseEvent(...))` 与 `window.dispatchEvent(new Event('scroll'))` 注入事件**，既不阻塞后台运行，又能被 `H5guard` 正常采集。
4. **1 分钟滑动窗口请求频次（QPM）超限**
   - 当 1 分钟内请求数持续超过 20~25 次且无间歇停顿时，网关会在第 55~60 次请求触发滑块。

---

## 三、零封控、高吞吐的标准采集参数（已固化至 `crawl_h5_store.py`）

1. **分类遍历顺序**：
   - 先遍历 `idx >= 7` 的实体商品分类（如 `杜蕾斯`、`玛氏专区`、`饮料专区`...），再遍历 `idx < 7` 的促销聚合分类（跳过纯领券分类 `idx == 2`）。
   - 因为促销分类 99% 的商品已包含在实体分类中，全局 `seen_spus` 去重可直接省去 25% 以上的冗余请求。
2. **单分类内闭环拉取**：
   - 每个一级分类取 `sub_idx = 0`（即 `全部` 视图，已实测验证 `全部` 视图的 `allSortedSpuId` 100% 包含该分类下所有二级子分类的全部商品 ID，0 遗漏）。
   - 拿到 `allSortedSpuId` 后，立即在该分类内按 **50 个 SPU/批** 调用 `requestSmoothSpus` 拉完该分类所有未采集商品，再切下一个分类。
3. **三级呼吸限速**：
   - 每次请求前：注入 6 步 DOM 鼠标轨迹 + 点击 + 滚动事件，随机间隔 `2.8 ~ 4.2 秒`；
   - 每满 **10 次请求**：小憩 `8.0 ~ 12.0 秒`；
   - 每满 **32 次请求**：执行 `22.0 ~ 28.0 秒` 的滑动窗口清零休息（期间穿插 1 次鼠标移动）。
   - 在该节奏下，单次请求可拿 50 个 SPU，一个 8,000+ SKU 的大型闪电仓仅需约 **180~210 次请求（约 15~18 分钟）** 即可全量抓完，支持一天连续稳定采集多家门店。
