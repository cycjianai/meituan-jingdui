#!/usr/bin/env python3
"""
Export Meituan H5 Shangou Full-Dimension Competitor Data to a Single-Sheet Excel.
Part of the `meituanjingdui` skill.
"""
import os
import re
import json
import argparse
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment


# Built-in fallback mapping of known 满佳喜超市 L2 sub_tag IDs -> L2 category names
KNOWN_SUB_TAG_NAMES = {
    '1646828890': '绿箭/益达/大大',
    '1646900173': '绿箭/益达/大大',
    '1646839952': '彩虹糖/真知棒',
    '1646889255': '彩虹糖/真知棒',
    '1646878293': '德芙/士力架',
    '1646554617': '碳酸饮料',
    '1692462633': '碳酸饮料',
    '1646830823': '茶饮系列',
    '1748749344': '茶饮系列',
    '1646853771': '奶茶咖啡',
    '1646863639': '可口可乐',
    '1646871355': '网红热销',
    '1646884202': '整件特惠',
    '1646891054': '功能饮料',
    '1646906079': '果蔬汁',
    '1646861468': '饮料专区',
    '1646844655': '肉蛋',
    '1646854543': '应季蔬菜',
    '1646852750': '新鲜水果',
    '1646872254': '新鲜水果',
    '1646538783': '康面专区',
    '1646906090': '康面专区',
    '1748913166': '康面专区',
    '1646856662': '饮用水',
    '1646864661': '饮用水',
    '1646835783': '饮用水',
    '1646871354': '苏打水',
    '1646863645': '箱装水',
    '1791178923': '箱装水',
    '1646814997': '果酒鸡尾酒',
    '1646827786': '果酒鸡尾酒',
    '1646861474': '白酒洋酒',
    '1646857654': '黄酒白酒',
    '1646896195': '黄酒白酒',
    '1646862706': '啤酒专区',
    '1646554629': '啤酒专区',
    '1646903137': '啤酒专区',
    '1791213560': '啤酒专区',
    '1646868408': '精酿啤酒',
    '1646869445': '鲜奶酸奶',
    '1646893111': '鲜奶酸奶',
    '1646876347': '常温奶',
    '1646845920': '常温奶',
    '1646877393': '乳饮料',
    '1646867645': '高纤烘焙',
    '1646878295': '蛋白肉食',
    '1646872266': '蛋白肉食',
    '1646851502': '辣卤风味',
    '1646813999': '烘焙辅料',
    '1646671540': '烘焙辅料',
    '1646841663': '精美包装',
    '1646847844': '淀粉面粉',
    '1646866447': '蛋挞皮液',
    '1646875493': '工具模具',
    '1646894080': '奶油黄油',
    '1646828906': '自热速食',
    '1646835803': '白象统一',
    '1646838972': '网红泡面',
    '1646841682': '甜粥罐头',
    '1646863664': '火腿卤蛋',
    '1646830816': '火腿卤蛋',
    '1646865470': '熟食',
    '1790727277': '火鸡农心',
    '1791236112': '白象系列.',
}


def parse_monthly_sales(saled_content, saled_num=0):
    if saled_num and int(saled_num) > 0:
        return int(saled_num)
    txt = str(saled_content or '').strip()
    if not txt:
        return 0
    m_wan = re.search(r'(\d+(?:\.\d+)?)\s*万', txt)
    if m_wan:
        return int(float(m_wan.group(1)) * 10000)
    m_num = re.search(r'(\d+)', txt)
    if m_num:
        return int(m_num.group(1))
    return 0


def clean_pic_url(url):
    if not url:
        return ''
    u = re.sub(r'@watermark=.*$', '', str(url))
    u = re.sub(r'/\d+\.\d+\.\d+/', '/', u)
    return u


def extract_std_categories(spu):
    cats = spu.get('standardCategorys') or []
    l1_name, l2_name, l3_name = '', '', ''
    for c in cats:
        lvl = c.get('level')
        if lvl == 1:
            l1_name = c.get('name', '')
        elif lvl == 2:
            l2_name = c.get('name', '')
        elif lvl == 3:
            l3_name = c.get('name', '')
    return l1_name, l2_name, l3_name


