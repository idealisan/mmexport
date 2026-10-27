import socket, struct, threading, time, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import cam_download as d

IP, PORT = "127.0.0.1", 19004
stop = False

def udp_hdr(total_len, session, seq, pkt_type):
    w0 = 0x8000 | total_len
    b = bytearray(struct.pack("<HHH", w0, session, seq)); b.append(pkt_type)
    x = 0
    for c in b: x ^= c
    b.append(x)
    return bytes(b)

def serve():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((IP, PORT)); s.settimeout(0.3)
    while not stop:
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            continue
        pkt_type = data[6] if len(data) > 6 else -1
        if pkt_type == 0x00:
            s.sendto(udp_hdr(8, 0x1234, 0xABCD, 0x00), addr)
        elif pkt_type == 0x05:
            # 找里面的 协议帧 命令
            i = data.find(b"\x55")
            if i < 0 or len(data) < i + 11: continue
            cs, ci = data[i+9], data[i+10]
            if (cs, ci) == (0x00, 0x26):
                payload = data[i+11:-2]
                ctr = payload[4]
                path = b"DCIM/DIR_001/TEST_0001_D.MP4"
                field = bytes([0x1a, len(path)+6, 0, 0, 0, 1]) + path
                body = struct.pack("<I", 1) + field
                sub = bytes([0x4a, 0x01, 0, 0, ctr, 0, 0, 0, 0, 0])
                frame = d.build_frame(0x0201, 0x0A00, 0xC0, 0x00, 0x27, sub + body)
                pkt = udp_hdr(8 + len(frame), 0x1234, 0xABCD, 0x02) + frame
                s.sendto(pkt, addr)
                ends = bytes([0x4a, 0x03, 0, 0, ctr, 0, 0, 0, 0, 0])
                ef = d.build_frame(0x0201, 0x0A01, 0xC0, 0x00, 0x27, ends)
                s.sendto(udp_hdr(8 + len(ef), 0x1234, 0xABCD, 0x02) + ef, addr)
        # 心跳（34B, type 1）用于学习通道
        hb = bytearray(34); hb[6] = 0x01
        struct.pack_into("<H", hb, 8, 0x2233)
        struct.pack_into("<H", hb, 10, 0x0010)
        struct.pack_into("<H", hb, 18, 0x0020)
        s.sendto(bytes(hb), addr)

t = threading.Thread(target=serve, daemon=True); t.start()
cam = d.Camera(IP, PORT, verbose=True)
cam.connect()
cam.register()
files = cam.list_media(timeout=5)
print("FILES:", files)
assert len(files) == 1 and files[0]["name"] == "TEST_0001_D.MP4", files
print("LOOPBACK TEST OK")
stop = True
