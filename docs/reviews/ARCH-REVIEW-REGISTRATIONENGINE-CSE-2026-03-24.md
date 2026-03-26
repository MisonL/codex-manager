# 架构复核与脆弱性评估报告

日期: 2026-03-24
范围:
- 代码: `src/core/register.py`, `src/core/http_client.py`, `src/web/routes/registration.py`, `src/services/base.py`, `src/services/tempmail.py`
- 已有审计: `docs/reviews/ARCH-AUDIT-REGISTRATIONENGINE-2026-03-23.md`
- 验证口径: `docs/reviews/TASK-5-VALIDATION-2026-03-23.md`, `docs/reviews/FUNCTIONAL-AVAILABILITY-REPORT-2026-03-24.md`
- 测试口径: `tests/test_register_protocol_baseline.py`, `tests/test_registration_otp_phase.py`, `tests/test_registration_email_service_failover.py`
- 外部约束: OpenAI Terms of Use (生效日期 2026-01-01) 与 OpenAI Usage Policies / agent policy 页面

## 1. 执行摘要

基于 2026-03-24 当前代码事实，本系统已经不再是单一大函数，而是一个包含显式阶段结果的分层控制回路:

- `src/core/register.py:1908` 的 `phase_sequence` 代码里实际列出了 10 个 phase。
- 若将 `ip_check` 视为前置预检，则主业务状态机确实是 9 个阶段:
  - `email_prepare`
  - `signup_submit`
  - `signup_password`
  - `otp_primary`
  - `account_create`
  - `oauth_reenter`
  - `otp_secondary`
  - `workspace_resolve`
  - `oauth_callback`

本轮复核结论有四条:

1. 当前系统已经形成“局部闭环”，但还没有形成“全局稳定闭环”。
2. 系统对邮箱供应商限流和二次 OTP 抖动已经具备可观测、可退避、可持久化的控制能力，这是目前最坚固的环节。
3. 系统对 OpenAI 风控的“对抗有效性”从第一性原理上并不稳固，因为它只能控制请求表象，无法控制平台真正用于判别信任的上游变量。
4. 系统对一般网络抖动具备有限容忍度，但容忍范围主要集中在邮箱 OTP 这一条支路，尚不足以覆盖授权、代理信誉、Cookie 连续性和协议漂移的全链路扰动。

一句话判断:

- 这是一个“对邮箱时滞已有局部控制能力、但对平台级自适应风控缺乏可持续控制权”的注册自动化系统。

## 2. 证据基线

当前结论建立在以下代码事实之上:

- `PhaseResult`、`phase_history` 和 `next_action` 已经进入主引擎，见 `src/core/register.py:126`, `src/core/register.py:195`, `src/core/register.py:1895`。
- 二次 OTP 已有独立错误码 `OTP_TIMEOUT_SECONDARY`，见 `src/core/register.py:53`, `src/core/register.py:820`。
- 邮箱供应商退避状态已进入可持久化结构 `EmailProviderBackoffState`，见 `src/services/base.py:23`。
- 路由层把 OTP 超时回写为邮箱服务退避状态，并用锁保护并发更新，见 `src/web/routes/registration.py:331`, `src/web/routes/registration.py:364`。
- Tempmail 实现已经使用 `otp_sent_at` 和时间戳过滤旧邮件，见 `src/services/tempmail.py:177`, `src/services/tempmail.py:248`。
- 实测报告已经证明任务日志实时推送、深度冷却、重启恢复等外围能力生效，见 `docs/reviews/FUNCTIONAL-AVAILABILITY-REPORT-2026-03-24.md`。

外部边界也需要明确:

- OpenAI Terms of Use 页面在 2026-01-01 生效版本中明确禁止规避 rate limits、restrictions、protective measures 和 safety mitigations。
- OpenAI 的 usage / agent policy 页面同样明确禁止 bypass safeguards。

因此，本报告对“对抗风控”的判断只做系统脆弱性分析，不把规避能力视为正当目标，也不输出规避方案。

## 3. CSE 控制映射

### 3.1 Plant

被控对象不是单一程序，而是四个耦合子系统:

1. OpenAI 授权与账号建立链路
   - 账号创建
   - OTP 校验
   - consent / workspace 选择
   - OAuth callback 换 token
2. 邮箱供应商链路
   - 创建邮箱
   - 拉取 OTP
   - 供应商限流与时滞
3. 代理与网络链路
   - 代理可用性
   - TLS / curl 错误
   - 地理位置与网络抖动
4. 本地状态面
   - 数据库任务状态
   - 邮箱服务退避状态
   - WebSocket / 日志观测

