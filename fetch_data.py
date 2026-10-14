import urllib.request
import re
import ssl
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股全市场行情抓取脚本 → data.json
数据源：东方财富公开行情接口（push2.eastmoney.com），免费、无需 token。
输出：按行业分组的层级 JSON，供前端热力图使用。

用法：
    python fetch_data.py                # 全量抓取并覆盖 data.json（重建行业分组）
    python fetch_data.py --incremental  # 增量刷新：只更新已有股票的价格字段（更快、请求更少）
    python fetch_data.py --quiet        # 只输出最终结果，不打印分页进度（可与上面组合）
    （全量模式同时生成 global.json：全球指数 28 条，供世界地图视图使用）
"""
import json
import os
import random
import sys
import time

# urllib 兼容层：无 requests 时降级标准库（AutoClaw 内置 python 为 embeddable 发行版，无第三方包）
try:
    import requests
except ImportError:
    import urllib.request, urllib.parse, json as _json
    class _CompatResp:
        def __init__(self, body):
            self._body = body
            self.status_code = 200
        def json(self):
            return _json.loads(self._body.decode("utf-8"))
        @property
        def text(self):
            return self._body.decode("utf-8", "replace")
    class _CompatRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            if params:
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout or 15) as resp:
                return _CompatResp(resp.read())
    requests = _CompatRequests()


# AutoClaw 内置 python 为 embeddable 发行版（python313._pth 固定 sys.path），脚本目录不在 path 中，手动加入
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)
import datalock

# 注：push2 主站对无 Referer/陌生 IP 断连较严，push2delay 延迟节点最宽松，故优先
DOMAINS = [
    "https://push2delay.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://push2.eastmoney.com",
]
BASE = "{}/api/qt/clist/get"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
# f12 代码 / f14 名称 / f2 现价 / f3 涨跌幅 / f4 涨跌额 / f6 成交额(元) / f20 总市值(元) / f100 行业
FIELDS = "f12,f14,f2,f3,f4,f6,f20,f100,f109,f160,f110,f24,f25,f115,f45,f46,f40,f41"
MARKETS = [
    ("沪主板", "m:1+t:2"),
    ("深主板", "m:0+t:6"),
    ("创业板", "m:0+t:80"),
    ("科创板", "m:1+t:23"),
]
PAGE_SIZE = 100   # 2026-09-23 实测：东财 clist 单页最多返回 100 条（pz=200 也被截成 100），按 100 翻页
SLEEP = 2.0  # v105: 0.9→2.0 慢速翻页 + 随机抖动（下方 sleep 处生效），压低请求速率避开风控
INC_BATCH = 100   # 增量模式每批代码数
INC_SLEEP = 0.35  # 增量模式批次间隔

# 反爬防护（2026-09-23 排查"全量只抓到 100 只"问题引入）：
# - 请求失败与真空页区分：连接被限流/RST 走长退避重试，不得当成"翻完了"提前 break
# - 全量结果过少时拒绝覆盖 data.json，保留旧快照（限流期反复重抓只会越抓越少）
PAGE_FAIL_RETRIES = 4      # v105: 8→4 单页失败重试轮数（快失败，恢复权交给 server 180s 补抓）
PAGE_FAIL_BACKOFF = 45     # 失败退避基础秒数（第 i 轮失败睡 45*i 秒）
PAGE_FAIL_MAX_SLEEP = 90   # v105: 120→90 单轮退避上限
MIN_FULL_STOCKS = 3000     # 全量结果低于此阈值视为限流截断，拒绝写入 data.json
RUN_BUDGET_S = 900         # v105: 2200→900 全局时间预算（秒）：耗尽即止，进入完整性判定；须 < server 侧 960s 超时


_START_TS = time.time()   # v105: 模块级起点，供 fetch_page_strict 页内预算检查

def fetch_page(fs, pn, tries=6):
    params = {
        "pn": pn, "pz": PAGE_SIZE, "po": 1, "np": 1,
        "fltt": 2, "invt": 2, "fid": "f3", "fs": fs, "fields": FIELDS,
    }
    for i in range(tries):
        # 轮换镜像域名，降低单连接限流概率
        url = BASE.format(DOMAINS[i % len(DOMAINS)])
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=12)
            return r.json()
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


def fetch_page_strict(fs, pn):
    """带失败判定的分页抓取：返回 (diff列表或None)。
    None = 重试耗尽仍失败（限流/断连）；[] = 服务端成功返回的空页。
    失败退避 45s 起步线性加长、上限 120s——东财 IP 风控窗口为分钟级，短重试只会持续被断连。
    """
    for i in range(PAGE_FAIL_RETRIES):
        d = fetch_page(fs, pn, tries=3)  # 每轮轮换全部 3 个域名，外层控制长退避
        if d is not None:
            data = (d or {}).get("data") or {}
            return data.get("diff") or []
        if time.time() - _START_TS > RUN_BUDGET_S:   # v105: 页内退避也受全局预算约束
            print(f"    [页 {pn}] 全局预算耗尽，放弃重试", flush=True)
            return None
        wait = min(PAGE_FAIL_BACKOFF * (i + 1), PAGE_FAIL_MAX_SLEEP)
        print(f"    [页 {pn}] 请求失败（疑似限流），退避 {wait}s 后重试（{i+1}/{PAGE_FAIL_RETRIES}）", flush=True)
        time.sleep(wait)
    return None


def fetch_ulist(secids, tries=5):
    """按 secid 列表批量查行情（增量模式用）"""
    params = {
        "secids": ",".join(secids),
        "fields": "f12,f2,f3,f4,f6,f20,f109,f160,f110,f24,f25",
        "fltt": 2, "invt": 2, "np": 1,
        "pn": 1, "pz": len(secids),
    }
    for i in range(tries):
        url = DOMAINS[i % len(DOMAINS)] + "/api/qt/ulist.np/get"
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=12)
            j = r.json()
            diff = (j or {}).get("data", {}).get("diff") or []
            if diff:
                return diff
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None



def fetch_industry_pe(tries=5, quiet=True):
    """行业平均市盈率（动态 PE，东财板块 clist f9 字段）→ {行业名: PE}
    负值表示行业整体亏损；东财单页最多返回 100 条，翻页抓全量（约 500 个板块）。
    失败返回空 dict（不影响主流程）。"""
    m = {}
    for i in range(tries):
        try:
            url = DOMAINS[i % len(DOMAINS)] + "/api/qt/clist/get"
            # 第一页拿 total
            params = {
                "pn": 1, "pz": 100, "po": 1, "np": 1,
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": "m:90+t:2", "fields": "f12,f14,f9",
            }
            r = requests.get(url, params=params, headers=HEADERS, timeout=12)
            j = r.json()
            data = (j or {}).get("data") or {}
            arr = data.get("diff") or []
            if not arr:
                continue
            for x in arr:
                if x.get("f14"):
                    m[str(x.get("f14"))] = norm(x.get("f9"))
            total = int(data.get("total") or 0)
            pages = (total + 99) // 100
            for pn in range(2, pages + 1):
                params["pn"] = pn
                try:
                    r = requests.get(url, params=params, headers=HEADERS, timeout=12)
                    arr2 = ((r.json() or {}).get("data") or {}).get("diff") or []
                except Exception:
                    arr2 = []
                if not arr2:
                    break
                for x in arr2:
                    if x.get("f14"):
                        m[str(x.get("f14"))] = norm(x.get("f9"))
                time.sleep(0.4)
            if not quiet:
                print("[行业PE] 板块数：%d（total=%d）" % (len(m), total), flush=True)
            return m
        except Exception:
            time.sleep(1.0 * (i + 1))
    return {}


# 全球指数清单（东财 100. 前缀 secid）：(secid, 中文名, 国家/地区中文, 地图区域英文名)
# 2026-08-06 实测：28 个全部可拉；沙特/土耳其/南非/阿联酋未命中（二期再补）
GLOBAL_INDICES = [
    # A股核心指数（真实 secid 前缀 1./0.；与境外 100. 不同）
    ("1.000001", "上证指数", "中国大陆", "China"),
    ("1.000300", "沪深300", "中国大陆", "China"),
    ("0.399006", "创业板指", "中国大陆", "China"),
    ("1.000688", "科创50", "中国大陆", "China"),
    ("100.NDX", "纳斯达克", "美国", "United States"),
    ("100.DJIA", "道琼斯", "美国", "United States"),
    ("100.SPX", "标普500", "美国", "United States"),
    ("100.HSI", "恒生指数", "中国香港", "China"),
    ("124.HSTECH", "恒生科技", "中国香港", "China"),
    ("100.TWII", "台湾加权", "中国台湾", "China"),
    ("100.N225", "日经225", "日本", "Japan"),
    ("100.KS11", "韩国KOSPI", "韩国", "Korea"),
    ("100.FTSE", "英国富时100", "英国", "United Kingdom"),
    ("100.GDAXI", "德国DAX30", "德国", "Germany"),
    ("100.FCHI", "法国CAC40", "法国", "France"),
    ("100.MIB", "意大利MIB", "意大利", "Italy"),
    ("100.IBEX", "西班牙IBEX35", "西班牙", "Spain"),
    ("100.ATX", "奥地利ATX", "奥地利", "Austria"),
    ("100.SSMI", "瑞士SMI", "瑞士", "Switzerland"),
    ("100.STI", "新加坡海峡时报", "新加坡", "Singapore"),
    ("100.SENSEX", "印度孟买SENSEX", "印度", "India"),
    ("100.AS51", "澳大利亚标普200", "澳大利亚", "Australia"),
    ("100.TSX", "加拿大S&P/TSX", "加拿大", "Canada"),
    ("100.BVSP", "巴西BOVESPA", "巴西", "Brazil"),
    ("100.MXX", "墨西哥BOLSA", "墨西哥", "Mexico"),
    ("100.RTS", "俄罗斯RTS", "俄罗斯", "Russia"),
    ("100.NZ50", "新西兰50", "新西兰", "New Zealand"),
    ("100.VNINDEX", "越南胡志明", "越南", "Vietnam"),
    ("100.KLSE", "马来西亚KLCI", "马来西亚", "Malaysia"),
    ("100.JKSE", "印尼雅加达综合", "印度尼西亚", "Indonesia"),
    ("100.PSI", "菲律宾马尼拉", "菲律宾", "Philippines"),
    ("100.SET", "泰国SET", "泰国", "Thailand"),
]
GLOBAL_FIELDS = "f2,f3,f4,f12,f14,f109,f160,f110,f24,f25,f124"

# ---------- 中东指数（tradingeconomics.com/stocks，无鉴权静态 HTML；直连可用） ----------
# (TE页指数名, 中文名, 国家中文名, 世界地图region名, ECharts地图国家名)
TE_MIDEAST = [
    ("TASI", "沙特TASI", "沙特", "Saudi Arabia", "Saudi Arabia"),
    ("ADX General", "阿布扎比ADX", "阿联酋", "United Arab Emirates", "United Arab Emirates"),
    ("DFM General", "迪拜DFM", "阿联酋", "United Arab Emirates", "United Arab Emirates"),
    ("BIST 100", "土耳其BIST100", "土耳其", "Turkey", "Turkey"),
    ("TA-125", "以色列TA125", "以色列", "Israel", "Israel"),
    ("QE", "卡塔尔QE", "卡塔尔", "Qatar", "Qatar"),
    ("MSM 30", "阿曼MSM30", "阿曼", "Oman", "Oman"),
    ("ASE", "约旦ASE", "约旦", "Jordan", "Jordan"),
    ("Kuwait All Share", "科威特全指", "科威特", "Kuwait", "Kuwait"),
    ("Estirad", "巴林全指", "巴林", "Bahrain", "Bahrain"),
    ("EGX 30", "埃及EGX30", "埃及", "Egypt", "Egypt"),
    ("CFG 25", "摩洛哥CFG25", "摩洛哥", "Morocco", "Morocco"),
]
TE_STOCKS_URL = "https://tradingeconomics.com/stocks"
TE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def _te_num(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", ""))
    except Exception:
        return None

def fetch_te_mideast():
    """tradingeconomics /stocks 主表格 → 中东指数行情 dict{指数名: {price,pct,pctYTD}}（显式直连）"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(3):
        try:
            req = urllib.request.Request(TE_STOCKS_URL, headers=TE_HEADERS)
            r = opener.open(req, timeout=35)
            body = r.read().decode("utf-8", "replace")
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
            out = {}
            for row in rows:
                if not re.search(r"flag flag-[a-z]{2}", row):
                    continue
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                texts = []
                for c in cells:
                    t = re.sub(r"<[^>]+>", "", c)
                    t = re.sub(r"\s+", " ", t).strip()
                    texts.append(t)
                # 行结构：[空, 指数名, 价格, 涨跌额, 日%, 1日%, 1周%, 1月%, YTD%, 日期, 空]
                if len(texts) >= 9 and texts[1]:
                    out[texts[1]] = {
                        "price": _te_num(texts[2]),
                        "pct": _te_num(texts[4]) if "%" in texts[4] else None,
                        "pctYTD": _te_num(texts[8]) if "%" in texts[8] else None,
                    }
            if out:
                return out
        except Exception as e:
            print(f"TE 抓取第 {attempt+1} 次失败: {e}")
            time.sleep(3 * (attempt + 1))
    return {}

