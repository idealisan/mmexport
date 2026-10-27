# 相机 Wi-Fi 取素材 demo

纯 Python 标准库，不需要官方 SDK/App，也不需要抓包。

## 一条命令跑完

相机开机 → 屏幕下滑 → 设置 → 无线连接（打开开关）→ Wi-Fi 频段选 **2.4G** → 在「无线信息」里看 SSID/密码。
Mac 连上这个热点（会断网，正常），然后：

```bash
cd demo
python3 run_test.py
```

它会依次跑：环境检查 → TCP7001 戳醒 + UDP 握手 → 注册 + 读固件版本 → 进回放 → 取列表 → HTTP HEAD → 下载 1 MB 验证。
全程约 30–60 秒，**不需要你中途操作**。

跑完在 `test-run-<时间>/` 里留了：

| 文件 | 内容 |
|---|---|
| `summary.txt` | 每个阶段的 PASS/FAIL + 关键参数（直接贴这个就行） |
| `run.log` | 全量日志，含每一条下发命令 `→ 00/26 len=42` |
| `udp.bin` | 收到的所有 UDP 包（`[ms:u64][len:u32][data]`），可离线复现 |
| `manifest.bin` | 拼好的 CompositePack 清单原始字节 |
| `*.MP4` | 验证用的那一小段（默认只取 1 MB） |

想完整下一条：`python3 run_test.py --full-download`
只想确认网通没通：`python3 run_test.py --check-only`

## 各阶段失败说明

| 阶段 | 失败含义 | 怎么办 |
|---|---|---|
| 0. 环境检查 | 本机不在 `192.168.2.x`，或 192.168.2.1 完全无响应 | 相机「无线连接」没开 / 频段 5.8G 扫不到 / 被手机 App 占着。开 2.4G，重启相机 Wi-Fi |
| 1. 握手 | UDP 9004、10004 都没回 | 相机没进 Wi-Fi 待连状态；手机先断开；等相机 AP 起来 10–15 s 再跑 |
| 2. 注册 | 收到包但版本串解析不出 | 一般不影响后面，继续看列表 |
| 3. 回放 | 只是发送序列，不会失败 | 若 4 阶段空，就是这个没生效：相机上手动进一次回放再跑 |
| 4. 列表 0 条 | 没进回放（回 `d8`）或卡里真没素材 | 手动进回放；或换一张有素材的卡 |
| 5. HEAD 0 字节 | 路径对但 HTTP 取不到 | 检查 `storage`：目标机型是 0；也可能是相机在读卡 |
| 6. 下载 | 断流 / 404 | 脚本本身会 Range 续传；连续 404 说明路径不对 |

## 它到底做了什么

```
TCP 7001  ── 配对请求（SetPairingPIN）          把相机 UDP 数据面戳醒
UDP 9004  ── 握手（pktType 0x00）             学 camera_channel，seq = channel + 8
          ── 0x00/0x81 / 0x00/0x88 / 0x03/0xDA   注册
          ── 0x01/0x01 × N                      目标机型进回放（0x02/0x0c 会被 e0 拒绝）
          ── 0x00/0x26 → 0x00/0x27 × N          取清单，剥掉 10 字节子头再拼
          ── TLV 1a 00 00 00 01 "DCIM/..."      拿到媒体路径
HTTP      ── GET /v2?storage=0&path=...        拉文件，Range 续传（强制不走系统代理）
```

## 单独用

```bash
python3 cam_download.py --self-test              # 离线自检（CRC/组帧，不需要设备）
python3 test_loopback.py                                 # 假相机回环测试
python3 cam_download.py probe                    # 只握手 + 读版本
python3 cam_download.py list --sizes             # 列最新 45 条
python3 cam_download.py download --all -o ./videos
```

## 已知限制

- 只取**最新一页**（45 条）。全量分页要用 handle 游标，demo 没做。
- 只解析媒体路径，不解析删除/收藏用的 handle（避免误删）。
- 长传输偶发 404/500 是相机固件行为，脚本会退避续传。
- 连相机热点时没有外网，属正常。