从控制论角度看，真正最强的外部 Plant 是 OpenAI 风控系统本身，因为它拥有规则更新权、判定权和封禁权，而本系统没有这些控制权。

### 3.2 Sensors

当前可用传感器分成四层:

1. 引擎内传感器
   - `_log()` 日志流，见 `src/core/register.py:202`
   - `phase_history`，见 `src/core/register.py:195`
   - `PhaseResult.error_code / retryable / next_action`，见 `src/core/register.py:126`
2. 路由层传感器
   - `email_prepare` 的供应商退避状态
   - OTP 超时触发的持久化 backoff
   - 代理网络错误识别，见 `src/web/routes/registration.py:83`
3. 状态面传感器
   - 任务状态写库
   - 任务日志写库
   - 邮箱服务 circuit breaker 状态
4. 验证传感器
   - 协议基线测试
   - OTP phase 测试
   - failover / 并发 / 深度冷却测试
   - live 功能实测

传感器质量评价:

- 比 2026-03-23 之前明显增强。
- 已经能区分一部分“邮箱供应商限流”和“OTP 超时”。
- 仍然无法统一感知全链路 HTTP 状态、代理信誉、风控挑战页命中率和 token 交换失败根因。

### 3.3 Controller

当前控制器不是单点，而是双层控制器:

1. 内层控制器: `RegistrationEngine.run()`
   - 负责按 phase 顺序推进，见 `src/core/register.py:1895`
   - 负责在 phase 失败时根据 `retryable + next_action` 跳转
2. 外层控制器: `_run_sync_registration_task()`
   - 负责代理选择
   - 负责邮箱服务候选集切换
   - 负责 OTP 超时后将 backoff 持久化
   - 负责成功后的状态收口，见 `src/web/routes/registration.py:530`

这是一个典型的“内层状态机 + 外层资源调度器”结构。

优点:

- 已经把邮箱供应商 failover 从业务 phase 中抽离出来。
- 已经有局部控制动作: `switch_provider` 与换代理。

缺点:

- 控制器的目标函数没有统一定义。
- 内层关心 phase 成败，外层关心资源替换，但二者没有共享“成功概率 / 成本 / 风险”的统一评价函数。
- `next_action` 只支持向前跳，不支持局部重试闭环、补偿闭环或回滚闭环。

### 3.4 Actuators

当前执行器主要有六类:

1. 发 HTTP 请求到 OpenAI
2. 发 HTTP 请求到邮箱供应商
3. 选择并切换邮箱服务
4. 选择并禁用代理
5. 跟随重定向并选择 workspace
6. 处理 OAuth callback 并交换 token

执行器能力评价:

- 对邮箱供应商的执行器已经具备退避与切换能力。
- 对代理执行器仅具备“网络错误后淘汰”的弱能力。
- 对 OpenAI 主链执行器仍然缺乏统一重试、统一超时预算和统一分类策略。

## 4. 控制拓扑判断

把当前系统压缩成控制拓扑，可以得到如下判断:

- 控制面主落点: `src/core/register.py` 的 phase state machine，加上 `src/web/routes/registration.py` 的外层资源调度。
- 数据面主落点: OpenAI 授权请求、OTP 请求、workspace 选择、token 交换。
- 状态面主落点: 任务表、邮箱服务 backoff 状态、任务日志。

复杂性转移账本如下:

| 原复杂性位置 | 新位置 | 收益 | 新成本 | 新失效模式 |
| --- | --- | --- | --- | --- |
| OTP 超时归因混在注册主流程 | `PhaseResult` + 路由层 backoff | Timeout 可分类、可持久化 | 控制链跨文件 | 内层与外层结论不一致 |
| 单次任务里的邮箱供应商判断 | 路由层候选集与 circuit breaker | 可在任务间复用经验 | 需要锁与共享状态 | 并发下可能形成全局抖动 |
| Workspace 解析只依赖 Cookie | Consent / HTML / JSON / URL 多路径解析 | 抗协议表面漂移更强 | 解析逻辑增多 | 错误匹配与过拟合页面结构 |

总体评价:

- 系统已经把复杂性从单一大函数部分下沉到路由层和状态面。
- 但这种转移目前仍然是“局部治理”，没有形成统一的项目级总体设计部。

## 5. 第一性原理分析

### 5.1 对抗 OpenAI 风控的根本有效性

从第一性原理看，一个注册自动化系统若要稳定通过平台风控，至少要同时满足三件事:

1. 它发出的外部行为必须持续接近真实用户行为。
2. 它必须能够控制平台判定所依赖的大部分关键信号。
3. 平台判定规则的更新速度不能长期快于它的适配速度。

