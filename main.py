"""
Musy - a free music bot for Yapin' (yapin.gg).

Plays audio from YouTube links or song-name searches in voice channels.
No paid APIs, no premium tier. Uses yt-dlp (search + extraction) and ffmpeg.

Commands (type /name or !name):
  /play <link or song name>   queue a song (joins the voice channel you're in)
  /skip  /pause  /resume  /stop  /queue  /nowplaying
  /247                        toggle 24/7 mode (on = stay forever, off = leave after 10 min idle)
  /musy                       help

Voice calls follow Yapin's developer docs (bot.connect_voice + call.source.capture_frame).
Bots on Yapin' send one audio track only, so there is no screen share / video.
"""
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_env():
    """Tiny .env loader (no dependency). Real environment variables win."""
    p = HERE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

TOKEN = next((os.environ[k] for k in
              ("YAPIN_BOT_TOKEN", "YAPPER_BOT_TOKEN", "YAPIN_TOKEN", "BOT_TOKEN", "TOKEN")
              if os.environ.get(k)), None)
if not TOKEN:
    sys.exit("Musy: no bot token found. Put YAPIN_BOT_TOKEN=yb_... in .env or the host's environment variables.")


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:  # bundled static ffmpeg, so hosts without ffmpeg still work
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()
if not FFMPEG:
    sys.exit("Musy: ffmpeg not found. Add 'imageio-ffmpeg' to requirements.txt (see README).")

import yapper                # noqa: E402
import yt_dlp                # noqa: E402
from livekit import rtc      # noqa: E402

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
IDLE_SECONDS = int(os.getenv("MUSY_IDLE_SECONDS", "600"))          # 10 minutes
MAX_QUEUE = 50
MAX_TRACK_SECONDS = int(os.getenv("MUSY_MAX_MINUTES", "240")) * 60
FRAME_SAMPLES = 480                                                 # 10 ms @ 48 kHz
FRAME_BYTES = FRAME_SAMPLES * 2 * 2                                 # stereo, 16-bit
SETTINGS_FILE = HERE / "musy_data.json"

bot = yapper.Bot(TOKEN)

LOOP = asyncio.new_event_loop()
POOL = ThreadPoolExecutor(max_workers=4)
threading.Thread(target=LOOP.run_forever, daemon=True, name="musy-loop").start()

NOW = ["🎧 Now playing:", "💿 Spinning:", "🎶 Oh I love this one:", "🔊 Turning it up:", "✨ Here we go:"]
QUEUED = ["Great taste! 🎶", "Ooh yes, added! 💿", "My ears are so happy 🎧", "Banger incoming 🔥", "Added to the playlist ✨"]

# ----------------------------------------------------------------------------
# Saved settings (24/7 mode survives restarts)
# ----------------------------------------------------------------------------
_save_lock = threading.Lock()
try:
    SETTINGS = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
except Exception:
    SETTINGS = {}


def server_cfg(sid):
    cfg = SETTINGS.setdefault(str(sid), {})
    cfg["server_id"] = sid
    return cfg


def save_settings():
    with _save_lock:
        try:
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(SETTINGS), encoding="utf-8")
            tmp.replace(SETTINGS_FILE)
        except Exception as e:
            log("could not save settings:", e)


# ----------------------------------------------------------------------------
# YouTube lookup (yt-dlp, no API keys)
# ----------------------------------------------------------------------------
@dataclass
class Track:
    title: str
    page: str
    stream: str
    headers: dict = field(default_factory=dict)
    duration: int = 0
    live: bool = False
    resolved_at: float = field(default_factory=time.time)


def ydl_opts():
    o = {"format": "bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True,
         "skip_download": True, "socket_timeout": 15}
    ck = HERE / "cookies.txt"
    if ck.exists():
        o["cookiefile"] = str(ck)
    return o


def resolve(query):
    q = query.strip()
    if not re.match(r"^https?://", q, re.I):
        q = "ytsearch1:" + q
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
        info = ydl.extract_info(q, download=False)
    if info and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else None
    if not info:
        raise ValueError("I couldn't find anything for that")
    url = info.get("url")
    if not url and info.get("requested_formats"):
        url = info["requested_formats"][0].get("url")
    if not url:
        raise ValueError("no playable audio found for that")
    return Track(title=info.get("title") or "Unknown song",
                 page=info.get("webpage_url") or (q if q.startswith("http") else ""),
                 stream=url, headers=info.get("http_headers") or {},
                 duration=int(info.get("duration") or 0), live=bool(info.get("is_live")))


