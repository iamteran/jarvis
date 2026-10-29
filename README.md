# JARVIS

A voice assistant that lives on this laptop. Say **"Hey Jarvis"**, talk normally, and it
answers out loud — and it can act on the machine: open apps and sites, change volume, lock
the screen, tell the time.

**Status: Phases 1-3 complete - voice loop, desktop control, 70+ tool model-driven toolbelt,
persistent memory, Grok vision, files, reminders, Gmail drafts, live local dashboard,
Telegram remote control, skills system, video downloads, social publishing, knowledge vault,
coding agent.**

### What Jarvis can do right now

- **Hear you offline.** Local Whisper (`faster-whisper tiny.en`) transcribes on this CPU in
  ~2 s with no network and no quota. Verified against quiet-mic/noisy-room levels.
- **Act on its own judgement.** 70+ tools (apps, sites, windows, clicks, typing, keys,
  volume, lock, files, vision, reminders, memory, undo, skills, video downloads, social
  publishing, knowledge search, code exploration) and the model chooses which to call.
- **See.** `look_at_screen` captures the screen and answers about it (verified: described
  the live desktop correctly). Accessibility reading stays for exact, instant text.
- **Remember.** Facts in SQLite, injected into every conversation, surviving restarts.
- **Ask before destroying.** Delete/close/shutdown go through a spoken yes/no gate; the
  token comes from the interface, never from the model.
- **Behave like a desktop app.** One instance (named mutex), autostart shortcut, tray icon,
  floating orb, hotkeys, reminders that speak when due.

---

### 🤖 Telegram Remote Control

Control Jarvis from anywhere via Telegram. The bot runs as a long-poll bridge:

| Command | What it does |
|---|---|
| `/start` | Shows your authorized user ID and confirms the bot is running |
| `/id` | Returns your Telegram user ID (for `.env` configuration) |
| Any other message | Forwards to Jarvis as a voice command; replies come back in chat |

