#!/usr/bin/env python3
"""
Meituan Shangou H5 Store Catalog Collector (Compliant Polite-Pacing Edition)
Part of the `meituanjingdui` skill.

Design & Compliance Principles:
1. Uses only the user's already-logged-in regular Chrome tab on the target store page.
2. Strictly NO synthetic touch/mouse trajectory spoofing, NO cookie/fingerprint tampering,
   NO DevTools tab manipulation, and NO multi-account rotation.
3. Immediate clean stop (after saving checkpoint & partial 27-col Excel):
   - Exit 42 if redirected to verification (`verify.meituan.com` / status 418) — never waits out or bypasses challenges.
   - Exit 43 if server returns HTTP 403/406 access restriction — never retries or rotates accounts.
   - Exit 44 if single-account daily soft cap (`--max-sputag-daily` / `--max-smooth-daily`) is reached.
4. Conservative polite rate-limiting ON by default (`68~88s` interval, `<=38` reqs/hour, `10m` rest every `30` reqs).
"""
import argparse
import asyncio
import datetime
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import websockets

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_EXCEL_PY = os.path.join(SCRIPT_DIR, 'export_competitor_excel.py')

PROMO_TAB_KEYWORDS = (
    '推荐', '神价', '活动', '热销', '折扣', '狂欢', '促销', '特惠',
    '满减', '爆品', '秒杀', '囤货', '买过', '常买', '优惠', '新品上新',
)
SKIP_VIRTUAL_TAGS = {'op0050'}


