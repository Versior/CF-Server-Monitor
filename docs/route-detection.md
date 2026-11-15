# 自动线路检测

线路检测由 **VPS 上的独立 Python 脚本、NextTrace 和 Worker** 配合完成，无需更换现有 cfsm-agent。未安装检测脚本的服务器继续使用手动标签。

脚本从后台读取本台服务器的电信、联通、移动测试目标，继承项目的全局目标及节点覆盖配置；设置为 `0` 的目标不检测。使用有限次数的 TCP 或 ICMP traceroute，上报每跳的 ASN，由 Worker 识别 CN2、163、4837、9929、CMI、CMNET。IPv4 默认开启，IPv6 可选。

## 检测结果的含义

- 结果表示 **VPS 到当前配置目标的单向路径**。不表示另一方向，也不认证 GIA/GT、带宽、延迟或商家的线路等级。
- 完整路径经过已知 ASN 时显示对应网络；混合网络会全部保留，例如 `CN2 / 163`。
- 未到达目标时标记“路径未完成”，缺少已知 ASN 时显示“未识别”，执行失败显示“检测失败”。失败结果会替换旧记录。
- 结果有效期为 24 小时，过期后不再用于自动线路标签。
- 手动标签优先。原生首页在没有手动标签时使用已完成检测的线路；详情显示检测时间、各方向状态和 ASN 证据。第三方主题可读取同样的数据。
- IPv4 和 IPv6 分开记录。每个运营商最多检测两个地址族，不推断未检测的方向。

