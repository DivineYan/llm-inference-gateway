"""多实例全局一致性 —— 真·两进程演示（M1 §9 最后一条）。

启动两个独立的 uvicorn 进程（端口 8001 / 8002），共享同一 Redis 与配置。
对同一调用方交替发请求，观察限流额度在两进程间"合并计算"。

运行（在 agent_project 环境下，项目根目录）：
    python scripts/multi_instance_demo.py

成功标志：用 burst=2 的配置，两进程合计第 3 个请求即被 429 rate_limited，
证明限流状态在 Redis 全局共享，而非各进程内存。
"""
import os
import subprocess
import sys
import time

import httpx
import redis as redis_sync

CONFIG = "tests/configs/small_limits.yaml"  # burst=2
REDIS_URL = "redis://127.0.0.1:6379/14"
PORTS = [8001, 8002]
AUTH = {"Authorization": "Bearer m"}  # svc:test, burst=2
BODY = {"model": "gpt", "input": "ping"}


def wait_ready(client: httpx.Client, port: int, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"实例 {port} 未就绪")


def main():
    redis_sync.from_url(REDIS_URL).flushdb()  # 清空，确保从满桶开始

    env = {**os.environ, "REDIS_URL": REDIS_URL, "CONFIG_PATH": CONFIG}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(p), "--log-level", "warning"],
            env=env,
        )
        for p in PORTS
    ]

    try:
        with httpx.Client() as c:
            for p in PORTS:
                wait_ready(c, p)

            print(f"两实例就绪：{PORTS}，配置 burst=2，交替发 4 个请求：\n")
            results = []
            for i in range(4):
                port = PORTS[i % 2]
                r = c.post(f"http://127.0.0.1:{port}/v1/infer", json=BODY, headers=AUTH)
                reason = r.json().get("reason", "ok")
                print(f"  #{i + 1} → 实例 {port}: HTTP {r.status_code} ({reason})")
                results.append(r.status_code)

            ok = results[:2] == [200, 200] and 429 in results[2:]
            print("\n结果:", "[OK] 限流额度在两进程间全局合并" if ok else "[FAIL] 未观察到全局合并")
            sys.exit(0 if ok else 1)
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait()


if __name__ == "__main__":
    main()
