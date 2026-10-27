# -*- coding: utf-8 -*-
"""macOS：读取当前 Wi-Fi、加入指定热点、等待拿到相机网段地址。"""

import re
import subprocess
import time


def _run(args, timeout=20):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def wifi_device():
    code, out = _run(["networksetup", "-listallhardwareports"])
    if code != 0:
        return None
    blocks = out.split("\n\n")
    for b in blocks:
        if "Wi-Fi" in b or "AirPort" in b:
            m = re.search(r"Device:\s*(\S+)", b)
            if m:
                return m.group(1)
    return None


def current_ssid():
    dev = wifi_device()
    if not dev:
        return None
    code, out = _run(["networksetup", "-getairportnetwork", dev])
    if code == 0 and ":" in out:
        ssid = out.split(":", 1)[1].strip()
        return ssid or None
    return None


def ip_of(dev):
    code, out = _run(["ipconfig", "getifaddr", dev], timeout=5)
    return out.strip() if code == 0 else ""


def join(ssid, password, log=print, timeout=30.0, expect_prefix="192.168.2."):
    dev = wifi_device()
    if not dev:
        return False, "找不到 Wi-Fi 网卡"

    log(f"加入热点 {ssid} …")
    code, out = _run(["networksetup", "-setairportnetwork", dev, ssid, password], timeout=45)
    text = out.strip()
    if code != 0 or "Could not find" in text or "Failed" in text or "Error" in text:
        return False, text or f"退出码 {code}"

    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = ip_of(dev)
        if ip.startswith(expect_prefix):
            log(f"已连接，本机地址 {ip}")
            return True, ip
        time.sleep(1.0)
    ip = ip_of(dev)
    if ip:
        return False, f"已关联但地址是 {ip}（多半是 DHCP 没下来，可手动配 192.168.2.2）"
    return False, "关联超时"


def leave(log=print):
    """切回原来的网络由系统自动完成；这里只提示。"""
    log("可关闭窗口后手动切回常用 Wi-Fi")