**Setup:** Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_ID` to `.env`. Only the authorized user ID can send commands.

**Examples from your phone:**
- "open chrome and search for python tutorials"
- "what's on my screen right now"
- "remind me to call the bank in 30 minutes"
- "run health check"
- "download video https://instagram.com/reel/..."

---

### 🎬 Video Downloader (Instagram, TikTok, X, YouTube)

Download videos from social media links — watermark-free when possible.

| Tool | Description |
|---|---|
| `video_info` | Get metadata (title, duration, thumbnail, available formats) without downloading |
| `download_video` | Download a video; auto-picks best quality or specify format; watermark-free for TikTok/Reels |
| `list_downloads` | Show recent downloads with file paths |

**Usage via Telegram:** Send any Instagram Reel, TikTok, YouTube Short, or X/Twitter video link — Jarvis downloads it and sends the file back to you in chat.

---

### 📱 Social Publishing (Instagram Stories & WhatsApp Status)

Post directly from local files to Instagram Stories and WhatsApp Status with format validation.

| Tool | Description |
|---|---|
| `instagram_connect` | Link an Instagram account (stores session securely) |
| `instagram_publish_story` | Post an image/video to Stories; validates 9:16 aspect, ≤60s video, ≤30MB |
| `instagram_list_accounts` | Show connected Instagram accounts |
| `whatsapp_publish_status` | Post image/video/text to WhatsApp Status |
| `whatsapp_check` | Verify WhatsApp Web session is active |

**Usage via Telegram:** "post this image to my Instagram story" (attach the image in Telegram chat).

---

### 🧠 Knowledge Vault (Local Full-Text Search)

Search across your Documents, Downloads, and Desktop folders instantly.

| Tool | Description |
|---|---|
| `index_knowledge` | Build/update the search index (PDF, DOCX, TXT, MD, XLSX, PPTX) |
| `search_knowledge` | Full-text search with snippet previews |
| `knowledge_stats` | Index size, file count, last updated |

---

### 💻 Coding Agent (Git Repository Exploration)

Explore and modify codebases from Telegram or voice.

| Tool | Description |
|---|---|
| `list_repo_files` | List files in a repo (respects `.gitignore`) |
| `read_repo_file` | Read any file's contents |
| `search_repo_code` | Ripgrep-style code search |
| `get_repo_info` | Branch, remotes, last commit, dirty files |
| `write_repo_file` | Create new files |
| `edit_repo_file` | Edit existing files (string replace) |
| `apply_patch` | Apply unified diff patches |
| `run_repo_tests` | Run test suite (pytest, npm test, etc.) |
| `run_repo_command` | Run arbitrary commands in repo directory |

---

### 🛠 Skills System (Sandboxed Extensions)

Create custom capabilities with a permissioned sandbox:

| Tool | Description |
|---|---|
| `create_skill` | Write a new Python skill with declared permissions |
| `test_skill` | Run a skill in the sandbox with a test payload |
| `list_skills` | Show all skills with approval status |
| `approve_skill` | Grant a skill permanent approval |
| `reject_skill` | Revoke approval |
| `get_skill` | View a skill's source and permissions |

Skills declare exactly what they can access (files, network, shell, etc.) — nothing runs without approval.

---

## Start it

```bat
run_jarvis.bat
```

First-time setup (already done on this machine): `install_jarvis.bat`, then put your keys
in `.env` (see `.env.example`).

### Talking avatar (Windows)

Jarvis runs beside [AVATAR](https://github.com/ARPAHLS/avatar), a separate transparent
VRM desktop overlay. Install the official [AVATAR 0.8.0 Windows release](https://github.com/ARPAHLS/avatar/releases/tag/v0.8.0).
Double-click `run_jarvis.bat` in the project folder: it starts AVATAR if installed and not
already running, then starts Jarvis with the old orb hidden.

Jarvis retains its microphone, wake word, speech recognition, brain, tools, and tray/hotkey
controls; AVATAR is its visible face, not a second assistant. In AVATAR, open **Gear →
Voice** and select **Device output (auto)**. Under **Gear → Appearance → Environments**,
select **None** so the desktop shows through. AVATAR drives the VRM mouth from local
Windows output audio levels; this is amplitude-based movement, not phoneme-perfect lip
sync. The green live dot should brighten while Jarvis speaks. Load a licensed `.vrm`
model under **Settings → Directories → Avatars**; a still `.jpg` cannot provide a rigged
mouth. Bundled sample models have their own asset terms, separate from AVATAR's MIT
software license.

Jarvis uses offline SAPI by default so replies do not depend on the Fish Audio service.
To opt into Fish Audio, set `JARVIS_TTS=fish`, `FISH_API_KEY`, and
`FISH_VOICE_ID=98655a12fa944e26b274c535e5e03842` in `.env`; text is sent remotely.
If the key or voice ID is missing, or synthesis fails/times out, Jarvis logs the issue and
uses offline SAPI for speech. `run_jarvis.bat`
uses `JARVIS_SHOW_ORB=0` from `.env` to keep the orb hidden; set it to `1` there to restore
the orb.

Manual verification:

1. Double-click `run_jarvis.bat`; confirm there is one AVATAR window and no Jarvis orb.
2. Confirm the desktop shows through around the model. If not, set **Gear → Appearance →
   Environments → None**.
3. Confirm **Gear → Voice → Device output (auto)** is selected and the live dot responds
   during speech.
4. Say **“Hey Jarvis”**, give a command, and listen for the answer. Check
   `data\logs\jarvis.log` for `Fish synthesis succeeded` to confirm Fish actually spoke;
   a Fish failure/timeout warning or a missing-key message means offline SAPI was used.
5. Run `.venv\Scripts\python.exe -m jarvis --status` to verify Jarvis and microphone health.

---

## Talk to it

| You do | Jarvis does |
| --- | --- |
| Say **"Hey Jarvis"** | beeps and starts listening |
| Talk | records until you pause (~0.75 s of silence) |
| "open chrome" / "open notepad" / "launch whatsapp" | opens it — **instantly, no network** |
| **"open chrome and search youtube"** | does both actions, in order |
| **"open YouTube, search for cats"** / "open YouTube, play a song" | recognizes common misheard search/play command words and routes the query to YouTube |
| **"open crhome"** / "open youtub" | guesses the nearest installed app or known site and says what it assumed |
| "go to youtube" / "open github.com" / "search for flights" | opens the browser |
| "volume up" / "turn it down" / "mute" | changes system volume |
| "set the volume to 30" | exact level (Windows Core Audio) |
| **"undo that"** | reverses the last change (e.g. volume back to where it was) |
| "lock the screen" | locks Windows |
| "what time is it" / "what's the date" | answers instantly |
| "pause the music" | media play/pause |
| **"switch to chrome"** / "bring up file explorer" | focuses that window |
| **"what's on screen"** / "read the window" | reads the visible window's text and buttons (accessibility tree, offline) |
| **"click save"** / "click the Send button" | presses a named control in the focused window |
| **"type: hello world"** | types into the focused window |
| **"look at my screen" / "what does this error say"** | vision: screenshot → model describes what it sees |
| **"remember that ..."** / "what do you remember" | persistent memory across restarts |
| **"remind me to X in 10 minutes"** / "set a timer for 5 minutes" | spoken reminder at the right time |
| **"read report.docx"** / "summarise my CV" | reads txt/md/docx/pdf/xlsx/pptx locally |
| **"delete my cv"** | asks *"Shall I move … to the Recycle Bin?"* — only yes runs it |
| "what windows are open" | lists them |
| Anything else | full conversation (Grok → Gemini) |
| Talk over it, or `Ctrl+Space` | interrupts it (barge-in) |
| Stay quiet after an answer | 8 s follow-up window, then it sleeps |

Speech autocorrection is limited to command phrases (for example, a misheard “search”
immediately after “open YouTube”). It leaves the search or song title unchanged rather
than guessing what you meant.

### Keys

| Shortcut | Action |
| --- | --- |
| `Ctrl+Space` | listen now |
| `Ctrl+Alt+J` | mute / unmute the mic |
| `Ctrl+Alt+Esc` | panic stop |
| Right-click tray icon | menu: listen, mute, quit |

---

## Measured on this laptop

These are real numbers from this machine, not estimates.

| Part | Time | Notes |
| --- | --- | --- |
| Wake word | ~0.1 s | openWakeWord, offline, score 0.99 on a test clip |
| Fast command (open app, time, volume) | **instant** | local router + offline voice |
| Offline voice (SAPI) | **0.15–1.3 s** | in-process COM, no network |
| **Hearing (local Whisper)** | **~2.0 s** | 5.8 s clip → 2.0 s, offline, unlimited |
| Window listing | **65 ms** | Win32 EnumWindows (was 18 s via UIA) |
| Brain with tools (Grok `grok-4.7`) | 4–8 s | one extra round trip when it calls a tool |
| Vision (Grok screenshot) | ~4 s small image, ~13 s full screen | one capture, one question |
| Ears (Gemini flash, fallback only) | 6–13 s | each model has its own 20/day quota |
| Fish Audio (your cloned voice) | ~14 s | working key, slow on a ~50 KB/s link |
| edge-tts (British neural) | 1.4–2 s healthy, 25–40 s congested | |

**The bottleneck is the internet connection (~50 KB/s), not the code.** Conversation
quality is therefore gated by the cloud round trip.

### Make it fast: local Whisper or a Groq key

Two independent upgrades, either one solves hearing:

1. **Local Whisper** (`pip install faster-whisper`) — offline, unlimited, ~1–3 s per
   utterance on this CPU. Jarvis picks it up automatically (`JARVIS_WHISPER=auto`) and
   downloads the model on first use. No key, no quota, works with no internet.
2. **Groq free tier** — hearing in ~1 s *and* faster replies. Sign up at
   <https://console.groq.com> → API Keys, then add to `.env`:
   ```
   GROQ_API_KEY=gsk_...
   ```
   Jarvis already prefers it; no code changes.

The Gemini path has one quirk worth knowing: **each model has its own 20 requests/day
free quota**, so `JARVIS_STT_CHAIN` lists four models and a `429` rolls to the next.
That is roughly 80 utterances/day before cloud hearing runs dry — which is why local
Whisper is the better default here.

---

## Configuration (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | — | primary brain |
| `GROQ_API_KEY` | — | optional: fastest brain + ears |
| `GROK_API_KEY` | — | Grok brain via an OpenAI-compatible gateway (primary here) |
| `GROK_BASE_URL` | `https://api.openlux.ai/v1` | gateway base URL for Grok |
| `GROK_MODEL` | `grok-4.7` | Grok model name |
| `GEMINI_API_KEY` | — | ears + backup brain |
| `JARVIS_LLM_CHAIN` | `grok:grok-4.7,gemini:gemini-3.5-flash-lite,gemini:gemini-3.5-flash` | provider:model order; missing keys are skipped |
| `JARVIS_VISION_CHAIN` | `grok:grok-4.7,gemini:gemini-3-flash-preview` | screenshot provider:model order |
| `JARVIS_STT_MODEL` | `gemini-3.5-flash` | Gemini speech-to-text (fallback `gemini-3.6-flash`) |
| `JARVIS_TTS` | `sapi` | `sapi` offline · `fish` your clone (SAPI fallback) · `edge` British neural · `auto` edge→sapi |
| `JARVIS_EDGE_VOICE` | `en-GB-RyanNeural` | edge-tts voice |
| `FISH_VOICE_ID` | — | Fish Audio voice id (used only when `JARVIS_TTS=fish`) |
| `JARVIS_FISH_BUDGET_MS` | `30000` | maximum time to wait for Fish before using SAPI |
| `JARVIS_MIC` / `JARVIS_SPEAKER` | system default | index or name from `--devices` |
| `JARVIS_WAKE_THRESHOLD` | `0.55` | lower = more sensitive |
| `JARVIS_END_SILENCE_MS` | `750` | pause length that ends your sentence |
| `JARVIS_SHOW_ORB` | `0` | set to `1` to restore the orb while keeping tray controls and hotkeys |
| `JARVIS_SPEECH_RMS` | `0.018` | raise if it reacts to room noise |