# 期货主力连续合约（东财公开接口，无鉴权；secid = 市场前缀.品种代码m）
# 市场前缀：113 上期所 / 114 大商所 / 115 郑商所 / 142 上海能源 / 112 国际 / 225 广期所
FUTURES_FIELDS = "f2,f3,f4,f12,f14,f124"
FUTURES = [
    ("113.rbm", "螺纹钢", "黑色系"),
    ("114.im", "铁矿石", "黑色系"),
    ("114.jm", "焦炭", "黑色系"),
    ("113.cum", "沪铜", "有色金属"),
    ("109.LCPT", "伦敦铜", "有色金属"),
    ("113.alm", "沪铝", "有色金属"),
    ("113.aum", "沪金", "贵金属"),
    ("113.agm", "沪银", "贵金属"),
    ("101.GC00Y", "纽约金", "贵金属"),
    ("142.scm", "原油", "能源化工"),
    ("112.B00Y", "布伦特原油", "能源化工"),
    ("115.TAM", "PTA", "能源化工"),
    ("114.ppm", "聚丙烯", "能源化工"),
    ("114.cm", "玉米", "农产品"),
    ("114.mm", "豆粕", "农产品"),
    ("115.CFM", "棉花", "农产品"),
    ("115.SRM", "白糖", "农产品"),
    ("115.OIM", "菜油", "农产品"),
    ("114.lhm", "生猪", "农产品"),
    ("225.psm", "多晶硅", "新能源材料"),
    ("225.lcm", "碳酸锂", "新能源材料"),
    # 中金所金融期货（CFFEX，市场前缀 220；详情页走 qihuo 路径）
    ("220.IFM", "沪深300", "股指期货"),
    ("220.IHM", "上证50", "股指期货"),
    ("220.ICM", "中证500", "股指期货"),
    ("220.IMM", "中证1000", "股指期货"),
    ("220.TM", "十年国债", "国债期货"),
    ("220.TFM", "五年国债", "国债期货"),
    ("220.TSM", "二年国债", "国债期货"),
    ("220.TLM", "三十国债", "国债期货"),
    # 美债收益率（东财市场 171 = 债券 UB；与国内国债期货分开分组）
    ("171.US10Y", "美债10年", "美债"),
    ("171.US2Y", "美债2年", "美债"),
]


