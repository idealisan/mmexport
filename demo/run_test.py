#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_test.py — 一次性跑完整条链路，把所有证据落在磁盘上。

为什么要有这个脚本：连上相机 Wi-Fi 就没网，现场调试很难受。
所以它做成"跑一遍、全部记录、回头再分析"：

  preflight → 握手 → 注册/读版本 → 进回放 → 取列表 → HTTP HEAD → 下载 1 MB 验证

跑完会生成一个目录（默认 test-run-<时间>）：
  run.log        全量日志（含每条下发命令、每个阶段耗时）
  udp.bin        收到的所有 UDP 包（[ms:u64][len:u32][data]），可离线复现
  manifest.bin   拼好的 CompositePack 清单原始字节
  summary.txt    结论摘要（直接贴给我就行）

用法：
    python3 run_test.py                     # 全流程，约 30–60 秒
    python3 run_test.py --full-download     # 额外把最小的那个素材整段下下来
    python3 run_test.py --ip 192.168.2.1 --port 9004
"""

import argparse
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cam_download as camlib


class Abort(Exception):
    """预期内的失败：记录后直接结束，不再往下跑。"""


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass

    def isatty(self):
        return False


class Ctx:
    def __init__(self, outdir):
        self.outdir = outdir
        self.rows = []          # (stage, ok, detail)
        self.t0 = time.time()

    def stage(self, name):
        return _Stage(self, name)

    def record(self, name, ok, detail=""):
        self.rows.append((name, ok, detail))
        flag = "PASS" if ok else "FAIL"
        print(f"\n[{flag}] {name}" + (f" — {detail}" if detail else ""))


class _Stage:
    def __init__(self, ctx, name):
        self.ctx, self.name = ctx, name
        self.t = 0.0

    def __enter__(self):
        print(f"\n=== {self.name} " + "=" * max(0, 60 - len(self.name)))
        self.t = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        dt = time.time() - self.t
        if exc_type:
            if not issubclass(exc_type, Abort):
                print(f"!! {self.name} 异常: {exc}")
                traceback.print_exception(exc_type, exc, tb, file=sys.stdout)
            else:
                print(f"!! {self.name} 未通过: {exc}")
            self.ctx.record(self.name, False, f"{exc} ({dt:.1f}s)")
            return False    # 向上抛，由 main 收尾
        print(f"--- {self.name} 用时 {dt:.1f}s")
        return False


def find_wifi_ssid():
    """macOS：读当前 Wi-Fi SSID，纯日志用途。"""
    try:
        devs = subprocess.run(["networksetup", "-listallhardwareports"],
                              capture_output=True, text=True, timeout=5).stdout
        dev = None
        lines = devs.splitlines()
        for i, line in enumerate(lines):
            if "Wi-Fi" in line and i + 1 < len(lines) and "Device:" in lines[i + 1]:
                dev = lines[i + 1].split("Device:")[1].strip()
                break
        if not dev:
            return None, None
        out = subprocess.run(["networksetup", "-getairportnetwork", dev],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        ssid = out.split(": ", 1)[1] if ": " in out else None
        return dev, ssid
    except Exception:
        return None, None


def local_ip_towards(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def tcp_probe(ip, port, timeout=1.0):
    """返回 'open' / 'refused'(主机在，只是没监听) / 'timeout'(基本不在同一网段)。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except (socket.timeout, TimeoutError):
        return "timeout"
    except OSError as e:
        return f"error:{e}"


