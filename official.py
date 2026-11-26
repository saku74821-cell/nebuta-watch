"""
飯店官網空房查詢
-----------------
支援兩種官網訂房系統：
  toyoko : 東横INN 官網（直接讀官網公開的價格/空房資料，不需要瀏覽器）
  tripla : 用 tripla 訂房系統的官網（ルートイン、ダイワロイネット、JALシティ 等）
           用無頭瀏覽器（Playwright）打開官網的訂房結果頁，讀畫面上的結果。

每個函式回傳 dict 或 None：
  {"available": bool, "count": 方案數或 None, "url": 訂房頁網址}
  None 代表這次查詢失敗（網站沒回應等），不當成「沒空房」。
"""
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


class TriplaBrowser:
    """共用一個瀏覽器，查完全部再關掉。沒裝 Playwright 時 enabled=False。"""

    def __init__(self, log):
        self.log = log
        self.enabled = False
        self._pw = self._browser = None
        try:
            from playwright.sync_api import sync_playwright  # noqa
            self._sync_playwright = sync_playwright
            self.enabled = True
        except ImportError:
            log("ℹ️ 沒有安裝 Playwright，略過 tripla 系統的官網（ルートイン/ダイワロイネット/JALシティ）")

    def __enter__(self):
        if self.enabled:
            try:
                self._pw = self._sync_playwright().start()
                self._browser = self._pw.chromium.launch(
                    executable_path=os.environ.get("CHROME_PATH") or None)
            except Exception as e:
                self.log(f"⚠️ 無法啟動瀏覽器（{e}）。請執行：python3 -m playwright install chromium")
                self.enabled = False
        return self

    def __exit__(self, *a):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def check(self, hotel, checkin, checkout, people):
        if not self.enabled:
            return None
        url = tripla_url(hotel, checkin, checkout, people)
        page = self._browser.new_page(locale="ja-JP", user_agent=UA)
        try:
            page.goto(url, timeout=60000)
            # 頁面載入中會短暫出現「見つかりませんでした」，所以先等一下再判斷
            page.wait_for_timeout(6000)
            try:
                page.wait_for_function(
                    "document.body.innerText.includes('検索結果') || "
                    "document.body.innerText.includes('見つかりませんでした')",
                    timeout=25000)
            except Exception:
                pass
            text = page.inner_text("body")
            if "検索結果" not in text and "見つかりませんでした" in text:
                page.wait_for_timeout(5000)  # 再確認一次，避免誤判
                text = page.inner_text("body")
        except Exception as e:
            self.log(f"⚠️ {hotel['name']} 官網查詢失敗：{e}")
            return None
        finally:
            page.close()

        m = re.search(r"検索結果[:：]\s*(\d+)", text)
        if m:
            n = int(m.group(1))
            return {"available": n > 0, "count": n, "min_total": None, "url": url}
        if "見つかりませんでした" in text:
            return {"available": False, "count": 0, "min_total": None, "url": url}
        self.log(f"⚠️ {hotel['name']} 官網畫面看不懂（可能改版了），這次略過")
        return None


def check_all(hotels, checkin, checkout, people, log):
    """查全部官網，回傳 {key: {name, available, ...}}；查詢失敗的不會出現在結果裡。"""
    out = {}
    tripla = [h for h in hotels if h.get("type") == "tripla"]
    for h in hotels:
        if h.get("type") == "toyoko":
            r = check_toyoko(h, checkin, checkout, people, log)
            if r:
                out["toyoko:" + h["code"]] = {"name": h["name"], **r}
    if tripla:
        with TriplaBrowser(log) as br:
            for h in tripla:
                r = br.check(h, checkin, checkout, people)
                if r:
                    out["tripla:" + h["code"]] = {"name": h["name"], **r}
    return out
