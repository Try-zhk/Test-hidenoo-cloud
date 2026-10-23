## HidenCloud自动续期

使用 GitHub Actions 自动给 HidenCloud 服务续期。**支持多账号，每个账号强制使用自己的独立代理**，严格隔离环境，避免同 IP 多账号被风控。

> 温馨提示：脚本使用账号密码登录（HidenCloud 的 remember_web cookie 有效期很短，已不再使用免密登录），需要过 Cloudflare 验证；请尽量使用干净节点，否则无法过验证。
>
> 过盾强化：真 Chrome + 去自动化标志 + 关闭 AutomationControlled + 禁用后量子 TLS（修复部分代理出口的握手失败）+ 拟人鼠标轨迹 + 验证前拟人行为 + 同一验证连续点不过自动刷新换新验证（最多 2 次）；可用 `BROWSER_TZ` / `BROWSER_LOCALE` 把浏览器时区/语言对齐到代理出口所在地。

---

## 运行机制

1. 每日自动运行 2 次（默认 `01:00` 与 `13:00`，UTC）
2. 每次先读取 `state.json`，判断哪些账号临近到期
3. **只有存在需要续期的账号时，才会安装依赖、启动浏览器并执行续期**；全部未到期则直接结束，不消耗运行时间
4. 处理完成后把最新的到期时间写回 `state.json` 并自动提交，供下次判断
5. 续期成功以「到期时间确已延长」为准：点击支付后如未能核实到期时间延长，会在本次运行内自动重试（`RENEW_MAX_ATTEMPTS`，默认 3 次），不会误报成功；站点提示「未到续期时间」时会先按页面刷新到期时间再结束（上次可能其实已成功）
6. `state.json` 每处理完一个账号就保存一次，中途崩溃或超时也不会丢失已完成账号的进度
7. 设有全局运行时限（`MAX_RUN_SECONDS`，默认 70 分钟）：超时会停止等待验证、跳过剩余账号并发送汇总通知，避免被 CI 硬杀后毫无输出
8. 工作流已加 `concurrency` 锁，同一时间只有一条续期运行，防止定时任务撞车互相覆盖 `state.json`

判定规则：账号剩余时间 ≤ `DUE_WITHIN_HOURS`（默认 24 小时）就续期；`state.json` 中无该账号记录时也会运行一次以补全信息。

---

## 配置

在仓库 `Settings → Secrets and variables → Actions` 中添加以下变量。

### 多账号（变量名与 Test-hiden-cloud 完全一致）

| 变量名 | 是否必填 | 说明 | 示例 |
|---|---|---|---|
| `HIDEN_ACCOUNT_1` | ✅必填 | 第 1 个账号，格式：`邮箱 密码`（空格分隔） | `abc@gmail.com mypassword` |
| `HIDEN_ACCOUNT_2` | ✅必填 | 第 2 个账号，依次类推支持 `_3`、`_4`… | `def@gmail.com mypassword` |
| `PROXY_URL_1` | ✅必填 | 第 1 个账号的专属代理，**必须与账号序号对应** | `vless://uuid@server:port?...` |
| `PROXY_URL_2` | ✅必填 | 第 2 个账号的专属代理 | `socks5://user:pass@server:port` |
| `PROXY_LOCK_1` | ❌可选 | 保留参数（当前代理不可用时一律放弃该账号） | `true` |
| `PROXY_LOCK_2` | ❌可选 | 同上 | `true` |

> ⚠️ **环境隔离是硬性要求**：每个账号都必须有自己的 `PROXY_URL_n`，且**不允许两个账号共用同一个代理**。只要发现缺代理或代理重复，本次运行会直接终止、不执行任何账号，并打印具体原因。

### 通知（可选）

| 变量名 | 说明 |
|---|---|
| `TG_TOKEN` | Telegram Bot Token |
| `TG_CHAT` | Telegram Chat ID |
| `SMTP_CONFIG` | SMTP 配置，JSON 或 JS 对象风格均可，见下 |
| `EMAIL_CHAT` | 接收续期报告的邮箱 |
| `WXPUSH_API` | WxPush 推送地址 |
| `WXPUSH_TOKEN` | WxPush Token |

Telegram 和 WxPush 参考 `Test-hiden-cloud-main` 的通知接口，通过独立 HTTP 会话直连发送，不复用账号的 `PROXY_URL_n`，也不继承 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 等环境代理。因此账号代理在续期结束后关闭，不会影响通知。运行环境需要能直接访问对应通知服务。