ASN 对照来自 [RIPEstat](https://stat.ripe.net/)，路径结构与 CLI 参数按 [NextTrace v1.7.6](https://github.com/nxtrace/NTrace-core/releases/tag/v1.7.6) 校验。

## Worker 更新

部署本版 Worker 后，在管理后台执行一次“数据库升级”，为 `servers` 增加 `route_report TEXT DEFAULT ''`。这是追加字段，保留服务器配置和原有标签。无需清空历史或重建数据库。

旧数据库未升级时，检测接口返回 HTTP 409。回退到旧版 Worker 时停止检测定时器即可，额外字段不会改变旧版指标上报；无需删除该列。

## VPS 安装

### 一次安装监控探针和线路检测

Linux **x86_64 / aarch64**、使用 **systemd** 的 VPS，可以通过统一入口同时安装 Go 监控探针、NextTrace 和线路检测。先完成上面的 Worker 更新与数据库升级，并在后台添加服务器取得 ID。在 VPS 的 root 终端运行：

```sh
curl -fsSL https://raw.githubusercontent.com/Versior/CF-Server-Monitor/main/scripts/install-probe.sh -o /root/cfsm-install.sh && sh /root/cfsm-install.sh
```

首次安装按提示输入监控站点地址、服务器 ID、Agent 上报凭据和站点 UA。凭据输入隐藏，只写入权限为 `600` 的配置文件，不放进命令参数、URL 或日志。默认 UA 为 `cfsm`，修改过站点 UA 时填写相同值。

已有 root/system Go 探针时，统一入口直接复用 `/etc/config/cf-probe/config.conf` 中的地址、ID 和凭据，保留原探针版本、配置与流量记录。只有探针尚未安装时才调用官方 Go 二进制的 `install`，无需分别执行两套安装命令。

统一入口会检查或安装 Python、curl 和 CA 证书，验证 Worker 检测接口后安装两项服务、执行首轮线路检测，并启用每六小时的定时器。旧 Worker 返回网页、数据库尚未升级或上报凭据错误时会停止安装。线路结果是否完整、是否识别到已知 ASN，以节点网络详情为准。

全新监控探针使用 **cfsm-agent v1.1.3**，线路检测使用 **NextTrace v1.7.6**；按官方 Release 的 SHA256 校验二进制后才执行。NextTrace 安装在 `/opt/cfsm-route-detector/nexttrace`，不覆盖系统已有的 NextTrace。检测到非 root、Docker、旧 Shell 或多实例探针时，统一入口会停止，避免重复上报；非 systemd 系统不使用此入口。

重复运行无需先卸载：同一个 root/system 监控探针继续使用 `cf-probe.service`；线路检测会停止旧任务、覆盖原检测文件，再启用同一个 `cfsm-route-detector.timer`。监控和线路检测是两项不同服务，不会创建第二份同名监控探针。

双栈检测在同一命令末尾加 `--ipv6`。无人值守首次安装可以使用 `--config /path/to/private.json`，文件权限须为 `600`：

```json
{
  "url": "https://monitor.example.com",
  "id": "YOUR_SERVER_ID",
  "secret": "YOUR_AGENT_API_SECRET",
  "ua": "cfsm"
}
```

安装步骤仅作用于当前 VPS，不能替代 Cloudflare Worker 部署或管理后台的数据库升级。

### 只安装线路检测

已有其他形式的监控探针、只需要独立线路检测时，要求 Linux、Python **3.9+**、[NextTrace **v1.7.6+**](https://github.com/nxtrace/NTrace-core)。先按 NextTrace 官方文档安装对应架构的版本。NextTrace 需要发送原始探测包的权限。

从本仓库下载 `scripts/route-detector.py`、`scripts/route-detector.service`、`scripts/route-detector.timer` 后，安装到 VPS：

```sh
sudo install -d -m 755 /opt/cfsm-route-detector
sudo install -m 755 scripts/route-detector.py /opt/cfsm-route-detector/route-detector.py
sudo install -m 644 scripts/route-detector.service /etc/systemd/system/cfsm-route-detector.service
sudo install -m 644 scripts/route-detector.timer /etc/systemd/system/cfsm-route-detector.timer
sudo install -m 600 /dev/null /etc/cfsm-route-detector.json
sudoedit /etc/cfsm-route-detector.json
```

配置内容：

```json
{
  "url": "https://monitor.example.com",
  "id": "YOUR_SERVER_ID",
  "secret": "YOUR_AGENT_API_SECRET",
  "ua": "cfsm",
  "families": [4],
  "nexttrace": "/usr/local/bin/nexttrace"
}
```

`secret` 使用现有 Agent 的上报凭据。只写在 VPS 的私有文件中，脚本要求文件权限为 `600`，不放在命令参数、URL 或日志中。`url` 为 Worker 的 HTTPS origin；HTTP 仅允许 localhost 测试。`ua` 默认 `cfsm`，修改过站点 UA 时填写相同值。双栈检测将 `families` 改为 `[4, 6]`。

先运行一次并确认记录：

```sh
sudo /usr/bin/python3 /opt/cfsm-route-detector/route-detector.py --config /etc/cfsm-route-detector.json
```

再启用每六小时的自动检测：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now cfsm-route-detector.timer
systemctl list-timers cfsm-route-detector.timer
sudo journalctl -u cfsm-route-detector.service --no-pager -n 20
```

单次最多六条方向，每条限 30 跳、每跳两次探测，单条进程超时 120 秒，整个服务超时 15 分钟。每次运行都读取当前后台目标；修改三网目标后可手动启动 `cfsm-route-detector.service`，无需修改脚本。systemd 不会同时运行同一个 oneshot 服务。

停用：

```sh
sudo systemctl disable --now cfsm-route-detector.timer
```

停止后，最后一次记录会在 24 小时后失效，手动标签继续显示。

## 数据与认证

`GET /update/routes?id=<server-id>` 返回本台服务器的有效测试目标；`POST /update/routes` 保存检测证据。两个接口都使用 `Authorization: Bearer <agent-secret>`，与现有 Agent 上报权限相同。App 访问口令和前端登录状态不能代替这个凭据。

POST 示例（无凭据、IP 地址或主机名）：

```json
{
  "id": "YOUR_SERVER_ID",
  "routes": [
    { "carrier": "ct", "family": 4, "status": "ok", "completed": true, "hops": [[], [4809], [4134]] }
  ]
}
```

`carrier` 为 `ct/cu/cm`，`family` 为 `4/6`。客户端状态为 `ok/failed/disabled`；失败和禁用时 `hops` 必须为空、`completed` 必须为 `false`。Worker 重新计算识别结果，不接受客户端直接指定“GIA”或网络名称。

公开 `/api/servers` 和 `/api/server` 新增 `route_report` 对象：`checked_at`、`expires_at`（毫秒时间戳）和 `routes`。每条方向包含 `carrier`、`family`、`status`（`detected/partial/unknown/failed/disabled`）、`completed`、`networks`、`hops`。未检测为 `null`。沿用现有私有站点及隐藏服务器的访问规则。

Worker 只保存 ASN，不保存本次检测的真实 IP、路由器主机名、完整 NextTrace 输出或凭据。**NextTrace 本身可能向所选 GeoIP 服务查询各跳的地址**，按 NextTrace 官方数据服务说明配置。检测命令 stdout/stderr 被脚本捕获，不写入项目日志。

## 验证

```sh
node --test test/route-detection.test.js
python3 -m unittest discover -s test -p route_detector_test.py
python3 -m unittest discover -s test -p probe_installer_test.py
npm test
npm run build:frontend
```

测试覆盖真实 D1 写入、旧数据库追加升级、鉴权、隐藏节点、16 KiB 载荷上限、缓存刷新、混合 ASN、IPv6、失败与过期，以及 NextTrace JSON 的解析。统一安装测试覆盖 SHA256、私有文件、旧配置复用、不执行配置中的 Shell 表达式、两项服务调用和真实 HTTP 预检查。目标网络是否允许 traceroute、VPS 权限及真实路径仍须在部署后的 VPS 上验证。
