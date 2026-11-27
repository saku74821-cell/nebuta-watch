#!/usr/bin/env python3
"""
睡魔祭（青森ねぶた祭）空房監控
---------------------------------
用樂天旅遊官方「空房搜尋 API」(VacantHotelSearch) 定時查詢青森車站周邊的空房，
一出現「之前沒有、現在有」的空房，就推播到手機（ntfy）和／或寄 Email。

只用 Python 標準函式庫，不需要 pip install。

用法：
  python3 nebuta_watch.py            # 持續監控（依 config 的間隔）
  python3 nebuta_watch.py --list     # 查一次，把目前有空房的旅館全部列出來（不發通知）
  python3 nebuta_watch.py --once     # 查一次，有新空房就通知（給排程 / cron 用）
  python3 nebuta_watch.py --test-notify   # 發一則測試通知，確認手機/信箱收得到
"""
import argparse
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

import official

API_URL = "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("STATE_PATH") or os.path.join(HERE, "state.json")
REQUEST_GAP_SEC = 1.2  # 樂天 API 約每秒 1 次，保守一點


# ---------------------------------------------------------------- 小工具
def log(msg):
    print(f"[{datetime.now():%m/%d %H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def secret(env_name, cfg_value):
    """優先讀環境變數，沒有才用 config 裡的值。"""
    return os.environ.get(env_name) or cfg_value or ""


# ---------------------------------------------------------------- 樂天 API
def api_get(params, cfg):
    app_id = secret("RAKUTEN_APP_ID", cfg["rakuten"].get("application_id"))
    access_key = secret("RAKUTEN_ACCESS_KEY", cfg["rakuten"].get("access_key"))
    if not app_id or not access_key:
        raise RuntimeError("沒有樂天金鑰，略過樂天（在 config.json 或環境變數設定）")

    q = dict(params)
    q.update(applicationId=app_id, format="json", responseType="small")
    url = API_URL + "?" + urllib.parse.urlencode(q)
    # 2026 年起樂天新 API 會檢查 Referer：要跟樂天後台登記的「WebサイトURL」一致
    referer = (os.environ.get("RAKUTEN_REFERER") or cfg["rakuten"].get("referer")
               or "https://github.com/saku74821-cell/nebuta-watch")
    req = urllib.request.Request(
        url, headers={"accessKey": access_key, "User-Agent": "nebuta-watch/1.0",
                      "Referer": referer, "Origin": referer.rstrip("/").split("/", 3)[0]
                      + "//" + referer.split("/")[2]}
    )

    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # 樂天用 404 表示「沒有符合的空房」
                return {"hotels": [], "pagingInfo": {"pageCount": 0}}
            if e.code in (429, 500, 503):
                wait = 15 * (attempt + 1)
                log(f"⚠️ API {e.code}，{wait} 秒後重試")
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            hint = ""
            if e.code == 400:
                hint = "（參數錯誤：檢查日期格式、人數、hotel_nos）"
            elif e.code in (401, 403):
                hint = ("（金鑰錯誤，或樂天後台登記的 WebサイトURL 跟程式送出的 Referer 不一致："
                        f"{referer}）")
            raise RuntimeError(f"API HTTP {e.code}{hint}: {body}")
        except (urllib.error.URLError, TimeoutError) as e:
            wait = 10 * (attempt + 1)
            log(f"⚠️ 連線失敗（{e}），{wait} 秒後重試")
            time.sleep(wait)
    raise RuntimeError("API 重試多次仍失敗")


def _walk(obj, key):
    """在巢狀的 list/dict 裡找出所有名為 key 的值（依出現順序）。"""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            else:
                found.extend(_walk(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_walk(v, key))
    return found


def parse_hotels(data):
    """把 API 回應整理成 [{hotelNo, name, url, plans:[...], min_total}]。"""
    out = []
    for entry in data.get("hotels", []):
        basics = _walk(entry, "hotelBasicInfo")
        if not basics:
            continue
        b = basics[0]
        rooms = _walk(entry, "roomBasicInfo")
        charges = _walk(entry, "dailyCharge")
        plans = []
        for i, r in enumerate(rooms):
            c = charges[i] if i < len(charges) else {}
            total = c.get("total") or c.get("rakutenCharge")
            plans.append({
                "room": r.get("roomName", ""),
                "plan": r.get("planName", ""),
                "total": total,
                "url": r.get("reserveUrl") or b.get("planListUrl"),
            })
        totals = [p["total"] for p in plans if isinstance(p["total"], (int, float))]
        out.append({
            "hotelNo": b.get("hotelNo"),
            "name": b.get("hotelName", "?"),
            "url": b.get("planListUrl") or b.get("hotelInformationUrl"),
            "info_url": b.get("hotelInformationUrl"),
            "address": (b.get("address1") or "") + (b.get("address2") or ""),
            "plans": plans,
            "min_total": min(totals) if totals else b.get("hotelMinCharge"),
        })
    return out


def search_stay(cfg, stay):
    """查某一組入住/退房日期，回傳 {hotelNo: hotel}。"""
    g = cfg["guests"]
    base = {
        "checkinDate": stay["checkin"],
        "checkoutDate": stay["checkout"],
        "adultNum": g.get("adults", 1),
        "upClassNum": g.get("upper_elem_children", 0),
        "lowClassNum": g.get("lower_elem_children", 0),
        "roomNum": g.get("rooms", 1),
        "hits": 30,
        "sort": "+roomCharge",
    }
    if cfg.get("max_charge_per_night"):
        base["maxCharge"] = cfg["max_charge_per_night"]

    targets = []
    hotel_nos = [str(h) for h in cfg.get("hotel_nos") or []]
    if hotel_nos:  # 指定旅館模式（API 一次最多 15 間）
        for i in range(0, len(hotel_nos), 15):
            targets.append({"hotelNo": ",".join(hotel_nos[i:i + 15])})
    else:  # 範圍模式：以青森車站為中心
        a = cfg["area"]
        targets.append({
            "latitude": a["latitude"],
            "longitude": a["longitude"],
            "searchRadius": a.get("radius_km", 2.0),
            "datumType": 1,
        })

    results = {}
    exclude = cfg.get("exclude_keywords") or []
    for t in targets:
        page, page_count = 1, 1
        while page <= min(page_count, 10):
            data = api_get({**base, **t, "page": page}, cfg)
            page_count = (data.get("pagingInfo") or {}).get("pageCount", 1) or 0
            for h in parse_hotels(data):
                text = h["name"] + " ".join(p["room"] + p["plan"] for p in h["plans"])
                if any(k in text for k in exclude):
                    continue
                results[str(h["hotelNo"])] = h
            page += 1
            time.sleep(REQUEST_GAP_SEC)
    return results


# ---------------------------------------------------------------- 通知
def fmt_yen(v):
    return f"¥{int(v):,}" if isinstance(v, (int, float)) else "價格請見頁面"


def notify(cfg, title, lines, click_url=None, buttons=None):
    n = cfg.get("notify", {})
    body = "\n".join(lines)
    sent = False

    topic = secret("NTFY_TOPIC", n.get("ntfy_topic"))
    if len(body.encode("utf-8")) > 3800:  # ntfy 上限 4096 位元組
        body = body.encode("utf-8")[:3700].decode("utf-8", "ignore") + "\n…（內容太長，已截斷）"
    if topic:
        payload = {"topic": topic, "title": title, "message": body,
                   "priority": 5, "tags": ["rotating_light"]}
        if click_url:
            payload["click"] = click_url
        if buttons:
            payload["actions"] = [{"action": "view", "label": lbl[:30], "url": u}
                                  for lbl, u in buttons[:3]]
        server = n.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            server + "/", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        for attempt in range(3):
            try:
                urllib.request.urlopen(req, timeout=15).read()
                sent = True
                break
            except Exception as e:
                log(f"⚠️ ntfy 推播失敗（第 {attempt + 1} 次）：{e}")
                time.sleep(10 * (attempt + 1))

    em = n.get("email") or {}
    user = secret("SMTP_USER", em.get("smtp_user"))
    pw = secret("SMTP_PASSWORD", em.get("smtp_password"))
    to = em.get("to") or user
    if user and pw and to:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = user
        msg["To"] = to
        try:
            with smtplib.SMTP_SSL(em.get("smtp_host", "smtp.gmail.com"),
                                  int(em.get("smtp_port", 465)), timeout=20) as s:
                s.login(user, pw)
                s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
            sent = True
        except Exception as e:
            log(f"⚠️ Email 寄送失敗：{e}")

    if not sent:
        log("（沒有設定任何通知管道，或全部失敗，只印在畫面上）")
    return sent


# ---------------------------------------------------------------- 主流程
def guests_total(cfg):
    g = cfg["guests"]
    return (g.get("adults", 1) + g.get("upper_elem_children", 0)
            + g.get("lower_elem_children", 0))


def search_official(cfg, stay):
    """查各飯店官網。回傳 (有空房的 {key: hotel}, 這次有成功查到的 key 集合)。"""
    hotels = cfg.get("official_hotels") or []
    if not hotels:
        return {}, set()
    res = official.check_all(hotels, stay["checkin"], stay["checkout"],
                             guests_total(cfg), log)
    found = {}
    for k, r in res.items():
        if r["available"]:
            extra = f"（{r['count']} 個方案）" if r.get("count") else ""
            found["官網:" + k] = {
                "hotelNo": k, "name": f"【官網】{r['name']}{extra}",
                "url": r["url"], "plans": [], "min_total": r.get("min_total"),
            }
    return found, {"官網:" + k for k in res}


def _wrap(tag, res, ok, state, prefix):
    """把慢速來源的結果轉成統一格式；查詢成功時，之前有、這次沒出現的都算已確認沒房。"""
    found = {f"{tag}:{i}": {"hotelNo": i, "name": f"【{tag}】{h['name']}", "url": h["url"],
                            "plans": [], "min_total": None} for i, h in res.items()}
    queried = set(found)
    if ok:
        queried |= {k[len(prefix):] for k in state if k.startswith(f"{prefix}{tag}:")}
    return found, queried


def slow_due(cfg, state, name, default_min):
    scfg = cfg.get(name) or {}
    if not scfg.get("enabled"):
        return False
    last = state.get(f"_{name}_last", 0)
    return time.time() - last >= int(scfg.get("min_interval_min", default_min)) * 60


def slow_sources(cfg, stay, state, prefix, due):
    """Jalan / TSUNAGIYA / Airbnb：頻率較低的來源。"""
    found, queried, counts = {}, set(), []
    ci, co, people = stay["checkin"], stay["checkout"], guests_total(cfg)
    if due.get("jalan"):
        res, ok = official.check_jalan(cfg["jalan"], ci, co, people, log)
        f, q = _wrap("Jalan", {i: {"name": h["name"], "url": h["url"]} for i, h in res.items()},
                     ok, state, prefix)
        found.update(f); queried |= q; counts.append(f"Jalan {len(f)}")
    if due.get("airhost"):
        res, ok = official.check_airhost(cfg["airhost"], ci, co, people, log)
        f, q = _wrap(cfg["airhost"].get("name", "AirHost"), res, ok, state, prefix)
        found.update(f); queried |= q; counts.append(f"{cfg['airhost'].get('name')} {len(f)}")
    if due.get("airbnb"):
        g = cfg["guests"]
        kids = g.get("upper_elem_children", 0) + g.get("lower_elem_children", 0)
        res, ok = official.check_airbnb(cfg["airbnb"], ci, co, g.get("adults", 1), kids, log)
        f, q = _wrap("Airbnb", res, ok, state, prefix)
        found.update(f); queried |= q; counts.append(f"Airbnb {len(f)}")
    return found, queried, counts


def check_knt(cfg, state):
    """旅行社方案上架通知（不分日期，只通知一次）。"""
    kcfg = cfg["knt"]
    n = official.check_knt(kcfg, log)
    if n is None:
        return []
    log(f"KNT 睡魔祭方案：{n} 筆")
    if n > 0 and "KNT:listed" not in state:
        state["KNT:listed"] = datetime.now().isoformat(timespec="seconds")
        return [("旅行社", {"hotelNo": "KNT", "name": f"【KNT】睡魔祭套裝方案上架了（{n} 筆）",
                            "url": kcfg.get("page_url") or kcfg["url"], "plans": [],
                            "min_total": None}), ]
    return []


def check_once(cfg, state, send=True):
    """查所有日期組合；回傳新出現的空房清單，並更新 state。"""
    new_items, new_keys = [], []
    due = {}
    for name, dflt in (("jalan", 20), ("airhost", 30), ("airbnb", 60), ("knt", 360)):
        if slow_due(cfg, state, name, dflt):
            due[name] = True
            state[f"_{name}_last"] = time.time()
    if due.get("knt"):
        for item in check_knt(cfg, state):
            new_items.append(item)
            new_keys.append("KNT:listed")
    for stay in cfg["stays"]:
        label = stay.get("label") or f'{stay["checkin"]}→{stay["checkout"]}'
        prefix = f'{stay["checkin"]}|{stay["checkout"]}|'
        hotels, queried_ok = {}, set()

        try:  # 樂天
            rk = search_stay(cfg, stay)
            hotels.update(rk)
            rakuten_ok = True
        except RuntimeError as e:
            log(f"（樂天）{label}：{e}")
            rakuten_ok = False

        off, off_ok = search_official(cfg, stay)  # 官網
        hotels.update(off)
        queried_ok |= off_ok
        sl, sl_ok, counts = slow_sources(cfg, stay, state, prefix, due)
        hotels.update(sl)
        queried_ok |= sl_ok
        log(f"{label}：樂天 {len(hotels) - len(off) - len(sl)} 間、官網 {len(off)} 間"
            + "".join(f"、{c} 間" for c in counts) + " 有空房")

        current = {prefix + no for no in hotels}
        # 消失的空房從 state 移除 → 之後再出現會再通知一次
        # （只移除這次「有成功查到」的來源，查詢失敗的不動，避免重複通知）
        for k in list(state):
            if not k.startswith(prefix) or k in current:
                continue
            src = k[len(prefix):]
            if ":" in src:  # 官網／Jalan／TSUNAGIYA／Airbnb：只移除這次有成功查到的
                if src in queried_ok:
                    del state[k]
                continue
            if rakuten_ok:
                del state[k]
        for no, h in hotels.items():
            key = prefix + no
            if key not in state:
                state[key] = datetime.now().isoformat(timespec="seconds")
                new_items.append((label, h))
                new_keys.append(key)

    if new_items and send:
        lines, buttons = [], []
        # ntfy 一則最多 4096 位元組，超過會被拒絕 → 控制在約 3300 位元組內
        LIMIT, shown = 3300, 0
        for label, h in new_items:
            block = [f"【{label}】{h['name']}"]
            if h.get("min_total"):
                block.append(f"  最低 {fmt_yen(h['min_total'])}（第一晚・一間）")
            if h["plans"]:
                p = h["plans"][0]
                block.append(f"  {p['room']}｜{p['plan'][:40]}")
            block += [f"  {h['url']}", ""]
            if len("\n".join(lines + block).encode("utf-8")) > LIMIT:
                break
            lines += block
            shown += 1
            if len(buttons) < 3:
                buttons.append((h["name"], h["url"]))
        if len(new_items) > shown:
            lines.append(f"…另外還有 {len(new_items) - shown} 筆（打開 GitHub 的執行紀錄可看全部）")
        lines.append("空房通常很快被訂走，請盡快下訂。")
        for label, h in new_items:
            log(f"  新空房【{label}】{h['name']} {h['url']}")
        title = f"睡魔祭有空房了！{len(new_items)} 筆新空房"
        if notify(cfg, title, lines, click_url=new_items[0][1]["url"], buttons=buttons):
            log(f"🔔 已通知 {len(new_items)} 筆新空房")
        else:
            # 通知沒送出去：不要記成「已通知」，下一輪會再試一次
            for k in new_keys:
                state.pop(k, None)
            log(f"⚠️ 通知送不出去，{len(new_items)} 筆空房下一輪會再通知一次")
    return new_items


def run_auto(cfg, state):
    """白天（日本時間 day_hours_jst）連續監控；深夜只查一次。"""
    jst = timezone(timedelta(hours=9))
    start_h, end_h = cfg.get("day_hours_jst", [9, 23])
    interval = max(int(cfg.get("loop_interval_sec", 120)), 60)
    max_min = int(cfg.get("loop_max_minutes", 330))

    now = datetime.now(jst)
    if not (start_h <= now.hour < end_h):
        log(f"日本時間 {now:%H:%M}，深夜時段：只查一次")
        check_once(cfg, state)
        save_json(STATE_PATH, state)
        return

    day_end = now.replace(hour=end_h % 24, minute=0, second=0, microsecond=0)
    if day_end <= now:
        day_end += timedelta(days=1)
    deadline = min(time.time() + max_min * 60, day_end.timestamp())
    log(f"白天時段：連續監控到日本時間 {datetime.fromtimestamp(deadline, jst):%H:%M}，"
        f"每輪間隔 {interval} 秒")
    rounds = 0
    while True:
        t0 = time.time()
        try:
            check_once(cfg, state)
            save_json(STATE_PATH, state)
        except Exception as e:
            log(f"❌ 未預期錯誤：{e}")
        rounds += 1
        took = time.time() - t0
        wait = max(interval - took, 10)
        # 下一輪預估跑不完就結束，讓下一個排程接手
        if time.time() + wait + took > deadline:
            break
        time.sleep(wait)
    log(f"本次共查了 {rounds} 輪，結束")


def main():
    ap = argparse.ArgumentParser(description="睡魔祭空房監控")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--once", action="store_true", help="只查一次（給排程用）")
    ap.add_argument("--list", action="store_true", help="列出目前所有空房，不通知")
    ap.add_argument("--test-notify", action="store_true", help="發測試通知")
    ap.add_argument("--auto", action="store_true",
                    help="給 GitHub 用：白天連續監控（跑到晚上或時間上限），深夜只查一次")
    args = ap.parse_args()

    cfg = load_json(args.config, None)
    if cfg is None:
        sys.exit(f"❌ 找不到設定檔 {args.config}")

    if args.test_notify:
        ok = notify(cfg, "睡魔祭空房監控：測試通知",
                    ["收到這則就代表通知設定成功 🎉"], click_url="https://travel.rakuten.co.jp/")
        sys.exit(0 if ok else 1)

    if args.list:
        for stay in cfg["stays"]:
            label = stay.get("label") or f'{stay["checkin"]}→{stay["checkout"]}'
            try:
                hotels = search_stay(cfg, stay)
            except RuntimeError as e:
                log(f"（樂天）{e}")
                hotels = {}
            print(f"\n=== {label}（{stay['checkin']} → {stay['checkout']}）樂天 {len(hotels)} 間 ===")
            for no, h in sorted(hotels.items(), key=lambda x: x[1]["min_total"] or 9e9):
                print(f"  [{no}] {h['name']}  {fmt_yen(h['min_total'])}")
                print(f"        {h['url']}")
            res = official.check_all(cfg.get("official_hotels") or [], stay["checkin"],
                                     stay["checkout"], guests_total(cfg), log)
            print("  --- 官網 ---")
            for k, r in res.items():
                mark = "⭕ 有空房" if r["available"] else "✖ 沒空房／還沒開賣"
                print(f"  {r['name']}：{mark}")
        return

    state = load_json(STATE_PATH, {})
    if args.auto:
        run_auto(cfg, state)
        return
    if args.once:
        check_once(cfg, state)
        save_json(STATE_PATH, state)
        return

    interval = max(int(cfg.get("check_interval_sec", 300)), 120)
    log(f"開始監控，每 {interval // 60} 分鐘查一次（Ctrl+C 停止）")
    first = not state  # 第一次跑：已經有空房的也會通知一次
    while True:
        try:
            check_once(cfg, state)
            save_json(STATE_PATH, state)
        except Exception as e:  # 任何意外都不要讓程式停掉
            log(f"❌ 未預期錯誤：{e}")
        if first:
            log("（第一輪已把目前有空房的旅館通知過，之後只通知『新』出現的空房）")
            first = False
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已停止")
