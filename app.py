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
import zlib
from urllib.parse import parse_qs, unquote, urlsplit

import requests

try:
    from patchright.sync_api import sync_playwright

    USING_PATCHRIGHT = True
except ImportError:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None

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


# ======== 代理链接 → sing-box outbound（来自 linktosb.py）========

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def b64decode(value):
    value = value.strip().replace("-", "+").replace("_", "/")
    value += "=" * (-len(value) % 4)
    return base64.b64decode(value).decode("utf-8", errors="replace")


def query_value(query, key, default=""):
    return query.get(key, [default])[0] or default


def bool_value(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def split_host_port(value):
    """host:port / [ipv6]:port"""
    value = (value or "").strip().rstrip("/")
    if not value:
        raise ValueError("空的 host:port")

    if value.startswith("["):
        if "]:" not in value:
            raise ValueError("IPv6 地址缺少端口: " + value)
        host, port = value.rsplit("]:", 1)
        return host[1:], int(port)

    # 仅最后一个 : 作为端口分隔（兼容域名/IPv4；无括号 IPv6 需带 []）
    if value.count(":") == 1 or (value.count(":") > 1 and value.rsplit(":", 1)[-1].isdigit()):
        host, port = value.rsplit(":", 1)
        if host.count(":") >= 2 and not host.startswith("["):
            # 裸 IPv6 无端口
            raise ValueError("IPv6 请使用 [addr]:port 形式: " + value)
        return host, int(port)

    raise ValueError("无法解析 host:port: " + value)


def tag_from_url(parts, default):
    name = unquote(parts.fragment).strip()
    return name or default


def ensure_tag(outbound, default="proxy"):
    if not isinstance(outbound, dict):
        raise ValueError("outbound 必须是对象")
    tag = outbound.get("tag")
    if not tag:
        outbound["tag"] = default
    return outbound


def normalize_link(link):
    link = (link or "").strip()
    if not link:
        raise ValueError("链接为空")
    return link


def scheme_of(link):
    return urlsplit(link).scheme.lower()


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

def _build_tls_base(query, server, default_insecure=False):
    """构建基础 TLS 属性（insecure / server_name / utls / alpn）。"""
    insecure_param = query_value(query, "allowInsecure") or query_value(query, "insecure")
    if insecure_param == "":
        insecure = default_insecure
    else:
        insecure = bool_value(insecure_param)

    tls = {
        "enabled": True,
        "server_name": query_value(query, "sni") or server,
        "insecure": insecure,
    }

    fingerprint = query_value(query, "fp")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}

    alpn = query_value(query, "alpn")
    if alpn:
        tls["alpn"] = [item.strip() for item in alpn.split(",") if item.strip()]

    return tls


def make_tls(query, server, default_insecure=False):
    """
    security=tls|reality 时生成 tls 块。
    insecure：显式参数为准；未写时默认 False（比旧版 True 更安全）。
    SNI：sni > server（不用 host 兜底，host 多为 WS Host）。
    """
    security = query_value(query, "security").lower()
    if security not in ("tls", "reality"):
        return None

    tls = _build_tls_base(query, server, default_insecure=default_insecure)

    if security == "reality":
        public_key = query_value(query, "pbk")
        short_id = query_value(query, "sid")
        if not public_key:
            raise ValueError("Reality 节点缺少 pbk 参数")
        tls["reality"] = {
            "enabled": True,
            "public_key": public_key,
            "short_id": short_id or "",
        }

    return tls


def force_tls(query, server, default_insecure=False):
    """trojan / anytls / hy2 等默认强制 TLS。"""
    tls = make_tls(query, server, default_insecure=default_insecure)
    if tls:
        return tls
    return _build_tls_base(query, server, default_insecure=default_insecure)

# ---------------------------------------------------------------------------
# Worker path：ed / proxyip / p / s / wk
# ---------------------------------------------------------------------------