def refresh(track):
    """Stream URLs expire, so re-resolve right before a queued song plays."""
    t = resolve(track.page or track.title)
    track.stream, track.headers, track.resolved_at = t.stream, t.headers, time.time()


def ffmpeg_cmd(track):
    cmd = [FFMPEG, "-loglevel", "error", "-nostdin"]
    if track.headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in track.headers.items())]
    if re.match(r"^https?://", track.stream, re.I):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    cmd += ["-i", track.stream, "-vn", "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"]
    return cmd


def update_ytdlp():
    """YouTube changes often; keep yt-dlp fresh (takes effect on next restart)."""
    if os.getenv("MUSY_AUTOUPDATE", "1") == "0":
        return
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-U", "--quiet",
                        "--disable-pip-version-check", "yt-dlp"],
                       timeout=180, capture_output=True)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
async def blocking(fn, *a):
    return await LOOP.run_in_executor(POOL, partial(fn, *a))


def fmt_time(t):
    t = int(t or 0)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def friendly_error(e):
    s = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    low = s.lower()
    if "not a bot" in low or "sign in" in low or "cookies" in low:
        return "YouTube is blocking this host right now (see README: cookies.txt)"
    if "private" in low or "unavailable" in low or "removed" in low:
        return "that video isn't available"
    return (s.splitlines() or ["unknown error"])[0][:160]


def get_args(msg, cmd):
    """Text after the command. Tries the SDK's likely fields, then falls back to the raw message."""
    pat = re.compile(r"^\s*[/!.?]?" + re.escape(cmd) + r"\b\s*", re.I)
    cands = []
    for attr in ("args", "arguments", "argument", "arg", "rest", "query"):
        v = getattr(msg, attr, None)
        if callable(v):
            continue
        if isinstance(v, (list, tuple)):
            v = " ".join(str(x) for x in v)
        cands.append(v)
    opts = getattr(msg, "options", None)
    if isinstance(opts, dict):
        cands.append(" ".join(str(x) for x in opts.values()))
    elif isinstance(opts, (list, tuple)):
        cands.append(" ".join(str(o.get("value", "")) if isinstance(o, dict) else str(o) for o in opts))
    cands.append(getattr(msg, "content", None))
    for v in cands:
        if isinstance(v, str) and v.strip():
            out = pat.sub("", v.strip(), count=1).strip()
            if out:
                return out
    return ""


_bot_id = [False, None]


def bot_id():
    """Musy's own user id, to tell whether she's alone in a channel. Optional: MUSY_BOT_ID overrides."""
    if _bot_id[0]:
        return _bot_id[1]
    _bot_id[0] = True
    if os.getenv("MUSY_BOT_ID"):
        _bot_id[1] = os.environ["MUSY_BOT_ID"]
        return _bot_id[1]
    for name in ("user_id", "id", "me", "bot_id", "self_id"):
        try:
            v = getattr(bot, name, None)
            v = v() if callable(v) else v
            if isinstance(v, dict):
                v = v.get("id")
            elif v is not None and not isinstance(v, (str, int)):
                v = getattr(v, "id", None)
            if isinstance(v, (str, int)) and v:
                _bot_id[1] = str(v)
                break
        except Exception:
            continue
    return _bot_id[1]


def reply_sync(msg, text):
    try:
        msg.reply(text)
    except Exception as e:
        log("reply failed:", e)


async def say(msg, text):
    await blocking(reply_sync, msg, text)


def submit(coro):
    fut = asyncio.run_coroutine_threadsafe(coro, LOOP)

    def done(f):
        try:
            f.result()
        except Exception as e:
            log("task error:", repr(e))
    fut.add_done_callback(done)


_voice_actions = []


async def voice_rate_limit():
    """Yapin' allows ~10 voice joins/leaves per minute per bot; stay under it."""
    while True:
        now = time.time()
        _voice_actions[:] = [t for t in _voice_actions if now - t < 60]
        if len(_voice_actions) < 8:
            _voice_actions.append(now)
            return
        await asyncio.sleep(2)


