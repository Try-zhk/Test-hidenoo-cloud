import base64
import html as html_mod
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests

try:
    from patchright.sync_api import sync_playwright

    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright

    USING_PATCHRIGHT = False


TG_TOKEN = os.environ.get("TG_TOKEN") or os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT = os.environ.get("TG_CHAT") or os.environ.get("TG_CHAT_ID") or ""
SMTP_CONFIG = os.environ.get("SMTP_CONFIG") or ""
EMAIL_CHAT = os.environ.get("EMAIL_CHAT") or ""
WXPUSH_API = os.environ.get("WXPUSH_API") or ""
WXPUSH_TOKEN = os.environ.get("WXPUSH_TOKEN") or ""


EMAIL = ""
PASSWORD = ""
CURRENT_ACCOUNT = {}
LOGIN_METHOD = "未知"

BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"


IS_PROXY = False
PROXY_SERVER = ""
PROXY_PORT_BASE = 10800
SINGBOX_BIN = os.environ.get("SINGBOX_BIN") or os.path.join(os.getcwd(), "sing-box")
SINGBOX_CONFIG = os.environ.get("SINGBOX_CONFIG") or os.path.join(
    os.getcwd(), "config.json"
)
SINGBOX_LOG = os.path.join(os.getcwd(), "singbox.log")
STATE_FILE = os.path.join(os.getcwd(), "state.json")

DUE_WITHIN_HOURS = float(os.environ.get("DUE_WITHIN_HOURS") or 24)

RETRY_AFTER_HOURS = float(os.environ.get("RETRY_AFTER_HOURS") or 6)
SINGBOX_PROCESS = None


CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'


if sys.platform == "win32":
    DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
else:
    DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def log(message):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {redact_sensitive_text(message)}",
        flush=True,
    )


def screenshot(page, name):

    try:
        acc_id = CURRENT_ACCOUNT.get("id") or "0"
        out_dir = os.path.join(os.getcwd(), "art", f"acc_{acc_id}")
        os.makedirs(out_dir, exist_ok=True)
        page.screenshot(path=os.path.join(out_dir, f"{name}.png"))
    except Exception as e:
        log(f"🔍 截图失败: {e}")


def mask_email(email):

    if not email or "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    masked_name = (name[:3] + "***") if len(name) > 3 else (name + "***")
    tld = domain.split(".")[-1] if "." in domain else "com"
    return f"{masked_name}@***.{tld}"


def mask_ip(ip):
    parts = str(ip or "").strip().split(".")
    if len(parts) == 4 and all(
        part.isascii() and part.isdecimal() and 0 <= int(part) <= 255 for part in parts
    ):
        return f"{parts[0]}.*.*.{parts[3]}"
    return "***"


def mask_server_id(server_id):
    value = str(server_id or "").strip()
    if not value.isascii() or not value.isdecimal() or len(value) <= 4:
        return "*" * len(value) if value.isdecimal() else "***"
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def redact_sensitive_text(text):
    value = str(text or "")
    for secret in (TG_TOKEN, WXPUSH_TOKEN):
        if secret:
            value = value.replace(secret, "***")
    value = re.sub(
        r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])",
        lambda match: mask_ip(match.group()),
        value,
    )
    return re.sub(
        r"(/service/|Server ID:\s*|Free Server\s*#)(\d+)(?![\d*])",
        lambda match: match.group(1) + mask_server_id(match.group(2)),
        value,
        flags=re.IGNORECASE,
    )


def escape_html(text):
    return html_mod.escape(redact_sensitive_text(text), quote=False)


def get_accounts():

    accounts = []
    for key in list(os.environ.keys()):
        match = re.match(r"^HIDEN_ACCOUNT_(\d+)$", key)
        if not match:
            continue
        acc_id = match.group(1)
        raw = (os.environ.get(key) or "").strip()
        parts = re.split(r"\s+", raw)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            log(f"⚠️ HIDEN_ACCOUNT_{acc_id} 格式应为『邮箱 密码』，已跳过")
            continue
        accounts.append(
            {
                "id": acc_id,
                "username": parts[0],
                "password": parts[1],
                "proxy_url": (os.environ.get(f"PROXY_URL_{acc_id}") or "").strip(),
                "proxy_lock": os.environ.get(f"PROXY_LOCK_{acc_id}", "true").lower()
                != "false",
                "port": PROXY_PORT_BASE + int(acc_id),
            }
        )
    accounts.sort(key=lambda a: int(a["id"]))

    if not accounts and os.environ.get("EMAIL") and os.environ.get("PASSWORD"):
        accounts.append(
            {
                "id": "1",
                "username": (os.environ.get("EMAIL") or "").strip(),
                "password": (os.environ.get("PASSWORD") or "").strip(),
                "proxy_url": (os.environ.get("PROXY_URL") or "").strip(),
                "proxy_lock": os.environ.get("PROXY_LOCK", "true").lower() != "false",
                "port": PROXY_PORT_BASE + 1,
                "legacy": True,
            }
        )
    return accounts


def validate_proxy_isolation(accounts):

    problems = []
    missing = [a["id"] for a in accounts if not a["proxy_url"]]
    if missing:
        problems.append(
            "以下账号未配置代理（环境隔离要求每个账号必须有自己的 PROXY_URL_n）："
            + ", ".join(f"账号 {i} 缺少 PROXY_URL_{i}" for i in missing)
        )

    seen = {}
    for acc in accounts:
        url = acc["proxy_url"]
        if url:
            seen.setdefault(url, []).append(acc["id"])
    for url, ids in seen.items():
        if len(ids) > 1:
            problems.append(
                f"以下账号共用了同一个代理（禁止同代理多账号）：账号 {'、'.join(ids)}"
            )
    return problems