def split_early_data(path, query=None):
    """
    只剥离 ed，保留 proxyip 等其余 query。
    顶层 query 的 ed 优先于 path 内 ed。
    """
    max_early_data = None

    if query is not None:
        ed_param = query_value(query, "ed")
        if ed_param:
            try:
                max_early_data = int(ed_param)
            except ValueError:
                max_early_data = None

    path = path or "/"
    if "?" not in path:
        return path, max_early_data

    base, _, qs = path.partition("?")

    m = re.search(r"(?:^|&)ed=(\d+)(?=&|$)", qs)
    if m and max_early_data is None:
        try:
            max_early_data = int(m.group(1))
        except ValueError:
            pass

    new_qs = re.sub(r"(?:^|&)ed=\d+(?=&|$)", "", qs)
    new_qs = re.sub(r"^&+|&+$", "", new_qs)
    new_qs = re.sub(r"&{2,}", "&", new_qs)

    if new_qs:
        clean = f"{base or '/'}?{new_qs}"
    else:
        clean = base or "/"

    return clean, max_early_data


def strip_inline_note(value):
    """去掉 path 内 /#备注 或尾部 #备注。"""
    value = (value or "").strip()
    if "/#" in value:
        value = value.split("/#", 1)[0]
    # socks URL 中 # 极少作密码；落地备注常见 /#name
    if value.endswith("#"):
        value = value[:-1]
    return value.rstrip("/")


def parse_path_params(path):
    """
    解析 Worker 风格 path，不用 parse_qs，避免破坏 :// @。
    支持：
      /?ed=2560&proxyip=socks5://u:p@h:port/
      /proxyip=1.2.3.4:81
      /p=1.1.1.1
      /?s=user:pass@host:1080&wk=us
    """
    path = unquote(path or "/")
    params = {}

    m = re.match(r"^/(proxyip|p|s|wk)=(.+)$", path, re.I)
    if m:
        params[m.group(1).lower()] = m.group(2).strip()
        return params

    if "?" not in path:
        return params

    _, _, qs = path.partition("?")
    for part in qs.split("&"):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        if k:
            params[k] = v.strip()
    return params


def parse_proxy_url_to_outbound(proxy_url, tag, default_socks_version="5"):
    """socks:// socks5:// http:// https:// 或 user:pass@host:port → outbound。"""
    raw = strip_inline_note(proxy_url)
    if not raw:
        raise ValueError("空的 proxyip")

    lower = raw.lower()
    version = default_socks_version

    if lower.startswith("socks5h://"):
        scheme, rest = "socks", raw[len("socks5h://") :]
        version = "5"
    elif lower.startswith("socks5://"):
        scheme, rest = "socks", raw[len("socks5://") :]
        version = "5"
    elif lower.startswith("socks4://"):
        scheme, rest = "socks", raw[len("socks4://") :]
        version = "4"
    elif lower.startswith("socks://"):
        scheme, rest = "socks", raw[len("socks://") :]
        version = default_socks_version
    elif lower.startswith("https://"):
        scheme, rest = "http", raw[len("https://") :]
    elif lower.startswith("http://"):
        scheme, rest = "http", raw[len("http://") :]
    elif "://" in raw:
        raise ValueError("不支持的 proxy 协议: " + raw.split("://", 1)[0])
    else:
        scheme, rest = "socks", raw
        version = default_socks_version


    rest = strip_inline_note(rest)
    username = password = ""

    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
        if ":" in userinfo:
            username, password = userinfo.split(":", 1)
        else:
            username = userinfo
    else:
        hostport = rest

    hostport = strip_inline_note(hostport)
    if "#" in hostport:
        hostport = hostport.split("#", 1)[0]

    host, port = split_host_port(hostport)

    if scheme == "socks":
        outbound = {
            "type": "socks",
            "tag": tag,
            "server": host,
            "server_port": port,
            "version": str(version),
        }
        if username:
            outbound["username"] = unquote(username)
        if password:
            outbound["password"] = unquote(password)
        return outbound

    outbound = {
        "type": "http",
        "tag": tag,
        "server": host,
        "server_port": port,
    }
    if username:
        outbound["username"] = unquote(username)
    if password:
        outbound["password"] = unquote(password)
    if lower.startswith("https://"):
        outbound["tls"] = {
            "enabled": True,
            "server_name": host,
            "insecure": False,
        }
    return outbound