def fetch_global():
    """全球指数 → global.json（供世界地图视图使用；结构与 data.json 同一模式）"""
    quiet = "--quiet" in sys.argv
    secids = [g[0] for g in GLOBAL_INDICES]
    params = {
        "secids": ",".join(secids),
        "fields": GLOBAL_FIELDS,
        "fltt": 2, "invt": 2, "np": 1,
        "pn": 1, "pz": len(secids),
    }
    diff = None
    for i in range(5):
        url = DOMAINS[i % len(DOMAINS)] + "/api/qt/ulist.np/get"
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=12)
            j = r.json()
            diff = (j or {}).get("data", {}).get("diff") or []
            if diff:
                break
        except Exception:
            time.sleep(1.0 * (i + 1))
    if not diff:
        print("全球指数抓取失败")
        return
    by_code = {}
    for x in diff:
        by_code[str(x.get("f12"))] = x
    items = []
    for secid, name, cn, en in GLOBAL_INDICES:
        code = secid.split(".")[1]
        x = by_code.get(code)
        if x is None:
            items.append({"code": code, "name": name, "country": cn, "region": en, "secid": secid,
                          "price": None, "pct": None, "chg": None,
                          "pct5": None, "pct10": None, "pct20": None, "pct60": None, "pctYTD": None,
                          "time": None})
            continue
        items.append({
            "code": code, "name": name, "country": cn, "region": en, "secid": secid,
            "price": norm(x.get("f2")), "pct": norm(x.get("f3")), "chg": norm(x.get("f4")),
            "pct5": norm(x.get("f109")), "pct10": norm(x.get("f160")),
            "pct20": norm(x.get("f110")), "pct60": norm(x.get("f24")), "pctYTD": norm(x.get("f25")),
            "time": x.get("f124"),
        })
    # ---- 中东指数（TE 源）合并：te:1 标记、无 secid，前端东财实时循环自动跳过 ----
    try:
        te = fetch_te_mideast()
        for idx_name, cn, country_cn, region, _mapname in TE_MIDEAST:
            x = te.get(idx_name)
            items.append({
                "code": "TE:" + idx_name, "name": cn, "country": country_cn, "region": region,
                "secid": None, "te": 1,
                "price": x["price"] if x else None,
                "pct": x["pct"] if x else None,
                "chg": None,
                "pct5": None, "pct10": None, "pct20": None, "pct60": None,
                "pctYTD": x["pctYTD"] if x else None,
                "time": None,
            })
        print(f"中东指数（TE）合并：{sum(1 for it in items if it.get('te'))} 条")
    except Exception as e:
        print(f"中东指数合并失败（跳过）: {e}")
    payload = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items}
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "global.json")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, out)  # 原子写
    print(f"全球指数完成：{len(items)} 条 → global.json")