# ----------------------------------------------------------------------------
# Per-server player
# ----------------------------------------------------------------------------
class Player:
    def __init__(self, sid):
        self.sid = sid
        self.queue = []
        self.current = None
        self.call = None
        self.channel_id = None
        self.text_channel = None
        self.last_msg = None
        self.skip = False
        self.paused = False
        self.leave_now = False
        self.task = None
        self.wake = asyncio.Event()
        self.connect_lock = asyncio.Lock()
        self.idle_since = None
        self.alone_since = None
        self.last_presence = 0.0
        self.last_reconnect = 0.0

    @property
    def always_on(self):
        return bool(server_cfg(self.sid).get("always_on"))

    def remember(self, msg):
        self.last_msg = msg
        ch = getattr(msg, "channel_id", None)
        if ch and ch != self.text_channel:
            self.text_channel = ch
            server_cfg(self.sid)["text_channel"] = ch
            if self.always_on:
                save_settings()

    async def announce(self, text):
        def _go():
            try:
                if self.text_channel:
                    bot.send(self.text_channel, text)
                elif self.last_msg is not None:
                    self.last_msg.reply(text)
            except Exception as e:
                log("announce failed:", e)
        await blocking(_go)

    def ensure_task(self):
        if self.task is None or self.task.done():
            self.task = LOOP.create_task(self.run())

    # ---- voice connection -------------------------------------------------
    def connected(self):
        if not self.call:
            return False
        room = getattr(self.call, "room", None)
        chk = getattr(room, "isconnected", None)
        if callable(chk):
            try:
                return bool(chk())
            except Exception:
                return True
        return True

    async def connect(self, channel_id):
        async with self.connect_lock:
            if self.call and self.connected() and str(self.channel_id) == str(channel_id):
                return
            await self.drop_call()
            await voice_rate_limit()
            self.call = await bot.connect_voice(self.sid, channel_id)
            self.channel_id = channel_id
            self.alone_since = None
            cfg = server_cfg(self.sid)
            if cfg.get("always_on"):
                cfg["channel_id"] = channel_id
                save_settings()
            log(f"joined voice channel {channel_id} in server {self.sid}")

    async def drop_call(self):
        call, self.call = self.call, None
        if call:
            await voice_rate_limit()
            try:
                await asyncio.wait_for(call.leave(), 10)
            except Exception as e:
                log("leave error (ignored):", e)

    async def leave(self):
        self.queue.clear()
        self.current = None
        self.paused = False
        self.skip = False
        await self.drop_call()
        self.channel_id = None
        self.alone_since = None
        self.idle_since = None
        log(f"left voice in server {self.sid}")

    async def reconnect(self):
        ch = self.channel_id or server_cfg(self.sid).get("channel_id")
        if not ch:
            return False
        try:
            await self.connect(ch)
            return True
        except Exception as e:
            log("reconnect failed:", repr(e))
            return False

    # ---- presence (is anybody listening?) ---------------------------------
    async def check_alone(self):
        now = time.time()
        if now - self.last_presence < 30 or self.channel_id is None:
            return
        self.last_presence = now
        try:
            voice = await blocking(bot.voice, self.sid)
        except Exception:
            return
        ids = []
        for v in voice or []:
            if str(v.get("channel_id")) == str(self.channel_id):
                ids = list(v.get("user_ids") or [])
        me = bot_id()
        humans = [u for u in ids if me is None or str(u) != me]
        if humans:
            self.alone_since = None
        elif self.alone_since is None:
            self.alone_since = now

    def abandoned(self):
        return (not self.always_on and self.alone_since is not None
                and time.time() - self.alone_since >= IDLE_SECONDS)

    # ---- playback ---------------------------------------------------------
    async def play_track(self, track):
        self.current = track
        self.skip = False
        if not self.connected() and not await self.reconnect():
            await self.announce("I lost my voice connection and couldn't get back in 😢 Try /play again!")
            self.queue.clear()
            self.current = None
            return
        if time.time() - track.resolved_at > 240:
            try:
                await blocking(refresh, track)
            except Exception as e:
                await self.announce(f"Couldn't load **{track.title}** ({friendly_error(e)}). Skipping it 💔")
                self.current = None
                return
        length = "LIVE" if track.live else fmt_time(track.duration)
        await self.announce(f"{random.choice(NOW)} **{track.title}** [{length}]\n{track.page}".strip())

        call = self.call
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd(track), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
        frames = 0
        last_check = time.time()
        try:
            while not (self.skip or self.leave_now):
                while self.paused and not (self.skip or self.leave_now):
                    await asyncio.sleep(0.25)
                if self.skip or self.leave_now:
                    break
                try:
                    chunk = await asyncio.wait_for(proc.stdout.readexactly(FRAME_BYTES), 30)
                except asyncio.IncompleteReadError as e:
                    chunk = e.partial
                    if chunk:
                        chunk += bytes(FRAME_BYTES - len(chunk))
                        await call.source.capture_frame(rtc.AudioFrame(chunk, 48000, 2, FRAME_SAMPLES))
                        frames += 1
                    break
                except asyncio.TimeoutError:
                    log("stream stalled, moving on")
                    break
                await call.source.capture_frame(rtc.AudioFrame(chunk, 48000, 2, FRAME_SAMPLES))
                frames += 1
                if time.time() - last_check > 30:
                    last_check = time.time()
                    await self.check_alone()
                    if self.abandoned():
                        self.leave_now = True
            if self.skip or self.leave_now:
                clear = getattr(call.source, "clear_queue", None)
                if callable(clear):
                    try:
                        clear()
                    except Exception:
                        pass
            else:
                try:
                    await asyncio.wait_for(call.source.wait_for_playout(), 15)
                except Exception:
                    pass
        except Exception as e:
            log("playback error:", repr(e))
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            self.current = None
        if frames == 0 and not (self.skip or self.leave_now):
            try:
                err = (await asyncio.wait_for(proc.stderr.read(2000), 2)).decode(errors="ignore").strip()
                log("ffmpeg produced no audio:", err.splitlines()[-1] if err else "(no error output)")
            except Exception:
                pass
            await self.announce(f"I couldn't stream **{track.title}** 😿 YouTube may be blocking this host (see README).")

    async def run(self):
        while True:
            if self.leave_now:
                self.leave_now = False
                if not self.always_on:
                    await self.announce("Heading out! Call me back anytime with /play 🎶")
                    await self.leave()
                    return
            if self.queue:
                self.idle_since = None
                await self.play_track(self.queue.pop(0))
                continue

            now = time.time()
            if self.always_on:
                self.idle_since, self.alone_since = now, None
                if not self.connected() and now - self.last_reconnect > 30:
                    self.last_reconnect = now
                    await self.reconnect()
            else:
                if self.idle_since is None:
                    self.idle_since = now
                await self.check_alone()
                if now - self.idle_since >= IDLE_SECONDS or self.abandoned():
                    await self.announce("It's been quiet for 10 minutes, so I'm heading out. "
                                        "Use /play to bring me back, or /247 to keep me here! 🎶")
                    await self.leave()
                    return
            try:
                await asyncio.wait_for(self.wake.wait(), 5)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()