def is_direct_proxy_url(value):
    if not value:
        return False
    low = value.lower()
    if low.startswith(("socks://", "socks5://", "socks5h://", "socks4://", "http://", "https://")):
        return True
    return False


def rewrite_worker_path(path, query=None):
    """
    返回 (clean_path, max_early_data, direct_proxy_url_or_None)

    - socks/http 类 proxyip 或 s= → direct_proxy_url，由调用方生成直连 outbound
    - 裸 IP/域名 → 无问号 path /proxyip=...，减轻 sing-box 对 ? 的编码问题
    - 无落地 → 仅剥 ed
    """
    path = unquote(path or "/")
    clean, max_early_data = split_early_data(path, query)
    params = parse_path_params(clean)

    socks_short = params.get("s")
    # p 与 proxyip：README 中 p 为 ProxyIP；proxyip 为完整写法
    proxyip = params.get("proxyip") or params.get("p")
    wk = params.get("wk")

    candidate = None
    if socks_short:
        if "://" not in socks_short:
            candidate = "socks5://" + strip_inline_note(socks_short)
        else:
            candidate = strip_inline_note(socks_short)
    elif proxyip:
        candidate = strip_inline_note(proxyip)

    if candidate:
        # 显式代理 URL 或 s= 用户信息 → 直连
        if is_direct_proxy_url(candidate) or socks_short:
            if not is_direct_proxy_url(candidate) and "@" in candidate:
                candidate = "socks5://" + candidate
            return "/", max_early_data, candidate

        # 裸 IP / 域名 / host:port → 无 ? 的 Worker path（p 优先于 wk）
        return "/proxyip=" + candidate, max_early_data, None

    if wk:
        # 仅地区码：无问号形式
        others = {k: v for k, v in params.items() if k not in ("ed", "wk")}
        if not others:
            return "/wk=" + wk, max_early_data, None
        # 仍有其它参数时尽量拼 query（少见）
        rest = "&".join(f"{k}={v}" for k, v in params.items() if k != "ed")
        return (("/?" + rest) if rest else "/"), max_early_data, None

    return clean, max_early_data, None

# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def make_transport(network, query):
    """
    返回 (transport_or_None, direct_proxy_url_or_None)
    direct_proxy 非空时：外层应直接生成 socks/http，不再使用 transport。
    """
    network = (network or "tcp").lower()

    if network in ("", "tcp", "none"):
        if query_value(query, "headerType").lower() == "http":
            path = unquote(query_value(query, "path", "/")) or "/"
            host = query_value(query, "host")
            transport = {"type": "http", "path": path}
            if host:
                transport["host"] = [h.strip() for h in host.split(",") if h.strip()]
            return transport, None
        return None, None

    path = unquote(query_value(query, "path", "/")) or "/"
    host = query_value(query, "host")

    if network == "ws":
        clean_path, max_early_data, direct_proxy = rewrite_worker_path(path, query)
        if direct_proxy:
            return None, direct_proxy
        transport = {
            "type": "ws",
            "path": clean_path or "/",
        }
        if host:
            transport["headers"] = {"Host": host}
        if max_early_data:
            transport["max_early_data"] = max_early_data
            transport["early_data_header_name"] = "Sec-WebSocket-Protocol"
        return transport, None

    if network in ("grpc", "gun"):
        service_name = (
            query_value(query, "serviceName")
            or query_value(query, "service_name")
            or unquote(query_value(query, "path", "")).strip("/")
        )
        return {"type": "grpc", "service_name": service_name}, None

    if network == "httpupgrade":
        clean_path, max_early_data, direct_proxy = rewrite_worker_path(path, query)
        if direct_proxy:
            return None, direct_proxy
        transport = {"type": "httpupgrade", "path": clean_path or "/"}
        if host:
            transport["host"] = host
        return transport, None

    if network in ("http", "h2"):
        transport = {"type": "http", "path": path}
        if host:
            transport["host"] = [h.strip() for h in host.split(",") if h.strip()]
        return transport, None

    return None, None