def fetch_json(url, timeout=5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def load_existing_spus(spus_jsonl):
    seen = {}
    if os.path.exists(spus_jsonl):
        with open(spus_jsonl, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    sid = obj.get('id')
                    if sid:
                        seen[int(sid)] = obj
                except Exception:
                    pass
    return seen


def classify_menu_tab(tab):
    tag_str = str(tab.get('tag') or '').strip()
    name = str(tab.get('name') or '').strip()
    tab_type = int(tab.get('type') or 0)

    if tag_str in SKIP_VIRTUAL_TAGS:
        return 'skip'
    is_numeric_tag = tag_str.isdigit() or (tag_str.endswith('_27') and tag_str[:-3].isdigit())
    if not is_numeric_tag and tab_type not in (1, 27):
        return 'promo'
    if any(kw in name for kw in PROMO_TAB_KEYWORDS):
        return 'promo'
    return 'entity'


async def eval_js(ws, msg_id, expr, timeout=25.0):
    await ws.send(json.dumps({
        'id': msg_id,
        'method': 'Runtime.evaluate',
        'params': {
            'expression': expr,
            'awaitPromise': True,
            'returnByValue': True,
        }
    }))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        if msg.get('id') == msg_id:
            if 'error' in msg:
                raise RuntimeError(f"CDP error: {msg['error']}")
            res = msg.get('result', {})
            if 'exceptionDetails' in res:
                raise RuntimeError(f"JS exception: {res['exceptionDetails']}")
            return res.get('result', {}).get('value')


async def verify_store_ready_or_stop(ws, msg_seq, expected_poi_id_str=None, expected_poi_name=None, on_stop=None):
    """
    Check once that the tab is on the expected store page.
    If gated by verify.meituan.com, flush partial data and exit immediately (code 42) — NEVER wait out challenges.
    """
    msg_seq += 1
    state = await eval_js(ws, msg_seq, '''
    (() => {
        const hasPages = typeof getCurrentPages === 'function' && getCurrentPages().length > 0;
        const p = hasPages ? getCurrentPages()[0] : null;
        const menuLen = p && p.data && Array.isArray(p.data.menuTabs) ? p.data.menuTabs.length : 0;
        const poiIdStr = p && p.data ? p.data.poi_id_str : '';
        const poiName = p && p.data && p.data.poiInfo ? p.data.poiInfo.name : '';
        const poiAddr = p && p.data && p.data.poiInfo ? p.data.poiInfo.address : '';
        return {url: location.href, hasPages, menuLen, poiIdStr, poiName, poiAddr};
    })()
    ''')
    cur_url = (state or {}).get('url') or ''
    if 'verify.meituan.com' in cur_url:
        print(f"[STOP_ON_VERIFY] Page redirected to verification challenge ({cur_url[:90]}). Saving partial data and stopping immediately.", flush=True)
        if on_stop:
            on_stop()
        sys.exit(42)

    if not state or not state.get('hasPages') or state.get('menuLen', 0) <= 0:
        if on_stop:
            on_stop()
        raise RuntimeError(f"Target store page is not ready in Chrome tab (url={cur_url[:90]}). Please open the store page first.")

    actual_id = state.get('poiIdStr') or ''
    actual_name = state.get('poiName') or ''
    if expected_poi_id_str and actual_id != expected_poi_id_str:
        if on_stop:
            on_stop()
        raise RuntimeError(
            f"Store poi_id_str mismatch! Expected '{expected_poi_id_str}', but tab is on '{actual_name}' ({actual_id})."
        )
    if expected_poi_name and expected_poi_name not in actual_name:
        if on_stop:
            on_stop()
        raise RuntimeError(
            f"Store name mismatch! Expected substring '{expected_poi_name}', but tab is on '{actual_name}' ({actual_id})."
        )
    return msg_seq, state


REQ_TIMESTAMPS = []
SPUTAG_TIMESTAMPS = []
SMOOTH_TIMESTAMPS = []


def load_timestamps(ts_json):
    global REQ_TIMESTAMPS, SPUTAG_TIMESTAMPS, SMOOTH_TIMESTAMPS
    now = time.time()
    if os.path.exists(ts_json):
        try:
            data = json.load(open(ts_json, 'r', encoding='utf-8'))
            REQ_TIMESTAMPS = [float(t) for t in data.get('req', []) if now - float(t) < 3720.0]
            SPUTAG_TIMESTAMPS = [float(t) for t in data.get('sputag', []) if now - float(t) < 3720.0]
            SMOOTH_TIMESTAMPS = [float(t) for t in data.get('smooth', []) if now - float(t) < 3720.0]
            return
        except Exception:
            pass
    REQ_TIMESTAMPS = []
    SPUTAG_TIMESTAMPS = []
    SMOOTH_TIMESTAMPS = []


def save_timestamps(ts_json):
    now = time.time()
    data = {
        'req': [t for t in REQ_TIMESTAMPS if now - t < 3720.0],
        'sputag': [t for t in SPUTAG_TIMESTAMPS if now - t < 3720.0],
        'smooth': [t for t in SMOOTH_TIMESTAMPS if now - t < 3720.0],
    }
    with open(ts_json, 'w', encoding='utf-8') as f:
        json.dump(data, f)


async def polite_sleep(
    req_counter, ts_json, endpoint='sputag_p0', export_cb=None,
    min_delay=68.0, max_delay=88.0, hourly_cap=38, wave_size=30, wave_rest_sec=600.0
):
    """Pure time-based polite rate-limiting (zero synthetic DOM/touch events)."""
    await asyncio.sleep(random.uniform(min_delay, max_delay))

    if export_cb and req_counter > 0 and req_counter % 3 == 0:
        export_cb()

    if wave_size > 0 and wave_rest_sec > 0 and req_counter > 1 and (req_counter - 1) % wave_size == 0:
        rest_s = random.uniform(wave_rest_sec * 0.95, wave_rest_sec * 1.1)
        if export_cb:
            export_cb()
        print(f"  [Wave Rest] Completed {req_counter - 1} requests -> pausing {rest_s/60:.1f} mins to keep request rate low...", flush=True)
        await asyncio.sleep(rest_s)

    ep_list = SPUTAG_TIMESTAMPS if endpoint == 'sputag_p0' else SMOOTH_TIMESTAMPS
    ep_cap = min(55, int(hourly_cap))
    total_cap = min(58, max(ep_cap, int(hourly_cap) + 2))
    while True:
        now = time.time()
        in_2m = [t for t in REQ_TIMESTAMPS if now - t < 125.0]
        in_62m_ep = [t for t in ep_list if now - t < 3720.0]
        in_62m_total = [t for t in REQ_TIMESTAMPS if now - t < 3720.0]
        if len(in_2m) >= 3:
            wait_2m = 126.0 - (now - in_2m[0]) + random.uniform(4.0, 8.0)
            if wait_2m > 0:
                print(f"  [2m Rate Guard] {len(in_2m)} reqs in last 125s, sleeping {wait_2m:.1f}s...", flush=True)
                await asyncio.sleep(wait_2m)
                continue
        if len(in_62m_ep) >= ep_cap:
            wait_62m = 3725.0 - (now - in_62m_ep[0]) + random.uniform(20.0, 40.0)
            if wait_62m > 0:
                if export_cb:
                    export_cb()
                print(f"  [60m Endpoint Rate Guard ({endpoint})] {len(in_62m_ep)} reqs in last 62m (cap {ep_cap}), sleeping {wait_62m/60:.1f} mins...", flush=True)
                await asyncio.sleep(wait_62m)
                continue
        if len(in_62m_total) >= total_cap:
            wait_tot = 3725.0 - (now - in_62m_total[0]) + random.uniform(20.0, 40.0)
            if wait_tot > 0:
                if export_cb:
                    export_cb()
                print(f"  [60m Total Rate Guard] {len(in_62m_total)} total reqs in last 62m (cap {total_cap}), sleeping {wait_tot/60:.1f} mins...", flush=True)
                await asyncio.sleep(wait_tot)
                continue
        break

    now_done = time.time()
    REQ_TIMESTAMPS.append(now_done)
    ep_list.append(now_done)
    save_timestamps(ts_json)


def find_store_tab(tabs):
    page_tabs = [t for t in tabs if t.get('type') == 'page']
    for pattern in ('cactivityapi-sc.waimai.meituan.com', 'verify.meituan.com', 'wx-shangou.meituan.com', 'h5.waimai.meituan.com'):
        match = next((t for t in page_tabs if pattern in t.get('url', '')), None)
        if match:
            return match
    return None


async def run_crawler(args):
    try:
        v = fetch_json(f'http://127.0.0.1:{args.port}/json/version')
        tabs = fetch_json(f'http://127.0.0.1:{args.port}/json/list')
    except (urllib.error.URLError, OSError) as e:
        print(f"[ERROR] Cannot reach Chrome remote debugging port {args.port}. Start regular Chrome with --remote-debugging-port={args.port}.", file=sys.stderr)
        raise SystemExit(2) from e

    # Preserve normal download filenames in Chrome
    if v.get('webSocketDebuggerUrl'):
        async with websockets.connect(v['webSocketDebuggerUrl'], max_size=20 * 1024 * 1024) as bws:
            await bws.send(json.dumps({'id': 1, 'method': 'Browser.setDownloadBehavior', 'params': {'behavior': 'default'}}))
            await bws.recv()

    target = find_store_tab(tabs)
    if not target:
        raise RuntimeError(f"Could not find open Meituan H5 store tab in Chrome on port {args.port}. Please log in and open the store page first.")

    if args.inspect_only:
        async with websockets.connect(target['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024) as ws:
            state = await eval_js(ws, 1, '''
            (() => {
                const hasPages = typeof getCurrentPages === 'function' && getCurrentPages().length > 0;
                const p = hasPages ? getCurrentPages()[0] : null;
                const menuTabs = (p && p.data && Array.isArray(p.data.menuTabs)) ? p.data.menuTabs : [];
                const poiInfo = (p && p.data && p.data.poiInfo) || {};
                const hasAuth = /(?:^|;\\s*)(?:token|oops|mt_c_token)=/.test(document.cookie);
                return {
                    url: location.href,
                    hasAuth,
                    inStorePage: hasPages && menuTabs.length > 0,
                    poi_name: poiInfo.name || '',
                    poi_id_str: (p && p.data && p.data.poi_id_str) || '',
                    address: poiInfo.address || '',
                    l1_category_count: menuTabs.length
                };
            })()
            ''')
            print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)
            return

    if not args.out_dir:
        raise SystemExit('--out-dir is required unless --inspect-only is specified')

    os.makedirs(args.out_dir, exist_ok=True)
    spus_jsonl = os.path.join(args.out_dir, 'spus.jsonl')
    cat_map_json = os.path.join(args.out_dir, 'categories.json')
    ts_json = os.path.join(args.out_dir, 'timestamps.json')
    progress_json = os.path.join(args.out_dir, 'progress.json')
    daily_quota_json = os.path.join(args.out_dir, 'daily_quota.json')

    daily_quota = {}
    if os.path.exists(daily_quota_json):
        try:
            daily_quota = json.load(open(daily_quota_json, 'r', encoding='utf-8'))
        except Exception:
            daily_quota = {}

    def today_key():
        return datetime.date.today().isoformat()

    def get_daily_count(ep):
        return int((daily_quota.get(today_key()) or {}).get(ep, 0))

    def inc_daily_count(ep):
        tk = today_key()
        daily_quota.setdefault(tk, {'sputag': 0, 'smooth': 0})
        daily_quota[tk][ep] = int(daily_quota[tk].get(ep, 0)) + 1
        with open(daily_quota_json, 'w', encoding='utf-8') as df:
            json.dump(daily_quota, df, ensure_ascii=False, indent=2)

    print(
        f"[Daily Quota] {today_key()}: sputag={get_daily_count('sputag')}/{args.max_sputag_daily}, "
        f"smooth={get_daily_count('smooth')}/{args.max_smooth_daily}",
        flush=True,
    )

    load_timestamps(ts_json)
    seen_spus = load_existing_spus(spus_jsonl)
    cat_map = {}
    if os.path.exists(cat_map_json):
        try:
            cat_map = json.load(open(cat_map_json, 'r', encoding='utf-8'))
        except Exception:
            cat_map = {}

    msg_seq = 1
    req_counter = 0

    async with websockets.connect(target['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024) as ws:
        msg_seq, state = await verify_store_ready_or_stop(ws, msg_seq, args.poi_id_str, args.poi_name)
        actual_poi_id = state.get('poiIdStr')
        actual_poi_name = state.get('poiName')
        store_name_for_file = args.poi_name or actual_poi_name or '竞对门店'
        out_f = open(spus_jsonl, 'a', encoding='utf-8')

        def export_current_excel():
            out_f.flush()
            if os.path.exists(EXPORT_EXCEL_PY) and os.path.exists(spus_jsonl) and os.path.exists(cat_map_json):
                cmd = [
                    sys.executable, EXPORT_EXCEL_PY,
                    '--spus-jsonl', spus_jsonl,
                    '--cat-json', cat_map_json,
                    '--out-dir', args.excel_out_dir or args.out_dir,
                    '--store-name', store_name_for_file,
                ]
                if args.date_mmdd:
                    cmd.extend(['--date-mmdd', args.date_mmdd])
                subprocess.run(cmd, check=False)

        def handle_stop_error(context_label, err_msg):
            export_current_excel()
            out_f.close()
            if '418' in err_msg or 'verify' in err_msg.lower():
                print(f"[STOP_ON_VERIFY] {context_label} encountered verification challenge ({err_msg}). Saved partial Excel and exiting immediately.", flush=True)
                sys.exit(42)
            if '403' in err_msg or '406' in err_msg:
                print(f"[STOP_ON_403] {context_label} encountered HTTP 403/406 restriction ({err_msg}). Saved partial Excel and exiting immediately.", flush=True)
                sys.exit(43)
            print(f"[STOP_ON_ERROR] {context_label} failed ({err_msg}). Saved partial Excel and exiting.", flush=True)
            sys.exit(1)

        msg_seq += 1
        meta = await eval_js(ws, msg_seq, '''
        (() => {
            const p = getCurrentPages()[0];
            const rawTags = (p.store && p.store.getState && p.store.getState().poi && p.store.getState().poi.food_spu_tags) || [];
            return {
                poi_id_str: p.data.poi_id_str,
                poiInfo: p.data.poiInfo,
                menuTabs: (p.data.menuTabs || []).map((m, idx) => ({
                    idx,
                    name: m.name,
                    tag: m.tag,
                    type: m.type,
                    hasSub: typeof p._ifHasSubTag === 'function' ? p._ifHasSubTag(idx) : false,
                    sub_tags: ((rawTags[idx] && rawTags[idx].tags) || []).map(st => ({
                        tag: st.tag,
                        name: st.name
                    }))
                }))
            };
        })()
        ''')
        menu_tabs = meta.get('menuTabs') or []
        poi_info = meta.get('poiInfo') or {}
        print(
            f"[Store Verified] {poi_info.get('name')} ({meta.get('poi_id_str')}) | "
            f"Address: {poi_info.get('address', '')} | L1 categories: {len(menu_tabs)} | Existing SPUs: {len(seen_spus)}",
            flush=True,
        )

        unfetchable_ids = set()

        def record_spus(spus_list, l1_idx, l1_name, l1_tag):
            added = 0
            for s in (spus_list or []):
                if not isinstance(s, dict) or not s.get('id'):
                    continue
                sid = int(s['id'])
                if sid not in seen_spus:
                    s['_l1_idx'] = l1_idx
                    s['_l1_name'] = l1_name
                    s['_l1_tag'] = l1_tag
                    seen_spus[sid] = s
                    out_f.write(json.dumps(s, ensure_ascii=False) + '\n')
                    added += 1
            if added > 0:
                out_f.flush()
            return added

        def save_progress(phase, step_num, total_steps, idx, name):
            all_discovered = {
                int(sid)
                for v_cat in cat_map.values()
                for sid in (v_cat.get('allSortedSpuId') or [])
            }
            with open(progress_json, 'w', encoding='utf-8') as pf:
                json.dump({
                    'phase': phase,
                    'poi_id_str': actual_poi_id,
                    'poi_name': actual_poi_name,
                    'step': step_num,
                    'total_steps': total_steps,
                    'current_l1_idx': idx,
                    'current_l1_name': name,
                    'unique_spus_collected': len(seen_spus),
                    'total_discovered_ids': len(all_discovered),
                    'requests_made': req_counter,
                    'timestamp': time.time(),
                }, pf, ensure_ascii=False, indent=2)

        # Harvest initial SPUs already in Redux memory (0 network requests)
        msg_seq += 1
        mem_spus = await eval_js(ws, msg_seq, '''
        (() => {
            const p = getCurrentPages()[0];
            const tags = (p.store && p.store.getState && p.store.getState().poi && p.store.getState().poi.food_spu_tags) || [];
            const out = [];
            tags.forEach((t, idx) => {
                if (Array.isArray(t.spus) && t.spus.length) {
                    out.push({idx, name: t.name, tag: t.tag, spus: t.spus});
                }
                if (Array.isArray(t.tags)) {
                    t.tags.forEach(st => {
                        if (Array.isArray(st.spus) && st.spus.length) {
                            out.push({idx, name: t.name, tag: t.tag, spus: st.spus});
                        }
                    });
                }
            });
            return out;
        })()
        ''')
        mem_added = sum(record_spus(item.get('spus'), item.get('idx'), item.get('name'), item.get('tag')) for item in (mem_spus or []))
        if mem_added:
            print(f"[Memory Harvest] Recovered +{mem_added} SPUs from initial page state (unique={len(seen_spus)})", flush=True)

        entity_tabs, promo_tabs = [], []
        for m in menu_tabs:
            kind = classify_menu_tab(m)
            if kind == 'skip':
                continue
            m['_is_promo'] = (kind == 'promo')
            (entity_tabs if kind == 'entity' else promo_tabs).append(m)
        ordered_tabs = entity_tabs + promo_tabs

        pacing_kw = dict(
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            hourly_cap=args.hourly_cap,
            wave_size=args.wave_size,
            wave_rest_sec=args.wave_rest_sec,
        )
        print(
            f"[Pacing] delay={args.min_delay:.0f}~{args.max_delay:.0f}s, hourly_cap={args.hourly_cap}, "
            f"wave_size={args.wave_size}, wave_rest={args.wave_rest_sec/60:.1f}m",
            flush=True,
        )

        # PASS 1: L1 categories at page_index=0
        for step_num, m in enumerate(ordered_tabs, 1):
            idx, name, tag = m['idx'], m['name'], m['tag']
            sub_idx = 0 if m['hasSub'] else -1
            str_idx = str(idx)
            is_promo = bool(m.get('_is_promo'))

            if str_idx in cat_map and cat_map[str_idx].get('allSortedSpuId'):
                continue

            msg_seq += 1
            mem_cat = await eval_js(ws, msg_seq, f'''
            (() => {{
                const p = getCurrentPages()[0];
                const c = p._getCachedData({idx}, {sub_idx});
                return {{
                    tag: c && c.tag,
                    type: c && c.type,
                    allSortedSpuId: (c && c.allSortedSpuId) || [],
                    spus: (c && c.spus) || []
                }};
            }})()
            ''')
            if mem_cat and len(mem_cat.get('allSortedSpuId') or []) > 0:
                added_mem = record_spus(mem_cat.get('spus'), idx, name, tag)
                cat_map[str_idx] = {
                    'idx': idx, 'name': name, 'tag': mem_cat.get('tag') or tag,
                    'type': mem_cat.get('type') or 1, 'is_promo': is_promo,
                    'sub_idx': sub_idx, 'sub_tags': m.get('sub_tags', []),
                    'allSortedSpuId': mem_cat['allSortedSpuId'],
                }
                with open(cat_map_json, 'w', encoding='utf-8') as cf:
                    json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                save_progress('Pass1_L1_mem', step_num, len(ordered_tabs), idx, name)
                print(f"[Pass1 {step_num}/{len(ordered_tabs)}] Cat[{idx}] {name}: {len(mem_cat['allSortedSpuId'])} IDs in memory (+{added_mem} SPUs, 0 reqs)", flush=True)
                continue

            if args.max_sputag_daily > 0 and get_daily_count('sputag') >= args.max_sputag_daily:
                print(f"[DAILY_QUOTA_GUARD] spuTagProducts reached daily soft cap ({get_daily_count('sputag')}/{args.max_sputag_daily}). Saving and stopping cleanly.", flush=True)
                export_current_excel()
                out_f.close()
                sys.exit(44)

            req_counter += 1
            await polite_sleep(req_counter, ts_json, endpoint='sputag_p0', export_cb=export_current_excel, **pacing_kw)
            msg_seq, _ = await verify_store_ready_or_stop(ws, msg_seq, actual_poi_id, args.poi_name, on_stop=export_current_excel)
            inc_daily_count('sputag')

            msg_seq += 1
            res = await eval_js(ws, msg_seq, f'''
            (async () => {{
                const p = getCurrentPages()[0];
                const c = p._getCachedData({idx}, {sub_idx});
                try {{
                    await p.requestSpusAndSortedIds({idx}, {sub_idx}, c.tag, c.type, 0, true);
                }} catch (err) {{
                    return {{error: String(err.message || err)}};
                }}
                const c2 = p._getCachedData({idx}, {sub_idx});
                return {{
                    tag: c2.tag,
                    type: c2.type,
                    allSortedSpuId: c2.allSortedSpuId || [],
                    spus: c2.spus || []
                }};
            }})()
            ''')
            if res and res.get('error'):
                handle_stop_error(f"Pass1 Cat[{idx}] {name}", str(res['error']))

            all_ids = (res or {}).get('allSortedSpuId') or []
            added = record_spus((res or {}).get('spus') or [], idx, name, tag)
            cat_map[str_idx] = {
                'idx': idx, 'name': name, 'tag': (res or {}).get('tag', tag),
                'type': (res or {}).get('type', 1), 'is_promo': is_promo,
                'sub_idx': sub_idx, 'sub_tags': m.get('sub_tags', []),
                'allSortedSpuId': all_ids,
            }
            with open(cat_map_json, 'w', encoding='utf-8') as cf:
                json.dump(cat_map, cf, ensure_ascii=False, indent=2)
            save_progress('Pass1_L1_p0', step_num, len(ordered_tabs), idx, name)
            print(f"[Pass1 {step_num}/{len(ordered_tabs)}] Cat[{idx}] {name}: {len(all_ids)} total IDs, +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

        export_current_excel()

        # PASS 1.5: Smooth pagination batch=30
        smooth_capped = bool(args.max_smooth_daily > 0 and get_daily_count('smooth') >= args.max_smooth_daily)
        for step_num, m in enumerate(ordered_tabs, 1):
            if smooth_capped:
                break
            idx, name, tag = m['idx'], m['name'], m['tag']
            sub_idx = 0 if m['hasSub'] else -1
            cinfo = cat_map.get(str(idx)) or {}
            all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
            if not all_ids:
                continue

            while not smooth_capped:
                if args.max_smooth_daily > 0 and get_daily_count('smooth') >= args.max_smooth_daily:
                    print(f"[DAILY_QUOTA_GUARD] smoothRender reached daily soft cap ({get_daily_count('smooth')}/{args.max_smooth_daily}). Switching to Pass 2 if quota allows.", flush=True)
                    smooth_capped = True
                    break
                missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
                if not missing_ids:
                    break
                batch = [str(x) for x in missing_ids[:30]]
                req_counter += 1
                await polite_sleep(req_counter, ts_json, endpoint='smooth', export_cb=export_current_excel, **pacing_kw)
                msg_seq, _ = await verify_store_ready_or_stop(ws, msg_seq, actual_poi_id, args.poi_name, on_stop=export_current_excel)
                inc_daily_count('smooth')

                msg_seq += 1
                res = await eval_js(ws, msg_seq, f'''
                (async () => {{
                    const p = getCurrentPages()[0];
                    const c = p._getCachedData({idx}, {sub_idx});
                    if (!Array.isArray(c.allSortedSpuId) || !c.allSortedSpuId.length) {{
                        c.allSortedSpuId = {json.dumps(all_ids)};
                    }}
                    const targetIds = {json.dumps(batch)};
                    try {{
                        await p.requestSmoothSpus(targetIds, {idx}, {sub_idx}, c.tag, c.type, {{}});
                        const c2 = p._getCachedData({idx}, {sub_idx});
                        const idSet = new Set(targetIds.map(Number));
                        return {{spus: (c2.spus || []).filter(s => s && idSet.has(Number(s.id)))}};
                    }} catch (err) {{
                        return {{error: String(err.message || err)}};
                    }}
                }})()
                ''')
                if res and res.get('error'):
                    handle_stop_error(f"Pass1.5 Cat[{idx}] {name}", str(res['error']))

                spus = (res or {}).get('spus') or []
                returned_ids = {int(s['id']) for s in spus if isinstance(s, dict) and s.get('id')}
                for bid_str in batch:
                    bid = int(bid_str)
                    if bid not in returned_ids and bid not in seen_spus:
                        unfetchable_ids.add(bid)
                added = record_spus(spus, idx, name, tag)
                rem_after = len([x for x in all_ids if x not in seen_spus and x not in unfetchable_ids])
                save_progress('Pass1.5_smoothRender', step_num, len(ordered_tabs), idx, name)
                print(f"  -> Cat[{idx}] {name} [batch={len(batch)}, rem={rem_after}]: +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

        export_current_excel()

        # PASS 2: Optional L2 sub-categories at page_index=0 (only if smooth hit soft cap, never after HTTP 403)
        pass2_candidates = []
        for m in ordered_tabs:
            cinfo = cat_map.get(str(m['idx'])) or {}
            all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
            missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
            sub_tags_list = cinfo.get('sub_tags') or m.get('sub_tags') or []
            sub_left = sum(1 for s_i, st in enumerate(sub_tags_list) if s_i > 0 and st.get('name') != '全部' and not st.get('allSortedSpuId'))
            if len(missing_ids) >= 20 and sub_left > 0:
                pass2_candidates.append((len(missing_ids) / sub_left, len(missing_ids), m))
        pass2_candidates.sort(key=lambda item: (-item[0], -item[1]))

        for step_num, (_, _, m) in enumerate(pass2_candidates, 1):
            idx, name, tag = m['idx'], m['name'], m['tag']
            cinfo = cat_map.get(str(idx)) or {}
            all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
            sub_tags_list = cinfo.get('sub_tags') or m.get('sub_tags') or []
            cinfo['sub_tags'] = sub_tags_list
            for s_idx, st in enumerate(sub_tags_list):
                if s_idx == 0 or st.get('name') == '全部' or st.get('allSortedSpuId'):
                    continue
                if not [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]:
                    break
                if args.max_sputag_daily > 0 and get_daily_count('sputag') >= args.max_sputag_daily:
                    print(f"[DAILY_QUOTA_GUARD] spuTagProducts reached daily soft cap ({get_daily_count('sputag')}/{args.max_sputag_daily}). Saving and stopping cleanly.", flush=True)
                    export_current_excel()
                    out_f.close()
                    sys.exit(44)
                st_name = st.get('name', '')
                req_counter += 1
                await polite_sleep(req_counter, ts_json, endpoint='sputag_p0', export_cb=export_current_excel, **pacing_kw)
                msg_seq, _ = await verify_store_ready_or_stop(ws, msg_seq, actual_poi_id, args.poi_name, on_stop=export_current_excel)
                inc_daily_count('sputag')

                msg_seq += 1
                res = await eval_js(ws, msg_seq, f'''
                (async () => {{
                    const p = getCurrentPages()[0];
                    const c = p._getCachedData({idx}, {s_idx});
                    try {{
                        await p.requestSpusAndSortedIds({idx}, {s_idx}, c.tag, c.type, 0, true);
                    }} catch (err) {{
                        return {{error: String(err.message || err)}};
                    }}
                    const c2 = p._getCachedData({idx}, {s_idx});
                    return {{allSortedSpuId: c2.allSortedSpuId || [], spus: c2.spus || []}};
                }})()
                ''')
                if res and res.get('error'):
                    handle_stop_error(f"Pass2 Cat[{idx}] Sub[{s_idx}] {st_name}", str(res['error']))

                st['allSortedSpuId'] = [int(x) for x in ((res or {}).get('allSortedSpuId') or [])]
                added = record_spus((res or {}).get('spus') or [], idx, name, tag)
                with open(cat_map_json, 'w', encoding='utf-8') as cf:
                    json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                save_progress('Pass2_L2_p0', step_num, len(ordered_tabs), idx, f"{name}/{st_name}")
                print(f"  -> Cat[{idx}] {name} / Sub[{s_idx}] {st_name}: +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

        export_current_excel()
        out_f.close()
        all_discovered_ids = {int(sid) for v_cat in cat_map.values() for sid in (v_cat.get('allSortedSpuId') or [])}
        missing_final = len([sid for sid in all_discovered_ids if sid not in seen_spus])
        print(
            f"[DONE] {actual_poi_name}: collected {len(seen_spus)} unique SPUs "
            f"(remaining_in_l1={missing_final}) in {req_counter} requests.",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description='Meituan H5 Store Catalog Collector (Polite-Pacing Compliant Edition)')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port (default: 9222)')
    parser.add_argument('--inspect-only', action='store_true', help='Only inspect open Meituan H5 store tab and exit')
    parser.add_argument('--poi-id-str', type=str, default=None, help='Expected poi_id_str')
    parser.add_argument('--poi-name', type=str, default=None, help='Expected store name substring')
    parser.add_argument('--out-dir', type=str, default=None, help='Output directory for spus.jsonl, categories.json, timestamps.json')
    parser.add_argument('--excel-out-dir', type=str, default=None, help='Directory for exported {MMDD}{店铺名称}.xlsx')
    parser.add_argument('--date-mmdd', type=str, default=None, help='Optional MMDD override for Excel filename')
    parser.add_argument('--min-delay', type=float, default=68.0, help='Minimum delay between requests in seconds (default: 68)')
    parser.add_argument('--max-delay', type=float, default=88.0, help='Maximum delay between requests in seconds (default: 88)')
    parser.add_argument('--hourly-cap', type=int, default=38, help='Max requests per 62-minute window (default: 38)')
    parser.add_argument('--wave-size', type=int, default=30, help='Requests per wave before deep pause (default: 30)')
    parser.add_argument('--wave-rest-sec', type=float, default=600.0, help='Deep pause duration in seconds after each wave (default: 600)')
    parser.add_argument('--max-sputag-daily', type=int, default=92, help='Daily soft cap for spuTagProducts(p=0) (default: 92)')
    parser.add_argument('--max-smooth-daily', type=int, default=85, help='Daily soft cap for requestSmoothSpus(batch=30) (default: 85)')
    args = parser.parse_args()
    asyncio.run(run_crawler(args))


if __name__ == '__main__':
    main()
