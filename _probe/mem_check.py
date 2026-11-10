# 内存老化：AUTOTEST 模式跑完整程序 90 秒，psutil 采样 RSS。
# 红线（方案 §5.1）：启动稳态 ≤100MB。
# 跑法：py -3.10 -X utf8 _probe/mem_check.py
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
shutil.rmtree(ROOT / "src" / "__pycache__", ignore_errors=True)

DURATION = 90


def main():
    env = dict(os.environ)
    env["FLIPCLOCK_AUTOTEST"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["FLIPCLOCK_DURATION"] = str(DURATION)
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "src" / "main.py")],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    ps = psutil.Process(proc.pid)
    samples = []
    t0 = time.time()
    try:
        while time.time() - t0 < DURATION + 5:
            if proc.poll() is not None:
                break
            try:
                samples.append(ps.memory_info().rss / 1048576)
            except psutil.NoSuchProcess:
                break
            time.sleep(3)
        proc.wait(timeout=15)
        out, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("MEM_CHECK FAIL: process did not exit")
        return 1
    if proc.returncode != 0:
        print(f"MEM_CHECK FAIL: exit={proc.returncode}")
        return 1
    if not samples:
        print("MEM_CHECK FAIL: no samples")
        return 1
    steady = samples[5:] or samples
    peak = max(samples)
    last10 = samples[-10:]
    lo, hi = min(last10), max(last10)
    print(f"MEM_CHECK n={len(samples)} first={samples[0]:.1f}MB "
          f"steady_avg={sum(steady) / len(steady):.1f}MB peak={peak:.1f}MB "
          f"last10={lo:.1f}-{hi:.1f}MB")
    limit = 100.0
    if peak > limit:
        print(f"MEM_CHECK FAIL: peak {peak:.1f}MB > {limit}MB budget")
        return 1
    if hi - lo > 10.0:
        print(f"MEM_CHECK FAIL: last10 drift {hi - lo:.1f}MB > 10MB")
        return 1
    print("MEM_CHECK PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