def accounts_to_renew(accounts, state):

    now_ts = int(time.time())
    threshold = DUE_WITHIN_HOURS * 3600
    pending = []
    for acc in accounts:
        acc_state = state.get(f"ACCOUNT_{acc['id']}") or {}
        last_failed = acc_state.get("last_result") == "failed"
        retry_at = acc_state.get("retry_at") or 0

        if last_failed and retry_at and now_ts < retry_at:
            log(
                f"⏳ 账号 {acc['id']} 上次续期失败，退避中（{int((retry_at - now_ts) / 60)} 分钟后重试）"
            )
            continue

        due_ts = acc_state.get("due_ts") or 0
        due_date = acc_state.get("due_date") or "未知"
        if not due_ts:

            log(f"🆕 账号 {acc['id']} 在 state.json 中无到期记录，纳入本次运行")
            pending.append(acc["id"])
            continue
        remaining = due_ts - now_ts
        if remaining <= threshold:
            hours = remaining / 3600
            log(
                f"⏰ 账号 {acc['id']} 距离到期 {hours:.1f} 小时（到期日 {due_date}），需要续期"
            )
            pending.append(acc["id"])
        else:
            log(
                f"✅ 账号 {acc['id']} 距离到期 {remaining / 3600:.1f} 小时（到期日 {due_date}），本次跳过"
            )
    return pending