def fetch_futures():
    """期货主力连续 → futures.json（供期货视图使用；东财公开接口，无鉴权）"""
    quiet = "--quiet" in sys.argv
    secids = [f[0] for f in FUTURES]
    params = {
        "secids": ",".join(secids),
        "fields": FUTURES_FIELDS,
        "fltt": 2, "invt": 2, "np": 1,
        "pn": 1, "pz": len(secids),
    }
    diff = None
    for i in range(5):
        url = DOMAINS[i % len(DOMAINS)] + "/api/qt/ulist.np/get"
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=12)
            j = r.json()
            diff = (j or {}).get("data", {}).get("diff") or []
            if diff:
                break
        except Exception:
            time.sleep(1.0 * (i + 1))
    if not diff:
        print("期货抓取失败")
        return
    by_code = {}
    for x in diff:
        by_code[str(x.get("f12")).lower()] = x
    # 兜底：ulist 偶发缺失品种（如沪金 aum）→ 逐只单查补齐，避免 price=null
    for _sid, _nm, _grp in FUTURES:
        c = _sid.split(".")[1].lower()
        if c in by_code:
            continue
        try:
            r = requests.get(DOMAINS[0] + "/api/qt/stock/get",
                             params={"secid": _sid, "fields": FUTURES_FIELDS,
                                     "fltt": 2, "invt": 2},
                             headers=HEADERS, timeout=12)
            d = (r.json() or {}).get("data") or {}
            if d.get("f12"):
                by_code[c] = d
                print("期货补查成功：%s" % _nm, flush=True)
        except Exception:
            pass
    items = []
    for secid, name, group in FUTURES:
        code = secid.split(".")[1]
        market = secid.split(".")[0]
        # 详情页：101/112 国际期货用 globalfuture 路径；171 美债收益率无 PC 详情页 → wap 详情页（171.US10Y.html，已验证）；其余用 qihuo 路径
        if market == "171":
            url = "https://wap.eastmoney.com/quote/stock/" + secid + ".html"
        else:
            url = ("https://quote.eastmoney.com/globalfuture/" if market in ("101", "109", "112") else "https://quote.eastmoney.com/qihuo/") + code + ".html"
        x = by_code.get(code.lower())
        if x is None:
            items.append({"secid": secid, "code": code, "name": name, "group": group, "url": url,
                          "price": None, "pct": None, "chg": None, "time": None})
            continue
        items.append({
            "secid": secid, "code": code, "name": name, "group": group, "url": url,
            "price": norm(x.get("f2")), "pct": norm(x.get("f3")), "chg": norm(x.get("f4")),
            "time": x.get("f124"),
        })
    payload = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items}
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "futures.json")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, out)  # 原子写
    print(f"期货完成：{len(items)} 条 → futures.json")


def secid_of(code):
    """A股代码 → 东财 secid：沪市(6开头，含科创板688) → 1.代码；深市(0/3开头) → 0.代码"""
    if code.startswith("6"):
        return "1." + code
    return "0." + code


def norm(v):
    """'-' 或缺失 → None；数字 → float"""
    if v is None or v == "-" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def data_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")


