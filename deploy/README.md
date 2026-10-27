# Ubuntu 常开用量采集

`quota_server.py` 是独立 Linux 服务，不依赖 Mac、psutil、屏幕状态或会话日志。
它通过本机 Codex App Server 的 `account/rateLimits/read` 查询用量，
每 60 秒更新一次缓存，每 5 秒向小喵发送兼容的 WebSocket 快照。
仅识别实际窗口时长为 300 / 10080 分钟的额度；缺失窗口保持未知。
查询失败后标记过期，不把缓存冒充最新额度。

要求 Python 3.11+、Node.js 及 Codex CLI。Codex 在服务器上以服务用户独立登录，
不要复制 Mac 的 refresh token。设备登录若被账号组织限制，需在账号侧启用或改用交互登录。

部署路径为 `/opt/xiaomiao-quota`，状态目录为 `/var/lib/xiaomiao-quota`，
服务用户为 `xiaomiao-quota`。安装 aiohttp 和 `@openai/codex` 后，使用：

```sh
sudo -u xiaomiao-quota env HOME=/var/lib/xiaomiao-quota \
  CODEX_HOME=/var/lib/xiaomiao-quota/.codex \
  /opt/xiaomiao-quota/bin/codex login --device-auth
```

在自己的浏览器按提示完成授权。凭据保存在该服务用户私有目录。
部署时把 npm 包所带的本平台原生 Codex 二进制安装到 `/opt/xiaomiao-quota/bin/codex`，
避免依赖 root 私有目录里的 Node 运行时。
完成后安装 `xiaomiao-quota.service` 至 `/etc/systemd/system/`，执行
`systemctl daemon-reload` 和 `systemctl enable --now xiaomiao-quota`。
模板绑定 `192.168.1.153:8765`，其他机器需修改地址。

小喵现有固件无需更改协议：在设备配网页填写服务器地址 `192.168.1.153`，
端口 `8765`。它读取 `/ws`；`/health` 和 `/usage` 用于诊断。
服务器模式的性能页从 Mac agent 接收真实 Mac 指标。
配置上报令牌与自动启动步骤见 [配置指南](../docs/CONFIGURATION.md)；
没有 agent 或 45 秒未收到上报时显示 Mac 离线，不使用服务器负载代替 Mac 性能。
Mac 休眠不会影响服务器继续查询，但服务器本身必须保持开机和联网。

## Mac 硬件数据归档

服务开启 `MACHWLOGS_LOCAL_DIR` 后，会把每次 Mac 上报追加到服务器本地
`mac/<主机名>/YYYY-MM-DD.ndjson`，并每分钟通过受限 SSH 密钥同步到 NAS 的
`/vol3/1000/512/machwlogs/mac/`。服务器本地队列是数据源；NAS 短暂离线时只会
积压，恢复后继续同步，不会阻塞小喵或改写 `512` 根目录权限。归档文件为 0600，
目录为 0700。密钥只允许从 `192.168.1.153` 登录 NAS 的 `scall0p`，并关闭转发和终端。

该版只用于可信私有局域网，接口无设备认证，不应转发至公网。
公网 VPS 需要另行加入 HTTPS、设备认证与证书校验后使用。
App Server 协议随 Codex 版本变化，升级前请运行本仓库测试。

停止：`systemctl stop xiaomiao-quota`；日志：`journalctl -u xiaomiao-quota`。
重新登录用同一服务用户执行上面的命令，随后重启服务。

官方接口文档：https://developers.openai.com/codex/app-server/
