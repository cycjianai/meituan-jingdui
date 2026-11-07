# 美团 H5 闪购（`wx-shangou`）风控机制剖析与防 418 压测手册

## 一、核心底层架构发现

当用户在 `https://h5.waimai.meituan.com/waimai/mindex/home` 点击进入任何一家闪购/超市便利门店（如满佳喜超市、鲸速达超市、京东便利店）时：
1. 页面会跳转至 `https://cactivityapi-sc.waimai.meituan.com/h5/sub-trade/restaurant/restaurant?poi_id_str=<门店加密ID>`；
2. 该页面在浏览器内运行美团闪购 Web 化小程序框架（`reco_fe_shangchao_wxapp`），其页面实例可直接通过全局 `getCurrentPages()[0]` 访问；
3. 页面数据接口直连 `https://wx-shangou.meituan.com/quickbuy/v1/*`，主要包括：
   - `/quickbuy/v1/poi/food`：进店首包（约 1.3 MB），包含门店信息 `poi_info`、全店一级与二级分类树 `food_spu_tags`（含 70+ 个一级分类及 400+ 个子分类）；
   - `/quickbuy/v1/poi/sputag/products`：按分类 ID 拉取该分类下的**全量排序商品 ID 数组（`allSortedSpuId`）**及前 20 个商品详情；
   - `/quickbuy/v1/poi/product/smooth/render`：按传入的 `spuIds` 数组批量返回完整的商品 SPU 与子 SKU 结构化详情（**注意：虽然前端 JS 切片上限写为 50，但服务端实际单次最多返回 30 个 SPU！必须按 30 个/批动态计算剩余未采集 ID，否则每批会漏掉后 20 个商品并产生跳跃式异常请求特征**）。

---

## 二、为什么普通脚本/小程序快刷必中 `HTTP 418`（Yoda 验证中心）？

在两轮真实压力测试中，我们精确测出了触发 `HTTP 418`（跳转 `https://verify.meituan.com/v2/app/general_page`）的 5 个边界条件：

1. **DevTools 窗口开启触发反调试陷阱**
   - 若针对 H5 标签页打开了 Chrome `DevTools` 窗口（`devtools://...`），`H5guard` 会检测到调试器并挂起或标记风险。采集前必须通过 `/json/close/<devtoolsTargetId>` 关闭 DevTools 标签页。
2. **连续调用 `spuTagProducts` 超过 55~60 次且无分类点击埋点与 `smoothRender` 配合**
   - 第一轮压测中，连续 60 次仅调用 `requestSpusAndSortedIds`（速率 ~2.0 秒/次，无鼠标移动、无 `_lxFirstCategoryClick`、无 `smoothRender`），在第 60 个分类时触发 `Status code not 200[418]`。
   - **原因**：真人浏览单个分类时，会先触发灵犀埋点 `_lxFirstCategoryClick(idx)`（上报 `bid: "b_g16gkfb4"`），接着拉取首屏 `spuTagProducts`，随后下滑触发 `smoothRender`。
3. **`smoothRender` 批次大小不匹配导致 ID 跳跃特征**
   - 前端 `_requestSmoothMenuData` 在首屏 20 个商品后，每次请求 30 个商品（服务端硬上限 30 个/次）。若脚本按 50 个切片并固定 `b_start += 50`，不仅会漏采 40% 的分页商品，还会向服务端发送不连续的跳跃式 `spuIds`（如跳过第 50~69 个直接请求第 70~119 个），极易触发风控异常识别。
4. **缺少 DOM 行为生物探针上报（`msp.meituan.com/fingerprint/v1/notapp/bio/info/report`）**
   - 页面内的 `H5guard` 持续收集 `document` / `window` 上的 `mousemove`、`mousedown`、`mouseup`、`click`、`scroll` 事件并周期性上报 `bio/info/report`。
   - **macOS 踩坑铁律**：在 macOS 下，当 Chrome 窗口处于后台或被遮挡时，调用 CDP 的 `Input.dispatchMouseEvent(type="mouseWheel")` 会因为等待渲染合成器（Compositor）帧确认而永久阻塞 `ws.recv()`。因此**必须通过 `Runtime.evaluate` 在 JS 内部使用 `document.dispatchEvent(new MouseEvent(...))` 与 `window.dispatchEvent(new Event('scroll'))` 注入事件**，既不阻塞后台运行，又能被 `H5guard` 正常采集。
5. **2 分钟滑动窗口请求上限（~28–30 次请求）**
   - 第二轮压测证实：进店首屏自带约 4~5 次底层请求，此后在连续请求（含 10 秒短停顿）累计达到第 **29~30 次**（加上进店首屏共约 34 次请求/2分钟）时会触发滑动窗口阈值。短于 15 秒的停顿无法清零网关的 2 分钟滑动窗口计数器，必须在每 **18~20 次请求**后执行一次 **38~48 秒** 的窗口清零休眠。

---

## 三、零封控、高吞吐的标准采集参数（已固化至 `crawl_h5_store.py`）

1. **分类遍历顺序**：
   - 先遍历 `idx >= 7` 的实体商品分类（如 `杜蕾斯`、`玛氏专区`、`饮料专区`...），再遍历 `idx < 7` 的促销聚合分类（跳过纯领券分类 `idx == 2`）。
   - 因为促销分类 99% 的商品已包含在实体分类中，全局 `seen_spus` 去重可直接省去 25% 以上的冗余请求。
2. **单分类内闭环 + 30 个/批动态差集拉取**：
   - 每个一级分类取 `sub_idx = 0`（即 `全部` 视图，已实测验证 `全部` 视图的 `allSortedSpuId` 100% 包含该分类下所有二级子分类的全部商品 ID，0 遗漏）。
   - 拿到 `allSortedSpuId` 后，实时计算 `missing_ids = [x for x in all_ids if x not in seen_spus]`，每次取前 **30 个 SPU ID** 调用 `requestSmoothSpus`，确保 100% 连续无遗漏。
3. **三级呼吸限速（20 请求滑动窗口清零机制）**：
   - 每次请求前：注入 6 步 DOM 鼠标轨迹 + 点击 + 滚动事件，随机间隔 `3.5 ~ 5.2 秒`；
   - 每满 **8 次请求**：小憩 `9.0 ~ 13.0 秒`；
   - 第 **18 次请求**及此后每满 **20 次请求**：执行 `38.0 ~ 48.0 秒` 的滑动窗口清零休息（期间穿插鼠标移动）。
   - 若遇到验证页（`verify.meituan.com`），脚本自动挂起等待并在用户完成点选/滑块后从断点无缝续传，0 数据丢失。
