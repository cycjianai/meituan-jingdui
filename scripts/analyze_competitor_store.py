#!/usr/bin/env python3
"""
Offline Competitor Assortment, Pricing & Barcode Benchmark Analyzer (`analyze_competitor_store.py`)
Part of the `meituanjingdui` skill (100% offline Excel/JSONL analysis — zero network requests).

Capabilities:
1. Reads a locked 27-column `{MMDD}{店铺名称}.xlsx` workbook and generates `{MMDD}{店铺名称}_数据分析报告.md`
   covering: SPU/SKU scale, active-sales rate, stockout rate, 69-barcode coverage, price-band distribution,
   direct-discount & `限1份` profit-lock tactics, coupon ladder, Top 20 L1 category sales contribution,
   and Top 30 traffic/profit benchmark items.
2. Optional `--our-excel`: matches by 69-barcode (`UPC`) against our own store's exported product sheet
   to output a 2-sheet actionable workbook `{MMDD}{店铺名称}_对标选品与调价清单.xlsx`:
   - Sheet 1 `同条码比价清单` (price comparison on matched 69-barcodes)
   - Sheet 2 `竞对热销我方缺品TOP` (high-sales competitor items whose 69-barcode is absent from our store)
"""
import argparse
from collections import Counter, defaultdict
import os
import re
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill


def norm_barcode(val):
    s = re.sub(r'\.0$', '', str(val or '').strip())
    digits = re.sub(r'\D+', '', s)
    return digits if len(digits) >= 8 else ''


def load_our_barcodes(our_excel_path):
    wb = openpyxl.load_workbook(our_excel_path, read_only=True)
    barcodes = {}
    for sname in wb.sheetnames:
        ws = wb[sname]
        rows = ws.iter_rows(values_only=True)
        headers = [str(x or '').strip() for x in next(rows, [])]
        if not headers:
            continue
        bc_col = next((i for i, h in enumerate(headers) if any(k in h.lower() for k in ('条码', '条形码', 'upc', 'barcode', '69码'))), None)
        name_col = next((i for i, h in enumerate(headers) if any(k in h for k in ('商品名称', '名称', '标题'))), None)
        price_col = next((i for i, h in enumerate(headers) if any(k in h for k in ('折扣价', '售价', '现价', '原价', '价格'))), None)
        if bc_col is None:
            continue
        for r in rows:
            if bc_col >= len(r):
                continue
            bc = norm_barcode(r[bc_col])
            if bc and bc not in barcodes:
                barcodes[bc] = {
                    'name': str(r[name_col] if (name_col is not None and name_col < len(r)) else '') or '',
                    'price': r[price_col] if (price_col is not None and price_col < len(r)) else '',
                }
    return barcodes


