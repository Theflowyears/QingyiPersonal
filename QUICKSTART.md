# 快速上手（QingyiPersonal · 公开白板版）

> 本仓库是**平台白板**：不含任何预设人格。角色卡（性格/形象/称呼）由部署方放在
> `DATA_DIR/personas/`，见 README「人格包约定」。下面只讲怎么把平台跑起来。

## 一条命令

```bash
git clone https://github.com/Theflowyears/QingyiPersonal.git
cd QingyiPersonal
python setup.py --napcat --start
```

`setup.py` 会依次：

1. 建 venv 并安装依赖（`requirements.txt`）
2. 生成 `config.env`：随机面板密钥 + 管理员凭据（**只打印一次，请记好**）；模型 API key 留空，交给面板填
3. `--napcat`：创建 NapCat 容器（**固定 MAC**，设备指纹稳定）。容器已存在时**绝不重启/重建**，保护 QQ 登录态
4. `--start`：后台拉起 `launcher.py`（bot + web 面板）

结束时打印面板地址与管理员凭据。常用开关：

| 开关 | 作用 |
|---|---|
| `--napcat` | 顺带创建 NapCat 容器（需 docker） |
| `--start` | 生成后后台启动 |
| `--no-venv` | 不建 venv，用当前解释器 |
| `--port N` | 面板端口（默认 5000） |
| `--admin-user / --admin-password` | 指定管理员凭据（默认随机密码） |
| `--mark-complete` | 直接标记初始化完成、跳过向导（生产式部署，向导置惰性） |

## 打开面板，走「快速配置向导」

浏览器打开 `http://127.0.0.1:5000/admin/login`，用 setup 打印的管理员凭据登录，
然后进入 **`/setup`** 向导，三步：

1. **模型 API** —— 填文本通道（必需）的 `base_url / 模型 / API key`，点「测试连接」实时验证；
   视觉/摘要/长文可留空。保存写入 `config.env`，**文本/视觉通道需重启 bot 生效**（向导提供受控重启）。
2. **QQ 登录** —— 显示 NapCat 状态与登录二维码，用 QQ 扫码；每 3 秒自动刷新。
3. **后台状态** —— 查看服务健康；确认后点「完成初始化」。

完成后向导自动关闭（落 `data/.setup_complete` 标记），改用常规面板 `/admin/dashboard`。
**已完成的部署里向导完全惰性**——写配置/测试/重启等接口一律拒绝，不影响正常运行。

## 手动 / 进阶

不想用向导，也可以直接编辑 `config.env`（参考 `.env.example`），再 `python launcher.py`。
参数优先级：`data/control-v4/settings.json`（面板热改）> `config.env` > 代码默认。

生产建议用 systemd 托管 `launcher.py`（`WorkingDirectory=` 仓库目录），面板经 nginx/HTTPS 对外。
模型通道、图像/语音、监控账单等全部键位见 `.env.example` 与 [配置参考](docs/CONFIGURATION.md)。

## 红线（务必遵守）

- **永不自动重启/重建 NapCat 容器**：QQ 登录态与设备指纹的稳定性高于一切；`setup.py` 对已存在容器只报告不动作。
- **不删不覆盖用户数据**：`config.env` 已存在时 setup 不覆盖；改数据先备份。
- **密钥不进仓库**：`config.env`、`data/`、`memory/` 均已 gitignore；向导只把 key 写进本机 `config.env`，绝不回显/记录。
- **人格是数据不是代码**：具体角色内容放 `DATA_DIR/personas/`，不写进平台代码或公开仓库。

## 测试

```bash
python tests/run_all.py     # 全量隔离测试，无需任何真实凭据
```
