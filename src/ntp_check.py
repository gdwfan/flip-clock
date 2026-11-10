"""网络校时：daemon 线程，stdlib socket。

角色是「每日一次偏差体检」而非走时来源（方案 §1.2）：
- 显示永远走本地时间；本模块只产出 {状态, 偏差秒数, 服务器} 供 UI 徽标展示
- NTP UDP 123 优先（轮询服务器），HTTP Date 头兜底（秒级精度）
- 任何失败静默；绝不碰 UI（线程铁律：工作线程只写状态，UI 侧轮询 snapshot()）
"""
import socket
import struct
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

NTP_SERVERS = ["ntp.aliyun.com", "time.windows.com", "cn.ntp.org.cn"]
HTTP_FALLBACKS = ["www.baidu.com", "www.microsoft.com"]
NTP_EPOCH_DELTA = 2208988800  # 1900→1970
WARN_OFFSET_S = 2.0  # 偏差超过此值徽标转黄提示


class NtpChecker:
    def __init__(self, interval_h=24):
        self._interval = interval_h * 3600
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._gen = 0  # 线程代际：重启校时时让老线程自行退休
        self.status = "init"  # init / ok / fail
        self.offset_s = None
        self.last_check = None  # datetime | None
        self.server = None

    # ---- 生命周期 ----
    def start(self, delay_s=30):
        """启动校时线程。

        代际机制的必要性：stop() 只是置事件，老线程若正在 DNS/网络等待（最长 3 秒超时）
        或尚在 wait 中，直接再 start 会留下两个线程同时 _check_once（2026-11-08 复查发现）。
        """
        self._gen += 1          # 老线程看到代际不符即自行退出
        self._stop.clear()
        gen = self._gen
        self._thread = threading.Thread(target=self._run, args=(delay_s, gen),
                                        name=f"flipclock-ntp-{gen}", daemon=True)
        self._thread.start()

    def stop(self):
        self._gen += 1          # 与新 start 双重保险：在跑线程下一轮必退
        self._stop.set()

    def _run(self, delay_s, gen):
        if self._stop.wait(delay_s):
            return
        while not self._stop.is_set() and gen == self._gen:
            self._check_once()
            if self._stop.wait(self._interval):
                return

    # ---- 状态读取（UI 侧轮询）----
    def snapshot(self):
        with self._lock:
            return {
                "status": self.status,
                "offset_s": self.offset_s,
                "last_check": self.last_check,
                "server": self.server,
            }

    def _publish(self, status, offset, server):
        with self._lock:
            self.status = status
            self.offset_s = offset
            self.last_check = datetime.now()
            self.server = server

    # ---- 一次校验 ----
    def _check_once(self):
        for host in NTP_SERVERS:
            try:
                off = self._ntp_offset(host, timeout=3.0)
                if off is not None:
                    self._publish("ok", off, host)
                    return
            except OSError:
                continue
        for host in HTTP_FALLBACKS:
            try:
                off = self._http_date_offset(host, timeout=3.0)
                if off is not None:
                    self._publish("ok", off, host + " (HTTP)")
                    return
            except OSError:
                continue
        self._publish("fail", None, None)

    # ---- NTP 协议 ----
    @staticmethod
    def _ntp_offset(host, timeout=3.0):
        """返回 offset（服务器 - 本机，秒）；失败返回 None。"""
        req = b"\x1b" + b"\x00" * 47  # LI=0 VN=3 Mode=3(client)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            t0 = time.time()
            s.sendto(req, (host, 123))
            data, _ = s.recvfrom(1024)
            t1 = time.time()
        return NtpChecker.parse_ntp_transmit(data, t0, t1)

    @staticmethod
    def parse_ntp_transmit(data, t0, t1):
        """纯函数：48 字节 NTP 响应 + 收发时刻 → offset 秒。（离线单测用）"""
        if len(data) < 48:
            return None
        secs = struct.unpack("!I", data[40:44])[0]
        frac = struct.unpack("!I", data[44:48])[0]
        if secs == 0 and frac == 0:
            return None  # 服务器没填时间戳（Kiss-o'-Death 等）
        server_t = secs + frac / 2**32 - NTP_EPOCH_DELTA
        return server_t - (t0 + t1) / 2.0

    # ---- HTTP Date 头兜底 ----
    @staticmethod
    def _http_date_offset(host, timeout=3.0):
        with socket.create_connection((host, 80), timeout=timeout) as s:
            t0 = time.time()
            s.sendall(f"HEAD / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode("ascii"))
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
            t1 = time.time()
        header = resp.decode("latin-1", "ignore")
        for line in header.split("\r\n"):
            if line.lower().startswith("date:"):
                try:
                    dt = parsedate_to_datetime(line[5:].strip())
                except (TypeError, ValueError):
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp() - (t0 + t1) / 2.0
        return None