def _parse_proxy_outbound(url):

    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:

        try:
            padded = url.replace("-", "+").replace("_", "/")
            padded += "=" * ((4 - len(padded) % 4) % 4)
            decoded = base64.b64decode(padded).decode("utf-8", "ignore")
            url = next((l.strip() for l in decoded.splitlines() if "://" in l), "")
        except Exception:
            return None
    if "://" not in url:
        return None

    scheme = url.split("://")[0].lower()
    try:
        if scheme == "vmess":
            payload = url.split("://", 1)[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            cfg = json.loads(base64.b64decode(payload).decode("utf-8", "ignore"))
            out = {
                "type": "vmess",
                "server": cfg.get("add", ""),
                "server_port": int(cfg.get("port") or 443),
                "uuid": cfg.get("id", ""),
                "security": cfg.get("scy") or "auto",
                "alter_id": int(cfg.get("aid") or 0),
            }
            tls_flag = cfg.get("tls") or ""
            if tls_flag in ("tls", "anytls") or cfg.get("sni"):
                tls = {"enabled": True}
                if cfg.get("sni") or cfg.get("host"):
                    tls["server_name"] = cfg.get("sni") or cfg.get("host")
                if cfg.get("alpn"):
                    tls["alpn"] = [a for a in str(cfg["alpn"]).split(",") if a]
                if tls_flag == "anytls":
                    tls["insecure"] = True
                out["tls"] = tls
            if cfg.get("net") == "ws":
                transport = {"type": "ws"}
                if cfg.get("path"):
                    transport["path"] = cfg["path"]
                if cfg.get("host"):
                    transport["headers"] = {"Host": cfg["host"]}
                out["transport"] = transport
            return out

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        get = lambda k, d="": (params.get(k, [d]) or [d])[0]

        if scheme in ("socks5", "socks", "socks5h"):
            out = {
                "type": "socks",
                "server": parsed.hostname,
                "server_port": int(parsed.port or 1080),
                "version": "5",
            }
            if parsed.username:
                out["username"] = unquote(parsed.username)
            if parsed.password:
                out["password"] = unquote(parsed.password)
            return out

        if scheme in ("http", "https"):
            out = {
                "type": "http",
                "server": parsed.hostname,
                "server_port": int(parsed.port or (443 if scheme == "https" else 8080)),
            }
            if parsed.username:
                out["username"] = unquote(parsed.username)
            if parsed.password:
                out["password"] = unquote(parsed.password)
            if scheme == "https":
                out["tls"] = {"enabled": True}
            return out

        if scheme == "vless":
            out = {
                "type": "vless",
                "server": parsed.hostname,
                "server_port": int(parsed.port or 443),
                "uuid": unquote(parsed.username or ""),
            }
            flow = get("flow")
            if flow:
                out["flow"] = flow
            security = get("security")
            if security in ("tls", "reality", "anytls"):
                tls = {"enabled": True}
                if get("sni"):
                    tls["server_name"] = get("sni")
                if get("alpn"):
                    tls["alpn"] = [a for a in get("alpn").split(",") if a]
                if get("fp"):
                    tls["utls"] = {"enabled": True, "fingerprint": get("fp")}
                if (
                    get("insecure") in ("1", "true")
                    or get("allowInsecure") in ("1", "true")
                    or security == "anytls"
                ):
                    tls["insecure"] = True
                if security == "reality":
                    reality = {"enabled": True}
                    if get("pbk"):
                        reality["public_key"] = get("pbk")
                    if get("sid"):
                        reality["short_id"] = get("sid")
                    tls["reality"] = reality
                out["tls"] = tls
            if get("type") == "ws":
                transport = {"type": "ws"}
                if get("path"):
                    transport["path"] = unquote(get("path"))
                if get("host"):
                    transport["headers"] = {"Host": get("host")}
                out["transport"] = transport
            return out

        if scheme == "trojan":
            out = {
                "type": "trojan",
                "server": parsed.hostname,
                "server_port": int(parsed.port or 443),
                "password": unquote(parsed.username or ""),
            }
            if get("security", "tls") in ("tls", "anytls"):
                tls = {"enabled": True}
                if get("sni"):
                    tls["server_name"] = get("sni")
                if get("alpn"):
                    tls["alpn"] = [a for a in get("alpn").split(",") if a]
                if (
                    get("insecure") in ("1", "true")
                    or get("allowInsecure") in ("1", "true")
                    or get("security") == "anytls"
                ):
                    tls["insecure"] = True
                out["tls"] = tls
            if get("type") == "ws":
                transport = {"type": "ws"}
                if get("path"):
                    transport["path"] = unquote(get("path"))
                if get("host"):
                    transport["headers"] = {"Host": get("host")}
                out["transport"] = transport
            return out

        if scheme in ("hy2", "hysteria2"):
            out = {
                "type": "hysteria2",
                "server": parsed.hostname,
                "server_port": int(parsed.port or 443),
                "password": unquote(parsed.username or ""),
            }
            tls = {"enabled": True}
            if get("sni"):
                tls["server_name"] = get("sni")
            if get("alpn"):
                tls["alpn"] = [a for a in get("alpn").split(",") if a]
            if get("insecure") in ("1", "true") or get("allowInsecure") in (
                "1",
                "true",
            ):
                tls["insecure"] = True
            out["tls"] = tls
            return out

        if scheme == "tuic":
            user_part = unquote(parsed.username or "")
            pass_part = unquote(parsed.password or "")
            if ":" in user_part and not pass_part:
                uuid, password = user_part.split(":", 1)
            else:
                uuid, password = user_part, pass_part
            out = {
                "type": "tuic",
                "server": parsed.hostname,
                "server_port": int(parsed.port or 443),
                "uuid": uuid,
                "password": password,
                "congestion_control": get("congestion_control", "bbr"),
            }
            tls = {"enabled": True}
            if get("sni"):
                tls["server_name"] = get("sni")
            if get("alpn"):
                tls["alpn"] = [a for a in get("alpn").split(",") if a]
            if get("insecure") in ("1", "true") or get("allowInsecure") in (
                "1",
                "true",
            ):
                tls["insecure"] = True
            out["tls"] = tls
            return out

        if scheme in ("ss", "shadowsocks"):
            user_part = unquote(parsed.username or "")
            pass_part = unquote(parsed.password or "")
            auth = f"{user_part}:{pass_part}" if pass_part else user_part
            method, password = user_part, pass_part
            try:
                padded = auth.replace("-", "+").replace("_", "/")
                padded += "=" * ((4 - len(padded) % 4) % 4)
                decoded = base64.b64decode(padded).decode("utf-8", "ignore")
                if ":" in decoded:
                    method, password = decoded.split(":", 1)
            except Exception:
                pass
            return {
                "type": "shadowsocks",
                "server": parsed.hostname,
                "server_port": int(parsed.port or 8388),
                "method": method,
                "password": password,
            }
    except Exception as e:
        log(f"⚠️ 解析代理链接失败: {e}")
    return None


def start_singbox(accounts):

    global SINGBOX_PROCESS
    proxied = [a for a in accounts if a.get("proxy_url")]
    if not proxied:
        return

    if not os.path.exists(SINGBOX_BIN) and not shutil.which(SINGBOX_BIN):
        log(f"⚠️ 未找到 sing-box 可执行文件（{SINGBOX_BIN}），账号级代理将被跳过")
        for a in proxied:
            a["proxy_ready"] = False
        return

    inbounds, outbounds, rules = [], [], []
    for acc in proxied:
        outbound = _parse_proxy_outbound(acc["proxy_url"])
        tag = f"proxy_{acc['id']}"
        if not outbound:
            log(
                f"⚠️ 账号 {acc['id']} 的 PROXY_URL_{acc['id']} 解析失败，该账号将不使用代理"
            )
            acc["proxy_ready"] = False
            continue
        outbound["tag"] = tag
        outbounds.append(outbound)

        inbounds.append(
            {
                "type": "mixed",
                "tag": f"in_{acc['id']}",
                "listen": "127.0.0.1",
                "listen_port": acc["port"],
            }
        )
        rules.append({"inbound": [f"in_{acc['id']}"], "outbound": tag})
        acc["proxy_ready"] = True

    if not inbounds:
        return

    config = {
        "log": {"level": "fatal", "timestamp": True},
        "inbounds": inbounds,
        "outbounds": outbounds + [{"type": "direct", "tag": "direct"}],
        "route": {"rules": rules},
    }
    try:
        with open(SINGBOX_CONFIG, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"❌ 写入 sing-box 配置失败: {e}")
        for acc in proxied:
            acc["proxy_ready"] = False
        return

    try:
        SINGBOX_PROCESS = subprocess.Popen(
            [SINGBOX_BIN, "run", "-c", SINGBOX_CONFIG],
            stdout=open(SINGBOX_LOG, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(3)
        if SINGBOX_PROCESS.poll() is not None:
            log("❌ sing-box 启动后立即退出，请查看 singbox.log")
            SINGBOX_PROCESS = None
            for acc in proxied:
                acc["proxy_ready"] = False
            return
        log(
            f"✅ sing-box 已启动，代理端口: {', '.join(str(a['port']) for a in proxied if a.get('proxy_ready'))}"
        )
    except Exception as e:
        log(f"❌ 启动 sing-box 失败: {e}")
        for acc in proxied:
            acc["proxy_ready"] = False


def stop_singbox():
    global SINGBOX_PROCESS
    if not SINGBOX_PROCESS:
        return
    try:
        SINGBOX_PROCESS.terminate()
        SINGBOX_PROCESS.wait(timeout=10)
    except Exception:
        try:
            SINGBOX_PROCESS.kill()
        except Exception:
            pass
    SINGBOX_PROCESS = None
    try:
        if os.path.exists(SINGBOX_CONFIG):
            os.remove(SINGBOX_CONFIG)
    except Exception:
        pass


def get_current_ip(proxy_server=None):

    use_proxy = bool(proxy_server) and (
        IS_PROXY or proxy_server.startswith("http://127.0.0.1")
    )
    proxies = {"http": proxy_server, "https": proxy_server} if use_proxy else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)

        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"⚠️ 读取 state.json 失败: {e}")
    return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log(f"💾 已更新 state.json")
    except Exception as e:
        log(f"⚠️ 写入 state.json 失败: {e}")


MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ]
    )
}


