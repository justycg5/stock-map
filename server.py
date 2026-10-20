# -*- coding: utf-8 -*-
# v31 轻量后端：静态文件 + /api/update（触发 fetch_spot.py 更新现货数据）
# 用法: python server.py [port]   默认 8080
import gzip, html, json, mimetypes, os, re, subprocess, sys, threading, time, urllib.request, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))

# ---- 网络直连：绕过系统代理（127.0.0.1:7890 代理进程间歇不可用时 10061 拒连）----
for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_v, None)
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

# pythonw.exe 无窗口运行时 sys.stdout/stderr 为 None → 重定向到 log.log（utf-8 行缓冲）
if sys.stdout is None or sys.stderr is None:
    try:
        _logf = open(os.path.join(BASE, "log.log"), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _logf
        if sys.stderr is None:
            sys.stderr = _logf
    except Exception:
        pass

SPOT_SCRIPT = os.path.join(BASE, "fetch_spot.py")
RARE_EARTH_SCRIPT = os.path.join(BASE, "fetch_rare_earth.py")
DATA_JSON = os.path.join(BASE, "data.json")
NOTES_JSON = os.path.join(BASE, "notes.json")
WATCHLIST_JSON = os.path.join(BASE, "watchlist.json")
WATCHLIST_MAX_BYTES = 512 * 1024
NOTES_MAX_BYTES = 2 * 1024 * 1024
CALNOTES_JSON = os.path.join(BASE, "calnotes.json")   # 日历当日备忘（键=日期，值=文本）
CALNOTES_MAX_BYTES = 256 * 1024
COSTS_JSON = os.path.join(BASE, "costs.json")
COSTS_MAX_BYTES = 512 * 1024
ALERTS_JSON = os.path.join(BASE, "alerts.json")
WEBHOOK_JSON = os.path.join(BASE, "webhook.json")

MONITOR_JSON = os.path.join(BASE, "monitor.json")
MONITOR_WEBHOOK_JSON = os.path.join(BASE, "monitor_webhook.json")
STOCK_WEBHOOK_JSON = os.path.join(BASE, "stock_webhook.json")  # v91：股票告警独立群（期货告警群继续用 webhook.json）
MONITOR_INTERVAL_S = 60          # 公告轮询周期（秒）
MONITOR_DAYS = 7                 # 公告查询窗口（天）
MONITOR_MAX_BYTES = 512 * 1024
_monitor_lock = threading.Lock()  # monitor.json 读改写互斥锁（防后台轮询覆盖用户 POST）
MONITOR_FAIL_ALERT_ROUNDS = 3        # 连续查询失败 N 轮后推送告警
MONITOR_FAIL_ALERT_COOLDOWN_S = 1800  # 同类失败告警冷却（秒，30 分钟）
_monitor_fail = {}                  # 失败状态（内存态）：code -> {"count": 连续失败轮数, "lastAlert": 上次告警 ts}
REFETCH_AT = (15, 5)
LUNCH_REFETCH_AT = (11, 32)   # 午休快照：上午收盘后抓一次（覆盖午休时段首次打开场景）             # 每个交易日收盘后自动重抓 data.json 的时刻
ALERT_INTERVAL_S = 8          # 告警轮询周期（秒）
ALERT_COOLDOWN_S = 60         # 同规则触发后冷却（秒，v84.2 由 300 改为 60）
ALERT_DEFAULT_RESET = 0.5     # 触发后回落百分比才复位（防抖动）
ALERT_FIELDS = "f12,f14,f2,f3,f4,f109,f160,f110,f24,f25"
ALERT_TYPE_FIELD = {
    "price": ("f2", "现价"),
    "pct": ("f3", "当日涨跌幅"),
    "chg": ("f4", "涨跌额"),
    "pct5": ("f109", "近5日涨跌幅"),
    "pct10": ("f160", "近10日涨跌幅"),
    "pct20": ("f110", "近20日涨跌幅"),
    "pct60": ("f24", "近60日涨跌幅"),
    "pctYTD": ("f25", "年初至今涨跌幅"),
}

# ===== 告警交易时段（v67）：按 secid 推断产品类型，时段外不检测 =====
# A股：工作日 9:30-11:30 / 13:00-15:00（周末不检测，法定节假日未做日历，按工作日处理）
# 期货：日盘 9:00-10:15 / 10:30-11:30 / 13:30-15:00（工作日）+ 夜盘按品种
#   贵金属(沪金aum/沪银agm)：夜盘 21:00-次日02:30
#   有色金属(cum/alm/znm/pbm/nim/snm/ssm)：夜盘 21:00-次日01:00
#   无夜盘(生猪lhm/lh、鸡蛋jd、苹果ap、红枣cj、尿素ur、锰硅sf、硅铁sm等)：仅日盘
#   其他期货(113/114/115/142 前缀)：默认夜盘 21:00-23:00
# 未知 secid：全天检测（维持现状，不误伤）
FUT_DAY_WINDOWS = [(9 * 60, 10 * 60 + 15), (10 * 60 + 30, 11 * 60 + 30), (13 * 60 + 30, 15 * 60)]
FUT_NIGHT_TEMPLATES = {
    "au_ag": (21 * 60, 2 * 60 + 30),   # 跨天：21:00 - 次日02:30
    "metal": (21 * 60, 1 * 60),        # 跨天：21:00 - 次日01:00
    "normal": (21 * 60, 23 * 60),      # 不跨天：21:00 - 23:00
}
FUT_NIGHT_MAP = {  # 品种代码 -> 夜盘模板
    "aum": "au_ag", "agm": "au_ag",
    "scm": "au_ag",  # 原油（上期能源）：夜盘 21:00-次日02:30，与贵金属同窗
    "cum": "metal", "alm": "metal", "znm": "metal", "pbm": "metal",
    "nim": "metal", "snm": "metal", "ssm": "metal",
}
FUT_NO_NIGHT = {"lhm", "lh", "jd", "ap", "cj", "ur", "sf", "sm", "cs", "rr", "eb", "pg", "lr", "wh", "pm", "ri", "jr", "bb", "fb"}


def _in_session(secid, now=None):
    """按 secid 推断产品类型，判断 now 是否在其交易时段内。未知类型返回 True（全天）。"""
    import datetime
    now = now or datetime.datetime.now()
    sid = str(secid or "").lower()
    wd = now.weekday()  # 0=周一
    t = now.hour * 60 + now.minute

    # A股
    if sid.startswith("1.") or sid.startswith("0."):
        if wd >= 5:
            return False
        return any(s <= t <= e for s, e in [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)])

    # 国内期货
    if sid.startswith(("113.", "114.", "115.", "142.")):
        code = sid.split(".")[1] if "." in sid else ""
        if any(s <= t <= e for s, e in FUT_DAY_WINDOWS):
            return wd < 5
        if code in FUT_NO_NIGHT:
            return False
        tmpl = FUT_NIGHT_MAP.get(code, "normal")
        start, end = FUT_NIGHT_TEMPLATES[tmpl]
        if end > start:  # 不跨天（21:00-23:00）
            return start <= t <= end and wd < 5
        # 跨天夜盘：21:00-24:00 属今晚；00:00-end 属前一晚
        if t >= start:
            return wd < 5
        if t <= end:
            return ((wd - 1) % 7) < 5
        return False

    # 未知：全天检测
    return True
COOLDOWN_S = 60
MONITOR_MAX_COUNT = 20       # 监控股票上限（东财批量接口 100 条共享窗口，过多会稀释漏公告）
_last_spot = {"ts": 0}
_last_rare_earth = {"ts": 0}  # 稀土独立冷却（v83.1：与 q5500 分开，互不影响）
REFETCH_COOLDOWN_S = 600        # 手动重抓冷却（秒）
_last_refetch = {"ts": 0}       # 手动重抓时间戳
_refetch_lock = threading.Lock()  # 重抓并发锁（防自动/手动同时抓）

# 快照触发窗口与重试上限（2026-09-24 修复）：
# Bug1 原逻辑 now>=11:32 即触发，18:30 启动也会打"午休快照"；Bug2 失败后 600s 无限重试，
# 东财风控期一天 59 次触发、每次都在刷新封禁窗口，形成"越封越抓、越抓越封"死循环。
SNAPSHOT_MAX_ATTEMPTS = 5      # 单快照点单日最多尝试次数（含失败），耗尽等下一个事件点
LUNCH_WINDOW_END = (13, 30)    # 午休快照触发窗口截止
CLOSE_WINDOW_END = (16, 30)    # 收盘快照触发窗口截止
_snap_attempts = {"date": None, "lunch": 0, "close": 0}


def _snap_attempt_slot(key):
    # 当日内某快照点已尝试次数（跨日自动清零；daily_refetch_loop 单线程调用，无需加锁）
    today = time.strftime("%Y-%m-%d")
    if _snap_attempts["date"] != today:
        _snap_attempts["date"] = today
        _snap_attempts["lunch"] = 0
        _snap_attempts["close"] = 0
    return _snap_attempts[key]

