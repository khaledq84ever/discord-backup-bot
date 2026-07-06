"""Owner-only SSH bridge — /run and /pm2 slash commands + channel relay.

Recovered from the minimal bridge bot that ran on Railway 2026-06-25→07-06
(archived at tools/ssh_bridge_bot_deployed.py) and merged into the full
backup bot so every registered command has a live handler.

Needs SSH_HOST, SSH_USER and SSH_PASSWORD or SSH_PRIVATE_KEY in the env;
if they are absent setup() is a no-op so local/dev runs keep working.
"""
import asyncio
import io
import logging
import os
import time
import uuid

import discord
from discord import app_commands

log = logging.getLogger("sshbridge")

OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
SSH_HOST = os.environ.get("SSH_HOST")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ.get("SSH_USER")
SSH_PASSWORD = os.environ.get("SSH_PASSWORD")
SSH_PRIVATE_KEY = os.environ.get("SSH_PRIVATE_KEY")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
COMMAND_TIMEOUT = int(os.environ.get("COMMAND_TIMEOUT", "20"))

shell_lock = asyncio.Lock()
_ssh_client = None
_shell = None


def _load_private_key(key_str):
    import paramiko
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(io.StringIO(key_str))
        except paramiko.SSHException:
            continue
    raise ValueError("Unsupported SSH_PRIVATE_KEY format")