# ---------------------------------------------------------------------------
# 各协议
# ---------------------------------------------------------------------------

def parse_vless(link):
    parts = urlsplit(link)
    query = parse_qs(parts.query)

    if not parts.username or not parts.hostname or not parts.port:
        raise ValueError("VLESS 链接缺少 UUID、服务器或端口")

    tag = tag_from_url(parts, "vless")
    transport, direct_proxy = make_transport(query_value(query, "type"), query)

    if direct_proxy:
        return parse_proxy_url_to_outbound(direct_proxy, tag)

    outbound = {
        "type": "vless",
        "tag": tag,
        "server": parts.hostname,
        "server_port": int(parts.port),
        "uuid": unquote(parts.username),
        "packet_encoding": "xudp",
    }

    flow = query_value(query, "flow")
    if flow:
        outbound["flow"] = flow

    encryption = query_value(query, "encryption")
    if encryption and encryption != "none":
        # sing-box vless 通常无 encryption 字段；忽略 none 以外的兼容提示
        pass

    tls = make_tls(query, parts.hostname, default_insecure=False)
    if tls:
        outbound["tls"] = tls

    if transport:
        outbound["transport"] = transport

    return outbound


def parse_trojan(link):
    parts = urlsplit(link)
    query = parse_qs(parts.query)

    if not parts.username or not parts.hostname or not parts.port:
        raise ValueError("Trojan 链接缺少密码、服务器或端口")

    tag = tag_from_url(parts, "trojan")
    transport, direct_proxy = make_transport(query_value(query, "type"), query)
    if direct_proxy:
        return parse_proxy_url_to_outbound(direct_proxy, tag)

    outbound = {
        "type": "trojan",
        "tag": tag,
        "server": parts.hostname,
        "server_port": int(parts.port),
        "password": unquote(parts.username),
    }
    outbound["tls"] = force_tls(query, parts.hostname, default_insecure=False)
    if transport:
        outbound["transport"] = transport
    return outbound


def parse_anytls(link):
    parts = urlsplit(link)
    query = parse_qs(parts.query)

    if not parts.hostname or not parts.port:
        raise ValueError("AnyTLS 链接缺少服务器或端口")

    outbound = {
        "type": "anytls",
        "tag": tag_from_url(parts, "anytls"),
        "server": parts.hostname,
        "server_port": int(parts.port),
        "password": unquote(parts.username or ""),
    }
    outbound["tls"] = force_tls(query, parts.hostname, default_insecure=False)
    return outbound


def parse_ss(link):
    raw = link[len("ss://") :]
    raw, _, fragment = raw.partition("#")
    tag = unquote(fragment) or "shadowsocks"

    plugin = plugin_opts = ""
    if "?" in raw:
        raw, _, q = raw.partition("?")
        qmap = parse_qs(q)
        plugin = query_value(qmap, "plugin")
        # SIP002: plugin=name;opt=val
        if plugin and ";" in plugin:
            plugin, plugin_opts = plugin.split(";", 1)

    if "@" not in raw:
        try:
            raw = b64decode(raw)
        except Exception as exc:
            raise ValueError("Shadowsocks 链接解码失败") from exc

    if "@" not in raw:
        raise ValueError("Shadowsocks 链接格式无效")

    userinfo, address = raw.rsplit("@", 1)

    try:
        decoded = b64decode(userinfo)
        if ":" in decoded:
            userinfo = decoded
    except Exception:
        pass

    userinfo = unquote(userinfo)
    if ":" not in userinfo:
        raise ValueError("Shadowsocks 链接缺少加密方式或密码")

    method, password = userinfo.split(":", 1)
    server, port = split_host_port(unquote(address))

    outbound = {
        "type": "shadowsocks",
        "tag": tag,
        "server": server,
        "server_port": port,
        "method": method,
        "password": password,
    }

    # sing-box 插件名与 SIP002 不完全一致；有 plugin 时尽量映射常见项
    if plugin:
        name = plugin.lower()
        if "obfs" in name:
            # 简单兼容：无法可靠转成 sing-box 时保留注释字段不利于内核；跳过并警告
            print("警告: SS plugin 未映射到 sing-box outbound，已忽略: " + plugin, file=sys.stderr)
        elif "v2ray" in name:
            print("警告: SS v2ray-plugin 未映射，已忽略: " + plugin, file=sys.stderr)
        else:
            print("警告: 未知 SS plugin，已忽略: " + plugin, file=sys.stderr)

    return outbound


