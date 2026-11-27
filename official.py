"""
飯店官網空房查詢
-----------------
支援兩種官網訂房系統：
  toyoko : 東横INN 官網（直接讀官網公開的價格/空房資料，不需要瀏覽器）
  tripla : 用 tripla 訂房系統的官網（ルートイン、ダイワロイネット、JALシティ、レンブラントイン 等）
           用無頭瀏覽器（Playwright）打開官網的訂房結果頁，讀畫面上的結果。
  dormy  : ドーミーイン官網（同樣用無頭瀏覽器讀畫面）
  rjp    : reservation.jp 訂房系統（スマイルホテル 等，同樣用無頭瀏覽器）

每個函式回傳 dict 或 None：
  {"available": bool, "count": 方案數或 None, "url": 訂房頁網址}
  None 代表這次查詢失敗（網站沒回應等），不當成「沒空房」。
"""
import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------------------------------------------------------- 東横INN
def check_toyoko(hotel, checkin, checkout, people, log):
    code = hotel["code"]
    inp = {"0": {
        "json": {
            "hotelCodes": [code],
            "checkinDate": checkin + "T00:00:00.000Z",
            "checkoutDate": checkout + "T00:00:00.000Z",
            "numberOfPeople": people,
            "numberOfRoom": 1,
            "smokingType": "all",
        },
        "meta": {"values": {"checkinDate": ["Date"], "checkoutDate": ["Date"]}},
    }}
    url = ("https://www.toyoko-inn.com/api/trpc/hotels.availabilities.prices?batch=1&input="
           + urllib.parse.quote(json.dumps(inp, separators=(",", ":"))))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
        info = data[0]["result"]["data"]["json"]["prices"][code]
    except Exception as e:
        log(f"⚠️ 東横INN 官網查詢失敗：{e}")
        return None
    ok = bool(info.get("existEnoughVacantRooms")) and not info.get("isUnderMaintenance")
    page = (f"https://www.toyoko-inn.com/search/detail/{code}/"
            f"?start={checkin}&end={checkout}&room=1&people={people}")
    return {"available": ok, "count": None,
            "min_total": info.get("lowestPrice") or None, "url": page}


# ---------------------------------------------------------------- tripla
def tripla_url(hotel, checkin, checkout, people):
    q = {
        "code": hotel["code"],
        "checkin": checkin.replace("-", "/"),
        "checkout": checkout.replace("-", "/"),
        "type": "plan",
        "is_day_use": "false",
        "order": "recommended",
        "is_including_occupied": "false",
        "rooms": json.dumps([{"adults": people}], separators=(",", ":")),
        "mcp_currency": "JPY",
    }
    return hotel["base"].rstrip("/") + "/booking/result?" + urllib.parse.urlencode(q)


# ---------------------------------------------------------------- 需要瀏覽器的官網
# tripla / dormy / rjp 都用無頭瀏覽器打開官網頁面讀畫面。
# 為了加快速度，同時開好幾個分頁一起查（PARALLEL 個）。
PARALLEL = 4


async def _read_page(browser, url, done_js, settle_ms=0):
    """打開網址，等到 done_js 成立（最多 35 秒），回傳畫面文字。"""
    page = await browser.new_page(locale="ja-JP", user_agent=UA)
    try:
        await page.goto(url, timeout=60000)
        try:
            await page.wait_for_function(done_js, timeout=35000)
        except Exception:
            pass
        if settle_ms:
            await page.wait_for_timeout(settle_ms)
        return await page.inner_text("body")
    finally:
        await page.close()


async def _tripla(browser, hotel, checkin, checkout, people, log):
    url = tripla_url(hotel, checkin, checkout, people)
    page = await browser.new_page(locale="ja-JP", user_agent=UA)
    try:
        await page.goto(url, timeout=60000)
        # 一看到「検索結果」就結束；頁面載入中會短暫出現「見つかりませんでした」，
        # 所以要連續 5 秒都是「見つかりませんでした」才算真的沒房。最多等 35 秒。
        text, none_since = "", None
        for i in range(70):
            await page.wait_for_timeout(500)
            text = await page.inner_text("body")
            if re.search(r"検索結果[:：]\s*\d+", text):
                break
            if "見つかりませんでした" in text:
                none_since = none_since if none_since is not None else i
                if i - none_since >= 10:
                    break
            else:
                none_since = None
    finally:
        await page.close()
    m = re.search(r"検索結果[:：]\s*(\d+)", text)
    if m:
        n = int(m.group(1))
        return {"available": n > 0, "count": n, "min_total": None, "url": url}
    if "見つかりませんでした" in text:
        return {"available": False, "count": 0, "min_total": None, "url": url}
    log(f"⚠️ {hotel['name']} 官網畫面看不懂（可能改版了），這次略過")
    return None


