# 相机 Wi-Fi 素材下载工具

通过相机自带的热点，列出并下载原始视频/照片。纯 Python 标准库，不需要官方 SDK、不需要官方 App、不需要抓包。

## 快速开始

```bash
# 1. 相机：设置 > 无线连接（打开）> Wi-Fi 频段 2.4G，屏幕上会显示热点名和密码
# 2. 电脑连上该热点，确认拿到 192.168.2.x 地址
ipconfig getifaddr en0

cd demo
python3 run_test.py        # 环境 → 握手 → 注册 → 回放 → 列表 → HEAD → 下载 1MB
```

产物落在 `demo/test-run-<时间>/`：`summary.txt`、`run.log`、`udp.bin`、`manifest.bin`。

## 目录

| 路径 | 说明 |
|---|---|
| `demo/run_test.py` | 一键全流程自测，失败会停在对应阶段并给出排查提示 |
| `demo/cam_download.py` | 协议实现 + 单条命令（probe / list / download） |
| `demo/test_loopback.py` | 无设备自测：本机假相机跑通握手与列表 |

## 协议速览

| 环节 | 做法 |
|---|---|
| 连接 | 相机 AP `192.168.2.1`(WPA2) → TCP 7001 配对请求 → UDP 9004 握手 |
| 控制 | `[8B 传输头][12B 路由头][协议帧]`，CRC8/CRC16，`cmd_set/cmd_id` |
| 列表 | `0x00/0x26` 查询 → 多条 `0x00/0x27` 分片，剥 10B 子头拼接成 CompositePack |
| 路径 | 清单里 `1a … 01` 是不带扩展名的路径，文件名在同记录的 `0d` 字段 |
| 下载 | `GET /v2?storage=<n>&path=<完整路径>`，支持 HEAD / Range 断点续传 |

更多细节见 `demo/README.md`（阶段含义与排错表）。

## 图形界面

pywebview 开一个本地窗口（macOS 上是 WKWebView），Python 侧跑协议与下载，界面负责选片、看进度。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # 只有 pywebview
.venv/bin/python gui/app.py
```

功能：连接/重连、刷新列表、全选/多选、选输出目录、逐个下载并显示速度与剩余时间、随时取消（保留 .part，下次续传）、运行日志。

| 路径 | 说明 |
|---|---|
| `gui/app.py` | 窗口与 JS API |
| `gui/core.py` | 后台任务线程与状态机（UI 每 400ms 轮询） |
| `gui/web/index.html` | 界面（单文件，无前端依赖） |

> 连接相机热点期间电脑没有外网，属正常现象。

## 自动连接（蓝牙）

界面上的「蓝牙自动连接」会做完整链路，不需要手动连热点：

```
BLE 扫描（名字 Osmo…/Action… 或厂商字段） 
  → 连接 GATT 服务 fff0，写 fff5、订阅 fff4，向 fff4 写 01 00 武装配对
  → 0x07/0x45 SetPairingPIN(identifier, token)
       ├─ 回 00 01：之前配对过，直接过
       └─ 回 00 02：相机屏幕显示配对码，用户按确认 → 相机发 0x07/0x46，App 回 ACK
  → 0x07/0x07 / 0x07/0x0e 读热点 SSID 和密码（两条间隔 ~500ms）
  → 断开 BLE，用 networksetup 加入该热点，等 192.168.2.x
  → 进入原有的 UDP 链路流程
```

原版客户端也是这套：BLE 只做配对和发凭据，真正的数据全走 Wi-Fi（Android 用 `WifiNetworkSpecifier` + `bindProcessToNetwork`，iOS 用 `NEHotspotConfiguration`）。

注意：

- 需要系统蓝牙权限（系统设置 → 隐私与安全性 → 蓝牙，允许终端/iTerm）。
- 首次配对要在相机屏幕上确认；配对记录按 App 标识存在相机里，后续可直连。
- 加入热点期间会断网；退出后手动切回常用 Wi-Fi。
- 目前自动连热点只实现了 macOS（`networksetup`）；Windows/Linux 仍需手动连。

## 打包成 macOS App

```bash
./build_macos.sh          # 需要本机有 Python 3 + Xcode CLT
open dist/CameraExport.app
```

产物是 `dist/CameraExport.app`（约 24 MB，自带 Python 运行时，不依赖 .venv）。

- 首次打开若提示“未验证的开发者”，右键 → 打开，或 `xattr -dr com.apple.quarantine`。
- 已写入 `NSBluetoothAlwaysUsageDescription`，第一次用蓝牙会弹系统授权。
- 打包后没有终端，诊断输出写到 `~/Library/Logs/CameraExport.log`。
- 重新打包：改完代码直接再跑 `./build_macos.sh`（`build/`、`dist/` 已被 git 忽略）。

### 几个容易误会的地方

- 相机屏幕上**不会**出现“已连接 Mimo/App”的提示——我们不走官方 App，只用了相机的 Wi-Fi 热点。相机侧能看到的是热点被占用、传输中的存储灯。
- 下载中途变慢/停住：脚本会检测断流并自动 `Range` 续传（日志里能看到“中断于 X MB，正在续传”）。如果是一直不动，先看电脑是否切回了别的 Wi-Fi（状态栏），以及相机是否息屏/自动关机。
- 长时间导出建议：相机设置里关掉自动关机、保持屏幕点亮，频段固定在 2.4G。

## 说明

- 仅用于自己拥有的设备；连相机热点时电脑会断网，属正常现象。
- 仓库不放固件/大文件，也不包含刷机实现。