def parse_vmess(link):
    raw = link[len("vmess://") :]
    # 部分链接带 fragment
    raw = raw.split("#", 1)[0]
    data = json.loads(b64decode(raw))

    host = data.get("add")
    uuid = data.get("id")
    if not host or not uuid:
        raise ValueError("VMess 链接缺少服务器或 UUID")

    try:
        port = int(data.get("port", 443))
    except (TypeError, ValueError) as exc:
        raise ValueError("VMess 端口无效") from exc

    tag = data.get("ps") or "vmess"
    path = data.get("path") or "/"
    host_header = data.get("host") or ""
    network = (data.get("net") or "tcp").lower()

    # vmess path 也可能带 worker 落地
    if network == "ws":
        clean_path, max_early_data, direct_proxy = rewrite_worker_path(path, None)
        if direct_proxy:
            return parse_proxy_url_to_outbound(direct_proxy, tag)
    else:
        clean_path, max_early_data, direct_proxy = path, None, None

    outbound = {
        "type": "vmess",
        "tag": tag,
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "security": data.get("scy") or "auto",
        "alter_id": int(data.get("aid") or 0),
        "packet_encoding": "xudp",
    }

    tls_mode = str(data.get("tls") or "").lower()
    if tls_mode in ("tls", "reality"):
        insecure = bool_value(data.get("allowInsecure") or data.get("insecure") or "")
        tls = {
            "enabled": True,
            "server_name": data.get("sni") or host,
            "insecure": insecure,
        }
        if data.get("fp"):
            tls["utls"] = {"enabled": True, "fingerprint": data["fp"]}
        if tls_mode == "reality":
            if not data.get("pbk"):
                raise ValueError("VMess Reality 缺少 pbk")
            tls["reality"] = {
                "enabled": True,
                "public_key": data.get("pbk", ""),
                "short_id": data.get("sid", "") or "",
            }
        outbound["tls"] = tls

    if network == "ws":
        transport = {"type": "ws", "path": clean_path or "/"}
        if host_header:
            transport["headers"] = {"Host": host_header}
        if max_early_data:
            transport["max_early_data"] = max_early_data
            transport["early_data_header_name"] = "Sec-WebSocket-Protocol"
        outbound["transport"] = transport
    elif network in ("grpc", "gun"):
        outbound["transport"] = {
            "type": "grpc",
            "service_name": str(path).strip("/"),
        }
    elif network in ("http", "h2"):
        transport = {"type": "http", "path": path or "/"}
        if host_header:
            transport["host"] = [h.strip() for h in host_header.split(",") if h.strip()]
        outbound["transport"] = transport
    elif network == "httpupgrade":
        transport = {"type": "httpupgrade", "path": path or "/"}
        if host_header:
            transport["host"] = host_header
        outbound["transport"] = transport

    return outbound


def parse_hysteria2(link):
    parts = urlsplit(link)
    query = parse_qs(parts.query)

    if not parts.hostname or not parts.port:
        raise ValueError("Hysteria2 链接缺少服务器或端口")

    if parts.password:
        password = unquote(parts.username or "") + ":" + unquote(parts.password)
    else:
        password = unquote(parts.username or "")

    outbound = {
        "type": "hysteria2",
        "tag": tag_from_url(parts, "hysteria2"),
        "server": parts.hostname,
        "server_port": int(parts.port),
        "password": password,
        "tls": force_tls(query, parts.hostname, default_insecure=True),
    }

    # 带宽（可选）
    for key_src, key_dst in (
        ("up", "up_mbps"),
        ("upmbps", "up_mbps"),
        ("down", "down_mbps"),
        ("downmbps", "down_mbps"),
    ):
        val = query_value(query, key_src)
        if val:
            try:
                # 支持 "100" 或 "100Mbps"
                num = int(re.sub(r"[^\d]", "", val) or "0")
                if num > 0:
                    outbound[key_dst] = num
            except ValueError:
                pass

    # 端口跳跃 mport / ports
    ports = query_value(query, "mport") or query_value(query, "ports")
    if ports:
        outbound["server_ports"] = [ports]

    hop = query_value(query, "hop_interval") or query_value(query, "hopInterval")
    if hop:
        if hop.isdigit():
            hop = hop + "s"
        outbound["hop_interval"] = hop

    obfs = query_value(query, "obfs")
    obfs_password = query_value(query, "obfs-password") or query_value(query, "obfs_password")
    if obfs == "salamander" and obfs_password:
        outbound["obfs"] = {"type": "salamander", "password": obfs_password}

    return outbound


