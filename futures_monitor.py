import os
import datetime
import pytz
import requests
import yfinance as yf
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 1. 設定區
# ==========================================
LINE_TOKEN   = os.environ.get("LINE_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")

GSHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1OW7i2D8Auk6n3fnJPnbf4EOosEphe--NEASJjoSpVdg"
    "/export?format=csv&gid=0"
)

TW_TIMEZONE  = pytz.timezone("Asia/Taipei")
US_TIMEZONE  = pytz.timezone("America/New_York")

MXF_MULTIPLIER      = 10     # 微台指每點 10 元
ROLLOVER_WARN_DAYS  = 3      # 結算前幾個交易日提醒轉倉
CRASH_TW_PCT        = -2.5   # 台指急跌警示門檻
CRASH_US_PCT        = -1.5   # 美股急跌警示門檻
VIX_WARN            = 25     # VIX 警示門檻

# 判斷目前是日盤還是夜盤
# 日盤：08:45 ~ 13:45
# 夜盤：15:00 ~ 05:00（隔日）
def get_session():
    now = datetime.datetime.now(TW_TIMEZONE)
    h, m = now.hour, now.minute
    total = h * 60 + m
    if 8*60+45 <= total <= 13*60+45:
        return "DAY"    # 日盤
    elif total >= 15*60 or total <= 5*60:
        return "NIGHT"  # 夜盤
    return "CLOSED"     # 休市中


# ==========================================
# 2. 自動抓取保證金（期交所）
# ==========================================
def fetch_mxf_margin():
    print("💰 抓取期交所保證金公告...")
    DEFAULT_INIT, DEFAULT_MAINT = 17000, 13000
    try:
        r = requests.get(
            "https://www.taifex.com.tw/cht/5/margin_1",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
                "Referer": "https://www.taifex.com.tw/",
            },
            timeout=20, verify=False,
        )
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        for row in soup.find_all("tr"):
            if any(kw in row.get_text() for kw in ["微型臺股", "微型台股", "MXF"]):
                nums = []
                for col in row.find_all("td"):
                    txt = col.get_text(strip=True).replace(",", "")
                    try:
                        v = int(txt)
                        if 5000 < v < 500000:
                            nums.append(v)
                    except ValueError:
                        continue
                if len(nums) >= 2:
                    print("✅ 保證金 — 原始:" + str(nums[0]) + " 維持:" + str(nums[1]))
                    return nums[0], nums[1]
        print("⚠️ 找不到微台指保證金，使用預設值")
    except Exception as e:
        print("❌ 保證金失敗: " + str(e))
    return DEFAULT_INIT, DEFAULT_MAINT


# ==========================================
# 3. 自動抓取台灣假日（證交所 API）
# ==========================================
def fetch_tw_holidays():
    print("📅 抓取台灣假日...")
    holidays = set()
    now = datetime.datetime.now(TW_TIMEZONE)
    for year in [now.year, now.year + 1]:
        try:
            url = ("https://www.twse.com.tw/rwd/zh/holiday/holidaySchedule"
                   "?response=json&queryYear=" + str(year))
            data = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                timeout=15).json()
            if data.get("stat") == "OK":
                for item in data.get("data", []):
                    parts = item[0].strip().split("/")
                    if len(parts) == 3:
                        try:
                            holidays.add(
                                str(int(parts[0]) + 1911) + "-" + parts[1] + "-" + parts[2]
                            )
                        except ValueError:
                            continue
        except Exception as e:
            print("❌ " + str(year) + " 假日失敗: " + str(e))

    if not holidays:
        holidays = {
            "2025-01-01","2025-01-27","2025-01-28","2025-01-29","2025-01-30",
            "2025-01-31","2025-02-28","2025-04-03","2025-04-04","2025-05-01",
            "2025-05-30","2025-10-10","2026-01-01","2026-02-12","2026-02-13",
            "2026-02-16","2026-02-17","2026-02-18","2026-02-19","2026-02-20",
            "2026-04-03","2026-04-06","2026-05-01","2026-06-19",
            "2026-09-25","2026-10-09","2026-10-10",
        }
    print("✅ 載入 " + str(len(holidays)) + " 個假日")
    return holidays