async def _dormy(browser, hotel, checkin, checkout, people, log):
    """ドーミーイン官網：方案有標價（￥xx,xxx〜）＝有空房；價格顯示「-」＝沒空房。"""
    from datetime import date
    nights = (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days
    q = [("checkin", checkin.replace("-", "/")), ("number_of_nights", nights),
         ("search_by_tag", "plan"), ("keyword", hotel["keyword"]),
         ("number_of_rooms", 1), ("stock_check", "true"),
         ("number_of_adults[]", people)]
    url = "https://dormy-hotels.com/reserve/search-plan?" + urllib.parse.urlencode(q)
    text = await _read_page(browser, url,
                            "document.body.innerText.includes('条件にあったプラン')", 3000)
    if "条件にあったプラン" not in text:
        log(f"⚠️ {hotel['name']} 官網畫面看不懂（可能改版了），這次略過")
        return None
    prices = [int(p.replace(",", "")) for p in re.findall(r"￥\s*([\d,]+)\s*〜", text)]
    return {"available": bool(prices), "count": len(prices),
            "min_total": min(prices) if prices else None, "url": url}


async def _rjp(browser, hotel, checkin, checkout, people, log):
    """reservation.jp 系統（スマイルホテル 等）：「件数：N」＝有空房；「予約できる客室がありません」＝沒空房。"""
    q = [("checkin_date", checkin.replace("-", "")), ("checkout_date", checkout.replace("-", "")),
         ("adults", people), ("children", 0), ("rooms", 1), ("dayuseFlg", 0), ("sort", 2)]
    url = f"{hotel['base'].rstrip('/')}/ja/hotels/{hotel['slug']}/rooms?" + urllib.parse.urlencode(q)
    text = await _read_page(
        browser, url,
        "document.body.innerText.includes('件数') || "
        "document.body.innerText.includes('予約できる客室がありません')", 2000)
    if "予約できる客室がありません" in text:
        return {"available": False, "count": 0, "min_total": None, "url": url}
    m = re.search(r"件数[:：]\s*(\d+)", text)
    if m:
        n = int(m.group(1))
        return {"available": n > 0, "count": n, "min_total": None, "url": url}
    log(f"⚠️ {hotel['name']} 官網畫面看不懂（可能改版了），這次略過")
    return None


_CHECKERS = {"tripla": (_tripla, lambda h: "tripla:" + h["code"]),
             "dormy": (_dormy, lambda h: "dormy:" + h["keyword"]),
             "rjp": (_rjp, lambda h: "rjp:" + h["slug"])}


async def _browser_all(hotels, checkin, checkout, people, log):
    from playwright.async_api import async_playwright
    out = {}
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                executable_path=os.environ.get("CHROME_PATH") or None)
        except Exception as e:
            log(f"⚠️ 無法啟動瀏覽器（{e}）。請執行：python3 -m playwright install chromium")
            return out
        sem = asyncio.Semaphore(PARALLEL)

        async def one(h):
            fn, keyf = _CHECKERS[h["type"]]
            async with sem:
                try:
                    r = await fn(browser, h, checkin, checkout, people, log)
                except Exception as e:
                    log(f"⚠️ {h['name']} 官網查詢失敗：{e}")
                    r = None
            if r:
                out[keyf(h)] = {"name": h["name"], **r}

        await asyncio.gather(*(one(h) for h in hotels))
        await browser.close()
    return out


