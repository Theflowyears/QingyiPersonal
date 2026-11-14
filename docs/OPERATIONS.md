# 日常运维

## 状态与日志

```bash
systemctl status qq-openclaw-bridge --no-pager -l
journalctl -u qq-openclaw-bridge -n 100 --no-pager
systemctl list-timers --all | grep -E 'openclaw|heartbeat'
```

同一台主机上跑 launcher 的部署（本仓库当前形态）服务名不同，且额外有两个定时器：

```bash
systemctl status qqbot --no-pager -l          # launcher + bot + web
tail -100 /opt/qqbot/logs/Bot.log             # launcher 侧日志（systemd 还额外写 data/bot_out.log）
systemctl list-timers --all | grep -E 'qqbot-(watchdog|probe)'
cd /opt/qqbot && ./venv/bin/python bot/watchdog.py status
cd /opt/qqbot && DATA_DIR=/opt/qqbot/data ./venv/bin/python bot/login_probe.py status
```

`qqbot.service` 用 `User=root` 运行，因为面板要在请求里执行 `docker inspect/cp/logs` 与 `systemctl restart`（自愈与 NapCat 二维码功能依赖它）。已启用 `NoNewPrivileges=yes` 与 `UMask=0077`；未启用 `PrivateTmp` —— `docker cp` 取二维码写的是宿主 `/tmp`，开了私有 /tmp 会让该接口读不到文件。

## 入站面守卫

Docker 发布端口绕过 ufw 的 INPUT 链，所以只删 `ufw allow` 不够。本部署用 `/usr/local/sbin/qqbot-netguard.sh` 在 `DOCKER-USER` 链按外部网卡 DROP `3001/6099`，loopback 不受影响，因此 `ONEBOT_ACCESS_TOKEN` 留空也能跑；该脚本由 `docker.service` 的 `ExecStartPost` 重复施加，docker 重启后规则不会丢。复核：

```bash
iptables -S DOCKER-USER          # 应看到两条 -i <eth> --dport 3001/6099 -j DROP
ufw status numbered              # 不应再有 3001/6099/5000
curl -m 5 -o /dev/null -w '%{http_code}\n' http://<公网IP>:5000/admin/login   # 期望超时/拒绝
```

Flask 面板只绑 `WEB_HOST=127.0.0.1`，外网一律走域名。若要把 OneBot token 也补上，需要改 NapCat 配置并重启容器，而重启容器可能触发重新扫码 —— 属于要择时、有人守着手机才能做的动作。

## 日志轮转

`/etc/logrotate.d/qqbot` 覆盖 `logs/*.log` 与 `data/bot_out.log`、`data/bot_err.log`，用 `copytruncate`（进程持有追加式文件句柄，不能改名重建），20 MB 触发、保留 14 份。

## 更新

1. 记录当前 commit 和服务状态。
2. 备份代码目录、systemd 单元和环境文件；数据库与状态文件单独备份。
3. 在临时目录拉取新版本并运行 `pytest -q`。
4. 使用安装器更新，它会再次生成代码备份。
5. `python3 -m compileall -q` 后重启服务并检查日志。

日常增量同步走 `sync_to_cloud.py`：它只碰 `bot/`、`website/`、`launcher.py`，永不覆盖云端 `config.env` 与 `data/`，且只重启 `qqbot`，不重启 NapCat 容器。`--dry-run` 只比 MD5，不改动服务器。

## 健康判断

- Bridge `active` 且没有连续重启。
- NapCat 登录、OneBot API 与 OpenClaw TCP 可达。
- 心跳新鲜；详细遥测允许短暂落后。
- 模型异常率、平均延迟、队列和磁盘没有持续恶化。
- 黑名单启用状态正确，具体 ID 不出现在网站或日志导出中。

## 回滚

停止服务，把当前目录改名保留，然后从安装器输出的备份目录恢复。恢复后先编译，再启动，并验证原状态文件和数据库仍在。不要用空目录覆盖状态数据。
