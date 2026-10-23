# Add your own Linux command to the bot

Every place you need to touch in `bot.py` is marked:

```
# ➕ ADD YOUR COMMAND HERE (step N/5)
```

Search for `ADD YOUR COMMAND` in your editor to jump between them.

---

## Example: `/uptime`

### Step 1/5 — write the handler

Put it next to the other `cmd_*` functions (the marker is just above `cmd_help`):

```python
def cmd_uptime(_args):
    return "Uptime:\n" + run("uptime -p")
```

- A handler receives `args` (the text typed after the command, or `""`) and
  **returns** what the bot should reply.
- `run(cmd, timeout=10)` runs a shell command and returns stdout + stderr as text.
  It never raises: timeouts and errors come back as `(command timed out)` /
  `(error: ...)`.
- Replies longer than ~3,900 characters are cut. For long output, return a
  **list** of strings and each becomes its own message (see `chunk_messages()`).

### Step 2/5 — register it in `COMMANDS`

```python
COMMANDS = {
    ...
    "/uptime": cmd_uptime,
}
```

The key must be lowercase and start with `/`. This alone makes the command
work when typed.

### Step 3/5 — (optional) add a button to `MENU`

`MENU` is the button grid under every reply. Each inner list is one row
(the existing rows have 2 buttons each):

```python
    [{"text": "Uptime", "callback_data": "/uptime"}],
```

`callback_data` is exactly what the button "types", so it can include
arguments, e.g. `"/banned sshd"`. Keep it under 64 bytes (Telegram limit).

### Step 4/5 — (optional) add it to Telegram's **Menu** button

```python
    {"command": "uptime", "description": "Server uptime"},
```

No leading `/`, lowercase letters/digits/underscores only. The bot sends this
list to Telegram on start (`setMyCommands`), so restart to see it.

### Step 5/5 — describe it in `/help`

```python
        "/uptime - how long the server has been up\n"
```

### Apply

```bash
python3 -m py_compile bot.py && sudo systemctl restart telegram-vps-status-bot
```

`py_compile` catches typos before the restart, so a mistake never takes the bot
offline.

---

## More recipes

### A command that needs root

The bot runs as a normal user, so call `sudo -n` (non-interactive: it fails
instead of hanging on a password prompt):

```python
def cmd_disk(_args):
    return "Largest folders in /var:\n" + run("sudo -n du -xh /var --max-depth=1 | sort -rh | head", timeout=30)
```

If your user does not have `NOPASSWD: ALL`, allow just that command:

```bash
echo 'guest ALL=(root) NOPASSWD: /usr/bin/du' | sudo tee /etc/sudoers.d/telegram-bot-extra
sudo chmod 440 /etc/sudoers.d/telegram-bot-extra && sudo visudo -c
```

### A command that takes an argument — validate it first

`run()` uses the shell, so **never** paste user text into it unchecked. A
message like `/ping 8.8.8.8; rm -rf ~` would run both commands. Validate, then use
only the cleaned value:

```python
def cmd_ping(args):
    try:
        ip = str(ipaddress.ip_address(args.strip()))   # rejects anything that is not an IP
    except ValueError:
        return "Usage: /ping <ip>"
    return run(f"ping -c 3 -W 2 {ip}", timeout=15)
```

For service names, file names, etc. check against a fixed allow-list:

```python
ALLOWED_SERVICES = {"nginx", "docker", "fail2ban", "ssh"}

def cmd_restart(args):
    svc = args.strip().lower()
    if svc not in ALLOWED_SERVICES:
        return "Usage: /restart <" + "|".join(sorted(ALLOWED_SERVICES)) + ">"
    return run(f"sudo -n systemctl restart {svc} && systemctl is-active {svc}", timeout=30)
```

### A command that changes something — add a confirm button

Copy the pattern from `cmd_reboot`: without the word `confirm` it only returns
a question with buttons; the button sends the command again with `confirm`.
Returning a `(text, markup)` tuple shows your own buttons instead of the main menu.

```python
def cmd_clearcache(args):
    if args.strip().lower() != "confirm":
        markup = json.dumps({"inline_keyboard": [
            [{"text": "⚠️ Yes, clear it", "callback_data": "/clearcache confirm"}],
            [{"text": "Cancel", "callback_data": "/help"}],
        ]})
        return ("This deletes the apt cache. Continue?", markup)
    return run("sudo -n apt-get clean && echo done", timeout=60)
```

### A slow command

`run()` defaults to a 10 s timeout. Pass a bigger one (`timeout=120`). The bot
answers one message at a time, so keep commands short; for long jobs start them
in the background (`systemd-run --unit=mytask ...`) and add a second command
that reports their status.

---

## Safety checklist

- [ ] User input is validated (IP parser, allow-list, or regex) before `run()`.
- [ ] Anything destructive has a confirm step.
- [ ] Output never contains secrets (tokens, passwords, private keys).
- [ ] `python3 -m py_compile bot.py` passes before restarting.
- [ ] `ALLOWED_CHAT_ID` is still only your own ID.
