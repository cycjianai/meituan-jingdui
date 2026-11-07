#!/usr/bin/env python3
"""
Step 2 On-Demand Single-Product Detail & Image Fetcher (`fetch_spu_details.py`)
Part of the `meituanjingdui` skill.

Fetches carousel sub-images (`pictures`), graphic detail long images (`pic_content.contents`),
structured product attributes (`standard_productinfo_list`), sub-spec images (`skus[].picture`),
and text description (`description`) via `getCurrentPages()[0].api.productInfoV2` (`/quickbuy/v2/poi/product/info`)
for selected new/hot competitor products that need to be assembled into our own store's assortment.
"""
import os
import re
import json
import time
import random
import argparse
import urllib.request
import asyncio
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
import websockets


def clean_pic_url(url):
    if not url:
        return ''
    u = re.sub(r'@watermark=.*$', '', str(url))
    u = re.sub(r'/\d+\.\d+\.\d+/', '/', u)
    return u


def get_store_ws_url(port=9222):
    # Ensure no DevTools window triggers H5guard debugger pause
    targets = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list').read().decode())
    for t in targets:
        if 'devtools://' in t.get('url', ''):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{t['id']}")
            except Exception:
                pass
    targets = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list').read().decode())
    for t in targets:
        u = t.get('url', '')
        if 'cactivityapi-sc.waimai.meituan.com' in u or 'wx-shangou.meituan.com' in u or 'h5.waimai.meituan.com' in u:
            return t['webSocketDebuggerUrl'], u
    raise RuntimeError(f'No open Meituan H5 store tab found on port {port}')


