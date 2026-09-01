# -*- coding: utf-8 -*-
"""pytdx 服务器扫描：找出支持 行情/列表 命令的服务器，并测速"""
import time

from pytdx.hq import TdxHq_API
from pytdx.config.hosts import hq_hosts

QUOTE_OK = []
LIST_OK = []
FAIL = []

print(f"扫描 {len(hq_hosts)} 个内置服务器（每个超时 3s，请耐心）…")
for i, h in enumerate(hq_hosts):
    name, ip, port = h[0], h[1], int(h[2])
    api = TdxHq_API()
    try:
        if not api.connect(ip, port, time_out=3):
            FAIL.append((name, ip, port, "connect-false"))
            api.disconnect()
            continue
        q = api.get_security_quotes([(1, "600000")])
        if q and len(q) > 0 and q[0].get("price"):
            QUOTE_OK.append((name, ip, port))
            # 顺便测列表
            lst = api.get_security_list(1, 0)
            if lst:
                LIST_OK.append((name, ip, port))
        else:
            FAIL.append((name, ip, port, "quotes-empty"))
        api.disconnect()
    except Exception as e:
        FAIL.append((name, ip, port, type(e).__name__))
        try:
            api.disconnect()
        except Exception:
            pass

print("\n=== 支持行情 quotes 的服务器 ===")
for x in QUOTE_OK:
    print(" ", x)
print(f"共 {len(QUOTE_OK)} 个")
print("\n=== 同时支持列表 list 的服务器 ===")
for x in LIST_OK:
    print(" ", x)
print(f"共 {len(LIST_OK)} 个")
print(f"\n失败 {len(FAIL)} 个（示例: {FAIL[:3]}）")

# 对第一个可用服务器测性能
if QUOTE_OK:
    name, ip, port = QUOTE_OK[0]
    api = TdxHq_API()
    api.connect(ip, port, time_out=5)
    print(f"\n=== 性能测试 @ {name} {ip}:{port} ===")
    # quotes 批量上限
    for n in [80, 100, 200]:
        codes = [(1, f"600{str(i).zfill(3)}") for i in range(1, n)]
        t0 = time.time()
        q = api.get_security_quotes(codes)
        dt = time.time() - t0
        print(f"quotes 请求 {n-1} 只: 返回 {len(q) if q else 0} 条, 耗时 {dt:.2f}s")
        if q and len(q) == n - 1:
            print("  -> 上限至少", n - 1)
            break
    # finance_info 速度（20 只）
    t0 = time.time()
    for i in range(20):
        api.get_finance_info(1, f"600{str(i+1).zfill(3)}")
    dt = time.time() - t0
    print(f"get_finance_info 20 只耗时 {dt:.2f}s -> 全市场 5400 只预计 {dt/20*5400:.0f}s")
    # 列表全量速度
    t0 = time.time()
    cnt = 0
    start = 0
    while True:
        part = api.get_security_list(1, start)
        if not part:
            break
        cnt += len(part)
        start += len(part)
        if len(part) < 1000:
            break
    print(f"沪市列表全量 {cnt} 条, 耗时 {time.time()-t0:.2f}s")
    api.disconnect()
