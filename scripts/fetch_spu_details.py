#!/usr/bin/env python3
"""
Step 2 On-Demand Single-Product Detail & Image Fetcher (`fetch_spu_details.py`)
Part of the `meituanjingdui` skill.

Fetches carousel sub-images (`pictures`), graphic detail long images (`pic_content.contents`),
structured product attributes (`standard_productinfo_list`), sub-spec images (`skus[].picture`),
and text description (`description`) via `getCurrentPages()[0].api.productInfoV2` (`/quickbuy/v2/poi/product/info`)
for selected new/hot competitor products that need to be assembled into our own store's assortment.

Fixes & Risk Controls:
- Uses `parse_monthly_sales(month_saled_content, month_saled)` for `--top-n-sales` (never raw `month_saled`).
- Uses `unify_price` v5 pricing (`activity_price` / `underlined_price`), `combine_spec` fallback,
  `supply_poi_sku.upccode` fallback, and `.webp`-stripped clean image URLs.
- Safe anti-418/anti-403 pacing (`18~28s` + 125s sliding guard + `_onTouchMove`/mouse/scroll + C-end token
  self-healing) and immediate hard circuit breaker on HTTP 403/406 (never retries 403!).
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import urllib.request
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
import websockets

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from export_competitor_excel import (
    clean_pic_url,
    extract_sku_pricing_and_promo,
    parse_monthly_sales,
)


def fetch_json(url, timeout=5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def get_store_ws_url(port=9222):
    targets = fetch_json(f'http://127.0.0.1:{port}/json/list')
    for t in targets:
        if 'devtools://' in t.get('url', ''):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{t['id']}", timeout=2).read()
            except Exception:
                pass
    targets = fetch_json(f'http://127.0.0.1:{port}/json/list')
    page_tabs = [t for t in targets if t.get('type') == 'page']
    for pattern in ('cactivityapi-sc.waimai.meituan.com', 'wx-shangou.meituan.com', 'h5.waimai.meituan.com'):
        for t in page_tabs:
            u = t.get('url', '')
            if pattern in u:
                return t['webSocketDebuggerUrl'], u
    raise RuntimeError(f'No open Meituan H5 store tab found on port {port}')


class StoreDetailClient:
    def __init__(self, port=9222):
        self.port = port
        self.ws_url = None
        self.msg_id = 1
        self.req_timestamps = []
        self.connect()

    def connect(self):
        v = fetch_json(f'http://127.0.0.1:{self.port}/json/version')
        if v.get('webSocketDebuggerUrl'):
            self._cdp_on(v['webSocketDebuggerUrl'], 'Browser.setDownloadBehavior', {'behavior': 'default'})
        self.ws_url, _ = get_store_ws_url(self.port)

    def _cdp_on(self, ws_url, method, params=None):
        self.msg_id += 1
        cur_id = self.msg_id

        async def _call():
            async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
                await ws.send(json.dumps({'id': cur_id, 'method': method, 'params': params or {}}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data = json.loads(raw)
                    if data.get('id') == cur_id:
                        return data

        return asyncio.run(_call())

    def cdp(self, method, params=None):
        return self._cdp_on(self.ws_url, method, params)

    def eval_js(self, expr, await_promise=True):
        res = self.cdp('Runtime.evaluate', {
            'expression': expr,
            'awaitPromise': await_promise,
            'returnByValue': True
        })
        return res.get('result', {}).get('result', {}).get('value')

    def heal_c_end_token(self):
        tok = self.eval_js('''
        (() => {
            try {
                const m = document.cookie.match(/(?:^|;\\s*)(?:oops|mt_c_token)=([^;]+)/);
                let tok = m ? m[1] : "";
                if (!tok) {
                    const su = JSON.parse(localStorage.getItem("shangou_user") || "{}");
                    tok = (su.data && su.data.token) || "";
                }
                if (tok && !/(?:^|;\\s*)token=/.test(document.cookie)) {
                    document.cookie = "token=" + tok + "; domain=.meituan.com; path=/; max-age=15552000; secure; samesite=none";
                }
                return tok;
            } catch (e) { return ""; }
        })()
        ''', await_promise=False)
        if tok:
            self.cdp('Network.setCookie', {
                'name': 'token',
                'value': tok,
                'domain': '.meituan.com',
                'path': '/',
                'secure': True,
                'sameSite': 'None'
            })

    def simulate_human_browse(self):
        js = """
        (async () => {
            try {
                const p = typeof getCurrentPages === 'function' && getCurrentPages()[0];
                for (let i = 0; i < 6; i++) {
                    const x = 140 + Math.floor(Math.random() * 200);
                    const y = 220 + Math.floor(Math.random() * 380);
                    document.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: x, clientY: y}));
                    if (p && typeof p._onTouchMove === 'function') {
                        p._onTouchMove({touches: [{clientX: x, clientY: y}], changedTouches: [{clientX: x, clientY: y}]});
                    }
                    await new Promise(r => setTimeout(r, 35));
                }
                window.dispatchEvent(new Event('scroll', {bubbles: true}));
            } catch (e) {}
            return true;
        })()
        """
        try:
            self.eval_js(js, await_promise=True)
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
            self.heal_c_end_token()
            break

    def adaptive_sleep(self):
        self.simulate_human_browse()
        time.sleep(random.uniform(18.0, 28.0))
        while True:
            now = time.time()
            self.req_timestamps = [t for t in self.req_timestamps if now - t < 3720.0]
            in_2m = [t for t in self.req_timestamps if now - t < 125.0]
            if len(in_2m) >= 3:
                wait_s = 126.0 - (now - in_2m[0]) + random.uniform(4.0, 8.0)
                if wait_s > 0:
                    print(f'  [2m Sliding Guard] {len(in_2m)} detail reqs in 125s -> waiting {wait_s:.1f}s...', flush=True)
                    time.sleep(wait_s)
                    continue
            if len(self.req_timestamps) >= 45:
                wait_h = 3725.0 - (now - self.req_timestamps[0]) + random.uniform(20.0, 40.0)
                if wait_h > 0:
                    print(f'  [60m Detail Guard] {len(self.req_timestamps)} reqs in 62m -> resting {wait_h/60:.1f} mins...', flush=True)
                    time.sleep(wait_h)
                    continue
            break
        self.req_timestamps.append(time.time())

    def fetch_one_spu_detail(self, spu_id, spu_tag='', activity_tag=''):
        self.wait_if_verify()
        self.adaptive_sleep()
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
                return {{code: -500, msg: String(e.message || e)}};
            }}
        }})()
        """
        return self.eval_js(js, await_promise=True)


