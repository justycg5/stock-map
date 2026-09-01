# -*- coding: utf-8 -*-
"""用真实代码测 pytdx 批量行情上限与全市场分批耗时"""
import json
import time

from pytdx.hq import TdxHq_API

# 读取东财抓的代码列表（市场推断：6/9 开头=沪 1，其余=深 0）
data = json.load(open(r"C:\Users\you\.openclaw-autoclaw\workspace\a-share-heatmap\data.json", encoding="utf-8"))
codes = []
for ind in data["industries"]:
    for s in ind["stocks"]:
        c = s["code"]
        market = 1 if c.startswith(("6", "9")) else 0
        codes.append((market, c))
print(f"真实代码总数: {len(codes)}")

SERVER = ("上海电信主站Z1", "180.153.18.170", 7709)
api = TdxHq_API()
if not api.connect(SERVER[1], SERVER[2], time_out=5):
    print("连接失败"); raise SystemExit(1)
print(f"已连接 {SERVER[0]} {SERVER[1]}:{SERVER[2]}")

print("\n=== 批量上限测试 ===")
for n in [10, 80, 100, 200, 400, 800]:
    q = api.get_security_quotes(codes[:n])
    got = len(q) if q else 0
    print(f"请求 {n} 只 -> 返回 {got} 条")
    if got == n:
        print(f"  -> 上限至少 {n}")

print("\n=== 全市场分批拉取（每批 80） ===")
t0 = time.time()
allq = []
batch = 80
ok = 0
for i in range(0, len(codes), batch):
    q = api.get_security_quotes(codes[i:i + batch])
    if q:
        allq.extend(q)
        ok += len(q)
    time.sleep(0.02)
dt = time.time() - t0
print(f"分批 {len(codes)//batch + 1} 次请求，成功 {ok}/{len(codes)}，总耗时 {dt:.1f}s")
if allq:
    print("样例字段:", list(allq[0].keys()))
    r = allq[0]
    print(f"样例: {r['code']} 现价 {r['price']} 昨收 {r['last_close']} "
          f"涨跌 {(r['price']-r['last_close'])/r['last_close']*100:.2f}% 成交额 {r['amount']}")

print("\n=== 连续请求频率限制测试（100 次，间隔 0ms） ===")
t0 = time.time()
ok = 0
for i in range(100):
    q = api.get_security_quotes(codes[:10])
    if q:
        ok += 1
dt = time.time() - t0
print(f"100 次请求成功 {ok}/100，耗时 {dt:.1f}s")
# 再测 100 次看是否被踢
t0 = time.time()
ok = 0
for i in range(100):
    q = api.get_security_quotes(codes[:10])
    if q:
        ok += 1
dt = time.time() - t0
print(f"第二轮 100 次请求成功 {ok}/100，耗时 {dt:.1f}s")

api.disconnect()
