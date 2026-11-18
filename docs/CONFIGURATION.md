# 两种固件配置方式

| 方式 | 编译命令 | 输出 | 适合 |
|---|---|---|---|
| 网页配网 | `python3 scripts/build_xiaomiao.py` | `dist/XiaoMiao-Portal-Launcher.bin` | 通用安装、首次开机网页设置 |
| 编译预配置 | `python3 scripts/build_xiaomiao.py --configured` | `dist-private/XiaoMiao-Preconfigured-PRIVATE.bin` | 个人预填 Wi-Fi 和服务器，安装后自动连接 |

两者使用同一套显示、按键和 Launcher 安装流程。预配置方式也保留网页设置入口；出于安全原因，网页配置只允许从设备自己的 `192.168.4.x` 设置热点访问，接入家庭 LAN 后的同一页面会返回 403。
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

### 家外中继配置

配置生成器支持在私有 `device.local.json` 中增加 `home_relay` 对象：`host`、`port`、`ca_pem`、`read_token`、`device_id`、`device_secret` 和 `home_ssids`。读取令牌与命令密钥必须不同，设备 ID 使用 1–64 位字母、数字、下划线或短横线。CA 需为 PEM 格式；只有确定实际 HTTPS 证书链后才能配置。

预配置小喵固件按当前连接的 SSID 选择通道：`home_ssids` 内仍走原有局域网，其他 Wi-Fi 使用 Relay HTTPS 及 CA 证书验证。家外模式只显示 Relay 的新鲜配额与 Mac 在线状态，当前 Relay 不提供趋势或 Mac 性能详情，不显示旧值作为实时信息。数据过期、设备时钟不可信或 TLS/网络错误时，配额显示不可用。

预热长按 B 发起前会将随机请求 ID 写入设备 NVS；提交成功只表示待处理，设备轮询最终状态。结果不明确或查询返回 404 时保留 ID，阻止自动重发和本地手动预热，直到已确定终态或 Relay 记录管理员已核查。受服务端远程预热开关与家庭端 guard 限制；`completed` 不是新额度窗口的证明。设备对时使用 SNTP，网络环境需允许访问时间服务器；时间不对会拒绝发送或因 TLS 校验失败而拒绝连接。

可用 `scripts/prepare_relay_config.py` 从既有私有设备 JSON、Relay 私有环境文件与 CA 证书合并产生仅本机的配置，再以 `XM_DEVICE_CONFIG_FILE` 指定文件构建。不要将配置、生成头文件或包含凭据的固件公开；编译进固件的凭据可以通过固件读取，应为设备设置独立可撤销的凭据。SSID 仅用于选择网络路径，不是认证依据。HTTPS 服务端证书轮换时须检查设备内信任锚是否仍然有效。

固件实现已编译，家外实际网络和真机预热效果须另行验证；不要将状态接口在线视为设备端端到端验证。

固件首先尝试第一个 Wi-Fi；连接失败时每 15 秒轮换下一个。
这属于 Wi-Fi 连接回退，不会因服务器不通自动切换网络。子路由器需允许访问服务器所在网段。

首次运行预配置固件时，网络配置写入 NVS；重新编译且网络配置发生变化时，会更新 NVS。
相同配置再次启动不覆盖网页改动。网页保存一个不在编译列表内的 SSID 后，普通固件使用网页网络；家外中继预配置固件在连接失败时会轮换编译列表和网页网络，方便返回家后自动连接家中 Wi-Fi。已连接某个网络时不会主动抢占切换。

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
