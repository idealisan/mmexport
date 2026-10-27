#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cam_download.py — 通过相机自带 Wi-Fi 热点列出并下载原始视频/照片，不依赖官方 SDK、不依赖官方 App。

流程（按相机 Wi-Fi 上的公开报文整理）：
  1) 电脑加入相机热点（192.168.2.1，WPA2，密码显示在相机屏幕上）。
  2) 向 TCP 7001 发一帧配对请求（SetPairingPIN），把数据链路"戳醒"。
  3) 在 UDP 9004 上做私有滑动窗口握手（pktType 0x00），学习相机通道号后
     把发送序列同步到 camera_channel + 8。
  4) 注册：0x00/0x81(设备信息) → 0x00/0x88(APP 心跳) → 0x03/0xDA(云台 init)。
  5) 目标机型对 0x02/0x0c（进入回放）回 e0，必须改用 0x01/0x01 两段式命令进入回放，
     否则 0x00/0x26（取文件列表）只回 d8（不给数据）。
  6) 0x00/0x26 查询 → 相机用多条 0x00/0x27 分片回列表；每条 payload = 10 字节子头 + 数据，
     去掉子头按到达顺序拼接，就是 CompositePack 清单。
  7) 清单里的媒体路径字段是  \x1a <len> 00 00 00 01 <ascii "DCIM/...">（不带扩展名），
     真实文件名在同一条记录的 0d 字段里，两者拼起来才是完整路径。
  8) 文件本体走相机内置 lighttpd：
         GET http://192.168.2.1/v2?storage=0&path=<清单里的完整路径>
     支持 HEAD(Content-Length) 与 Range(206)，因此可以断点续传。

用法：
    python3 cam_download.py probe                  # 握手 + 打印相机状态
    python3 cam_download.py list                   # 列出最新一页（45 条）
    python3 cam_download.py download --index 0     # 下载第 0 条
    python3 cam_download.py download --all -o ./videos
    python3 cam_download.py --self-test            # 离线自检（CRC/组帧）

