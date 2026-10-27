import copy
import os
import sys
import threading
import time
import traceback

if getattr(sys, "frozen", False):
    ROOT = sys._MEIPASS                      # PyInstaller 解包目录
    WEB_DIR = os.path.join(ROOT, "web")
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    WEB_DIR = os.path.join(ROOT, "gui", "web")

sys.path.insert(0, os.path.join(ROOT, "demo"))
import cam_download as lib  # noqa: E402
import blepair  # noqa: E402
import wifijoin  # noqa: E402

DEFAULT_OUT = os.path.expanduser("~/Movies/CameraExport")


class Core:
    """单工作线程 + 轮询状态：UI 只读 snapshot()，所有耗时操作都在后台跑。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._busy = False
        self._cam = None
        self._state = {
            "phase": "idle",          # idle / connecting / listing / downloading / done / error
            "message": "未连接",
            "connected": False,
            "device": "",
            "ip": "192.168.2.1",
            "port": 9004,
            "files": [],
            "outdir": DEFAULT_OUT,
            "progress": {"done": 0, "total": 0, "speed": 0, "name": "", "index": 0, "count": 0},
            "log": [],
        }

    # ---------- state ----------
    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _update(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _set_progress(self, **kw):
        with self._lock:
            self._state["progress"].update(kw)

    def log(self, msg: str):
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            log = self._state["log"]
            log.append(f"{stamp}  {msg}")
            del log[:-300]
        print(f"[gui] {msg}", flush=True)

    def _fail(self, msg, exc=None):
        if exc is not None:
            self.log(f"错误: {exc}")
            self.log(traceback.format_exc(limit=2).strip().splitlines()[-1])
        self._update(phase="error", message=msg)

    def _begin(self, phase, message):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._cancel.clear()
        self._update(phase=phase, message=message)
        return True

    def _end(self):
        with self._lock:
            self._busy = False

    def _thread(self, fn, *args):
        t = threading.Thread(target=fn, args=args, daemon=True)
        t.start()
        return {"ok": True}

    # ---------- jobs ----------
    def start_connect(self, ip: str, port: int):
        if not self._begin("connecting", "正在连接..."):
            return {"ok": False, "error": "busy"}
        self._update(ip=ip, port=int(port), files=[], device="")
        self.log(f"连接 {ip}:{port}")
        return self._thread(self._job_connect, ip, int(port))

    def start_auto(self, ip: str, port: int):
        """蓝牙发现 → 读热点凭据 → 自动连 Wi-Fi → 建链路。"""
        if not self._begin("pairing", "扫描附近的相机（蓝牙）…"):
            return {"ok": False, "error": "busy"}
        self._update(ip=ip, port=int(port), files=[], device="")
        return self._thread(self._job_auto, ip, int(port))

    def start_refresh(self):
        if not self._begin("listing", "读取文件列表..."):
            return {"ok": False, "error": "busy"}
        return self._thread(self._job_list)

    def start_download(self, indices, outdir: str):
        if not self._begin("downloading", "准备下载..."):
            return {"ok": False, "error": "busy"}
        self._update(outdir=outdir)
        return self._thread(self._job_download, list(indices), outdir)

    def cancel(self):
        self._cancel.set()
        self.log("已请求取消")
        return {"ok": True}

    # ---------- internals ----------
    def _wifi_ip(self):
        try:
            dev = wifijoin.wifi_device()
            return wifijoin.ip_of(dev) if dev else ""
        except Exception:
            return ""

    def _start_keepalive(self):
        """下载期间每 2s 发一次 APP 心跳：相机 AP 空闲会休眠，长传输也更容易被掐。"""
        self._ka_stop = threading.Event()

        def loop():
            while not self._ka_stop.wait(2.0):
                cam = self._cam
                if not cam:
                    continue
                try:
                    cam.link.send_cmd(0x00, 0x88, lib.APP_PRESENCE, 0x08, 1)
                except Exception:
                    pass

        threading.Thread(target=loop, daemon=True).start()

    def _stop_keepalive(self):
        ev = getattr(self, "_ka_stop", None)
        if ev:
            ev.set()

    def _close_cam(self):
        try:
            if self._cam:
                self._cam.close()
        except Exception:
            pass
        self._cam = None

    def _ensure_cam(self):
        with self._lock:
            ip, port = self._state["ip"], self._state["port"]
        self._close_cam()
        cam = lib.Camera(ip, port, verbose=False)
        if not cam.open_link():
            raise RuntimeError("握手失败：确认电脑已连上相机 Wi-Fi")
        cam.register()
        version = cam.get_version()
        cam.enter_playback()
        self._cam = cam
        return version

    def _fetch_files(self):
        files = self._cam.list_media()
        with self._lock:
            ip = self._state["ip"]
        out = []
        for i, f in enumerate(files):
            if self._cancel.is_set():
                break
            size = lib.head_size(ip, f["store"], f["path"])
            out.append({"idx": i, "name": f["name"], "path": f["path"],
                        "store": f["store"], "size": size})
            if i % 10 == 0:
                self._update(message=f"读取文件信息 {i + 1}/{len(files)}")
        return out

    def _job_auto(self, ip, port):
        try:
            info = blepair.discover_and_pair(log=self.log)
            self._update(message=f"{info['name']} · 正在连接热点…")
            ok, detail = wifijoin.join(info["ssid"], info["password"], log=self.log)
            if not ok:
                raise RuntimeError(f"加入热点失败：{detail}")
            self._update(message="已加入热点，建立数据链路…", connected=False)
            version = None
            last = None
            for _ in range(3):
                try:
                    version = self._ensure_cam()
                    break
                except Exception as e:
                    last = e
                    self.log(f"建链失败，重试: {e}")
                    time.sleep(2)
            if version is None:
                raise last or RuntimeError("建链失败")
            self._update(connected=True,
                         device=version.replace("\x00", " | ") if version else "相机")
            files = self._fetch_files()
            self._update(files=files, phase="idle", message=f"已连接 · {len(files)} 个文件")
            self.log(f"自动连接完成：{len(files)} 个文件")
        except Exception as e:
            self._fail(f"自动连接失败：{e}", e)
        finally:
            self._end()

    def _job_connect(self, ip, port):
        try:
            version = self._ensure_cam()
            self._update(connected=True, device=version.replace("\x00", " | ") if version else "相机",
                         message="已连接，读取列表...")
            self.log(f"已连接 {version!r}")
            files = self._fetch_files()
            self._update(files=files, phase="idle",
                         message=f"已连接 · {len(files)} 个文件")
            self.log(f"列表完成：{len(files)} 个文件")
        except Exception as e:
            self._fail(f"连接失败：{e}", e)
        finally:
            self._end()

    def _job_list(self):
        try:
            if not self._cam:
                self.log("重新建立连接")
                version = self._ensure_cam()
                self._update(connected=True, device=version or "相机")
            files = self._fetch_files()
            self._update(files=files, phase="idle", message=f"{len(files)} 个文件")
        except Exception as e:
            self._fail(f"读取列表失败：{e}", e)
        finally:
            self._end()

    def _job_download(self, indices, outdir):
        try:
            with self._lock:
                files = list(self._state["files"])
                ip = self._state["ip"]
            queue = [files[i] for i in indices if 0 <= i < len(files)]
            if not queue:
                self._update(phase="idle", message="没有选中的文件")
                return
            os.makedirs(outdir, exist_ok=True)
            subnet = ip.rsplit(".", 1)[0] + "."
            self._start_keepalive()
            ok = 0
            for k, f in enumerate(queue):
                if self._cancel.is_set():
                    break
                now_ip = self._wifi_ip()
                if now_ip and not now_ip.startswith(subnet):
                    raise RuntimeError(f"电脑已离开相机热点（当前 {now_ip}），请重连后点下载继续（会续传）")
                dest = os.path.join(outdir, f["name"])
                if os.path.exists(dest) and (not f["size"] or os.path.getsize(dest) == f["size"]):
                    self.log(f"跳过（已存在）{f['name']}")
                    ok += 1
                    continue
                self._set_progress(done=0, total=f["size"], speed=0, name=f["name"],
                                   index=k + 1, count=len(queue))
                self._update(message=f"下载中 {k + 1}/{len(queue)} · {f['name']}")
                self.log(f"开始下载 {f['name']} ({f['size'] / 1048576:.1f} MB)")

                last = {"t": time.time(), "bytes": 0}
                def progress(done, total, _k=k, _f=f):
                    now = time.time()
                    if now - last["t"] >= 0.15:
                        speed = (done - last["bytes"]) / max(now - last["t"], 1e-6)
                        last["t"], last["bytes"] = now, done
                        self._set_progress(done=done, total=total or _f["size"], speed=speed,
                                           name=_f["name"], index=_k + 1, count=len(queue))

                last_log = {"mb": -1}
                def on_retry(done, err, _f=f):
                    self.log(f"{_f['name']} 中断于 {done/1048576:.1f} MB（{err[:60]}），正在续传")
                    cur = self._wifi_ip()
                    if cur and not cur.startswith(subnet):
                        self.log(f"网络已切走（{cur}），停止下载")
                        self._cancel.set()

                def progress2(done, total, _f=f):
                    progress(done, total)
                    mb = int(done / (16 * 1048576))
                    if mb != last_log["mb"]:
                        last_log["mb"] = mb
                        self.log(f"{_f['name']} {done/1048576:.1f} MB")

                got = lib.download_file(ip, f["store"], f["path"], dest,
                                        progress=progress2, should_stop=self._cancel.is_set,
                                        on_retry=on_retry)
                if got:
                    ok += 1
                    self.log(f"完成 {f['name']}")
                elif self._cancel.is_set():
                    self.log("已取消")
                    break
                else:
                    self.log(f"失败 {f['name']}（可重试，会断点续传）")
            msg = f"已取消，完成 {ok}/{len(queue)}" if self._cancel.is_set() else f"完成 {ok}/{len(queue)}"
            self._update(phase="idle", message=msg)
            self.log(msg)
        except Exception as e:
            self._fail(f"下载出错：{e}", e)
        finally:
            self._stop_keepalive()
            self._end()
