# 小喵独立应用与局域网 OTA

独立版占用整机，只运行 Codex Monitor，不与 Launcher 共存。4 MB Flash 分为 NVS、OTA 启动记录和两个 `0x1f0000` 字节的应用槽。第一次安装必须经 USB 写入匹配的启动程序和分区表；现有 Launcher 应用 BIN 不能通过这里的 OTA 安装。

## 编译与密码

```bash
python3 scripts/build_xiaomiao.py --standalone --configured
```

Wi-Fi 与服务端配置来自本地 `device.local.json`。首次构建自动生成 `ota.local.json`，用户名固定为 `admin`，密码随机生成。该文件、编译产生的头文件及预配置 BIN 均为私密资料，不要上传 GitHub。产物 `dist-private/XiaoMiao-Standalone-OTA-PRIVATE.bin` 是应用 BIN，用于 OTA，不能直接刷到整机地址 `0x0`。

长按 A+B 约 1.5 秒可显示 OTA 地址和密码，按 A 关闭。浏览器访问 `http://codexusage.local/ota`，使用上述账号密码登录并上传独立版应用 BIN。

## 供局域网内其他 Codex 使用

设备发布 `_xiaomiao-ota._tcp.local.` 服务，TXT 中提供设备类型、路径、认证方式和协议版本，不广播密码。其他电脑获得源码和 OTA 密码后，无需 Wi-Fi 编译配置即可发现和更新设备：

```bash
python3 -m pip install requests zeroconf
python3 scripts/ota_upload.py --discover
python3 scripts/ota_upload.py --host codexusage.local \
  --password-file /path/to/private-ota.json --image /path/to/firmware.bin
```

私密密码文件的格式为 `{"username":"admin","password":"你的OTA密码"}`。修改权限为仅自己可读。多台设备或同名主机时，使用发现结果中的实际主机名或 IPv4 地址。mDNS 需要处于同一支持组播的局域网；路由器隔离或跨子网时可改用可达的 IP。

上传脚本先检查 ESP32 镜像、校验和、镜像 SHA-256 与本项目标记；设备在非运行槽逐段擦写、校验后才切换启动槽。脚本在重启后核对槽位变化及运行镜像 SHA-256。只有出现 `OTA verified` 才表示这套端到端验证通过。双槽不代表支持启动失败自动回滚。

OTA 使用 HTTP Digest 密码认证，HTTP 本身不加密，项目标记也不是数字签名。仅用于可信局域网，不对公网开放。首次升级及显示方向、颜色仍需实机检查；构建通过不等于已刷入或实机验证通过。