注意：本脚本只做"能跑通"的最小实现；连着相机热点时电脑没有外网，属正常现象。
"""

import argparse
import os
import random
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------------------
# 1. 协议帧 组帧 / 解帧
# --------------------------------------------------------------------------------------
# 帧格式（SOF=0x55）：
#   0    1B  SOF = 0x55
#   1    1B  len_lo          ) total_len = 13 + payload_len
#   2    1B  (ver<<2)|len_hi ) ver = 1  → 该字节通常为 0x04
#   3    1B  CRC8(bytes[0:3])
#   4    2B  target  = 0x02 | ((recv_id<<5|recv_type) << 8)   (小端)
#   6    2B  msg id / seq (小端; UDP datalink 上就是递增序号)
#   8    3B  type = flags | (cmd_set<<8) | (cmd_id<<16)       (小端)
#   11   NB  payload
#   end  2B  CRC16(bytes[0:11+NB])                            (小端)
# flags: 0x40=请求 0xC0=响应/ACK 0x00=通知
SOF = 0x55
CRC8_INIT, CRC8_POLY = 0x77, 0x8C      # 反射形式；等价 spec init=0xEE poly=0x31
CRC16_INIT, CRC16_POLY = 0x3692, 0x8408


def crc8(data: bytes, init: int = CRC8_INIT, poly: int = CRC8_POLY) -> int:
    crc = init & 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
    return crc & 0xFF


def crc16(data: bytes, init: int = CRC16_INIT, poly: int = CRC16_POLY) -> int:
    crc = init & 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_frame(target: int, msg_id: int, flags: int, cmd_set: int, cmd_id: int,
               payload: bytes = b"") -> bytes:
    """组一个完整 协议帧。"""
    total = 13 + len(payload)
    if total > 1023:
        raise ValueError("payload too long for a 协议帧 frame")
    head = bytearray([SOF, total & 0xFF, ((total >> 8) & 0x03) | (1 << 2)])
    head.append(crc8(bytes(head)))
    head += struct.pack("<H", target)
    head += struct.pack("<H", msg_id & 0xFFFF)
    head += struct.pack("<I", (flags & 0xFF) | ((cmd_set & 0xFF) << 8) | ((cmd_id & 0xFF) << 16))[:3]
    body = bytes(head) + payload
    return body + struct.pack("<H", crc16(body))


def iter_build_frames(buf: bytes):
    """在一段原始字节里扫出所有 CRC 正确的 协议帧，逐字节前进（长帧/隧道帧都能覆盖）。"""
    i = 0
    n = len(buf)
    while i + 13 <= n:
        if buf[i] != SOF:
            i += 1
            continue
        total = (buf[i + 1] | ((buf[i + 2] & 0x03) << 8))
        ver = buf[i + 2] >> 2
        if ver != 1 or total < 13 or i + total > n:
            i += 1
            continue
        if crc8(buf[i:i + 3]) != buf[i + 3]:
            i += 1
            continue
        body = buf[i:i + total - 2]
        if crc16(body) != struct.unpack_from("<H", buf, i + total - 2)[0]:
            i += 1
            continue
        flags = buf[i + 8]
        cmd_set = buf[i + 9]
        cmd_id = buf[i + 10]
        payload = buf[i + 11:i + total - 2]
        yield cmd_set, cmd_id, flags, payload
        i += 1


def hexdump(b: bytes) -> str:
    return b.hex()


# --------------------------------------------------------------------------------------
# 2. UDP datalink（[8B 传输头][12B 路由头][协议帧]）
# --------------------------------------------------------------------------------------
HANDSHAKE = bytes.fromhex(
    "000064006400c005140000640000019001c005140000640014006400c00514000064000101040102")
APP_PRESENCE = bytes.fromhex("170046237c415050000000000002")

# 0x00/0x26 文件列表查询模板；@4=请求计数器, @10-13=游标(u32-LE), @14=每页条数
LIST_CMD = bytearray(bytes.fromhex(
    "4a002a10010000000000010000002d000d0100ffffffffffffffff000100000000000000000000000000"))
TRIGGER_CMD = bytes.fromhex("4a040e1001000000000001000000")

# 目标机型 / Pocket 3 专用回放进入（0x01/0x01, SPECIAL Control）两段负载
PB_STAGE1 = bytes.fromhex("0300000000040000000701")
PB_STAGE2 = bytes.fromhex("0000000000040000000401")


class Datalink:
    """UDP 数据链路：会话、序列、握手、ACK、命令下发。"""

    def __init__(self, ip: str, port: int = 9004, verbose: bool = True, capture_path: str = None):
        self.ip = ip
        self.port = port
        self.verbose = verbose
        self.capture_path = capture_path
        self._cap = None
        self.sock = None
        self.session_id = 0
        self.base_seq = 0
        self.cam_channel = 0
        self.udp_seq = 0
        self.frame_seq = 0xA000
        self.cmd_counter = 0
        self.peer_cursor = 0            # 相机的视频可靠窗口末端
        self.peer_download_cursor = 0   # 相机的下载可靠窗口末端（列表就骑在这上面）
        self._buf = b""

    # ---- 基础 ---------------------------------------------------------------------
    def log(self, msg: str):
        if self.verbose:
            print(f"[datalink] {msg}", file=sys.stderr)

    def udp_header(self, pkt_type: int, payload_len: int, seq: int = None) -> bytes:
        total = 8 + payload_len
        w0 = 0x8000 | (total & 0x3FFF)
        s = self.udp_seq if seq is None else seq
        b = bytearray(struct.pack("<HHH", w0, self.session_id, s & 0xFFFF))
        b.append(pkt_type & 0xFF)
        x = 0
        for c in b:
            x ^= c
        b.append(x)
        return bytes(b)

    def routing_header(self) -> bytes:
        ack = (self.udp_seq - 8) & 0xFFFF
        return struct.pack("<HH", ack, self.udp_seq) + bytes(
            [0, 0, 0, 0, self.cmd_counter & 0xFF, 0x01, 0x00, 0x00])

    def advance(self):
        self.udp_seq = (self.udp_seq + 8) & 0xFFFF

    def send_packet(self, pkt: bytes):
        try:
            self.sock.sendto(pkt, (self.ip, self.port))
            return True
        except OSError as e:
            self.log(f"send failed: {e}")
            return False

    def send_raw(self, pkt_type: int, payload: bytes):
        pkt = self.udp_header(pkt_type, len(payload)) + payload
        if self.send_packet(pkt):
            self.advance()

    # ---- 生命周期 ------------------------------------------------------------------
    def open(self):
        if self.capture_path:
            os.makedirs(os.path.dirname(os.path.abspath(self.capture_path)), exist_ok=True)
            self._cap = open(self.capture_path, "ab")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.15)
        self.session_id = random.randrange(0x1000, 0xFFFE)
        self.base_seq = random.randrange(0x1000, 0xF000) & 0xFFF8
        self.cam_channel = self.base_seq
        self.udp_seq = 0
        self.cmd_counter = 0
        self.frame_seq = 0xA000

    def close(self):
        if self._cap:
            try:
                self._cap.close()
            except OSError:
                pass
            self._cap = None
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ---- 接收 ---------------------------------------------------------------------
    def recv(self, duration: float = 0.4):
        """在 duration 秒内尽量多地收包，并顺带学习相机通道号 / 窗口游标。"""
        out = []
        deadline = time.time() + duration
        while time.time() < deadline:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            out.append(data)
            if self._cap:
                self._cap.write(struct.pack("<QI", int(time.time() * 1000), len(data)) + data)
                self._cap.flush()
            if len(data) >= 10:
                ch = data[8] | (data[9] << 8)
                if ch:
                    self.cam_channel = ch
            if len(data) == 34 and data[6] == 0x01:
                self.peer_cursor = data[10] | (data[11] << 8)
                self.peer_download_cursor = data[18] | (data[19] << 8)
        return out

    def ack(self):
        """回 34 字节 pktType 0x04 窗口 ACK；不回相机会停止推列表。"""
        def grp(v):
            return struct.pack("<HH", v & 0xFFFF, v & 0xFFFF) + b"\x00\x00\x00\x00"

        payload = grp(self.peer_cursor) + grp(self.peer_download_cursor) + grp(self.base_seq) + b"\x00\x00"
        old = self.udp_seq
        self.udp_seq = 0
        pkt = self.udp_header(0x04, len(payload), seq=0) + payload
        self.udp_seq = old
        self.send_packet(pkt)

    # ---- 握手 ---------------------------------------------------------------------
    def tcp_poke(self):
        """TCP 7001：发一帧配对请求（SetPairingPIN），把相机 AP 上的数据链路戳醒。"""
        try:
            with socket.create_connection((self.ip, 7001), timeout=1.2) as s:
                s.sendall(script_pair_frame())
                time.sleep(0.4)
            return True
        except OSError as e:
            self.log(f"TCP 7001 poke failed (可能不需要): {e}")
            return False

    def handshake(self, attempts: int = 20):
        payload = bytearray(HANDSHAKE)
        struct.pack_into("<H", payload, 0, self.base_seq)
        for _ in range(attempts):
            self.send_raw(0x00, bytes(payload))
            for d in self.recv(0.35):
                if len(d) >= 8 and d[6] == 0x00:
                    self.log(f"handshake OK on udp/{self.port}")
                    return True
        return False

    def sync_seq_to_peer(self):
        self.udp_seq = (self.cam_channel + 8) & 0xFFFF

    # ---- 命令 ---------------------------------------------------------------------
    def send_cmd(self, cmd_set: int, cmd_id: int, payload: bytes = b"",
                 recv_type: int = 0x01, recv_id: int = 0, cmd_type: int = 2):
        self.cmd_counter += 1
        self.log(f"→ {cmd_set:02x}/{cmd_id:02x} len={len(payload)} to {recv_type:02x}/{recv_id} type={cmd_type}")
        target = 0x02 | ((((recv_id << 5) | recv_type) & 0xFF) << 8)
        flags = (cmd_type << 5) & 0xFF
        frame = build_frame(target, self.frame_seq, flags, cmd_set, cmd_id, payload)
        self.frame_seq = (self.frame_seq + 1) & 0xFFFF
        pkt = (self.udp_header(0x05, len(self.routing_header()) + len(frame))
               + self.routing_header() + frame)
        if self.send_packet(pkt):
            self.advance()


def script_pair_frame() -> bytes:
    """TCP 7001 / BLE 用的 SetPairingPIN 帧：identifier + token 两个 PackString。"""
    ident = b"284ae5b8d76b3375a04a6417ad71bea3"
    # 最后一个 PackString 是配对 token（协议常量，固定 4 字节）
    token = bytes.fromhex("6f736d6f")
    payload = bytes([len(ident)]) + ident + bytes([4]) + token
    return build_frame(0x0702, 0x8092, 0x40, 0x07, 0x45, payload)


# --------------------------------------------------------------------------------------
# 3. 相机会话：注册 → 进回放 → 取列表
# --------------------------------------------------------------------------------------
class Camera:
    def __init__(self, ip: str = "192.168.2.1", port: int = 9004, verbose: bool = True,
                 capture_path: str = None):
        self.ip = ip
        self.port = port
        self.verbose = verbose
        self.capture_path = capture_path
        self.poke_ok = None
        self.link = Datalink(ip, port, verbose, capture_path)

    def log(self, msg: str):
        if self.verbose:
            print(f"[cam] {msg}", file=sys.stderr)

    def _sync(self):
        for _ in range(5):
            self.link.recv(0.4)
            self.link.ack()
        self.link.sync_seq_to_peer()

    def open_link(self, try_alt: bool = True) -> bool:
        """TCP 7001 戳醒 + UDP 握手 + 同步序列。失败返回 False，不抛异常。"""
        self.link.open()
        self.poke_ok = self.link.tcp_poke()
        if self.link.handshake():
            self._sync()
            return True
        if not try_alt:
            return False
        # 换壳机型（Xtra Edge Pro / 同系列机型）数据链路在 10004，且不需要 poke
        self.link.close()
        self.log(f"udp/{self.port} 没回应，换 10004 再试")
        self.port = 10004
        self.link = Datalink(self.ip, self.port, self.verbose, self.capture_path)
        self.link.open()
        if self.link.handshake():
            self._sync()
            return True
        return False

    def connect(self):
        if not self.open_link():
            raise RuntimeError("握手失败：确认电脑已连上相机 Wi-Fi，且相机不在息屏/关机状态")

    def get_version(self) -> str:
        """0x00/0x00 → DM368(id=2)，响应是 NUL 分隔的 sdk\0name\0firmware。"""
        self.link.send_cmd(0x00, 0x00, b"", 0x08, 2, cmd_type=4)
        text = ""
        for d in self.link.recv(0.8):
            for cs, ci, fl, pl in iter_build_frames(d):
                if cs == 0x00 and ci == 0x00 and pl:
                    text = pl.decode("latin-1", "replace").strip("\x00")
        if text:
            self.log("设备: " + " | ".join(p for p in text.split("\x00") if p))
        return text

    def register(self):
        # 0x00/0x81 设备信息（DM368 id=2, cmd_type=4）
        self.link.send_cmd(0x00, 0x81, device_info_payload(), 0x08, 2, cmd_type=4)
        self.link.recv(0.4); self.link.ack()
        # 0x00/0x88 APP 在线心跳
        self.link.send_cmd(0x00, 0x88, APP_PRESENCE, 0x08, 1)
        self.link.recv(0.4); self.link.ack()
        # 0x03/0xDA 云台 init
        self.link.send_cmd(0x03, 0xDA, bytes.fromhex("05ffffffff"), 0x03, 0)
        self.link.recv(0.4); self.link.ack()

    def enter_playback(self):
        """目标机型 对 0x02/0x0c 回 e0，只认 0x01/0x01 两段式。"""
        self.link.send_cmd(0x02, 0x0C, bytes.fromhex("01010001"), 0x01, 0)
        replied = False
        for d in self.link.recv(0.6):
            for cs, ci, fl, pl in iter_build_frames(d):
                if cs == 0x02 and ci == 0x0C:
                    replied = True
                    if pl and pl[0] not in (0x00,):
                        self.log(f"0x02/0x0c 被拒 (0x{pl[0]:02x})，改用 0x01/0x01 进回放")
        # 无论是否被拒都走 0x01/0x01（对已进回放的机身是幂等的）
        for _ in range(6):
            self.link.send_cmd(0x01, 0x01, PB_STAGE1, 0x01, 0, cmd_type=0)
            time.sleep(0.05)
        deadline = time.time() + 2.5
        while time.time() < deadline:
            self.link.send_cmd(0x01, 0x01, PB_STAGE2, 0x01, 0, cmd_type=0)
            time.sleep(0.05)
        if not replied:
            self.log("已发送回放入场（0x01/0x01）")
        # 回放确认后要等约 1.7s 让存储挂载，否则 0x00/0x26 只回 d8
        time.sleep(1.7)

    # ---- 列表 ---------------------------------------------------------------------
    @staticmethod
    def list_payload(counter: int, cursor: int, page_size: int = 45) -> bytes:
        b = bytearray(LIST_CMD)
        b[4] = counter & 0xFF
        struct.pack_into("<I", b, 10, cursor & 0xFFFFFFFF)
        b[14] = page_size & 0xFF
        return bytes(b)

    def list_media(self, timeout: float = 12.0, dump_manifest: str = None):
        """取最新一页（最多 45 条）。返回 [{path,name,store}]。"""
        self.link.send_cmd(0x00, 0x26, self.list_payload(1, 0x00000001), 0x01, 0)

        chunks = {1: bytearray(), 2: bytearray()}
        ended = set()
        deadline = time.time() + timeout
        last_beat = 0.0
        last_data = 0.0
        end_at = 0.0
        batch = 0
        trigger_sent = second_sent = False

        while time.time() < deadline:
            pkts = self.link.recv(0.4)
            batch += 1
            for p in pkts:
                for cs, ci, fl, pl in iter_build_frames(p):
                    if cs == 0x00 and ci == 0x27 and len(pl) > 10 and pl[0] == 0x4A:
                        sub = pl[1]
                        ctr = pl[4]
                        if sub == 0x01 and ctr in chunks:
                            chunks[ctr] += pl[10:]
                            last_data = time.time()
                        elif sub == 0x03:
                            ended.add(ctr)
                            if ctr == 1:
                                end_at = time.time()
            self.link.ack()

            if time.time() - last_beat > 1.0:
                self.link.send_cmd(0x00, 0x88, APP_PRESENCE, 0x08, 1)
                last_beat = time.time()
            if not trigger_sent and batch >= 2:
                self.link.send_cmd(0x00, 0x26, TRIGGER_CMD, 0x01, 0)
                trigger_sent = True
            if not second_sent and batch >= 4:
                # 双存储机身（内部存储）用 0x40000001；目标机型 只有 SD，回空是正常的
                self.link.send_cmd(0x00, 0x26, self.list_payload(2, 0x40000001), 0x01, 0)
                second_sent = True
            # 收全的判定：清单头声明的条数都到了（最可靠）
            got_n = len(extract_media(bytes(chunks[1])))
            declared = manifest_count(bytes(chunks[1]))
            if declared and got_n >= declared:
                break
            # 或者收到结束帧，再多等 0.6s 收尾巴
            if 1 in ended and end_at and time.time() - end_at > 0.6:
                break
            # 兜底：有数据后安静 3s（1.5s 会切掉还在路上的尾页）
            if last_data and time.time() - last_data > 3.0 and batch >= 3:
                break

        if dump_manifest:
            with open(dump_manifest, "wb") as fh:
                fh.write(bytes(chunks[1]))
            self.log(f"清单已存到 {dump_manifest} ({len(chunks[1])} B)")

        if not chunks[1]:
            self.log("清单为空：多半是没进回放（相机对 0x00/0x26 回 d8），或相机里没有素材")
        else:
            declared = manifest_count(bytes(chunks[1]))
            got_n = len(extract_media(bytes(chunks[1])))
            if declared and got_n < declared:
                self.log(f"警告：清单声明 {declared} 条，只收到 {got_n} 条（传输被截断，重跑一次）")

        files = []
        for ctr, buf in chunks.items():
            if not buf:
                continue
            store = 0 if ctr == 1 else 1
            for m in extract_media(bytes(buf)):
                files.append({"path": m["path"], "name": m["name"], "store": store})
        # 去重（分页会有重叠）
        seen, out = set(), []
        for f in files:
            if f["path"] in seen:
                continue
            seen.add(f["path"])
            out.append(f)
        return out

    def close(self):
        self.link.close()


def device_info_payload() -> bytes:
    """62 字节：\x00APP + 37*00 + 02 + 8*00 + 02 08 + 10*00（与 官方 App 一致）。"""
    b = bytearray(62)
    b[1], b[2], b[3] = ord("A"), ord("P"), ord("P")
    b[41] = 0x02
    b[50], b[51] = 0x02, 0x08
    return bytes(b)


def _find_name(buf: bytes, lo: int, hi: int, stem: str):
    """在一条记录的区间里找 0d <len> <ascii 文件名.扩展名>，用文件名主干和路径主干对齐。"""
    i = max(0, lo)
    while i + 2 < hi:
        if buf[i] == 0x0D:
            ln = buf[i + 1]
            if 5 <= ln <= 120 and i + 2 + ln <= hi:
                raw = buf[i + 2:i + 2 + ln]
                if all(0x20 <= c <= 0x7E for c in raw) and b"." in raw:
                    s = raw.decode("latin-1")
                    if s.rsplit(".", 1)[0] == stem:
                        return s
        i += 1
    return None


def extract_media(manifest: bytes):
    """按 TLV 解析 CompositePack：媒体路径 1a .. 00 00 00 01 DCIM/...（**不带扩展名**），
    真实文件名在它前面的 0d 字段里（带扩展名），两者拼起来才是 HTTP 要的路径。"""
    hits = []       # (pos, end, path)
    i = 0
    n = len(manifest)
    while i + 6 <= n:
        if manifest[i] == 0x1A and manifest[i + 2] == 0 and manifest[i + 3] == 0 and manifest[i + 4] == 0:
            sub = manifest[i + 5]
            total = manifest[i + 1]
            slen = total - 6
            if sub == 0x01 and slen > 5 and i + 6 + slen <= n:
                path = manifest[i + 6:i + 6 + slen].decode("latin-1")
                if path.startswith("DCIM/") and all(0x20 <= ord(c) <= 0x7E for c in path):
                    hits.append((i, i + 6 + slen, path))
                    i += 6 + slen
                    continue
        i += 1

    out = []
    for k, (pos, end, path) in enumerate(hits):
        lo = hits[k - 1][1] if k else 0
        hi = hits[k + 1][0] if k + 1 < len(hits) else n
        stem = path.rsplit("/", 1)[-1]
        name = _find_name(manifest, lo, hi, stem)
        if name:
            out.append({"path": path + "." + name.rsplit(".", 1)[1], "name": name})
        else:
            out.append({"path": path, "name": stem})
    return out


def extract_paths(manifest: bytes):
    return [m["path"] for m in extract_media(manifest)]


def manifest_count(manifest: bytes) -> int:
    """清单开头的 u32LE 是这一页的记录数（目标机型 是 45），拿它判断有没有收全。"""
    if len(manifest) >= 4:
        v = int.from_bytes(manifest[:4], "little")
        if 1 <= v <= 255:
            return v
    return 0


# --------------------------------------------------------------------------------------
# 4. HTTP 下载（/v2?storage=&path=）
# --------------------------------------------------------------------------------------
def http_open(req, timeout: float = 20):
    """强制不走系统代理：连相机热点时系统里多半挂着 Clash/Surge，走代理必失败。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def media_url(ip: str, store: int, path: str) -> str:
    return f"http://{ip}/v2?storage={store}&path={urllib.parse.quote(path, safe='/')}"


