#!/usr/bin/env python3
"""Telegram status bot for a self-hosted VPS (PRIVATE-SERVER) + its Docker containers.

Polls Telegram's getUpdates, replies only to ALLOWED_CHAT_ID, and runs a
small set of status checks (docker, xrdp, nginx, host resources).
"""
import calendar
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.parse

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# Folder this file lives in; runtime files (offset, state, logs) are kept next to it.
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
OFFSET_FILE = os.environ.get("OFFSET_FILE", os.path.join(BOT_DIR, "offset.txt"))
UNAUTHORIZED_LOG = os.environ.get(
    "UNAUTHORIZED_LOG", os.path.join(BOT_DIR, "unauthorized_access.log")
)


def api_call(method, params=None, timeout=35):
    url = f"{API}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def send_message(chat_id, text, menu=False, markup=None):
    # Telegram caps messages at 4096 chars; truncate defensively.
    text = text[:3900]
    params = {"chat_id": chat_id, "text": text}
    if markup:
        params["reply_markup"] = markup
    elif menu:
        params["reply_markup"] = MENU
    api_call("sendMessage", params)


def send_reply(chat_id, result):
    """Send a handler result: plain text gets the main menu, (text, markup) its own buttons,
    a list is several messages with the menu under the last one."""
    if isinstance(result, list):
        for text in result[:-1]:
            send_message(chat_id, text)
        send_message(chat_id, result[-1], menu=True)
    elif isinstance(result, tuple):
        send_message(chat_id, result[0], markup=result[1])
    else:
        send_message(chat_id, result, menu=True)


def send_photo(chat_id, file_id, caption=""):
    api_call(
        "sendPhoto",
        {"chat_id": chat_id, "photo": file_id, "caption": caption[:1000]},
    )


def run(cmd, timeout=10):
    try:
        out = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return (out.stdout + out.stderr).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "(command timed out)"
    except Exception as e:
        return f"(error: {e})"


def list_containers():
    """All existing containers as (name, running, status), read live from docker."""
    out = run("sudo -n docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}'")
    found = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            found.append((parts[0], parts[1] == "running", parts[2]))
    return sorted(found)


def read_proc_stat_cpu_lines():
    with open("/proc/stat") as f:
        return [l for l in f if l.startswith("cpu")]


def host_cpu_lines():
    """Per-core + overall CPU usage, sampled over a short interval."""
    s1 = read_proc_stat_cpu_lines()
    time.sleep(0.3)
    s2 = read_proc_stat_cpu_lines()
    lines = []
    for l1, l2 in zip(s1, s2):
        name = l1.split()[0]
        v1 = list(map(int, l1.split()[1:]))
        v2 = list(map(int, l2.split()[1:]))
        idle1, idle2 = v1[3] + v1[4], v2[3] + v2[4]
        total1, total2 = sum(v1), sum(v2)
        totald, idled = total2 - total1, idle2 - idle1
        usage = (totald - idled) / totald * 100 if totald else 0.0
        label = "avg" if name == "cpu" else name
        lines.append(f"{label}: {usage:.1f}%")
    return lines


def parse_free_lines(free_raw):
    for line in free_raw.splitlines():
        if line.startswith("Mem:"):
            f = line.split()
            total, used, freeb, shared, buffcache, available = map(int, f[1:7])

            def g(x):
                return x / 1048576

            pct = used / total * 100 if total else 0
            return [
                f"used: {pct:.0f}% ({g(used):.1f}G/{g(total):.1f}G)",
                f"free: {g(freeb):.1f}G",
                f"buff/cache: {g(buffcache):.1f}G",
                f"available: {g(available):.1f}G",
            ]
    return [free_raw]


def parse_df(df_raw):
    lines = df_raw.strip().splitlines()
    if len(lines) < 2:
        return df_raw
    f = lines[-1].split()
    if len(f) >= 5:
        return f"{f[2]}/{f[1]} ({f[4]})"
    return df_raw


def parse_df_inode(df_raw):
    lines = df_raw.strip().splitlines()
    if len(lines) < 2:
        return df_raw
    f = lines[-1].split()
    if len(f) >= 5:
        return f"{f[2]}/{f[1]} ({f[4]})"
    return df_raw


def indented(label, lines, indent="    "):
    body = "\n".join(f"{indent}  {l}" for l in lines)
    return f"{indent}{label}:\n{body}"


def host_block():
    cpu_lines = host_cpu_lines()
    mem_lines = parse_free_lines(run("free"))
    fs = parse_df(run("df -h /"))
    inodes = parse_df_inode(run("df -i /"))
    xrdp_raw = run("sudo -n systemctl is-active xrdp xrdp-sesman")
    xrdp_parts = [p.strip() for p in xrdp_raw.splitlines() if p.strip()]
    xrdp_ok = bool(xrdp_parts) and all(p == "active" for p in xrdp_parts)
    stale = run("ps -ef | grep '[X]org :10' | wc -l")
    nginx_svc = run("sudo -n systemctl is-active nginx").strip()
    nginx_ok = nginx_svc == "active"
    text = "\n".join(
        [
            "GUAC SERVER (172.20.0.1)",
            indented("CPU", cpu_lines, indent="  "),
            indented("Memory", mem_lines, indent="  "),
            indented("Filesystem (/)", [f"usage: {fs}", f"inodes: {inodes}"], indent="  "),
            f"  XRDP: {'/'.join(xrdp_parts)}",
            f"    stale Xorg :10 procs: {stale}",
            f"  NGINX: {nginx_svc}",
        ]
    )
    return text, xrdp_ok, nginx_ok


SITES = os.environ.get(
    "SITES", "owner-domain01.com owner-domain02.com"
).split()
CORE_SERVICES = ["docker", "fail2ban", "ssh", "sysstat"]
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
# Errors logged on every boot of this VM that mean nothing (no vsock, Debian
# alsa rule bug, PipeWire portal config), sudo session noise, and xrdp's
# per-session connect/disconnect chatter (xrdp outages show in the XRDP check).
HARMLESS_ERR_RE = re.compile(
    r"AF_VSOCK|systemd-ssh-generator|alsa-restore\.rules|pw\.conf|sudo|pam_unix"
    r"| xrdp(-sesexec|-sesman)?\[\d+\]:|gkr-pam|mm_reap: preauth child terminated"
)