# v100 gzip 结果缓存：key=(绝对路径, mtime_ns, size)。文件原子替换（os.replace）后 mtime 变化自动失效
_GZ_CACHE = {}
_GZ_CACHE_MAX = 16

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE, **kw)

    def end_headers(self):
        # v100 缓存策略分三类：
        #  1) lib/、map/ 内容不可变（echarts 5.6.0 / world 底图，只在手动升级时变且必带 ?v=）→ 长缓存 immutable
        #  2) html/js（app.js、index.html 改动频繁）不缓存：否则手机端启发式缓存旧代码，修复到不了设备（v96）
        #  3) 其余（数据 json 等）沿用默认协商行为
        path = (self.path or "").split("?")[0].lower()
        if path.startswith("/lib/") or path.startswith("/map/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif path in ("", "/") or path.endswith(".html") or path.endswith(".js"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_fut_quotes(self):
        """期货实时（v104）：代理新浪 hq 批量行情，盘中实时彻底不走东财。
        浏览器不直连外网源：东财/新浪均无 CORS 头，只能服务端代理。"""
        try:
            data, fresh = _sina_fut_quotes()
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:80], "quotes": {}})
        return self._json({"ok": True, "fresh": fresh, "ts": int(_FUT_QUOTE_MEM["ts"]), "quotes": data})

    def _handle_idx_quotes(self):
        """v107 指数条代理：{ok, quotes:[...]}（东财字段名兼容，diff 数组形态）"""
        try:
            data = _tx_idx_quotes()
        except Exception as e:
            data = None
        if not data:
            return self._json({"ok": False, "msg": "idx unavailable", "data": {"diff": []}})
        diff = [data[k] for k in ("000001", "399006", "000688", "000300", "399001") if k in data]
        return self._json({"ok": True, "data": {"diff": diff}, "ts": int(time.time())})

    def _handle_version(self):
        """data.json 版本探测（文件 mtime，秒级）。
        自动刷新轮询先比对这个值，快照没变（午休/收盘/周末）就不拉整包，移动端闲置流量≈0。"""
        try:
            st = os.stat(DATA_JSON)
        except OSError:
            return self._json({"ok": False, "v": 0})
        return self._json({"ok": True, "v": int(st.st_mtime)})

    def _serve_static_gzip(self):
        """大体积文本静态文件按需 gzip 压缩响应（Tailscale 移动端提速：data.json 1.7MB→约 250KB）。
        仅 >30KB 的 .json/.js/.html/.css/.svg（v100：150KB→30KB，纳入 index.html/calendar.json）；
        API 与小文件不介入（小文件保留 304 协商）。
        压缩结果按 (路径, mtime, size) 缓存：data.json 每请求现场压 2MB 约 100ms CPU，
        缓存后同一快照只压一次，TTFB 即时。"""
        path = self.path.split("?")[0]
        if path.startswith("/api/") or ".." in path:
            return False
        if path in ("", "/"):
            path = "/index.html"   # 首页走 gzip（49KB→约 13KB）
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".json", ".js", ".html", ".css", ".svg"):
            return False
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        if "gzip" not in ae:
            return False
        try:
            full = self.translate_path(path)
            if not os.path.isfile(full):
                return False
            st = os.stat(full)
            if st.st_size <= 30 * 1024:
                return False
            ctype = mimetypes.guess_type(full)[0] or ("application/json" if ext == ".json" else "application/octet-stream")
            key = (full, st.st_mtime_ns, st.st_size)
            gz = _GZ_CACHE.get(key)
            if gz is None:
                with open(full, "rb") as fp:
                    raw = fp.read()
                gz = gzip.compress(raw, 5)
                if len(_GZ_CACHE) >= _GZ_CACHE_MAX:
                    _GZ_CACHE.clear()   # 简单防膨胀；GIL 下 dict 读写原子，最坏多压一次
                _GZ_CACHE[key] = gz
        except Exception:
            return False
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(gz)))
            self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
            self.end_headers()
            self.wfile.write(gz)
            return True
        except (ConnectionError, BrokenPipeError):
            return True  # 客户端断开，静默
        except Exception:
            return False

    def do_GET(self):
        try:
            if self._serve_static_gzip():
                return
        except Exception:
            pass
        path = self.path.split("?")[0]
        if path == "/api/version":
            return self._handle_version()
        if path == "/api/update":
            return self._handle_update()
        if path == "/api/costs":
            return self._handle_costs_get()
        if path == "/api/notes":
            return self._handle_notes_get()
        if path == "/api/calnotes":
            return self._handle_calnotes_get()
        if path == "/api/watchlist":
            return self._handle_watchlist_get()
        if path == "/api/refetch":
            return self._handle_refetch()
        if path == "/api/alerts":
            return self._handle_alerts_get()
        if path == "/api/alertcfg":
            return self._handle_alertcfg_get()
        if path == "/api/stockalertcfg":
            return self._handle_stockalertcfg_get()
        if path == "/api/monitor":
            return self._handle_monitor_get()
        if path == "/api/monitorcfg":
            return self._handle_monitorcfg_get()
        if path == "/api/rare_earth_refetch":
            return self._handle_rare_earth_refetch()
        if path == "/api/fut_quotes":
            return self._handle_fut_quotes()
        if path == "/api/idx_quotes":
            return self._handle_idx_quotes()
        if path == "/api/f10":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            data = _f10_get(code)
            if data is None:
                return self._json({"ok": False, "msg": "f10 unavailable"}, 200)
            return self._json({"ok": True, "data": data})
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/costs":
            return self._handle_costs_post()
        if path == "/api/notes":
            return self._handle_notes_post()
        if path == "/api/calnotes":
            return self._handle_calnotes_post()
        if path == "/api/watchlist":
            return self._handle_watchlist_post()
        if path == "/api/alerts":
            return self._handle_alerts_post()
        if path == "/api/alertcfg":
            return self._handle_alertcfg_post()
        if path == "/api/stockalertcfg":
            return self._handle_stockalertcfg_post()
        if path == "/api/alerttest":
            return self._handle_alerttest()
        if path == "/api/monitor":
            return self._handle_monitor_post()
        if path == "/api/monitorcfg":
            return self._handle_monitorcfg_post()
        if path == "/api/monitortest":
            return self._handle_monitortest()
        if path == "/api/cal_refetch":
            return self._handle_cal_refetch()
        return self._json({"ok": False, "msg": "not found"}, 404)

    # ---------- /api/rare_earth_refetch (v83 稀土现货更新) ----------
    def _handle_rare_earth_refetch(self):
        now = time.time()
        left = COOLDOWN_S - (now - _last_rare_earth["ts"])
        if left > 0:
            return self._json({"ok": False, "msg": "更新冷却中，%.0f 秒后再试" % left, "cooldown": int(left)})
        py = sys.executable
        try:
            p = subprocess.run([py, RARE_EARTH_SCRIPT], capture_output=True, timeout=60, cwd=BASE)
        except Exception as e:
            return self._json({"ok": False, "msg": "启动抓取失败: %s" % e})
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", "replace").strip() or "抓取脚本异常退出"
            return self._json({"ok": False, "msg": err[:200]})
        _last_rare_earth["ts"] = now
        try:
            with open(DATA_JSON, "r", encoding="utf-8") as f:
                payload = json.load(f)
            q = payload.get("rare_earth")
            return self._json({"ok": True, "rare_earth": q, "time": time.strftime("%H:%M:%S")})
        except Exception as e:
            return self._json({"ok": True, "msg": "数据已更新但读取失败: %s" % e})

    def _handle_update(self):
        now = time.time()
        left = COOLDOWN_S - (now - _last_spot["ts"])
        if left > 0:
            return self._json({"ok": False, "msg": "更新冷却中，%.0f 秒后再试" % left, "cooldown": int(left)})
        py = sys.executable
        try:
            p = subprocess.run([py, SPOT_SCRIPT], capture_output=True, timeout=60, cwd=BASE)
        except Exception as e:
            return self._json({"ok": False, "msg": "启动抓取失败: %s" % e})
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", "replace").strip() or "抓取脚本异常退出"
            return self._json({"ok": False, "msg": err[:200]})
        _last_spot["ts"] = now
        try:
            with open(DATA_JSON, "r", encoding="utf-8") as f:
                payload = json.load(f)
            q = payload.get("q5500")
            return self._json({"ok": True, "q5500": q, "time": time.strftime("%H:%M:%S")})
        except Exception as e:
            return self._json({"ok": True, "msg": "数据已更新但读取失败: %s" % e})

    def _handle_costs_get(self):
        try:
            with open(COSTS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        return self._json({"ok": True, "costs": data})

    def _handle_costs_post(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > COSTS_MAX_BYTES:
                return self._json({"ok": False, "msg": "\u6570\u636e\u8fc7\u5927\u6216\u4e3a\u7a7a"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            if not isinstance(obj, dict):
                return self._json({"ok": False, "msg": "\u683c\u5f0f\u9519\u8bef"})
            clean = {}
            for k, v in obj.items():
                code = str(k)[:12]
                if not code or not isinstance(v, dict):
                    continue
                try:
                    cost = float(v.get("cost"))
                except Exception:
                    continue
                if cost <= 0 or cost > 1e9:
                    continue
                qty = None
                if v.get("qty") is not None:
                    try:
                        qty = float(v.get("qty"))
                    except Exception:
                        qty = None
                    if qty is not None and qty < 0:
                        qty = None
                item = {"cost": round(cost, 4)}
                if qty is not None:
                    item["qty"] = round(qty, 2)
                clean[code] = item
            tmp = COSTS_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=1)
            os.replace(tmp, COSTS_JSON)
            return self._json({"ok": True, "count": len(clean)})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:200]})

    def _handle_notes_get(self):
        try:
            with open(NOTES_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        return self._json({"ok": True, "notes": data})

    def _handle_notes_post(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > NOTES_MAX_BYTES:
                return self._json({"ok": False, "msg": "数据过大或为空"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            if not isinstance(obj, dict):
                return self._json({"ok": False, "msg": "格式错误"})
            clean = {}
            for k, v in obj.items():
                k = str(k)[:16]
                if isinstance(v, str) and v.strip():
                    clean[k] = v.strip()[:200]
            tmp = NOTES_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=1)
            os.replace(tmp, NOTES_JSON)
            return self._json({"ok": True, "count": len(clean)})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:200]})

    def _handle_calnotes_get(self):
        """日历当日备忘读取（键=YYYY-MM-DD，值=文本）。"""
        try:
            with open(CALNOTES_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return self._json({"ok": True, "notes": data})

    def _handle_calnotes_post(self):
        """日历当日备忘写入：键限日期格式，值截断 200 字；清理 45 天前的旧键防膨胀。"""
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > CALNOTES_MAX_BYTES:
                return self._json({"ok": False, "msg": "数据过大或为空"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            if not isinstance(obj, dict):
                return self._json({"ok": False, "msg": "格式错误"})
            clean = {}
            for k, v in obj.items():
                k = str(k)
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", k):
                    continue
                if isinstance(v, str) and v.strip():
                    clean[k] = v.strip()[:200]
            cutoff = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
            clean = {k: v for k, v in clean.items() if k >= cutoff}
            tmp = CALNOTES_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=1)
            os.replace(tmp, CALNOTES_JSON)
            return self._json({"ok": True, "count": len(clean)})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:200]})

    # ---------- /api/alerts ----------
    def _handle_refetch(self):
        r = _refetch_force()
        if not isinstance(r, (tuple, list)) or len(r) != 3:
            return self._json({"ok": False, "msg": "服务内部错误（重抓返回值异常）", "cooldown": 0})
        ok, msg, cd = r
        return self._json({"ok": ok, "msg": msg, "cooldown": cd})

    def _handle_watchlist_get(self):
        try:
            with open(WATCHLIST_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        updated_ms = 0
        try:
            updated_ms = int(os.path.getmtime(WATCHLIST_JSON) * 1000)
        except Exception:
            updated_ms = 0
        return self._json({"ok": True, "list": data.get("list", []), "collapsed": data.get("collapsed", []), "updated_ms": updated_ms})

    def _handle_watchlist_post(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > WATCHLIST_MAX_BYTES:
                return self._json({"ok": False, "msg": "数据过大或为空"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            if not isinstance(obj, dict):
                return self._json({"ok": False, "msg": "格式错误"})
            lst = obj.get("list") if isinstance(obj.get("list"), list) else []
            clean_list = []
            seen = set()
            for it in lst:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("code", "") or "")[:12]
                if not code or code in seen:
                    continue
                seen.add(code)
                clean_list.append({
                    "code": code,
                    "name": str(it.get("name", "") or "")[:32],
                    "secid": str(it.get("secid", "") or "")[:16],
                    "ts": int(it.get("ts", 0) or 0),
                })
            clean_list = clean_list[:2000]
            coll = obj.get("collapsed") if isinstance(obj.get("collapsed"), list) else []
            clean_coll = [str(x)[:32] for x in coll if isinstance(x, str)][:500]
            payload = {"list": clean_list, "collapsed": clean_coll,
                       "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            tmp = WATCHLIST_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, WATCHLIST_JSON)
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)})

    def _handle_alerts_get(self):
        return self._json({"ok": True, "alerts": _load_json(ALERTS_JSON, {})})

    def _clean_alerts(self, obj):
        out = {}
        if not isinstance(obj, dict):
            return out
        for code, rules in obj.items():
            code = str(code)[:12]
            if not code or not isinstance(rules, list):
                continue
            clean_rules = []
            for r in rules:
                if not isinstance(r, dict):
                    continue
                ttype = r.get("type")
                if ttype not in ALERT_TYPE_FIELD:
                    ttype = "price"
                op = r.get("op")
                if op not in (">=", "<="):
                    op = ">="
                try:
                    value = float(r.get("value"))
                except Exception:
                    continue
                state = r.get("state") if r.get("state") in ("idle", "fired") else "idle"
                clean_rules.append({
                    "id": str(r.get("id") or "")[:24],
                    "mkt": "fut" if str(r.get("mkt") or "").startswith("f") else "stock",
                    "secid": str(r.get("secid") or "")[:20],
                    "type": ttype,
                    "op": op,
                    "value": round(value, 4),
                    "note": str(r.get("note") or "")[:100],
                    "enabled": bool(r.get("enabled", True)),
                    "autoOff": bool(r.get("autoOff", False)),
                    "resetPct": float(r.get("resetPct", ALERT_DEFAULT_RESET) or ALERT_DEFAULT_RESET),
                    "cooldownS": int(r.get("cooldownS", ALERT_COOLDOWN_S) or ALERT_COOLDOWN_S),
                    "state": state,
                    "firedAt": r.get("firedAt"),
                    "resetAt": r.get("resetAt"),
                    "lastErr": r.get("lastErr"),
                })
            if clean_rules:
                out[code] = clean_rules
        return out

    def _handle_alerts_post(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > NOTES_MAX_BYTES:
                return self._json({"ok": False, "msg": "数据过大或为空"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            clean = self._clean_alerts(obj)
            _save_json(ALERTS_JSON, clean)
            return self._json({"ok": True, "count": sum(len(v) for v in clean.values())})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:200]})

    # ---------- /api/alertcfg ----------
    def _handle_alertcfg_get(self):
        cfg = _load_json(WEBHOOK_JSON, {}) or {}
        url = cfg.get("webhook", "") or ""
        return self._json({"ok": True, "cfg": {"webhook": _mask_webhook(url), "hasKey": bool(url), "atAll": bool(cfg.get("atAll"))}})

    def _handle_alertcfg_post(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > 8192:
                return self._json({"ok": False, "msg": "数据过大或为空"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            if not isinstance(obj, dict):
                return self._json({"ok": False, "msg": "格式错误"})
            cfg = _load_json(WEBHOOK_JSON, {}) or {}
            w = (obj.get("webhook") or "").strip()
            cfg["webhook"] = w
            cfg["atAll"] = bool(obj.get("atAll", False))
            _save_json(WEBHOOK_JSON, cfg)
            return self._json({"ok": True, "hasKey": bool(w)})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:200]})

    # ---------- /api/alerttest（v91：mkt=fut 期货群 / mkt=stock 股票群） ----------
    def _handle_alerttest(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            body = ""
            if 0 < ln <= 8192:
                body = self.rfile.read(ln).decode("utf-8")
        except Exception:
            body = ""
        mkt = "fut"
        custom = None
        if body:
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    if str(obj.get("mkt") or "").strip().lower() == "stock":
                        mkt = "stock"
                    if obj.get("msg"):
                        custom = str(obj["msg"])[:200]
            except Exception:
                pass
        label = "股票" if mkt == "stock" else "期货"
        if custom:
            msg = "【测试】%s（%s群）" % (custom, label)
        else:
            msg = "【测试】%s告警通道已就绪\n时间：%s" % (label, time.strftime("%Y-%m-%d %H:%M:%S"))
        if mkt == "stock":
            ok, err = _send_stock_webhook(msg)
        else:
            ok, err = _send_webhook(msg)
        return self._json({"ok": ok, "msg": err})

    # ---------- /api/stockalertcfg (v91 股票告警独立群 webhook 配置) ----------
    def _handle_stockalertcfg_get(self):
        cfg = _load_json(STOCK_WEBHOOK_JSON, {}) or {}
        url = cfg.get("webhook", "") or ""
        return self._json({"ok": True, "cfg": {"webhook": _mask_webhook(url), "hasKey": bool(url)}})

    def _handle_stockalertcfg_post(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > 8192:
                return self._json({"ok": False, "msg": "数据过大或为空"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            if not isinstance(obj, dict):
                return self._json({"ok": False, "msg": "格式错误"})
            w = (obj.get("webhook") or "").strip()
            _save_json(STOCK_WEBHOOK_JSON, {"webhook": w})
            return self._json({"ok": True, "hasKey": bool(w)})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:200]})

    # ---------- /api/monitorcfg (公告监控 webhook 配置) ----------
    def _handle_monitorcfg_get(self):
        cfg = _load_json(MONITOR_WEBHOOK_JSON, {}) or {}
        url = cfg.get("webhook", "") or ""
        return self._json({"ok": True, "cfg": {"webhook": _mask_webhook(url), "hasKey": bool(url)}})

    def _handle_monitorcfg_post(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > 8192:
                return self._json({"ok": False, "msg": "数据过大或为空"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            if not isinstance(obj, dict):
                return self._json({"ok": False, "msg": "格式错误"})
            w = (obj.get("webhook") or "").strip()
            _save_json(MONITOR_WEBHOOK_JSON, {"webhook": w})
            return self._json({"ok": True, "hasKey": bool(w)})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:200]})

    # ---------- /api/monitortest ----------
    def _handle_monitortest(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            body = ""
            if 0 < ln <= 8192:
                body = self.rfile.read(ln).decode("utf-8")
        except Exception:
            body = ""
        msg = "【测试】公告监控系统已就绪\n时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S")
        if body:
            try:
                obj = json.loads(body)
                if isinstance(obj, dict) and obj.get("msg"):
                    msg = "【测试】%s" % str(obj["msg"])[:200]
            except Exception:
                pass
        ok, err = _send_monitor_webhook(msg)
        return self._json({"ok": ok, "msg": "已推送" if ok else ("推送失败: " + err)})

    # ---------- /api/monitor (v82 公告监控) ----------
    def _handle_cal_refetch(self):
        """手动触发日历抓取：60 秒 handler 冷却 + 锁内 60 秒新鲜度检查（撞上刚完成的轮询零浪费）。"""
        try:
            now = time.time()
            gap = now - _last_cal_refetch["ts"]
            if gap < CAL_REFETCH_COOLDOWN_S:
                remain = int(CAL_REFETCH_COOLDOWN_S - gap)
                return self._json({"ok": False, "msg": "更新冷却中，%d 秒后再试" % remain, "cooldown": remain})
            ok, status, count = _fetch_calendar(min_gap=60)
            if ok:
                _last_cal_refetch["ts"] = now
                if status == "fresh":
                    return self._json({"ok": True, "msg": "数据已是最新", "fresh": True, "count": count})
                return self._json({"ok": True, "msg": "已更新", "count": count})
            return self._json({"ok": False, "msg": "抓取失败: %s" % status})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:120]})

    def _handle_monitor_get(self):
        data = _load_json(MONITOR_JSON, {}) or {}
        lst = data.get("monitored") if isinstance(data.get("monitored"), list) else []
        return self._json({"ok": True, "monitored": lst})

    def _handle_monitor_post(self):
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln <= 0 or ln > MONITOR_MAX_BYTES:
                return self._json({"ok": False, "msg": "数据过大或为空"})
            obj = json.loads(self.rfile.read(ln).decode("utf-8"))
            if not isinstance(obj, dict):
                return self._json({"ok": False, "msg": "格式错误"})
            lst = obj.get("monitored") if isinstance(obj.get("monitored"), list) else []
            # 读取旧基线：前端 toggle 时 seen_ids 传空数组，若直接覆盖会把所有股票去重基线清空
            # → 下次轮询把历史公告全当新公告推送（8/26 16:31 事故根因）。旧文件有值则保留。
            old_data = _load_json(MONITOR_JSON, {}) or {}
            old_seen_map = {str(x.get("code") or "").strip(): list(x.get("seen_ids") or []) for x in (old_data.get("monitored") or [])}
            old_baseline_map = {str(x.get("code") or "").strip(): bool(x.get("baseline")) for x in (old_data.get("monitored") or [])}
            clean = []
            seen = set()
            for it in lst:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("code", "") or "")[:12]
                if not code or code in seen:
                    continue
                seen.add(code)
                new_ids = [str(x)[:32] for x in (it.get("seen_ids") or []) if x]
                old_ids = old_seen_map.get(code, [])
                merged = new_ids if new_ids else old_ids  # 前端没传基线时保留旧基线
                clean.append({
                    "code": code,
                    "name": str(it.get("name", "") or "")[:32],
                    "secid": str(it.get("secid", "") or "")[:16],
                    "ts": int(it.get("ts", 0) or 0),
                    "seen_ids": merged[:500],
                    "baseline": bool(it.get("baseline")) or old_baseline_map.get(code, False),
                    # v85：监控起点时间。每次保存都刷新 → 此刻之前发布的公告永不推送
                    #（防共享窗口扩容把历史公告当新公告推，8/28 金风科技事故根因）
                    "baseline_ts": time.strftime("%Y-%m-%d %H:%M"),
                })
            if len(clean) > MONITOR_MAX_COUNT:
                return self._json({"ok": False, "msg": "监控最多 %d 只（当前 %d 只），请先移除部分再添加" % (MONITOR_MAX_COUNT, len(clean))})
            payload = {"monitored": clean, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            with _monitor_lock:
                _save_json(MONITOR_JSON, payload)
            return self._json({"ok": True, "count": len(clean)})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)[:200]})
        return self._json({"ok": ok, "msg": "已推送" if ok else ("推送失败: " + err)})