def http_headers(ip: str, store: int, path: str, method: str = "HEAD", range_from: int = 0):
    req = urllib.request.Request(media_url(ip, store, path), method=method)
    req.add_header("User-Agent", "CamLink/1.0")
    if range_from > 0:
        req.add_header("Range", f"bytes={range_from}-")
    return req


def head_detail(ip: str, store: int, path: str):
    """返回 (status, content_length, headers)；status=-1 表示连接层就失败了。"""
    req = http_headers(ip, store, path, "HEAD")
    try:
        with http_open(req, timeout=5) as r:
            return r.getcode(), int(r.headers.get("Content-Length") or 0), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, 0, dict(e.headers or {})
    except Exception as e:
        return -1, 0, {"error": f"{type(e).__name__}: {e}"}


def head_size(ip: str, store: int, path: str) -> int:
    try:
        with http_open(http_headers(ip, store, path, "HEAD"), timeout=5) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def download_file(ip: str, store: int, path: str, dest: str, retries: int = 12,
                  progress=None, should_stop=None, on_retry=None) -> bool:
    """下载单个文件。progress(done, total) 用于 UI；should_stop() 返回 True 时中断（保留 .part 便于续传）。"""
    quiet = progress is not None
    total = head_size(ip, store, path)
    part = dest + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    barren = 0
    for attempt in range(1, retries + 1):
        if should_stop and should_stop():
            return False
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if total and have >= total:
            break
        try:
            req = http_headers(ip, store, path, "GET", range_from=have)
            with http_open(req, timeout=12) as r:
                code = r.getcode()
                if have > 0 and code != 206:
                    # 相机忽略了 Range：只能从头来
                    have = 0
                    open(part, "wb").close()
                mode = "ab" if have > 0 else "wb"
                written = have
                with open(part, mode) as f:
                    while True:
                        if should_stop and should_stop():
                            return False
                        block = r.read(1024 * 1024)
                        if not block:
                            break
                        f.write(block)
                        written += len(block)
                        barren = 0
                        if progress:
                            progress(written, total)
                        elif not quiet:
                            pct = (written * 100 / total) if total else 0
                            print(f"\r  {os.path.basename(dest)}  {written/1048576:8.1f} MB"
                                  + (f" / {total/1048576:.1f} MB ({pct:5.1f}%)" if total else ""),
                                  end="", flush=True)
            if not quiet:
                print()
            if not total or os.path.getsize(part) >= total:
                break
        except Exception as e:
            barren += 1
            left = os.path.getsize(part) if os.path.exists(part) else 0
            if on_retry:
                on_retry(left, str(e))
            if not quiet:
                print(f"\n  ! 传输中断 ({e})，已收 {left/1048576:.1f} MB，{0.75*barren:.2f}s 后续传")
            if barren >= 6:
                return False
            if should_stop and should_stop():
                return False
            time.sleep(0.75 * barren)

    if not os.path.exists(part):
        return False
    size = os.path.getsize(part)
    if total and size < total:
        return False
    os.replace(part, dest)
    return True