def incremental():
    """增量刷新：读现有 data.json，按代码列表只更新价格/涨跌/成交额/市值字段"""
    quiet = "--quiet" in sys.argv
    path = data_path()
    if not os.path.exists(path):
        print("未找到 data.json，请先执行全量抓取：python fetch_data.py")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    # 代码 → 股票对象索引
    index = {}
    for ind in payload["industries"]:
        for st in ind["stocks"]:
            index[st["code"]] = st
    codes = list(index.keys())

    updated = 0
    for i in range(0, len(codes), INC_BATCH):
        batch_codes = codes[i:i + INC_BATCH]
        diff = fetch_ulist([secid_of(c) for c in batch_codes])
        got = 0
        if diff:
            for x in diff:
                code = x.get("f12")
                st = index.get(code)
                if st is None:
                    continue
                v = norm(x.get("f2"))
                if v is not None:
                    st["price"] = v
                v = norm(x.get("f3"))
                if v is not None:
                    st["pct"] = v
                v = norm(x.get("f4"))
                if v is not None:
                    st["chg"] = v
                v = norm(x.get("f6"))
                if v is not None:
                    st["amount"] = v
                v = norm(x.get("f20"))
                if v is not None:
                    st["mcap"] = v
                v = norm(x.get("f109"))
                if v is not None:
                    st["pct5"] = v
                v = norm(x.get("f160"))
                if v is not None:
                    st["pct10"] = v
                v = norm(x.get("f110"))
                if v is not None:
                    st["pct20"] = v
                v = norm(x.get("f24"))
                if v is not None:
                    st["pct60"] = v
                v = norm(x.get("f25"))
                if v is not None:
                    st["pctYTD"] = v
                got += 1
        updated += got
        if not quiet:
            print(f"[增量] {min(i + INC_BATCH, len(codes))}/{len(codes)}，本批 {got} 条")
        time.sleep(INC_SLEEP)

    # 市值可能变化，重算行业 total_mcap
    for ind in payload["industries"]:
        m = sum(s["mcap"] for s in ind["stocks"] if s["mcap"])
        ind["total_mcap"] = m
    payload["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    payload["mode"] = "incremental"

    # 写回前重新合并 q5500（防止抓取期间 fetch_spot 写入的新现货被覆盖），临界区加锁
    try:
        with datalock.DataLock():
            try:
                with open(path, encoding="utf-8") as f_old:
                    old_payload = json.load(f_old)
                if old_payload.get("q5500"):
                    payload["q5500"] = old_payload["q5500"]
                if old_payload.get("rare_earth"):
                    payload["rare_earth"] = old_payload["rare_earth"]
            except Exception:
                pass
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)  # 原子写：避免浏览器读到半截 JSON
    except datalock.DataLockTimeoutError as e:
        print(str(e))
        sys.exit(1)
    print(f"增量完成：更新 {updated}/{len(codes)} 只股票行情 → data.json")


# ========== v102 新浪备源：全市场快照（东财 clist 被风控时接管） ==========
SINA_HQ_BASE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData?page={p}&num=100&node=hs_a&sort=symbol&asc=1")
SINA_HEADERS = {"User-Agent": HEADERS["User-Agent"], "Referer": "https://finance.sina.com.cn"}
TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq"
PCT_BACKFILL_PER_ROUND = 500   # 每轮备源最多补的腾讯日K只数（防触发腾讯频控）


