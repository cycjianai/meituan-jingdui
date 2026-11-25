#!/usr/bin/env python3
"""
Meituan Shangou (H5 -> wx-shangou) Full-Store Anti-418 & Anti-403 Crawler
Part of the `meituanjingdui` skill.

Empirically Proven Architecture (Validated on 8,867-SPU / 11,848-SKU Supermarket):
1. Multi-Pass Native Request Ordering (STRICTLY FORBIDS `spuTagProducts(page_index >= 1)`!):
   - Pass 1: Fetch L1 category `page_index=0` (`requestSpusAndSortedIds(..., 0, true)`) to obtain
     `allSortedSpuId` + first 20 SPUs per category + all L2 `sub_tags`.
   - Pass 1.5 (Primary Harvester): Call `requestSmoothSpus(batch=30)` with explicit `c.allSortedSpuId`
     pre-injection (Meituan backend hard-caps returns at 30 SPUs/call) and UI thumbnail render sync.
   - Pass 2 (Safe Fallback if `smoothRender` hits 403): Click L2 sub-categories at `page_index=0`
     (`_lxSecondCategoryClick` + `_highlightTag` + `requestSpusAndSortedIds(..., 0, true)`).
   - NEVER calls `spuTagProducts(page_index >= 1)` — empirically proven across 3 accounts to trigger
     a hard 24h account 403 block at ~46-51 cumulative calls regardless of request delay.
2. Zero-418 / Zero-403 Pacing:
   - 58~76s interval per request (~50-53 req/hour), <= 2-3 reqs per 125s sliding window,
     <= 55 reqs per 62m per endpoint, <= 58 total reqs per 62m (timestamps persisted to disk).
3. C-End Token Self-Healing (`clean_verify_url_params`):
   - Co-existing B-end tabs (`shangoue.meituan.com`) periodically clear `.meituan.com` `token` cookie,
     causing `code=408`. Before every call, auto-restores `token` from `oops` / `mt_c_token` /
     `localStorage.shangou_user` via `Network.setCookie`.
4. Hard Circuit Breaker & Auto Fingerprint Sanitization on 403:
   - Never loops or polls after 403 (probing extends/resets cooldown). Immediately flushes data,
     exports the locked 27-column Excel, runs `prepare_clean_login.py` to wipe C-end device
     fingerprints across all subdomains (preserving B-end logins), and exits with code 43.
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
PREPARE_CLEAN_LOGIN_PY = os.path.join(SCRIPT_DIR, 'prepare_clean_login.py')
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
    """
    Dynamically classify any Meituan Shangou store's L1 menuTabs without store-specific index hardcoding:
    - skip: non-numeric virtual container tabs like `op0050` ("活动") that hold no direct product list
    - promo: virtual tabs (`new_op0080`, `god_price_tag`) or promotional aggregate categories
    - entity: standard numeric-tag shelf categories (crawled FIRST so every SPU maps to its real shelf L1)
    """
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
            'returnByValue': True
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


async def simulate_human_interaction(ws, msg_seq, is_category_switch=False):
    """
    Emit realistic DOM touch + mouse + scroll events inside JS so both:
    - H5guard (`bio/info/report`)
    - `/api/rohr.js` (`handleTouchMove`, `handleTap` -> `mT`, `tT`, `aT`)
    record genuine human touch/scroll trajectories.
    """
    msg_seq += 1
    await eval_js(ws, msg_seq, '''
    (async () => {
        const x0 = Math.floor(140 + Math.random() * 180);
        const y0 = Math.floor(380 + Math.random() * 220);
        const x1 = Math.floor(130 + Math.random() * 190);
        const y1 = Math.floor(180 + Math.random() * 180);
        const steps = 6;
        const p = typeof getCurrentPages === 'function' && getCurrentPages()[0];

        for (let i = 1; i <= steps; i++) {
            const t = i / steps;
            const cx = Math.floor(x0 + (x1 - x0) * t + (Math.random() * 6 - 3));
            const cy = Math.floor(y0 + (y1 - y0) * t + (Math.random() * 6 - 3));
            document.dispatchEvent(new MouseEvent('mousemove', {clientX: cx, clientY: cy, bubbles: true}));
            window.dispatchEvent(new MouseEvent('mousemove', {clientX: cx, clientY: cy, bubbles: true}));
            try {
                if (p && typeof p._onTouchMove === 'function') {
                    p._onTouchMove({touches: [{clientX: cx, clientY: cy}], changedTouches: [{clientX: cx, clientY: cy}]});
                }
            } catch (e) {}
            await new Promise(r => setTimeout(r, 35));
        }
        document.dispatchEvent(new MouseEvent('mousedown', {clientX: x1, clientY: y1, bubbles: true}));
        document.dispatchEvent(new MouseEvent('mouseup', {clientX: x1, clientY: y1, bubbles: true}));
        document.dispatchEvent(new MouseEvent('click', {clientX: x1, clientY: y1, bubbles: true}));
        window.dispatchEvent(new Event('scroll', {bubbles: true}));
        return true;
    })()
    ''')
    return msg_seq


async def clean_verify_url_params(ws, msg_seq):
    """
    1. Auto-heal C-end `.meituan.com` `token` cookie from `oops`/`mt_c_token`/`localStorage.shangou_user`
       in case a B-end `shangoue.meituan.com` tab cleared it.
    2. Strip residual verify callback params (`code=121044`, `request_code`, `response_code`).
    """
    msg_seq += 1
    tok = await eval_js(ws, msg_seq, '''
    (() => {
        let tok = "";
        try {
            const m = document.cookie.match(/(?:^|;\\s*)(?:oops|mt_c_token)=([^;]+)/);
            tok = m ? m[1] : "";
            if (!tok) {
                try {
                    const su = JSON.parse(localStorage.getItem("shangou_user") || "{}");
                    tok = (su.data && su.data.token) || "";
                } catch (e) {}
            }
            if (tok && !/(?:^|;\\s*)token=/.test(document.cookie)) {
                document.cookie = "token=" + tok + "; domain=.meituan.com; path=/; max-age=15552000; secure; samesite=none";
            }

            const u = new URL(location.href);
            let changed = false;
            for (const k of ['code', 'msg', 'response_code', 'request_code', 'responseCode', 'requestCode']) {
                if (u.searchParams.has(k)) {
                    u.searchParams.delete(k);
                    changed = true;
                }
            }
            if (changed) {
                history.replaceState(null, '', u.toString());
            }
            const p = typeof getCurrentPages === 'function' && getCurrentPages()[0];
            if (p && p.options) {
                for (const k of ['code', 'msg', 'response_code', 'request_code', 'responseCode', 'requestCode']) {
                    delete p.options[k];
                }
            }
            if (p && p.setData) {
                p.setData({'toast.show': false});
            }
        } catch (e) {}
        return tok;
    })()
    ''')
    if tok:
        msg_seq += 1
        await ws.send(json.dumps({
            'id': msg_seq,
            'method': 'Network.setCookie',
            'params': {
                'name': 'token',
                'value': tok,
                'domain': '.meituan.com',
                'path': '/',
                'secure': True,
                'sameSite': 'None'
            }
        }))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            if json.loads(raw).get('id') == msg_seq:
                break
    return msg_seq


async def wait_until_store_ready(ws, msg_seq, expected_poi_id_str=None, expected_poi_name=None):
    """Ensure tab is on the target store page and not on verify.meituan.com."""
    logged_wait = False
    while True:
        msg_seq += 1
        try:
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
        except Exception as e:
            state = {'url': f'err:{e}', 'hasPages': False, 'menuLen': 0}

        if (
            state
            and state.get('hasPages')
            and state.get('menuLen', 0) > 0
            and 'verify.meituan.com' not in state.get('url', '')
        ):
            actual_id = state.get('poiIdStr') or ''
            actual_name = state.get('poiName') or ''
            if expected_poi_id_str and actual_id != expected_poi_id_str:
                raise RuntimeError(
                    f"Store poi_id_str mismatch! Expected '{expected_poi_id_str}', but tab is on '{actual_name}' ({actual_id}). Aborting to prevent crawling wrong branch."
                )
            if expected_poi_name and expected_poi_name not in actual_name:
                raise RuntimeError(
                    f"Store name mismatch! Expected substring '{expected_poi_name}', but tab is on '{actual_name}' ({actual_id}). Aborting to prevent crawling wrong branch."
                )
            if logged_wait:
                print(f"[RESUMED] Store page ready again: {actual_name} ({actual_id})", flush=True)
                await asyncio.sleep(3.0)
            msg_seq = await clean_verify_url_params(ws, msg_seq)
            return msg_seq, state

        if not logged_wait:
            print(f"[WAIT_READY] Waiting for store page / verification completion (url={state.get('url', '')[:90]}...)", flush=True)
            logged_wait = True
        await asyncio.sleep(3.0)


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


async def adaptive_sleep(
    ws, msg_seq, req_counter, ts_json,
    endpoint='sputag_p0', is_category_switch=False, export_cb=None,
    min_delay=58.0, max_delay=76.0, hourly_cap=55, wave_size=0, wave_rest_sec=0.0
):
    """
    Empirically Proven Zero-418 / Zero-403 Pacing Model:
    - Applies to `sputag_p0` (native `page_index=0` clicks) and `smooth` (`requestSmoothSpus(batch=30)`):
      Default 58~76s interval (~50-53 reqs/hour, <= 2-3 reqs per 125s window).
      Supports ultra-conservative single-account overrides via CLI (--min-delay, --max-delay,
      --hourly-cap, --wave-size, --wave-rest-sec).
    """
    total_wait = random.uniform(min_delay, max_delay)
    chunks = 2
    for _ in range(chunks):
        await asyncio.sleep(total_wait / chunks)
        msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=is_category_switch)

    if export_cb and req_counter > 0 and req_counter % 3 == 0:
        export_cb()

    if wave_size > 0 and wave_rest_sec > 0 and req_counter > 1 and (req_counter - 1) % wave_size == 0:
        rest_s = random.uniform(wave_rest_sec * 0.9, wave_rest_sec * 1.15)
        if export_cb:
            export_cb()
        print(f"  [Wave Rest] Completed {req_counter - 1} requests -> deep natural rest {rest_s/60:.1f} mins to protect single-account quota...", flush=True)
        w_chunks = max(2, int(rest_s // 90))
        for _ in range(w_chunks):
            await asyncio.sleep(rest_s / w_chunks)
            msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=False)

    ep_list = SPUTAG_TIMESTAMPS if endpoint == 'sputag_p0' else SMOOTH_TIMESTAMPS
    ep_cap = min(55, int(hourly_cap))
    total_cap = min(58, max(ep_cap, int(hourly_cap) + 3))
    while True:
        now = time.time()
        in_2m = [t for t in REQ_TIMESTAMPS if now - t < 125.0]
        in_62m_ep = [t for t in ep_list if now - t < 3720.0]
        in_62m_total = [t for t in REQ_TIMESTAMPS if now - t < 3720.0]
        if len(in_2m) >= 3:
            wait_2m = 126.0 - (now - in_2m[0]) + random.uniform(4.0, 8.0)
            if wait_2m > 0:
                print(f"  [2m Sliding Guard] {len(in_2m)} reqs in last 125s, waiting {wait_2m:.1f}s...", flush=True)
                await asyncio.sleep(wait_2m)
                continue
        if len(in_62m_ep) >= ep_cap:
            wait_62m = 3725.0 - (now - in_62m_ep[0]) + random.uniform(20.0, 40.0)
            if wait_62m > 0:
                if export_cb:
                    export_cb()
                print(f"  [60m Endpoint Guard ({endpoint})] {len(in_62m_ep)} reqs in last 62m (cap {ep_cap}), resting {wait_62m/60:.1f} mins...", flush=True)
                chunks = max(1, int(wait_62m // 90))
                chunk = wait_62m / chunks
                for _ in range(chunks):
                    await asyncio.sleep(chunk)
                    msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=False)
                continue
        if len(in_62m_total) >= total_cap:
            wait_tot = 3725.0 - (now - in_62m_total[0]) + random.uniform(20.0, 40.0)
            if wait_tot > 0:
                if export_cb:
                    export_cb()
                print(f"  [60m Total Guard] {len(in_62m_total)} total reqs in last 62m (cap {total_cap}), resting {wait_tot/60:.1f} mins...", flush=True)
                chunks = max(1, int(wait_tot // 90))
                chunk = wait_tot / chunks
                for _ in range(chunks):
                    await asyncio.sleep(chunk)
                    msg_seq = await simulate_human_interaction(ws, msg_seq, is_category_switch=False)
                continue
        break

    now_done = time.time()
    REQ_TIMESTAMPS.append(now_done)
    ep_list.append(now_done)
    save_timestamps(ts_json)
    return msg_seq


def find_store_tab(tabs):
    page_tabs = [t for t in tabs if t.get('type') == 'page']
    for pattern in ('cactivityapi-sc.waimai.meituan.com', 'verify.meituan.com', 'wx-shangou.meituan.com', 'h5.waimai.meituan.com'):
        match = next((t for t in page_tabs if pattern in t.get('url', '')), None)
        if match:
            return match
    return None


def trigger_clean_login_script(port):
    if os.path.exists(PREPARE_CLEAN_LOGIN_PY):
        subprocess.run([sys.executable, PREPARE_CLEAN_LOGIN_PY, '--port', str(port)], check=False)


async def run_crawler(args):
    try:
        v = fetch_json(f'http://127.0.0.1:{args.port}/json/version')
        tabs = fetch_json(f'http://127.0.0.1:{args.port}/json/list')
    except (urllib.error.URLError, OSError) as e:
        print(f"[ERROR] Cannot reach Chrome remote debugging port {args.port}. Run prepare_clean_login.py or start Chrome with --remote-debugging-port={args.port}.", file=sys.stderr)
        raise SystemExit(2) from e

    # Set default download behavior at Browser level so downloaded files never become GUIDs
    async with websockets.connect(v['webSocketDebuggerUrl'], max_size=20 * 1024 * 1024) as bws:
        await bws.send(json.dumps({'id': 1, 'method': 'Browser.setDownloadBehavior', 'params': {'behavior': 'default'}}))
        await bws.recv()

    # Close any open DevTools tab to prevent H5guard debugger trap
    for t in tabs:
        if 'devtools://' in t.get('url', ''):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json/close/{t['id']}", timeout=2).read()
                print(f"[Init] Closed DevTools tab {t['id']} to prevent H5guard debugger trap", flush=True)
            except Exception:
                pass

    tabs = fetch_json(f'http://127.0.0.1:{args.port}/json/list')
    target = find_store_tab(tabs)
    if not target:
        raise RuntimeError(f"Could not find open Meituan H5 tab in Chrome on port {args.port}! Run prepare_clean_login.py first.")

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

    def load_daily_quota():
        if os.path.exists(daily_quota_json):
            try:
                return json.load(open(daily_quota_json, 'r', encoding='utf-8'))
            except Exception:
                pass
        return {}

    daily_quota = load_daily_quota()

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
        flush=True
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
        msg_seq, state = await wait_until_store_ready(ws, msg_seq, args.poi_id_str, args.poi_name)
        actual_poi_id = state.get('poiIdStr')
        actual_poi_name = state.get('poiName')
        store_name_for_file = args.poi_name or actual_poi_name or '竞对门店'

        def export_current_excel():
            if os.path.exists(EXPORT_EXCEL_PY) and os.path.exists(spus_jsonl) and os.path.exists(cat_map_json):
                subprocess.run(
                    [
                        sys.executable, EXPORT_EXCEL_PY,
                        '--spus-jsonl', spus_jsonl,
                        '--cat-json', cat_map_json,
                        '--out-dir', args.excel_out_dir or args.out_dir,
                        '--store-name', store_name_for_file,
                    ],
                    check=False,
                )

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
            flush=True
        )

        out_f = open(spus_jsonl, 'a', encoding='utf-8')
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
            all_discovered = set()
            for v_cat in cat_map.values():
                for sid in (v_cat.get('allSortedSpuId') or []):
                    all_discovered.add(int(sid))
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
                    'timestamp': time.time()
                }, pf, ensure_ascii=False, indent=2)

        def handle_fatal_403(context_label, err_msg):
            print(f"[STOP_ON_403] {context_label} hit {err_msg}. Halting IMMEDIATELY (no polling/retries).", flush=True)
            out_f.flush()
            export_current_excel()
            if not args.no_clean_on_403:
                print("[AUTO_SANITIZE] Running prepare_clean_login.py to wipe C-end device fingerprints (B-end safe)...", flush=True)
                trigger_clean_login_script(args.port)
            print("[ACTION_REQUIRED] 当前账号已触发 403 日度配额封禁（通常次日恢复）。请换干净新号重新登录；若同宽带 IP 已连封多号，建议切换手机 4G/5G 个人热点（严禁 VPN/机房代理）。登录后重跑同命令即可断点续采。", flush=True)
            sys.exit(43)

        # Harvest any SPUs already in Redux store memory before sending any request
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
        mem_added = 0
        for item in (mem_spus or []):
            mem_added += record_spus(item.get('spus'), item.get('idx'), item.get('name'), item.get('tag'))
        if mem_added:
            print(f"[Memory Harvest] Recovered +{mem_added} SPUs directly from initial page state (unique={len(seen_spus)})", flush=True)

        # Dynamic category ordering: entity shelf categories FIRST, promotional aggregate tabs LAST
        entity_tabs = []
        promo_tabs = []
        for m in menu_tabs:
            kind = classify_menu_tab(m)
            if kind == 'skip':
                continue
            m['_is_promo'] = (kind == 'promo')
            if kind == 'entity':
                entity_tabs.append(m)
            else:
                promo_tabs.append(m)
        ordered_tabs = entity_tabs + promo_tabs
        print(f"[Plan] Crawling {len(entity_tabs)} entity categories first, then {len(promo_tabs)} promo/virtual categories.", flush=True)

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
            flush=True
        )

        # PASS 1: Fetch L1 page_index=0 for every category (`allSortedSpuId` + initial 20 SPUs)
        for step_num, m in enumerate(ordered_tabs, 1):
            idx = m['idx']
            name = m['name']
            tag = m['tag']
            has_sub = m['hasSub']
            sub_idx = 0 if has_sub else -1
            str_idx = str(idx)
            is_promo = bool(m.get('_is_promo'))

            if str_idx in cat_map and cat_map[str_idx].get('allSortedSpuId'):
                updated_meta = False
                if m.get('sub_tags') and not cat_map[str_idx].get('sub_tags'):
                    cat_map[str_idx]['sub_tags'] = m['sub_tags']
                    updated_meta = True
                if 'is_promo' not in cat_map[str_idx]:
                    cat_map[str_idx]['is_promo'] = is_promo
                    updated_meta = True
                if updated_meta:
                    with open(cat_map_json, 'w', encoding='utf-8') as cf:
                        json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                continue

            # Check if already populated in page Redux memory (e.g. initial landing tab) to save quota
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
                    'idx': idx,
                    'name': name,
                    'tag': mem_cat.get('tag') or tag,
                    'type': mem_cat.get('type') or 1,
                    'is_promo': is_promo,
                    'sub_idx': sub_idx,
                    'sub_tags': m.get('sub_tags', []),
                    'allSortedSpuId': mem_cat['allSortedSpuId']
                }
                with open(cat_map_json, 'w', encoding='utf-8') as cf:
                    json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                save_progress('Pass1_L1_mem', step_num, len(ordered_tabs), idx, name)
                print(f"[Pass1 {step_num}/{len(ordered_tabs)}] Cat[{idx}] {name}: {len(mem_cat['allSortedSpuId'])} IDs loaded from memory (+{added_mem} SPUs, 0 reqs!)", flush=True)
                continue

            while True:
                if args.max_sputag_daily > 0 and get_daily_count('sputag') >= args.max_sputag_daily:
                    print(
                        f"[DAILY_QUOTA_GUARD] spuTagProducts reached safe daily soft cap ({get_daily_count('sputag')}/{args.max_sputag_daily}) -> "
                        "pausing cleanly BEFORE 403 so this account remains 100% healthy & unbanned!",
                        flush=True
                    )
                    out_f.flush()
                    export_current_excel()
                    out_f.close()
                    sys.exit(44)
                req_counter += 1
                msg_seq = await adaptive_sleep(ws, msg_seq, req_counter, ts_json, endpoint='sputag_p0', is_category_switch=True, export_cb=export_current_excel, **pacing_kw)
                msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id, args.poi_name)
                inc_daily_count('sputag')

                msg_seq += 1
                res = await eval_js(ws, msg_seq, f'''
                (async () => {{
                    const p = getCurrentPages()[0];
                    const c = p._getCachedData({idx}, {sub_idx});
                    try {{
                        if (typeof p._lxFirstCategoryClick === 'function') {{
                            p._lxFirstCategoryClick({idx});
                        }}
                        if (typeof p._highlightTag === 'function') {{
                            p._highlightTag({idx}, {sub_idx}, false);
                        }}
                        await p.requestSpusAndSortedIds({idx}, {sub_idx}, c.tag, c.type, 0, true);
                    }} catch (err) {{
                        return {{error: String(err.message || err)}};
                    }}
                    const c2 = p._getCachedData({idx}, {sub_idx});
                    try {{
                        if (c2.spus && c2.spus.length > 0 && typeof p.setList === 'function') {{
                            const renderObj = p.setList(0, Math.min(6, c2.spus.length), c2.spus, "smoothRender.showList", true);
                            p.setData(Object.assign({{"smoothRender.scrollTop": 0, "smoothRender.currentItemIndex": 0}}, renderObj));
                        }}
                    }} catch (e) {{}}
                    return {{
                        tag: c2.tag,
                        type: c2.type,
                        product_count: c2.product_count,
                        allSortedSpuId: c2.allSortedSpuId || [],
                        spus: c2.spus || []
                    }};
                }})()
                ''')

                if res and res.get('error'):
                    err_msg = str(res['error'])
                    print(f"[CIRCUIT_BREAKER] Pass1 Cat[{idx}] {name} spuTagProducts(p=0) error: {err_msg}", flush=True)
                    if '403' in err_msg or '406' in err_msg:
                        handle_fatal_403(f"Pass1 Cat[{idx}] {name}", err_msg)
                    out_f.flush()
                    export_current_excel()
                    msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id, args.poi_name)
                    await asyncio.sleep(25.0)
                    continue
                break

            all_ids = (res or {}).get('allSortedSpuId') or []
            spus = (res or {}).get('spus') or []
            added = record_spus(spus, idx, name, tag)
            cat_map[str_idx] = {
                'idx': idx,
                'name': name,
                'tag': (res or {}).get('tag', tag),
                'type': (res or {}).get('type', 1),
                'is_promo': is_promo,
                'sub_idx': sub_idx,
                'sub_tags': m.get('sub_tags', []),
                'allSortedSpuId': all_ids
            }
            with open(cat_map_json, 'w', encoding='utf-8') as cf:
                json.dump(cat_map, cf, ensure_ascii=False, indent=2)
            save_progress('Pass1_L1_p0', step_num, len(ordered_tabs), idx, name)
            print(f"[Pass1 {step_num}/{len(ordered_tabs)}] Cat[{idx}] {name}: {len(all_ids)} total IDs, +{added} initial SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

        export_current_excel()

        # PASS 1.5: High-efficiency `requestSmoothSpus(batch=30)` with seeded `c.allSortedSpuId` (30 SPUs per call)
        smooth_banned = False
        if args.max_smooth_daily > 0 and get_daily_count('smooth') >= args.max_smooth_daily:
            print(
                f"[DAILY_QUOTA_GUARD] smoothRender already at safe daily soft cap ({get_daily_count('smooth')}/{args.max_smooth_daily}) -> "
                "skipping Pass 1.5 today and switching to Pass 2 (L2 p=0)!",
                flush=True
            )
            smooth_banned = True
        for step_num, m in enumerate(ordered_tabs, 1):
            if smooth_banned:
                break
            idx = m['idx']
            name = m['name']
            tag = m['tag']
            has_sub = m['hasSub']
            sub_idx = 0 if has_sub else -1
            str_idx = str(idx)

            cinfo = cat_map.get(str_idx) or {}
            all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
            if not all_ids:
                continue

            while not smooth_banned:
                if args.max_smooth_daily > 0 and get_daily_count('smooth') >= args.max_smooth_daily:
                    print(
                        f"  [DAILY_QUOTA_GUARD] smoothRender reached safe daily soft cap ({get_daily_count('smooth')}/{args.max_smooth_daily}) -> "
                        "stopping smoothRender BEFORE 403 and switching to Pass 2 (L2 p=0)!",
                        flush=True
                    )
                    smooth_banned = True
                    break
                missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
                if not missing_ids:
                    break
                batch = [str(x) for x in missing_ids[:30]]
                req_counter += 1
                msg_seq = await adaptive_sleep(ws, msg_seq, req_counter, ts_json, endpoint='smooth', is_category_switch=False, export_cb=export_current_excel, **pacing_kw)
                msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id, args.poi_name)
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
                        if (typeof p._highlightTag === 'function' && p.data.activeTypeIndex !== {idx}) {{
                            p._highlightTag({idx}, {sub_idx}, false);
                        }}
                        await p.requestSmoothSpus(targetIds, {idx}, {sub_idx}, c.tag, c.type, {{}});
                        const c2 = p._getCachedData({idx}, {sub_idx});
                        const idSet = new Set(targetIds.map(Number));
                        const matched = (c2.spus || []).filter(s => s && idSet.has(Number(s.id)));
                        try {{
                            if (matched.length > 0 && typeof p.setList === 'function') {{
                                const renderObj = p.setList(0, Math.min(6, matched.length), matched, "smoothRender.showList", true);
                                p.setData(renderObj);
                            }}
                        }} catch (e) {{}}
                        return {{spus: matched}};
                    }} catch (err) {{
                        return {{error: String(err.message || err)}};
                    }}
                }})()
                ''')
                if res and res.get('error'):
                    err_msg = str(res['error'])
                    print(f"  [WARN] Cat[{idx}] {name} requestSmoothSpus(batch={len(batch)}) error: {err_msg}", flush=True)
                    out_f.flush()
                    export_current_excel()
                    if '403' in err_msg or '406' in err_msg:
                        print("  [FALLBACK] smoothRender hit 403/406 -> stopping smoothRender immediately and switching to Pass 2 (L2 p=0)!", flush=True)
                        smooth_banned = True
                        break
                    msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id, args.poi_name)
                    await asyncio.sleep(25.0)
                    continue

                spus = (res or {}).get('spus') or []
                returned_ids = {int(s['id']) for s in spus if isinstance(s, dict) and s.get('id')}
                for bid_str in batch:
                    bid = int(bid_str)
                    if bid not in returned_ids and bid not in seen_spus:
                        unfetchable_ids.add(bid)
                added = record_spus(spus, idx, name, tag)
                rem_after = len([x for x in all_ids if x not in seen_spus and x not in unfetchable_ids])
                save_progress('Pass1.5_smoothRender', step_num, len(ordered_tabs), idx, name)
                print(f"  -> Cat[{idx}] {name} [smoothRender batch={len(batch)}, rem={rem_after}]: +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

        export_current_excel()

        # PASS 2: Safe fallback — L2 sub-category clicks at `page_index=0` (NEVER uses page_index >= 1)
        # Prioritize highest-yield categories (highest missing SPUs per remaining L2 sub-tag) and skip tiny leftovers (<20)
        pass2_candidates = []
        for m in ordered_tabs:
            str_idx = str(m['idx'])
            cinfo = cat_map.get(str_idx) or {}
            all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
            missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
            sub_tags_list = cinfo.get('sub_tags') or m.get('sub_tags') or []
            sub_left = sum(1 for s_i, st in enumerate(sub_tags_list) if s_i > 0 and st.get('name') != '全部' and not st.get('allSortedSpuId'))
            if len(missing_ids) >= 20 and sub_left > 0:
                pass2_candidates.append((len(missing_ids) / sub_left, len(missing_ids), m))
        pass2_candidates.sort(key=lambda item: (-item[0], -item[1]))

        for step_num, (_, _, m) in enumerate(pass2_candidates, 1):
            idx = m['idx']
            name = m['name']
            tag = m['tag']
            str_idx = str(idx)

            cinfo = cat_map.get(str_idx) or {}
            all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
            if not all_ids:
                continue

            missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
            if len(missing_ids) < 20:
                continue

            sub_tags_list = cinfo.get('sub_tags') or m.get('sub_tags') or []
            cinfo['sub_tags'] = sub_tags_list
            for s_idx, st in enumerate(sub_tags_list):
                if s_idx == 0 or st.get('name') == '全部' or st.get('allSortedSpuId'):
                    continue
                missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
                if not missing_ids:
                    break
                if args.max_sputag_daily > 0 and get_daily_count('sputag') >= args.max_sputag_daily:
                    print(
                        f"[DAILY_QUOTA_GUARD] spuTagProducts reached safe daily soft cap ({get_daily_count('sputag')}/{args.max_sputag_daily}) -> "
                        "pausing cleanly BEFORE 403! Account remains 100% healthy & logged in for seamless next-day resume.",
                        flush=True
                    )
                    out_f.flush()
                    export_current_excel()
                    out_f.close()
                    sys.exit(44)
                st_name = st.get('name', '')
                req_counter += 1
                msg_seq = await adaptive_sleep(ws, msg_seq, req_counter, ts_json, endpoint='sputag_p0', is_category_switch=True, export_cb=export_current_excel, **pacing_kw)
                msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id, args.poi_name)
                inc_daily_count('sputag')

                msg_seq += 1
                res = await eval_js(ws, msg_seq, f'''
                (async () => {{
                    const p = getCurrentPages()[0];
                    const c = p._getCachedData({idx}, {s_idx});
                    try {{
                        if (typeof p._lxSecondCategoryClick === 'function') {{
                            p._lxSecondCategoryClick({idx}, {s_idx});
                        }}
                        if (typeof p._highlightTag === 'function') {{
                            p._highlightTag({idx}, {s_idx}, false);
                        }}
                        await p.requestSpusAndSortedIds({idx}, {s_idx}, c.tag, c.type, 0, true);
                    }} catch (err) {{
                        return {{error: String(err.message || err)}};
                    }}
                    const c2 = p._getCachedData({idx}, {s_idx});
                    try {{
                        if (c2.spus && c2.spus.length > 0 && typeof p.setList === 'function') {{
                            const renderObj = p.setList(0, Math.min(6, c2.spus.length), c2.spus, "smoothRender.showList", true);
                            p.setData(renderObj);
                        }}
                    }} catch (e) {{}}
                    return {{
                        allSortedSpuId: c2.allSortedSpuId || [],
                        spus: c2.spus || []
                    }};
                }})()
                ''')
                if res and res.get('error'):
                    err_msg = str(res['error'])
                    print(f"  [WARN] Cat[{idx}] {name} -> Sub[{s_idx}] {st_name} (p=0) error: {err_msg}", flush=True)
                    if '403' in err_msg or '406' in err_msg:
                        handle_fatal_403(f"Pass2 Cat[{idx}] Sub[{s_idx}] {st_name}", err_msg)
                    out_f.flush()
                    export_current_excel()
                    msg_seq, _ = await wait_until_store_ready(ws, msg_seq, actual_poi_id, args.poi_name)
                    await asyncio.sleep(25.0)
                    continue

                sub_spus = (res or {}).get('spus') or []
                sub_all_ids = [int(x) for x in ((res or {}).get('allSortedSpuId') or [])]
                st['allSortedSpuId'] = sub_all_ids
                added = record_spus(sub_spus, idx, name, tag)
                rem_after = len([x for x in all_ids if x not in seen_spus and x not in unfetchable_ids])
                with open(cat_map_json, 'w', encoding='utf-8') as cf:
                    json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                save_progress('Pass2_L2_p0', step_num, len(ordered_tabs), idx, f"{name}/{st_name}")
                print(f"  -> Cat[{idx}] {name} / Sub[{s_idx}] {st_name} (p=0, sub_total={len(sub_all_ids)}, rem={rem_after}): +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

        export_current_excel()
        out_f.close()
        all_discovered_ids = set()
        for v_cat in cat_map.values():
            for sid in (v_cat.get('allSortedSpuId') or []):
                all_discovered_ids.add(int(sid))
        missing_final = len([sid for sid in all_discovered_ids if sid not in seen_spus])
        print(
            f"[DONE] {actual_poi_name}: collected {len(seen_spus)} / {len(all_discovered_ids)} unique SPUs "
            f"(remaining={missing_final}) in {req_counter} requests!",
            flush=True
        )
        if missing_final > 0 and smooth_banned:
            print(
                f"[RESUME_TIP] smoothRender reached its per-account quota with {missing_final} SPUs remaining. "
                "Run prepare_clean_login.py, log in with a backup account (or wait until next day), and re-run the same command to finish Pass 1.5 automatically.",
                flush=True
            )


def main():
    parser = argparse.ArgumentParser(description='Meituan H5 Shangou Anti-418 & Anti-403 Store Crawler')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port (default: 9222)')
    parser.add_argument('--inspect-only', action='store_true', help='Only inspect open Meituan H5 tab (login status, store name, poi_id_str, category count) and exit')
    parser.add_argument('--poi-id-str', type=str, default=None, help='Expected poi_id_str (strongly recommended to prevent wrong branch)')
    parser.add_argument('--poi-name', type=str, default=None, help='Expected store name substring (used for verification & Excel filename)')
    parser.add_argument('--out-dir', type=str, default=None, help='Output directory for spus.jsonl, categories.json, timestamps.json')
    parser.add_argument('--excel-out-dir', type=str, default=None, help='Directory for exported {MMDD}{店铺名称}.xlsx (defaults to --out-dir)')
    parser.add_argument('--no-clean-on-403', action='store_true', help='Do not auto-run prepare_clean_login.py when 403 occurs')
    parser.add_argument('--min-delay', type=float, default=58.0, help='Minimum delay between requests in seconds (default: 58)')
    parser.add_argument('--max-delay', type=float, default=76.0, help='Maximum delay between requests in seconds (default: 76)')
    parser.add_argument('--hourly-cap', type=int, default=55, help='Max requests per 62-minute window per endpoint (default: 55)')
    parser.add_argument('--wave-size', type=int, default=0, help='Optional deep-rest wave size (0 = disabled)')
    parser.add_argument('--wave-rest-sec', type=float, default=0.0, help='Optional deep-rest duration in seconds after each wave')
    parser.add_argument('--max-sputag-daily', type=int, default=92, help='Safe daily soft cap for spuTagProducts(p=0) to stay well below ~112 403 cliff (default: 92, 0=disable)')
    parser.add_argument('--max-smooth-daily', type=int, default=85, help='Safe daily soft cap for requestSmoothSpus(batch=30) to stay below 90 403 cliff (default: 85, 0=disable)')
    args = parser.parse_args()
    asyncio.run(run_crawler(args))


if __name__ == '__main__':
    main()
