# -*- coding: utf-8 -*-
"""pytdx 可行性实测：连接、列表、批量行情、板块文件、财务字段"""
import json
import time

from pytdx.hq import TdxHq_API
from pytdx.config.hosts import hq_hosts

print("=== 1. 服务器连通性（内置列表前 30 个） ===")
api = TdxHq_API()
connected = None
for h in hq_hosts[:30]:
    name, ip, port = h[0], h[1], int(h[2])
    try:
        if api.connect(ip, port, time_out=4):
            connected = (name, ip, port)
            print(f"[OK] 连接成功: {name} {ip}:{port}")
            break
        else:
            print(f"[X] 拒绝: {name} {ip}:{port}")
    except Exception as e:
        print(f"[X] 超时/错误: {name} {ip}:{port} ({type(e).__name__})")
if not connected:
    print("所有内置服务器均不可用")
    raise SystemExit(1)

print("\n=== 1.5 基本命令可用性 ===")
try:
    q = api.get_security_quotes([(1, "600000")])
    print("get_security_quotes:", "OK" if q else "返回空")
except Exception as e:
    print("get_security_quotes 失败:", type(e).__name__, e)

try:
    cnt = api.get_security_count(1)
    print("get_security_count(沪):", cnt)
except Exception as e:
    print("get_security_count 失败:", type(e).__name__, e)

print("\n=== 2. 股票列表 get_security_list（沪市 0 起 5 条） ===")
t0 = time.time()
lst = api.get_security_list(1, 0)
if lst:
    print(f"耗时 {time.time()-t0:.2f}s，返回 {len(lst)} 条")
    print("字段:", list(lst[0].keys()))
    print("样例:", lst[0])
else:
    print("返回 None/空")

print("\n=== 3. 全市场列表完整性（沪市分页计数） ===")
t0 = time.time()
count = 0
start = 0
while True:
    part = api.get_security_list(1, start)
    if not part:
        break
    count += len(part)
    start += len(part)
    if len(part) < 1000:
        break
print(f"沪市共 {count} 只，耗时 {time.time()-t0:.2f}s")

print("\n=== 4. 批量行情 get_security_quotes ===")
q = api.get_security_quotes([(1, "600000"), (0, "000001"), (1, "600519"), (0, "300750")])
if q:
    print("字段:", list(q[0].keys()))
    for row in q:
        print(f"  {row['code']} {row.get('name','')} 现价 {row['price']} 昨收 {row['last_close']} "
              f"涨跌 {(row['price']-row['last_close'])/row['last_close']*100:.2f}% "
              f"成交额 {row['amount']}")

print("\n=== 5. 批量上限测试（一次请求 200 只） ===")
codes = [(1, f"600{str(i).zfill(3)}") for i in range(1, 200)]
t0 = time.time()
q = api.get_security_quotes(codes)
print(f"请求 199 只，返回 {len(q) if q else 0} 条，耗时 {time.time()-t0:.2f}s")

print("\n=== 6. 板块文件 get_and_parse_block_info ===")
for bf in ["block_zs.dat", "block_gn.dat", "block_fg.dat"]:
    try:
        blk = api.get_and_parse_block_info(bf)
        print(f"{bf}: {'成功' if blk else '空'}，返回类型 {type(blk).__name__}，数量 {len(blk) if blk else 0}")
        if blk:
            print("  样例:", json.dumps(blk[0], ensure_ascii=False)[:150])
    except Exception as e:
        print(f"{bf}: 失败 {type(e).__name__}: {e}")

print("\n=== 7. 财务/股本字段 get_finance_info ===")
try:
    fin = api.get_finance_info(1, "600000")
    if fin:
        print("字段:", list(fin.keys()))
        # 找股本相关
        for k, v in fin.items():
            if any(x in k for x in ["guben", "zgb", "ltgb", "cap"]):
                print(f"  {k} = {v}")
except Exception as e:
    print(f"失败 {type(e).__name__}: {e}")

api.disconnect()