PLAYERS = {}


def get_player(sid):
    p = PLAYERS.get(str(sid))
    if p is None:
        p = PLAYERS[str(sid)] = Player(sid)
        cfg = SETTINGS.get(str(sid), {})
        p.text_channel = cfg.get("text_channel")
    return p


async def user_channel(msg):
    voice = await blocking(bot.voice, msg.server_id)
    uid = str(getattr(msg, "author_id", ""))
    for v in voice or []:
        if uid in [str(u) for u in (v.get("user_ids") or [])]:
            return v.get("channel_id")
    return None


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
def command(name, desc):
    def deco(fn):
        async def runner(msg, args):
            try:
                p = get_player(msg.server_id)
                p.remember(msg)
                await fn(msg, args, p)
            except Exception as e:
                log(f"/{name} failed:", repr(e))
                await say(msg, f"Oops, I tripped over my headphones: {friendly_error(e)} 🎧")

        def handler(msg):
            submit(runner(msg, get_args(msg, name)))

        handler.__name__ = f"cmd_{name}"
        bot.command(name, desc)(handler)
        return fn
    return deco


@command("play", "Play a YouTube link or search for a song by name")
async def cmd_play(msg, query, p):
    if not query:
        return await say(msg, "Tell me what to play! 🎵 `/play <YouTube link or song name>`")
    chan = await user_channel(msg)
    if chan is None:
        return await say(msg, "Hop into a voice channel first and I'll join you! 🎧")
    if p.call and p.connected() and str(p.channel_id) != str(chan) and (p.current or p.queue):
        return await say(msg, "I'm busy playing in another voice channel. Join me there, or wait till I'm done! 🎶")
    if len(p.queue) >= MAX_QUEUE:
        return await say(msg, f"My queue is full ({MAX_QUEUE} songs)! Let me play some first 😅")
    await say(msg, "🔎 Looking that up...")
    try:
        track = await blocking(resolve, query)
    except Exception as e:
        return await say(msg, f"Couldn't find that one: {friendly_error(e)} 😿")
    if track.duration and not track.live and track.duration > MAX_TRACK_SECONDS:
        return await say(msg, f"That one's too long (max {MAX_TRACK_SECONDS // 3600}h). Try something shorter!")
    try:
        await p.connect(chan)
    except Exception as e:
        return await say(msg, f"I couldn't join the voice channel: {friendly_error(e)}. "
                              "Does my role have Connect and Speak there? 🔇")
    waiting = len(p.queue) + (1 if p.current else 0)
    p.queue.append(track)
    p.ensure_task()
    p.wake.set()
    if waiting:
        await say(msg, f"{random.choice(QUEUED)} **{track.title}** is #{waiting + 1} in the queue.")