# --------------------------------------------------------------------------------------
# 5. CLI
# --------------------------------------------------------------------------------------
def cmd_probe(args):
    cam = Camera(args.ip, args.port)
    cam.connect()
    cam.register()
    print(f"session=0x{cam.link.session_id:04x} base=0x{cam.link.base_seq:04x} "
          f"channel=0x{cam.link.cam_channel:04x} udp/{cam.link.port}")
    cam.get_version()
    cam.close()


def cmd_list(args):
    cam = Camera(args.ip, args.port)
    cam.connect()
    cam.register()
    if not args.no_playback:
        cam.enter_playback()
    files = cam.list_media(dump_manifest=args.dump_manifest)
    cam.close()
    if not files:
        print("没有拿到文件列表：确认相机里有素材，或在相机上确认『进入回放』")
        return 1
    print(f"共 {len(files)} 个文件（最新一页）")
    for i, f in enumerate(files):
        extra = ""
        if args.sizes:
            size = head_size(args.ip, f["store"], f["path"])
            extra = f" {size/1048576:8.1f} MB"
        print(f"  [{i:2d}] {f['name']:<44} storage={f['store']}{extra}")
    return 0


def cmd_download(args):
    cam = Camera(args.ip, args.port)
    cam.connect()
    cam.register()
    if not args.no_playback:
        cam.enter_playback()
    files = cam.list_media()
    cam.close()
    if not files:
        print("没有拿到文件列表")
        return 1

    picks = files if args.all else [files[i] for i in args.index if i < len(files)]
    if not picks:
        print("索引越界")
        return 1
    for f in picks:
        print(f"→ {f['path']} (storage={f['store']})")
        download_file(args.ip, f["store"], f["path"], os.path.join(args.out, f["name"]))
    return 0


