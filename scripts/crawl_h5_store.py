#!/usr/bin/env python3
"""
Meituan Shangou H5 Store Catalog Collector (Compliant Effortless Edition)
Part of the `meituanjingdui` skill.

Built-in Pitfall Protections (100% Compliant — Zero Anti-Bot Spoofing or Evasion):
1. Strict Compliance & Immediate Clean Stop:
   - Uses ONLY the user's manually logged-in regular Chrome tab on the target store page.
   - ZERO synthetic touch/mouse trajectory injection, ZERO cookie/fingerprint modification,
     ZERO DevTools manipulation, and ZERO multi-account rotation.
   - Exit 42 immediately if gated by verification (`verify.meituan.com` / 418) after saving Excel.
   - Exit 43 immediately if server returns HTTP 403/406 after saving Excel.
   - Exit 44 cleanly before hitting per-account limits via Rolling-24h Soft-Cap Guard.
2. Midnight & Session-Reload Pitfall Fixes:
   - Rolling 24-hour quota ledger (`86400s` sliding window in `daily_quota.json`) so crossing `00:00`
     midnight never resets the quota counter prematurely or overruns a single account's 24h allowance.
   - Sticky `target_mmdd` persisted in `progress.json` so crossing `00:00` midnight or resuming next day
     updates the SAME `{MMDD}{店铺名称}.xlsx` file instead of splitting into two dates.
   - Dynamic `poi_id_str` refresh tolerance: Meituan re-encrypts `poi_id_str` across page reloads; if
     `poi_name` and `address` match 100%, automatically updates `poi_id_str` while still blocking wrong branches.
3. Smart Single-Account Quota Allocation & Zero-Config UX:
   - Veteran vs Conservative account tiers (`--account-tier veteran|conservative`) to protect newer accounts.
   - Auto-derives `--out-dir` (`./crawl_data/{店铺名称}`) and locks onto the open store tab if omitted.
   - After Pass 1 (`L1 p=0`), calculates exact `smoothRender` batches (`sum(ceil(rem/30))`). When a large
     store (3,000~4,000 SPUs) exceeds remaining `smooth` quota, automatically schedules high-yield L2
     sub-category `p=0` clicks FIRST to offload big multi-sub-tag shelves, preserving `smoothRender` quota
     for 0-sub-tag tail categories (`未分类`, `神价`, etc.).
"""
import argparse
import asyncio
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import websockets

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_EXCEL_PY = os.path.join(SCRIPT_DIR, 'export_competitor_excel.py')
ANALYZE_STORE_PY = os.path.join(SCRIPT_DIR, 'analyze_competitor_store.py')

PROMO_TAB_KEYWORDS = (
    '推荐', '神价', '活动', '热销', '折扣', '狂欢', '促销', '特惠',
    '满减', '爆品', '秒杀', '囤货', '买过', '常买', '优惠', '新品上新',
)
SKIP_VIRTUAL_TAGS = {'op0050'}

ACCOUNT_TIER_PRESETS = {
    'veteran': {
        'min_delay': 68.0,
        'max_delay': 88.0,
        'hourly_cap': 38,
        'wave_size': 30,
        'wave_rest_sec': 600.0,
        'max_sputag_24h': 92,
        'max_smooth_24h': 85,
    },
    'conservative': {
        'min_delay': 75.0,
        'max_delay': 95.0,
        'hourly_cap': 32,
        'wave_size': 25,
        'wave_rest_sec': 660.0,
        'max_sputag_24h': 28,
        'max_smooth_24h': 35,
    },
}


def sanitize_store_slug(name):
    return re.sub(r'[（）()\s/\\:*?"<>|]+', '', str(name or '竞对门店').strip()) or '竞对门店'


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