def parse_due_date(text):

    if not text:
        return 0
    match = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})", str(text))
    if not match:
        return 0
    day, mon, year = match.group(1), match.group(2)[:3].lower(), match.group(3)
    if mon not in MONTHS:
        return 0
    try:
        import calendar

        return int(
            calendar.timegm(
                time.strptime(
                    f"{year}-{MONTHS[mon]:02d}-{int(day):02d} 00:00:00",
                    "%Y-%m-%d %H:%M:%S",
                )
            )
        )
    except Exception:
        return 0


def post_notification(url, payload, headers=None):
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        return response


def send_telegram_notification(summary):

    if not TG_TOKEN or not TG_CHAT:
        log("⚠️ Telegram 未配置，跳过通知")
        return False

    sep = "━━━━━━━━━━━━━━━━━━"
    blocks = []
    for item in summary:
        if item["failed"]:
            ok, skip, fail = 0, 0, 1
        elif str(item["status"]).startswith("✅"):
            ok, skip, fail = 1, 0, 0
        else:
            ok, skip, fail = 0, 1, 0
        due = item["new_due"] if ok else item["old_due"]
        blocks.append(
            "\n".join(
                [
                    f"👤 <b>账号:</b> {html_mod.escape(str(item.get('full_user') or item['user']), quote=False)}",
                    f"🌐 <b>出口IP:</b> {html_mod.escape(str(item['ip']), quote=False)}",
                    f"🔑 <b>登录:</b> {html_mod.escape(str(item['login_method']), quote=False)}",
                    f"⚡ <b>续期:</b> {ok} 成功 / {skip} 未到期 / {fail} 失败",
                    f"📅 <b>到期:</b> {html_mod.escape(str(due), quote=False)}",
                ]
            )
        )

    text = (
        "☁️ <b>HidenCloud 自动续期报告</b>\n"
        + sep
        + "\n"
        + f"\n{sep}\n".join(blocks)
        + f"\n{sep}"
    )

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
    try:
        resp = post_notification(url, payload)
        data = resp.json()
        if isinstance(data, dict) and data.get("ok") is True:
            log("✅ Telegram 通知发送成功")
            return True
        log(f"❌ Telegram 通知失败: {resp.text}")
        return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False


def get_smtp_config():

    raw = (SMTP_CONFIG or "").strip()
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
        if cfg.get("host"):
            return cfg
    except Exception:
        pass
    try:
        host = re.search(r"""host['"]?\s*:\s*['"]([^'"]+)['"]""", raw)
        port = re.search(r"""port['"]?\s*:\s*(\d+)""", raw)
        user = re.search(r"""user['"]?\s*:\s*['"]([^'"]+)['"]""", raw)
        pwd = re.search(r"""pass(?:word)?['"]?\s*:\s*['"]([^'"]+)['"]""", raw)
        if host and user and pwd:
            return {
                "host": host.group(1),
                "port": int(port.group(1)) if port else 587,
                "user": user.group(1),
                "pass": pwd.group(1),
            }
    except Exception:
        pass
    return None


def send_email_notification(summary):

    smtp = get_smtp_config()
    if not smtp or not EMAIL_CHAT:
        log("⏭️ 未配置 SMTP_CONFIG / EMAIL_CHAT，跳过邮件通知")
        return False

    blocks = []
    for item in summary:
        color = "#e74c3c" if item["failed"] else "#2ecc71"
        body = (
            (
                f"<p style='margin:5px 0;font-size:15px;color:{color};'>"
                f"{escape_html(item['status'])}</p>"
            )
            if item["failed"]
            else (
                f"<p style='margin:5px 0;font-size:15px;'>{escape_html(item['status'])}</p>"
                f"<p style='margin:5px 0;font-size:15px;'>📅 <b>续期前:</b> {escape_html(item['old_due'])}"
                f" → <b>续期后:</b> {escape_html(item['new_due'])}</p>"
                f"<p style='margin:5px 0;font-size:15px;'>🌐 <b>IP:</b> {escape_html(mask_ip(item['ip']))}</p>"
            )
        )
        blocks.append(
            f"<div style='background:#fff;padding:15px;border-radius:8px;margin-bottom:15px;"
            f"border-left:5px solid {color};box-shadow:0 2px 4px rgba(0,0,0,0.05);'>"
            f"<p style='margin:5px 0;font-size:16px;'>👤 <b>账号:</b> {escape_html(item['user'])}</p>"
            f"<p style='margin:5px 0;font-size:15px;color:#7f8c8d;'>🔑 <b>登录:</b> {escape_html(item['login_method'])}</p>"
            f"{body}</div>"
        )

    html_body = (
        "<div style='font-family:Arial,sans-serif;max-width:650px;margin:auto;border:1px solid #e0e0e0;"
        "border-radius:10px;overflow:hidden;box-shadow:0 4px 6px rgba(0,0,0,0.1);'>"
        "<div style='background-color:#2c3e50;padding:20px;text-align:center;'>"
        "<h2 style='color:#fff;margin:0;font-size:24px;'>☁️ HidenCloud 自动续期</h2></div>"
        "<div style='padding:20px;background-color:#fcfcfc;'>"
        + "".join(blocks)
        + "</div></div>"
    )

    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.header import Header
    from email.utils import formataddr
    import smtplib

    msg = MIMEMultipart()
    msg["From"] = formataddr((str(Header("HidenCloud", "utf-8")), smtp["user"]))
    msg["To"] = EMAIL_CHAT
    msg["Subject"] = Header("☁️ HidenCloud 自动续期报告", "utf-8")
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    host, port = smtp["host"], int(smtp.get("port") or 587)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        server.login(smtp["user"], smtp["pass"])
        server.sendmail(smtp["user"], [EMAIL_CHAT], msg.as_string())
        server.quit()
        log("✅ 邮件通知已发送")
        return True
    except Exception as e:
        log(f"❌ 邮件通知失败: {e}")
        return False