class StoreDetailClient:
    def __init__(self, port=9222):
        self.port = port
        self.ws_url = None
        self.msg_id = 1
        self.req_count = 0
        self.connect()

    def connect(self):
        self.ws_url, _ = get_store_ws_url(self.port)
        # Preserve default download filenames per Mac browser rule
        self.cdp('Browser.setDownloadBehavior', {'behavior': 'default'})

    def cdp(self, method, params=None):
        self.msg_id += 1
        cur_id = self.msg_id

        async def _call():
            async with websockets.connect(self.ws_url, max_size=50*1024*1024) as ws:
                await ws.send(json.dumps({'id': cur_id, 'method': method, 'params': params or {}}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data = json.loads(raw)
                    if data.get('id') == cur_id:
                        return data

        return asyncio.run(_call())

    def eval_js(self, expr, await_promise=True):
        res = self.cdp('Runtime.evaluate', {
            'expression': expr,
            'awaitPromise': await_promise,
            'returnByValue': True
        })
        return res.get('result', {}).get('result', {}).get('value')

    def simulate_human_browse(self):
        js = """
        (() => {
            try {
                for (let i = 0; i < 6; i++) {
                    const x = 140 + Math.floor(Math.random() * 200);
                    const y = 220 + Math.floor(Math.random() * 380);
                    const el = document.elementFromPoint(x, y) || document.body;
                    el.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: x, clientY: y}));
                }
                window.dispatchEvent(new Event('scroll', {bubbles: true}));
            } catch (e) {}
            return true;
        })()
        """
        try:
            self.eval_js(js, await_promise=False)
        except Exception:
            pass

    def wait_if_verify(self):
        while True:
            try:
                href = self.eval_js('location.href', await_promise=False) or ''
            except Exception:
                self.connect()
                href = self.eval_js('location.href', await_promise=False) or ''
            if 'verify.meituan.com' in href:
                print('[Verify Wait] Page is on verify.meituan.com. Waiting for verification...', flush=True)
                time.sleep(5)
                continue
            break

    def adaptive_sleep(self):
        self.req_count += 1
        self.simulate_human_browse()
        if self.req_count % 15 == 0:
            pause = random.uniform(65.0, 75.0)
            print(f'  [Rolling Window Reset] {self.req_count} detail requests -> resting {pause:.1f}s...', flush=True)
            time.sleep(pause)
        elif self.req_count % 6 == 0:
            pause = random.uniform(14.0, 20.0)
            print(f'  [Breather] {self.req_count} detail requests -> pausing {pause:.1f}s...', flush=True)
            time.sleep(pause)
        else:
            time.sleep(random.uniform(5.2, 7.2))

    def fetch_one_spu_detail(self, spu_id, spu_tag='', activity_tag=''):
        self.wait_if_verify()
        js = f"""
        (async () => {{
            const p = getCurrentPages()[0];
            if (!p || !p.api || !p.api.productInfoV2) return {{code: -999, msg: 'no_page'}};
            try {{
                const res = await p.api.productInfoV2({{
                    wm_poi_id: p.data.poiId || -100,
                    poi_id_str: p.data.poi_id_str,
                    spu_id: {json.dumps(str(spu_id))},
                    sku_id: 0,
                    spu_tag: {json.dumps(str(spu_tag or ''))},
                    activity_tag: {json.dumps(str(activity_tag or ''))}
                }});
                return {{code: res.code, data: res.data || null, msg: res.msg || ''}};
            }} catch (e) {{
                return {{code: -500, msg: String(e)}};
            }}
        }})()
        """
        res = self.eval_js(js, await_promise=True)
        self.adaptive_sleep()
        return res


def extract_detail_fields(raw_detail, fallback_spu=None):
    d = raw_detail or {}
    fb = fallback_spu or {}

    # 1. Carousel images (main image + sub-images / 副图)
    raw_pics = d.get('pictures') or []
    if not raw_pics and d.get('picture'):
        raw_pics = [d.get('picture')]
    if not raw_pics and fb.get('picture'):
        raw_pics = [fb.get('picture')]
    carousel_pics = [clean_pic_url(u) for u in raw_pics if clean_pic_url(u)]

    # 2. Graphic detail long images (商品详情长图)
    pic_content = d.get('pic_content') or {}
    raw_detail_pics = pic_content.get('contents') or []
    detail_pics = [clean_pic_url(u) for u in raw_detail_pics if clean_pic_url(u)]

    # 3. Standard product attribute table (品牌/产地/净含量/保质期/包装方式...)
    std_props = d.get('standard_productinfo_list') or []
    props_dict = {}
    props_pairs = []
    for item in std_props:
        fname = str(item.get('fieldName') or '').strip()
        fval = str(item.get('value') or '').strip()
        if fname and fval:
            props_dict[fname] = fval
            props_pairs.append(f'{fname}:{fval}')

    # 4. Sub-spec images (子规格图片)
    skus = d.get('skus') or fb.get('skus') or []
    sku_pics = []
    for k in skus:
        sp = clean_pic_url(k.get('picture'))
        spec_name = str(k.get('spec') or '').strip()
        if sp:
            sku_pics.append(f'{spec_name}:{sp}' if spec_name else sp)

    return {
        'spu_id': str(d.get('id') or fb.get('id') or ''),
        'name': d.get('name') or fb.get('name') or '',
        'main_pic': carousel_pics[0] if carousel_pics else clean_pic_url(fb.get('picture')),
        'sub_pics': carousel_pics[1:] if len(carousel_pics) > 1 else [],
        'all_carousel_pics': carousel_pics,
        'detail_pics': detail_pics,
        'sku_pics': sku_pics,
        'props_dict': props_dict,
        'props_str': ' ; '.join(props_pairs),
        'description': str(d.get('description') or fb.get('description') or '').strip(),
        'unit': str(d.get('unit') or fb.get('unit') or '').strip(),
        'skus': skus,
    }


def download_spu_images(parsed, base_img_dir):
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', '_', parsed['name'])[:40]
    spu_dir = os.path.join(base_img_dir, f"{parsed['spu_id']}_{safe_name}")
    os.makedirs(spu_dir, exist_ok=True)
    for idx, url in enumerate(parsed['all_carousel_pics'], 1):
        ext = os.path.splitext(url.split('?')[0])[1] or '.jpg'
        fname = f'01_主图_{idx}{ext}' if idx == 1 else f'02_副图_{idx-1}{ext}'
        fpath = os.path.join(spu_dir, fname)
        if not os.path.exists(fpath):
            try:
                urllib.request.urlretrieve(url, fpath)
            except Exception:
                pass
    for idx, url in enumerate(parsed['detail_pics'], 1):
        ext = os.path.splitext(url.split('?')[0])[1] or '.jpg'
        fname = f'03_详情图_{idx:02d}{ext}'
        fpath = os.path.join(spu_dir, fname)
        if not os.path.exists(fpath):
            try:
                urllib.request.urlretrieve(url, fpath)
            except Exception:
                pass


def export_details_excel(details_jsonl, out_xlsx):
    records = [json.loads(line) for line in open(details_jsonl, 'r', encoding='utf-8') if line.strip()]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '新品建品详情与图片组装表'

    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)

    headers = [
        'SPU_ID', 'SKU_ID', 'UPC条形码(69码)', '商品名称',
        '规格类型', '子规格名称(spec)', '单位',
        '原价(元)', '直接折扣价(元)', '实时库存',
        '品牌', '产地', '净含量/规格参数', '保质期', '完整商品属性参数表(standard_productinfo_list)',
        '图文卖点描述(description)',
        '高清无水印主图URL', '轮播副图URL列表(竖线分隔)', '商品详情长图URL列表(竖线分隔)', '子规格图片URL'
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    for rec in records:
        parsed = extract_detail_fields(rec.get('detail'), rec.get('spu'))
        skus = parsed['skus'] or [{}]
        is_multi = len(skus) > 1
        spec_type = '多规格' if is_multi else '单规格'
        props = parsed['props_dict']
        brand = props.get('品牌', '')
        origin = props.get('产地', props.get('国产/进口', ''))
        net_wt = props.get('净含量', props.get('规格', ''))
        shelf_life = props.get('保质期', '')

        for k in skus:
            ws.append([
                parsed['spu_id'],
                str(k.get('id', '')),
                str(k.get('upccode') or ''),
                parsed['name'],
                spec_type,
                k.get('spec') or '',
                parsed['unit'],
                k.get('origin_price', ''),
                k.get('price', ''),
                k.get('real_stock', k.get('stock', 0)),
                brand,
                origin,
                net_wt,
                shelf_life,
                parsed['props_str'],
                parsed['description'],
                parsed['main_pic'],
                ' | '.join(parsed['sub_pics']),
                ' | '.join(parsed['detail_pics']),
                clean_pic_url(k.get('picture')) or parsed['main_pic'],
            ])

    wb.save(out_xlsx)
    print(f"[Details Excel Saved] {len(records)} SPUs exported to {out_xlsx}", flush=True)


def main():
    parser = argparse.ArgumentParser(description='Fetch Meituan H5 Single-Product Details (Carousel Sub-Images, Detail Images, Attributes)')
    parser.add_argument('--spu-ids', default='', help='Comma-separated SPU IDs to fetch')
    parser.add_argument('--spus-jsonl', default='', help='Optional path to existing spus.jsonl to look up tag/fallback info or filter top sales')
    parser.add_argument('--top-n-sales', type=int, default=0, help='Automatically select top N SPUs by monthly sales from --spus-jsonl')
    parser.add_argument('--out-jsonl', required=True, help='Output JSONL path for fetched product details')
    parser.add_argument('--out-xlsx', required=True, help='Output Excel path for new-product assembly')
    parser.add_argument('--download-images-dir', default='', help='Optional directory to download main/sub/detail images')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port')
    args = parser.parse_args()

    spu_lookup = {}
    if args.spus_jsonl and os.path.exists(args.spus_jsonl):
        for line in open(args.spus_jsonl, 'r', encoding='utf-8'):
            if line.strip():
                s = json.loads(line)
                spu_lookup[str(s.get('id'))] = s

    target_ids = [x.strip() for x in args.spu_ids.split(',') if x.strip()]
    if not target_ids and args.top_n_sales > 0 and spu_lookup:
        sorted_spus = sorted(
            spu_lookup.values(),
            key=lambda x: int(x.get('month_saled') or 0),
            reverse=True
        )
        target_ids = [str(s['id']) for s in sorted_spus[:args.top_n_sales]]

    if not target_ids:
        raise SystemExit('Please provide --spu-ids or (--spus-jsonl with --top-n-sales N)')

    done_ids = set()
    if os.path.exists(args.out_jsonl):
        for line in open(args.out_jsonl, 'r', encoding='utf-8'):
            if line.strip():
                obj = json.loads(line)
                done_ids.add(str(obj.get('spu_id')))

    client = StoreDetailClient(port=args.port)
    for idx, sid in enumerate(target_ids, 1):
        if sid in done_ids:
            print(f'[{idx}/{len(target_ids)}] SPU {sid} already fetched, skipping.', flush=True)
            continue
        fb = spu_lookup.get(sid) or {}
        res = client.fetch_one_spu_detail(
            sid,
            spu_tag=fb.get('tag', ''),
            activity_tag=fb.get('activity_tag', '')
        )
        if not res or res.get('code') != 0 or not res.get('data'):
            print(f'[{idx}/{len(target_ids)}] SPU {sid} fetch returned code={res.get("code") if res else None}, waiting for verify...', flush=True)
            client.wait_if_verify()
            res = client.fetch_one_spu_detail(
                sid,
                spu_tag=fb.get('tag', ''),
                activity_tag=fb.get('activity_tag', '')
            )
        if res and res.get('code') == 0 and res.get('data'):
            rec = {'spu_id': sid, 'detail': res['data'], 'spu': fb}
            with open(args.out_jsonl, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            parsed = extract_detail_fields(res['data'], fb)
            print(f"[{idx}/{len(target_ids)}] SPU {sid} ({parsed['name'][:20]}) -> 副图:{len(parsed['sub_pics'])}张, 详情长图:{len(parsed['detail_pics'])}张, 属性:{len(parsed['props_dict'])}项", flush=True)
            if args.download_images_dir:
                download_spu_images(parsed, args.download_images_dir)

    export_details_excel(args.out_jsonl, args.out_xlsx)


if __name__ == '__main__':
    main()