def self_test():
    ok = True
    # CRC 参考：实测报文里的 0x00/0x26 请求帧
    sample = bytes.fromhex(
        "553704f9020100a04000264a002a10010000000000010000002d000d0100"
        "ffffffffffffffff0001000000000000000000000000008185")
    if crc8(sample[:3]) != sample[3]:
        print("CRC8 失败"); ok = False
    if crc16(sample[:-2]) != struct.unpack_from("<H", sample, len(sample) - 2)[0]:
        print("CRC16 失败"); ok = False
    frames = list(iter_build_frames(sample))
    if len(frames) != 1 or frames[0][0] != 0x00 or frames[0][1] != 0x26:
        print("解帧失败"); ok = False
    # 组帧回环
    f = build_frame(0x0102, 0xA000, 0x40, 0x00, 0x26, bytes(sample[11:-2]))
    if f != sample:
        print("组帧与样例不一致"); ok = False
    # 清单里抽路径
    m = bytes.fromhex("00000000") + bytes.fromhex("1a1f00000001") + b"DCIM/DIR_001/TEST_20240101120000_0001_D.MP4"[:25]
    if not extract_paths(m):
        print("路径解析失败"); ok = False
    print("自检通过 ✓" if ok else "自检失败 ✗")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="相机 Wi-Fi 取素材 demo")
    p.add_argument("--ip", default="192.168.2.1")
    p.add_argument("--port", type=int, default=9004)
    p.add_argument("--self-test", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("probe", help="握手并打印链路信息")
    sp.set_defaults(func=cmd_probe)

    sl = sub.add_parser("list", help="列出最新一页素材")
    sl.add_argument("--no-playback", action="store_true", help="跳过进入回放")
    sl.add_argument("--sizes", action="store_true", help="对每个文件发 HEAD 取大小（慢）")
    sl.add_argument("--dump-manifest", default=None, help="把原始清单存盘，便于离线分析")
    sl.set_defaults(func=cmd_list)

    sd = sub.add_parser("download", help="下载素材")
    sd.add_argument("--index", type=int, nargs="*", default=[])
    sd.add_argument("--all", action="store_true")
    sd.add_argument("-o", "--out", default="./videos")
    sd.add_argument("--no-playback", action="store_true")
    sd.set_defaults(func=cmd_download)

    args = p.parse_args()
    if args.self_test:
        return self_test()
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