def check_all(hotels, checkin, checkout, people, log):
    """查全部官網，回傳 {key: {name, available, ...}}；查詢失敗的不會出現在結果裡。"""
    out = {}
    for h in hotels:
        if h.get("type") == "toyoko":
            r = check_toyoko(h, checkin, checkout, people, log)
            if r:
                out["toyoko:" + h["code"]] = {"name": h["name"], **r}
    browser_hotels = [h for h in hotels if h.get("type") in _CHECKERS]
    if browser_hotels:
        try:
            import playwright  # noqa
        except ImportError:
            log("ℹ️ 沒有安裝 Playwright，略過需要瀏覽器的官網")
            return out
        out.update(asyncio.run(_browser_all(browser_hotels, checkin, checkout, people, log)))
    return out


# ---------------------------------------------------------------- じゃらん（Jalan）
# 用車站的「周邊住宿」頁面查，一頁就能看到整區有空房的旅館。
# 這些頁面會列出整個「青森・浅虫・八甲田」大區，所以用 exclude 關鍵字濾掉八甲田山區等遠的地方。
JALAN_JS = r"""() => {
  const out = []; const seen = new Set();
  document.querySelectorAll('a[href*="/yad"]').forEach(a => {
    const m = a.href.match(/\/yad(\d+)\//); if (!m || seen.has(m[1])) return;
    const name = (a.innerText || '').trim().split('\n')[0]; if (!name || name.length < 2) return;
    seen.add(m[1]);
    let card = a;
    for (let i = 0; i < 10 && card.parentElement; i++) { card = card.parentElement; if (card.innerText.length > 300) break; }
    out.push({id: m[1], name: name, ctx: card.innerText.replace(/\n+/g, ' | ').slice(0, 600)});
  });
  return out;
}"""


def _jalan_dates(checkin, checkout):
    from datetime import date
    d1, d2 = date.fromisoformat(checkin), date.fromisoformat(checkout)
    return [("stayYear", d1.year), ("stayMonth", d1.month), ("stayDay", d1.day),
            ("stayCount", (d2 - d1).days)]


async def _jalan_area(browser, area, checkin, checkout, people, jcfg, log):
    q = _jalan_dates(checkin, checkout) + [("roomCount", 1), ("adultNum", people),
                                          ("dateUndecided", 0)]
    url = f"https://www.jalan.net/020000/STA_{area['station_code']}/?" + urllib.parse.urlencode(q)
    page = await browser.new_page(locale="ja-JP", user_agent=UA)
    try:
        await page.goto(url, timeout=60000)
        try:
            await page.wait_for_function(
                "document.body.innerText.includes('軒ありました') || "
                "document.body.innerText.includes('見つかりませんでした')", timeout=30000)
        except Exception:
            pass
        text = await page.inner_text("body")
        cards = await page.evaluate(JALAN_JS) if "軒ありました" in text else []
    finally:
        await page.close()
    if "見つかりませんでした" in text and "軒ありました" not in text:
        return []
    if "軒ありました" not in text:
        raise RuntimeError("Jalan 畫面看不懂（可能改版了）")
    allow_area = jcfg.get("allow_area_keyword", "青森・浅虫・八甲田")
    exclude = jcfg.get("exclude_keywords", [])
    found = []
    for c in cards:
        ctx = c["ctx"]
        if "エリア" in ctx and allow_area not in ctx:
            continue  # 別的大區（三沢、東津軽…）
        if any(k in c["name"] or k in ctx for k in exclude):
            continue
        plan = (f"https://www.jalan.net/yad{c['id']}/plan/?" +
                urllib.parse.urlencode(q))
        found.append({"id": c["id"], "name": c["name"], "area": area["name"], "url": plan})
    return found


async def _jalan_all(areas, checkin, checkout, people, jcfg, log):
    from playwright.async_api import async_playwright
    results, ok = {}, True
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
        for area in areas:  # 一區一區慢慢查，不同時開
            try:
                for h in await _jalan_area(browser, area, checkin, checkout, people, jcfg, log):
                    results.setdefault(h["id"], h)
            except Exception as e:
                log(f"⚠️ Jalan（{area['name']}）查詢失敗：{e}")
                ok = False
            await asyncio.sleep(3)
        await browser.close()
    return results, ok


def check_jalan(jcfg, checkin, checkout, people, log):
    """回傳 ({hotel_id: {...}}, 是否全部成功)。"""
    try:
        import playwright  # noqa
    except ImportError:
        return {}, False
    return asyncio.run(_jalan_all(jcfg["areas"], checkin, checkout, people, jcfg, log))