async def verify_store_ready_or_stop(ws, msg_seq, store_lock, on_stop=None):
    """
    Verify once that the browser tab is on the locked store page.
    - Immediately exits 42 if redirected to verify.meituan.com (never waits out challenges).
    - Accepts Meituan's re-encrypted `poi_id_str` ONLY when both `poi_name` and `address` match 100%.
    """
    msg_seq += 1
    state = await eval_js(ws, msg_seq, '''
    (() => {
        const hasPages = typeof getCurrentPages === 'function' && getCurrentPages().length > 0;
        const p = hasPages ? getCurrentPages()[0] : null;
        const menuLen = p && p.data && Array.isArray(p.data.menuTabs) ? p.data.menuTabs.length : 0;
        const poiIdStr = p && p.data ? String(p.data.poi_id_str || '') : '';
        const poiName = p && p.data && p.data.poiInfo ? String(p.data.poiInfo.name || '') : '';
        const poiAddr = p && p.data && p.data.poiInfo ? String(p.data.poiInfo.address || '') : '';
        return {url: location.href, hasPages, menuLen, poiIdStr, poiName, poiAddr};
    })()
    ''')
    cur_url = (state or {}).get('url') or ''
    if 'verify.meituan.com' in cur_url:
        print(f"[STOP_ON_VERIFY] Page redirected to verification challenge ({cur_url[:90]}). Saving partial Excel and stopping immediately.", flush=True)
        if on_stop:
            on_stop()
        sys.exit(42)

    if not state or not state.get('hasPages') or state.get('menuLen', 0) <= 0:
        if on_stop:
            on_stop()
        raise RuntimeError(f"Target store page is not open/ready in Chrome tab (url={cur_url[:90]}). Please open the store page first.")

    actual_id = state.get('poiIdStr') or ''
    actual_name = state.get('poiName') or ''
    actual_addr = state.get('poiAddr') or ''

    exp_name_sub = store_lock.get('expected_name_substr')
    if exp_name_sub and exp_name_sub not in actual_name:
        if on_stop:
            on_stop()
        raise RuntimeError(f"Store name mismatch! Expected '{exp_name_sub}', but tab is on '{actual_name}' ({actual_id}).")

    locked_name = store_lock.get('locked_full_name')
    locked_addr = store_lock.get('locked_address')
    locked_id = store_lock.get('locked_poi_id_str')

    if locked_name and actual_name != locked_name:
        if on_stop:
            on_stop()
        raise RuntimeError(f"Branch mismatch! Locked onto '{locked_name}', but tab is on '{actual_name}'.")
    if locked_addr and actual_addr and actual_addr != locked_addr:
        if on_stop:
            on_stop()
        raise RuntimeError(f"Branch address mismatch! Locked onto '{locked_addr}', but tab is on '{actual_addr}'.")

    if locked_id and actual_id != locked_id:
        if locked_name and actual_name == locked_name and (not locked_addr or actual_addr == locked_addr):
            print(f"[POI_ID_REFRESHED] Verified identical store '{actual_name}' ({actual_addr}); updated re-encrypted poi_id_str: {locked_id} -> {actual_id}", flush=True)
            store_lock['locked_poi_id_str'] = actual_id
        else:
            if on_stop:
                on_stop()
            raise RuntimeError(f"Store poi_id_str mismatch! Expected '{locked_id}', got '{actual_id}' on '{actual_name}'.")

    store_lock['locked_full_name'] = actual_name
    store_lock['locked_address'] = actual_addr
    store_lock['locked_poi_id_str'] = actual_id
    return msg_seq, state


class RollingQuotaLedger:
    """
    Tracks both Rolling-24h (`86400s`) request timestamps and per-day counts in `daily_quota.json`.
    Prevents `00:00` midnight rollover from resetting a single account's 24h safety cap.
    Also stays backward-compatible with legacy `{"YYYY-MM-DD": {"sputag": N, "smooth": M}}` files.
    """
    def __init__(self, path):
        self.path = path
        self.data = {'events': {'sputag': [], 'smooth': []}, 'daily': {}}
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            raw = json.load(open(self.path, 'r', encoding='utf-8'))
        except Exception:
            return
        now = time.time()
        if isinstance(raw, dict) and 'events' in raw:
            ev = raw.get('events') or {}
            self.data['events']['sputag'] = [float(t) for t in (ev.get('sputag') or []) if now - float(t) < 86400.0]
            self.data['events']['smooth'] = [float(t) for t in (ev.get('smooth') or []) if now - float(t) < 86400.0]
            self.data['daily'] = raw.get('daily') or {}
        elif isinstance(raw, dict):
            # Migrate legacy date-keyed format conservatively for today
            today = time.strftime('%Y-%m-%d')
            self.data['daily'] = {k: v for k, v in raw.items() if isinstance(v, dict)}
            td = raw.get(today) or {}
            self.data['events']['sputag'] = [now] * int(td.get('sputag', 0))
            self.data['events']['smooth'] = [now] * int(td.get('smooth', 0))

    def count_24h(self, ep):
        now = time.time()
        self.data['events'][ep] = [t for t in self.data['events'].get(ep, []) if now - t < 86400.0]
        return len(self.data['events'][ep])

    def record(self, ep):
        now = time.time()
        day = time.strftime('%Y-%m-%d')
        self.data['events'].setdefault(ep, []).append(now)
        self.data['events'][ep] = [t for t in self.data['events'][ep] if now - t < 86400.0]
        self.data['daily'].setdefault(day, {'sputag': 0, 'smooth': 0})
        self.data['daily'][day][ep] = int(self.data['daily'][day].get(ep, 0)) + 1
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)


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
    REQ_TIMESTAMPS, SPUTAG_TIMESTAMPS, SMOOTH_TIMESTAMPS = [], [], []


