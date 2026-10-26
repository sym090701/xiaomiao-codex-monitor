# 两种固件配置方式

| 方式 | 编译命令 | 输出 | 适合 |
|---|---|---|---|
| 网页配网 | `python3 scripts/build_xiaomiao.py` | `dist/XiaoMiao-Portal-Launcher.bin` | 通用安装、首次开机网页设置 |
| 编译预配置 | `python3 scripts/build_xiaomiao.py --configured` | `dist-private/XiaoMiao-Preconfigured-PRIVATE.bin` | 个人预填 Wi-Fi 和服务器，安装后自动连接 |

两者使用同一套显示、按键和 Launcher 安装流程。预配置方式也保留网页设置入口。
需要 PlatformIO；脚本会尝试 PATH 和 `~/.platformio/penv/bin/platformio`。

## 方便的交互配置

```sh
python3 scripts/build_xiaomiao.py --configure
```

按提示填 Wi-Fi、可选备用 Wi-Fi、服务器地址和端口。密码输入不回显。
脚本会创建仅本机保存的 `device.local.json` 并构建预配置固件。
以后改完该文件，只运行 `--configured` 即可重新编译。

## 直接改文件

复制 `device.example.json` 为 `device.local.json`，填写 `wifi_networks` 数组（1–5 个网络）、
`server_host`、`server_port`。服务器填 IPv4 地址或主机名，不带 `http://` 或路径。
`device.local.json`、生成头文件、私有固件均被 Git 忽略。

固件首先尝试第一个 Wi-Fi；连接失败时每 15 秒轮换下一个。
这属于 Wi-Fi 连接回退，不会因服务器不通自动切换网络。子路由器需允许访问服务器所在网段。

首次运行预配置固件时，网络配置写入 NVS；重新编译且网络配置发生变化时，会更新 NVS。
相同配置再次启动不覆盖网页改动。网页保存一个不在编译列表内的 SSID 后，使用网页的网络。

私有 BIN 内含 Wi-Fi 密码，不可作为 GitHub Release 附件或公开分享。
CI 只使用公开模板里的虚构 Wi-Fi 值验证预配置构建，不使用个人配置。

## Mac 状态通过服务器显示

```text
Codex 额度 ← Ubuntu Codex 登录 → Ubuntu 缓存 → 小喵
Mac CPU/内存/磁盘/网络 → Mac agent（每 10 秒）→ Ubuntu 缓存 → 小喵
```

服务器无法直接测量休眠 Mac 的 CPU/内存；`mac_agent.py` 在 Mac 清醒时上报这些指标。
45 秒没有新上报，性能页显示离线，Codex 额度仍由 Ubuntu 持续采集。
目前上报性能指标和主机名，不上报聊天内容或 Codex 登录信息，也不提供硬件温度传感器数据。

在 Mac 项目目录准备独立的上报令牌：

```sh
.venv/bin/python mac_agent.py --setup http://192.168.1.153:8765
```

本机会生成 `mac-agent.local.json` 和 `mac-agent.token`（权限 600，均被 Git 忽略）。
通过 SSH 安全传输同一 token 到服务器 `/var/lib/xiaomiao-quota/mac-agent.token`，
设置为服务用户可读、权限 600。不要把 token 放进 Git、命令参数、网页地址或公开日志。
升级服务代码并安装新 systemd 单元后，重启 `xiaomiao-quota`。

```sh
.venv/bin/python mac_agent.py --once
.venv/bin/python mac_agent.py --install
```

第二条将 Mac agent 安装为当前用户登录后启动的 LaunchAgent。
无需保持终端窗口，但 Mac 真正休眠时无法上报。
日志在 `~/Library/Logs/XiaoMiao/`。卸载自动启动：

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/io.github.xiaomiao.mac-agent.plist
```

删除该 plist 才会取消下次登录加载。Mac 旧的独立 Codex 服务可以保留；
小喵连接 Ubuntu 时，Mac agent 才是性能上报者。
服务器读接口仍限可信局域网；上报认证不等于公网部署的完整安全方案。
