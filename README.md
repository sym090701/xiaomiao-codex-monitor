# 小喵 Codex 用量显示器

把 Codex 的 5 小时与每周用量、重置倒计时显示在学而思小喵掌机上；A 键可切换到 Mac 性能页。Codex 登录信息留在 Mac，本项目固件不保存账号令牌。

[English](README.en.md) · [项目来源与许可说明](UPSTREAM.md)

## 状态

- 目标设备：原版学而思小喵，ESP32-D0WD，4 MB Flash，ST7735 160 × 128 屏幕。
- 已完成：固件可编译；电脑端数据解析与状态测试通过；Launcher 应用镜像约 1 MiB。
- 当前状态：已完成小喵实机启动、Wi-Fi、WebSocket、NAS 趋势接口和 OTA 验证；屏幕观感、按键手感仍建议在不同网络和长时间运行下继续观察。
- 上游 Codex 用量接口不是公开稳定 API；Codex 更新后可能需要调整。

## 功能

提供 [网页配网与编译预配置两种方式](docs/CONFIGURATION.md)，支持多个备用 Wi-Fi。
另提供[独立双槽 OTA 与 mDNS 发现](docs/OTA.md)，首次安装需通过 USB 更换分区布局。
Ubuntu 可持续采集额度，Mac agent 将 Mac 性能上报至同一服务器；Mac 休眠后性能页显示离线，额度继续更新。

- 显示 Codex 5 小时、每周剩余百分比和重置倒计时。
- 两个页面在 Codex 停止时均隐藏右侧状态条；工作或等待授权时显示对应提示。
- A 键切换到下一页；B 键返回上一页；长按 A+B 约 1.5 秒，Launcher 版返回 Launcher，独立版显示 OTA 地址与密码。
- 趋势页使用专用按键：上/下切换 3、24 小时窗口，左/右移动曲线游标；不会再触发页面翻页。
- 电脑端提供配网页面和状态面板；设备与 Mac 通过 Wi-Fi 和 WebSocket 通信。
- 第一次配网时创建独立热点密码，并显示在小喵屏幕上。

小喵原机背光没有本移植可控制的 PWM 引脚，因此网页里的亮度设置不会调节掌机背光。

## NAS 趋势页

趋势页的数据由 NAS 读取归档文件后提供给小喵，默认接口为
`http://192.168.1.158:8788/api/codex-trend`。小喵不保存长期趋势，只保留当前显示所需的采样点。

NAS 负责：

- 按 6、12 或 24 小时窗口筛选归档数据；
- 排序并均匀降采样为最多 48 个点；
- 计算有效消耗斜率、ETA 和 5 小时重置位置。

ETA 不把长时间的“剩余量不变”平线算入消耗时间。它只累计相邻采样之间实际下降的区间：

```text
有效消耗速度 = 实际下降的总百分比 ÷ 实际下降区间的总时间（小时）
ETA = 当前剩余百分比 ÷ 有效消耗速度
```

如果窗口内没有足够的下降点，设备显示 `ETA --`，而不是给出虚假的线性预测。趋势页顶部的 `LIVE/OLD` 只表示小喵最近一次 HTTP 拉取 NAS 是否成功，不代表 NAS 归档数据的新旧程度。

### 趋势页按键

| 按键 | 作用 |
|---|---|
| A 短按 | 下一页 |
| B 短按 | 返回上一页 |
| 上/下（趋势页） | 切换 6/12/24 小时窗口 |
| 左/右（趋势页） | 移动曲线游标，读取对应时间点 |
| A+B 长按约 1.5 秒 | OTA/维护入口 |

## 硬件与按键

| 小喵信号 | ESP32 GPIO | 说明 |
|---|---:|---|
| TFT CS | 5 | ST7735 SPI 片选 |
| TFT DC | 4 | 数据/命令 |
| TFT RST | 19 | 屏幕复位 |
| TFT SCLK | 18 | SPI 时钟 |
| TFT MOSI | 23 | SPI 数据 |
| SD CS | 22 | 启动时拉高，释放共享 SPI 总线 |
| A 键 | 34 | 低电平有效 |
| B 键 | 12 | 低电平有效 |

目标是 ESP32 版小喵，不适用于 ESP32-C3 SuperMini 或其他 160 × 80 小屏项目。引脚来自本项目使用的小喵硬件配置；其他硬件修订请先核对。

## 安装到 Launcher

1. 用 PlatformIO 构建小喵固件，生成 `.pio/build/xiaomiao/firmware.bin`。
2. 把该应用镜像放到 TF 卡 `/boot/` 目录。
3. 在 Launcher 中选择该 BIN 安装；Launcher 需为它分配至少 `0x100000`（1 MiB，按 64 KiB 对齐）的空间。
4. 安装后运行不需要 TF 卡。没有 TF 卡时先不要把应用镜像直写到 Flash 地址 `0x0` 或 `0x10000`。

这只是构建产物，不代表已在实机验证启动。安装前确认 Launcher 的可用空间；Launcher 应用安装流程会管理分区。

## 编译固件

需要 PlatformIO Core 和网络连接以下载构建依赖：

```bash
python3 -m pip install platformio
pio run -e xiaomiao
```

产物为 `.pio/build/xiaomiao/firmware.bin`。生成脚本会在构建时把设备配网页嵌入固件，无需单独上传 LittleFS。

## 安装并运行 Mac 服务

希望 Mac 休眠后仍可查询用量，可以使用 [Ubuntu 常开采集服务](deploy/README.md)。
该模式通过 Codex App Server 查询，现有小喵固件可直接连接其 WebSocket 端口。

需要 macOS、Python 3.10 或更新版本，以及已经登录 Codex 的本机环境。服务端从 `~/.codex/auth.json` 读取登录令牌，只在 Mac 上调用 Codex 用量接口。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
HOST=192.168.1.20 PORT=8080 PYTHON_BIN="$PWD/.venv/bin/python" ./server.sh start
```

把 `192.168.1.20` 换成 Mac 当前的局域网 IP。这里显式绑定 Mac 的局域网地址，让小喵可以访问服务。Mac 和小喵需连接同一局域网，网络防火墙需允许该端口。

```bash
./server.sh status
./server.sh stop
```

Mac 浏览器打开 `http://Mac的局域网IP:8080` 查看状态面板（与上面的 HOST 一致）。首次交互式启动会询问是否安装 Codex Hook；Hook 是可选的，只用于显示当前 Codex 会话状态，不参与用量读取。检查或移除 Hook 可运行 `./server.sh hook-status` 或 `./server.sh uninstall-hook`。

服务接口没有设备认证。只在可信的家庭/个人局域网使用；不要绑定公网网卡、配置路由器端口转发或把 8080 暴露到互联网。不要把 `auth.json`、日志、运行配置或 Wi-Fi 凭据提交到仓库。

## 检查

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s test -v
pio run -e xiaomiao
```

离线测试覆盖用量窗口解析和 Hook 状态；CI 会运行这些测试并编译固件。CI 通过只能确认代码构建，不等于实机屏幕或网络验证。

## 来源

本项目的小喵适配基于 [evoking6/esp32-code-monitor](https://github.com/evoking6/esp32-code-monitor)，公开仓库使用独立提交历史。完整来源、基线和许可证状态见 [UPSTREAM.md](UPSTREAM.md)。