# ========== 告警引擎（模块级函数，后台线程运行） ==========

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

def _mask_webhook(url):
    if not url:
        return ""
    if "key=" in url:
        head, key = url.split("key=", 1)
        return head + "key=" + key[:4] + "****"
    return url[:40] + "****"

# ===== v92 告警备源：东财 ulist 被风控时降级腾讯(股票)/新浪(股票+期货) =====
# 东财 ALERT_FIELDS 字段序：f12,f14,f2,f3,f4,f109,f160,f110,f24,f25
#                          代码,名称,现价,涨跌幅,涨跌额,5日,10日,20日,60日,YTD
# 2026-09-24 实测：腾讯 qt.gtimg 不支持 nf_/qh_/hf_ 期货代码（全部 pv_none_match），期货备源只有新浪
SINA_FUT_MAP = {
    "rbm": "nf_RB0", "im": "nf_I0", "jm": "nf_J0", "cum": "nf_CU0", "alm": "nf_AL0",
    "aum": "nf_AU0", "agm": "nf_AG0", "lcm": "nf_LC0", "psm": "nf_PS0",
    # scm/lhm 新浪无对应代码（实测返回空），这些品种备源缺失
    "B00Y": "hf_OIL",
}
_SINA_NF_IDX = {"price": 8, "prev": 10}   # nf_：8=最新价，10=昨结算（涨跌幅基准）
_SINA_HF_IDX = {"price": 0, "prev": 7}    # hf_：0=最新价，7=昨收


def _http_get_raw(url, headers, timeout=10):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _backup_quotes_stock(codes):
    """腾讯批量行情 → {code: {name,price,pct,chg}}。字段位：1名称 2代码 3现价 4昨收 31涨跌 32涨跌幅"""
    out = {}
    if not codes:
        return out
    q = ",".join(("sh" if c.startswith("6") else "sz") + c for c in codes)
    try:
        body = _http_get_raw("https://qt.gtimg.cn/q=" + q, {"User-Agent": "Mozilla/5.0"}).decode("gbk", "replace")
    except Exception:
        return out
    for m in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"]*)"', body):
        f = m.group(2).split("~")
        if len(f) > 32:
            try:
                out[m.group(1)] = {"name": f[1], "price": float(f[3]),
                                   "pct": float(f[32]), "chg": float(f[31])}
            except (ValueError, IndexError):
                continue
    return out


def _backup_quotes_fut(codes):
    """新浪期货连续合约 → {code: {name,price}}。涨跌幅需自算（拿昨结算），这里只给现价。"""
    out = {}
    sina_map = {c: SINA_FUT_MAP[c] for c in codes if c in SINA_FUT_MAP}
    if sina_map:
        q = ",".join(sina_map.values())
        try:
            body = _http_get_raw("https://hq.sinajs.cn/list=" + q,
                                 {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}).decode("gbk", "replace")
        except Exception:
            body = ""
        rev = {v: k for k, v in sina_map.items()}
        for m in re.finditer(r'hq_str_(nf_|hf_)(\w+)="([^"]*)"', body):
            code = rev.get(m.group(1) + m.group(2))
            f = m.group(3).split(",")
            if not code or not f or f[0] in ("", "0.000"):
                continue
            idx = _SINA_HF_IDX if m.group(1) == "hf_" else _SINA_NF_IDX
            try:
                price = float(f[idx["price"]])
                prev = float(f[idx["prev"]]) if len(f) > idx["prev"] else 0.0
                pct = round((price / prev - 1) * 100, 2) if price > 0 and prev > 0 else None
                out[code] = {"name": (f[13] if m.group(1) == "hf_" and len(f) > 13 else f[0]),
                             "price": price, "pct": pct,
                             "chg": round(price - prev, 3) if pct is not None else None}
            except (ValueError, IndexError):
                continue
    # 2026-09-24 实测：scm(多晶硅)/lhm(生猪) 新浪无对应连续代码，腾讯也不支持期货，
    # 这些品种备源缺失——备源轮里跳过（等东财恢复后自动回归主源）
    return out


def _send_webhook_url_cfg(url, text, at_all=False, retries=2):
    """向指定 webhook 发送文本消息（v91 告警双通道共用底层）。"""
    if not url:
        return False, "未配置 webhook"
    payload = {"msgtype": "text", "text": {"content": text}}
    if at_all:
        payload["text"]["mentioned_list"] = ["@all"]
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                rj = json.loads(resp.read().decode("utf-8"))
            if rj.get("errcode") == 0:
                return True, "ok"
            return False, "企业微信返回: %s" % rj.get("errmsg", rj)
        except Exception as e:
            if i >= retries:
                return False, str(e)
            time.sleep(1)
    return False, "unknown"


def _send_webhook(text, retries=2):
    """期货告警群（webhook.json；v91 起与股票群分通道，历史函数名保留）"""
    cfg = _load_json(WEBHOOK_JSON, {}) or {}
    url = cfg.get("webhook", "") or ""
    return _send_webhook_url_cfg(url, text, at_all=bool(cfg.get("atAll")), retries=retries)


def _send_stock_webhook(text, retries=2):
    """股票告警群（stock_webhook.json，v91 双通道拆分）"""
    cfg = _load_json(STOCK_WEBHOOK_JSON, {}) or {}
    url = cfg.get("webhook", "") or ""
    return _send_webhook_url_cfg(url, text, at_all=bool(cfg.get("atAll")), retries=retries)


def _alert_channel(rule, code):
    """告警路由：返回 (发送函数, 渠道名)。三层判定：
    1) mkt 字段（前端保存规则时写入 stock/fut）；
    2) secid/代码形态（6位纯数字=股票，市场前缀+字母码=期货）；
    3) 都判不出 → 期货群（旧 webhook.json），宁可错投不丢推送。
    """
    mkt = str((rule or {}).get("mkt") or "").strip().lower()
    sid = str((rule or {}).get("secid") or "").strip().lower()
    if mkt == "stock":
        return _send_stock_webhook, "股票群"
    if mkt == "fut":
        return _send_webhook, "期货群"
    if re.match(r"^[01]\.[0-9]{6}$", sid) or (not sid and re.match(r"^[0-9]{6}$", str(code or ""))):
        return _send_stock_webhook, "股票群"
    return _send_webhook, "期货群"

def _fmt_num(v, is_pct=False):
    if v is None:
        return "-"
    try:
        f = float(v)
    except Exception:
        return "-"
    if is_pct:
        return ("%+.2f%%" % f) if f != 0 else ("%.2f%%" % f)
    return "%.2f" % f

def _alert_cond_text(rule):
    t = ALERT_TYPE_FIELD.get(rule.get("type"), ALERT_TYPE_FIELD["price"])[1]
    op = "≥" if rule.get("op") == ">=" else "≤"
    val = rule.get("value")
    is_pct = rule.get("type") not in ("price", "chg")
    return "%s %s %s" % (t, op, _fmt_num(val, is_pct))