def site_code(host):
    # Browser UA: the nginx bot filter drops urllib's default UA with 444.
    req = urllib.request.Request(f"https://{host}/", headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return str(resp.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception:
        return "unreachable"


def vps_block():
    """Whole-VPS health: uptime, failed units, core services, sites, bans, errors.
    Returns (text, [issues])."""
    issues = []
    up = run("uptime -p").replace("up ", "")
    load = open("/proc/loadavg").read().split()[:3]
    cores = os.cpu_count() or 1
    swap = run("free -m | awk '/^Swap:/{print $3\"M/\"$2\"M\"}'")

    failed = [l.split()[0] for l in run(
        "systemctl --failed --no-legend --plain").splitlines() if l.strip() and "(no output)" not in l]
    if failed:
        issues.append(f"{len(failed)} failed unit(s)")

    states = run("systemctl is-active " + " ".join(CORE_SERVICES)).splitlines()
    svc = dict(zip(CORE_SERVICES, states))
    down = [s for s in CORE_SERVICES if svc.get(s) != "active"]
    if down:
        issues.append("down: " + ",".join(down))

    codes = {}
    threads = [threading.Thread(target=lambda h=h: codes.__setitem__(h, site_code(h))) for h in SITES]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    bad_sites = [h for h in SITES if codes.get(h) != "200"]
    if bad_sites:
        issues.append(f"sites {len(SITES) - len(bad_sites)}/{len(SITES)}")

    banned = 0
    for jail in all_jails():
        banned += len(jail_bans(jail) or {})

    unclean = run("sudo -n journalctl -b 0 -o cat -t systemd-journald | grep -c 'uncleanly shut down'").strip()
    if unclean not in ("0", ""):
        issues.append("last shutdown unclean")

    # short output keeps the "ident[pid]:" prefix so noise is matched by source.
    errs = [l.split(": ", 1)[-1] for l in run(
        "sudo -n journalctl -b 0 -p err --since -30min -o short --no-hostname --no-pager").splitlines()
        if l.strip() and l != "(no output)" and not HARMLESS_ERR_RE.search(l)]
    if errs:
        issues.append(f"{len(errs)} error(s)/30min")

    lines = [
        "VPS HEALTH",
        f"  uptime: {up}",
        f"  load: {' '.join(load)} ({cores} cores)",
        f"  swap used: {swap}",
        f"  failed units: {', '.join(failed) if failed else 'none'}",
        "  services: " + "  ".join(f"{s} {'✅' if svc.get(s) == 'active' else '❌'}" for s in CORE_SERVICES),
        "  websites:",
    ]
    lines += [f"    {'✅' if codes.get(h) == '200' else '❌'} {h} {codes.get(h, 'timeout')}" for h in SITES]
    lines.append(f"  banned IPs: {banned}")
    lines.append("  last shutdown: " + ("⚠️ UNCLEAN (crash/hang/power cut)" if "last shutdown unclean" in issues else "clean"))
    lines.append(f"  errors last 30 min: {len(errs)}" + (" (harmless boot noise filtered)" if not errs else ""))
    lines += [f"    {e[:150]}" for e in errs[-3:]]
    return "\n".join(lines), issues


def docker_stats_map():
    out = run(
        "sudo -n docker stats --no-stream "
        "--format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}'"
    )
    stats = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            stats[parts[0]] = parts[1:]
    return stats


def container_block(name, stats):
    cpu, mem_usage, mem_pct = stats.get(name, ("?", "?", "?"))
    mem_detail_raw = run(f"sudo -n docker exec {name} free 2>/dev/null")
    buffcache = "?"
    for line in mem_detail_raw.splitlines():
        if line.startswith("Mem:"):
            f = line.split()
            if len(f) >= 6:
                buffcache = f"{int(f[5]) / 1048576:.2f}G"
            break
    fs = parse_df(run(f"sudo -n docker exec {name} df -h /"))
    inodes = parse_df_inode(run(f"sudo -n docker exec {name} df -i / 2>/dev/null"))
    xrdp = run(
        f"sudo -n docker exec {name} sh -c "
        f"'command -v xrdp >/dev/null || {{ echo not-installed; exit; }}; "
        f"pgrep -x xrdp >/dev/null && pgrep -x xrdp-sesman >/dev/null "
        f"&& echo active || echo inactive'"
    ).strip()
    # Only a container that has xrdp installed can count as XRDP down.
    xrdp_ok = xrdp != "inactive"
    nginx = run(
        f"sudo -n docker exec {name} sh -c "
        f"'command -v nginx >/dev/null && echo installed || echo not-installed'"
    )
    text = "\n".join(
        [
            f"  {name}:",
            f"    CPU (cgroup, multi-core capable): {cpu}",
            indented("Memory", [f"usage: {mem_usage} ({mem_pct})", f"buff/cache: {buffcache}"]),
            indented("Filesystem (/)", [f"usage: {fs}", f"inodes: {inodes}"]),
            f"    XRDP: {xrdp}",
            f"    NGINX: {nginx}",
        ]
    )
    return text, xrdp_ok

PROXY_IPS = {"172.20.0.1", "127.0.0.1", ""}
# systemd unit name of this bot (shown in /status). Must match deploy/*.service.
BOT_SERVICE = os.environ.get("BOT_SERVICE", "telegram-vps-status-bot")
# Timezone for dates read from the Guacamole database (IANA name, e.g. Europe/Berlin).
DB_TIMEZONE = os.environ.get("DB_TIMEZONE", "UTC")
if not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", DB_TIMEZONE):
    raise SystemExit(f"Invalid DB_TIMEZONE: {DB_TIMEZONE!r}")
GUAC_DOMAIN = os.environ.get("GUAC_DOMAIN", "owner-domain01.com")
GUAC_LOG = os.environ.get("GUAC_LOG", f"/var/log/nginx/{GUAC_DOMAIN}-access.log")
GUAC_ERROR_LOG = os.environ.get("GUAC_ERROR_LOG", f"/var/log/nginx/{GUAC_DOMAIN}-error.log")
GUAC_DB_CMD = (
    "sudo -n docker exec guacstack-postgres psql -U guacamole_user "
    "-d guacamole_db -At -F '|' -c "
)


def fmt_duration(seconds):
    seconds = max(int(seconds), 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def recent_web_client_ips(window_s=900):
    """Real client IPs seen by nginx on authenticated Guacamole API calls.

    Guacamole itself only sees the nginx proxy address, so this is the best
    available source for the browser-side IP.
    """
    raw = run(f"sudo -n tail -n 400 {GUAC_LOG}", timeout=10)
    pat = re.compile(
        r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\w+) (\S+)[^"]*" (\d{3})'
    )
    now = time.time()
    seen = {}
    for line in raw.splitlines():
        m = pat.match(line)
        if not m:
            continue
        ip, ts, _method, path, status = m.groups()
        if not path.startswith("/api/session") or not status.startswith("2"):
            continue
        try:
            t = time.mktime(time.strptime(ts.split()[0], "%d/%b/%Y:%H:%M:%S"))
        except ValueError:
            continue
        if now - t <= window_s:
            seen[ip] = max(seen.get(ip, 0), t)
    return sorted(seen)


def guac_sessions():
    """Active Guacamole sessions: (user, connection, seconds, client_ip)."""
    started = run(
        "sudo -n docker inspect -f '{{.State.StartedAt}}' guacstack-guacd"
    ).strip()
    if not re.fullmatch(r"[0-9T:.\-]+Z", started):
        return None
    # Rows left open from before guacd's last restart are stale, skip them.
    sql = (
        "select username, connection_name, "
        "extract(epoch from now()-start_date)::int, remote_host "
        "from guacamole_connection_history "
        f"where end_date is null and start_date >= '{started}' "
        "order by start_date"
    )
    out = run(GUAC_DB_CMD + '"' + sql + '"')
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 4 and parts[2].isdigit():
            rows.append((parts[0], parts[1], int(parts[2]), parts[3]))
    return rows


def ssh_sessions(where, prefix=""):
    """Logged-in SSH sessions as (user, ip, seconds), de-duplicated."""
    out = run(f"{prefix}who -u")
    found = {}
    for line in out.splitlines():
        m = re.match(
            r"(\S+)\s+\S+\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+\S+\s+\d+\s+\((.+)\)",
            line,
        )
        if not m:
            continue
        user, when, ip = m.groups()
        try:
            t = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M"))
        except ValueError:
            continue
        key = (user, ip)
        found[key] = min(found.get(key, t), t)
    now = time.time()
    return [(u, ip, now - t, where) for (u, ip), t in sorted(found.items(), key=lambda kv: kv[1])]


def direct_rdp_peers():
    """RDP peers on the host that are not the docker network (not via guacd)."""
    out = run("ss -Htn state established '( sport = :3389 )'")
    peers = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4:
            ip = f[3].rsplit(":", 1)[0]
            if not ip.startswith("172.20."):
                peers.append(ip)
    return peers


def sessions_block(running):
    lines = ["ACTIVE REMOTE SESSIONS"]

    rows = guac_sessions()
    ips = recent_web_client_ips()
    lines.append("  Guacamole web:")
    if rows is None:
        lines.append("    (could not read guacd state)")
    elif not rows:
        lines.append("    none")
    else:
        # Sessions opened before X-Forwarded-For was trusted only record the
        # proxy address; fall back to client IPs seen recently in nginx.
        fallback = ", ".join(ips) if ips else "unknown"
        for user, conn, secs, ip in rows:
            if ip in PROXY_IPS:
                ip = f"{fallback} (nginx log)"
            lines.append(f"    {user} from {ip} | {conn} | {fmt_duration(secs)}")

    ssh = ssh_sessions("host")
    for c in running:
        ssh += ssh_sessions(c, prefix=f"sudo -n docker exec {c} ")
    lines.append("  SSH:")
    if not ssh:
        lines.append("    none")
    for user, ip, secs, where in ssh:
        lines.append(f"    {user} from {ip} on {where} | {fmt_duration(secs)}")

    direct = direct_rdp_peers()
    if direct:
        lines.append(f"  Direct RDP (not via Guacamole): {', '.join(direct)}")
    return "\n".join(lines)


IPINFO_TTL_S = 86400
_ipinfo_cache = {}  # ip -> (fetched_at, text)


def ip_info(ip):
    """'AS64500 Example ISP · City, CC' via ipinfo.io (HTTPS; outbound
    whois/43 is blocked by UFW). Cached for a day per IP."""
    if not valid_ip(ip):
        return "?"
    if ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback:
        return "internal"
    hit = _ipinfo_cache.get(ip)
    if hit and time.time() - hit[0] < IPINFO_TTL_S:
        return hit[1]
    try:
        req = urllib.request.Request(f"https://ipinfo.io/{ip}/json", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read())
        place = ", ".join(x for x in (d.get("city"), d.get("country")) if x)
        text = " · ".join(x for x in (d.get("org") or "ASN unknown", place) if x)
    except Exception:
        return "ASN lookup failed"
    _ipinfo_cache[ip] = (time.time(), text)
    return text


def fmt_since(epoch):
    return f"{time.strftime('%d %b %H:%M:%S', time.localtime(epoch))} ({fmt_duration(time.time() - epoch)} ago)"


def guac_active():
    """Live Guacamole web logins and their open connections, grouped by (user, ip).
    Returns {(user, ip): {"start", "tabs", "conns": [(kind, name, start)]}} or None."""
    started = run("sudo -n docker inspect -f '{{.State.StartedAt}}' guacstack-guacd").strip()
    if not re.fullmatch(r"[0-9T:.\-]+Z", started):
        return None
    # Rows still open from before the last container restart are stale.
    logins = guac_sql(
        "select username, coalesce(remote_host,''), extract(epoch from start_date)::bigint "
        "from guacamole_user_history "
        f"where end_date is null and start_date >= '{started}'"
    )
    conns = guac_sql(
        "select h.username, coalesce(h.remote_host,''), coalesce(c.protocol,''), "
        "coalesce(h.connection_name,''), extract(epoch from h.start_date)::bigint "
        "from guacamole_connection_history h "
        "left join guacamole_connection c on c.connection_id = h.connection_id "
        f"where h.end_date is null and h.start_date >= '{started}' order by h.start_date"
    )
    fallback = recent_web_client_ips()
    fallback_ip = fallback[0] if len(fallback) == 1 else ""

    def fix(ip):
        # Old sessions recorded only the nginx proxy address.
        return fallback_ip or ip if ip in PROXY_IPS else ip

    out = {}
    for line in logins.splitlines():
        f = line.split("|")
        if len(f) == 3 and f[2].isdigit():
            s = out.setdefault((f[0], fix(f[1])), {"start": int(f[2]), "tabs": 0, "conns": []})
            s["start"] = min(s["start"], int(f[2]))
            s["tabs"] += 1
    for line in conns.splitlines():
        f = line.split("|")
        if len(f) == 5 and f[4].isdigit():
            s = out.setdefault((f[0], fix(f[1])), {"start": int(f[4]), "tabs": 0, "conns": []})
            s["conns"].append((PROTOCOL_NAMES.get(f[2], f[2].upper() or "?"), f[3], int(f[4])))
    return out


def ssh_logins():
    """Direct SSH logins on the host: [(user, ip, start_epoch, tty)], second precision
    from the session process start; guacd's own SSH (docker net) is left out."""
    rows = []
    for line in run("who -u").splitlines():
        m = re.match(r"(\S+)\s+(\S+(?: \S+)?)\s+\d{4}-\S+ \S+\s+\S+\s+(\d+)\s+\((.+)\)", line)
        if not m:
            continue
        user, tty, pid, ip = m.groups()
        if ip.startswith("172.20.") or ip in PROXY_IPS:
            continue
        lstart = run(f"ps -o lstart= -p {pid}").strip()
        try:
            start = time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        rows.append((user, ip, start, tty.replace("sshd", "").strip() or "no tty"))
    return sorted(rows, key=lambda r: r[2])


def xrdp_desktops():
    """xrdp desktop sessions: [(user, display, start_epoch)] from the Xorg processes."""
    rows = []
    for line in run("ps -eo user=,lstart=,args=").splitlines():
        m = re.match(r"(\S+)\s+(\w{3} \w{3} +\d+ [\d:]+ \d{4})\s+\S*Xorg (:\d+) .*xrdp", line)
        if m:
            try:
                rows.append((m.group(1), m.group(3), time.mktime(time.strptime(m.group(2), "%a %b %d %H:%M:%S %Y"))))
            except ValueError:
                pass
    return rows


def cmd_sessions(_args):
    lines = ["👥 ACTIVE SESSIONS"]

    lines.append("\n🌐 Guacamole (web)")
    guac = guac_active()
    if guac is None:
        lines.append("  (could not read Guacamole state)")
    elif not guac:
        lines.append("  none")
    for (user, ip), s in sorted((guac or {}).items(), key=lambda kv: kv[1]["start"]):
        lines.append(f"  👤 {user}")
        lines.append(f"     since: {fmt_since(s['start'])}" + (f" · {s['tabs']} open logins" if s["tabs"] > 1 else ""))
        lines.append(f"     IP: {ip or 'unknown'}")
        lines.append(f"     ISP: {ip_info(ip)}")
        for kind, name, start in s["conns"]:
            lines.append(f"     ↳ {kind} \"{name}\" since {time.strftime('%H:%M:%S', time.localtime(start))}")
        if not s["conns"]:
            lines.append("     ↳ logged in, no connection open")

    lines.append("\n🔑 SSH (direct)")
    ssh = ssh_logins()
    if not ssh:
        lines.append("  none")
    for user, ip, start, tty in ssh:
        lines.append(f"  👤 {user} ({tty})")
        lines.append(f"     since: {fmt_since(start)}")
        lines.append(f"     IP: {ip}")
        lines.append(f"     ISP: {ip_info(ip)}")

    lines.append("\n🖥️ RDP")
    direct = direct_rdp_peers()
    if direct:
        for ip in direct:
            lines.append(f"  ⚠️ direct client (not via Guacamole): {ip} · {ip_info(ip)}")
    else:
        lines.append("  direct clients: none (RDP only via Guacamole)")
    for user, display, start in xrdp_desktops():
        lines.append(f"  desktop {display} user {user} since {fmt_since(start)}")

    return "\n".join(lines)



def cmd_status(_args):
    bot_svc = run(f"sudo -n systemctl is-active {BOT_SERVICE}.service").strip()
    bot_ok = bot_svc == "active"

    host_text, host_xrdp_ok, host_nginx_ok = host_block()
    vps_text, vps_issues = vps_block()

    containers = list_containers()
    running = [name for name, up, _ in containers if up]
    stats = docker_stats_map()
    container_texts = []
    down_containers = []
    for name, up, status in containers:
        if not up:
            container_texts.append(f"  {name}: {status}")
            down_containers.append(name)
            continue
        text, xrdp_ok = container_block(name, stats)
        container_texts.append(text)
        if not xrdp_ok:
            down_containers.append(name)
    if not containers:
        container_texts.append("  none")

    xrdp_mark = (
        "OK"
        if host_xrdp_ok and not down_containers
        else "DOWN (" + ", ".join((["guac host"] if not host_xrdp_ok else []) + down_containers) + ")"
    )
    nginx_mark = "OK" if host_nginx_ok else "DOWN"
    bot_mark = "OK" if bot_ok else "DOWN"
    vps_mark = "OK" if not vps_issues else "⚠️ " + "; ".join(vps_issues)
    summary = f"Quick check: VPS {vps_mark} | XRDP {xrdp_mark} | NGINX {nginx_mark} | Bot {bot_mark}"

    parts = [summary, vps_text, sessions_block(running), host_text, "CONTAINERS"] + container_texts
    parts.append(f"Telegram bot service: {bot_svc}")
    return "\n\n".join(parts)


def cmd_docker(_args):
    out = run(
        "sudo -n docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'"
    )
    return f"Containers:\n{out}"


def cmd_xrdp(_args):
    svc = run("sudo -n systemctl is-active xrdp xrdp-sesman")
    stale = run(
        "ps -ef | grep '[X]org :10' | wc -l"
    )
    return (
        f"xrdp / xrdp-sesman: {svc}\n"
        f"Active Xorg :10 processes: {stale} "
        f"(>2 may mean leaked sessions — known past issue)"
    )


def cmd_nginx(_args):
    svc = run("sudo -n systemctl is-active nginx")
    errs = run(
        f"sudo -n tail -n 5 {GUAC_ERROR_LOG} 2>/dev/null"
    )
    return f"nginx: {svc}\n\nLast 5 error log lines ({GUAC_DOMAIN}):\n{errs}"


# ➕ ADD YOUR COMMAND HERE (step 1/5): write a handler. It gets the text after the
# command as `args` and returns the reply text. Example:
#
# def cmd_uptime(_args):
#     return "Uptime:\n" + run("uptime -p")


def cmd_help(_args):
    return (
        "Available commands:\n"
        "/status - active remote sessions, host load, memory, disk\n"
        "/docker - container status\n"
        "/xrdp - xrdp/sesman service + stale session check\n"
        "/nginx - nginx service + recent errors\n"
        "/intruders - last unauthorized access attempts (name, ID, time)\n"
        "/banned - blocked IPs by type (bots, search engines, SSH, port scans...)\n"
        "/banned <group> - full list: search, ai, bots, scanners, probe, portscan, sshd, guac, recidive\n"
        "/sessions - who is connected: user, start time, client IP, ISP/ASN, via Guacamole/SSH/RDP\n"
        "/allbans - every banned IP on the VPS: all fail2ban jails, UFW, nginx deny files\n"
        "/unban - pick a banned IP from a list of buttons\n"
        "/unban <ip> [jail] - remove a ban (all jails, or just the one named)\n"
        "/reboot - reboot the VPS (asks to confirm first; drops every session)\n"
        "/help - this message\n"
        # ➕ ADD YOUR COMMAND HERE (step 5/5): describe it in /help, e.g.
        # "/uptime - how long the server has been up\n"
    )


def cmd_reboot(args):
    """Reboot the whole VPS. Destructive: asks for an explicit confirm tap first,
    then delays the actual reboot briefly so this confirmation message has time
    to leave before the host goes down."""
    if args.strip().lower() != "confirm":
        markup = json.dumps({"inline_keyboard": [
            [{"text": "⚠️ Confirm reboot now", "callback_data": "/reboot confirm"}],
            [{"text": "Cancel", "callback_data": "/help"}],
        ]})
        return (
            "⚠️ This reboots the whole VPS now.\n"
            "Guacamole, nginx, Docker and every container go down and back up with it.\n"
            "The server is unreachable for roughly 1-2 minutes. Any active session is dropped.\n\n"
            "Confirm?",
            markup,
        )
    print(f"[reboot] confirmed via Telegram at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    threading.Timer(2.0, lambda: run("sudo -n reboot", timeout=5)).start()
    return (
        "\U0001f504 Rebooting the VPS now.\n"
        "This bot goes offline until the host finishes coming back up (~1-2 min)."
    )


def cmd_intruders(_args):
    try:
        with open(UNAUTHORIZED_LOG) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return "No unauthorized access attempts logged yet."
    if not lines:
        return "No unauthorized access attempts logged yet."
    return "Last unauthorized attempts:\n" + "".join(lines[-15:])


# (key, title, fail2ban jail, bot-log class). nginx-bot bans are split by the
# class nginx logged for that IP; "" collects IPs no longer in the bot logs.
BAN_GROUPS = [
    ("search", "🔎 Search engines", "nginx-bot", "GOOD_BOT"),
    ("ai", "🤖 AI / SEO crawlers", "nginx-bot", "BAD_BOT"),
    ("bots", "🕷️ Other bots / spiders", "nginx-bot", "UNKNOWN_BOT"),
    ("scanners", "🧪 Scanners / scripts", "nginx-bot", "SCANNER"),
    ("botsold", "❔ Bots (class not in current logs)", "nginx-bot", ""),
    ("portscan", "📡 Port scanners", "portscan", None),
    ("probe", "🔍 Hidden-file probers (.env, .git)", "nginx-probe", None),
    ("sshd", "🔑 SSH brute force", "sshd", None),
    ("guac", "🖥️ Guacamole login attacks", "guacamole-nginx", None),
    ("recidive", "♻️ Repeat offenders", "recidive", None),
]
BANNED_PREVIEW = 15
BANIP_RE = re.compile(r"^(\S+)\s+\S+ \S+ \+ (-?\d+) = (\S+ \S+)")
BOT_LOG_LABEL_RE = re.compile(r"^\[[^\]]+\] ([A-Z_]+) IP=(\S+) ")


def jail_bans(jail):
    """{ip: ban end} for a fail2ban jail, or None if the jail can't be read."""
    out = run(f"sudo -n fail2ban-client get {jail} banip --with-time", timeout=15)
    if "does not exist" in out or "ERROR" in out:
        return None
    bans = {}
    for line in out.splitlines():
        m = BANIP_RE.match(line.strip())
        if m:
            bans[m.group(1)] = "permanent" if m.group(2).startswith("-") else m.group(3)[:16]
    return bans


def bot_classes():
    """Last bot class nginx logged per IP (GOOD_BOT, BAD_BOT, ...)."""
    raw = run("sudo -n sh -c 'cat /var/log/nginx/bots/*.log'", timeout=20)
    classes = {}
    for line in raw.splitlines():
        m = BOT_LOG_LABEL_RE.match(line)
        if m and m.group(1) != "HUMAN":
            classes[m.group(2)] = m.group(1)
    return classes


def cmd_banned(args):
    want = args.strip().lower()
    if want and want not in {g[0] for g in BAN_GROUPS}:
        return "Usage: /banned [" + "|".join(g[0] for g in BAN_GROUPS) + "]"
    jails = {jail: jail_bans(jail) for jail in dict.fromkeys(g[2] for g in BAN_GROUPS)}
    classes = bot_classes() if jails.get("nginx-bot") else {}
    total = len({ip for bans in jails.values() if bans for ip in bans})
    lines = [f"⛔ Banned IPs: {total} unique"]
    limit = None if want else BANNED_PREVIEW
    for key, title, jail, label in BAN_GROUPS:
        if want and key != want:
            continue
        bans = jails.get(jail)
        if bans is None:
            lines.append(f"\n{title}: jail {jail} unavailable")
            continue
        if label is not None:
            bans = {ip: end for ip, end in bans.items() if classes.get(ip, "") == label}
            if label == "" and not bans and not want:
                continue
        lines.append(f"\n{title} ({len(bans)})")
        items = sorted(bans.items(), key=lambda kv: kv[1], reverse=True)
        for ip, end in items[:limit]:
            lines.append(f"  {ip}  " + ("permanent" if end == "permanent" else f"until {end}"))
        if limit and len(items) > limit:
            lines.append(f"  … +{len(items) - limit} more: /banned {key}")
    if not want:
        lines.append("\nFull list of one group: /banned <group>")
    lines.append("Unban: /unban <ip>")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800].rsplit("\n", 1)[0] + "\n… (list truncated)"
    return text


ALLBANS_MAX_MESSAGES = 8
UFW_DENY_RE = re.compile(r"^(.+?)\s+(DENY|REJECT)(?: IN| OUT)?\s+(\S+)")
NGINX_DENY_FILES = [
    ("/etc/nginx/guac-deny.conf", re.compile(r"^\s*deny\s+(\S+?);")),
    ("/etc/nginx/bot-deny.conf", re.compile(r"^\s*(\S+)\s+1;")),
]


def all_jails():
    """Every jail fail2ban is running, not just the ones in BAN_GROUPS."""
    out = run("sudo -n fail2ban-client status", timeout=15)
    m = re.search(r"Jail list:\s*(.*)", out)
    return [j.strip() for j in m.group(1).split(",") if j.strip()] if m else []


def ufw_denies():
    """[(source, target)] for UFW deny/reject rules aimed at a specific address."""
    rules = []
    for line in run("sudo -n ufw status", timeout=15).splitlines():
        m = UFW_DENY_RE.match(line.strip())
        if m and m.group(3) != "Anywhere":
            rules.append((m.group(3), m.group(1).strip()))
    return rules


def chunk_messages(lines, limit=3800):
    msgs, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > limit:
            msgs.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        msgs.append(cur)
    return msgs or ["(nothing)"]


def cmd_allbans(_args):
    """Every blocked IP on the VPS: all fail2ban jails, UFW deny rules, nginx deny files."""
    lines, every_ip, f2b_ips = [], set(), set()
    jails = all_jails()
    if not jails:
        lines.append("⚠️ fail2ban not reachable (no jail list)")
    for jail in jails:
        bans = jail_bans(jail)
        if bans is None:
            lines.append(f"\n🔒 {jail}: unavailable")
            continue
        lines.append(f"\n🔒 {jail} ({len(bans)})")
        for ip, end in sorted(bans.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {ip}  " + ("permanent" if end == "permanent" else f"until {end}"))
        every_ip.update(bans)
        f2b_ips.update(bans)
    ufw = ufw_denies()
    lines.append(f"\n🧱 UFW deny rules ({len(ufw)})")
    for src, target in ufw:
        lines.append(f"  {src}  → {target}")
        every_ip.add(src)
    # nginx deny files are normally written by fail2ban; list only the extras.
    for path, rx in NGINX_DENY_FILES:
        raw = run(f"sudo -n cat {path}", timeout=10)
        extra = [m.group(1) for m in map(rx.match, raw.splitlines()) if m and m.group(1) not in f2b_ips]
        lines.append(f"\n📄 {path} manual entries ({len(extra)})")
        lines.extend(f"  {ip}" for ip in extra)
        every_ip.update(extra)
    lines.insert(0, f"⛔ All bans on this VPS: {len(every_ip)} unique IP(s)")
    lines.append("\nUnban: tap Unban, or /unban <ip>")
    msgs = chunk_messages(lines)
    if len(msgs) > ALLBANS_MAX_MESSAGES:
        msgs = msgs[:ALLBANS_MAX_MESSAGES]
        msgs[-1] += f"… list cut at {ALLBANS_MAX_MESSAGES} messages; see one group with /banned <group>"
    return msgs


# Jails that ban people (you, your users) come before the bot jail in the picker.
UNBAN_PICK_ORDER = ["guacamole-nginx", "sshd", "portscan", "recidive", "nginx-probe", "nginx-bot"]
UNBAN_PICK_MAX = 30


def unban_picker():
    """Banned IPs as tap-to-unban buttons, human-facing jails first."""
    jail_keys = {}
    for key, _title, jail, _label in BAN_GROUPS:
        jail_keys.setdefault(jail, "bot" if jail == "nginx-bot" else key)
    seen = {}
    for jail in UNBAN_PICK_ORDER:
        bans = jail_bans(jail) or {}
        for ip, _end in sorted(bans.items(), key=lambda kv: kv[1], reverse=True):
            seen.setdefault(ip, []).append(jail_keys.get(jail, jail))
    if not seen:
        return ("✅ No banned IPs right now.", MENU)
    buttons = [
        {"text": f"🔓 {ip} ({','.join(keys)})", "callback_data": f"/unban {ip}"}
        for ip, keys in list(seen.items())[:UNBAN_PICK_MAX]
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([{"text": "⬅️ Menu", "callback_data": "/help"}])
    text = f"⛔ {len(seen)} banned IP(s). Tap one to unban it from every jail."
    if len(seen) > UNBAN_PICK_MAX:
        text += f"\nShowing {UNBAN_PICK_MAX}; for others type /unban <ip>."
    return (text, json.dumps({"inline_keyboard": rows}))


def cmd_unban(args):
    jails = list(dict.fromkeys(g[2] for g in BAN_GROUPS))
    aliases = {g[0]: g[2] for g in BAN_GROUPS}
    parts = args.split()
    if not parts:
        return unban_picker()
    if len(parts) > 2:
        return (
            "Usage: /unban <ip> [jail]\n"
            "Without a jail the IP is removed from every jail it is banned in.\n"
            "Jails: " + ", ".join(jails)
        )
    try:
        ip = str(ipaddress.ip_address(parts[0]))
    except ValueError:
        return f"Not a valid IP address: {parts[0][:60]}"
    if len(parts) == 2:
        jail = aliases.get(parts[1].lower(), parts[1].lower())
        if jail not in jails:
            return "Unknown jail. Jails: " + ", ".join(jails)
        jails = [jail]
    removed, failed = [], []
    for jail in jails:
        bans = jail_bans(jail)
        if not bans or ip not in bans:
            continue
        out = run(f"sudo -n fail2ban-client set {jail} unbanip {ip}", timeout=30)
        if out.strip() == "1":
            removed.append(jail)
        else:
            failed.append(f"{jail}: {out[:200]}")
    if not removed and not failed:
        return f"{ip} is not banned in: {', '.join(jails)}"
    lines = []
    if removed:
        lines.append(f"✅ {ip} unbanned from: {', '.join(removed)}")
    if failed:
        lines.append("❌ Failed:\n  " + "\n  ".join(failed))
    if "nginx-bot" in removed:
        lines.append("Note: a bot that keeps crawling is banned again on its next request.")
    return "\n".join(lines)


COMMANDS = {
    "/status": cmd_status,
    "/docker": cmd_docker,
    "/xrdp": cmd_xrdp,
    "/nginx": cmd_nginx,
    "/intruders": cmd_intruders,
    "/banned": cmd_banned,
    "/unban": cmd_unban,
    "/allbans": cmd_allbans,
    "/sessions": cmd_sessions,
    "/reboot": cmd_reboot,
    "/help": cmd_help,
    "/start": cmd_help,
    # ➕ ADD YOUR COMMAND HERE (step 2/5): map the command to its handler, e.g.
    # "/uptime": cmd_uptime,
}


# Inline buttons attached under every reply to the allowed chat.
MENU = json.dumps({"inline_keyboard": [
    [{"text": "Status", "callback_data": "/status"},
     {"text": "Sessions", "callback_data": "/sessions"}],
    [{"text": "Docker", "callback_data": "/docker"},
     {"text": "XRDP", "callback_data": "/xrdp"}],
    [{"text": "Nginx", "callback_data": "/nginx"},
     {"text": "Intruders", "callback_data": "/intruders"}],
    [{"text": "Banned IPs", "callback_data": "/banned"},
     {"text": "All bans", "callback_data": "/allbans"}],
    [{"text": "Unban", "callback_data": "/unban"},
     {"text": "Help", "callback_data": "/help"}],
    # ➕ ADD YOUR COMMAND HERE (step 3/5): optional inline button (the existing rows have 2), e.g.
    # [{"text": "Uptime", "callback_data": "/uptime"}],
]})

MENU_COMMANDS = [
    {"command": "status", "description": "Sessions, host load, memory, disk"},
    {"command": "docker", "description": "Container status"},
    {"command": "xrdp", "description": "xrdp service + stale sessions"},
    {"command": "nginx", "description": "nginx service + recent errors"},
    {"command": "intruders", "description": "Unauthorized access attempts"},
    {"command": "banned", "description": "Blocked IPs: bots, search engines, SSH, port scans"},
    {"command": "sessions", "description": "Who is connected: user, start, IP, ISP, Guacamole/SSH/RDP"},
    {"command": "allbans", "description": "Every banned IP on the VPS (all jails, UFW, nginx)"},
    {"command": "unban", "description": "Unban an IP: pick from list, or /unban <ip> [jail]"},
    {"command": "reboot", "description": "Reboot the VPS (asks to confirm first)"},
    {"command": "help", "description": "Show menu"},
    # ➕ ADD YOUR COMMAND HERE (step 4/5): optional entry in Telegram's "Menu" button
    # (command name without "/", lowercase), e.g.
    # {"command": "uptime", "description": "Server uptime"},
]


def register_commands():
    """Populate Telegram's Menu button with the bot's commands."""
    try:
        api_call("setMyCommands", {"commands": json.dumps(MENU_COMMANDS)})
    except Exception as e:
        print(f"setMyCommands failed: {e}")


def load_offset():
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_offset(offset):
    os.makedirs(os.path.dirname(OFFSET_FILE), exist_ok=True)
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def get_profile_photo_file_id(user_id):
    try:
        resp = api_call("getUserProfilePhotos", {"user_id": user_id, "limit": 1})
        photos = resp.get("result", {}).get("photos", [])
        if photos and photos[0]:
            return photos[0][-1]["file_id"]  # largest available size
    except Exception:
        pass
    return None


def log_unauthorized(from_user, chat, text):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
    line = (
        f"{ts} | user_id={from_user.get('id')} "
        f"username=@{from_user.get('username', '-')} "
        f"name=\"{name}\" chat_id={chat.get('id')} "
        f"chat_type={chat.get('type')} text={text!r}\n"
    )
    try:
        os.makedirs(os.path.dirname(UNAUTHORIZED_LOG), exist_ok=True)
        with open(UNAUTHORIZED_LOG, "a") as f:
            f.write(line)
    except Exception as e:
        print(f"log_unauthorized failed: {e}")


def alert_unauthorized(from_user, chat, text):
    uid = from_user.get("id")
    uname = from_user.get("username")
    name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
    caption = (
        "⚠️ Unauthorized access attempt on status bot\n"
        f"Name: {name or '(none)'}\n"
        f"Username: @{uname if uname else '(none)'}\n"
        f"Telegram user ID: {uid}\n"
        f"Chat ID: {chat.get('id')} (type: {chat.get('type')})\n"
        f"Language: {from_user.get('language_code', '?')}\n"
        f"Message sent: {text!r}\n"
        "Phone number: not retrievable — Telegram's Bot API never exposes "
        "a user's phone number unless they explicitly share it via a contact "
        "card. An unsolicited sender will not do that, so this cannot be "
        "obtained through the bot."
    )
    file_id = get_profile_photo_file_id(uid)
    if file_id:
        send_photo(ALLOWED_CHAT_ID, file_id, caption)
    else:
        send_message(ALLOWED_CHAT_ID, caption + "\n\n(No profile photo available/visible.)")


def handle_callback(cb):
    """Inline-button press. Same whitelist check as typed commands."""
    from_user = cb.get("from", {}) or {}
    chat = (cb.get("message") or {}).get("chat", {}) or {}
    data = (cb.get("data") or "").strip()
    try:
        # Stop the button spinner right away; handlers can take seconds.
        api_call("answerCallbackQuery", {"callback_query_id": cb["id"]})
    except Exception as e:
        print(f"answerCallbackQuery failed: {e}")
    if from_user.get("id") != ALLOWED_CHAT_ID or chat.get("id") != ALLOWED_CHAT_ID:
        log_unauthorized(from_user, chat, f"[button] {data}")
        alert_unauthorized(from_user, chat, f"[button] {data}")
        return
    # Buttons may carry an argument, e.g. "/unban 1.2.3.4" from the unban picker.
    parts = data.split(maxsplit=1)
    handler = COMMANDS.get(parts[0]) if parts else None
    if handler:
        send_reply(ALLOWED_CHAT_ID, handler(parts[1] if len(parts) > 1 else ""))


def handle_update(update):
    cb = update.get("callback_query")
    if cb:
        handle_callback(cb)
        return
    msg = update.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    if chat_id != ALLOWED_CHAT_ID:
        from_user = msg.get("from", {}) or {}
        text = (msg.get("text") or "").strip()
        log_unauthorized(from_user, msg["chat"], text)
        alert_unauthorized(from_user, msg["chat"], text)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    handler = COMMANDS.get(cmd)
    if handler:
        send_reply(chat_id, handler(args))
    else:
        send_message(chat_id, f"Unknown command: {cmd}\nTry /help", menu=True)


# --- Guacamole login alerts ----------------------------------------------
LOGIN_STATE_FILE = os.environ.get(
    "LOGIN_STATE_FILE", os.path.join(BOT_DIR, "login_watch_state.txt")
)
LOGIN_POLL_S = 20
LOGIN_DEDUPE_S = 3600  # one alert per (user, IP) per hour
UA_LOG_RE = re.compile(
    r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\w+) (\S+)[^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"'
)
_whois_cache = {}


def load_login_cursor():
    try:
        with open(LOGIN_STATE_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def save_login_cursor(cursor):
    try:
        with open(LOGIN_STATE_FILE, "w") as f:
            f.write(str(cursor))
    except Exception as e:
        print(f"save_login_cursor failed: {e}")


def guac_sql(sql):
    return run(GUAC_DB_CMD + '"' + sql + '"', timeout=20)


def current_max_history_id():
    out = guac_sql("select coalesce(max(history_id),0) from guacamole_user_history")
    return int(out) if out.strip().isdigit() else None


def new_login_rows(cursor):
    """Login records after `cursor`:
    (history_id, user_id, username, ip, epoch, earlier_rows_for_ip, first_seen)."""
    sql = (
        "select h.history_id, h.user_id, h.username, coalesce(h.remote_host,''), "
        "extract(epoch from h.start_date)::bigint, "
        "(select count(*) from guacamole_user_history p "
        "where p.remote_host = h.remote_host and p.history_id < h.history_id), "
        f"coalesce((select to_char(min(p.start_date) at time zone '{DB_TIMEZONE}','YYYY-MM-DD') "
        "from guacamole_user_history p where p.remote_host = h.remote_host),'') "
        f"from guacamole_user_history h where h.history_id > {int(cursor)} "
        "order by h.history_id"
    )
    rows = []
    for line in guac_sql(sql).splitlines():
        f = line.split("|")
        if len(f) == 7 and f[0].isdigit() and f[4].isdigit():
            rows.append((int(f[0]), f[1], f[2], f[3], int(f[4]), int(f[5]), f[6]))
    return rows


def valid_ip(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def whois_summary(ip):
    if ip in _whois_cache:
        return _whois_cache[ip]
    if not valid_ip(ip) or ipaddress.ip_address(ip).is_private:
        return "private/internal address"
    out = run(f"timeout 15 whois {ip}", timeout=20)
    wanted = (
        ("net", ("netname",)),
        ("org", ("orgname", "org-name", "organization", "owner")),
        ("desc", ("descr",)),
        ("country", ("country",)),
        ("range", ("cidr", "inetnum", "netrange")),
        ("asn", ("origin", "originas")),
    )
    fields = {}
    for line in out.splitlines():
        if ":" not in line or line.startswith(("%", "#")):
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if not v:
            continue
        for want, keys in wanted:
            if k in keys and want not in fields:
                fields[want] = v
    parts = []
    if fields.get("org") or fields.get("desc"):
        parts.append(f"Org: {fields.get('org') or fields.get('desc')}")
    if fields.get("net"):
        parts.append(f"Net: {fields['net']}")
    if fields.get("range"):
        parts.append(f"Range: {fields['range']}")
    if fields.get("country"):
        parts.append(f"Country: {fields['country']}")
    if fields.get("asn"):
        parts.append(f"ASN: {fields['asn']}")
    summary = " | ".join(parts) or "no whois data"
    _whois_cache[ip] = summary
    return summary


def reverse_dns(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return "none"


def login_user_agent(ip, epoch, statuses=("200",)):
    """User agent of the nginx POST /api/tokens (login) closest to the login time."""
    best = None
    for path in (GUAC_LOG, GUAC_LOG + ".1"):
        raw = run(f"sudo -n tail -n 6000 {path}", timeout=20)
        for line in raw.splitlines():
            m = UA_LOG_RE.match(line)
            if not m or m.group(1) != ip:
                continue
            if m.group(3) != "POST" or not m.group(4).startswith("/api/tokens"):
                continue
            if m.group(5) not in statuses:
                continue
            try:
                t = time.mktime(time.strptime(m.group(2).split()[0], "%d/%b/%Y:%H:%M:%S"))
            except ValueError:
                continue
            d = abs(t - epoch)
            if best is None or d < best[0]:
                best = (d, m.group(6))
        if best and best[0] <= 120:
            break
    if best and best[0] <= 600:
        return best[1], ""
    if best:
        return best[1], f" (closest match, {int(best[0] / 60)} min off)"
    return "unknown (not in recent nginx logs)", ""


_server_ip_cache = {"ip": None, "at": 0}


def server_public_ip():
    """This VPS's own public IP via api.ipify.org (it returns the caller's address). Cached 1 h."""
    if _server_ip_cache["ip"] and time.time() - _server_ip_cache["at"] < 3600:
        return _server_ip_cache["ip"]
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=8) as resp:
            ip = resp.read().decode().strip()
    except Exception:
        return _server_ip_cache["ip"] or "unknown"
    if not valid_ip(ip):
        return "unknown"
    _server_ip_cache.update(ip=ip, at=time.time())
    return ip


def login_ok_in_log(ip, epoch):
    """True if nginx shows this IP completing a login (POST /api/tokens -> 200) at or after `epoch`.
    Guacamole writes its history row at the password step, before the TOTP code is entered,
    so the 200 follows the row by however long typing the code takes."""
    for line in run(f"sudo -n tail -n 6000 {GUAC_LOG}", timeout=20).splitlines():
        m = UA_LOG_RE.match(line)
        if not m or m.group(1) != ip or m.group(3) != "POST":
            continue
        if not m.group(4).startswith("/api/tokens") or m.group(5) != "200":
            continue
        try:
            t = time.mktime(time.strptime(m.group(2).split()[0], "%d/%b/%Y:%H:%M:%S"))
        except ValueError:
            continue
        if epoch - 5 <= t <= epoch + 300:
            return True
    return False


def wait_login_ok(ip, epoch, wait_s=90):
    end = time.time() + wait_s
    while True:
        if login_ok_in_log(ip, epoch):
            return True
        if time.time() >= end:
            return False
        time.sleep(5)


def describe_ua(ua):
    browser = "?"
    for pat, name in (
        (r"Edg/([\d.]+)", "Edge"),
        (r"OPR/([\d.]+)", "Opera"),
        (r"Firefox/([\d.]+)", "Firefox"),
        (r"Chrome/([\d.]+)", "Chrome"),
        (r"Version/([\d.]+).*Safari", "Safari"),
    ):
        m = re.search(pat, ua)
        if m:
            browser = f"{name} {m.group(1).split('.')[0]}"
            break
    system = "?"
    for pat, name in (
        ("Windows NT 10", "Windows 10/11"),
        ("Windows", "Windows"),
        ("Android", "Android"),
        ("iPhone|iPad", "iOS"),
        ("Mac OS X", "macOS"),
        ("CrOS", "ChromeOS"),
        ("Linux|X11", "Linux"),
    ):
        if re.search(pat, ua):
            system = name
            break
    return f"{browser} on {system}"


def format_login_alert(row):
    _hid, uid, user, ip, epoch, earlier, first_seen = row
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(epoch))
    lines = [
        "🔐 Guacamole login",
        f"User: {user} (Guacamole user_id={uid})",
        f"Time: {when}",
        f"Server: {server_public_ip()}",
    ]
    if ip in PROXY_IPS or not valid_ip(ip):
        lines.append(f"IP: unavailable (recorded as {ip or 'empty'} - proxy address, X-Forwarded-For not applied?)")
        ua_ip = None
    else:
        lines.append(f"IP: {ip}")
        lines.append(f"  ISP: {ip_info(ip)}")
        lines.append(f"  rDNS: {reverse_dns(ip)}")
        ua_ip = ip
    if earlier == 0:
        lines.append("History: ⚠️ FIRST TIME this IP has been seen")
    else:
        lines.append(f"History: seen before ({earlier} earlier login records, first seen {first_seen})")
    # The history row is written at the password step; the 2FA code follows. Wait for the
    # completed login so the alert is true (and the 200/403 lines carry the same user agent).
    login_done = wait_login_ok(ua_ip, epoch) if ua_ip else True
    if ua_ip:
        ua, note = login_user_agent(ua_ip, epoch, statuses=("200", "403"))
        lines.append(f"User agent: {ua[:220]}{note}")
        if not ua.startswith("unknown") and describe_ua(ua) != "? on ?":
            lines.append(f"  => {describe_ua(ua)}")
    if login_done:
        lines.append("Auth: password + 2FA (TOTP) accepted")
    else:
        lines.append("Auth: ⚠️ password accepted, but no completed 2FA login within 90s (wrong/missing code?)")
    if earlier == 0:
        lines.append("Not you? Change the password, reset 2FA and block the IP.")
    return "\n".join(lines)


def send_alert_with_retry(text, attempts=3):
    for i in range(attempts):
        try:
            send_message(ALLOWED_CHAT_ID, text)
            return True
        except Exception as e:
            print(f"login alert send failed ({i + 1}/{attempts}): {e}")
            time.sleep(3 * (i + 1))
    return False


def login_watcher():
    cursor = load_login_cursor()
    while cursor is None:
        cursor = current_max_history_id()  # first run: only alert for logins from now on
        if cursor is None:
            time.sleep(LOGIN_POLL_S)
        else:
            save_login_cursor(cursor)
    print(f"login watcher started, cursor={cursor}")
    last_alert = {}
    while True:
        try:
            for row in new_login_rows(cursor):
                key = (row[2], row[3])
                if row[4] - last_alert.get(key, 0) >= LOGIN_DEDUPE_S:
                    if not send_alert_with_retry(format_login_alert(row)):
                        break  # keep the cursor; retry on the next poll
                    last_alert[key] = row[4]
                cursor = row[0]
                save_login_cursor(cursor)
        except Exception as e:
            print(f"login watcher error: {e}")
        time.sleep(LOGIN_POLL_S)


# --- Restart / reboot health watcher ---------------------------------------
# Fires only when the VPS reboots or a guac container restarts, and alerts
# ONLY if the post-restart health check finds a problem -- a clean restart
# is logged quietly (journal), not messaged.
RESTART_STATE_FILE = os.environ.get(
    "RESTART_STATE_FILE", os.path.join(BOT_DIR, "restart_watch_state.txt")
)
RESTART_POLL_S = 20
RESTART_GRACE_S = 15  # settle time after a restart is detected, before checking health
GUAC_CONTAINERS = ["guacstack-guacamole", "guacstack-guacd", "guacstack-postgres"]
# Known, already-diagnosed noise that is NOT a sign of a bad restart (e.g. the
# client-network reconnect timeouts, or postgres logging its own shutdown).
RESTART_LOG_NOISE = {
    "guacstack-guacd": ["User is not responding"],
    "guacstack-guacamole": ["terminated abnormally", "InMemoryAuthenticationFailureTracker"],
    "guacstack-postgres": ["terminating connection due to administrator command"],
}


def host_boot_id():
    out = run("cat /proc/sys/kernel/random/boot_id", timeout=5)
    out = out.strip()
    return out if out and not out.startswith("(error") else None


def container_started_at(name):
    return run(f"sudo -n docker inspect -f '{{{{.State.StartedAt}}}}' {name}", timeout=10).strip()


def restart_fingerprint():
    """Changes whenever the host reboots OR any guac container restarts."""
    boot_id = host_boot_id() or "?"
    parts = [boot_id] + [f"{n}={container_started_at(n)}" for n in GUAC_CONTAINERS]
    return "|".join(parts)


def check_guac_health():
    """Run shortly after a restart. Returns a list of problem strings (empty = healthy)."""
    problems = []
    statuses = run(
        "sudo -n docker ps -a --filter name=guacstack "
        "--format '{{.Names}}|{{.State}}|{{.Status}}'",
        timeout=10,
    )
    seen = set()
    for line in statuses.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, state, status = parts
        seen.add(name)
        if state != "running":
            problems.append(f"{name}: not running ({status})")
        elif "unhealthy" in status.lower():
            problems.append(f"{name}: {status}")
    for name in GUAC_CONTAINERS:
        if name not in seen:
            problems.append(f"{name}: container missing")

    http_code = run(
        "curl -s -o /dev/null -w '%{http_code}' --max-time 5 "
        "http://127.0.0.1:8080/guacamole/",
        timeout=10,
    ).strip()
    if http_code != "200":
        problems.append(f"guacamole web app not responding (HTTP {http_code or 'no response'})")

    nginx_state = run("sudo -n systemctl is-active nginx", timeout=5).strip()
    if nginx_state != "active":
        problems.append(f"nginx not active ({nginx_state})")

    for name in GUAC_CONTAINERS:
        noise = RESTART_LOG_NOISE.get(name, [])
        excl = " ".join(f"| grep -vF '{p}'" for p in noise)
        errs = run(
            f"sudo -n docker logs --since 90s {name} 2>&1 "
            f"| grep -iE 'error|exception|fatal' {excl} | tail -5",
            timeout=10,
        ).strip()
        if errs and errs != "(no output)":
            problems.append(f"{name} new error (last 90s): {errs[:300]}")

    return problems


def restart_watcher():
    fp = restart_fingerprint()
    while fp.split("|")[0] == "?":
        time.sleep(RESTART_POLL_S)
        fp = restart_fingerprint()
    prev = load_text_state(RESTART_STATE_FILE)
    if prev is None:
        prev = fp
        save_text_state(RESTART_STATE_FILE, prev)
    print(f"restart watcher started, boot_id={fp.split('|')[0]}")
    while True:
        time.sleep(RESTART_POLL_S)
        try:
            cur = restart_fingerprint()
            if cur == prev:
                continue
            boot_changed = cur.split("|")[0] != prev.split("|")[0]
            kind = "VPS reboot" if boot_changed else "Guacamole container restart"
            print(f"restart watcher: {kind} detected, checking health after grace period")
            time.sleep(RESTART_GRACE_S)
            problems = check_guac_health()
            if problems:
                text = (
                    f"⚠️ {kind} detected — {len(problems)} issue(s) found:\n\n"
                    + "\n".join(f"- {p}" for p in problems)
                )
                send_alert_with_retry(text)
            else:
                print(f"restart watcher: {kind}, all healthy, no alert sent")
            prev = restart_fingerprint()
            save_text_state(RESTART_STATE_FILE, prev)
        except Exception as e:
            print(f"restart watcher error: {e}")


# --- Guacamole connection alerts (which RDP/SSH connection was opened) -----
CONN_STATE_FILE = os.environ.get(
    "CONN_STATE_FILE", os.path.join(BOT_DIR, "conn_watch_state.txt")
)
CONN_POLL_S = 20
CONN_DEDUPE_S = 3600  # one alert per (user, connection, IP) per hour; reconnects stay quiet
PROTOCOL_NAMES = {"rdp": "RDP", "ssh": "SSH", "vnc": "VNC", "telnet": "Telnet"}


def load_conn_cursor():
    try:
        with open(CONN_STATE_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def save_conn_cursor(cursor):
    try:
        with open(CONN_STATE_FILE, "w") as f:
            f.write(str(cursor))
    except Exception as e:
        print(f"save_conn_cursor failed: {e}")


def current_max_conn_history_id():
    out = guac_sql("select coalesce(max(history_id),0) from guacamole_connection_history")
    return int(out) if out.strip().isdigit() else None


def new_connection_rows(cursor):
    """Connections opened after `cursor`:
    (history_id, username, connection_name, protocol, host, port, ip, epoch)."""
    sql = (
        "select h.history_id, h.username, coalesce(h.connection_name,''), "
        "coalesce(c.protocol,''), coalesce(hp.parameter_value,''), "
        "coalesce(pp.parameter_value,''), coalesce(h.remote_host,''), "
        "extract(epoch from h.start_date)::bigint "
        "from guacamole_connection_history h "
        "left join guacamole_connection c on c.connection_id = h.connection_id "
        "left join guacamole_connection_parameter hp on hp.connection_id = h.connection_id "
        "and hp.parameter_name = 'hostname' "
        "left join guacamole_connection_parameter pp on pp.connection_id = h.connection_id "
        "and pp.parameter_name = 'port' "
        f"where h.history_id > {int(cursor)} "
        "order by h.history_id"
    )
    rows = []
    for line in guac_sql(sql).splitlines():
        f = line.split("|")
        if len(f) == 8 and f[0].isdigit() and f[7].isdigit():
            rows.append((int(f[0]), f[1], f[2], f[3], f[4], f[5], f[6], int(f[7])))
    return rows


def format_connection_alert(row):
    _hid, user, name, protocol, host, port, ip, epoch = row
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(epoch))
    kind = PROTOCOL_NAMES.get(protocol, protocol.upper() or "unknown")
    target = f"{host}:{port}" if host and port else host or "?"
    lines = [
        f"🖥️ Guacamole connection opened: {kind}",
        f"User: {user}",
        f"Connection: {name or '?'}",
        f"Type: {kind} -> {target}",
    ]
    if ip in PROXY_IPS or not valid_ip(ip):
        lines.append(f"IP: unavailable (recorded as {ip or 'empty'})")
    else:
        lines.append(f"IP: {ip}")
    lines.append(f"Time: {when}")
    return "\n".join(lines)


def connection_watcher():
    cursor = load_conn_cursor()
    while cursor is None:
        cursor = current_max_conn_history_id()  # first run: only alert for connections from now on
        if cursor is None:
            time.sleep(CONN_POLL_S)
        else:
            save_conn_cursor(cursor)
    print(f"connection watcher started, cursor={cursor}")
    last_alert = {}
    while True:
        try:
            for row in new_connection_rows(cursor):
                key = (row[1], row[2], row[6])
                if row[7] - last_alert.get(key, 0) >= CONN_DEDUPE_S:
                    if not send_alert_with_retry(format_connection_alert(row)):
                        break  # keep the cursor; retry on the next poll
                    last_alert[key] = row[7]
                cursor = row[0]
                save_conn_cursor(cursor)
        except Exception as e:
            print(f"connection watcher error: {e}")
        time.sleep(CONN_POLL_S)


# --- Guacamole failed-login / 2FA / auto-block alerts ----------------------
AUTH_STATE_FILE = os.environ.get(
    "AUTH_STATE_FILE", os.path.join(BOT_DIR, "auth_watch_state.txt")
)
AUTH_POLL_S = 15
AUTH_RATE_S = 60  # at most one alert per (IP, kind) per minute; the rest are summarised
GUAC_CONTAINER = "guacstack-guacamole"
AUTH_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z) \S+ \[[^\]]+\] \w+\s+(\S+) - (.*)$"
)
AUTH_FAIL_RE = re.compile(
    r'Authentication attempt from (?:\[([^\]]*)\]|(\S+)) for user "(.*)" failed'
    r'(?:: (.*?))?(?: \(rejected by "([^"]*)"\))?$'
)
AUTH_TRACK_RE = re.compile(
    r'Authentication has failed for address "([^"]+)" \(current total failures: (\d+)/(\d+)\)'
)
AUTH_BLOCK_RE = re.compile(r'Blocking authentication attempt from address "([^"]+)"')


def load_text_state(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except Exception:
        return None


def save_text_state(path, value):
    try:
        with open(path, "w") as f:
            f.write(value)
    except Exception as e:
        print(f"save state failed ({path}): {e}")


def clean_text(value, limit=60):
    return re.sub(r"[^\x20-\x7e]", "?", value)[:limit]


def ts_epoch(ts):
    return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def parse_auth_log(text, cursor):
    """Failed-login events from `docker logs -t` text, newer than `cursor`."""
    events, last_ts, track = [], cursor, {}
    for line in text.splitlines():
        m = AUTH_LINE_RE.match(line)
        if not m:
            continue
        ts, _logger, msg = m.groups()
        if ts <= cursor:
            continue
        last_ts = ts
        t = AUTH_TRACK_RE.search(msg)
        if t:
            track[t.group(1)] = (int(t.group(2)), int(t.group(3)))
            continue
        f = AUTH_FAIL_RE.search(msg)
        if f:
            ip = (f.group(1) or f.group(2) or "").split(",")[0].strip()
            blob = ((f.group(4) or "") + " " + (f.group(5) or "")).lower()
            events.append(
                {
                    "ts": ts,
                    "kind": "2fa_failed" if "totp" in blob else "bad_password",
                    "ip": ip,
                    "user": clean_text(f.group(3)),
                    "failures": track.get(ip),
                }
            )
            continue
        b = AUTH_BLOCK_RE.search(msg)
        if b:
            ip = b.group(1)
            events.append(
                {"ts": ts, "kind": "blocked", "ip": ip, "user": "", "failures": track.get(ip)}
            )
    return events, last_ts


def format_auth_alert(ev, suppressed=0, summary=False):
    kind, ip = ev["kind"], ev["ip"]
    epoch = ts_epoch(ev["ts"])
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(epoch))
    heads = {
        "2fa_failed": "🔑 Guacamole 2FA code REJECTED",
        "bad_password": "❌ Guacamole login FAILED (wrong username/password)",
        "blocked": "🚫 Guacamole auto-block active for this IP",
    }
    lines = [heads.get(kind, "Guacamole auth event")]
    if summary:
        lines.append(f"(summary: {suppressed} more similar attempt(s) in the last minute)")
    if ev["user"]:
        lines.append(f"Attempted username: {ev['user']}")
    lines.append(f"Time: {when}")
    if valid_ip(ip):
        lines.append(f"IP: {ip}")
        lines.append(f"  ISP: {ip_info(ip)}")
        lines.append(f"  rDNS: {reverse_dns(ip)}")
    else:
        lines.append(f"IP: {clean_text(ip) or 'unknown'}")
    if ev.get("failures"):
        n, m = ev["failures"]
        lines.append(f"Failures from this IP: {n}/{m} (Guacamole blocks the IP for 5 min at {m})")
    if kind == "blocked":
        lines.append("Guacamole is rejecting ALL login attempts from this IP for a few minutes.")
    if kind == "2fa_failed":
        lines.append("⚠️ The PASSWORD was accepted - only the 2FA code was wrong.")
        lines.append("If this was not you, the password is compromised: change it and reset 2FA.")
    if valid_ip(ip):
        ua, note = login_user_agent(ip, epoch, statuses=("403", "400", "401", "429"))
        lines.append(f"User agent: {ua[:220]}{note}")
        if not ua.startswith("unknown") and describe_ua(ua) != "? on ?":
            lines.append(f"  => {describe_ua(ua)}")
    if suppressed and not summary:
        lines.append(f"(+{suppressed} similar attempt(s) since the previous alert)")
    return "\n".join(lines)


def auth_event_watcher():
    cursor = load_text_state(AUTH_STATE_FILE)
    if cursor is None:
        cursor = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000000Z"
        save_text_state(AUTH_STATE_FILE, cursor)
    print(f"auth watcher started, cursor={cursor}")
    sent, held = {}, {}
    while True:
        try:
            text = run(f"sudo -n docker logs -t --since {cursor} {GUAC_CONTAINER} 2>&1", timeout=30)
            events, last_ts = parse_auth_log(text, cursor)
            ok = True
            for ev in events:
                key = (ev["ip"], ev["kind"])
                if time.time() - sent.get(key, 0) < AUTH_RATE_S:
                    held.setdefault(key, [0, ev])
                    held[key][0] += 1
                    held[key][1] = ev
                else:
                    n = held.pop(key, [0, None])[0]
                    if not send_alert_with_retry(format_auth_alert(ev, n)):
                        ok = False
                        break
                    sent[key] = time.time()
                cursor = ev["ts"]
                save_text_state(AUTH_STATE_FILE, cursor)
            if ok and last_ts > cursor:
                cursor = last_ts
                save_text_state(AUTH_STATE_FILE, cursor)
            for key, (n, ev) in list(held.items()):
                if n and time.time() - sent.get(key, 0) >= AUTH_RATE_S:
                    if send_alert_with_retry(format_auth_alert(ev, n, summary=True)):
                        sent[key] = time.time()
                        held.pop(key)
        except Exception as e:
            print(f"auth watcher error: {e}")
        time.sleep(AUTH_POLL_S)


def main():
    offset = load_offset()
    print(f"Starting bot, offset={offset}")
    register_commands()
    threading.Thread(target=login_watcher, daemon=True, name="login-watcher").start()
    threading.Thread(target=connection_watcher, daemon=True, name="conn-watcher").start()
    threading.Thread(target=auth_event_watcher, daemon=True, name="auth-watcher").start()
    threading.Thread(target=restart_watcher, daemon=True, name="restart-watcher").start()
    while True:
        try:
            resp = api_call(
                "getUpdates",
                {"offset": offset, "timeout": 30},
                timeout=40,
            )
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                handle_update(update)
                save_offset(offset)
        except Exception as e:
            print(f"poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