# ==========================================
# 4. 交易日判斷
# ==========================================
def is_trading_day(dt, holidays):
    if dt.weekday() >= 5:
        return False
    if dt.strftime("%Y-%m-%d") in holidays:
        return False
    return True


# ==========================================
# 5. 微台指結算日（每月第三個星期三）
# ==========================================
def get_settlement_date(year, month, holidays):
    count = 0
    for day in range(1, 32):
        try:
            d = datetime.date(year, month, day)
        except ValueError:
            break
        if d.weekday() == 2:
            count += 1
            if count == 3:
                while d.strftime("%Y-%m-%d") in holidays or d.weekday() >= 5:
                    d += datetime.timedelta(days=1)
                return d
    return None

def get_settlements(holidays):
    now = datetime.datetime.now(TW_TIMEZONE)
    y, m = now.year, now.month
    cur = get_settlement_date(y, m, holidays)
    if cur and now.date() > cur:
        m = m % 12 + 1
        y = y + (1 if m == 1 else 0)
        cur = get_settlement_date(y, m, holidays)
    nm = cur.month % 12 + 1
    ny = cur.year + (1 if nm == 1 else 0)
    return cur, get_settlement_date(ny, nm, holidays)

def trading_days_until(target, holidays):
    d = datetime.datetime.now(TW_TIMEZONE).date()
    count = 0
    while d < target:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in holidays:
            count += 1
    return count