def _alert_msg(name, code, cur, rule, price=None):
    is_pct = rule.get("type") != "price"
    lines = ["【股价告警】%s(%s)" % (name, code)]
    lines.append("当前值：%s" % _fmt_num(cur, is_pct))
    if is_pct and price is not None:                       # v106.1：涨跌幅类告警补当时价格
        lines.append("当前价：%s" % _fmt_num(price))
    lines.append("触发条件：%s" % _alert_cond_text(rule))
    note = (rule.get("note") or "").strip()
    if note:
        lines.append("备注：%s" % note)
    lines.append("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    return "\n".join(lines)

def _alert_reset_msg(name, code, cur, rule, price=None):
    """fired 状态回落（价格离开触发范围）时的恢复通知。"""
    lines = ["【股价告警 · 监控状态恢复】%s(%s)" % (name, code)]
    lines.append("当前值：%s" % _fmt_num(cur, rule.get("type") != "price"))
    if rule.get("type") != "price" and price is not None:  # v106.1：恢复通知同样带当时价格
        lines.append("当前价：%s" % _fmt_num(price))
    lines.append("此前触发条件：%s" % _alert_cond_text(rule))
    lines.append("说明：价格已回到触发范围之外，恢复正常监控")
    lines.append("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    return "\n".join(lines)

_ALERT_SRC_LOG = {"ts": 0.0}   # 告警主源成功日志节流（60s 一条，防 log.log 刷屏）

def _check_alerts():
    alerts = _load_json(ALERTS_JSON, {})
    if not isinstance(alerts, dict) or not alerts:
        return
    secids = []
    for code, rules in alerts.items():
        if not isinstance(rules, list):
            continue
        for r in rules:
            if isinstance(r, dict) and r.get("enabled") and r.get("type") in ALERT_TYPE_FIELD:
                sid = (r.get("secid") or "").strip()
                if not sid:
                    sid = ("1." if str(code).startswith("6") else "0.") + str(code)
                secids.append(sid)
                break
    if not secids:
        return
    # v104.1 主源切换（盘中实时不走东财）：腾讯(股票) + 新浪缓存(期货)为主，东财降为兜底。
    st_codes = sorted({str(c) for c in alerts if re.match(r"^[0-9]{6}$", str(c))})
    fut_codes = sorted({str(r.get("secid", "").split(".")[-1])
                        for rules in alerts.values() for r in rules
                        if isinstance(r, dict) and r.get("enabled")
                        and "." in str(r.get("secid", ""))
                        and not re.match(r"^[01]\.", str(r.get("secid", "")))})
    arr = None
    # 主源1：腾讯批量（股票，ACAO=* 无风控问题，字段 1名称 3现价 31涨跌 32pct）
    if st_codes:
        try:
            tq = ",".join(("sh" if c.startswith(("6", "9")) else "sz") + c for c in st_codes)
            body = _http_get_raw("https://qt.gtimg.cn/q=" + tq,
                                 {"User-Agent": "Mozilla/5.0"}, timeout=8).decode("gbk", "replace")
            rows = []
            for m in re.finditer(r'v_(?:sh|sz|bj)(\d{6})="([^"]*)"', body):
                f = m.group(2).split("~")
                if len(f) > 32 and f[1]:
                    try:
                        rows.append({"f12": m.group(1), "f14": f[1],
                                     "f2": float(f[3]), "f3": float(f[32]),
                                     "f4": float(f[31]) if f[31] else None})
                    except Exception:
                        pass
            if rows:
                arr = rows
        except Exception:
            arr = None
    # 主源2：新浪期货缓存（今天建的 _sina_fut_quotes，期货告警共用）
    if fut_codes:
        try:
            fq, fresh = _sina_fut_quotes()
        except Exception:
            fq, fresh = {}, False
        for c in fut_codes:
            v = fq.get(c.lower())
            if v:
                arr = arr or []
                arr.append({"f12": c, "f14": v.get("name"), "f2": v.get("price"),
                            "f3": v.get("pct"), "f4": v.get("chg")})
    if arr and time.time() - _ALERT_SRC_LOG["ts"] >= 60:
        _ALERT_SRC_LOG["ts"] = time.time()
        print("[告警主源] 腾讯/新浪取 %d 只（东财未请求）" % len(arr), flush=True)
    if not arr:
        # 兜底：腾讯+新浪都失败才碰东财（原 v92 路径）
        url = "https://push2delay.eastmoney.com/api/qt/ulist.np/get?secids=" + ",".join(secids) +               "&fields=" + ALERT_FIELDS + "&fltt=2&invt=2&np=1&pn=1&pz=" + str(len(secids))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                j = json.loads(resp.read().decode("utf-8"))
            arr = (j.get("data") or {}).get("diff") or []
            if not isinstance(arr, list) or not arr:
                arr = None
        except Exception:
            arr = None
        if arr is None:
            bq = _backup_quotes_stock(st_codes)
            bq.update(_backup_quotes_fut(fut_codes))
            if not bq:
                print("[告警兜底] 主源与东财均失败，本轮跳过", flush=True)
                return
            arr = [{"f12": c, "f14": v.get("name"), "f2": v.get("price"),
                    "f3": v.get("pct"), "f4": v.get("chg")} for c, v in bq.items()]
            print("[告警兜底] 东财ulist失败，已用腾讯/新浪备源取 %d 只" % len(arr), flush=True)
        else:
            print("[告警兜底] 主源失败，已回退东财取 %d 只" % len(arr), flush=True)
    by_code = {}
    for x in arr:
        by_code[str(x.get("f12")).lower()] = x
    now = time.time()
    changed = False
    for code, rules in alerts.items():
        if not isinstance(rules, list):
            continue
        x = by_code.get(str(code).lower())
        if not x:
            continue
        name = x.get("f14") or str(code)
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("enabled"):
                continue
            ttype = rule.get("type")
            if ttype not in ALERT_TYPE_FIELD:
                continue
            if not _in_session(rule.get("secid") or (("1." if str(code).startswith("6") else "0.") + str(code))):
                continue
            raw = x.get(ALERT_TYPE_FIELD[ttype][0])
            if raw is None or raw == "-" or raw == "":
                continue
            try:
                cur = float(raw)
                value = float(rule.get("value"))
            except Exception:
                continue
            op = rule.get("op", ">=")
            try:
                reset_pct = float(rule.get("resetPct", ALERT_DEFAULT_RESET) or ALERT_DEFAULT_RESET)
                cooldown = float(rule.get("cooldownS", ALERT_COOLDOWN_S) or ALERT_COOLDOWN_S)
            except Exception:
                reset_pct = ALERT_DEFAULT_RESET
                cooldown = ALERT_COOLDOWN_S
            hit = (cur >= value) if op == ">=" else (cur <= value)
            if op == ">=":
                reset_hit = cur <= value * (1 - reset_pct / 100.0)
            else:
                reset_hit = cur >= value * (1 + reset_pct / 100.0)
            if rule.get("state") == "fired":
                if reset_hit:
                    rule["state"] = "idle"
                    rule["resetAt"] = now
                    # v84.1：状态恢复通知（价格回到触发范围之外）
                    send_fn, ch = _alert_channel(rule, code)
                    ok_r, err_r = send_fn(_alert_reset_msg(name, str(code), cur, rule, x.get("f2")))
                    if not ok_r:
                        print("[股价告警] 恢复通知推送失败(%s) %s: %s" % (ch, code, err_r), flush=True)
                    changed = True
            else:
                try:
                    last_reset = float(rule.get("resetAt", 0) or 0)
                except Exception:
                    last_reset = 0
                if hit and (now - last_reset) >= cooldown:
                    send_fn, ch = _alert_channel(rule, code)
                    ok, msg = send_fn(_alert_msg(name, str(code), cur, rule, x.get("f2")))
                    if not ok:
                        print("[股价告警] 触发推送失败(%s) %s: %s" % (ch, code, msg), flush=True)
                    rule["state"] = "fired"
                    rule["firedAt"] = now
                    rule["lastErr"] = None if ok else msg
                    if rule.get("autoOff"):
                        rule["enabled"] = False
                    changed = True
    if changed:
        _save_json(ALERTS_JSON, alerts)


# ========== 公告监控引擎（v82，每分钟轮询 cninfo，新公告推送到独立微信群） ==========

def _monitor_webhook_url():
    """公告专用 webhook（monitor_webhook.json），独立于股价告警 webhook.json"""
    cfg = _load_json(MONITOR_WEBHOOK_JSON, {}) or {}
    return cfg.get("webhook", "") or ""

def _send_monitor_webhook(text, retries=2):
    url = _monitor_webhook_url()
    if not url:
        return False, "未配置公告 webhook"
    payload = {"msgtype": "text", "text": {"content": text}}
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                rj = json.loads(resp.read().decode("utf-8"))
            if rj.get("errcode") == 0:
                return True, "ok"
            return False, "企业微信返回: %s" % rj.get("errmsg", rj)
        except Exception as e:
            if i >= retries:
                return False, str(e)
            time.sleep(1)
    return False, "unknown"

# ---------- 财经日历（v86：华尔街见闻 macrodatas，无鉴权；今日+未来14天，二星以上） ----------
CAL_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar.json")
CAL_INTERVAL_S = 300
WSCN_MACRO_URL = "https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas"

_cal_lock = threading.Lock()   # 手动/自动抓取互斥（防 tmp 碰撞）
CAL_REFETCH_COOLDOWN_S = 60     # 手动刷新冷却（秒）
_last_cal_refetch = {"ts": 0}   # 手动刷新上次成功时刻（handler 冷却用）
CAL_META_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calmeta.json")  # diff 元数据 sidecar
CALPUSH_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calpush.json")   # 日历推送配置（key 不入代码）

def _cal_diff_and_notify(old_items, new_items):
    """L1: 按 id diff 新旧快照 + L2 标记注入 + L3 推送。冷启动静默吸收。须在写盘前调用。"""
    was_boot = False
    old_map = {}
    for it in old_items:
        if it.get("id"):
            old_map[str(it["id"])] = it  # 统一字符串键（JSON 往返保真）
    meta = _load_json(CAL_META_JSON, {}) or {}
    was_boot = bool(meta.get("bootstrapped"))
    events = meta.get("events", {})
    now_ts = time.time()
    # 窗口尾部日（本轮窗口最晚日期）＝滚动新增的第 14 天：该日事件属于「主动翻看」范畴，不推送不标新
    tail_day = max((it.get("date", "") for it in new_items if it.get("date")), default="")
    new_ids = []
    upgraded = []
    for it in new_items:
        eid = str(it.get("id")) if it.get("id") else ""
        if not eid:
            continue
        old = old_map.get(eid)
        rec = events.get(eid) or {}
        on_tail = bool(tail_day) and it.get("date") == tail_day
        # 尾部日初始批次（tail_day 首轮进入窗口时已存在）＝主动翻看范畴；同日后续新增＝普通新事件（推送+标记）
        is_tail_initial = on_tail and meta.get("tail_day") != tail_day
        if old is None and eid not in events:
            # 全新事件（冷启动/旧文件丢失时静默吸收不推；尾部日初始批次不推不标新）
            if was_boot and old_items and not is_tail_initial:
                new_ids.append(eid)
            rec.setdefault("first_seen", now_ts)
            if is_tail_initial:
                rec["tail"] = True
        # 变更检测（同 id 对比旧快照）
        if old is not None:
            old_imp, new_imp = old.get("imp"), it.get("imp")
            if new_imp and old_imp and new_imp > old_imp:
                rec["last_changed"] = now_ts
                rec["upgraded"] = True
                upgraded.append(eid)
            elif old.get("t") != it.get("t") or old.get("title") != it.get("title"):
                rec["last_changed"] = now_ts
        events[eid] = rec
    # 清理已掉出窗口的事件（窗口滚动≠删除，只清 meta 不推送）
    keep = set(str(it.get("id")) for it in new_items if it.get("id"))
    for eid in list(events.keys()):
        if eid not in keep:
            del events[eid]
    meta = {"bootstrapped": True, "updated": now_ts, "tail_day": tail_day, "events": events}
    _save_json(CAL_META_JSON, meta)
    # L2 标记注入：48h 内新增 → is_new；本轮升级 → upgraded（随 calendar.json 落盘供前端渲染）
    if was_boot:
        up_set = set(upgraded)
        for it in new_items:
            eid = str(it.get("id")) if it.get("id") else ""
            if not eid:
                continue
            rec = events.get(eid) or {}
            fs = rec.get("first_seen")
            if fs and (time.time() - fs) < 48 * 3600 and not rec.get("tail"):
                it["is_new"] = True  # 初始批次（tail）永不标新；尾部日中途新增照常标
            if eid in up_set:
                it["upgraded"] = True
    # L3 推送（冷启动不推）
    if new_ids or upgraded:
        try:
            _cal_push_notify(new_ids, upgraded, new_items, old_map)
        except Exception as pe:
            print("[日历推送] 异常: %s" % str(pe)[:80], flush=True)
    return new_ids, upgraded

def _cal_push_notify(new_ids, upgraded, new_items, old_map):
    """L3: 合并推送新增/升级事件（calpush.json 配置；免打扰时段跳过）。"""
    cfg = _load_json(CALPUSH_JSON, {}) or {}
    url = (cfg.get("webhook") or "").strip()
    if not url or not cfg.get("enabled", True):
        return
    by_id = {}
    for it in new_items:
        if it.get("id"):
            by_id[str(it["id"])] = it  # JSON 往返 id 变 int，统一 str 键与 new_ids/eid 一致
    min_imp = int(cfg.get("min_imp", 3) or 3)
    lines = []
    if cfg.get("notify_new", True):
        for eid in new_ids:
            it = by_id.get(eid)
            if not it or it.get("imp", 0) < min_imp:
                continue
            lines.append("%s %s %s %s%s" % ("[新增]", it.get("date", "")[5:].replace("-", "/"), it.get("t", ""), it.get("title", ""), " ★" * it.get("imp", 0)))
    if cfg.get("notify_upgrade", True):
        for eid in upgraded:
            it = by_id.get(eid)
            if not it:
                continue
            old_imp = (old_map.get(eid) or {}).get("imp")
            lines.append("%s %s %s %s（%s→%s★）" % ("[升级]", it.get("date", "")[5:].replace("-", "/"), it.get("t", ""), it.get("title", ""), old_imp, it.get("imp")))
    if not lines:
        # 可观测：候选全被过滤时留痕，避免再出现静默丢推
        print("[日历推送] 候选 %d 条未生成内容（低于 min_imp=%d 或 id 未命中）" % (len(new_ids) + len(upgraded), min_imp), flush=True)
        return
    # 免打扰时段：跳过（简化为直接跳过不累积）
    qs, qe = str(cfg.get("quiet_start", "")), str(cfg.get("quiet_end", ""))
    hm = time.strftime("%H:%M")
    if qs and qe:
        if qs <= qe:
            in_quiet = qs <= hm < qe
        else:
            in_quiet = hm >= qs or hm < qe
        if in_quiet:
            print("[日历推送] 免打扰(%s-%s)跳过 %d 条" % (qs, qe, len(lines)), flush=True)
            return
    # v106：分片发送（企微文本上限 2048 字节，每片留余量按 ≤1900 字节装填），超限不再截断
    MAX_BYTES = 1900
    chunks = []
    cur_lines, cur_bytes = [], len("【财经日历 · 重要事件更新】".encode("utf-8")) + 1
    for ln in lines:
        b = len(ln.encode("utf-8")) + 1   # +1 换行
        if cur_lines and cur_bytes + b > MAX_BYTES:
            chunks.append(cur_lines)
            cur_lines, cur_bytes = [], 0
        cur_lines.append(ln)
        cur_bytes += b
    if cur_lines:
        chunks.append(cur_lines)
    if len(chunks) == 1:
        text = "【财经日历 · 重要事件更新】" + chr(10) + chr(10).join(chunks[0])
        ok, err = _send_webhook_url(url, text)
        print("[日历推送] %s（%d 条）" % ("成功" if ok else "失败:" + str(err)[:60], len(lines)), flush=True)
    else:
        total, all_ok, last_err = len(lines), True, None
        for idx, cl in enumerate(chunks, 1):
            head = "【财经日历 · 重要事件更新】(%d/%d)" % (idx, len(chunks)) if len(chunks) > 1 else "【财经日历 · 重要事件更新】"
            text = head + chr(10) + chr(10).join(cl)
            ok, err = _send_webhook_url(url, text)
            all_ok = all_ok and ok
            if not ok:
                last_err = err
            time.sleep(1.0)   # 片间隔，防企微频控
        print("[日历推送] %s（%d 条 / %d 片）" % ("成功" if all_ok else "部分失败:" + str(last_err)[:60], total, len(chunks)), flush=True)

def _send_webhook_url(url, text, retries=2):
    """指定 URL 的 webhook 发送（日历独立配置用）。"""
    payload = {"msgtype": "text", "text": {"content": text}}
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                rj = json.loads(resp.read().decode("utf-8"))
            if rj.get("errcode") == 0:
                return True, None
            err = "errcode=%s" % rj.get("errcode")
        except Exception as e:
            err = str(e)[:80]
        if i < retries:
            time.sleep(1.5)
    return False, err
_cal_last_ok = {"ts": 0}       # 上次成功抓取时刻（锁内新鲜度检查用）
# ========== 董秘互动回答监控（深交所互动易，第一阶段） ==========
IRM_SECID_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "irmsecid.json")
IRM_STATE_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "irmstate.json")
IRM_INTERVAL_S = 300
IRM_MAX_SEEN = 2000
IRM_PUSH_COOLDOWN_S = 60
_IRM_SSE_INTERVAL_S = 300
IRM_SSE_SECID_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "irmsseuid.json")
IRM_SSE_STATE_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "irmssestate.json")
_IRM_SSE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36"
_last_irm_sse_push = {"ts": 0}