def _sina_get_page(page, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(SINA_HQ_BASE.format(p=page), headers=SINA_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("gbk", "replace")
            arr = json.loads(body)
            if isinstance(arr, list):
                return arr
            if isinstance(arr, dict) and arr.get("page") is not None:
                return arr.get("data") or []
            return []
        except Exception as e:
            wait = 20 * (i + 1)
            print(f"[新浪备源] 第 {page} 页失败（{str(e)[:50]}），退避 {wait}s 重试 {i+1}/{tries}", flush=True)
            time.sleep(wait)
    return None


def _sina_fetch_all(quiet):
    """新浪 hs_a 全市场分页。返回 [(code, dict)]，失败返回 None。
    字段：symbol(sh600000)/code/name/trade(现价)/pricechange(涨跌额)/changepercent(涨跌幅%)/
          volume/amount(万元)/mktcap(总市值万)/nmc(流通万)/turnoverratio/per/pb"""
    all_rows = []
    page = 1
    empty = 0
    while page <= 90:
        arr = _sina_get_page(page)
        if arr is None:
            return None
        if not arr:
            empty += 1
            if empty >= 2:
                break
            page += 1
            time.sleep(0.35)
            continue
        empty = 0
        all_rows.extend(arr)
        if not quiet and page % 10 == 0:
            print(f"[新浪备源] 已拉 {len(all_rows)} 只（第 {page} 页）", flush=True)
        page += 1
        time.sleep(0.35)
    if len(all_rows) < MIN_FULL_STOCKS:
        print(f"[新浪备源] 仅 {len(all_rows)} 只（< {MIN_FULL_STOCKS}），视为失败", flush=True)
        return None
    return all_rows


def _is_delisting(name, price):
    # 退市整理股过滤：名称带"退"，或便士股（<0.5 元）——实测未命中 code_map 的多为这类
    if not name:
        return False
    if "退" in name:
        return True
    try:
        return float(price) < 0.5
    except (TypeError, ValueError):
        return False


def fetch_tx_pct_batch(codes):
    """腾讯日K批量算 pct5/pct10/pct20/pct60/pctYTD → {code: {...}}。单只一请求，失败跳过。"""
    out = {}
    today = time.strftime("%Y-%m-%d")
    year_start_days = None
    for code in codes:
        sym = ("sh" if code.startswith("6") else "sz") + code
        try:
            # 首次探测年初至今所需根数（约 240 覆盖全年）；固定取 250 根足够
            url = TX_KLINE_URL.format(code=sym, n=250)
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.loads(r.read().decode("utf-8"))
            data = (j.get("data") or {}).get(sym) or {}
            days = data.get("qfqday") or data.get("day") or []
            if len(days) < 6:
                continue
            closes = [float(x[2]) for x in days]
            last = closes[-1]

            def _pct(n):
                return round((last / closes[-1 - n] - 1) * 100, 2) if len(closes) > n else None
            ytd = None
            for i, d in enumerate(days):
                if d[0] >= today[:4] + "-01-01":
                    base = closes[i - 1] if i > 0 else closes[0]
                    ytd = round((last / base - 1) * 100, 2)
                    break
            out[code] = {"pct5": _pct(5), "pct10": _pct(10), "pct20": _pct(20),
                         "pct60": _pct(60), "pctYTD": ytd}
        except Exception:
            continue
        time.sleep(0.3)
    return out


def fetch_all_sina(quiet):
    """新浪备源全量：分页行情 → 过滤 → 行业还原（code_map/indmap_sec）→ 两级 pct 补齐 → payload。
    成功返回 payload（未落盘），失败返回 None。"""
    t0 = time.time()
    rows = _sina_fetch_all(quiet)
    if rows is None:
        return None

    # 行业字典
    indmap_cfg = {}
    sec_dict = {}
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "indmap.json"), encoding="utf-8") as f:
            indmap_cfg = json.load(f) or {}
    except Exception as e:
        print("[新浪备源] indmap.json 加载失败: %s" % str(e)[:60], flush=True)
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "indmap_sec.json"), encoding="utf-8") as f:
            sec_dict = (json.load(f) or {}).get("map") or {}
    except Exception as e:
        print("[新浪备源] indmap_sec.json 加载失败: %s" % str(e)[:60], flush=True)
    l1_map = {str(k): v for k, v in (indmap_cfg.get("map") or {}).items()}
    code_l1 = {str(k): v for k, v in (indmap_cfg.get("code_map") or {}).items()}

    all_stocks = []
    unclassified = 0
    for x in rows:
        sym = str(x.get("symbol") or "")
        code = sym[2:] if len(sym) > 2 else str(x.get("code") or "")
        name = str(x.get("name") or "")
        try:
            price = float(x.get("trade") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0 or _is_delisting(name, price):
            continue

        def _wan_to_yuan(v):
            try:
                return float(v) * 10000.0
            except (TypeError, ValueError):
                return None
        info = sec_dict.get(code) or {}
        l1 = info.get("l1") or code_l1.get(code) or "综合"
        sec = info.get("sec")
        if not code_l1.get(code) and not info.get("l1"):
            unclassified += 1
        all_stocks.append({
            "code": code,
            "name": name,
            "price": price,
            "pct": norm(x.get("changepercent")),
            "chg": norm(x.get("pricechange")),
            "amount": _wan_to_yuan(x.get("amount")),
            "mcap": _wan_to_yuan(x.get("mktcap")),  # 新浪字段名为 mktcap（万元）→ 元
            "peTTM": norm(x.get("per")),
            "pct5": None, "pct10": None, "pct20": None, "pct60": None, "pctYTD": None,
            # 二级缺失时用一级名作二级显示（跟随正确的一级分组，避免全堆进"其他"）
            "industry": sec or l1,
            "_l1": l1,
        })
    if not quiet:
        print(f"[新浪备源] 行情 {len(all_stocks)} 只（剔除退市/异常 {len(rows)-len(all_stocks)}），"
              f"行业未分类 {unclassified} 只归'综合'", flush=True)

    # 两级 pct 补齐：优先告警相关+市值前 500
    alert_codes = []
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.json"), encoding="utf-8") as f:
            alerts = json.load(f) or {}
        alert_codes = [c for c in alerts.keys() if re.match(r"^[0-9]{6}$", str(c))]
    except Exception:
        pass
    by_mcap = sorted([s for s in all_stocks if s["mcap"]], key=lambda s: -s["mcap"])
    priority = [c for c in alert_codes if any(s["code"] == c for s in all_stocks)]
    priority += [s["code"] for s in by_mcap[:PCT_BACKFILL_PER_ROUND] if s["code"] not in priority]
    priority = priority[:PCT_BACKFILL_PER_ROUND + len(alert_codes)]
    if not quiet:
        print(f"[新浪备源] 腾讯日K回补区间涨幅：{len(priority)} 只（告警+市值前500）", flush=True)
    tx = fetch_tx_pct_batch(priority)
    for s in all_stocks:
        t = tx.get(s["code"])
        if t:
            s.update(t)
    filled = sum(1 for s in all_stocks if s["pct5"] is not None)
    if not quiet:
        print(f"[新浪备源] pct 已回补 {filled} 只，其余留空显示'-'（后续快照滚动补齐）", flush=True)

    # 分组（与东财主流程同构：二级行业 → 申万一级聚合）
    industries = {}
    for s in all_stocks:
        industries.setdefault(s["industry"], []).append(s)
    result = []
    for name, stocks in industries.items():
        total_mcap = sum(s["mcap"] for s in stocks if s["mcap"])
        result.append({"name": name, "total_mcap": total_mcap, "pe": None, "stocks": stocks})
    result.sort(key=lambda x: -x["total_mcap"])

    sec_bucket = {}
    for ind in result:
        votes = {}
        for s in ind["stocks"]:
            l1 = s.get("_l1") or "综合"
            votes[l1] = votes.get(l1, 0) + 1
        l1 = max(votes.items(), key=lambda kv: kv[1])[0] if votes else "综合"
        sec_bucket.setdefault(l1, []).append(ind)
    sectors = []
    for l1, children in sec_bucket.items():
        tm = sum(c["total_mcap"] for c in children)

        def wp(f, _st=[s for c in children for s in c["stocks"]]):
            num = den = 0.0
            for s in _st:
                v = s.get(f)
                mc = s.get("mcap")
                if v is not None and mc:
                    num += v * mc
                    den += mc
            return (num / den) if den else None
        sectors.append({
            "name": l1, "total_mcap": tm, "pe": None,
            "pct": wp("pct"), "pct5": wp("pct5"), "pct10": wp("pct10"),
            "pct20": wp("pct20"), "pct60": wp("pct60"), "pctYTD": wp("pctYTD"),
            "children": [c["name"] for c in sorted(children, key=lambda x: -x["total_mcap"])],
        })
    sectors.sort(key=lambda x: -x["total_mcap"])

    payload = {
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "full",
        "source": "sina_backup",
        "total_stocks": len(all_stocks),
        "total_industries": len(result),
        "industries": result,
        "sectors": sectors,
    }
    if not quiet:
        print(f"[新浪备源] 完成：{len(all_stocks)} 只 / {len(result)} 行业 / 耗时 {time.time()-t0:.0f}s", flush=True)
    return payload


def main():
    quiet = "--quiet" in sys.argv
    # v102 双源：东财主源连续失败 → 新浪备源全量接管（行业体系用 indmap/code_map/indmap_sec 还原）
    em_fail = 0
    for attempt in range(2):
        payload = _em_fetch_all(quiet)
        if payload is not None:
            _write_payload(payload, quiet)
            return
        em_fail += 1
        print("[双源] 东财主源第 %d/2 轮失败" % em_fail, flush=True)
        time.sleep(5)
    print("[双源] 东财主源连续 2 轮失败，切换新浪备源全量抓取…", flush=True)
    payload = fetch_all_sina(quiet)
    if payload is not None:
        _write_payload(payload, quiet)
        return
    print("[双源] 备源亦失败，data.json 保持旧快照不变。", flush=True)
    sys.exit(3)


def _write_payload(payload, quiet):
    """落盘 + 附带输出（global/futures 保留东财通道——它们未被风控）"""
    out = data_path()
    try:
        with datalock.DataLock():
            # 保留现货模块 q5500 节点（v33b：全量抓取不应抹掉现货数据）
            try:
                with open(out, encoding="utf-8") as f_old:
                    old_payload = json.load(f_old)
                if old_payload.get("q5500"):
                    payload["q5500"] = old_payload["q5500"]
                if old_payload.get("rare_earth"):
                    payload["rare_earth"] = old_payload["rare_earth"]
            except Exception:
                pass
            tmp = out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, out)
    except datalock.DataLockTimeoutError as e:
        print(str(e))
        sys.exit(1)
    print(f"完成：{payload.get('total_stocks')} 只股票，{payload.get('total_industries')} 个行业"
          f"（source={payload.get('source')}）→ data.json", flush=True)
    fetch_global()
    fetch_futures()