def send_wxpush_notification(summary):

    if not WXPUSH_API or not WXPUSH_TOKEN:
        log("⏭️ 未配置 WXPUSH_API / WXPUSH_TOKEN，跳过 WxPush 通知")
        return False

    lines = []
    for item in summary:
        header = f"👤 {item['user']}  🔑 {item['login_method']}"
        if item["failed"]:
            lines.append(f"{header}\n❌ 异常: {item['status']}")
        else:
            lines.append(
                f"{header}\n{item['status']}\n📅 {item['old_due']} → {item['new_due']}"
            )

    try:
        resp = post_notification(
            f"{WXPUSH_API.rstrip('/')}/wxsend",
            payload={
                "title": "☁️ HidenCloud 自动续期报告",
                "content": redact_sensitive_text("\n──────────────\n".join(lines)),
            },
            headers={"Authorization": WXPUSH_TOKEN, "Content-Type": "application/json"},
        )
        log(f"✅ WxPush 通知已发送: {resp.status_code}")
        return True
    except Exception as e:
        log(f"❌ WxPush 通知失败: {e}")
        return False


def send_notifications(summary):
    send_telegram_notification(summary)
    send_email_notification(summary)
    send_wxpush_notification(summary)


STEALTH_JS = """
try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
} catch (e) {}
"""


def _get_turnstile_token(page):

    try:
        return page.evaluate("""
            () => {
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        let t = null;
                        try { t = window.turnstile.getResponse(); } catch (e) {}
                        if (t && t.length > 30) return t;
                        for (let i = 0; i < 5; i++) {
                            try { t = window.turnstile.getResponse(String(i)); } catch (e) { t = null; }
                            if (t && t.length > 30) return t;
                        }
                    }
                } catch (e) {}
                const inputs = document.querySelectorAll(
                    'input[name="cf-turnstile-response"], input[id$="_response"]'
                );
                for (const el of inputs) {
                    if (el.value && el.value.length > 30) return el.value;
                }
                return null;
            }
            """)
    except Exception:
        return None


def _find_visible_cf_frames(page):

    result = []
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            try:
                box = handle.bounding_box()
                if box and box["width"] > 10 and box["height"] > 10:
                    result.append(handle)
            except Exception:
                continue
    except Exception:
        pass
    return result


def _cf_checkbox_visible(page):

    for frame in page.frames:
        if "challenges.cloudflare.com" not in (frame.url or ""):
            continue
        try:
            if frame.locator('input[type="checkbox"]:visible').count() > 0:
                return True
        except Exception:
            continue
    return False


def _click_cf_checkbox(page, frame_el=None):

    for frame in page.frames:
        if "challenges.cloudflare.com" not in (frame.url or ""):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            return True
        except Exception:
            continue

    if frame_el is None:
        return False
    try:
        try:
            frame_el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        box = frame_el.bounding_box()
        if not box:
            return False
        x = box["x"] + random.uniform(18, 34)
        y = box["y"] + box["height"] / 2 + random.uniform(-4, 4)
        page.mouse.move(x - random.uniform(30, 60), y + random.uniform(-8, 8))
        time.sleep(random.uniform(0.15, 0.4))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.2))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.1))
        page.mouse.up()
        return True
    except Exception:
        return False


SECURITY_TITLE_HINTS = (
    "security verification",
    "just a moment",
    "attention required",
    "checking your browser",
    "请稍候",
    "please wait",
    "one more step",
)


def _is_security_check_page(page):
    try:
        title = (page.title() or "").lower()
        if any(h in title for h in SECURITY_TITLE_HINTS):
            return True
    except Exception:
        return True
    try:
        return page.evaluate("""
            () => {
                const t = document.body ? document.body.innerText.slice(0, 5000).toLowerCase() : '';
                return t.includes('verify you are human') || t.includes('checking your browser')
                    || t.includes('security verification');
            }
            """)
    except Exception:
        return True


def _has_interstitial_iframe(page):

    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            if "/turnstile/" not in (handle.get_attribute("src") or ""):
                return True
    except Exception:
        pass
    return False


def _click_security_submit(page):

    for text in ("Verify", "Continue", "Submit", "Proceed", "验证"):
        try:
            btn = page.locator(
                f'button:has-text("{text}"):visible, input[type="submit"]:visible'
            ).first
            if btn.count() > 0:
                btn.click(timeout=3000)
                log(f"🖱️ 已点击安全验证页的确认按钮（{text}）...")
                return True
        except Exception:
            continue
    return False


def _dump_security_page(page):

    try:
        screenshot(page, "security_page")
        srcs = page.evaluate(
            "() => Array.from(document.querySelectorAll('iframe')).map(f => f.src).filter(Boolean)"
        )

        buttons = page.evaluate(
            "() => Array.from(document.querySelectorAll('button, input[type=submit], a.btn')).map(b => (b.innerText || b.value || '').trim()).filter(t => t && t.length < 40)"
        )

        body = page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 200) : ''"
        )

    except Exception as e:
        log(f"🔍 验证页信息采集失败: {e}")