当前系统只对第一件事做了部分表面模拟，对第二件事几乎没有控制权，对第三件事没有任何结构性保证。

更具体地说:

- 它能控制的主要是:
  - 请求顺序
  - 部分 header
  - Cookie 连续性的一小部分
  - 代理切换
  - 邮箱来源
- 它不能稳定控制的主要是:
  - 平台对 IP / ASN / 地域 / 历史信誉的综合评分
  - 设备指纹、浏览器指纹和 JS 运行环境的一致性
  - 页面挑战、风控分流、动态策略下发
  - 邮箱域名信誉
  - 账号链路关联分析
  - 平台规则变化节奏

因此，对抗风控的根本上限很低:

- 这套系统最多只能在部分静态或弱动态规则窗口内工作。
- 一旦平台把判定重点从“协议是否正确”转向“信号是否可信”，系统将迅速失稳。

这是结构性限制，不是“再补几个请求头”可以解决的问题。

### 5.2 容忍网络抖动的根本有效性

从第一性原理看，抗网络抖动的稳定系统至少要具备:

1. 扰动识别: 能分清网络抖动、供应商限流、业务拒绝、协议漂移。
2. 局部补偿: 失败后优先在局部 phase 收敛，而不是全局失败。
3. 状态保持: 重试时能保留足够上下文，不让前序成果丢失。
4. 预算控制: 每个 phase 都有独立 timeout / retry / rollback 预算。

当前系统只在 OTP 支路较接近这个标准:

- 有独立超时错误码。
- 有 `otp_sent_at` 锚点。
- 有邮箱供应商 backoff 持久化。
- 有并发下的退避状态保护。

但在全链路上仍然不足:

- 大量请求直接走 `self.session.get/post`，没有统一重试器。
- 绝大多数 phase 失败后直接任务失败，不做局部重试。
- 代理切换只覆盖少数 curl 错误，不覆盖 401 / 403 / 429 / challenge。
- phase pointer 只存在内存里，任务中断后不能从中间恢复。

结论:

- 当前系统对“邮箱 OTP 到达抖动”有实质容忍力。
- 对“全链路网络抖动”只有有限容忍力，还谈不上鲁棒控制。

## 6. 最坚固的环节

### 6.1 OTP 二阶段控制闭环

这是目前系统最坚固的部分。

证据:

- `src/core/register.py:820` 为二次 OTP 建立了独立 phase 与独立错误码。
- `src/services/tempmail.py:248` 开始用 `otp_sent_at` 过滤旧邮件。
- `src/web/routes/registration.py:331` 会把 OTP timeout 回写成邮箱服务 backoff。
- `tests/test_registration_email_service_failover.py:199` 验证了连续 3 次 OTP timeout 会进入 `3600s` 深度冷却。

为什么它坚固:

- 误差已经被正确命名。
- 控制动作明确存在。
- 控制状态能够跨任务保留。
- 并发更新有锁保护，不容易出现“两个任务同时把失败次数覆盖掉”的假收敛。

### 6.2 Workspace 解析的多路径冗余

这是第二坚固的环节。

证据:

- 可从 Cookie、HTML、JSON payload、URL 四类来源提取 workspace，见 `src/core/register.py:1035`, `src/core/register.py:1193`, `src/core/register.py:1675`。
- `tests/test_registration_otp_phase.py:232` 与 `tests/test_registration_otp_phase.py:249` 已覆盖 JSON / 文本提取口径。

为什么它坚固:

- 它不把成功建立在单一页面结构上。
- 对 OpenAI 页面表层字段漂移存在一定弹性。

### 6.3 路由层的外部资源调度

证据:

- 邮箱服务候选集、circuit breaker、代理切换逻辑都在路由层统一编排，见 `src/web/routes/registration.py:424`, `src/web/routes/registration.py:530`。
- 并发下 backoff 不丢失已被测试验证，见 `tests/test_registration_email_service_failover.py:418`。

为什么它坚固:

- 它已经把“资源选择”从单次 phase 执行里抽离。
- 这让邮箱服务的局部失败不会立刻变成整个系统的永久失败。

## 7. 潜藏的失控风险点

### 7.1 最大风险: 对平台信任判定没有控制权

严重性: Critical

风险描述:

- 系统试图自动化穿过一个外部平台的注册与授权链路。
- 平台拥有完整的规则更新权、信誉模型和封禁权。
- 本系统没有任何机制能够稳定建模或控制这些上游判定变量。

失控表现:

- 某天协议仍然正确，但通过率突然断崖下降。
- 某类代理或某类邮箱域名被整体降权，系统无法从代码内部自愈。
- 表面 phase 全部健康，实际成功率持续恶化。

这是最根本的失控点。

### 7.2 控制器仍然偏开环，只有前跳没有局部闭环

严重性: High

证据:

- `src/core/register.py:1944` 只允许 `retryable + next_action` 向后跳转。
- 没有 phase 内统一 retry budget，也没有失败后重试当前 phase 的通用框架。

风险:

- 任何瞬态故障都更容易被放大成终态失败。
- 控制器不能表达“同相位局部补偿后再继续”的基本控制动作。
- 一旦前一阶段成功获取的上下文需要被复用，当前结构很难安全实现真正的局部重试。

### 7.3 HTTP 观测与控制不统一

严重性: High

证据:

- `HTTPClient.request()` 具备统一请求包装，见 `src/core/http_client.py:84`。
- 但核心 phase 里的大量请求直接走 `self.session.get/post`，例如 `src/core/register.py:672`, `src/core/register.py:729`, `src/core/register.py:954`, `src/core/register.py:988`, `src/core/register.py:1252`, `src/core/register.py:1784`。

风险:

- 统一重试、统一超时、统一状态码分类没有真正落地到主链。
- 传感器看到的是碎片化错误，控制器拿不到统一误差模型。
- 401 / 403 / 429 / 5xx 的控制含义无法被一致解释。

### 7.4 代理控制过弱，无法覆盖风控型失败

严重性: High

证据:

- `src/web/routes/registration.py:83` 的代理重试只识别 curl 35 / 56。
- 403 challenge、401 会话失配、429 节流、页面挑战都不会自动触发代理评分或代理淘汰。

风险:

- 代理层只能处理“网络坏了”，不能处理“信誉坏了”。
- 一旦平台以 challenge 或策略页形式拒绝流量，系统会在错误的位置做错误的控制动作。

### 7.5 传感器和状态面仍然耦合

严重性: Medium

证据:

- `_log()` 在引擎内部同时写内存、回调、数据库和日志系统，见 `src/core/register.py:202`。

风险:

- 观测路径仍然是副作用路径的一部分。
- 数据库慢、锁冲突或写失败虽然被捕获，但仍会把控制器和状态面绑在一起。
- 系统还没有真正做到“先发布事件，再由观测器消费”。

### 7.6 状态机可恢复性不足

严重性: Medium

证据:

- `phase_pointer` 只存在于 `run()` 的本地变量中，见 `src/core/register.py:1928`。
- 实测报告证明任务级恢复有效，但不是 phase 级恢复，见 `docs/reviews/FUNCTIONAL-AVAILABILITY-REPORT-2026-03-24.md`。

风险:

- 如果进程在长链路中途退出，外部 backoff 会保留，但内部 OAuth / Cookie / phase 上下文全部丢失。
- 这会形成“局部状态持久化，主链状态不持久化”的不对称控制。

### 7.7 协议稳态增强了，但信号稳态没有增强

严重性: Medium

证据:

- 代码在 workspace 解析和 continue_url 路径上加了大量协议兼容逻辑。
- 但请求环境仍以 `curl_cffi` + 静态浏览器模拟为主，见 `src/core/http_client.py:27`, `src/core/http_client.py:257`。

风险:

- 这提高了“页面结构变动”下的生存率。
- 但几乎没有提高“平台信号建模升级”下的生存率。

换句话说:

- 系统现在更能适应协议表层漂移。
- 但仍然无法适应信任模型漂移。

## 8. 综合判断

如果只问“当前系统最坚固的环节是什么”，答案是:

- 二次 OTP 的独立归因、邮箱供应商 backoff、并发安全的退避持久化闭环。

如果只问“当前系统最大的失控风险点是什么”，答案是:

- 系统把大量精力投在协议流正确性上，但并不掌握平台风控真正依赖的信任信号，因此一旦对方把判定重点上移到信誉与指纹层，系统会出现结构性失稳。

最终判断如下:

1. 从 CSE 视角看，当前系统已经是“局部闭环系统”，不再是完全开环脚本。
2. 从第一性原理看，它对邮箱时滞有真实控制力，对网络抖动有有限控制力。
3. 但它对平台级风控只有表层适配力，没有稳定控制权。
4. 因此，这套引擎的真实上限不是由代码技巧决定，而是由外部 Plant 的信任判定权决定。

这意味着:

- 在“邮箱供应商限流 / OTP 抖动”问题上，系统正逐步变强。
- 在“长期对抗平台风控”问题上，系统仍然天然脆弱。