# ==========================================
# 6. 讀取 Google Sheet 部位
# ==========================================
def load_position():
    print("📋 讀取部位...")
    try:
        r = requests.get(GSHEET_CSV_URL, timeout=15)
        r.encoding = "utf-8"
        lines = [l for l in r.text.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            return None
        row = lines[1].split(",")
        pos = {
            "lots":        int(row[0].strip()),
            "entry_price": float(row[1].strip()),
            "margin_cash": float(row[2].strip()),
            "note":        row[3].strip() if len(row) > 3 else "",
            "updated_at":  row[4].strip() if len(row) > 4 else "未知",
        }
        print("✅ " + str(pos["lots"]) + " 口 @ " + str(pos["entry_price"]))
        return pos
    except Exception as e:
        print("❌ Sheet 失敗: " + str(e))
        return None


# ==========================================
# 7. 抓取各市場行情
# ==========================================
def get_tw_index():
    """台指（加權指數）"""
    try:
        hist = yf.Ticker("^TWII").history(period="3d")
        if len(hist) < 2:
            return None, None
        cur  = float(hist.iloc[-1]["Close"])
        prev = float(hist.iloc[-2]["Close"])
        return cur, (cur - prev) / prev * 100
    except Exception as e:
        print("❌ 台指失敗: " + str(e))
        return None, None

def get_us_markets():
    """S&P500、納斯達克、VIX"""
    results = {}
    tickers = {
        "sp500": "^GSPC",
        "nasdaq": "^IXIC",
        "vix": "^VIX",
    }
    for name, ticker in tickers.items():
        try:
            hist = yf.Ticker(ticker).history(period="3d")
            if len(hist) >= 2:
                cur  = float(hist.iloc[-1]["Close"])
                prev = float(hist.iloc[-2]["Close"])
                chg  = (cur - prev) / prev * 100
                results[name] = {"price": cur, "chg": chg}
            else:
                results[name] = None
        except Exception as e:
            print("❌ " + name + " 失敗: " + str(e))
            results[name] = None
    return results

def get_txf_night():
    """台指期夜盤（TXF=F）"""
    try:
        hist = yf.Ticker("TXF=F").history(period="3d")
        if len(hist) >= 2:
            cur  = float(hist.iloc[-1]["Close"])
            prev = float(hist.iloc[-2]["Close"])
            return cur, (cur - prev) / prev * 100
        return None, None
    except Exception as e:
        print("❌ 台指期夜盤失敗: " + str(e))
        return None, None


# ==========================================
# 8. 風險計算
# ==========================================
def calc_risk(position, current_price, margin_init, margin_maint):
    lots        = position["lots"]
    entry_price = position["entry_price"]
    margin_cash = position["margin_cash"]

    pnl_points = (current_price - entry_price) * lots
    pnl_twd    = pnl_points * MXF_MULTIPLIER
    equity     = margin_cash + pnl_twd
    buffer_twd = equity - margin_maint * lots
    buf_pts    = buffer_twd / MXF_MULTIPLIER / lots if lots > 0 else 0
    ratio      = equity / (margin_init * lots) * 100 if lots > 0 else 0
    call_price = entry_price - buf_pts

    return {
        "pnl_points":        round(pnl_points, 0),
        "pnl_twd":           round(pnl_twd, 0),
        "equity":            round(equity, 0),
        "margin_ratio":      round(ratio, 1),
        "buffer_points":     round(buf_pts, 1),
        "margin_call_price": round(call_price, 0),
        "current_price":     current_price,
    }

def danger_label(ratio):
    if ratio < 80:  return "🔴 極度危險｜立即補保或減碼！"
    if ratio < 100: return "🟠 危險｜接近追繳線！"
    if ratio < 120: return "🟡 警戒｜建議備妥補保資金"
    return "🟢 安全"


# ==========================================
# 9. 組裝日盤訊息
# ==========================================
def build_day_message(pos, risk, tw_chg, settlement, next_s,
                      days_left, margin_init, margin_maint, alerts):
    now_str  = datetime.datetime.now(TW_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    pnl_icon = "📈" if risk["pnl_twd"] >= 0 else "📉"
    chg_icon = "🔺" if tw_chg >= 0 else "🔻"
    sign     = "+" if risk["pnl_twd"] >= 0 else ""

    lines = []
    if alerts:
        lines.append("🔔 警示通知")
        for a in alerts:
            lines.append("  " + a)
        lines.append("")

    lines += [
        "【微台指監控】日盤報告",
        "🕐 " + now_str,
        "",
        "━━━ 🎯 部位狀況 ━━━",
        "📦 口數: " + str(pos["lots"]) + " 口（做多）",
        "🏷️ 進場均價: " + str(int(pos["entry_price"])) + " 點",
        "📊 台指: " + str(int(risk["current_price"])) +
            " (" + chg_icon + str(round(tw_chg, 2)) + "%)",
        pnl_icon + " 未實現: " + sign + str(int(risk["pnl_twd"])) +
            " 元 / " + sign + str(int(risk["pnl_points"])) + " 點",
        "",
        "━━━ 💀 保證金風險 ━━━",
        "💰 帳戶權益: " + str(int(risk["equity"])) + " 元",
        "📋 原始/維持: " + str(margin_init) + "/" + str(margin_maint) + " 元",
        "📉 保證金比率: " + str(risk["margin_ratio"]) + "%",
        "🚨 " + danger_label(risk["margin_ratio"]),
        "🛡️ 距追繳: " + str(risk["buffer_points"]) + " 點",
        "⚠️ 追繳點位: " + str(int(risk["margin_call_price"])) + " 點",
        "",
        "━━━ 📅 轉倉行事曆 ━━━",
        "📌 結算日: " + settlement.strftime("%Y/%m/%d") +
            "（剩 " + str(days_left) + " 個交易日）",
        "➡️ 下月結算: " + next_s.strftime("%Y/%m/%d"),
    ]

    if pos.get("note"):
        lines += ["", "📝 " + pos["note"]]
    lines += ["", "🔄 更新: " + pos.get("updated_at", "未知")]
    return "\n".join(lines)


# ==========================================
# 10. 組裝夜盤訊息
# ==========================================
def build_night_message(pos, risk, txf_price, txf_chg,
                        us_data, settlement, next_s, days_left, alerts):
    now_str  = datetime.datetime.now(TW_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    pnl_icon = "📈" if risk["pnl_twd"] >= 0 else "📉"
    sign     = "+" if risk["pnl_twd"] >= 0 else ""

    lines = []
    if alerts:
        lines.append("🔔 警示通知")
        for a in alerts:
            lines.append("  " + a)
        lines.append("")

    lines += [
        "【微台指監控】夜盤報告",
        "🕐 " + now_str,
        "",
        "━━━ 🌙 夜盤行情 ━━━",
    ]

    # 台指期夜盤
    if txf_price:
        txf_icon = "🔺" if txf_chg >= 0 else "🔻"
        lines.append("🇹🇼 台指期夜盤: " + str(int(txf_price)) +
                     " (" + txf_icon + str(round(txf_chg, 2)) + "%)")
    else:
        lines.append("🇹🇼 台指期夜盤: 資料不足")

    # 納斯達克
    if us_data.get("nasdaq"):
        nd = us_data["nasdaq"]
        icon = "🔺" if nd["chg"] >= 0 else "🔻"
        lines.append("🇺🇸 那斯達克: " + str(round(nd["price"], 0)) +
                     " (" + icon + str(round(nd["chg"], 2)) + "%)")

    # VIX
    if us_data.get("vix"):
        vd   = us_data["vix"]
        vicon = "🔴" if vd["price"] >= VIX_WARN else "🟡" if vd["price"] >= 20 else "🟢"
        lines.append("😱 VIX: " + str(round(vd["price"], 1)) +
                     " " + vicon +
                     " (" + ("+" if vd["chg"] >= 0 else "") + str(round(vd["chg"], 2)) + "%)")

    lines += [
        "",
        "━━━ 🎯 部位狀況 ━━━",
        "📦 " + str(pos["lots"]) + " 口 @ " + str(int(pos["entry_price"])) + " 點（做多）",
        pnl_icon + " 未實現: " + sign + str(int(risk["pnl_twd"])) +
            " 元 / " + sign + str(int(risk["pnl_points"])) + " 點",
        "💰 帳戶權益: " + str(int(risk["equity"])) + " 元",
        "📉 保證金比率: " + str(risk["margin_ratio"]) + "% — " +
            danger_label(risk["margin_ratio"]),
        "⚠️ 追繳點位: " + str(int(risk["margin_call_price"])) + " 點",
        "",
        "━━━ 📅 轉倉 ━━━",
        "📌 結算日: " + settlement.strftime("%Y/%m/%d") +
            "（剩 " + str(days_left) + " 個交易日）",
    ]

    return "\n".join(lines)


# ==========================================
# 11. LINE 發送
# ==========================================
def send_line(msg):
    if not LINE_TOKEN or not LINE_USER_ID:
        print("⚠️ 未設定 LINE Token")
        return False
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + LINE_TOKEN},
            json={"to": LINE_USER_ID,
                  "messages": [{"type": "text", "text": msg}]},
            timeout=15,
        )
        ok = resp.status_code == 200
        print("✅ LINE 成功" if ok else "❌ LINE 失敗 HTTP " + str(resp.status_code))
        return ok
    except Exception as e:
        print("❌ LINE 例外: " + str(e))
        return False


# ==========================================
# 12. 主程式
# ==========================================
if __name__ == "__main__":
    now = datetime.datetime.now(TW_TIMEZONE)
    print("🚀 微台指監控啟動 — " + now.strftime("%Y-%m-%d %H:%M:%S"))

    # 假日抓取
    holidays = fetch_tw_holidays()

    # 判斷目前時段
    session = get_session()
    print("📍 目前時段: " + session)

    # 夜盤時段要跨日判斷：
    # 夜盤屬於「前一個交易日的延伸」
    # 判斷邏輯：夜盤 00:00~05:00 屬於前天的夜盤（昨天是否為交易日）
    #           夜盤 15:00~24:00 屬於今天的夜盤（今天是否為交易日）
    if session == "NIGHT":
        if now.hour < 6:
            # 深夜段：判斷昨天是否為交易日
            check_dt = now - datetime.timedelta(days=1)
        else:
            # 下午段：判斷今天是否為交易日
            check_dt = now
        if not is_trading_day(check_dt, holidays):
            print("😴 非交易日夜盤，跳過。")
            exit(0)
    elif session == "DAY":
        if not is_trading_day(now, holidays):
            print("😴 今日非交易日，跳過。")
            exit(0)
    else:
        print("😴 休市中，跳過。")
        exit(0)

    # 共用資料抓取
    margin_init, margin_maint = fetch_mxf_margin()
    position = load_position() or {
        "lots": 1, "entry_price": 22000, "margin_cash": 25000,
        "note": "預設測試部位", "updated_at": "未設定",
    }
    settlement, next_s = get_settlements(holidays)
    days_left = trading_days_until(settlement, holidays)

    # ── 日盤邏輯 ──────────────────────────────────────
    if session == "DAY":
        tw_price, tw_chg = get_tw_index()
        if tw_price is None:
            print("❌ 無法取得台指現價")
            exit(1)

        risk = calc_risk(position, tw_price, margin_init, margin_maint)

        alerts = []
        if days_left <= ROLLOVER_WARN_DAYS:
            alerts.append("📅 距結算僅剩 " + str(days_left) + " 個交易日，請準備轉倉！")
        if risk["margin_ratio"] < 120:
            alerts.append("💀 保證金比率偏低 (" + str(risk["margin_ratio"]) + "%)")
        if tw_chg <= CRASH_TW_PCT:
            alerts.append("📉 台指急跌 " + str(round(tw_chg, 2)) + "%！")

        msg = build_day_message(position, risk, tw_chg, settlement, next_s,
                                days_left, margin_init, margin_maint, alerts)

    # ── 夜盤邏輯 ──────────────────────────────────────
    else:
        txf_price, txf_chg = get_txf_night()
        us_data = get_us_markets()

        # 夜盤用台指期夜盤價格計算損益，抓不到則用加權指數備援
        if txf_price:
            price_for_risk = txf_price
        else:
            tw_price, _ = get_tw_index()
            price_for_risk = tw_price or position["entry_price"]

        risk = calc_risk(position, price_for_risk, margin_init, margin_maint)

        alerts = []
        if days_left <= ROLLOVER_WARN_DAYS:
            alerts.append("📅 距結算僅剩 " + str(days_left) + " 個交易日，請準備轉倉！")
        if risk["margin_ratio"] < 120:
            alerts.append("💀 保證金比率偏低 (" + str(risk["margin_ratio"]) + "%)")
        if txf_chg is not None and txf_chg <= CRASH_TW_PCT:
            alerts.append("📉 台指期夜盤急跌 " + str(round(txf_chg, 2)) + "%！")
        if us_data.get("nasdaq") and us_data["nasdaq"]["chg"] <= CRASH_US_PCT:
            alerts.append("🇺🇸 那斯達克急跌 " + str(round(us_data["nasdaq"]["chg"], 2)) + "%！")
        if us_data.get("vix") and us_data["vix"]["price"] >= VIX_WARN:
            alerts.append("😱 VIX 超過 " + str(VIX_WARN) + "，市場恐慌！")

        msg = build_night_message(position, risk, txf_price, txf_chg or 0,
                                  us_data, settlement, next_s, days_left, alerts)

    print("\n" + "=" * 45)
    print(msg)
    print("=" * 45)
    send_line(msg)