def extract_detail_fields(raw_detail, fallback_spu=None):
    d = raw_detail or {}
    fb = fallback_spu or {}

    raw_pics = d.get('pictures') or []
    if not raw_pics and d.get('picture'):
        raw_pics = [d.get('picture')]
    if not raw_pics and fb.get('picture'):
        raw_pics = [fb.get('picture')]
    carousel_pics = [clean_pic_url(u) for u in raw_pics if clean_pic_url(u)]

    pic_content = d.get('pic_content') or {}
    raw_detail_pics = pic_content.get('contents') or []
    detail_pics = [clean_pic_url(u) for u in raw_detail_pics if clean_pic_url(u)]

    std_props = d.get('standard_productinfo_list') or []
    props_dict = {}
    props_pairs = []
    for item in std_props:
        fname = str(item.get('fieldName') or '').strip()
        fval = str(item.get('value') or '').strip()
        if fname and fval:
            props_dict[fname] = fval
            props_pairs.append(f'{fname}:{fval}')

    skus = d.get('skus') or fb.get('skus') or []
    sku_pics = []
    for k in skus:
        sp = clean_pic_url(k.get('picture'))
        spec_name = str(k.get('spec') or k.get('combine_spec') or k.get('cspu_grouping_value') or '').strip()
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
        'merged_spu': d or fb,
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
    if not os.path.exists(details_jsonl):
        return
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
        fb = rec.get('spu') or {}
        parsed = extract_detail_fields(rec.get('detail'), fb)
        skus = parsed['skus'] or [{}]
        is_multi = len(skus) > 1
        spec_type = '多规格' if is_multi else '单规格'
        props = parsed['props_dict']
        brand = props.get('品牌', '')
        origin = props.get('产地', props.get('国产/进口', ''))
        net_wt = props.get('净含量', props.get('规格', ''))
        shelf_life = props.get('保质期', '')
        fb_skus_by_id = {str(x.get('id')): x for x in (fb.get('skus') or []) if isinstance(x, dict)}

        for k in skus:
            merged_k = dict(fb_skus_by_id.get(str(k.get('id')), {}))
            merged_k.update(k)
            k_orig, k_disc, _, _, _, _, _ = extract_sku_pricing_and_promo(parsed['merged_spu'], merged_k, is_multi)
            spec_str = str(merged_k.get('spec') or merged_k.get('combine_spec') or merged_k.get('cspu_grouping_value') or '').strip()
            upc_str = str(merged_k.get('upccode') or (merged_k.get('supply_poi_sku') or {}).get('upccode') or '').strip()
            raw_rs = merged_k.get('real_stock')
            real_stock_val = int(raw_rs) if (raw_rs is not None and int(raw_rs) >= 0) else int(merged_k.get('stock') or 0)

            ws.append([
                parsed['spu_id'],
                str(merged_k.get('id', '')),
                upc_str,
                parsed['name'],
                spec_type,
                spec_str,
                parsed['unit'],
                k_orig,
                k_disc,
                real_stock_val,
                brand,
                origin,
                net_wt,
                shelf_life,
                parsed['props_str'],
                parsed['description'],
                parsed['main_pic'],
                ' | '.join(parsed['sub_pics']),
                ' | '.join(parsed['detail_pics']),
                clean_pic_url(merged_k.get('picture')) or parsed['main_pic'],
            ])

    os.makedirs(os.path.dirname(os.path.abspath(out_xlsx)), exist_ok=True)
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
            key=lambda x: parse_monthly_sales(x.get('month_saled_content'), x.get('month_saled')),
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
        err_msg = str((res or {}).get('msg') or '')
        if '403' in err_msg or '406' in err_msg:
            print(f'[STOP_ON_403] productInfoV2 returned {err_msg} on SPU {sid}. Saving collected details and halting immediately.', flush=True)
            export_details_excel(args.out_jsonl, args.out_xlsx)
            sys.exit(43)
        if not res or res.get('code') != 0 or not res.get('data'):
            print(f'[{idx}/{len(target_ids)}] SPU {sid} fetch returned code={res.get("code") if res else None} msg={err_msg}, checking verify...', flush=True)
            client.wait_if_verify()
            continue
        rec = {'spu_id': sid, 'detail': res['data'], 'spu': fb}
        os.makedirs(os.path.dirname(os.path.abspath(args.out_jsonl)), exist_ok=True)
        with open(args.out_jsonl, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        parsed = extract_detail_fields(res['data'], fb)
        print(f"[{idx}/{len(target_ids)}] SPU {sid} ({parsed['name'][:20]}) -> 副图:{len(parsed['sub_pics'])}张, 详情长图:{len(parsed['detail_pics'])}张, 属性:{len(parsed['props_dict'])}项", flush=True)
        if args.download_images_dir:
            download_spu_images(parsed, args.download_images_dir)

    export_details_excel(args.out_jsonl, args.out_xlsx)


if __name__ == '__main__':
    main()
