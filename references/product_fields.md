# 商品字段对账样例

原版在满佳喜超市（长清店）核对了 8,867 个 SPU 和 11,848 行 SKU。下表保存了这次对账中发现的字段差异。

| 业务字段 | 旧映射 | 样本中的差异 | 导出脚本使用的字段 |
| :--- | :--- | :--- | :--- |
| **`直接折扣价(元)`** | `skus[].price`（在 v5 下等于原价！） | **9,065 / 9,101 行全错成原价** | `item_details[0].act_price` $\rightarrow$ `skus[].unify_price.activity_info.activity_price` $\rightarrow$ `skus[].price` |
| `原价(元)` | 仅读 `skus[].origin_price` | 部分活动划线价不同步 | `skus[].unify_price.underlined_price` $\rightarrow$ `activity_info.underline_price` $\rightarrow$ `skus[].origin_price` |
| **`神价/到手价(元)`** | 只读 SPU 层 `actual_price_info` | 多规格非最低价子规格全丢 | 优先子规格 `skus[].unify_price.actual_price_info.actual_price`（覆盖 5,224+ 行）$\rightarrow$ `secondary_activity_price` |
| **`限购份数`** | `skus[].restrict`（v5 下全为 `0`） | **9,100 行漏掉限购** | `skus[].restrict` ($>0$) $\rightarrow$ `skus[].unify_price.activity_info.quota_per_order` ($>0$) |
| **`促销标签`** | `skus[].promotion_info`（v5 下为 `""`） | **9,102 行标签为空** | 外层为空时由 `(1 - discount_strength) * 10` 折 + `quota_per_order` 合成（如 `7折 限1份`） |
| **`月销量(数值)`** | `spu.month_saled`（95%+ 为 `0`） | **8,492 SPU 销量变 0** | `max(解析(spu.month_saled_content), spu.month_saled)` |
| **`点赞数`** | `spu.praise_num_new`（`smoothRender` 下常为 `0`） | **405 SPU 漏掉真实点赞** | `max(praise_num_new, praise_num, praise_count, 解析(praise_content))` |
| `想买人数` | 直接取 `spu.want_to_Buy` | **378 个新品返回 `None`** | `max(want_to_Buy, 解析(want_to_buy_content), 0)` |
| `子规格名称(spec)` | 仅读 `skus[].spec` | **135 行量贩装为空** | `skus[].spec` $\rightarrow$ `skus[].combine_spec` $\rightarrow$ `skus[].cspu_grouping_value` |
| `店内二级分类` | 仅用 `spu.tag` 查表 | **502 SPU 退化为一级名** | `spu.tag` $\rightarrow$ `skus[0].tagBelongTo` $\rightarrow$ `categories.json` 的 `sub_tags[].allSortedSpuId` 反查 |
| `满减券` / `69码` / `主图` | 只读 SPU 层 / 保留 `.webp` | 6 行子规格券 + 多规格图 | 子规格优先读 `dynamic_act_labels` / `picture`（去 `@watermark`、尺寸、`.webp`）；69 码兜底 `supply_poi_sku.upccode` |