CAL_STATUS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calstatus.json")
_last_irm_push = {"ts": 0}

def _irm_query_secid(keyword):
    """互动易公司搜索：名称/代码 → secid。"""
    try:
        body = "keyWord=" + urllib.parse.quote(keyword)
        req = urllib.request.Request("https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo?_t=%d" % int(time.time() * 1000),
                                     data=body.encode("utf-8"),
                                     headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
                                              "Referer": "https://irm.cninfo.com.cn/ircs/search",
                                              "X-Requested-With": "XMLHttpRequest"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        for it in (j.get("data") or []):
            if str(it.get("stockCode")) == keyword or keyword in str(it.get("shortName") or ""):
                return it.get("secid")
        return None
    except Exception as e:
        print("[互动易] secid 查询失败 %s: %s" % (keyword, str(e)[:60]), flush=True)
        return None

def _irm_get_secids(monitor_items):
    """批量取深市股 secid（缓存 irmsecid.json）；沪市 6 开头跳过。"""
    cache = _load_json(IRM_SECID_JSON, {})
    changed = False
    out = {}
    for it in monitor_items:
        code = str(it.get("code") or "")
        if not code.startswith(("0", "3")):
            continue
        if code in cache:
            out[code] = cache[code]
            continue
        secid = _irm_query_secid(code)
        if secid:
            cache[code] = secid
            out[code] = secid
            changed = True
        time.sleep(0.5)
    if changed:
        _save_json(IRM_SECID_JSON, cache)
    return out

def _irm_fetch_answers(secids):
    """查互动易已回答列表（infoTypes=11）。失败返回 None。"""
    if not secids:
        return []
    codes_param = ",".join("%s_%s" % (secid, code) for code, secid in sorted(secids.items()))
    start = time.strftime("%Y-%m-%d 00:00:00", time.localtime(time.time() - 30 * 86400))
    end = time.strftime("%Y-%m-%d %H:%M:%S")
    url = ("https://irm.cninfo.com.cn/newircs/search/searchResult?_t=%d" % int(time.time() * 1000)
          + "&stockCodes=" + urllib.parse.quote(codes_param)
          + "&keywords=&infoTypes=11"
          + "&startDate=" + urllib.parse.quote(start)
          + "&endDate=" + urllib.parse.quote(end)
          + "&pageNum=1&pageSize=50&onlyAttentionCompany=2")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://irm.cninfo.com.cn/ircs/search"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        rows = ((j.get("data") or {}).get("results")) or []
        name_by_code = {str(r.get("stockCode")): r.get("companyShortName") for r in rows}
        out = []
        for r in rows:
            ans = (r.get("attachedContent") or "").strip()
            if not ans:
                continue
            apd = r.get("attachedPubDate")
            ts = int(apd) / 1000 if apd else None
            out.append({"id": str(r.get("esId")), "code": str(r.get("stockCode")),
                        "name": name_by_code.get(str(r.get("stockCode")), ""),
                        "q": (r.get("mainContent") or "").strip(), "a": ans, "ts": ts})
        return out
    except Exception as e:
        print("[互动易] 查询失败: %s" % str(e)[:80], flush=True)
        return None

def _irm_monitor_items():
    """取公告监控列表（同一批股票）作为互动监控对象。"""
    data = _load_json(MONITOR_JSON, {}) or {}
    return data.get("monitored") or []

def _irm_check_and_notify():
    """单轮互动易检查：diff 新回答 → 合并推送。冷启动静默吸收；空结果不破坏 seen。"""
    monitor_items = _irm_monitor_items()
    if not monitor_items:
        return
    secids = _irm_get_secids(monitor_items)
    answers = _irm_fetch_answers(secids)
    if answers is None:
        return
    state = _load_json(IRM_STATE_JSON, {}) or {}
    seen = set(state.get("seen") or [])
    bootstrapped = bool(state.get("bootstrapped"))
    fresh = [a for a in answers if a["id"] not in seen]
    if not bootstrapped:
        seen |= set(a["id"] for a in answers)
        state = {"bootstrapped": True, "seen": list(seen)[-IRM_MAX_SEEN:]}
        _save_json(IRM_STATE_JSON, state)
        print("[互动易] 基线吸收 %d 条回答" % len(answers), flush=True)
        return
    if fresh:
        seen |= set(a["id"] for a in fresh)
        state["seen"] = list(seen)[-IRM_MAX_SEEN:]
        _save_json(IRM_STATE_JSON, state)
        # 时间窗过滤：只推 24h 内的回答（防新加监控股/基线残缺把 30 天历史全量补推）
        cutoff = time.time() - 24 * 3600
        recent = [a for a in fresh if (a["ts"] or 0) >= cutoff]
        if len(recent) < len(fresh):
            print("[互动易] %d 条超 24h 窗仅吸收不推送" % (len(fresh) - len(recent)), flush=True)
        fresh = recent
        if not fresh:
            return
        lines = []
        for a in sorted(fresh, key=lambda x: x["ts"] or 0)[-8:]:
            q = (a["q"] or "").replace(chr(10), " ")[:60]
            ans = (a["a"] or "").replace(chr(10), " ")[:100]
            lines.append("%s(%s)" % (a["name"], a["code"]))
            lines.append("❓ " + q)
            lines.append("💬 " + ans)
        now = time.time()
        if now - _last_irm_push["ts"] >= IRM_PUSH_COOLDOWN_S:
            _last_irm_push["ts"] = now
            murl = _monitor_webhook_url()
            if murl:
                text = "【董秘互动 · 新回答】" + chr(10) + chr(10).join(lines[:24])
                ok, err = _send_webhook_url(murl, text)
                print("[互动易推送] %s（%d 条）" % ("成功" if ok else "失败:" + str(err)[:60], len(fresh)), flush=True)
        else:
            print("[互动易推送] 冷却中跳过 %d 条" % len(fresh), flush=True)
        print("[互动易] 新回答 %d 条" % len(fresh), flush=True)

def _irm_loop():
    while True:
        try:
            _irm_check_and_notify()
        except Exception as e:
            print("[互动易] 轮询异常: %s" % str(e)[:80], flush=True)
        time.sleep(IRM_INTERVAL_S)


def _irm_sse_get(url, referer, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": _IRM_SSE_UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


_TAG_RE = None

def _irm_sse_clean(txt):
    global _TAG_RE
    if _TAG_RE is None:
        _TAG_RE = re.compile(r'<[^>]+>')
    txt = _TAG_RE.sub(' ', txt)
    txt = html.unescape(txt)
    return re.sub(r'\s+', ' ', txt).strip()


def _irm_sse_query_uid(code):
    '''company.do 解析公司 uid（双模式：uid=数字 / uid="数字"）。'''
    try:
        comp = _irm_sse_get('http://sns.sseinfo.com/company.do?stockcode=' + code,
                            'http://sns.sseinfo.com/')
        m = re.search(r'uid=(\d+)', comp) or re.search(r'uid="(\d+)"', comp)
        return m.group(1) if m else None
    except Exception as e:
        print('[e互动] uid 查询失败 %s: %s' % (code, str(e)[:60]), flush=True)
        return None


def _irm_sse_get_uids(monitor_items):
    '''批量取沪市股 uid（缓存 irmsseuid.json）；只处理 60/68 开头，深市与 B 股跳过。'''
    cache = _load_json(IRM_SSE_SECID_JSON, {})
    changed = False
    out = {}
    for it in monitor_items:
        code = str(it.get('code') or '')
        if not (code.startswith('60') or code.startswith('68')):
            continue
        if code in cache:
            out[code] = cache[code]
            continue
        uid = _irm_sse_query_uid(code)
        if uid:
            cache[code] = uid
            out[code] = uid
            changed = True
            print('[e互动] uid 解析 %s → %s' % (code, uid), flush=True)
        time.sleep(0.5)
    if changed:
        _save_json(IRM_SSE_SECID_JSON, cache)
    return out


def _irm_sse_fetch_answers(uid_by_code):
    '''逐只拉 type=11（董秘已回复）feed；返回 [{id,code,name,q,a,ts}]；全部失败返回 None。'''
    if not uid_by_code:
        return []
    out = []
    err_cnt = 0
    for code, uid in sorted(uid_by_code.items()):
        try:
            body = _irm_sse_get(
                'http://sns.sseinfo.com/ajax/userfeeds.do?typeCode=company&type=11&pageSize=100&uid=%s&page=1' % uid,
                'http://sns.sseinfo.com/company.do?stockcode=' + code)
        except Exception as e:
            err_cnt += 1
            print('[e互动] 抓取失败 %s: %s' % (code, str(e)[:60]), flush=True)
            time.sleep(0.6)
            continue
        time.sleep(0.6)
        name = ''
        segs = re.split(r'<div class="m_feed_item"\s+id="item-', body)
        for seg in segs[1:]:
            m = re.match(r'(\d+)', seg)
            if not m:
                continue
            iid = m.group(1)
            qa_at = seg.find('m_feed_detail m_qa')
            if qa_at < 0:
                continue
            q_part, a_part = seg[:qa_at], seg[qa_at:]
            if not name:
                mn = re.search(r'<img title="([^"]+)"[^>]*avatar/company', a_part)
                if mn:
                    name = mn.group(1)
            mq = re.search(r'<div class="m_feed_txt"[^>]*>(.*?)</div>', q_part, re.S)
            question = _irm_sse_clean(mq.group(1)) if mq else ''
            question = re.sub(r'^[:：]?\S+?\(\d{6}\)', '', question).strip()
            ma = re.search(r'<div class="m_feed_txt"[^>]*>(.*?)</div>', a_part, re.S)
            answer = _irm_sse_clean(ma.group(1)) if ma else ''
            if not answer:
                continue
            at = re.search(r'(\d{4})年(\d{2})月(\d{2})日\s*(\d{2}):(\d{2})', a_part)
            ats = ('%s-%s-%s %s:%s' % at.groups()) if at else ''
            out.append({'id': iid, 'code': code, 'name': name, 'q': question, 'a': answer, 'ts': ats})
    if err_cnt and err_cnt >= len(uid_by_code):
        return None
    return out


def _irm_sse_check_and_notify():
    '''单轮 e互动检查：diff 新回复 → 合并推送公告监控群。冷启动静默吸收；空结果不破坏 seen。'''
    monitor_items = _irm_monitor_items()
    uid_by_code = _irm_sse_get_uids(monitor_items)
    answers = _irm_sse_fetch_answers(uid_by_code)
    if answers is None:
        return
    state = _load_json(IRM_SSE_STATE_JSON, {}) or {}
    seen = set(str(x) for x in (state.get('seen') or []))
    bootstrapped = bool(state.get('bootstrapped'))
    fresh = [a for a in answers if str(a['id']) not in seen]
    if not bootstrapped:
        seen |= set(str(a['id']) for a in answers)
        state = {'bootstrapped': True, 'seen': list(seen)[-IRM_MAX_SEEN:]}
        _save_json(IRM_SSE_STATE_JSON, state)
        print('[e互动] 基线吸收 %d 条回复' % len(answers), flush=True)
        return
    if fresh:
        seen |= set(str(a['id']) for a in fresh)
        state['seen'] = list(seen)[-IRM_MAX_SEEN:]
        _save_json(IRM_SSE_STATE_JSON, state)
        # 时间窗过滤：只推 24h 内的回复（防首轮超时/基线残缺导致历史问答补推）
        cutoff = time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() - 24 * 3600))
        recent = [a for a in fresh if (a['ts'] or '') >= cutoff]
        if len(recent) < len(fresh):
            print('[e互动] %d 条超 24h 窗仅吸收不推送' % (len(fresh) - len(recent)), flush=True)
        fresh = recent
        if not fresh:
            return
        lines = []
        for a in sorted(fresh, key=lambda x: x['ts'] or '')[-8:]:
            q = (a['q'] or '').replace(chr(10), ' ')[:60]
            ans = (a['a'] or '').replace(chr(10), ' ')[:100]
            lines.append('%s(%s)' % (a['name'] or a['code'], a['code']))
            lines.append('Q: ' + q)
            lines.append('A: ' + ans)
        if lines:
            now = time.time()
            if now - _last_irm_sse_push['ts'] >= IRM_PUSH_COOLDOWN_S:
                _last_irm_sse_push['ts'] = now
                murl = _monitor_webhook_url()
                if murl:
                    text = '【董秘互动 · 新回复(沪)】' + chr(10) + chr(10).join(lines[:24])
                    ok, err = _send_webhook_url(murl, text)
                    print('[e互动推送] %s（%d 条）' % ('成功' if ok else '失败:' + str(err)[:60], len(fresh)), flush=True)
            else:
                print('[e互动推送] 冷却中跳过 %d 条' % len(fresh), flush=True)
        print('[e互动] 新回复 %d 条' % len(fresh), flush=True)


