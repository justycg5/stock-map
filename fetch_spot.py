# -*- coding: utf-8 -*-
# v31 现货抓取：Mysteel 秦皇岛港Q5500 动力煤指数（签名接口，凭证源自其前端 JS）
# 用法: python fetch_spot.py   -> 成功退出码 0；失败退出码 1（stderr 说明原因，不破坏 data.json）
import hashlib, json, os, sys, time, urllib.parse

# AutoClaw 内置 python 为 embeddable 发行版（python313._pth 固定 sys.path），脚本目录不在 path 中，手动加入
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)
import datalock
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


APP_KEY = "47EE3F12CF0C443F8FD51EFDA73AC815"
APP_SECRET = "3BA6477330684B19AA6AF4485497B5F2"
PATH = "/zs/newxpic/getReport.ms"
URL = "https://index.mysteel.com" + PATH
BASE = _BASE_DIR
DATA_JSON = os.path.join(BASE, "data.json")

def get_sign(ts):
    raw = "path" + PATH + "timestamp" + str(ts) + "version1.0.0" + APP_SECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

def main():
    ts = int(time.time() * 1000)
    params = {
        "typeName": "秦皇岛港Q5500", "tabName": "DLM_QINGGANG_MD",
        "dateType": "day", "startTime": "", "endTime": "", "returnType": "",
        "callback": "json", "v": str(ts),
    }
    headers = {
        "version": "1.0.0", "appKey": APP_KEY, "timestamp": str(ts),
        "sign": get_sign(ts),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Referer": "https://index.mysteel.com/xpic/detail.html?tabName=dlm",
        "Origin": "https://index.mysteel.com",
    }
    try:
        r = requests.get(URL, params=params, headers=headers, timeout=20)
    except Exception as e:
        sys.stderr.write("网络请求失败: %s\n" % e); sys.exit(1)
    try:
        j = r.json()
    except Exception:
        sys.stderr.write("响应解析失败: %s\n" % r.text[:200]); sys.exit(1)
    if j.get("code") not in (None, 200) or not j.get("data"):
        sys.stderr.write("接口返回异常: %s\n" % json.dumps(j, ensure_ascii=False)[:300]); sys.exit(1)
    d = j["data"][0]
    q = {
        "date": d.get("date", ""),
        "value": d.get("value", ""),
        "lastdayValue": d.get("lastdayValue", ""),
        "lastDayScale": d.get("lastDayScale", ""),
        "lastWeekValue": d.get("lastWeekValue", ""),
        "lastWeekScale": d.get("lastWeekScale", ""),
        "lastMonthValue": d.get("lastMonthValue", ""),
        "lastMonthScale": d.get("lastMonthScale", ""),
        "lastYearValue": d.get("lastYearValue", ""),
        "lastYearScale": d.get("lastYearScale", ""),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "Mysteel 秦皇岛港Q5500动力煤指数 (index.mysteel.com)",
    }
    # 合并写入 data.json（保留 A 股等其他字段；临界区加锁防并发丢失更新）
    try:
        with datalock.DataLock():
            try:
                with open(DATA_JSON, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                payload = {}
            payload["q5500"] = q
            tmp = DATA_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, DATA_JSON)  # 原子写：避免浏览器读到半截 JSON
    except datalock.DataLockTimeoutError as e:
        sys.stderr.write("%s\n" % e)
        sys.exit(1)
    print(json.dumps(q, ensure_ascii=False))

if __name__ == "__main__":
    main()
