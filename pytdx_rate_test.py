# -*- coding: utf-8 -*-
"""pytdx 频率限制边界测试：间隔、冷却、重连恢复"""
import json
import time

from pytdx.hq import TdxHq_API

data = json.load(open(r"C:\Users\you\.openclaw-autoclaw\workspace\a-share-heatmap\data.json", encoding="utf-8"))
codes = []
for ind in data["industries"]:
    for s in ind["stocks"]:
        c = s["code"]
        codes.append((1 if c.startswith(("6", "9")) else 0, c))

SERVER = ("上海电信主站Z1", "180.153.18.170", 7709)


def connect():
    api = TdxHq_API()
    if not api.connect(SERVER[1], SERVER[2], time_out=5):
        return None
    return api


def probe(api, n=10):
    q = api.get_security_quotes(codes[:n])
    return len(q) if q else 0


print("=== 0. 冷却 10s 后重连 ===")
time.sleep(10)
api = connect()
print("重连:", "OK" if api else "FAIL")
if api:
    print("单次请求:", probe(api))

print("\n=== 1. 间隔 0.5s 连续 60 次 ===")
t0 = time.time()
fails = []
for i in range(60):
    r = probe(api, 80)
    if r == 0:
        fails.append(i)
        break
    time.sleep(0.5)
print(f"在第 {fails[0]+1 if fails else 61} 次失败，此前 {i if fails else 60} 次成功，耗时 {time.time()-t0:.1f}s")

print("\n=== 2. 失败后冷却 15s ===")
time.sleep(15)
print("重试单次:", probe(api))

print("\n=== 3. 间隔 1s 连续 40 次 ===")
t0 = time.time()
fails = []
for i in range(40):
    r = probe(api, 80)
    if r == 0:
        fails.append(i)
        break
    time.sleep(1)
print(f"在第 {fails[0]+1 if fails else 41} 次失败，此前 {i if fails else 40} 次成功，耗时 {time.time()-t0:.1f}s")

api.disconnect()