def parse_tuic(link):
    parts = urlsplit(link)
    query = parse_qs(parts.query)

    if not parts.username or not parts.hostname or not parts.port:
        raise ValueError("TUIC 链接缺少 UUID、服务器或端口")

    insecure_param = query_value(query, "allowInsecure") or query_value(query, "insecure")
    if insecure_param == "":
        insecure = True  # tuic 分享链接常见自签
    else:
        insecure = bool_value(insecure_param)

    outbound = {
        "type": "tuic",
        "tag": tag_from_url(parts, "tuic"),
        "server": parts.hostname,
        "server_port": int(parts.port),
        "uuid": unquote(parts.username),
        "password": unquote(parts.password or ""),
        "congestion_control": query_value(query, "congestion_control", "bbr"),
        "tls": {
            "enabled": True,
            "server_name": query_value(query, "sni") or parts.hostname,
            "insecure": insecure,
        },
    }

    udp_relay = query_value(query, "udp_relay_mode") or query_value(query, "udp-relay-mode")
    if udp_relay:
        outbound["udp_relay_mode"] = udp_relay

    if bool_value(query_value(query, "zero_rtt_handshake") or query_value(query, "reduce_rtt")):
        outbound["zero_rtt_handshake"] = True

    alpn = query_value(query, "alpn")
    if alpn:
        outbound["tls"]["alpn"] = [item.strip() for item in alpn.split(",") if item.strip()]

    return outbound

def parse_socks_or_http(link):
    parts = urlsplit(link)
    scheme = parts.scheme.lower()
    port = parts.port
    if not port:
        if scheme == "https":
            port = 443
        elif scheme == "http":
            port = 80

    if not parts.hostname or not port:
        raise ValueError("代理链接缺少服务器或端口")

    is_socks = scheme in ("socks", "socks5", "socks5h", "socks4")
    outbound = {
        "type": "socks" if is_socks else "http",
        "tag": tag_from_url(parts, scheme),
        "server": parts.hostname,
        "server_port": int(port),
    }

    if parts.username:
        outbound["username"] = unquote(parts.username)
    if parts.password:
        outbound["password"] = unquote(parts.password)

    if is_socks:
        outbound["version"] = "4" if scheme == "socks4" else "5"

    if scheme == "https":
        outbound["tls"] = {
            "enabled": True,
            "server_name": parts.hostname,
            "insecure": False,
        }

    return outbound