Telegram 使用 `sendMessage` JSON 接口；WxPush 向 `WXPUSH_API` 下的 `/wxsend` 发送 `title`、`content`，并在 `Authorization` 请求头中传递 `WXPUSH_TOKEN`。HTTP 错误会记录为通知失败，Telegram 还会检查响应中的 `ok`。邮件继续通过 SMTP 独立发送。

`SMTP_CONFIG` 写法（两种都支持）：

```json
{
  "host": "smtp.gmail.com",
  "port": 587,
  "user": "abc@xxx.com",
  "pass": "xxxxxx"
}
```

### 其它（GitHub 仓库变量 Variables）

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `DUE_WITHIN_HOURS` | `24` | 距离到期不足该小时数才续期 |
| `MAX_RUN_SECONDS` | `4200` | 全局运行时限（秒），超时停止等待验证、跳过剩余账号并保底发送汇总通知 |
| `RENEW_MAX_ATTEMPTS` | `3` | 单个账号续期失败后，本次运行内的最大尝试次数 |
| `BROWSER_TZ` | 空 | 浏览器时区（如 `America/New_York`），建议与代理出口所在地一致 |
| `BROWSER_LOCALE` | 空 | 浏览器语言（如 `en-US`），建议与代理出口所在地一致 |

### 兼容旧版单账号变量

未配置任何 `HIDEN_ACCOUNT_n` 时，会自动回退到 `EMAIL` / `PASSWORD` 单账号模式（同样需要配置代理）。

---

## 代理格式

`PROXY_URL_n` 支持以下分享链接：

- **VLESS**：`vless://uuid@server:port?security=reality&sni=...&type=ws&...`
- **VMess**：`vmess://base64...`
- **Trojan**：`trojan://password@server:port?sni=...&type=ws&...`
- **hysteria2**：`hysteria2://password@server:port...`
- **tuic**：`tuic://uuid:password@server:port...`
- **SOCKS5**：`socks5://user:pass@server:port`
- **HTTP/HTTPS**：`http://user:pass@server:port`

每个账号的代理都会由脚本在本地拉起一个独立端口的 sing-box 实例（第 n 个账号对应 `127.0.0.1:(10800+n)`），浏览器与网络请求都只走该账号自己的出口，账号之间不共享代理，也不提供直连回落。

此隔离规则适用于账号登录、查询与续期请求；通知请求独立直连，不访问账号控制台。

## 日志隐私

- 出口 IPv4 仅显示首尾两段，例如 `78.154.103.35` 显示为 `78.*.*.35`；IPv6 或无法识别的值不显示原文。
- Server ID 仅显示前后两位，例如 `207307` 显示为 `20**07`；不超过四位的 ID 全部隐藏。
- 日志、通知中的服务链接与异常文本也会脱敏，通知 Token 不写入日志；异常文本里意外出现的邮箱地址同样会被打码。
- Telegram 通知（含异常兜底通知）全部发送明文：完整账号邮箱、完整出口 IP、未脱敏的异常文本；邮件/WxPush 仍为脱敏显示。
- 脱敏仅作用于输出，不改变续期使用的真实 IP、Server ID 或 `state.json` 数据。截图和 `state.json` 仍可能包含隐私信息，请勿公开分享。

---

## 使用

1. Fork 本仓库
2. 逐个配置 `HIDEN_ACCOUNT_n` 与配套的 `PROXY_URL_n`，按需配置通知变量
3. 在 Actions 菜单里手动触发 `workflow_dispatch` 跑一次，确认能正常登录续期（首次运行会为所有账号补全 `state.json`）
4. 之后保持默认的每日 2 次定时即可，脚本会自行判断哪些账号需要续期

### state.json 说明

```json
{
  "ACCOUNT_1": {
    "service_id": "218079",
    "due_date": "27 Oct 2026",
    "due_ts": 1793030400,
    "updated_at": "2026-10-10 12:00:00",
    "last_result": "success"
  }
}
```

- `due_date` / `due_ts`：当前到期时间，用于判断是否需要续期
- `last_result`：上次结果记录（`success` / `failed` / `not_time`），仅用于汇总展示
- 续期失败不冻结账号：没有跨运行退避，随时可以手动触发 workflow 重跑；重试都发生在本次运行内

### 运行产物

- 截图按账号归类在 `art/acc_n/` 下，并作为 artifact 上传，便于排查是哪个账号出问题
- `state.json` 会自动提交回仓库

---

**⚠️ 免责声明**：本脚本仅供学习交流使用，使用者需遵守 [HidenCloud](https://hidencloud.com) 的服务条款。因使用本脚本造成的任何问题，作者不承担任何责任。