---

## Diagnostics

```powershell
python -m jarvis --devices        # mics and speakers
python -m jarvis --check          # keys, brain, ears, voice, wake word, mic
python -m jarvis --stt file.wav   # test the exact hearing chain
python -m jarvis --wake-test file.wav
python -m jarvis --say "open chrome"   # typed command through the same path
python -m jarvis --no-orb
python -m jarvis --debug
```

Logs: `data\logs\jarvis.log` (rotating). Each turn logs `stt`, `llm` and first-audio timing.

---

## What we took from Mark-LVI

[Jarvis](https://github.com/FatihMakes/Mark-LVI) is a large, thoughtful computer-control
assistant (CC BY-NC 4.0). Rather than copy it, Jarvis adapted the ideas that fit:

- **Echo cancellation** (`jarvis/echo.py`) — band-energy subtraction plus a self-calibrating
  echo gain, so Jarvis can tell your voice from its own playback. This is what stops it
  interrupting itself or waking itself up.
- **Undo** (`jarvis/undo.py`) — act immediately and remember how to reverse it; "undo" is
  faster than a confirmation prompt, so confirmations are reserved for the irreversible.
- **Accessibility-tree control** (`jarvis/skills/screen.py`) — Windows already names every
  button; "click Save" is an exact lookup that works offline, no vision model needed.
- **Hearing provider chain** — local Whisper → Groq → Gemini, each with fallback.
- **Confirmation design** — the confirmation token must come from the interface, never from
  the model. See `NOTICE.md` for the full attribution and the non-commercial licence note.

## Calendar

A local calendar (SQLite) — no OAuth, works offline:

| You say | Jarvis does |
|---|---|
| "what's up for today" / "my schedule" | reads today's appointments |
| "what's coming up" / "my next appointment" | reads the next ones |
| "set an appointment on 12 November at 7:30" | records it, reads the date back |
| "remind me to call mum at 6 pm" | timed reminder at that clock time |
| "open my calendar" | opens Google Calendar in the browser |

Events are announced ~90 seconds before they start, and today's schedule is part of the
start-up briefing.

## Command centre (dashboard)

A live local HUD at **http://127.0.0.1:8765/** (bound to loopback only — email never
leaves this machine on the LAN):