def extract_coupon_tags(spu):
    labels = spu.get('dynamic_act_labels') or []
    texts = []
    for lb in labels:
        for st in (lb.get('sub_tags') or []):
            t = (st.get('text') or '').strip()
            if t:
                texts.append(t)
    return ' | '.join(texts)


def extract_attrs_str(spu):
    attrs = spu.get('attrs') or []
    parts = []
    for a in attrs:
        aname = a.get('name', '')
        vals = [v.get('value', '') for v in (a.get('values') or []) if v.get('value')]
        if vals:
            parts.append(f"{aname}:{','.join(vals)}")
    return ' ; '.join(parts)


def compute_effective_competitor_price(orig_price, sku_price, actual_price, promo_str, restrict_cnt):
    """
    Compute realistic competitor benchmark price (excluding 0.01 / <=0.1 yuan 1-piece new-customer loss-leader traps).
    """
    candidates = []
    for p in (actual_price, sku_price):
        if p is not None and p != '' and float(p) > 0:
            candidates.append(float(p))
    orig = float(orig_price) if (orig_price is not None and orig_price != '') else 0.0
    if not candidates:
        return orig
    lowest = min(candidates)
    if lowest <= 0.1 and orig > 1.0 and (restrict_cnt == 1 or '0.0' in str(promo_str or '')):
        return orig
    return lowest


def resolve_store_l2_name(spu, l1_name, sub_tag_lookup):
    raw_tag = str(spu.get('tag') or '')
    l1_tag = str(spu.get('_l1_tag') or '')
    if raw_tag in sub_tag_lookup:
        name = sub_tag_lookup[raw_tag]
        if name and name != '全部':
            return name
    if raw_tag in KNOWN_SUB_TAG_NAMES:
        return KNOWN_SUB_TAG_NAMES[raw_tag]
    if raw_tag and raw_tag == l1_tag:
        return l1_name
    return l1_name