def analyze_workbook(excel_path, report_path=None, our_excel_path=None, diff_excel_path=None):
    wb = openpyxl.load_workbook(excel_path, read_only=True)
    ws = wb['全量商品竞对明细'] if '全量商品竞对明细' in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return
    data = rows[1:]

    spu_seen = {}
    cat_spus = defaultdict(set)
    cat_skus = Counter()
    cat_sales = Counter()
    cat_disc_skus = Counter()
    price_bands = Counter()
    disc_folds = []
    coupons = Counter()
    zero_stock_skus = 0
    disc_rows = 0
    hand_rows = 0
    restrict_rows = 0
    upc_rows = 0
    multi_rows = 0
    loss_leaders = []

    for r in data:
        spu_id, sku_id, upc, name, spec_type, spec, l1, l2, rank, mt1, mt2, mt3, sales, want, praise, stock, orig, disc, hand, hand_txt, eff, promo, coupon, restrict, min_ord, attrs, pic = r
        orig_f = float(orig or 0)
        disc_f = float(disc or 0)
        eff_f = float(eff or 0)
        sales_i = int(sales or 0)
        stock_i = int(stock or 0)
        rest_i = int(restrict or 0)
        bc = norm_barcode(upc)

        if spec_type == '多规格':
            multi_rows += 1
        if stock_i <= 0:
            zero_stock_skus += 1
        if bc:
            upc_rows += 1
        if hand not in ('', None):
            hand_rows += 1
        if rest_i > 0:
            restrict_rows += 1

        cat_skus[l1] += 1
        if orig_f > 0 and disc_f < orig_f:
            disc_rows += 1
            cat_disc_skus[l1] += 1
            disc_folds.append(disc_f / orig_f * 10.0)
            if disc_f <= 1.0 and sales_i >= 10:
                loss_leaders.append((sales_i, name, spec, orig_f, disc_f, promo, l1))

        if eff_f < 5:
            price_bands['0 ~ 4.9元（凑单/引流带）'] += 1
        elif eff_f < 10:
            price_bands['5 ~ 9.9元（第一主力带）'] += 1
        elif eff_f < 20:
            price_bands['10 ~ 19.9元（核心客单带）'] += 1
        elif eff_f < 50:
            price_bands['20 ~ 49.9元（中客单利润带）'] += 1
        elif eff_f < 100:
            price_bands['50 ~ 99.9元（整箱/礼盒带）'] += 1
        else:
            price_bands['>= 100元（高客单标品/数码带）'] += 1

        if coupon:
            for c_item in str(coupon).split(' | '):
                if c_item.strip():
                    coupons[c_item.strip()] += 1

        if spu_id not in spu_seen:
            spu_seen[spu_id] = {
                'spu_id': spu_id, 'upc': bc, 'name': name, 'spec': spec,
                'l1': l1, 'l2': l2, 'mt1': mt1, 'spec_type': spec_type,
                'sales': sales_i, 'want': int(want or 0), 'praise': int(praise or 0),
                'orig': orig_f, 'disc': disc_f, 'hand': hand, 'eff': eff_f,
                'promo': promo or '', 'restrict': rest_i, 'stock': stock_i,
            }
            cat_spus[l1].add(spu_id)
            cat_sales[l1] += sales_i

    total_spus = len(spu_seen)
    total_skus = len(data)
    multi_spus = sum(1 for s in spu_seen.values() if s['spec_type'] == '多规格')
    active_spus = sum(1 for s in spu_seen.values() if s['sales'] > 0)
    avg_fold = (sum(disc_folds) / len(disc_folds)) if disc_folds else 10.0
    deep_disc = sum(1 for f in disc_folds if f <= 7.0)

    base_no_ext = os.path.splitext(excel_path)[0]
    out_md = report_path or f"{base_no_ext}_数据分析报告.md"
    store_title = os.path.basename(base_no_ext)

    lines = [
        f"# {store_title} 竞对货盘与定价策略分析报告",
        "",
        f"- **数据源文件**：`{os.path.basename(excel_path)}`",
        f"- **商品总规模**：**`{total_spus}` 款独立 SPU / `{total_skus}` 行展开 SKU**（单规格 `{total_spus - multi_spus}` 款；多规格 `{multi_spus}` 款 / `{multi_rows}` 行）",
        f"- **全店动销率**：**`{active_spus / max(1, total_spus) * 100:.1f}%`**（`{active_spus}` 款月销 > 0） | **零库存断货率**：**`{zero_stock_skus / max(1, total_skus) * 100:.1f}%`**（`{zero_stock_skus}` 行）",
        f"- **69 条形码覆盖率**：**`{upc_rows / max(1, total_skus) * 100:.1f}%`**（`{upc_rows}` 行）",
        "",
        "---",
        "",
        "## 一、价格带与折扣锁利结构",
        "",
        f"- **直接折扣覆盖**：`{disc_rows} / {total_skus}` 行（**`{disc_rows / max(1, total_skus) * 100:.1f}%`**），平均折扣力度 **`{avg_fold:.2f} 折`**（其中 `≤7 折` 深折品 `{deep_disc}` 行，占 `{deep_disc / max(1, total_skus) * 100:.1f}%`）",
        f"- **独立神价/到手价覆盖**：`{hand_rows} / {total_skus}` 行（**`{hand_rows / max(1, total_skus) * 100:.1f}%`**）",
        f"- **单笔限购锁毛利比例**：`{restrict_rows} / {max(1, disc_rows)}` 折扣行设置了单笔限购（**`{restrict_rows / max(1, disc_rows) * 100:.1f}%`** 首件低价引流、次件恢复原价）",
        f"- **高频满减/商品券组合**：{', '.join(f'`{k}`({v}行)' for k, v in coupons.most_common(8)) or '无'}",
        "",
        "| 有效基准价格带 | SKU 行数 | 占比 |",
        "| :--- | :---: | :---: |",
    ]
    for band in (
        '0 ~ 4.9元（凑单/引流带）',
        '5 ~ 9.9元（第一主力带）',
        '10 ~ 19.9元（核心客单带）',
        '20 ~ 49.9元（中客单利润带）',
        '50 ~ 99.9元（整箱/礼盒带）',
        '>= 100元（高客单标品/数码带）',
    ):
        cnt = price_bands[band]
        lines.append(f"| {band} | {cnt} | {cnt / max(1, total_skus) * 100:.1f}% |")

    lines.extend([
        "",
        "---",
        "",
        "## 二、店内一级分类销量贡献 Top 20",
        "",
        "| 排名 | 店内一级分类 | SPU 数 | SKU 数 | 分类月销合计 | 均 SPU 月销 | 折扣 SKU 占比 |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: |",
    ])
    for idx, (l1, tot_s) in enumerate(sorted(cat_sales.items(), key=lambda x: (-x[1], -len(cat_spus[x[0]])))[:20], 1):
        scnt = len(cat_spus[l1])
        kcnt = cat_skus[l1]
        dcnt = cat_disc_skus[l1]
        lines.append(f"| {idx} | `{l1}` | {scnt} | {kcnt} | **{tot_s}** | {tot_s / max(1, scnt):.1f} | {dcnt / max(1, kcnt) * 100:.1f}% |")

    lines.extend([
        "",
        "---",
        "",
        "## 三、全店热销爆品 Top 30（引流品与核心大单品）",
        "",
        "| 排名 | 店内分类 | 商品名称 | 月销 | 想买 | 原价 | 直接折扣价 | 神价到手价 | 促销/限购 | 库存 |",
        "| :---: | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- | :---: |",
    ])
    for idx, s in enumerate(sorted(spu_seen.values(), key=lambda x: (-x['sales'], -x['want']))[:30], 1):
        h_val = s['hand'] if s['hand'] not in ('', None) else '-'
        lines.append(
            f"| {idx} | {s['l1']}/{s['l2']} | {s['name']} | **{s['sales']}** | {s['want']} | "
            f"{s['orig']} | **{s['disc']}** | {h_val} | {s['promo'] or '-'} | {s['stock']} |"
        )

    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"[Analysis Report Saved] {out_md}", flush=True)

    # Optional 69-barcode comparison against our own store's Excel
    if our_excel_path and os.path.exists(our_excel_path):
        our_map = load_our_barcodes(our_excel_path)
        out_diff = diff_excel_path or f"{base_no_ext}_对标选品与调价清单.xlsx"
        dwb = openpyxl.Workbook()
        header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
        header_font = Font(color='FFFFFF', bold=True)

        ws_match = dwb.active
        ws_match.title = '同条码比价清单'
        ws_match.append(['69条形码', '竞对商品名称', '竞对分类', '竞对月销', '竞对原价', '竞对直接折扣价', '竞对有效基准价', '我方商品名称', '我方价格', '价差(我方-竞对)'])
        for c in ws_match[1]:
            c.fill, c.font, c.alignment = header_fill, header_font, Alignment(horizontal='center')

        ws_gap = dwb.create_sheet('竞对热销我方缺品TOP')
        ws_gap.append(['69条形码', '竞对SPU_ID', '竞对商品名称', '子规格', '店内分类', '美团三级类目', '竞对月销', '想买人数', '竞对原价', '竞对直接折扣价', '竞对神价到手价', '促销标签', '竞对实时库存'])
        for c in ws_gap[1]:
            c.fill, c.font, c.alignment = header_fill, header_font, Alignment(horizontal='center')

        matched_cnt, gap_cnt = 0, 0
        sorted_data = sorted(data, key=lambda x: (-int(x[12] or 0), -int(x[13] or 0)))
        seen_gap_bc = set()
        for r in sorted_data:
            bc = norm_barcode(r[2])
            if not bc:
                continue
            if bc in our_map:
                our_p = our_map[bc]['price']
                try:
                    diff_v = round(float(our_p) - float(r[20] or r[17] or 0), 2)
                except Exception:
                    diff_v = ''
                ws_match.append([bc, r[3], f"{r[6]}/{r[7]}", r[12], r[16], r[17], r[20], our_map[bc]['name'], our_p, diff_v])
                matched_cnt += 1
            elif bc not in seen_gap_bc:
                seen_gap_bc.add(bc)
                ws_gap.append([bc, r[0], r[3], r[5], f"{r[6]}/{r[7]}", r[11], r[12], r[13], r[16], r[17], r[18], r[21], r[15]])
                gap_cnt += 1

        dwb.save(out_diff)
        print(f"[Benchmark Workbook Saved] Matched={matched_cnt} SKUs, Missing Gaps={gap_cnt} SKUs -> {out_diff}", flush=True)


def main():
    parser = argparse.ArgumentParser(description='Offline Competitor Store Assortment & Barcode Benchmark Analyzer')
    parser.add_argument('--excel', required=True, help='Path to exported 27-column {MMDD}{店铺名称}.xlsx')
    parser.add_argument('--report-out', default=None, help='Optional output Markdown report path')
    parser.add_argument('--our-excel', default=None, help='Optional path to our own store Excel for 69-barcode comparison')
    parser.add_argument('--diff-out', default=None, help='Optional output Excel path for 69-barcode benchmark diff')
    args = parser.parse_args()
    analyze_workbook(args.excel, args.report_out, args.our_excel, args.diff_out)


if __name__ == '__main__':
    main()