def handle_cloudflare(page, timeout=240):
    def challenge_active():
        return _has_interstitial_iframe(page) or _is_security_check_page(page)

    if not challenge_active():
        for _ in range(3):
            time.sleep(1)
            if challenge_active():
                break
        else:
            return True

    log("🔒 检测到 Cloudflare 安全验证...")
    time.sleep(5)
    _dump_security_page(page)

    effective_timeout = timeout + (180 if timeout > 60 else 0)
    manual_hinted = False
    start_time = time.time()
    last_click = 0
    clear_rounds = 0
    while time.time() - start_time < effective_timeout:
        if not challenge_active():
            clear_rounds += 1
            if clear_rounds >= 2:

                return True
            time.sleep(1)
            continue
        clear_rounds = 0
        if (
            not manual_hinted
            and timeout > 60
            and time.time() - start_time > timeout - 30
        ):
            log("🤝 自动点击未能通过验证，脚本会继续等待...")
            manual_hinted = True
        if time.time() - last_click > 6:
            frames = _find_visible_cf_frames(page)
            if frames:
                log("🖱️ 点击 Turnstile 验证......")
                if _click_cf_checkbox(page, frames[-1]):
                    last_click = time.time()
                    time.sleep(random.uniform(3, 5))
                    continue
            elif _get_turnstile_token(page) and _is_security_check_page(page):

                if _click_security_submit(page):
                    last_click = time.time()
                    time.sleep(random.uniform(2, 4))
                    continue
        time.sleep(1)
    log("❌ 验证超时。")
    try:
        screenshot(page, "security_timeout")
    except Exception:
        pass
    return False


def solve_modal_turnstile(page, timeout=90):
    log("🛡️ 开始处理 Turnstile 验证...")
    start = time.time()
    last_click = 0
    clicks = 0
    no_frame_seconds = 0
    while time.time() - start < timeout:

        if _get_turnstile_token(page):
            log("✅ Turnstile 验证通过！")
            return True
        frames = _find_visible_cf_frames(page)
        if not frames:

            no_frame_seconds += 1
            if no_frame_seconds >= 20 and clicks == 0:
                log("ℹ️ 未检测到 Turnstile 验证框，无需验证。")
                return True
            time.sleep(1)
            continue
        no_frame_seconds = 0

        if (
            clicks > 0
            and time.time() - last_click > 4
            and not _cf_checkbox_visible(page)
        ):
            time.sleep(2)
            if not _cf_checkbox_visible(page):
                log("✅ Turnstile 复选框已消失，视为验证通过！")
                return True

        if time.time() - last_click > 6 and _cf_checkbox_visible(page):
            log(f"🖱️ 点击 Turnstile 复选框（第 {clicks + 1} 次）...")
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue

        if clicks == 0 and time.time() - start > 20 and time.time() - last_click > 6:
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        time.sleep(1)
    if _get_turnstile_token(page):
        log("✅ Turnstile 验证通过！")
        return True
    log("❌ Turnstile 验证超时。")
    return False


def open_browser(p, proxy_server=None):

    proxy_arg = {"server": proxy_server} if proxy_server else None
    if USING_PATCHRIGHT:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--disable-infobars"],
            proxy=proxy_arg,
        )
        ctx = browser.new_context(viewport=None, proxy=proxy_arg)
        page = ctx.new_page()
        return browser, page
    log(
        "⚠️ 未安装 patchright（建议 pip install patchright），退回原生 playwright，过 Cloudflare 能力较弱"
    )
    browser = p.chromium.launch(
        channel="chrome",
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )
    ctx = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=DEFAULT_UA,
        proxy=proxy_arg,
    )
    page = ctx.new_page()
    page.add_init_script(STEALTH_JS)
    return browser, page


def _is_logged_in(page):

    try:
        url = page.url or ""
        if "dash.hidencloud.com" not in url:
            return False
        if "auth/login" in url or _is_security_check_page(page):
            return False
        return True
    except Exception:
        return False


def login(page):
    global LOGIN_METHOD

    if not EMAIL or not PASSWORD:
        log("❌ 缺少账号密码，无法登录。")
        return False
    log("💣 尝试账号密码登录...")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page, timeout=240)
        time.sleep(2)

        log("⌨️ 输入账号密码...")

        user_input = page.locator(
            'input[name="username"], input[name="email"], input[type="email"]'
        ).first
        pwd_input = page.locator('input[name="password"], input[type="password"]').first
        try:

            user_input.wait_for(state="visible", timeout=30000)
        except Exception:
            handle_cloudflare(page, timeout=120)
            user_input.wait_for(state="visible", timeout=30000)
        user_input.fill(EMAIL, timeout=10000)
        pwd_input.fill(PASSWORD, timeout=10000)
        time.sleep(0.5)
        handle_cloudflare(page, timeout=60)

        if not solve_modal_turnstile(page, timeout=90):
            log("⚠️ 登录表单的 Turnstile 未确认通过，仍将尝试提交...")

        log("🖱️ 点击登录按钮提交...")
        try:
            page.click('button[type="submit"]', timeout=8000)
        except Exception:
            page.locator(
                'button:has-text("Sign in"), button:has-text("登录")'
            ).first.click(timeout=10000)
        time.sleep(3)
        handle_cloudflare(page, timeout=240)
        nav_start = time.time()
        navigated = False
        while time.time() - nav_start < 120:
            try:
                if "/auth/login" not in page.url and not _is_security_check_page(page):
                    navigated = True
                    break
                if _is_security_check_page(page) or _has_interstitial_iframe(page):
                    handle_cloudflare(page, timeout=60)
            except Exception:
                pass
            time.sleep(1)
        if not navigated:
            log("❌ 登录提交后未能完成跳转。")
            screenshot(page, "login_fail")
            return False
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page, timeout=120)
        page_title = page.title()
        log(f"📝 当前Title: {page_title}")
        if not _is_logged_in(page):
            log("❌ 登录失败。")
            return False
        LOGIN_METHOD = "密码验证 + CF盾"
        log(f"✅ 账号密码登录成功！当前已到达dashboard页面")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        screenshot(page, "login_fail")
        return False


