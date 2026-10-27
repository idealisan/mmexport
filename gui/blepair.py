# -*- coding: utf-8 -*-
"""BLE 配对：扫描相机 → SetPairingPIN → 读热点 SSID/密码。

相机把 Wi-Fi 凭据放在 BLE 控制通道上（GATT fff0 / 写 fff5 / 通知 fff4），
配对通过后向 0x07/0x07、0x07/0x0e 各要一次即可。
"""

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demo"))
import cam_download as lib  # noqa: E402

SVC = "0000fff0-0000-1000-8000-00805f9b34fb"
CH_FFF4 = "0000fff4-0000-1000-8000-00805f9b34fb"
CH_FFF5 = "0000fff5-0000-1000-8000-00805f9b34fb"

TARGET_WIFI = 0x0702
PAIR_TOKEN = b"osmo"
IDENT = b"284ae5b8d76b3375a04a6417ad71bea3"


class BleError(RuntimeError):
    pass


class _Buf:
    def __init__(self):
        self.data = b""

    def feed(self, chunk: bytes):
        self.data += chunk
        out = list(lib.iter_frames(self.data))
        # 丢掉已解析的字节：保留最后一个 SOF 之后的内容
        idx = self.data.rfind(b"\x55")
        self.data = self.data[idx:] if idx >= 0 else b""
        return out


def _pack_str(b: bytes) -> bytes:
    return bytes([len(b)]) + b


async def _scan(timeout=8.0):
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    cands = []
    if not found:
        print("（附近没有蓝牙设备，检查蓝牙是否开启）", flush=True)
    for addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name or ""
        mfr = adv.manufacturer_data or {}
        is_cam = name.lower().startswith(("osmo", "dji", "action", "xtra")) or 0x08AA in mfr
        if is_cam:
            cands.append((addr, name, dev))
    return cands


async def _request(client, buf, set_id, cmd_id, payload=b"", msg_id=0x8000, wait=1.5):
    frame = lib.build_frame(TARGET_WIFI, msg_id, 0x40, set_id, cmd_id, payload)
    await client.write_gatt_char(CH_FFF5, frame, response=False)
    deadline = time.time() + wait
    while time.time() < deadline:
        for cs, ci, fl, pl in buf.feed(b""):
            if (cs, ci) == (set_id, cmd_id):
                return pl
        await asyncio.sleep(0.05)
    return None


async def _pair_and_read(dev, log, timeout_approve=60.0):
    from bleak import BleakClient

    buf = _Buf()
    notify_holder = {}

    async with BleakClient(dev, timeout=15.0) as client:
        def on_notify(_char, data: bytearray):
            notify_holder.setdefault("frames", []).extend(buf.feed(bytes(data)))

        await client.start_notify(CH_FFF4, on_notify)
        try:
            await client.start_notify(CH_FFF5, on_notify)
        except Exception:
            pass
        # 武装配对
        try:
            await client.write_gatt_char(CH_FFF4, b"\x01\x00", response=True)
        except Exception as e:
            log(f"武装配对失败（可继续尝试）: {e}")
        await asyncio.sleep(0.25)

        payload = _pack_str(IDENT) + _pack_str(PAIR_TOKEN)
        frame = lib.build_frame(TARGET_WIFI, 0x8092, 0x40, 0x07, 0x45, payload)
        await client.write_gatt_char(CH_FFF5, frame, response=False)

        log("等待配对回执…")
        status = None
        deadline = time.time() + 8
        while time.time() < deadline and status is None:
            for cs, ci, fl, pl in buf.feed(b""):
                if (cs, ci) == (0x07, 0x45) and pl:
                    status = pl[-1]
            await asyncio.sleep(0.05)

        if status == 0x02:
            log(f"相机屏幕上会显示配对码，请在相机上确认（最多 {int(timeout_approve)}s）")
            deadline = time.time() + timeout_approve
            while time.time() < deadline:
                for cs, ci, fl, pl in buf.feed(b""):
                    if (cs, ci) == (0x07, 0x46):
                        await client.write_gatt_char(
                            CH_FFF5, lib.build_frame(TARGET_WIFI, 0x8093, 0xC0, 0x07, 0x46, b"\x00"),
                            response=False)
                        status = 0x01
                if status == 0x01:
                    break
                await asyncio.sleep(0.2)
        if status not in (0x00, 0x01):
            raise BleError("配对未完成（相机未确认或已超时）")
        log("配对通过，读取热点信息…")

        ssid_pl = await _request(client, buf, 0x07, 0x07, b"", 0x8010)
        pwd_pl = await _request(client, buf, 0x07, 0x0E, b"", 0x8011)

        def parse(pl):
            if not pl or len(pl) < 2:
                return None
            n = pl[1]
            return pl[2:2 + n].decode("utf-8", "replace") if n and 2 + n <= len(pl) else None

        ssid, pwd = parse(ssid_pl), parse(pwd_pl)
        if not ssid:
            raise BleError("没读到 SSID（相机可能不在可配对状态）")
        return {"ssid": ssid, "password": pwd or ""}


def discover_and_pair(log=print, timeout=10.0, approve_timeout=60.0):
    """同步入口：扫描 + 配对 + 读凭据。返回 {'ssid','password','name'}。"""
    try:
        import bleak  # noqa: F401
    except ImportError as e:
        raise BleError(f"缺少依赖 bleak（pip install bleak）：{e}")

    async def run():
        log("扫描附近相机（蓝牙）…")
        try:
            # 首次扫描要先等系统授权，给足时间
            cands = await asyncio.wait_for(_scan(timeout), timeout=timeout + 45)
        except asyncio.TimeoutError:
            raise BleError("蓝牙扫描超时：到「系统设置 → 隐私与安全性 → 蓝牙」允许终端使用蓝牙，并确认相机蓝牙已开")
        if not cands:
            raise BleError("没扫到相机：确认相机蓝牙已开、距离近一些，并允许终端使用蓝牙")
        addr, name, dev = cands[0]
        log(f"找到 {name or addr}，连接…")
        info = await _pair_and_read(dev, log, approve_timeout)
        info["name"] = name or addr
        return info

    box = {}

    def runner():
        try:
            box["v"] = asyncio.run(run())
        except BaseException as e:      # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout + approve_timeout + 100)
    if t.is_alive():
        raise BleError("蓝牙无响应：到「系统设置 → 隐私与安全性 → 蓝牙」允许终端使用蓝牙，再试一次")
    if "e" in box:
        e = box["e"]
        raise e if isinstance(e, BleError) else BleError(str(e))
    return box["v"]
