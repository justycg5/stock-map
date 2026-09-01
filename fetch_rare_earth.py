# -*- coding: utf-8 -*-
# v83 稀土现货抓取：中国稀土网 CRE（cre.net）氧化镨钕/氧化镝 日更报价（公开 HTML 页面，无鉴权）
# 用法: python fetch_rare_earth.py   -> 成功退出码 0；失败退出码 1（stderr 说明原因，不破坏 data.json）
import json, os, re, sys, time, urllib.request

# AutoClaw 内置 python 为 embeddable 发行版（python313._pth 固定 sys.path），脚本目录不在 path 中，手动加入
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)
import datalock

PAGE_URL = "https://www.cre.net/market-condition/daily-price/term"
BASE = _BASE_DIR
DATA_JSON = os.path.join(BASE, "data.json")
TARGETS = ("氧化镨钕", "氧化镝")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _fetch_html():
    req = urllib.request.Request(PAGE_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def _extract_date(html):
    # 页面中为 "数据日期:<!-- -->2026年08月24日"（shadcn 文本节点间有注释分隔）
    m = re.search(r"数据日期[:：]\s*(?:<!--[^>]*-->|\s)*?(\d{4})年(\d{2})月(\d{2})日", html)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    return ""


def _extract_row(html, name):
    """按名称锚点定位 <tr>，提取 td 单元格文本。返回 dict 或 None。"""
    i = html.find(name)
    if i < 0:
        return None
    row_start = html.rfind("<tr", 0, i)
    row_end = html.find("</tr>", i)
    if row_start < 0 or row_end < 0:
        return None
    row = html[row_start:row_end + 5]
    cells = []
    for m in re.finditer(r"<td[^>]*>([\s\S]*?)</td>", row):
        txt = re.sub(r"<[^>]+>", " ", m.group(1))
        txt = re.sub(r"\s+", " ", txt).strip()
        cells.append(txt)
    if len(cells) < 7:
        return None
    def _f(s):
        try:
            return float(str(s).replace(",", "").strip())
        except Exception:
            return None
    return {
        "name": cells[0],
        "spec": cells[1],
        "low": _f(cells[2]),
        "high": _f(cells[3]),
        "avg": _f(cells[4]),
        "change": _f(cells[5]),
        "unit": cells[6] if len(cells) > 6 else "元/千克",
    }


def main():
    try:
        html = _fetch_html()
    except Exception as e:
        sys.stderr.write("网络请求失败: %s\n" % e)
        sys.exit(1)

    date = _extract_date(html)
    items = {}
    missing = []
    for name in TARGETS:
        row = _extract_row(html, name)
        if row:
            items["pr_nd" if name == "氧化镨钕" else "dy"] = row
        else:
            missing.append(name)
    if not items:
        sys.stderr.write("未在页面中找到目标品种（氧化镨钕/氧化镝），页面结构可能已变化: %s\n" % PAGE_URL)
        sys.exit(1)

    q = {
        "date": date,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "中国稀土网 (www.cre.net)",
        "note": ("页面缺少品种: " + "、".join(missing)) if missing else "",
    }
    q.update(items)

    # 合并写入 data.json（保留其他字段；临界区加锁防并发丢失更新）
    try:
        with datalock.DataLock():
            try:
                with open(DATA_JSON, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                payload = {}
            payload["rare_earth"] = q
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
