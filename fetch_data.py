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
FIELDS = "f12,f14,f2,f3,f4,f6,f20,f100,f109,f160,f110,f24,f25"
MARKETS = [
    ("沪主板", "m:1+t:2"),
    ("深主板", "m:0+t:6"),
    ("创业板", "m:0+t:80"),
    ("科创板", "m:1+t:23"),
]
PAGE_SIZE = 200
SLEEP = 0.9  # 分页间隔，避免触发风控
INC_BATCH = 100   # 增量模式每批代码数
INC_SLEEP = 0.35  # 增量模式批次间隔


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


def main():
    quiet = "--quiet" in sys.argv
    all_stocks = []
    for label, fs in MARKETS:
        d = fetch_page(fs, 1)
        if not d or not d.get("data"):
            print(f"[{label}] 获取失败，跳过")
            continue
        total = d["data"]["total"]
        # 服务端可能将每页条数截断，动态翻页直到取满或连续空页
        pn = 1
        got = 0
        empty = 0
        while got < total and pn <= 200:
            d = fetch_page(fs, pn)
            diff = (d or {}).get("data", {}).get("diff") or []
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
            time.sleep(SLEEP)
        if not quiet:
            print(f"[{label}] 完成，共 {got} 只")
        time.sleep(1.0)

    # 行业分组（按总市值降序）
    industries = {}
    for s in all_stocks:
        industries.setdefault(s["industry"], []).append(s)
    # 行业平均市盈率（动态 PE；板块名与个股 f100 行业名按名称匹配，未匹配留空）
    pe_map = fetch_industry_pe(quiet=quiet)
    result = []
    unmatched = 0
    for name, stocks in industries.items():
        total_mcap = sum(s["mcap"] for s in stocks if s["mcap"])
        result.append({"name": name, "total_mcap": total_mcap, "pe": pe_map.get(name), "stocks": stocks})
        if name not in pe_map:
            unmatched += 1
    if unmatched and not quiet:
        print("[行业PE] %d 个行业未匹配到板块市盈率（留空）" % unmatched, flush=True)
    result.sort(key=lambda x: -x["total_mcap"])

    payload = {
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "full",
        "total_stocks": len(all_stocks),
        "total_industries": len(result),
        "industries": result,
    }
    out = data_path()
    # 保留现货模块 q5500 节点（v33b：全量抓取不应抹掉现货数据），临界区加锁
    try:
        with datalock.DataLock():
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
            os.replace(tmp, out)  # 原子写
    except datalock.DataLockTimeoutError as e:
        print(str(e))
        sys.exit(1)
    print(f"完成：{len(all_stocks)} 只股票，{len(result)} 个行业 → data.json")
    fetch_global()
    fetch_futures()


if __name__ == "__main__":
    if "--incremental" in sys.argv:
        incremental()
    else:
        main()