def _em_fetch_all(quiet):
    """东财主源（原 main 主体）。成功返回 payload dict（未落盘），失败返回 None。
    失败定义：任一市场分页重试耗尽 / 全量结果触发残缺保护。"""
    quiet = quiet
    # v90.1 限流预检：开抓前先轻探 3 轮（每轮轮换 3 域名，间隔 15s）。
    # 全失败 = IP 仍被限流，立即退出（退出码 3），不做注定失败的长退避——
    # 2026-09-23 17:17 教训：限流期硬重试 40 分钟，跑满 server 2400s 超时被强杀。
    probe_ok = False
    for i in range(3):
        d = fetch_page(MARKETS[0][1], 1, tries=3)
        if d and d.get("data"):
            probe_ok = True
            break
        print(f"[限流预检] 第 {i+1}/3 次预检失败，15s 后再试", flush=True)
        time.sleep(15)
    if not probe_ok:
        print("[限流预检] 预检 3 轮全部失败：东财仍在限流本机，主源判失败。", flush=True)
        return None  # v102：交由 main() 降级分支处理备源
    _start_ts = time.time()
    all_stocks = []
    for label, fs in MARKETS:
        if time.time() - _start_ts > RUN_BUDGET_S:
            print("[时间预算] 已耗尽，停止抓取后续市场，转入完整性判定", flush=True)
            break
        d = None
        for i in range(5):  # 首页失败=该市场全军覆没，长退避硬重试
            d = fetch_page(fs, 1, tries=2)
            if d and d.get("data"):
                break
            wait = min(60 * (i + 1), 90)   # v105: 上限 180→90，首页最多 5×90s，不再吃掉大半预算
            print(f"[{label}] 首页请求失败（疑似限流），退避 {wait}s 后重试（{i+1}/5）", flush=True)
            time.sleep(wait)
        if not d or not d.get("data"):
            print(f"[{label}] 首页重试耗尽仍失败，跳过该市场", flush=True)
            continue
        total = d["data"]["total"]
        # 服务端可能将每页条数截断，动态翻页直到取满或连续空页
        pn = 1
        got = 0
        empty = 0
        while got < total and pn <= 400 and time.time() - _start_ts < RUN_BUDGET_S:
            # v90：diff is None = 请求失败（限流），不算空页；空页必须来自成功请求
            diff = fetch_page_strict(fs, pn)
            if diff is None:
                print(f"[{label}] 第 {pn} 页重试耗尽仍失败，该市场止步 {got}/{total}", flush=True)
                return None  # v102：主源分页失败即整体失败，交给降级分支
            if not diff:
                empty += 1
                if empty >= 2:
                    break
            else:
                empty = 0
                for x in diff:
                    all_stocks.append({
                        "code": x.get("f12"),
                        "name": x.get("f14"),
                        "price": norm(x.get("f2")),
                        "pct": norm(x.get("f3")),
                        "chg": norm(x.get("f4")),
                        "amount": norm(x.get("f6")),
                        "mcap": norm(x.get("f20")),
                        "peTTM": norm(x.get("f115")),
                        "rev": norm(x.get("f40")),      # 营业总收入（累计，元）——f40 语义实测校准
                        "revYoy": norm(x.get("f41")),   # 营收同比 %
                        "np": norm(x.get("f45")),       # 归母净利润（累计，元）
                        "npYoy": norm(x.get("f46")),    # 归母同比 %
                        "pct5": norm(x.get("f109")),
                        "pct10": norm(x.get("f160")),
                        "pct20": norm(x.get("f110")),
                        "pct60": norm(x.get("f24")),
                        "pctYTD": norm(x.get("f25")),
                        "industry": x.get("f100") or "其他",
                    })
                got += len(diff)
            if not quiet:
                print(f"[{label}] {pn} 页，累计 {got}/{total}")
            pn += 1
            time.sleep(SLEEP + random.uniform(0, 1.2))   # v105: 2.0~3.2s 抖动，打散请求特征
        if not quiet:
            print(f"[{label}] 完成，共 {got} 只")
        time.sleep(1.0)

    # 行业分组（v88：申万一级 31 个，indmap.json 静态映射 code→一级；个股 industry 字段保留二级用于悬浮）
    indmap_cfg = {}
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "indmap.json"), encoding="utf-8") as f_map:
            indmap_cfg = json.load(f_map) or {}
    except Exception as e:
        print("[一级分组] indmap.json 加载失败，退回二级行业: %s" % str(e)[:60], flush=True)
    l1_map = {str(k): v for k, v in (indmap_cfg.get("map") or {}).items()}
    code_l1 = {str(k): v for k, v in (indmap_cfg.get("code_map") or {}).items()}
    for s in all_stocks:
        s["_l1"] = code_l1.get(str(s["code"])) or l1_map.get(s["industry"]) or "综合"
    industries = {}
    for s in all_stocks:
        industries.setdefault(s["industry"], []).append(s)
    # 行业平均市盈率（动态 PE；板块名与个股 f100 行业名按名称匹配，未匹配留空）
    pe_map = fetch_industry_pe(quiet=quiet)
    result = []
    unmatched = 0
    for name, stocks in industries.items():
        total_mcap = sum(s["mcap"] for s in stocks if s["mcap"])
        pe = pe_map.get(name)
        if pe is None and indmap_cfg.get("map"):
            # 一级行业 PE：按成分股二级 pe_map 市值加权推导
            num = den = 0.0
            sec_mcap = {}
            for s in stocks:
                if s["mcap"] and pe_map.get(s["industry"]) is not None:
                    sec_mcap[s["industry"]] = sec_mcap.get(s["industry"], 0) + s["mcap"]
            for sec, mc in sec_mcap.items():
                p2 = pe_map.get(sec)
                if p2:
                    num += p2 * mc
                    den += mc
            pe = (num / den) if den else None
        result.append({"name": name, "total_mcap": total_mcap, "pe": pe, "stocks": stocks})
        if pe is None:
            unmatched += 1
    if unmatched and not quiet:
        print("[行业PE] %d 个行业未匹配到板块市盈率（留空）" % unmatched, flush=True)
    result.sort(key=lambda x: -x["total_mcap"])

    # v89.1：申万一级聚合层（sectors）——industries 保持二级；sectors 按 _l1 多数归属把二级分组
    sec_bucket = {}
    for ind in result:
        votes = {}
        for s in ind["stocks"]:
            l1 = s.get("_l1") or "综合"
            votes[l1] = votes.get(l1, 0) + 1
        l1 = max(votes.items(), key=lambda kv: kv[1])[0] if votes else "综合"
        sec_bucket.setdefault(l1, []).append(ind)
    sectors = []
    for l1, children in sec_bucket.items():
        tm = sum(c["total_mcap"] for c in children)
        all_st = [s for c in children for s in c["stocks"]]
        def wp(f, _st=all_st):
            num = den = 0.0
            for s in _st:
                v = s.get(f)
                mc = s.get("mcap")
                if v is not None and mc:
                    num += v * mc
                    den += mc
            return (num / den) if den else None
        num = den = 0.0
        for c in children:
            if c.get("pe"):
                num += c["pe"] * c["total_mcap"]
                den += c["total_mcap"]
        sectors.append({
            "name": l1, "total_mcap": tm,
            "pe": (num / den) if den else None,
            "pct": wp("pct"), "pct5": wp("pct5"), "pct10": wp("pct10"),
            "pct20": wp("pct20"), "pct60": wp("pct60"), "pctYTD": wp("pctYTD"),
            # children 只存行业名（行业明细在 industries 已有一份，内嵌会把全部股票写两遍，
            # data.json 体积翻倍；前端按名从 payload.industries 反查建树）
            "children": [c["name"] for c in sorted(children, key=lambda x: -x["total_mcap"])],
        })
    sectors.sort(key=lambda x: -x["total_mcap"])

    payload = {
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "full",
        "source": "eastmoney",
        "total_stocks": len(all_stocks),
        "total_industries": len(result),
        "industries": result,
        "sectors": sectors,
    }
    # 残缺保护（2026-09-23）：静态阈值 + 动态对比双保险
    # 全量结果明显偏少（<3000 或 比旧快照缩水>10%）= 限流截断，拒绝覆盖，保留旧快照
    old_n = None
    try:
        with open(data_path(), encoding="utf-8") as f_old:
            old_n = json.load(f_old).get("total_stocks")
    except Exception:
        pass
    shrink = old_n is not None and len(all_stocks) < old_n * 0.9
    if len(all_stocks) < MIN_FULL_STOCKS or shrink:
        reason = (f"低于阈值 {MIN_FULL_STOCKS}" if len(all_stocks) < MIN_FULL_STOCKS
                  else f"比旧快照 {old_n} 只缩水超 10%")
        print(f"[残缺保护] 东财全量仅抓到 {len(all_stocks)} 只（{reason}），疑似限流截断。", flush=True)
        return None  # v102：交由 main() 降级分支处理备源
    return payload  # v102：落盘统一由 _write_payload 完成


if __name__ == "__main__":
    if "--incremental" in sys.argv:
        incremental()
    else:
        main()