def write_summary(ctx, extra):
    path = os.path.join(ctx.outdir, "summary.txt")
    with open(path, "w") as f:
        f.write("相机 Wi-Fi 取素材 — 联调报告\n")
        f.write(f"时间: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"总耗时: {time.time() - ctx.t0:.1f}s\n\n")
        for k, v in extra.items():
            f.write(f"{k}: {v}\n")
        f.write("\n")
        for name, ok, detail in ctx.rows:
            f.write(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "") + "\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.2.1")
    ap.add_argument("--port", type=int, default=9004)
    ap.add_argument("--out", default=None)
    ap.add_argument("--full-download", action="store_true", help="把最小的素材完整下载")
    ap.add_argument("--partial-mb", type=float, default=1.0, help="验证下载时只取这么多 MB")
    ap.add_argument("--handshake-attempts", type=int, default=12)
    ap.add_argument("--force", action="store_true", help="跳过 192.168.2.x 网段检查（调试用）")
    ap.add_argument("--check-only", action="store_true", help="只做网络连通性检查，不碰相机")
    args = ap.parse_args()

    outdir = args.out or f"test-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    os.makedirs(outdir, exist_ok=True)
    logf = open(os.path.join(outdir, "run.log"), "w", buffering=1)
    real_out, real_err = sys.stdout, sys.stderr
    tee = Tee(real_out, logf)
    sys.stdout = sys.stderr = tee

    ctx = Ctx(outdir)
    extra = {}
    cam = None
    try:
        # ---------------- 0. 环境 ----------------
        with ctx.stage("0. 环境检查"):
            dev, ssid = find_wifi_ssid()
            my_ip = local_ip_towards(args.ip)
            probe = tcp_probe(args.ip, 7001)
            extra.update({"Wi-Fi 设备": dev, "当前 SSID": ssid,
                          "本机到相机的源 IP": my_ip, "TCP 7001": probe,
                          "目标": f"{args.ip}:{args.port}"})
            print(f"Wi-Fi 设备 : {dev}")
            print(f"当前 SSID  : {ssid}")
            print(f"源 IP      : {my_ip}")
            print(f"TCP 7001   : {probe}")
            if my_ip and my_ip.startswith("169.254."):
                print("→ 拿到了 169.254.x.x（自分配地址），说明关联上了但没拿到 DHCP。")
                print("  手动配 IP：sudo networksetup -setmanual \"Wi-Fi\" 192.168.2.2 255.255.255.0 192.168.2.1")
                print("  用完恢复：sudo networksetup -setdhcp \"Wi-Fi\"")
            ok = (probe in ("open", "refused")) and bool(my_ip and my_ip.startswith("192.168.2."))
            if args.force and probe in ("open", "refused"):
                ok = True
                print("(--force: 跳过网段检查)")
            if not ok:
                print("\n→ 本机根本没连上相机热点（源 IP 不是 192.168.2.x 且 192.168.2.1 无响应）。")
                print("  先解决连接：相机 设置 > 无线连接 打开；Wi-Fi 频段切 2.4G；Mac 用『其他网络』手动加 SSID。")
                raise Abort("不在相机网段")
            ctx.record("0. 环境检查", True, f"SSID={ssid}, 本机={my_ip}")
            if args.check_only:
                print("\n--check-only：网络层看起来没问题，接着跑完整流程去掉这个参数即可。")
                raise Abort("check-only")

        # ---------------- 1. 数据链路 ----------------
        cam = camlib.Camera(args.ip, args.port, verbose=True,
                         capture_path=os.path.join(outdir, "udp.bin"))
        with ctx.stage("1. TCP7001 戳醒 + UDP 握手"):
            t = time.time()
            ok = cam.open_link()
            dt = time.time() - t
            extra["握手端口"] = cam.port
            extra["TCP7001 poke"] = cam.poke_ok
            if not ok:
                print("\n→ 9004 和 10004 都没回握手。常见原因：")
                print("  · 相机 Wi-Fi 刚开还没起 AP（等 10–15 秒再试）")
                print("  · 相机已经被手机 App 占着会话 → 关掉手机 App / 关手机 Wi-Fi")
                print("  · 相机息屏休眠 → 点一下屏幕，保持亮屏")
                print("  · Mac 走了代理/VPN 或别的网卡优先级更高 → 关掉再试")
                raise Abort(f"{dt:.1f}s 无响应")
            ctx.record("1. 握手", True,
                       f"udp/{cam.port} {dt:.1f}s session=0x{cam.link.session_id:04x} "
                       f"channel=0x{cam.link.cam_channel:04x}")

        # ---------------- 2. 注册 / 版本 ----------------
        with ctx.stage("2. 注册 + 读固件版本"):
            cam.register()
            ver = cam.get_version()
            extra["固件版本串"] = ver
            ctx.record("2. 注册", True, ver or "(没解析到版本，可能相机回包较慢)")

        # ---------------- 3. 进回放 ----------------
        with ctx.stage("3. 进入回放模式"):
            cam.enter_playback()
            ctx.record("3. 回放", True, "已发送 0x01/0x01 序列并等待 1.7s")

        # ---------------- 4. 列表 ----------------
        with ctx.stage("4. 取文件列表"):
            manifest_path = os.path.join(outdir, "manifest.bin")
            files = cam.list_media(dump_manifest=manifest_path)
            extra["文件数"] = len(files)
            try:
                declared = camlib.manifest_count(open(manifest_path, "rb").read())
            except OSError:
                declared = 0
            extra["清单声明条数"] = declared
            print(f"清单声明 {declared or '?'} 条，解析到 {len(files)} 个文件")
            if declared and len(files) < declared:
                print("  ! 少了几条：传输被截断，重跑一次")
            for i, f in enumerate(files[:10]):
                print(f"  [{i:2d}] storage={f['store']} {f['path']}")
            if not files:
                print("\n→ 列表为空：确认相机里确实有素材；若刚开机，重跑一次（存储挂载慢）。")
                raise Abort("0 条")
            ctx.record("4. 列表", True, f"{len(files)} 条")

        # ---------------- 5. HTTP HEAD ----------------
        with ctx.stage("5. HTTP HEAD 探测"):
            pick = files[0]
            code, size0, hdrs = camlib.head_detail(args.ip, pick["store"], pick["path"])
            print(f"storage={pick['store']} {pick['path']}")
            print(f"  HTTP {code}  Content-Length={size0}  headers={hdrs}")
            if size0 == 0 and pick["store"] == 0:
                code1, size1, hdrs1 = camlib.head_detail(args.ip, 1, pick["path"])
                print(f"storage=1 试探 -> HTTP {code1} Content-Length={size1} {hdrs1}")
                if size1 > 0:
                    pick["store"], size0 = 1, size1
            if size0 == 0:
                # 再确认一次：有的固件 HEAD 不给长度，但 Range GET 能拿到
                req = camlib.http_headers(args.ip, pick["store"], pick["path"], "GET")
                req.add_header("Range", "bytes=0-1023")
                try:
                    with camlib.http_open(req, timeout=8) as r:
                        probe = r.read(1024)
                    print(f"  Range GET -> HTTP {r.getcode()}  {len(probe)} B，前 32 字节 {probe[:32].hex()}")
                    if probe:
                        size0 = -1     # 标记：内容能取到，只是没长度
                except Exception as e:
                    print(f"  Range GET 也失败: {type(e).__name__}: {e}")
            extra["样本文件"] = pick["path"]
            extra["样本大小"] = size0
            if size0 == 0:
                raise Abort("HTTP 取不到内容（看上面的状态码/headers）")
            ctx.record("5. HEAD", True,
                       "Content-Length 未知，但 Range 能读到" if size0 < 0 else f"{size0/1048576:.1f} MB")

        # ---------------- 6. 下载验证 ----------------
        with ctx.stage("6. HTTP 下载验证"):
            limit = int(args.partial_mb * 1024 * 1024)
            take = size0 if (size0 > 0 and (args.full_download or size0 <= limit)) else limit
            dest = os.path.join(outdir, pick["name"])
            req = camlib.http_headers(args.ip, pick["store"], pick["path"], "GET", 0)
            req.add_header("Range", f"bytes=0-{take-1}")
            t = time.time()
            got = 0
            with camlib.http_open(req, timeout=30) as r, open(dest, "wb") as fh:
                if r.getcode() not in (200, 206):
                    raise RuntimeError(f"HTTP {r.getcode()}")
                while got < take:
                    block = r.read(min(262144, take - got))
                    if not block:
                        break
                    fh.write(block)
                    got += len(block)
            dt = max(time.time() - t, 1e-6)
            mbps = got / 1048576 / dt
            extra["下载字节"] = got
            extra["下载速度MB/s"] = round(mbps, 2)
            print(f"取回 {got/1048576:.2f} MB，用时 {dt:.2f}s，约 {mbps:.2f} MB/s")
            ok = got == take
            ctx.record("6. 下载", ok, f"{got} B, {mbps:.2f} MB/s")

    except (Abort, SystemExit) as e:
        extra["提前结束"] = str(e)
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        ctx.record("未捕获异常", False, str(e))
    finally:
        try:
            if cam:
                cam.close()
        except Exception:
            pass

        summary = write_summary(ctx, extra)
        print("\n" + "=" * 66)
        print("摘要:")
        for name, ok, detail in ctx.rows:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        print(f"\n全部文件在: {os.path.abspath(outdir)}")
        print("  run.log / udp.bin / manifest.bin / summary.txt")
        print("把整个目录打包发我，或直接贴 summary.txt。")
        sys.stdout = real_out
        sys.stderr = real_err
        logf.close()

    ok_all = all(ok for _, ok, _ in ctx.rows) and len(ctx.rows) >= 5
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