def build_excel(spus_jsonl, cat_json, out_xlsx):
    spus = [json.loads(line) for line in open(spus_jsonl, 'r', encoding='utf-8') if line.strip()]
    cat_map = json.load(open(cat_json, 'r', encoding='utf-8')) if (cat_json and os.path.exists(cat_json)) else {}

    # Build L1 category rank lookup and L2 sub_tag_lookup from cat_map
    spu_cat_info = {}
    sub_tag_lookup = {}
    sorted_cat_keys = sorted(
        [k for k in cat_map.keys() if str(k).isdigit()],
        key=lambda k: (0 if int(k) >= 7 else 1, int(k))
    )
    for k in sorted_cat_keys:
        c = cat_map[k]
        cname = c.get('name', '')
        for st in (c.get('sub_tags') or []):
            if isinstance(st, dict) and st.get('tag') and st.get('name') and st.get('name') != '全部':
                sub_tag_lookup[str(st['tag'])] = st['name']
        for rank_idx, sid in enumerate(c.get('allSortedSpuId') or [], 1):
            sid_int = int(sid)
            if sid_int not in spu_cat_info:
                spu_cat_info[sid_int] = (int(k), cname, rank_idx, len(c.get('allSortedSpuId') or []))

    # Enrich and sort SPUs
    for s in spus:
        sid = int(s['id'])
        s['_sales_num'] = parse_monthly_sales(s.get('month_saled_content'), s.get('month_saled'))
        if sid in spu_cat_info:
            c_idx, c_name, c_rank, c_tot = spu_cat_info[sid]
            s['_store_cat_idx'] = c_idx
            s['_store_cat_name'] = c_name
            s['_store_cat_rank'] = c_rank
            s['_store_cat_total'] = c_tot
        else:
            s['_store_cat_idx'] = s.get('_l1_idx', 999)
            s['_store_cat_name'] = s.get('_l1_name', '')
            s['_store_cat_rank'] = 9999
            s['_store_cat_total'] = 0
        s['_store_l2_name'] = resolve_store_l2_name(s, s['_store_cat_name'], sub_tag_lookup)

    spus.sort(key=lambda x: (-x['_sales_num'], -int(x.get('want_to_Buy') or 0), x['_store_cat_idx'], x['_store_cat_rank']))

    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)
    multi_fill = PatternFill(start_color='FFF2CC', end_color='FFF2CC', fill_type='solid')

    # Single Worksheet ONLY: 全量商品竞对明细表 (包含所有规格逐行展开，共27列)
    ws = wb.active
    ws.title = '全量商品竞对明细'
    headers = [
        'SPU_ID', 'SKU_ID', 'UPC条形码(69码)', '商品名称',
        '规格类型', '子规格名称(spec)',
        '店内一级分类', '店内二级分类', '店内分类排名',
        '美团一级类目', '美团二级类目', '美团三级类目名称',
        '月销量(数值)', '想买人数', '点赞数', '实时库存',
        '原价(元)', '直接折扣价(元)', '神价/到手价(元)', '到手价说明', '竞对有效基准价(元)',
        '促销标签', '满减商品券', '限购份数', '最小起购数',
        '售卖属性(温度/提示)', '高清无水印主图URL'
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    total_sku_rows = 0
    multi_spu_count = 0
    multi_sku_rows = 0

    for s in spus:
        skus = s.get('skus') or [{}]
        # Strictly two options for 规格类型: '单规格' or '多规格'
        is_multi = len(skus) > 1
        spec_type = '多规格' if is_multi else '单规格'
        if is_multi:
            multi_spu_count += 1

        l1_name, l2_name, l3_name = extract_std_categories(s)
        unify = s.get('unify_price') or {}
        actual_info = unify.get('actual_price_info') or {}
        spu_actual_price = actual_info.get('actual_price')
        actual_text = actual_info.get('actual_price_text', '')

        spu_promo_str = s.get('promotion_info') or ''
        coupon_tags = extract_coupon_tags(s)
        attrs_str = extract_attrs_str(s)
        main_pic = clean_pic_url(s.get('picture'))

        for k in skus:
            k_orig = k.get('origin_price', s.get('min_price'))
            k_disc = k.get('price', s.get('min_price'))
            k_rest = k.get('restrict', 0)
            k_promo = k.get('promotion_info') or spu_promo_str
            sku_actual_p = spu_actual_price if (spu_actual_price is not None and (not is_multi or k_disc == s.get('min_price'))) else ''
            k_eff = compute_effective_competitor_price(k_orig, k_disc, sku_actual_p if sku_actual_p != '' else None, k_promo, k_rest)

            row_data = [
                str(s.get('id', '')),
                str(k.get('id', '')),
                str(k.get('upccode') or ''),
                s.get('name', ''),
                spec_type,
                k.get('spec') or '',
                s.get('_store_cat_name', ''),
                s.get('_store_l2_name', ''),
                s.get('_store_cat_rank', ''),
                l1_name,
                l2_name,
                l3_name,
                s.get('_sales_num', 0),
                s.get('want_to_Buy', 0),
                s.get('praise_num_new', 0),
                k.get('real_stock', k.get('stock', 0)),
                k_orig,
                k_disc,
                sku_actual_p,
                actual_text if sku_actual_p != '' else '',
                k_eff,
                k_promo,
                coupon_tags,
                k_rest,
                k.get('min_order_count', 1),
                attrs_str,
                main_pic
            ]
            ws.append(row_data)
            total_sku_rows += 1
            if is_multi:
                for cell in ws[ws.max_row]:
                    cell.fill = multi_fill
                multi_sku_rows += 1

    wb.save(out_xlsx)
    print(f"[Export Done] Single sheet '{ws.title}' (27 cols): {len(spus)} SPUs -> {total_sku_rows} SKU rows (单规格: {len(spus)-multi_spu_count}, 多规格: {multi_spu_count} SPUs / {multi_sku_rows} rows) saved to {out_xlsx}", flush=True)


def main():
    parser = argparse.ArgumentParser(description='Export Meituan H5 Competitor JSONL to Single-Sheet Excel')
    parser.add_argument('--spus-jsonl', required=True, help='Path to spus.jsonl')
    parser.add_argument('--cat-json', default=None, help='Path to categories.json')
    parser.add_argument('--out-xlsx', required=True, help='Output Excel path')
    args = parser.parse_args()
    build_excel(args.spus_jsonl, args.cat_json, args.out_xlsx)


if __name__ == '__main__':
    main()