def _connect_shell():
    global _ssh_client, _shell
    import paramiko
    log.info("Connecting SSH shell...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(hostname=SSH_HOST, port=SSH_PORT, username=SSH_USER, timeout=10)
    if SSH_PRIVATE_KEY:
        connect_kwargs["pkey"] = _load_private_key(SSH_PRIVATE_KEY)
    else:
        connect_kwargs["password"] = SSH_PASSWORD
    ssh.connect(**connect_kwargs)
    shell = ssh.invoke_shell(term="dumb", width=200, height=50)
    shell.settimeout(0.5)
    time.sleep(0.5)
    while shell.recv_ready():
        shell.recv(65536)
    _ssh_client = ssh
    _shell = shell
    log.info("SSH shell connected.")


def _discard_shell():
    global _ssh_client, _shell
    try:
        if _shell is not None:
            _shell.close()
    except Exception:
        pass
    try:
        if _ssh_client is not None:
            _ssh_client.close()
    except Exception:
        pass
    _shell = None
    _ssh_client = None


def run_in_shell_streaming(command: str, loop, on_update):
    global _shell
    if _shell is None or _shell.closed:
        _connect_shell()

    marker = f"__DONE_{uuid.uuid4().hex}__"
    try:
        _shell.send(command + f"\necho {marker}\n")
    except OSError:
        _connect_shell()
        _shell.send(command + f"\necho {marker}\n")

    buf = b""
    last_update = 0.0
    deadline = time.time() + COMMAND_TIMEOUT
    while time.time() < deadline:
        try:
            if _shell.recv_ready():
                buf += _shell.recv(65536)
                text = buf.decode(errors="replace")
                if marker in text:
                    break
                now = time.time()
                if now - last_update > 1:
                    loop.call_soon_threadsafe(on_update, text)
                    last_update = now
            else:
                time.sleep(0.05)
        except Exception as e:
            log.warning("run_in_shell_streaming error: %r", e)
            _discard_shell()
            return f"(shell error: {e})"
    else:
        log.warning("Timed out. Partial buffer: %r", buf[:500])
        _discard_shell()
        partial = buf.decode(errors="replace").strip()
        note = (
            "(no output after {}s — connection reset. This often means the command is "
            "waiting for interactive input, e.g. a Y/n confirmation. Use non-interactive "
            "flags like 'apt install -y ...' instead.)"
        ).format(COMMAND_TIMEOUT)
        if partial:
            return f"{partial}\n\n{note}"
        return note

    text = buf.decode(errors="replace")
    lines = [l for l in text.splitlines() if marker not in l]
    if lines and lines[0].strip() == command.strip():
        lines = lines[1:]
    return "\n".join(lines).strip() or "(no output)"


INTERACTIVE_PROGRAMS = {
    "vim": "use a non-interactive editor command, e.g. `sed -i ...` or `cat > file <<EOF`",
    "vi": "use a non-interactive editor command, e.g. `sed -i ...` or `cat > file <<EOF`",
    "nano": "use a non-interactive editor command, e.g. `sed -i ...` or `cat > file <<EOF`",
    "top": "use `top -bn1` for a one-shot snapshot",
    "htop": "use `top -bn1` for a one-shot snapshot",
    "less": "use `cat` instead",
    "more": "use `cat` instead",
    "man": "use `command --help` instead",
    "gemini": "use `gemini -p \"your prompt\"` or the `ask <question>` wrapper",
    "opencode": "use `opencode run \"your message\"` for a one-shot response",
    "python": "use `python3 -c \"...\"` instead of the bare REPL",
    "python3": "use `python3 -c \"...\"` instead of the bare REPL",
    "mysql": "pass a query directly, e.g. `mysql -e \"SELECT ...\"`",
    "psql": "pass a query directly, e.g. `psql -c \"SELECT ...\"`",
    "ssh": "this channel is already an SSH session; nested ssh will hang waiting for a TTY",
}


def interactive_warning(command: str):
    first_word = command.strip().split(" ", 1)[0].split("/")[-1] if command.strip() else ""
    tip = INTERACTIVE_PROGRAMS.get(first_word)
    if tip:
        return f"`{first_word}` launches an interactive/full-screen UI that won't work in this text-only bridge.\nTip: {tip}"
    return None


CONTROL_KEYS = {
    "!ctrlc": b"\x03",
    "!ctrld": b"\x04",
    "!esc": b"\x1b",
}


def send_control_key(raw: bytes) -> str:
    global _shell
    if _shell is None or _shell.closed:
        return "(no active shell session)"
    _shell.send(raw)
    time.sleep(0.5)
    buf = b""
    while _shell.recv_ready():
        buf += _shell.recv(65536)
    return buf.decode(errors="replace").strip() or "(sent)"


async def execute_streaming(command: str, on_update) -> str:
    loop = asyncio.get_event_loop()
    async with shell_lock:
        return await asyncio.wait_for(
            loop.run_in_executor(None, run_in_shell_streaming, command, loop, on_update),
            timeout=COMMAND_TIMEOUT + 10,
        )


def fmt(header: str, body: str) -> str:
    full = f"{header}\n{body}" if header else body
    return full[-1900:]


async def _run_for_interaction(interaction: discord.Interaction, command: str):
    if interaction.user.id != OWNER_ID:
        log.info("Unauthorized SSH cmd attempt from user.id=%s (%s)", interaction.user.id, interaction.user)
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    warning = interactive_warning(command)
    if warning:
        await interaction.response.send_message(warning, ephemeral=True)
        return

    header = f"$ {command}"
    await interaction.response.defer(thinking=True, ephemeral=True)
    msg = await interaction.followup.send(f"```\n{header}\n(running...)\n```", ephemeral=True, wait=True)

    def on_update(text):
        asyncio.ensure_future(msg.edit(content=f"```\n{fmt(header, text)}\n```"))

    try:
        output = await execute_streaming(command, on_update)
    except asyncio.TimeoutError:
        await msg.edit(content="Command timed out.")
        return
    except Exception as e:
        await msg.edit(content=f"SSH error: {e}")
        return

    await msg.edit(content=f"```\n{fmt(header, output)}\n```")


def setup(bot: discord.Client, tree: app_commands.CommandTree) -> bool:
    """Register /run, /pm2 and the owner channel relay. Returns True if active."""
    if not (SSH_HOST and SSH_USER and (SSH_PASSWORD or SSH_PRIVATE_KEY) and OWNER_ID):
        log.info("SSH bridge disabled — SSH_HOST/SSH_USER/credentials/OWNER_ID not all set")
        return False

    @tree.command(name="run", description="Run a command on your server via SSH (owner only)")
    @app_commands.describe(command="Shell command to run")
    async def run_command(interaction: discord.Interaction, command: str):
        await _run_for_interaction(interaction, command)

    @tree.command(name="pm2", description="Run a pm2 command on your server (owner only)")
    @app_commands.describe(args="pm2 arguments, e.g. 'list' or 'restart all'")
    async def pm2_command(interaction: discord.Interaction, args: str = "list"):
        await _run_for_interaction(interaction, f"pm2 {args}")

    if CHANNEL_ID:
        @bot.event
        async def on_message(message: discord.Message):
            if message.author.id == bot.user.id:
                return
            if str(message.channel.id) != str(CHANNEL_ID):
                return
            if message.author.id != OWNER_ID:
                return

            command = message.content.strip()
            if not command:
                return

            if command.lower() in CONTROL_KEYS:
                loop = asyncio.get_event_loop()
                async with shell_lock:
                    result = await loop.run_in_executor(None, send_control_key, CONTROL_KEYS[command.lower()])
                await message.channel.send(f"```\n{result[-1900:]}\n```")
                return

            warning = interactive_warning(command)
            if warning:
                await message.reply(warning)
                return

            msg = await message.channel.send(f"```\n$ {command}\n(running...)\n```")

            def on_update(text):
                asyncio.ensure_future(msg.edit(content=f"```\n{fmt('', text)}\n```"))

            try:
                output = await execute_streaming(command, on_update)
            except asyncio.TimeoutError:
                await msg.edit(content="Command timed out.")
                return
            except Exception as e:
                await msg.edit(content=f"SSH error: {e}")
                return

            await msg.edit(content=f"```\n{fmt('', output)}\n```")

    log.info("SSH bridge enabled: /run + /pm2 (owner %s)%s", OWNER_ID,
             f", channel relay {CHANNEL_ID}" if CHANNEL_ID else "")
    return True