def parse_sn(link):
    parts = urlsplit(link)
    blob = parts.query or parts.netloc

    pad = "=" * (-len(blob) % 4)
    try:
        raw = base64.urlsafe_b64decode(blob + pad)
        data = zlib.decompress(raw)
    except Exception as exc:
        raise ValueError(f"sn:// 链接解码失败: {exc}") from exc

    start = data.find(b"{")
    if start == -1:
        raise ValueError("sn:// 链接中未找到内嵌配置")

    depth = 0
    end = -1
    for i in range(start, len(data)):
        if data[i : i + 1] == b"{":
            depth += 1
        elif data[i : i + 1] == b"}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        raise ValueError("sn:// 链接中内嵌配置不完整")

    try:
        text = data[start : end + 1].decode("utf-8", errors="surrogatepass")
        text = text.encode("utf-16", "surrogatepass").decode("utf-16")
        outbound = json.loads(text)
    except Exception as exc:
        raise ValueError(f"sn:// 链接内嵌配置解析失败: {exc}") from exc

    if not isinstance(outbound, dict) or "type" not in outbound:
        raise ValueError("sn:// 链接内嵌配置不是有效的 outbound")

    return ensure_tag(outbound, "proxy")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_link(link):
    link = normalize_link(link)
    scheme = scheme_of(link)

    if scheme == "vless":
        return ensure_tag(parse_vless(link))
    if scheme == "vmess":
        return ensure_tag(parse_vmess(link))
    if scheme == "trojan":
        return ensure_tag(parse_trojan(link))
    if scheme == "anytls":
        return ensure_tag(parse_anytls(link))
    if scheme == "ss":
        return ensure_tag(parse_ss(link))
    if scheme in ("hysteria2", "hy2"):
        return ensure_tag(parse_hysteria2(link))
    if scheme == "tuic":
        return ensure_tag(parse_tuic(link))
    if scheme in ("socks", "socks5", "socks5h", "socks4", "http", "https"):
        return ensure_tag(parse_socks_or_http(link))
    if scheme == "sn":
        return ensure_tag(parse_sn(link))

    raise ValueError("不支持的链接类型: " + (scheme or "(空)"))


def _parse_proxy_outbound(url):

    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:

        try:
            decoded = b64decode(url)
            url = next((l.strip() for l in decoded.splitlines() if "://" in l), "")
        except Exception:
            return None
    if "://" not in url:
        return None
    try:
        return parse_link(url)
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
                    f"🌐 <b>出口IP:</b> <code>{html_mod.escape(str(item['ip']), quote=False)}</code>",
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


def build_full_summary(accounts, state, results, ips=None):
    by_id = {r["id"]: r for r in results}
    full = []
    for acc in accounts:
        if acc["id"] in by_id:
            full.append(by_id[acc["id"]])
            continue
        st = state.get(f"ACCOUNT_{acc['id']}") or {}
        due = st.get("due_date") or "未知"
        backoff = (
            st.get("last_result") == "failed"
            and (st.get("retry_at") or 0) > time.time()
        )
        full.append(
            {
                "id": acc["id"],
                "user": mask_email(acc["username"]),
                "full_user": acc["username"],
                "login_method": st.get("login_method") or "未知",
                "status": "❌ 上次续期失败，退避中" if backoff else "⏭️ 未到期，已跳过",
                "old_due": due,
                "new_due": due,
                "ip": (ips or {}).get(acc["id"], "未检测"),
                "failed": backoff,
            }
        )
    return full


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
    pending = [a for a in accounts if a["id"] in pending_ids]
    if pending:
        log(f"🎯 本次需要续期的账号: {', '.join(a['id'] for a in pending)}")

    start_singbox(accounts)

    ips = {}
    for acc in accounts:
        if acc["id"] in pending_ids:
            continue
        if acc.get("proxy_ready"):
            ips[acc["id"]] = get_current_ip(f"http://127.0.0.1:{acc['port']}")
            log(f"🔎 账号 {acc['id']} 节点检测，出口IP: {mask_ip(ips[acc['id']])}")
        else:
            ips[acc["id"]] = "获取失败"

    if not pending:
        log("⏭️ 所有账号均未到期，本次不启动浏览器，仅检测节点并发送汇总通知。")
        stop_singbox()
        send_notifications(build_full_summary(accounts, state, [], ips))
        sys.exit(0)

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
                if result.get("login_method") not in ("未登录", "未知"):
                    state.setdefault(f"ACCOUNT_{acc['id']}", {})["login_method"] = result[
                        "login_method"
                    ]
                if result.get("failed"):
                    exit_code = 1
        finally:
            stop_singbox()

    save_state(state)

    send_notifications(build_full_summary(accounts, state, summary, ips))

    log("═══════════ 运行汇总 ═══════════")
    for item in summary:
        log(
            f"账号 {item['id']} ({item['user']}): {item['status']} | {item['old_due']} → {item['new_due']}"
        )
    log("════════════════════════════════")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