- **Inbox** — today's Gmail, unread markers, click to read, reply box that saves a draft.
- **Today** — local calendar events and pending reminders, merged by time.
- **System Pulse** — battery/charging, CPU, RAM, disk, uptime, open windows and recent
  files from Downloads/Desktop/Documents.
- **Command box** — typed commands go through the same fast router and Grok brain as voice.
- The orb shows live state and the server starts with Jarvis; `--dashboard` opens it (or
  reuses the running instance's server).

## Gmail (App Password, draft-only)

Sign in from the dashboard's Inbox panel, or add to `.env`:

```env
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

2-Step Verification must be on; create the App Password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). Jarvis reads
over IMAP and **never sends**: "reply saying ..." appends a message to Gmail's Drafts folder
with `\Draft`, where you press Send yourself.

| Say | What happens |
|---|---|
| "do I have any gmails today" | reads today's count, senders and subjects |
| "read the email from mum" | reads the full text aloud |
| "reply saying I'll be there at seven" | saves that reply as a Gmail draft |

## Tests

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

29 tests covering date parsing, calendar, routing, confirmation safety, memory, reminders,
transcript cleaning and the Whisper normaliser. They run in under a second and use a
private temporary database — your real memory is never touched.

## Reliability (all-day operation)

- **Single instance** — a named Windows mutex; a second launch refuses instead of fighting
  over the microphone.
- **Crash supervisor** — `run_jarvis.bat` restarts Jarvis within 5 s if it dies, and exits
  cleanly when you quit from the tray. Verified by killing the process: it came back with a
  new pid and a healthy mic.
- **Audio watchdog** — if the microphone stream stalls or disappears (headset unplugged,
  device sleep), it is detected within ~10 s and reopened.
- **Health heartbeat** — `data/health.json`, written every 10 s; `python -m jarvis --status`
  prints pid, uptime, state, mic health and a verdict.
- **Proactive briefing** — on start it says the time, your pending reminders, and something
  it remembers. Ask "brief me" any time.
- **Log rotation** — `data/logs/jarvis.log`, 3 files max.

## How it works

```
mic 16 kHz ─► "hey jarvis" (offline ONNX) ─► energy VAD endpointing
   ─► EARS: local Whisper (~2 s, offline) ─► Gemini flash (fallback)
   ─► FAST ROUTER (regex, local, 0 ms) ── matched? ──► execute + offline voice
   ─► BRAIN: DeepSeek with tool-calling ──► picks a tool (apps/windows/click/type/files)
        │                         └─ tool result fed back ─┐
        └───────────────── answer ────────────────────────┘
   ─► MEMORY: SQLite facts + aliases injected into every prompt
   ─► voice: SAPI (offline) / edge-tts / Fish clone, sentence pipeline, interruptible
```

Design notes that matter:

- **One audio thread owns the microphone**, so wake word, endpointing, barge-in and
  follow-up can never race.
- **Turns are fenced by a turn id**, so an interrupted answer can't overwrite a newer state.
- **Provider chains everywhere** (brain, ears, voice), so one dead API never kills Jarvis.
- **Fast commands never touch the network**, which is why "open Chrome" works even when
  the connection is down.
- **One instance only** (named Windows mutex) — two Jarvis instances would fight over the mic.
- **Tools are gated, not guessed**: delete, close-app, shutdown and destructive shell
  commands ask out loud first, and the yes/no token comes from the interface — never from
  the model. Everything else (open, type, click, read, remember) runs directly.

## Project layout

| Path | Purpose |
| --- | --- |
| `jarvis/app.py` | state machine: boot, listening, thinking, speaking, follow-up |
| `jarvis/router.py` | offline fast-path commands |
| `jarvis/skills/apps.py` | app index (Start Menu + Store AppIDs) and launching |
| `jarvis/skills/system.py` | volume, lock, clock, media keys |
| `jarvis/audio/` | capture, VAD, wake word |
| `jarvis/ai.py` | Gemini / DeepSeek / Groq clients |
| `jarvis/ears.py` | speech-to-text preference chain |
| `jarvis/brain/` | persona + history + provider chain |
| `jarvis/tts/` | SAPI, edge-tts, Fish, sentence pipeline, interruptible player |
| `jarvis/ui/` | floating orb, tray icon |
| `tools/` | art generator, latency probes |
| `PLAN.md` | architecture, phases, safety model |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "I didn't get that" | Speech-to-text failed. Run `--stt` on a recording; if it is slow/empty, Gemini quota or connection. Add `GROQ_API_KEY`. |
| Wake word never fires | `--wake-test`; lower `JARVIS_WAKE_THRESHOLD` to 0.4. |
| Fires on its own | raise threshold to 0.7 or `JARVIS_SPEECH_RMS`. |
| Cuts you off | raise `JARVIS_END_SILENCE_MS` to 1100. |
| Voice sounds robotic | that's the offline SAPI voice; set `JARVIS_TTS=auto` or `edge` when the connection is good, `fish` for your clone. |
| Orb invisible | delete `data\ui.json` (it may be off-screen). |
| Hotkeys dead | another app owns Ctrl+Space; change `JARVIS_HOTKEY_LISTEN`. |

## Privacy

The wake word runs locally — no audio leaves the laptop until it fires. Recordings are not
written to disk; only transcripts are logged. Delete `data\logs\` any time.
