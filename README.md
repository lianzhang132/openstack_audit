# OpenStack Audit

[![Tests](https://github.com/lianzhang132/openstack_audit/actions/workflows/tests.yml/badge.svg)](https://github.com/lianzhang132/openstack_audit/actions/workflows/tests.yml)

一个面向中小型 OpenStack 环境的云主机审计、到期管理与资源回收 Web 应用。

项目使用 Flask、SQLite、OpenStack SDK 和 APScheduler 构建，提供云主机同步、项目归属管理、到期提醒、变更记录、虚机回收及附属资源回收等能力。

> [!CAUTION]
> 本项目可以直接删除 OpenStack 云主机、存储卷和网络端口。当前版本没有内置登录与权限控制，请勿直接暴露到公网。生产部署前应放置在 VPN、零信任网关或带身份认证的反向代理之后，并使用最小权限 OpenStack 服务账号。

## 功能

- 从 OpenStack 同步云主机、镜像、规格、网络、宿主机和存储卷信息
- 管理内部项目归属、用途、申请人和长期使用状态
- 根据到期时间自动将云主机加入待回收队列
- 独立管理虚机与附属资源的回收周期、延期和长期保留
- 记录同步、修改、延期和回收操作
- 禁止通过应用删除审计记录
- 将云主机与审计信息导出为 Excel
- 从 Excel 导入历史记录

## 安全行为

资源回收属于高风险操作，当前实现包含以下保护：

- OpenStack 批量查询失败时，同步任务会失败，不会把空结果当作全部云主机已删除。
- 批量结果缺少某台云主机时，会再次逐台确认后才更新删除状态。
- 云主机删除失败时，本地状态不会推进到已删除。
- 附属资源只会在确认删除成功或确认不存在后标记为已回收。
- 部分附属资源删除失败时，云主机保持待回收状态。
- 定时任务尊重附属资源延期时间和长期保留设置。
- 审计记录没有删除入口，并在 ORM 层阻止删除。

仍需注意：

- 当前没有内置身份认证、操作审批和 CSRF 防护。
- OpenStack 删除请求可能是异步操作，执行前应确认目标资源与快照信息。
- 建议定期备份 SQLite 数据库，并先在测试环境验证回收流程。

## 技术栈

- Python 3.11
- Flask 2.3
- Flask-SQLAlchemy
- SQLite
- OpenStack SDK
- APScheduler
- Gunicorn
- Jinja2

## 快速开始

### 1. 准备配置

复制环境变量示例并填写 OpenStack 连接信息：

```bash
cp .env.example .env
```

至少需要设置：

```dotenv
SECRET_KEY=replace-with-a-long-random-value
OS_AUTH_URL=https://openstack.example.com:5000/v3
OS_USERNAME=audit-service
OS_PASSWORD=replace-with-openstack-password
OS_PROJECT_NAME=service
```

### 2. Docker 运行

```bash
docker build -t openstack-audit .

docker run --name openstack-audit \
  --env-file .env \
  -p 5432:5432 \
  -v openstack-audit-data:/app/data \
  openstack-audit
```

访问：

```text
http://localhost:5432
```

### 3. 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_PATH=./data/openstack_audit.db
export ENABLE_SCHEDULER=false
python app.py
```

Windows PowerShell 可使用：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:DATABASE_PATH = ".\data\openstack_audit.db"
$env:ENABLE_SCHEDULER = "false"
python app.py
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SECRET_KEY` | 启动时随机生成 | Flask 会话密钥，生产环境必须显式设置 |
| `DATABASE_PATH` | `/app/data/openstack_audit.db` | SQLite 数据库路径 |
| `OS_AUTH_URL` | `http://controller:5000/v3` | Keystone 认证地址 |
| `OS_USERNAME` | `admin` | OpenStack 用户名 |
| `OS_PASSWORD` | 空 | OpenStack 密码，必须通过环境变量提供 |
| `OS_PROJECT_NAME` | `admin` | OpenStack 项目名 |
| `OS_USER_DOMAIN_NAME` | `Default` | 用户域 |
| `OS_PROJECT_DOMAIN_NAME` | `Default` | 项目域 |
| `OS_REGION_NAME` | `RegionOne` | OpenStack 区域 |
| `VM_DEFAULT_EXPIRE_DAYS` | `90` | 云主机默认有效天数 |
| `ATTACHED_RESOURCE_EXPIRE_DAYS` | `30` | 虚机删除后附属资源默认保留天数 |
| `SYNC_INTERVAL_HOURS` | `6` | OpenStack 自动同步间隔 |
| `ENABLE_SCHEDULER` | `true` | 是否启动定时任务 |
| `DEBUG` | `false` | Flask 调试模式 |

## 部署建议

1. 使用只具备必要读取和回收权限的 OpenStack 服务账号。
2. 将应用部署在受控网络中，并在前置代理实现身份认证和访问控制。
3. 只允许一个应用进程启用 APScheduler，避免任务重复执行。
4. 挂载持久化 `/app/data`，并定期备份 SQLite 数据库。
5. 首次接入生产环境前，先关闭调度器并手动核对同步结果。

如需多副本部署，应在 Web 实例上设置 `ENABLE_SCHEDULER=false`，并单独运行一个启用调度器的实例。

## 测试

安全回归测试使用内存数据库和 OpenStack SDK 测试替身，不会连接或删除真实资源：

```bash
python -m unittest discover -s tests -v
```

当前覆盖：

- 同步列表失败时不误判云主机已删除
- 单台确认失败时不推进删除状态
- 云主机回收失败时保持原状态
- 附属资源部分失败时保持待回收状态
- 长期保留和延期策略
- 审计记录删除保护

## 项目结构

```text
.
├── app.py                  # Flask 路由与页面逻辑
├── models.py               # 数据模型与审计记录保护
├── openstack_service.py    # OpenStack SDK 封装
├── scheduler.py            # 同步与到期检查任务
├── import_from_excel.py    # 历史数据导入工具
├── templates/              # Jinja2 页面模板
├── static/                 # 页面样式
├── tests/                  # 安全回归测试
├── config.py               # 环境变量配置
└── Dockerfile
```

## Excel 历史数据导入

将待导入文件命名为 `vm_data.xlsx`，根据需要调整 `import_from_excel.py` 中的列映射后执行：

```bash
python import_from_excel.py
```

建议导入前备份数据库，并先使用少量测试数据验证字段映射。

## 贡献

欢迎提交 Issue 和 Pull Request。涉及回收、同步状态判断或 OpenStack 删除操作的改动，请同时补充回归测试，并明确说明失败场景下的状态处理方式。

安全问题请优先通过 GitHub Security Advisory 私下报告，避免在公开 Issue 中披露凭据或漏洞细节。

## License

[MIT](LICENSE)
