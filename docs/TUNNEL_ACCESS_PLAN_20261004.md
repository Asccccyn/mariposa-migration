# mariposa 公网上线方案（Tunnel + Access + token 重生成）· 2026-10-04

状态：**草案，等江乔生逐项批准**。本文档只是方案——没有执行任何网络、
DNS、Cloudflare 面板或服务变更（隧道只读优先策略）。

## 0. 取证快照（2026-10-04，只读）

- 本机已有 7 条 Cloudflare Tunnel 常驻（family / superposition /
  siren / antinomy / bobo / zashidele / devspace），全部
  `--protocol http2`；其中 family 为 **token 模式**（dashboard 远程
  管理，ingress 规则在 Cloudflare 面板，本地无 config 文件），
  其余部分走 `~/Data/cloudflare/*.yml`。
- mariposa 的 launchd 已备好未加载：
  `~/Library/LaunchAgents/com.charstarearth.mariposa-runtime.plist`
  （uvicorn 127.0.0.1:18780，MARIPOSA_ROOT=/Users/zhoujiaming/Data/
  mariposa，KeepAlive+RunAtLoad）。
- 生产数据根 `~/Data/mariposa` 尚不存在（未初始化，无真实数据）。
- 应用层门禁已在代码内落地（2026-10-04）：认证失败锁定（同 IP 连
  续 5 次 → 15 分钟起步指数升级封顶 24h，audit 留痕）+ 限速
  （principal 读 240/min、写 30/min、匿名 IP 60/min）+ CF-Connecting-IP
  仅在回环直连时采信。回归测试 12 例全绿。

## 1. 上线后的三层门

```
互联网
  │
  ▼
① Cloudflare Access（身份层：只有她的邮箱/设备能到达——挡扫描
   与无关访客；MCP 客户端走 Service Token）
  │
  ▼
② 应用 Bearer token（身份层第二道：client_bindings 哈希存储，
   可撤销；配合失败锁定防在线枚举）
  │
  ▼
③ 应用层限速 + 失败锁定（gate.py，已落地）+ 既有业务权限矩阵
```

任何一层被绕过，下一层仍然独立生效；三层凭证互不相同。

## 2. 拟执行步骤（每一步都单独等批准）

### 步骤 A：初始化生产数据根并加载服务（本机，无网络变更）

1. 建 `~/Data/mariposa`（MARIPOSA_ROOT），migrate 建库；
2. `launchctl load com.charstarearth.mariposa-runtime`；
3. 验证 `curl 127.0.0.1:18780/health` 只听回环（lsof 确认
   无局域网/公网监听）。

### 步骤 B：Cloudflare 侧新增 hostname（唯一网络变更）

推荐**复用现有 tunnel 加一条 ingress**（新增规则，不动既有 7 条
服务的任何规则；回退 = 删掉这一条）：

- Hostname：**待她定**。候选（都是 `charstarearth.online` 子域）：
  - `mariposa.charstarearth.online`（直白）
  - `memory.charstarearth.online`（对外语义中性）
  - 其他她喜欢的名字
- 指向：`http://127.0.0.1:18780`
- 若该 tunnel 是 token 模式：在 Cloudflare Zero Trust 面板
  Tunnels → 该 tunnel → Public Hostname → Add（她操作或她给我
  明确授权后代操作，全程只加不改）；yml 模式则改对应 yml 加
  ingress 条目后 `launchctl kickstart` 重载。
- 已知环境约束（0929 取证）：VPN 常开、不固定 edge；每 region
  DNS 只回 1 地址 → 连接数上限 2，面板偶现 DEGRADED 属环境上限
  非故障，不作为回退依据。

### 步骤 C：Cloudflare Access 应用（身份层）

Zero Trust → Access → Applications → Self-hosted：

- Application domain = 步骤 B 的 hostname（整域覆盖 `/`）；
- 策略 1（她的浏览器）：Email / Google OAuth = 她的邮箱，
  Require 示符完后再加设备 posture 等强化，首版从简；
- 策略 2（周家明 MCP / 程序调用）：**Service Token**——Zero Trust
  生成一对 `CF-Access-Client-Id` / `CF-Access-Client-Secret`，
  客户端每次请求带这两个头。明文同样走剪贴板盲搬，不落屏不进
  命令行参数。
- 会话时长建议 24h（私人应用，频繁输验证码烦；可调）。

> 备选：不配 Access，只靠 ②+③（Bearer + 门禁）。上线更快，但
> 少一层"根本到不了登录框"的挡板，且 token 泄露时只剩单层。
> 推荐配 Access；她嫌麻烦可以后补。

### 步骤 D：生产 token 重生成（替换测试值 tok-q/tok-j/tok-w）

1. 写一次性脚本：`secrets.token_urlsafe(32)` 生成
   qiaosheng / jiaming（worker 视维护入口需要）三个新 token；
2. `identity.rebind`（或等价撤销+重绑）写入生产库——库里只有
   sha256 哈希，明文不落库；
3. 明文经 **pbcopy 盲搬**给她（逐个生成→复制→她粘贴到自己的
   密码管理器/MCP 配置；终端与屏幕不显示值）；
4. 周家明侧的 MCP 配置更新由她决定经手方式（可走 gpt-zcode-bridge
   inbox 传递文件，不经过屏幕明文）。

### 步骤 E：她导入真实 md → 收口上线

- 她自导（或她授权后由网页上传——上传链路本轮已修通）；
- 导入后跑一轮 codex 复审探针对生产只读抽查（不写）；
- 观察门禁 audit（auth.locked / rate）一周，无异常即收口。

## 3. 回退预案

- 步骤 B 出问题：面板删该 hostname 规则（其余 7 服务零影响）；
- 步骤 C 出问题：停用该 Access 应用（服务仍受 token+门禁保护）；
- 步骤 A 出问题：`launchctl unload` + 保留数据根待查。

## 4. 本方案不做的事

- 不动既有 7 条 tunnel 的任何配置；
- 不动 VPN / DNS / 路由（任何此类变更永远单独请示）；
- 不在本文件或任何屏幕输出中粘贴 tunnel token / Access secret /
  Bearer token 明文。