def get_server_id(page):
    try:
        if "dash.hidencloud.com" not in page.url:
            log("⚠️ 当前不在控制台页面，先跳转 dashboard...")
            page.goto(
                f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000
            )
            handle_cloudflare(page)
            time.sleep(2)
        handle_cloudflare(page)
        time.sleep(3)
        html = page.content()
        log(f"📝 页面长度: {len(html)}, URL: {page.url}")

        matches = re.findall(r"/service/(\d+)/manage", html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从链接中获取到 Server ID: {mask_server_id(server_id)}")
            return server_id

        matches = [m for m in re.findall(r"#(\d{4,})", html) if set(m) != {"0"}]
        if matches:
            server_id = matches[0]
            log(f"✅ 从文本 #号中获取到 Server ID: {mask_server_id(server_id)}")
            return server_id

        log("❌ 所有 URL 均未找到 Server ID")
        return None
    except Exception as e:
        log(f"❌ 获取 Server ID 失败: {e}")
        screenshot(page, "server_id_error")
        return None


def get_due_date(page):
    try:
        if SERVICE_URL not in page.url:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        body_text = page.locator("body").inner_text()
        patterns = [
            r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date.*?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
            if match:
                due_date = match.group(1).strip()
                log(f"📅 获取到Due Date: {due_date}")
                return due_date
    except Exception as e:
        log(f"❌ 获取Due Date失败: {e}")
    return "未知"


def renew_service(page):

    try:
        log("➡ 进入续期流程...")
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)

        log("🖱️ 准备点击 Renew 按钮...")
        renew_btn = page.locator('button:has-text("Renew")')
        create_btn = page.locator('button:has-text("Create Invoice")')

        modal_opened = False
        for i in range(6):
            try:
                renew_btn.wait_for(state="visible", timeout=10000)
                renew_btn.scroll_into_view_if_needed()
                log(f"🖱️ 第 {i+1} 次尝试点击 'Renew'...")
                renew_btn.click()

                time.sleep(3)
                page_text = page.locator("body").inner_text()
                if (
                    "Renewal Restricted" in page_text
                    or "can only renew" in page_text.lower()
                ):
                    log("⚠️ 未到续期时间，无法续期。")
                    screenshot(page, "renew_not_allowed")
                    return "NOT_TIME"

                log("🖲️ 等待弹窗出现...")
                try:
                    create_btn.wait_for(state="visible", timeout=8000)
                    modal_opened = True
                    log("✅ 弹窗已成功弹出！")
                    break
                except:
                    log("⚠️ 弹窗未出现，可能是点击未响应，准备重试...")
                    time.sleep(2)
            except Exception as e:
                log(f"❌ 点击尝试出错: {e}")

        if not modal_opened:
            log("❌ 错误：尝试多次后，续费弹窗仍未出现。")
            screenshot(page, "renew_modal_failed")
            return False

        handle_cloudflare(page, timeout=60)
        if not solve_modal_turnstile(page, timeout=90):
            log("⚠️ Turnstile 未确认通过，仍将尝试提交...")
            screenshot(page, "turnstile_timeout")

        new_invoice_url = None
        for attempt in range(6):
            log(f"🖱️ 点击 'Create Invoice'（第 {attempt + 1} 次）...")
            try:
                create_btn.wait_for(state="visible", timeout=20000)
                create_btn.click(timeout=20000)
            except Exception as e:
                log(f"⚠️ 点击 Create Invoice 失败: {e}")
                solve_modal_turnstile(page, timeout=45)
                continue

            start_wait = time.time()
            while time.time() - start_wait < 30:
                if "/payment/invoice/" in page.url:
                    new_invoice_url = page.url
                    log(f"🎉 页面已跳转: {new_invoice_url}")
                    break

                if _has_interstitial_iframe(page) or _is_security_check_page(page):
                    log("⚠️ 遇到整页拦截，尝试处理...")
                    handle_cloudflare(page, timeout=60)
                time.sleep(1)
            if new_invoice_url:
                break

            log("⚠️ 未跳转到发票页面，尝试重新完成 Turnstile 验证后重试...")
            solve_modal_turnstile(page, timeout=45)

        if not new_invoice_url:
            log("❌ 未能进入发票页面，超时。")
            screenshot(page, "renew_stuck_invoice")
            return False

        if page.url != new_invoice_url:
            page.goto(new_invoice_url)
        handle_cloudflare(page)

        log("🔎 查找 Pay 按钮...")
        pay_btn = page.locator(
            'a:has-text("Pay"):visible, button:has-text("Pay"):visible'
        ).first
        pay_btn.wait_for(state="visible", timeout=30000)
        pay_btn.click()
        log("✅ Pay 按钮已点击")

        time.sleep(5)

        page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        screenshot(page, "renew_error")
        return False


def run_single_account(p, acc, state):

    global EMAIL, PASSWORD, CURRENT_ACCOUNT, SERVICE_URL, IS_PROXY, PROXY_SERVER, LOGIN_METHOD

    EMAIL = acc.get("username") or ""
    PASSWORD = acc.get("password") or ""
    CURRENT_ACCOUNT = acc
    LOGIN_METHOD = "未知"

    masked = mask_email(EMAIL) if EMAIL else f"账号{acc['id']}"
    result = {
        "id": acc["id"],
        "user": masked,
        "full_user": EMAIL,
        "login_method": "未登录",
        "status": "❌ 未执行",
        "old_due": "未知",
        "new_due": "未知",
        "ip": "未知",
        "failed": True,
    }

    log("===========================================")
    log(f"▶ 开始处理账号: {masked} (ID: {acc['id']})")

    if not acc.get("proxy_url"):
        log(
            f"🚫 账号 {acc['id']} 未配置 PROXY_URL_{acc['id']}，为满足环境隔离原则，放弃执行！"
        )
        result["status"] = f"❌ 失败 (缺少 PROXY_URL_{acc['id']})"
        return result
    if not acc.get("proxy_ready"):
        log(
            f"🚫 PROXY_URL_{acc['id']} 不可用，放弃执行当前账号（不允许改用其它代理）！"
        )
        result["status"] = "❌ 失败 (代理失效)"
        return result

    account_proxy = f"http://127.0.0.1:{acc['port']}"
    log(
        f"🌐 使用账号 {acc['id']} 专属代理: PROXY_URL_{acc['id']} → 127.0.0.1:{acc['port']}"
    )
    IS_PROXY = True
    PROXY_SERVER = account_proxy

    try:
        log(f"⚙️ 代理已启用: {PROXY_SERVER}")

        current_ip = get_current_ip(PROXY_SERVER)
        log(f"🎯 当前出口IP: {mask_ip(current_ip)}")
        result["ip"] = current_ip

        log("🚀 启动反检测内核浏览器...")
        browser, page = open_browser(p, account_proxy)

        try:
            if not login(page):
                log("❌ 登录失败，跳过该账号。")
                result["status"] = "❌ 登录失败"
                return result

            result["login_method"] = LOGIN_METHOD

            server_id = get_server_id(page)
            if not server_id:
                log("❌ 无法获取 Server ID，跳过该账号。")
                result["status"] = "❌ 无法获取 Server ID"
                return result
            SERVICE_URL = f"{BASE_URL}/service/{server_id}/manage"

            old_due = get_due_date(page)
            log(f"📆 续费前到期时间：{old_due}")
            result["old_due"] = old_due

            acc_state = state.setdefault(f"ACCOUNT_{acc['id']}", {})
            acc_state["service_id"] = server_id
            acc_state["due_date"] = old_due
            acc_state["due_ts"] = parse_due_date(old_due) or acc_state.get("due_ts", 0)
            acc_state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

            remaining = (
                acc_state["due_ts"] - int(time.time())
                if acc_state.get("due_ts")
                else None
            )
            if remaining is not None and remaining > DUE_WITHIN_HOURS * 3600:
                log(
                    f"⏭️ 距离到期还有 {remaining / 3600:.1f} 小时（>{DUE_WITHIN_HOURS:g}H），无需续期。"
                )
                result["status"] = "⏭️ 未到期，已跳过"
                result["new_due"] = old_due
                result["failed"] = False
                return result

            renew_result = renew_service(page)

            new_due = old_due
            if renew_result == "NOT_TIME":
                log("⏳ 未到续期时间，目前无法续期")
                result["status"] = "⏳ 未到续期时间"
                result["failed"] = False
                acc_state["last_result"] = "not_time"
                acc_state.pop("retry_at", None)
            elif renew_result is False:
                log("❌ 续费失败。")
                result["status"] = "❌ 续期失败"
                result["failed"] = True

                acc_state["last_result"] = "failed"
                acc_state["retry_at"] = int(time.time()) + RETRY_AFTER_HOURS * 3600
            else:
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")
                result["status"] = "✅ 续期成功"
                result["failed"] = False
                acc_state["due_date"] = new_due
                acc_state["due_ts"] = parse_due_date(new_due) or acc_state.get(
                    "due_ts", 0
                )
                acc_state["last_result"] = "success"
                acc_state.pop("retry_at", None)

            result["new_due"] = new_due

            return result
        finally:
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        log(f"❌ 浏览器启动出错: {e}")
        result["status"] = f"❌ 异常: {e}"
        acc_state = state.setdefault(f"ACCOUNT_{acc['id']}", {})
        acc_state["last_result"] = "failed"
        acc_state["retry_at"] = int(time.time()) + RETRY_AFTER_HOURS * 3600
        return result


def main():
    global SERVICE_URL

    state = load_state()
    accounts = get_accounts()

    log(f"🔍 凭证检测: 共发现 {len(accounts)} 个账号")
    for acc in accounts:
        log(
            f"   • 账号 {acc['id']}: EMAIL={'已配置' if acc['username'] else '未配置'}, "
            f"PASSWORD={'已配置' if acc['password'] else '未配置'}, "
            f"PROXY_URL_{acc['id']}={'已配置' if acc.get('proxy_url') else '未配置'}"
        )
    if not accounts:
        log("❌ 未检测到任何 HIDEN_ACCOUNT_X 环境变量（也没有 EMAIL/PASSWORD），退出。")
        sys.exit(1)

    problems = validate_proxy_isolation(accounts)
    if problems:
        log("🚫 环境隔离校验未通过，为保证账号安全，本次不执行任何账号：")
        for p in problems:
            log(f"   • {p}")
        sys.exit(1)
    log(f"✅ 环境隔离校验通过：{len(accounts)} 个账号各自使用独立代理")

    pending_ids = accounts_to_renew(accounts, state)
    if not pending_ids:
        log("⏭️ 所有账号均未到期，本次无需运行（不启动浏览器、不执行续期）。")
        sys.exit(0)
    pending = [a for a in accounts if a["id"] in pending_ids]
    log(f"🎯 本次需要续期的账号: {', '.join(a['id'] for a in pending)}")

    start_singbox(pending)

    summary = []
    exit_code = 0
    with sync_playwright() as p:
        try:
            for acc in pending:
                try:
                    result = run_single_account(p, acc, state)
                except Exception as e:
                    log(f"❌ 账号 {acc['id']} 处理异常: {e}")
                    result = {
                        "id": acc["id"],
                        "user": mask_email(acc.get("username", "")),
                        "full_user": acc.get("username", ""),
                        "login_method": "未知",
                        "status": f"❌ 异常: {e}",
                        "old_due": "未知",
                        "new_due": "未知",
                        "ip": "未知",
                        "failed": True,
                    }
                summary.append(result)
                if result.get("failed"):
                    exit_code = 1
        finally:
            stop_singbox()

    save_state(state)

    send_notifications(summary)

    log("═══════════ 运行汇总 ═══════════")
    for item in summary:
        log(
            f"账号 {item['id']} ({item['user']}): {item['status']} | {item['old_due']} → {item['new_due']}"
        )
    log("════════════════════════════════")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