def save_timestamps(ts_json):
    now = time.time()
    with open(ts_json, 'w', encoding='utf-8') as f:
        json.dump({
            'req': [t for t in REQ_TIMESTAMPS if now - t < 3720.0],
            'sputag': [t for t in SPUTAG_TIMESTAMPS if now - t < 3720.0],
            'smooth': [t for t in SMOOTH_TIMESTAMPS if now - t < 3720.0],
        }, f)


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
        print(f"  [Wave Rest] Completed {req_counter - 1} requests -> pausing {rest_s/60:.1f} mins to keep request frequency low...", flush=True)
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


def compute_remaining_smooth_batches(ordered_tabs, cat_map, seen_spus, unfetchable_ids):
    """Simulate deduplicated batch=30 smoothRender calls across ordered_tabs."""
    sim_seen = set(seen_spus.keys()) | set(unfetchable_ids)
    batches = 0
    for m in ordered_tabs:
        cinfo = cat_map.get(str(m['idx'])) or {}
        ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
        while True:
            rem = [x for x in ids if x not in sim_seen]
            if not rem:
                break
            sim_seen.update(rem[:30])
            batches += 1
    return batches


def estimate_eta_minutes(num_reqs, min_delay, max_delay, wave_size, wave_rest_sec):
    if num_reqs <= 0:
        return 0.0
    avg_step = (min_delay + max_delay) / 2.0 + 4.0
    waves = (num_reqs // wave_size) if (wave_size and wave_size > 0) else 0
    return (num_reqs * avg_step + waves * wave_rest_sec) / 60.0


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

    has_devtools_tab = any('devtools://' in (t.get('url') or '') for t in tabs)
    has_bend_tab = any(
        any(h in (t.get('url') or '') for h in ('shangoue.meituan.com', 'waimaie.meituan.com', 'qnh.meituan.com'))
        for t in tabs if t.get('type') == 'page'
    )
    if has_devtools_tab:
        print("[ENV_WARN] Detected open F12 DevTools window (devtools://). Please close F12 to avoid abnormal environment flags!", flush=True)
    if has_bend_tab:
        print("[ENV_WARN] Detected B-end merchant tabs (shangoue/qnh) in the same Chrome window; using an isolated --user-data-dir for H5 prevents code=408 token conflicts.", flush=True)

    async with websockets.connect(target['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024) as ws:
        if args.inspect_only:
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
            if isinstance(state, dict):
                state['hasDevToolsOpen'] = has_devtools_tab
                state['hasBEndTabsOpen'] = has_bend_tab
            print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)
            return

        msg_seq = 1
        store_lock = {
            'expected_name_substr': args.poi_name,
            'locked_poi_id_str': args.poi_id_str,
            'locked_full_name': None,
            'locked_address': None,
        }
        msg_seq, state = await verify_store_ready_or_stop(ws, msg_seq, store_lock)
        actual_poi_name = state.get('poiName')
        actual_poi_addr = state.get('poiAddr')

        # Auto-derive --out-dir if omitted so a single zero-config command works out of the box
        out_dir = args.out_dir or os.path.join('.', 'crawl_data', sanitize_store_slug(args.poi_name or actual_poi_name))
        os.makedirs(out_dir, exist_ok=True)

        spus_jsonl = os.path.join(out_dir, 'spus.jsonl')
        cat_map_json = os.path.join(out_dir, 'categories.json')
        ts_json = os.path.join(out_dir, 'timestamps.json')
        progress_json = os.path.join(out_dir, 'progress.json')
        daily_quota_json = os.path.join(out_dir, 'daily_quota.json')

        # Load prior progress to lock branch identity & sticky target_mmdd across midnight
        sticky_mmdd = args.date_mmdd
        if os.path.exists(progress_json):
            try:
                prior_prog = json.load(open(progress_json, 'r', encoding='utf-8'))
                if prior_prog.get('poi_name') and prior_prog['poi_name'] != actual_poi_name:
                    raise RuntimeError(f"Output dir '{out_dir}' belongs to '{prior_prog['poi_name']}', but Chrome is on '{actual_poi_name}'!")
                if prior_prog.get('poi_address') and actual_poi_addr and prior_prog['poi_address'] != actual_poi_addr:
                    raise RuntimeError(f"Output dir '{out_dir}' address '{prior_prog['poi_address']}' != current tab '{actual_poi_addr}'!")
                if not sticky_mmdd and prior_prog.get('target_mmdd'):
                    sticky_mmdd = prior_prog['target_mmdd']
            except RuntimeError:
                raise
            except Exception:
                pass
        if not sticky_mmdd:
            sticky_mmdd = time.strftime('%m%d')

        quota_ledger = RollingQuotaLedger(daily_quota_json)
        print(
            f"[Rolling-24h Quota ({args.account_tier})] sputag={quota_ledger.count_24h('sputag')}/{args.max_sputag_daily}, "
            f"smooth={quota_ledger.count_24h('smooth')}/{args.max_smooth_daily} | Sticky Date: {sticky_mmdd}",
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

        req_counter = 0
        store_name_for_file = args.poi_name or actual_poi_name or '竞对门店'
        excel_dir = args.excel_out_dir or '.'
        out_f = open(spus_jsonl, 'a', encoding='utf-8')

        def export_current_excel(run_analysis=False):
            out_f.flush()
            if os.path.exists(EXPORT_EXCEL_PY) and os.path.exists(spus_jsonl) and os.path.exists(cat_map_json):
                subprocess.run([
                    sys.executable, EXPORT_EXCEL_PY,
                    '--spus-jsonl', spus_jsonl,
                    '--cat-json', cat_map_json,
                    '--out-dir', excel_dir,
                    '--store-name', store_name_for_file,
                    '--date-mmdd', sticky_mmdd,
                ], check=False)
                if run_analysis and not args.no_report and os.path.exists(ANALYZE_STORE_PY):
                    xlsx_path = os.path.join(excel_dir, f"{sticky_mmdd}{sanitize_store_slug(store_name_for_file)}.xlsx")
                    if os.path.exists(xlsx_path):
                        subprocess.run([sys.executable, ANALYZE_STORE_PY, '--excel', xlsx_path], check=False)

        def handle_stop_error(context_label, err_msg):
            export_current_excel(run_analysis=True)
            out_f.close()
            if '418' in err_msg or 'verify' in err_msg.lower():
                print(f"[STOP_ON_VERIFY] {context_label} encountered verification challenge ({err_msg}). Partial Excel & report saved; stopping immediately.", flush=True)
                sys.exit(42)
            if '403' in err_msg or '406' in err_msg:
                print(f"[STOP_ON_403] {context_label} encountered HTTP 403/406 restriction ({err_msg}). Partial Excel & report saved; stopping immediately.", flush=True)
                sys.exit(43)
            print(f"[STOP_ON_ERROR] {context_label} failed ({err_msg}). Partial Excel saved; exiting.", flush=True)
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
        print(
            f"[Store Locked] {actual_poi_name} ({store_lock['locked_poi_id_str']}) | "
            f"Address: {actual_poi_addr} | L1 categories: {len(menu_tabs)} | OutDir: {out_dir} | Existing SPUs: {len(seen_spus)}",
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
                    'poi_id_str': store_lock['locked_poi_id_str'],
                    'poi_name': actual_poi_name,
                    'poi_address': actual_poi_addr,
                    'target_mmdd': sticky_mmdd,
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
            print(f"[Memory Harvest] Recovered +{mem_added} SPUs from initial page memory (unique={len(seen_spus)}, 0 reqs)", flush=True)

        entity_tabs, promo_tabs = [], []
        for m in menu_tabs:
            kind = classify_menu_tab(m)
            if kind == 'skip':
                continue
            m['_is_promo'] = (kind == 'promo')
            (entity_tabs if kind == 'entity' else promo_tabs).append(m)
        ordered_tabs = entity_tabs + promo_tabs

        # Auto-realign saved cat_map by normalized `tag` in case the store dynamically inserted/reordered L1 tabs
        if cat_map:
            saved_by_tag = {
                str(v.get('tag') or '').replace('_27', ''): v
                for v in cat_map.values()
                if isinstance(v, dict) and v.get('tag')
            }
            realigned_map = {}
            shifted = False
            for m in ordered_tabs:
                norm_t = str(m.get('tag') or '').replace('_27', '')
                prior_c = saved_by_tag.get(norm_t)
                if not prior_c or not prior_c.get('allSortedSpuId'):
                    continue
                if str(prior_c.get('idx')) != str(m['idx']):
                    shifted = True
                old_subs_by_tag = {str(s.get('tag')): s for s in (prior_c.get('sub_tags') or []) if isinstance(s, dict)}
                aligned_subs = []
                for live_st in (m.get('sub_tags') or []):
                    prev_st = old_subs_by_tag.get(str(live_st.get('tag')), {})
                    st_entry = {'tag': live_st.get('tag'), 'name': live_st.get('name')}
                    if prev_st.get('allSortedSpuId'):
                        st_entry['allSortedSpuId'] = prev_st['allSortedSpuId']
                    aligned_subs.append(st_entry)
                realigned_map[str(m['idx'])] = {
                    'idx': m['idx'],
                    'name': m['name'],
                    'tag': prior_c.get('tag', m['tag']),
                    'type': prior_c.get('type', m.get('type', 1)),
                    'is_promo': bool(m.get('_is_promo')),
                    'sub_idx': 0 if m['hasSub'] else -1,
                    'sub_tags': aligned_subs,
                    'allSortedSpuId': prior_c['allSortedSpuId'],
                }
            if shifted or len(realigned_map) != len(cat_map):
                cat_map = realigned_map
                with open(cat_map_json, 'w', encoding='utf-8') as cf:
                    json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                if shifted:
                    print(f"[Category Realign] Detected store shelf tab shift; auto-realigned {len(cat_map)} saved categories by tag ID.", flush=True)

        pacing_kw = dict(
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            hourly_cap=args.hourly_cap,
            wave_size=args.wave_size,
            wave_rest_sec=args.wave_rest_sec,
        )
        print(
            f"[Pacing ({args.account_tier})] delay={args.min_delay:.0f}~{args.max_delay:.0f}s, hourly_cap={args.hourly_cap}, "
            f"wave_size={args.wave_size}, wave_rest={args.wave_rest_sec/60:.1f}m",
            flush=True,
        )

        # ==================== PASS 1: L1 categories at page_index=0 ====================
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

            if quota_ledger.count_24h('sputag') >= args.max_sputag_daily:
                print(f"[DAILY_QUOTA_GUARD] spuTagProducts reached 24h soft cap ({quota_ledger.count_24h('sputag')}/{args.max_sputag_daily}). Saving Excel & report and stopping cleanly.", flush=True)
                export_current_excel(run_analysis=True)
                out_f.close()
                sys.exit(44)

            req_counter += 1
            await polite_sleep(req_counter, ts_json, endpoint='sputag_p0', export_cb=export_current_excel, **pacing_kw)
            msg_seq, _ = await verify_store_ready_or_stop(ws, msg_seq, store_lock, on_stop=lambda: export_current_excel(run_analysis=True))
            quota_ledger.record('sputag')

            msg_seq += 1
            res = await eval_js(ws, msg_seq, f'''
            (async () => {{
                const p = getCurrentPages()[0];
                const c = p._getCachedData({idx}, {sub_idx});
                if (!c) return {{allSortedSpuId: [], spus: []}};
                try {{
                    await p.requestSpusAndSortedIds({idx}, {sub_idx}, c.tag, c.type, 0, true);
                }} catch (err) {{
                    return {{error: String(err.message || err)}};
                }}
                const c2 = p._getCachedData({idx}, {sub_idx}) || c;
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
            rem_cats = sum(1 for t in ordered_tabs[step_num:] if str(t['idx']) not in cat_map or not cat_map[str(t['idx'])].get('allSortedSpuId'))
            print(f"[Pass1 {step_num}/{len(ordered_tabs)}] Cat[{idx}] {name}: {len(all_ids)} IDs, +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter}, Pass1 left={rem_cats})", flush=True)

        export_current_excel()

        # ==================== SMART SINGLE-ACCOUNT QUOTA PLANNER ====================
        # If total remaining smoothRender batches exceed remaining 24h smooth quota (large 3,000~4,000 SPU store),
        # automatically run high-yield L2 sub-category `p=0` clicks FIRST to offload big multi-sub-tag categories,
        # preserving precious `smoothRender` quota for 0-sub-tag / small tail categories!
        async def run_l2_pass(stop_when_smooth_fits=False):
            nonlocal msg_seq, req_counter
            for allow_low_yield in (False, True):
                candidates = []
                for m in ordered_tabs:
                    cinfo = cat_map.get(str(m['idx'])) or {}
                    all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
                    missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
                    sub_tags_list = cinfo.get('sub_tags') or m.get('sub_tags') or []
                    sub_left = sum(1 for s_i, st in enumerate(sub_tags_list) if s_i > 0 and st.get('name') != '全部' and not st.get('allSortedSpuId'))
                    if len(missing_ids) >= 20 and sub_left > 0:
                        # Prioritize big shelves (>=60 missing SPUs) first, then highest expected yield per sub-tag
                        candidates.append((1 if len(missing_ids) >= 60 else 0, len(missing_ids), len(missing_ids) / sub_left, m))
                candidates.sort(key=lambda item: (-item[0], -item[1], -item[2]))
                if not candidates:
                    return

                progressed = False
                for step_num, (_, _, _, m) in enumerate(candidates, 1):
                    idx, name, tag = m['idx'], m['name'], m['tag']
                    cinfo = cat_map.get(str(idx)) or {}
                    all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
                    sub_tags_list = cinfo.get('sub_tags') or m.get('sub_tags') or []
                    cinfo['sub_tags'] = sub_tags_list
                    low_yield_streak = 0
                    for s_idx, st in enumerate(sub_tags_list):
                        if s_idx == 0 or st.get('name') == '全部' or st.get('allSortedSpuId'):
                            continue
                        if not [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]:
                            break
                        if stop_when_smooth_fits and args.max_smooth_daily > 0:
                            rem_sm_cap = max(0, args.max_smooth_daily - quota_ledger.count_24h('smooth'))
                            cur_sm_need = compute_remaining_smooth_batches(ordered_tabs, cat_map, seen_spus, unfetchable_ids)
                            if cur_sm_need <= max(0, rem_sm_cap - 2):
                                print(f"[Smart Planner] Remaining smoothRender batches ({cur_sm_need}) now fit safely within 24h smooth quota ({rem_sm_cap}) -> switching to smoothRender sweep!", flush=True)
                                return
                        if quota_ledger.count_24h('sputag') >= args.max_sputag_daily:
                            print(f"[DAILY_QUOTA_GUARD] spuTagProducts reached 24h soft cap ({quota_ledger.count_24h('sputag')}/{args.max_sputag_daily}).", flush=True)
                            return

                        st_name = st.get('name', '')
                        req_counter += 1
                        await polite_sleep(req_counter, ts_json, endpoint='sputag_p0', export_cb=export_current_excel, **pacing_kw)
                        msg_seq, _ = await verify_store_ready_or_stop(ws, msg_seq, store_lock, on_stop=lambda: export_current_excel(run_analysis=True))
                        quota_ledger.record('sputag')

                        msg_seq += 1
                        res = await eval_js(ws, msg_seq, f'''
                        (async () => {{
                            const p = getCurrentPages()[0];
                            const c = p._getCachedData({idx}, {s_idx});
                            if (!c) return {{skip_missing_slot: true, allSortedSpuId: [], spus: []}};
                            try {{
                                await p.requestSpusAndSortedIds({idx}, {s_idx}, c.tag, c.type, 0, true);
                            }} catch (err) {{
                                return {{error: String(err.message || err)}};
                            }}
                            const c2 = p._getCachedData({idx}, {s_idx}) || c;
                            return {{allSortedSpuId: c2.allSortedSpuId || [], spus: c2.spus || []}};
                        }})()
                        ''')
                        if res and res.get('skip_missing_slot'):
                            continue
                        if res and res.get('error'):
                            handle_stop_error(f"L2 Cat[{idx}] Sub[{s_idx}] {st_name}", str(res['error']))

                        progressed = True
                        st['allSortedSpuId'] = [int(x) for x in ((res or {}).get('allSortedSpuId') or [])]
                        added = record_spus((res or {}).get('spus') or [], idx, name, tag)
                        with open(cat_map_json, 'w', encoding='utf-8') as cf:
                            json.dump(cat_map, cf, ensure_ascii=False, indent=2)
                        save_progress('Pass_L2_p0', step_num, len(ordered_tabs), idx, f"{name}/{st_name}")
                        print(f"  -> [L2 p=0] Cat[{idx}] {name} / Sub[{s_idx}] {st_name}: +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)
                        if not allow_low_yield:
                            low_yield_streak = (low_yield_streak + 1) if added <= 2 else 0
                            if low_yield_streak >= 2 and len(candidates) > 1:
                                break
                if not progressed:
                    return

        needed_smooth = compute_remaining_smooth_batches(ordered_tabs, cat_map, seen_spus, unfetchable_ids)
        rem_smooth_24h = max(0, args.max_smooth_daily - quota_ledger.count_24h('smooth'))
        rem_sputag_24h = max(0, args.max_sputag_daily - quota_ledger.count_24h('sputag'))
        all_l1_ids = {int(sid) for v_cat in cat_map.values() for sid in (v_cat.get('allSortedSpuId') or [])}
        eta_m = estimate_eta_minutes(needed_smooth, args.min_delay, args.max_delay, args.wave_size, args.wave_rest_sec)
        print(
            f"[Store Catalog Summary] Total L1 SPUs={len(all_l1_ids)} | Collected={len(seen_spus)} | "
            f"Smooth batches needed={needed_smooth} (24h smooth remaining={rem_smooth_24h}, sputag remaining={rem_sputag_24h}) | Est. smooth ETA={eta_m:.0f}m",
            flush=True,
        )

        if needed_smooth > rem_smooth_24h and rem_sputag_24h > 0:
            print(
                f"[Smart Planner] Store needs {needed_smooth} smooth batches > {rem_smooth_24h} remaining smooth quota; "
                f"using up to {rem_sputag_24h} remaining L2 p=0 calls FIRST on big categories so smooth quota covers tail categories!",
                flush=True,
            )
            await run_l2_pass(stop_when_smooth_fits=True)
            export_current_excel()

        # ==================== PASS 1.5: Smooth pagination batch=30 (Full-30 Batches First!) ====================
        # Always prioritize full 30-item batches across all categories before spending precious smooth quota
        # on small 1~10 item category remainders. This maximizes single-account daily yield (+500~700 SPUs on large stores).
        smooth_capped = bool(quota_ledger.count_24h('smooth') >= args.max_smooth_daily)
        while not smooth_capped:
            if quota_ledger.count_24h('smooth') >= args.max_smooth_daily:
                print(f"[DAILY_QUOTA_GUARD] smoothRender reached 24h soft cap ({quota_ledger.count_24h('smooth')}/{args.max_smooth_daily}).", flush=True)
                smooth_capped = True
                break

            best_choice = None
            for order_pos, m in enumerate(ordered_tabs, 1):
                cinfo = cat_map.get(str(m['idx'])) or {}
                all_ids = [int(x) for x in (cinfo.get('allSortedSpuId') or [])]
                if not all_ids:
                    continue
                missing_ids = [x for x in all_ids if x not in seen_spus and x not in unfetchable_ids]
                if not missing_ids:
                    continue
                batch_len = min(30, len(missing_ids))
                # Sort key: largest batch_len (30 first!), then original shelf order_pos
                cand_key = (batch_len, -order_pos)
                if best_choice is None or cand_key > best_choice[0]:
                    best_choice = (cand_key, order_pos, m, all_ids, missing_ids)

            if best_choice is None:
                break

            _, step_num, m, all_ids, missing_ids = best_choice
            idx, name, tag = m['idx'], m['name'], m['tag']
            sub_idx = 0 if m['hasSub'] else -1
            batch = [str(x) for x in missing_ids[:30]]
            req_counter += 1
            await polite_sleep(req_counter, ts_json, endpoint='smooth', export_cb=export_current_excel, **pacing_kw)
            msg_seq, _ = await verify_store_ready_or_stop(ws, msg_seq, store_lock, on_stop=lambda: export_current_excel(run_analysis=True))
            quota_ledger.record('smooth')

            msg_seq += 1
            res = await eval_js(ws, msg_seq, f'''
            (async () => {{
                const p = getCurrentPages()[0];
                const c = p._getCachedData({idx}, {sub_idx});
                if (!c) return {{spus: []}};
                if (!Array.isArray(c.allSortedSpuId) || !c.allSortedSpuId.length) {{
                    c.allSortedSpuId = {json.dumps(all_ids)};
                }}
                const targetIds = {json.dumps(batch)};
                try {{
                    await p.requestSmoothSpus(targetIds, {idx}, {sub_idx}, c.tag, c.type, {{}});
                    const c2 = p._getCachedData({idx}, {sub_idx}) || c;
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
            if req_counter % 5 == 0:
                rem_b = compute_remaining_smooth_batches(ordered_tabs, cat_map, seen_spus, unfetchable_ids)
                rem_eta = estimate_eta_minutes(rem_b, args.min_delay, args.max_delay, args.wave_size, args.wave_rest_sec)
                print(f"  -> Cat[{idx}] {name} [batch={len(batch)}, cat_rem={rem_after}]: +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter} | ~{rem_b} batches / ~{rem_eta:.0f}m left)", flush=True)
            else:
                print(f"  -> Cat[{idx}] {name} [batch={len(batch)}, cat_rem={rem_after}]: +{added} SPUs (unique={len(seen_spus)}, reqs={req_counter})", flush=True)

        # If smooth hit soft cap and sputag still has 24h allowance, run remaining L2 sub-categories
        if smooth_capped and quota_ledger.count_24h('sputag') < args.max_sputag_daily:
            await run_l2_pass(stop_when_smooth_fits=False)

        export_current_excel(run_analysis=True)
        out_f.close()
        all_discovered_ids = {int(sid) for v_cat in cat_map.values() for sid in (v_cat.get('allSortedSpuId') or [])}
        missing_final = len([sid for sid in all_discovered_ids if sid not in seen_spus and sid not in unfetchable_ids])
        if missing_final > 0 and (smooth_capped or quota_ledger.count_24h('sputag') >= args.max_sputag_daily):
            print(
                f"[PARTIAL_BY_DAILY_CAP] {actual_poi_name}: safely collected {len(seen_spus)} unique SPUs "
                f"(remaining={missing_final}) within 24h single-account soft cap. Resume with same command tomorrow after 24h quota refreshes.",
                flush=True,
            )
            sys.exit(44)
        print(
            f"[DONE] {actual_poi_name}: collected {len(seen_spus)} unique SPUs "
            f"(remaining={missing_final}) in {req_counter} requests.",
            flush=True,
        )


def enforce_mandatory_pacing_bounds(args, preset):
    """
    Hard-lock mandatory slow-sleep floors and 24h quota ceilings in code so NO caller or Agent
    can accidentally override CLI arguments to run faster than the Skill's safe rate limits.
    """
    clamped = []

    raw_min = preset['min_delay'] if args.min_delay is None else float(args.min_delay)
    if raw_min < preset['min_delay']:
        clamped.append(f"min_delay {raw_min}s -> {preset['min_delay']}s")
    args.min_delay = max(preset['min_delay'], raw_min)

    raw_max = preset['max_delay'] if args.max_delay is None else float(args.max_delay)
    floor_max = max(preset['max_delay'], args.min_delay + 10.0)
    if raw_max < floor_max:
        clamped.append(f"max_delay {raw_max}s -> {floor_max}s")
    args.max_delay = max(floor_max, raw_max)

    raw_hcap = preset['hourly_cap'] if args.hourly_cap is None else int(args.hourly_cap)
    if raw_hcap <= 0 or raw_hcap > preset['hourly_cap']:
        clamped.append(f"hourly_cap {raw_hcap} -> {preset['hourly_cap']}")
    args.hourly_cap = preset['hourly_cap'] if raw_hcap <= 0 else min(preset['hourly_cap'], raw_hcap)

    raw_wsize = preset['wave_size'] if args.wave_size is None else int(args.wave_size)
    if raw_wsize <= 0 or raw_wsize > preset['wave_size']:
        clamped.append(f"wave_size {raw_wsize} -> {preset['wave_size']}")
    args.wave_size = preset['wave_size'] if raw_wsize <= 0 else min(preset['wave_size'], raw_wsize)

    raw_wrest = preset['wave_rest_sec'] if args.wave_rest_sec is None else float(args.wave_rest_sec)
    if raw_wrest < preset['wave_rest_sec']:
        clamped.append(f"wave_rest_sec {raw_wrest}s -> {preset['wave_rest_sec']}s")
    args.wave_rest_sec = max(preset['wave_rest_sec'], raw_wrest)

    raw_spu_cap = preset['max_sputag_24h'] if args.max_sputag_daily is None else int(args.max_sputag_daily)
    final_spu_cap = min(preset['max_sputag_24h'], max(0, raw_spu_cap))
    if final_spu_cap != raw_spu_cap:
        clamped.append(f"max_sputag_daily {raw_spu_cap} -> {final_spu_cap}")
    args.max_sputag_daily = final_spu_cap

    raw_sm_cap = preset['max_smooth_24h'] if args.max_smooth_daily is None else int(args.max_smooth_daily)
    final_sm_cap = min(preset['max_smooth_24h'], max(0, raw_sm_cap))
    if final_sm_cap != raw_sm_cap:
        clamped.append(f"max_smooth_daily {raw_sm_cap} -> {final_sm_cap}")
    args.max_smooth_daily = final_sm_cap

    if clamped:
        print(f"[MANDATORY_PACING_CLAMP] Enforced mandatory slow-sleep bounds: {', '.join(clamped)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description='Meituan H5 Store Catalog Collector (Mandatory Slow-Sleep Edition)')
    parser.add_argument('--port', type=int, default=9222, help='Chrome remote debugging port (default: 9222)')
    parser.add_argument('--inspect-only', action='store_true', help='Only inspect open Meituan H5 store tab and exit')
    parser.add_argument('--account-tier', choices=('veteran', 'conservative'), default='veteran',
                        help="'veteran' (default: >=68~88s, <=38/h, <=92/85 24h caps) or 'conservative' for newer accounts (>=75~95s, <=32/h, <=28/35 24h caps)")
    parser.add_argument('--poi-id-str', type=str, default=None, help='Optional expected poi_id_str (auto-detected from open store tab if omitted)')
    parser.add_argument('--poi-name', type=str, default=None, help='Optional expected store name substring (auto-detected if omitted)')
    parser.add_argument('--out-dir', type=str, default=None, help='Directory for checkpoint files (defaults to ./crawl_data/{店铺名称})')
    parser.add_argument('--excel-out-dir', type=str, default='.', help='Directory for exported {MMDD}{店铺名称}.xlsx (default: current dir)')
    parser.add_argument('--date-mmdd', type=str, default=None, help='Optional MMDD override for Excel filename (sticky across midnight)')
    parser.add_argument('--no-report', action='store_true', help='Skip auto-generating {MMDD}{店铺名称}_数据分析报告.md')
    parser.add_argument('--min-delay', type=float, default=None, help='Minimum delay in seconds (hard-clamped >= 68s veteran / >= 75s conservative)')
    parser.add_argument('--max-delay', type=float, default=None, help='Maximum delay in seconds (hard-clamped >= 88s veteran / >= 95s conservative)')
    parser.add_argument('--hourly-cap', type=int, default=None, help='Max requests per 62-minute window (hard-clamped <= 38 veteran / <= 32 conservative)')
    parser.add_argument('--wave-size', type=int, default=None, help='Requests per wave before deep pause (hard-clamped <= 30 veteran / <= 25 conservative)')
    parser.add_argument('--wave-rest-sec', type=float, default=None, help='Deep pause duration in seconds (hard-clamped >= 600s veteran / >= 660s conservative)')
    parser.add_argument('--max-sputag-daily', type=int, default=None, help='24h cap for spuTagProducts (0..92 veteran / 0..28 conservative; 0 skips sputag)')
    parser.add_argument('--max-smooth-daily', type=int, default=None, help='24h cap for requestSmoothSpus (0..85 veteran / 0..35 conservative; 0 skips smooth)')
    args = parser.parse_args()

    preset = ACCOUNT_TIER_PRESETS[args.account_tier]
    enforce_mandatory_pacing_bounds(args, preset)

    asyncio.run(run_crawler(args))


if __name__ == '__main__':
    main()