def _irm_sse_loop():
    while True:
        try:
            _irm_sse_check_and_notify()
        except Exception as e:
            print('[e互动] 轮询异常: %s' % str(e)[:80], flush=True)
        time.sleep(_IRM_SSE_INTERVAL_S)


def _fetch_calendar(min_gap=0):
    """抓华尔街见闻财经日历（今日+未来14天，importance>=2）→ calendar.json（原子写；失败保留旧文件）。
    min_gap>0 时（手动抓取路径）：距上次成功抓取不足 min_gap 秒则跳过网络请求直接返回。
    全程持 _cal_lock：与自动轮询互斥，消除 tmp 碰撞窗口。"""
    with _cal_lock:
        try:
            if min_gap > 0 and _cal_last_ok["ts"] and (time.time() - _cal_last_ok["ts"]) < min_gap:
                return True, "fresh", 0
            return _fetch_calendar_inner()
        except Exception as e:
            print("[日历] 抓取失败: %s" % str(e)[:80], flush=True)
            try:
                cur = _load_json(CAL_STATUS_JSON, {}) or {}
                cur["last_fail"] = {"ts": time.time(), "err": str(e)[:100], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                _save_json(CAL_STATUS_JSON, cur)
            except Exception:
                pass
            return False, str(e)[:80], 0

def _fetch_calendar_inner():
    try:
        import datetime as _dt
        now = _dt.datetime.now()
        start = int(time.mktime(now.replace(hour=0, minute=0, second=0, microsecond=0).timetuple()))
        end = start + 14 * 86400
        params = urllib.parse.urlencode({"start": start, "end": end})
        req = urllib.request.Request(WSCN_MACRO_URL + "?" + params, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://wallstreetcn.com/calendar",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        items = []
        for a in ((j or {}).get("data", {}) or {}).get("items") or []:
            try:
                imp = int(a.get("importance"))
                ts = int(a.get("public_date"))
            except Exception:
                continue
            if imp < 2 or not ts:
                continue
            items.append({
                "id": a.get("id"), "ts": ts,
                "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
                "t": time.strftime("%H:%M", time.localtime(ts)),
                "cc": str(a.get("country_id") or ""), "country": str(a.get("country") or ""),
                "title": str(a.get("title") or "").strip(),
                "imp": imp, "type": str(a.get("calendar_type") or ""),
                "prev": str(a.get("previous") or ""), "fcst": str(a.get("forecast") or ""),
                "actual": str(a.get("actual") or ""), "period": str(a.get("period") or ""),
                "foresight": str(a.get("foresight") or ""),
            })
        items.sort(key=lambda x: x["ts"])
        if not items:
            # 接口瞬时异常（HTTP 200 但 data 为空）时不写盘，保留上一轮数据
            print("[日历] 返回 0 条，保留旧数据不写盘", flush=True)
            try:
                cur = _load_json(CAL_STATUS_JSON, {}) or {}
                cur["last_fail"] = {"ts": time.time(), "err": "接口返回 0 条（数据源异常）", "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                _save_json(CAL_STATUS_JSON, cur)
            except Exception:
                pass
            return True, "empty", 0
        # L1 diff + L2 标记注入 + L3 推送（写盘前：prev=旧文件内容；payload.items 引用同一 list，注入的标记随盘落库）
        prev = _load_json(CAL_JSON, {}) or {}
        try:
            _cal_diff_and_notify(prev.get("items", []), items)
        except Exception as de:
            print("[日历] diff/推送异常(不影响数据): %s" % str(de)[:80], flush=True)
        payload = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items}
        tmp = CAL_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, CAL_JSON)
        _cal_last_ok["ts"] = time.time()
        _save_json(CAL_STATUS_JSON, {"last_ok": {"ts": time.time(), "count": len(items), "at": time.strftime("%Y-%m-%d %H:%M:%S")}, "last_fail": (_load_json(CAL_STATUS_JSON, {}) or {}).get("last_fail")})
        print("[日历] %d 条（二星以上）→ calendar.json" % len(items), flush=True)
        return True, "ok", len(items)
    except Exception as e:
        print("[日历] 抓取失败: %s" % str(e)[:80], flush=True)
        return False, str(e)[:80], 0

def _calendar_loop():
    while True:
        _fetch_calendar()
        time.sleep(CAL_INTERVAL_S)

def fetch_announcements(codes, days=MONITOR_DAYS):
    """抓取指定股票最近 days 天的公告列表（东财 np-anotice-stock 批量接口，无鉴权）。

    codes: str 单只股票代码，或 list 多只。返回 {code: [{id,title,time,url}]}。
    兼容旧调用（传单 code 返回 dict 而非 list；调用方已同步改造）。
    """
    import datetime as _dt
    if isinstance(codes, str):
        codes = [codes]
    codes = [c for c in codes if c]
    if not codes:
        return {}
    params = {
        "sr": -1, "page_size": 100, "page_index": 1, "ann_type": "A",
        "client_source": "web", "stock_list": ",".join(codes),
        "f_node": 0, "s_node": 0,
    }
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://data.eastmoney.com/notices/",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    out = {c: [] for c in codes}
    for a in (j.get("data", {}).get("list") or []):
        art = str(a.get("art_code") or "").strip()
        if not art:
            continue
        title = str(a.get("title") or "").strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", str(a.get("display_time") or ""))
        tstr = m.group(1) if m else ""
        for c in (a.get("codes") or []):
            sc = str(c.get("stock_code") or "").strip()
            if sc in out:
                url2 = "https://data.eastmoney.com/notices/detail/%s/%s.html" % (sc, art)
                out[sc].append({"id": art, "title": title, "time": tstr, "url": url2})
    for c in out:
        out[c].sort(key=lambda x: x["time"])
    return out

def _monitor_once():
    with _monitor_lock:
        """单轮公告检查：遍历监控列表，发现新公告推送。返回 (ok, msg)。"""
        data = _load_json(MONITOR_JSON, {}) or {}
        lst = data.get("monitored") if isinstance(data.get("monitored"), list) else []
        if not lst:
            return True, "无监控股票"
        if not _monitor_webhook_url():
            return False, "未配置公告 webhook"
        for _c in list(_monitor_fail):
            if _c not in {str(x.get("code") or "").strip() for x in lst}:
                del _monitor_fail[_c]
        changed = False
        errors = []
        batch_ok = False  # 批量查询是否成功（失败时禁止吸收基线，防空基线下轮全推）
        codes_all = [str(x.get("code") or "").strip() for x in lst if str(x.get("code") or "").strip()]
        try:
            all_anns = fetch_announcements(codes_all) if codes_all else {}
            batch_ok = True
        except Exception as e:
            all_anns = {}
            err = str(e)[:60]
            for item in lst:
                code = str(item.get("code") or "").strip()
                if not code:
                    continue
                errors.append("%s: %s" % (code, err))
                st = _monitor_fail.setdefault(code, {"count": 0, "lastAlert": 0})
                st["count"] += 1
                now_ts = time.time()
                if st["count"] >= MONITOR_FAIL_ALERT_ROUNDS and (now_ts - st["lastAlert"]) >= MONITOR_FAIL_ALERT_COOLDOWN_S:
                    name = str(item.get("name") or code)
                    msg = ("【公告监控】数据源查询持续失败" + chr(10) +
                           "股票：%s(%s)" + chr(10) +
                           "连续失败：%d 轮" + chr(10) +
                           "最近错误：%s") % (name, code, st["count"], err)
                    ok2, err2 = _send_monitor_webhook(msg)
                    if ok2:
                        st["lastAlert"] = now_ts
                    else:
                        print("[公告监控] 失败告警推送失败 %s: %s" % (code, err2), flush=True)
        for item in lst:
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            seen = set(str(x) for x in (item.get("seen_ids") or []))
            anns = all_anns.get(code) or []
            # 查询成功（即使空列表）：清零连续失败计数
            if code in _monitor_fail and _monitor_fail[code]["count"]:
                _monitor_fail[code]["count"] = 0
            if not item.get("baseline"):
                # 新加入监控的股票：首轮静默吸收窗口内全部公告建立基线，不推送
                # （否则历史公告会全部当新公告推一遍，8/26 16:31 事故同源）
                if not batch_ok:
                    continue  # 查询失败轮吸收会写出空基线 → 恢复后历史公告全推（8/28 金风事故次因）
                item["seen_ids"] = [str(a["id"]) for a in anns][-500:]
                item["baseline"] = True
                changed = True
                continue
            # v85：只推"未见过 且 发布时间晚于监控起点"的公告。
            # seen_ids 只能去重窗口内已见项；共享窗口扩容（移除其他股票腾槽）会让更早的
            # 历史公告进入窗口被当新公告推（8/28 金风科技 20→30 条事故）。baseline_ts 兜底。
            bts = str(item.get("baseline_ts") or "")
            candidates = [a for a in anns if a["id"] not in seen]  # 全部未见项（含被时间下限挡掉的旧公告）
            new_ones = [a for a in candidates if (not bts or (a.get("time") or "") >= bts)]
            if not candidates:
                continue  # 窗口内全部已见
            name = str(item.get("name") or code)
            for a in sorted(new_ones, key=lambda x: x["time"]):
                msg = ("【公告监控】%s(%s)" + chr(10) + "📄 %s" + chr(10) + "🗓 %s" + chr(10) + "🔗 %s") % (name, code, a["title"], a["time"], a["url"] or "无链接")
                ok, err = _send_monitor_webhook(msg)
                if not ok:
                    errors.append("%s 推送失败: %s" % (code, err))
            # 无论推送成功与否，都记为已见（避免重复推送）
            item["seen_ids"] = [str(x) for x in (seen | set(a["id"] for a in anns))][-500:]
            changed = True
        if changed:
            _save_json(MONITOR_JSON, data)
        if errors:
            return False, "; ".join(errors[:3])
        return True, "ok"

def monitor_loop():
    while True:
        try:
            _monitor_once()
        except Exception as e:
            print("[公告监控] 异常: %s" % e, flush=True)
        time.sleep(MONITOR_INTERVAL_S)

# ========== 每日收盘后自动重抓 data.json（15:05，工作日，幂等） ==========

def _refetched_after(h, m):
    # 判断今天 h:m 之后是否已经抓取过（读 data.json 的 fetched_at）
    try:
        with open(DATA_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
        ft = str(d.get("fetched_at", "") or "")
        if ft.startswith(time.strftime("%Y-%m-%d")) and ft[11:16] >= "%02d:%02d" % (h, m):
            return True
    except Exception:
        pass
    return False


def _today_refetched():
    # 判断今天 15:05 之后是否已经抓取过（收盘快照幂等判定，语义不变）
    return _refetched_after(*REFETCH_AT)

def _refetch_force():
    # 手动强制重抓：并发锁 + 冷却；返回 (ok, msg)
    if not _refetch_lock.acquire(blocking=False):
        return (False, "已有抓取任务进行中，请稍候", 0)
    try:
        now = time.time()
        left = REFETCH_COOLDOWN_S - (now - _last_refetch["ts"])
        if left > 0:
            return (False, "抓取冷却中，%.0f 秒后再试" % left, int(left))
        print("[手动重抓] 开始抓取全市场数据…", flush=True)
        r = subprocess.run([sys.executable, "fetch_data.py"], cwd=BASE,
                           capture_output=True, text=True, timeout=960,   # v105: 2400→960 快失败（fetch_data 预算 900s），恢复权交给 180s 补抓
                           encoding="utf-8", errors="replace",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        if r.returncode == 0:
            _last_refetch["ts"] = time.time()
            print("[手动重抓] 完成 → data.json / global.json / futures.json", flush=True)
            return (True, "抓取完成", 0)
        tail = (r.stderr or r.stdout or "")[-300:]
        print("[手动重抓] 失败 rc=%s: %s" % (r.returncode, tail), flush=True)
        return (False, "抓取失败：" + tail[:100], 0)
    except Exception as e:
        print("[手动重抓] 异常: %s" % e, flush=True)
        return (False, "抓取异常：" + str(e)[:100], 0)
    finally:
        _refetch_lock.release()

def _refetch_if_needed():
    if _today_refetched():
        print("[数据自动刷新] 今天 %02d:%02d 后已抓取，跳过" % REFETCH_AT, flush=True)
        return
    if not _refetch_lock.acquire(blocking=False):
        print("[数据自动刷新] 已有抓取任务进行中，本次自动刷新跳过", flush=True)
        return
    try:
        print("[数据自动刷新] 开始抓取全市场数据…", flush=True)
        r = subprocess.run([sys.executable, "fetch_data.py"], cwd=BASE,
                           capture_output=True, text=True, timeout=960,   # v105: 2400→960 快失败（fetch_data 预算 900s），恢复权交给 180s 补抓
                           encoding="utf-8", errors="replace",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        if r.returncode == 0:
            print("[数据自动刷新] 完成 → data.json / global.json / futures.json", flush=True)
        else:
            tail = (r.stderr or r.stdout or "")[-300:]
            print("[数据自动刷新] 失败 rc=%s: %s" % (r.returncode, tail), flush=True)
    except Exception as e:
        print("[数据自动刷新] 异常: %s" % e, flush=True)
    finally:
        _refetch_lock.release()

def _at_today(now, h, m):
    # 今天的 h:m（秒/微秒归零）
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def _next_weekday_at(now, h, m):
    # 从 now 起下一个工作日 h:m（今天该时刻已过或逢周末则顺延）
    t = _at_today(now, h, m)
    while t <= now or t.weekday() >= 5:
        t = t + datetime.timedelta(days=1)
    return t


def _snapshot_due_unfulfilled(now):
    # 快照时刻已过但 data.json 仍缺（多为抓取失败）→ 窗口内短间隔重试；窗口外/次数耗尽 → 等下一事件点
    if now.weekday() >= 5:
        return False
    if (_at_today(now, *LUNCH_REFETCH_AT) <= now <= _at_today(now, *LUNCH_WINDOW_END)
            and not _refetched_after(*LUNCH_REFETCH_AT)
            and _snap_attempt_slot("lunch") < SNAPSHOT_MAX_ATTEMPTS):
        return True
    if (_at_today(now, *REFETCH_AT) <= now <= _at_today(now, *CLOSE_WINDOW_END)
            and not _refetched_after(*REFETCH_AT)
            and _snap_attempt_slot("close") < SNAPSHOT_MAX_ATTEMPTS):
        return True
    return False


def _refetch_wake_wait():
    # 距下一次醒来的秒数：已到期未完成 → 600s 重试；否则睡到下一个事件点（11:32/15:05，取更早者），
    # 上限 3600s 心跳兜底（失败重算/时钟漂移）。触发时刻不再随服务启动相位漂移（v96 前 11:32 快照
    # 可能拖到 12:03 才触发，正是午间看盘时段显示昨日收盘价的根因）。
    if _snapshot_due_unfulfilled(datetime.datetime.now()):
        return 180   # v105: 600→180 快照未完成时 3 分钟补抓（配合 fetch_data 900s 快失败）
    now = datetime.datetime.now()
    nxt = min(_next_weekday_at(now, *LUNCH_REFETCH_AT), _next_weekday_at(now, *REFETCH_AT))
    return max(60, min((nxt - now).total_seconds(), 3600))


def daily_refetch_loop():
    while True:
        try:
            now = datetime.datetime.now()
            # 触发点 1：交易日 11:32 午休快照（窗口 11:32-13:30；幂等=当天 11:32 后已抓则跳过；每日最多 5 次）
            if (now.weekday() < 5 and _at_today(now, *LUNCH_REFETCH_AT) <= now <= _at_today(now, *LUNCH_WINDOW_END)
                    and not _refetched_after(*LUNCH_REFETCH_AT)
                    and _snap_attempt_slot("lunch") < SNAPSHOT_MAX_ATTEMPTS):
                print("[%s] [午休快照] 触发抓取（第 %d/%d 次）" % (
                    time.strftime("%H:%M:%S"), _snap_attempts["lunch"] + 1, SNAPSHOT_MAX_ATTEMPTS), flush=True)
                _snap_attempts["lunch"] += 1
                _refetch_if_needed_lunch()
            # 触发点 2：交易日 15:05 收盘快照（窗口 15:05-16:30；幂等=当天 15:05 后已抓则跳过；每日最多 5 次）
            if (now.weekday() < 5 and _at_today(now, *REFETCH_AT) <= now <= _at_today(now, *CLOSE_WINDOW_END)
                    and not _refetched_after(*REFETCH_AT)
                    and _snap_attempt_slot("close") < SNAPSHOT_MAX_ATTEMPTS):
                print("[%s] [数据自动刷新] 触发抓取（第 %d/%d 次）" % (
                    time.strftime("%H:%M:%S"), _snap_attempts["close"] + 1, SNAPSHOT_MAX_ATTEMPTS), flush=True)
                _snap_attempts["close"] += 1
                _refetch_if_needed()
            time.sleep(_refetch_wake_wait())
        except Exception:
            time.sleep(300)


def _refetch_if_needed_lunch():
    # 午休快照：与收盘快照共用锁与抓取子进程，但幂等判定独立（>= 11:32）
    if _refetch_lock.acquire(blocking=False):
        try:
            print("[午休快照] 开始抓取全市场数据…", flush=True)
            r = subprocess.run([sys.executable, "fetch_data.py"], cwd=BASE,
                               capture_output=True, text=True, timeout=960,   # v105: 2400→960 快失败（fetch_data 预算 900s），恢复权交给 180s 补抓
                               encoding="utf-8", errors="replace",
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            if r.returncode == 0:
                print("[午休快照] 完成 → data.json / global.json / futures.json", flush=True)
            else:
                tail = (r.stderr or r.stdout or "")[-300:]
                print("[午休快照] 失败 rc=%s: %s" % (r.returncode, tail), flush=True)
        except Exception as e:
            print("[午休快照] 异常: %s" % e, flush=True)
        finally:
            _refetch_lock.release()
    else:
        print("[午休快照] 已有抓取任务进行中，跳过", flush=True)

# ========== F10 财务指标（悬停详情用）：datacenter 按需缓存 + 每日失效 ==========
F10_JSON = os.path.join(BASE, "f10.json")
F10_TTL_S = 24 * 3600
_F10_LOCK = threading.Lock()
_F10_MEMORY = {}   # code -> {"data": {...}, "ts": epoch}（内存级热缓存，落盘 f10.json）

def _f10_fetch(secucode):
    """东财 datacenter F10 主财务指标（最新报告期一行）。secucode 形如 600928.SH"""
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = ('https://datacenter.eastmoney.com/securities/api/data/v1/get?'
           'reportName=RPT_F10_FINANCE_MAINFINADATA&columns=ALL'
           '&quoteColumns=&filter=(SECUCODE%3D%22' + urllib.parse.quote(secucode) + '%22)'
           '&pageNumber=1&pageSize=1&sortTypes=-1&sortColumns=REPORT_DATE&source=HSF10&client=PC')
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
        "Referer": "https://emweb.securities.eastmoney.com/"})
    j = json.loads(op.open(req, timeout=12).read().decode("utf-8"))
    rows = (j.get("result") or {}).get("data") or []
    if not rows:
        return None
    r0 = rows[0]
    def _n(x):
        try:
            v = float(x)
            return v
        except Exception:
            return None
    return {
        "reportDate": str(r0.get("REPORT_DATE") or "")[:10],
        "deductNp": _n(r0.get("KCFJCXSYJLR")),
        "deductNpYoy": _n(r0.get("KCFJCXSYJLRTZ")),
        "opCash": _n(r0.get("NETCASH_OPERATE_PK")),
        "speedRatio": _n(r0.get("GUARD_SPEED_RATIO")),
        # v106.3：备源快照无营收/净利 → F10 补位（同一请求多取 4 字段，零额外请求）
        "rev": _n(r0.get("TOTALOPERATEREVE")),
        "revYoy": _n(r0.get("TOTALOPERATEREVETZ")),
        "np": _n(r0.get("PARENTNETPROFIT")),
        "npYoy": _n(r0.get("PARENTNETPROFITTZ")),
    }

def _f10_get(code):
    """code 6 位 → 查缓存（内存/磁盘），过期则拉 F10。失败返回 None。"""
    if not re.match(r"^\d{6}$", code or ""):
        return None
    # v106.2：北交所修正——92 开头为北交所（.BJ），此前误判 .SZ 导致 351 只 BJ 股 F10 永远「—」
    if code.startswith("92"):
        secucode = code + ".BJ"
    else:
        secucode = code + ("." + ("SH" if code.startswith(("6", "9")) else "SZ"))
    now = time.time()
    ent = _F10_MEMORY.get(code)
    if ent and now - ent["ts"] < F10_TTL_S:
        return ent["data"]
    try:
        disk = _load_json(F10_JSON, {}) or {}
        ent = disk.get(code)
        if ent and now - ent.get("ts", 0) < F10_TTL_S:
            _F10_MEMORY[code] = ent
            return ent.get("data")
    except Exception:
        pass
    # v93：同 code 并发（mousemove 高频触发）时等锁而不是立刻返回 None——
    # 东财单次抓取超时 12s，这里最多等 13s；等到了先复查内存缓存（前一线程可能已抓完写入），避免重复抓取
    if not _F10_LOCK.acquire(timeout=13):
        return _F10_MEMORY.get(code, {}).get("data")
    try:
        ent = _F10_MEMORY.get(code)
        if ent and time.time() - ent["ts"] < F10_TTL_S:
            return ent["data"]
        data = _f10_fetch(secucode)
        if data is None:
            data = {"error": "no data"}
        # 错误结果内存缓存 5 分钟（过期后重试），不落盘；成功结果缓存 24h 并落盘
        if data.get("error"):
            _F10_MEMORY[code] = {"ts": now - F10_TTL_S + 300, "data": data}
        else:
            _F10_MEMORY[code] = {"ts": now, "data": data}
            try:
                disk = _load_json(F10_JSON, {}) or {}
                disk[code] = {"ts": now, "data": data}
                if len(disk) > 3000:
                    items = sorted(disk.items(), key=lambda kv: kv[1].get("ts", 0))[-2000:]
                    disk = dict(items)
                _save_json(F10_JSON, disk)
            except Exception:
                pass
        return data
    except Exception as e:
        print("[F10] %s 抓取失败: %s" % (code, str(e)[:60]), flush=True)
        return None
    finally:
        _F10_LOCK.release()
# ========== TE 中东指数 30 分钟轮询（方案A：独立轻线程，只更新 global.json 的 te 条目） ==========
GLOBAL_JSON = os.path.join(BASE, "global.json")
TE_LOOP_S = 30 * 60          # 30 分钟
_TE_LOCK = threading.Lock()  # TE 写互斥（防双线程同时写 global.json）

def _te_refresh_once():
    """抓一次 TE 并更新 global.json 的 te 条目（失败保留旧值）。返回 (ok, n_updated, msg)"""
    if not _TE_LOCK.acquire(blocking=False):
        return (False, 0, "上一轮 TE 未结束，跳过")
    try:
        try:
            if BASE not in sys.path:
                sys.path.insert(0, BASE)
            import fetch_data as _fd
        except Exception as e:
            return (False, 0, "import fetch_data 失败: %s" % e)
        try:
            te = _fd.fetch_te_mideast()
        except Exception as e:
            return (False, 0, "TE 抓取异常: %s" % e)
        if not te:
            return (False, 0, "TE 抓取为空（保留旧值）")
        try:
            with open(GLOBAL_JSON, "r", encoding="utf-8") as f:
                g = json.load(f)
        except Exception as e:
            return (False, 0, "读 global.json 失败: %s" % e)
        items = g.get("items") or []
        n = 0
        for it in items:
            if not it.get("te"):
                continue
            # code 形如 "TE:TASI" → 匹配 TE 页指数名
            name = str(it.get("code", ""))[3:]
            x = te.get(name)
            if not x:
                continue
            ch = False
            for k in ("price", "pct", "pctYTD"):
                if x.get(k) is not None and it.get(k) != x[k]:
                    it[k] = x[k]
                    ch = True
            if ch:
                n += 1
        if n:
            g["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            tmp = GLOBAL_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(g, f, ensure_ascii=False)
            os.replace(tmp, GLOBAL_JSON)
        return (True, n, "更新 %d 条" % n)
    finally:
        _TE_LOCK.release()

def _te_loop():
    # 启动先等 30s（避开服务启动高峰与全量抓取竞态）
    time.sleep(30)
    fails = 0
    while True:
        try:
            # 与全量抓取互斥：全量抓取中（900s 级）跳过本轮，避免同时写 global.json
            if _refetch_lock.acquire(blocking=False):
                _refetch_lock.release()
                ok, n, msg = _te_refresh_once()
                fails = 0 if ok else (fails + 1)
                print("[TE轮询] %s" % msg, flush=True)
            else:
                print("[TE轮询] 全量抓取进行中，本轮跳过", flush=True)
        except Exception as e:
            print("[TE轮询] 异常: %s" % e, flush=True)
        time.sleep(TE_LOOP_S)

# ========== 期货实时备源代理（v104：盘中实时脱离东财） ==========
# 背景：东财 ulist 无 CORS 头（浏览器 fetch 结构性失败）+ 本机 IP 对东财长期风控（告警日志常年切备源）。
# 前端期货 8s 实时改走 /api/fut_quotes → 新浪 hq 单请求批量；快照层 fetch_data.py 维持东财不变。
# 覆盖位实测（2026-10-12）：商品 nf_ 44 字段 价=8 昨结=10；股指/国债 nf_ 50 字段 价=0 昨结=14；
# 外盘 hf_ 15 字段 价=0 昨收=7。腾讯期货 nf_/hf_ 实测全空，期货仅新浪单源可用。
_SINA_FUT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
_FUT_SINA_MAP = {
    # (新浪代码, 类型, 展示名)——名字用于告警推送标题（v104.2 修复 B00Y(B00Y)）
    "rbm": ("nf_RB0", "nf", "螺纹钢"), "im": ("nf_I0", "nf", "铁矿石"), "jm": ("nf_J0", "nf", "焦炭"),
    "cum": ("nf_CU0", "nf", "沪铜"), "alm": ("nf_AL0", "nf", "沪铝"), "aum": ("nf_AU0", "nf", "沪金"),
    "agm": ("nf_AG0", "nf", "沪银"), "lcm": ("nf_LC0", "nf", "碳酸锂"), "psm": ("nf_PS0", "nf", "多晶硅"),
    "scm": ("nf_SC0", "nf", "原油"), "lhm": ("nf_LH0", "nf", "生猪"),
    "cm": ("nf_C0", "nf", "玉米"), "mm": ("nf_M0", "nf", "豆粕"), "TAM": ("nf_TA0", "nf", "PTA"),
    "ppm": ("nf_PP0", "nf", "聚丙烯"), "CFM": ("nf_CF0", "nf", "棉花"), "SRM": ("nf_SR0", "nf", "白糖"),
    "OIM": ("nf_OI0", "nf", "菜油"),
    "IFM": ("nf_IF0", "if", "沪深300"), "IHM": ("nf_IH0", "if", "上证50"), "ICM": ("nf_IC0", "if", "中证500"),
    "IMM": ("nf_IM0", "if", "中证1000"), "TM": ("nf_T0", "if", "十年国债"), "TFM": ("nf_TF0", "if", "五年国债"),
    "TSM": ("nf_TS0", "if", "二年国债"), "TLM": ("nf_TL0", "if", "三十国债"),
    "GC00Y": ("hf_GC", "hf", "纽约金"), "B00Y": ("hf_OIL", "hf", "布伦特原油"),
    "LCPT": ("hf_CAD", "hf", "伦敦铜"),
}
_FUT_QUOTE_MEM = {"ts": 0.0, "data": {}}

# ========== v107 指数条本地代理（腾讯源）：页面 JSONP 直连东财被封时静默空白 ==========
# 腾讯指数字段（gbk, ~ 分隔）：1名称 3现价 4昨收 31涨跌额 32涨跌幅% 37成交额(万元)
_IDX_TX_CODES = "sh000001,sz399006,sh000688,sh000300,sz399001"   # 前4渲染 + 深证成指(仅两市成交额)
_IDX_MEM = {"ts": 0.0, "data": None}
_IDX_LOCK = threading.Lock()

def _tx_idx_quotes():
    """腾讯指数批量 → {f12: {f2现价, f3涨跌幅, f12代码, f14名称, f6成交额(元)}}（对齐东财字段名，前端零适配）。
    4s 内存缓存，多页签共享。失败返回 None（前端保留旧值）。"""
    now = time.time()
    if _IDX_MEM["data"] is not None and now - _IDX_MEM["ts"] < 4:
        return _IDX_MEM["data"]
    with _IDX_LOCK:
        now = time.time()
        if _IDX_MEM["data"] is not None and now - _IDX_MEM["ts"] < 4:
            return _IDX_MEM["data"]
        rows = None
        try:
            body = _http_get_raw("https://qt.gtimg.cn/q=" + _IDX_TX_CODES,
                                 {"User-Agent": "Mozilla/5.0"}, timeout=8).decode("gbk", "replace")
            rows = {}
            for m in re.finditer(r'v_(?:sh|sz|bj)(\d{6})="([^"]*)"', body):
                f = m.group(2).split("~")
                if len(f) <= 37 or not f[1]:
                    continue
                try:
                    rows[m.group(1)] = {
                        "f12": m.group(1), "f14": f[1],
                        "f2": float(f[3]) if f[3] else None,
                        "f3": float(f[32]) if f[32] else None,
                        "f6": (float(f[37]) * 10000.0) if f[37] else None,   # 万元→元
                    }
                except ValueError:
                    continue
        except Exception as e:
            print("[指数代理] 腾讯失败: %s" % e, flush=True)
        if rows and len(rows) >= 4:
            _IDX_MEM["data"] = rows
            _IDX_MEM["ts"] = now
            return rows
        return _IDX_MEM["data"]   # 失败回退最近一次成功值（最长 10 分钟内）

_FUT_QUOTE_TTL_S = 4       # 内存缓存：多页签共享，新浪侧实际 1 请求/8s
_FUT_QUOTE_STALE_S = 600   # 新浪失败时最长回退 10 分钟内最近成功值
_SINA_FUT_LOCK = threading.Lock()

def _sina_fut_quotes():
    """新浪期货批量 → ({code小写: {price,pct,chg,time}}, fresh)。一次请求覆盖全部映射品种。"""
    now = time.time()
    if now - _FUT_QUOTE_MEM["ts"] < _FUT_QUOTE_TTL_S and _FUT_QUOTE_MEM["data"]:
        return _FUT_QUOTE_MEM["data"], True
    with _SINA_FUT_LOCK:
        now = time.time()
        if now - _FUT_QUOTE_MEM["ts"] < _FUT_QUOTE_TTL_S and _FUT_QUOTE_MEM["data"]:
            return _FUT_QUOTE_MEM["data"], True
        rows = {}
        try:
            q = ",".join(v[0] for v in _FUT_SINA_MAP.values())
            body = _http_get_raw("https://hq.sinajs.cn/list=" + q, _SINA_FUT_HEADERS, timeout=8).decode("gbk", "replace")
            rev = {v[0]: (k, v[1], v[2]) for k, v in _FUT_SINA_MAP.items()}
            for m in re.finditer(r'hq_str_(nf_|hf_)(\w+)="([^"]*)"', body):
                hit = rev.get(m.group(1) + m.group(2))
                if not hit:
                    continue
                code, kind, nm = hit
                f = m.group(3).split(",")
                if kind == "hf":
                    ip, ipv, itime = 0, 7, 6
                elif kind == "if":
                    ip, ipv, itime = 0, 14, 37
                else:
                    ip, ipv, itime = 8, 10, 17   # 商品行无盘中时间位，17=日期
                try:
                    price = float(f[ip])
                except Exception:
                    continue
                if price <= 0:
                    continue
                prev = 0.0
                try:
                    prev = float(f[ipv]) if len(f) > ipv else 0.0
                except Exception:
                    prev = 0.0
                pct = round((price / prev - 1) * 100, 2) if price > 0 and prev > 0 else None
                chg = round(price - prev, 3) if prev > 0 else None
                tstr = f[itime] if len(f) > itime else ""
                rows[code.lower()] = {"price": price, "pct": pct, "chg": chg, "time": tstr, "name": nm}
        except Exception as e:
            print("[期货备源代理] 新浪失败: %s" % e, flush=True)
        if rows:
            _FUT_QUOTE_MEM["data"] = rows
            _FUT_QUOTE_MEM["ts"] = now
            return rows, True
        if _FUT_QUOTE_MEM["data"] and now - _FUT_QUOTE_MEM["ts"] < _FUT_QUOTE_STALE_S:
            return _FUT_QUOTE_MEM["data"], False
        return {}, False

def alert_loop():
    while True:
        try:
            _check_alerts()
        except Exception:
            pass
        time.sleep(ALERT_INTERVAL_S)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    # v100 HTTP/1.1 keep-alive：移动端高 RTT 下首屏十几个请求复用一条 TCP 连接，省每次握手延迟。
    # 安全前提已审：所有响应路径（_json/gzip/静态/SimpleHTTP 404）均显式发 Content-Length，无流式端点
    Handler.protocol_version = "HTTP/1.1"
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=alert_loop, daemon=True).start()
    threading.Thread(target=daily_refetch_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=_calendar_loop, daemon=True).start()
    threading.Thread(target=_irm_loop, daemon=True).start()
    threading.Thread(target=_irm_sse_loop, daemon=True).start()
    threading.Thread(target=_te_loop, daemon=True).start()
    print("A股云图服务: http://127.0.0.1:%d/  (现货更新 /api/update | 手动重抓 /api/refetch | 备注读写 /api/notes | 日历备忘 /api/calnotes | 告警 /api/alerts)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
