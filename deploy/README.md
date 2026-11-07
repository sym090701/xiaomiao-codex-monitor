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
服务源码应由 `xiaomiao-quota:xiaomiao-quota` 持有、权限 `0640`；若以 root
替换 `/opt/xiaomiao-quota/*.py`，替换后必须恢复这个属主和权限，否则 systemd
会因 `ProtectHome`/服务用户无读权限而反复重启。

小喵现有固件无需更改协议：在设备配网页填写服务器地址 `192.168.1.153`，
端口 `8765`。它读取 `/ws`；`/health` 和 `/usage` 用于诊断。
服务器模式的性能页从 Mac agent 接收真实 Mac 指标。
配置上报令牌与自动启动步骤见 [配置指南](../docs/CONFIGURATION.md)；
没有 agent 或 45 秒未收到上报时显示 Mac 离线，不使用服务器负载代替 Mac 性能。
Mac 休眠不会影响服务器继续查询，但服务器本身必须保持开机和联网。

安全默认：Mac companion `server.sh` 默认只绑定 `127.0.0.1`；需要让小喵访问时必须显式设置 `HOST=Mac局域网IP`，并配合防火墙或反向代理认证。Hook 仅接受 `CODEX_HOME` 下的 transcript，活动会话和单次读取字节数有限制。预热失败后会进入 30 分钟熔断；NAS 趋势服务使用 15 秒缓存、最多 4 个并发读取，繁忙时返回 429。

## 自动预热

服务模板以 `PREHEAT_AUTO_MODE=armed` 启用历史预测预热。默认安全提前量为 3.5 小时；当至少 3 个完整历史会话测出“开始使用到明显消耗”的持续时间时，系统会动态倒推提前量，目标是在预计用尽前约 15 分钟刷新窗口。样本不足时回退到 3.5 小时。

预测由 Ubuntu 上的 `usage_predictor.py` 计算，读取 `CODEXLOGS_LOCAL_DIR` 的原始归档；这些是同步到 NAS 的同一份数据。本版不是在 NAS 上执行预测，NAS 的折线图计算服务保持独立。读取放在线程中执行，每 5 分钟更新一次预测，不阻塞实时用量查询。部署时需同时安装 `quota_server.py`、`machw_archive.py`、`usage_predictor.py`。

学习规则：

1. 回看最近 28 天（`Asia/Shanghai`），识别相邻有效采样中的额度下降；间隔超过 15 分钟不推测中间发生的活动。
2. 空闲至少 1 小时后的下降视为新会话，每个历史日期只采用首次观测到的开始时间；平坦数据不作为使用证据，当天未结束的数据暂不进入训练。
3. 优先同一星期几，样本不足时按工作日/周末汇总。至少 3 个不同日期的开始时间聚集在一个 90 分钟时段内，并占该组使用日的至少 60%，才取该簇中位数预测今天或明天的使用时间。
4. 预热产生的归档标记为 `record_type=codex_preheat`，并保存尝试时间；其前后 10 分钟的下降不参与学习，避免学习自己的自动请求。升级前无标记的旧数据只能利用状态文件保留的最近一次预热时间排除，无法精确还原所有旧预热。
5. 完整会话必须在 5 小时窗口内连续采样并累计下降约 90 个百分点；动态提前量限制在 1～4 小时，按 5 分钟取整。长时间平线不计入瞬时消耗斜率，避免零斜率把小时消耗速度拉低。

执行规则：当前快照须新鲜，5 小时窗口须不存在或已到期，冷却须结束。后台每 30 秒检查，到预测时间减动态提前量时执行；最多允许延迟 10 分钟，超过后跳过，不在快开用时补发。每次预测只尝试一次，每天最多尝试一次；失败后所有来源进入默认 30 分钟熔断，自动任务另有一次延迟重试，避免反复启动真实 Codex turn。没有足够历史、习惯分散或采样断档时不乱猜，回退到 3.5 小时安全值。

参数可通过 systemd 覆盖，修改后执行 `systemctl daemon-reload && systemctl restart xiaomiao-quota`：

```ini
Environment=PREHEAT_AUTO_MODE=armed
Environment=PREHEAT_AUTO_LEAD_SECONDS=12600
Environment=PREHEAT_AUTO_HISTORY_DAYS=28
Environment=PREHEAT_AUTO_MIN_EVENTS=3
Environment=PREHEAT_AUTO_RETRY_SECONDS=1800
Environment=PREHEAT_AUTO_DAILY_LIMIT=1
```

`armed` 表示允许真实请求，`off` 表示仅保留手动长按 B。状态接口 `/api/preheat/status` 保持原有鉴权，新增 `auto_lead_hours`、`auto_lead_basis`、`auto_duration_hours`、`auto_prediction_local`、`auto_due_local`、`preheat_phase`、`preheat_last_phase`、`auto_retry_count`；`auto_lead_basis=historical_duration` 表示动态倒推，`safe_default` 表示回退 3.5 小时。turn 等待默认 180 秒，可由 `PREHEAT_TURN_TIMEOUT_SECONDS` 覆盖。超时错误会记录为 `TimeoutError@thread_resume`、`TimeoutError@turn_wait` 等阶段标记；同一预测只允许一次 30 分钟后的延迟重试，避免无限重复请求。

该估计只预测首次观测到的日常使用时段，不识别具体设备或对话；百分比粒度、长时间连续使用和历史缺测会影响预测。`completed` 表示专用对话 turn 完成，不保证上游额度窗口一定按预期建立。若上游确实从预热起算 5 小时，那么提前 3.5 小时会使预计开用时距该窗口结束约剩 1.5 小时，不会增加总额度。

## Codex 用量趋势归档

Codex 用量成功查询后，会另外追加到本地 `codexuselogs-spool/YYYY-MM-DD.ndjson`，
并同步到 NAS 的 `/vol3/1000/512/codexuselogs/`。新记录使用 `schema_version=2`，
文件按 `Asia/Shanghai` 本地日期分区；每行包含 UTC 与本地时间、星期、周末标记、
30 分钟槽位、`night/morning/afternoon/evening` 时段桶，以及 5 小时和周窗口的
原始快照，适合长期按日、按星期和按时段分析。查询失败不会写入伪造数据。
仓库提供 `scripts/migrate_archive_v1.py`，用于把早期没有时间维度的 JSONL 文件
补齐到 v2；迁移会先生成 `.schema1.bak`，再原子替换原文件。

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