@command("skip", "Skip the current song")
async def cmd_skip(msg, _, p):
    if not p.current:
        return await say(msg, "Nothing's playing right now 🎧")
    p.skip, p.paused = True, False
    await say(msg, "⏭️ Skipped!")


@command("pause", "Pause the music")
async def cmd_pause(msg, _, p):
    if not p.current:
        return await say(msg, "Nothing's playing right now 🎧")
    p.paused = True
    await say(msg, "⏸️ Paused. Use /resume when you're ready!")


@command("resume", "Resume the music")
async def cmd_resume(msg, _, p):
    p.paused = False
    await say(msg, "▶️ Back to the music!")


@command("stop", "Stop the music and clear the queue")
async def cmd_stop(msg, _, p):
    p.queue.clear()
    p.skip, p.paused = True, False
    if not p.always_on:
        p.leave_now = True
    p.wake.set()
    await say(msg, "⏹️ Stopped and cleared the queue." +
              (" I'm staying here because 24/7 mode is on! 💜" if p.always_on else " See you next time! 👋"))


@command("queue", "Show what's playing and what's next")
async def cmd_queue(msg, _, p):
    if not p.current and not p.queue:
        return await say(msg, "The queue is empty. Feed me music with /play! 🎶")
    lines = []
    if p.current:
        lines.append(f"🎧 **Now:** {p.current.title}")
    for i, t in enumerate(p.queue[:10], 1):
        lines.append(f"{i}. {t.title} [{fmt_time(t.duration)}]")
    if len(p.queue) > 10:
        lines.append(f"...and {len(p.queue) - 10} more")
    await say(msg, "\n".join(lines))


@command("nowplaying", "Show the current song")
async def cmd_np(msg, _, p):
    if not p.current:
        return await say(msg, "Nothing's playing right now. Pick something! 🎵")
    await say(msg, f"🎧 **{p.current.title}**\n{p.current.page}".strip())


@command("247", "Toggle 24/7 mode: stay in voice forever (on) or leave after 10 min idle (off)")
async def cmd_247(msg, _, p):
    cfg = server_cfg(p.sid)
    if cfg.get("always_on"):
        cfg["always_on"] = False
        cfg.pop("channel_id", None)
        save_settings()
        p.idle_since = time.time()
        if p.call:
            p.ensure_task()
        return await say(msg, "24/7 mode is **off** 💤 I'll leave after 10 minutes of quiet in voice.")
    chan = p.channel_id if (p.call and p.connected()) else await user_channel(msg)
    if chan is None:
        return await say(msg, "Join a voice channel first, then run /247 again and I'll stay there! 🎧")
    cfg["always_on"], cfg["channel_id"] = True, chan
    save_settings()
    try:
        await p.connect(chan)
    except Exception as e:
        cfg["always_on"] = False
        save_settings()
        return await say(msg, f"I couldn't join the voice channel: {friendly_error(e)}. "
                              "Does my role have Connect and Speak there? 🔇")
    p.ensure_task()
    p.wake.set()
    await say(msg, "24/7 mode is **on** 🎶 I'll stay in this voice channel forever, even with nobody playing. "
                   "Run /247 again to turn it off.")


@command("musy", "Meet Musy and see what I can do")
async def cmd_help(msg, _, p):
    await say(msg, "Hi, I'm **Musy**! 🎵 I LOVE music, and I'm 100% free.\n"
                   "/play <link or song name> • /skip • /pause • /resume • /stop • /queue • /nowplaying\n"
                   "/247 — toggle 24/7 mode (stay in voice forever, or leave after 10 min idle)")


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
async def restore_all():
    """After a restart, rejoin the voice channels where 24/7 mode is on."""
    for cfg in list(SETTINGS.values()):
        if not (isinstance(cfg, dict) and cfg.get("always_on") and cfg.get("channel_id")):
            continue
        p = get_player(cfg.get("server_id"))
        for attempt in range(3):
            try:
                await p.connect(cfg["channel_id"])
                p.ensure_task()
                break
            except Exception as e:
                log("restore failed:", repr(e))
                await asyncio.sleep(10 * (attempt + 1))


if __name__ == "__main__":
    log("Musy is warming up... 🎶")
    threading.Thread(target=update_ytdlp, daemon=True).start()
    threading.Timer(8, lambda: submit(restore_all())).start()
    bot.run()
