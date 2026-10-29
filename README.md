# Telegram VPS Status Bot

A single-file Python bot that lets you check and manage your own Linux VPS from
Telegram: host health, Docker containers, xrdp, nginx, who is logged in, banned
IPs (fail2ban / UFW / nginx), one-tap unban, safe reboot — plus instant alerts
for Apache Guacamole logins, failed logins, 2FA problems, dropped sessions and
bad restarts.

- **Python 3 standard library only** — no `pip install`.
- **Locked to one chat.** Anyone else who messages the bot is logged and you get
  an alert with their name, username, Telegram ID and profile photo.
- **Read-only except two commands:** `/unban` (removes a ban) and `/reboot`
  (asks you to confirm first). Everything else only reads.

> This is step **4** of a small series of scripts for building a hardened,
> browser-accessible Debian server. See [Setup order](#setup-order) below.

---

## Setup order

Run the repos in this order on a fresh Debian/Ubuntu VPS. Each step builds on
the one before it.

| Step | Repo | What it gives you | Needed for this bot? |
|---|---|---|---|
| **1** | [hardening-script](https://github.com/sheloab/hardening-script) | Sudo user, SSH lockdown, UFW allowlist, fail2ban (`sshd`, `recidive`, `portscan` jails), sysctl hardening, NTP | **Recommended** — `/banned`, `/allbans` and `/unban` read these jails |
| **2** | [hardening-nginx-script](https://github.com/sheloab/hardening-nginx-script) | Hardened nginx: security headers, TLS, rate limits | Recommended — `/nginx` and `/status` check nginx |
| **3** | [xfce-remote-desktop-setup](https://github.com/sheloab/xfce-remote-desktop-setup) | XFCE desktop + xrdp (RDP on 3389) | Optional — `/xrdp` and the session list use it |
| — | Apache Guacamole in Docker (your own stack) | Browser access to the RDP/SSH desktop behind nginx | Optional — enables the Guacamole login/connection/2FA alerts |
| **4** | **telegram-vps-status-bot** (this repo) | Telegram control panel + alerts for everything above | — |

The bot still runs if you skip the optional parts: the matching checks show
as down or empty. Two exceptions to know about:

- The `/banned` groups **search engines, AI crawlers, other bots, scanners,
  hidden-file probers and Guacamole** read the fail2ban jails `nginx-bot`,
  `nginx-probe` and `guacamole-nginx` and the files `/var/log/nginx/bots/*.log`,
  `/etc/nginx/bot-deny.conf`, `/etc/nginx/guac-deny.conf`. None of the repos
  above create these; without your own jails/files those groups stay empty.
- **Without Guacamole**, the restart/reboot watcher reports the three
  `guacstack-*` containers as missing after every server reboot.

---

## Commands

| Command | What it does |
|---|---|
| `/status` | Host CPU/memory/disk, failed systemd units, core services, website checks (every nginx site, found automatically), active sessions, every container |
| `/sessions` | Who is connected: user, start time, client IP, ISP/ASN, via Guacamole / SSH / RDP |
| `/docker` | All containers with status and ports |
| `/xrdp` | xrdp + xrdp-sesman state and leaked Xorg session check |
| `/nginx` | nginx state + last 5 lines of the Guacamole site's error log |
| `/intruders` | Recent unauthorized attempts to use the bot |
| `/banned [group]` | Banned IPs grouped by type (search engines, AI crawlers, scanners, SSH, port scans, Guacamole, repeat offenders) |
| `/allbans` | Every block on the server: all fail2ban jails, UFW deny rules, nginx deny files |
| `/unban [ip] [jail]` | Without arguments: tap-to-unban buttons. With an IP: remove it from every jail (or just one) |
| `/reboot` | Reboots the VPS after a **Confirm** tap |
| `/help` | Command list + button menu |

### Background alerts (no command needed)

- **Guacamole login** — user, IP, ISP, reverse DNS, browser/OS, and whether the
  2FA (TOTP) step completed within 90 s.
- **Guacamole connection opened** — which RDP/SSH connection, from which IP.
  Login and connection alerts are sent once per user/IP per hour, so reconnects
  stay quiet; this memory is kept on disk, so restarting the bot doesn't repeat them.
- **Guacamole session dropped** — a session that ended abnormally (e.g. the
  browser stopped answering for 15 s on a flaky Wi-Fi link): connection, time,
  session length and reason. The first drop is sent at once; further drops within
  10 minutes come as one summary. Normal disconnects stay quiet.
- **Failed logins / wrong 2FA / auto-blocked IPs** — at most one alert per IP
  and alert type per minute; the rest are summarised.
- **Restart / reboot health check** — after a reboot or a Guacamole container
  restart, the bot checks the stack and messages you **only if something is wrong**.

---

## Requirements

- Debian or Ubuntu with `systemd` and **Python 3.8 or newer** (the bot is
  imported and checked on Python 3.8, 3.9 and 3.13; it runs in production on
  Debian 13).
- A user with **passwordless sudo** (step 1 creates one). The bot runs
  `sudo -n` for `docker`, `fail2ban-client`, `systemctl`, `ufw`, `journalctl`,
  `tail`, `cat`, `sh` and `reboot`.
- Packages: `curl`, `whois`, `iproute2` (`ss`), `procps` (`ps`, `pgrep`,
  `free`, `uptime`) — `sudo apt install curl whois iproute2 procps`
- Optional: Docker, fail2ban, UFW, nginx, xrdp + XFCE, and a Guacamole stack
  whose containers are named `guacstack-guacamole`, `guacstack-guacd`,
  `guacstack-postgres` (database `guacamole_db`, user `guacamole_user`) with the
  web app on `http://127.0.0.1:8080/guacamole/` — change these in `bot.py` if
  yours differ.

---

## Install

The examples use the Linux user **`guest`** — the same user
[hardening-script](https://github.com/sheloab/hardening-script) (step 1)
creates by default — and the host name `PRIVATE-SERVER`. Linux user names are
case-sensitive (`guest` ≠ `GUEST`). If your user is different, replace `guest`
everywhere below. Everything lines up like this:

| Item | Value in this repo | Must match |
|---|---|---|
| Linux user running the bot | `guest` | `User=` in the `.service` file |
| Bot folder | `/home/guest/telegram-vps-status-bot` (= `~/telegram-vps-status-bot` when logged in as `guest`) | `WorkingDirectory=`, `EnvironmentFile=`, `ExecStart=` in the `.service` file |
| Settings file | `/home/guest/telegram-vps-status-bot/bot.env` | `EnvironmentFile=` |
| systemd service | `telegram-vps-status-bot` | `.service` file name, `BOT_SERVICE` in `bot.env` |
| Runtime files | next to `bot.py` in the bot folder | automatic |

If you clone to a different folder or rename the service, change those places too.

### 1. Create the bot in Telegram

1. Talk to **@BotFather** → `/newbot` → copy the token.
2. Talk to **@userinfobot** → copy your numeric ID. That is `ALLOWED_CHAT_ID`.

### 2. Get the code

Log in as the bot user (`guest`), then:

```bash
git clone https://github.com/sheloab/telegram-vps-status-bot.git ~/telegram-vps-status-bot
cd ~/telegram-vps-status-bot
chmod 700 .
```

### 3. Configure

```bash
cp bot.env.example bot.env
chmod 600 bot.env
nano bot.env        # set BOT_TOKEN, ALLOWED_CHAT_ID; optionally SITES, SITE_PATHS, GUAC_DOMAIN, DB_TIMEZONE
```

`bot.env` is in `.gitignore` — never commit it.

### 4. Test in the foreground

```bash
set -a; . ./bot.env; set +a
python3 bot.py
```

Send `/help` to your bot. Press `Ctrl+C` when it answers.

### 5. Run it as a service

```bash
# only needed if your user is not "guest":
sed -i "s/guest/$USER/g" deploy/telegram-vps-status-bot.service
sudo cp deploy/telegram-vps-status-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-vps-status-bot
journalctl -u telegram-vps-status-bot -f
```

### 6. (Optional) Keep background polling out of the sudo log

The alert watchers call `sudo` every 15–20 s. This drop-in stops the login,
connection and failed-login watchers (`docker exec … psql`, `docker logs`,
`tail` of the nginx log) from filling the sudo log. It does **not** cover the
restart watcher's `docker inspect` calls (3 every 20 s), which are still
logged. Edit the domain in `deploy/sudoers-telegram-bot-quiet`, then:

```bash
sudo install -m 440 deploy/sudoers-telegram-bot-quiet /etc/sudoers.d/telegram-bot-quiet
sudo visudo -c
```

---

## Add your own Linux command to the menu

Want `/uptime`, `/disk`, `/backup` or any other CLI command as a Telegram
button? It takes 5 small edits in `bot.py`, each marked in the code with
**`# ➕ ADD YOUR COMMAND HERE (step N/5)`**:

1. **Handler** — a function that runs the command with `run()` and returns the text.
2. **`COMMANDS`** — map `/yourcommand` to that function.
3. **`MENU`** — (optional) an inline button under every reply.
4. **`MENU_COMMANDS`** — (optional) an entry in Telegram's blue **Menu** button.
5. **`cmd_help`** — one line in the `/help` text.

Then restart: `sudo systemctl restart telegram-vps-status-bot`.

Full walkthrough with examples (commands that need sudo, commands that take
arguments, commands that need a confirm step, and the safety rules):
**[docs/ADD-YOUR-OWN-COMMAND.md](docs/ADD-YOUR-OWN-COMMAND.md)**.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | The whole bot |
| `bot.env.example` | Settings template (dummy values) → copy to `bot.env` |
| `deploy/telegram-vps-status-bot.service` | systemd unit |
| `deploy/sudoers-telegram-bot-quiet` | Optional sudoers drop-in that silences polling logs |
| `docs/ADD-YOUR-OWN-COMMAND.md` | How to add your own commands and buttons |

Created at runtime next to `bot.py` (git-ignored): `offset.txt`,
`*_state.txt`, `unauthorized_access.log`.

## Security notes

- Only `ALLOWED_CHAT_ID` can run commands; everything else is logged and alerted.
- Keep `bot.env` at `chmod 600`. If the token leaks, `/revoke` it in @BotFather.
- Commands run through a shell. Never pass user-typed text straight into
  `run()` — validate it first (see the guide).
- The bot needs passwordless sudo. Run it only on a server you own and keep the
  bot chat private.

## License

[MIT](LICENSE)
