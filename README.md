# Musy 🎵

A free music bot for Yapin' (yapin.gg). Plays YouTube links or song names in voice channels. No paid APIs, no premium tier.

| Command | What it does |
|---|---|
| `/play <link or song name>` | Joins your voice channel and plays it (or queues it) |
| `/skip` `/pause` `/resume` | Playback controls |
| `/stop` | Clears the queue (and leaves, unless 24/7 is on) |
| `/queue` `/nowplaying` | See what's playing and what's next |
| `/247` | Toggle 24/7 mode |
| `/musy` | Help |

**24/7 mode:** ON = Musy stays in the voice channel forever (even with nothing playing, and it rejoins after a restart). OFF = she leaves after 10 minutes of quiet (nothing playing, or nobody else in the channel).

## Setup

### 1. Make the bot on Yapin'
1. In Yapin': **Settings → Developer** → create a bot and copy its token (starts with `yb_`).
2. Add the bot to your server.
3. Give the bot's role **Connect** and **Speak** on your voice channels. Without Speak it joins muted.

### 2. Host it free on bot-hosting.net
1. Log in at bot-hosting.net with Discord, create your free bot, and create a **Python** server. (Free servers die every 4 days, and are free to renew manually.)
2. **Files** tab → upload `main.py` and `requirements.txt`.
3. Token: either upload a `.env` file (copy `.env.example`, fill in your token, remove `.example` from the end of the name) or add `YAPIN_BOT_TOKEN` as an environment variable if your panel has that.
4. **Startup** tab → main file = `main.py`, requirements file = `requirements.txt` (or paste the package names into *Additional Python packages*).
5. Start the server and watch the console for `Musy is warming up...`.

### 3. Try it
Join a voice channel, then type `/play lofi hip-hop` or paste a YouTube link.

## Good to know

- **No screen share / video.** Yapin's developer docs say bots send one audio track only and can't share video, so Musy is audio only (sigh).
- **YouTube risk.** The open-source `yapin-voice-bot` README quotes Yapin's voice docs saying streaming from YouTube breaks their terms and bots playing audio they lack rights to can be turned off. YouTube's own terms also forbid it, and hosts can remove bots for it. Use at your own risk, ideally on a small private server (like bot-hosting.net or your own machine).
- **YouTube may block the host.** Free hosts use datacenter IPs, which YouTube sometimes blocks ("Sign in to confirm you're not a bot"). Fix: export your YouTube cookies as `cookies.txt` (Netscape format, e.g. with the "Get cookies.txt LOCALLY" browser extension) and put it next to `main.py`. Use a throwaway Google account, since YouTube can flag accounts used this way.
- **yt-dlp breaks sometimes.** Musy updates it on every start. If search suddenly fails, restart the server.
- **Limits.** One voice channel per server, 10 voice joins/leaves per minute (Musy stays under it), songs up to 4 hours (`MUSY_MAX_MINUTES`), 50 songs in the queue.
- **Saved data.** 24/7 settings live in `musy_data.json` next to `main.py`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` on start | Requirements didn't install. Check the Startup tab points to `requirements.txt`. |
| Joins but nobody hears it | Give the bot's role **Speak**. |
| "I couldn't join the voice channel" | Give the bot's role **Connect** and make sure it can see the channel. |
| Console says `ffmpeg produced no audio` | Usually YouTube blocking. Add `cookies.txt` (see above). |
| `/play` says "Tell me what to play" even when you typed something | The SDK names command arguments differently than expected. Edit `get_args()` in `main.py`. |
| Musy doesn't leave an empty channel | She can't tell which user is her. Set `MUSY_BOT_ID` in `.env` to the bot's user id. (She still leaves after 10 min of silence.) |
