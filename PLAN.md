# JARVIS — a voice-operated desktop assistant

**Project home:** `C:\Users\Brian\jarvis` · **Owner:** Brian ("Teran") · **Status:** Phases 1–2 complete, 3 in progress (tools, memory, vision, safety, files, reminders)
**Target machine:** this laptop (Windows, i5-5300U, 8 GB RAM, no GPU)

> **Build log — round 5 (23 Oct 2026). Grok brain + Grok vision.**
>
> The Gemini endpoint had become unusable from this laptop: every request died with
> `SSL: UNEXPECTED_EOF_WHILE_READING`, and the retry ladder turned one turn into 105 s.
> Jarvis now runs on **Grok (`grok-4.7`) through an OpenAI-compatible gateway**:
>
> - New `Grok` client (`GROK_API_KEY`, `GROK_BASE_URL`, `GROK_MODEL`); chain is
>   `grok:grok-4.7` first, Gemini last resort. Tool calling, the follow-up tool round and
>   streaming all verified against the live gateway (4–5 s per leg).
> - Fixed a real bug found while wiring it: `chat()` ignored the model supplied by the
>   chain, so `provider:model` silently used the client default.
> - **Vision moved off Gemini too** — the gateway accepts OpenAI image messages, so
>   screenshots go through `JARVIS_VISION_CHAIN` (Grok first). Verified by describing the
>   live desktop and by reading a synthetic test image.
> - Hearing stays local: Whisper now loads on demand instead of falling through to a
>   30–60 s cloud retry when the model has not finished warming.
> - Diagnostics tell the truth: `--check` reports the actual brain/ears/vision providers,
>   and checks vision with a synthetic image so it never uploads the screen.
>
> Measured: brain 4.9–7.2 s per turn, vision 4.5 s (small) / 13 s (full screen),
> local hearing ~2 s, 29 tests green, `--check` all green.

> **Build log — round 2 (21 Oct 2026, evening).**
>
> **Hearing fixed for good.** Local Whisper (`tiny.en`) transcribes offline in ~2 s. Three
> stacked bugs were found and fixed by measurement, not guesswork:
> 1. the energy VAD's noise floor rose above quiet speech, so the recorder declared
>    "no speech" and Jarvis apologised without ever transcribing (log: `peak 0.014–0.038`);
> 2. `normalise()` converted int16 → float32 *before* the dtype check, handing Whisper
>    samples outside [-1, 1] — so it returned empty every time;
> 3. empty local results fell through to rate-limited Gemini, adding 30–60 s of retry
>    before the same apology.
>
> Verified against simulated mic levels from the logs (voice RMS 0.02–0.03 + room noise):
> "open notepad", "what time is it", "set the volume to 20" all transcribe correctly.
>
> **New capabilities this round, each verified live:** 30-tool toolbelt with model-chosen
> calls (`list_windows` verified: 65 ms after replacing UIA enumeration with Win32);
> persistent SQLite memory ("remember" → recalled after restart); spoken confirmation gate
> for destructive actions (request → yes → executes); file reading for txt/md/docx/pdf/
> xlsx/pptx (read the real resume); reminders that fire and speak; vision via screenshot
> (correctly described the screen); single-instance mutex; autostart shortcut; transcript
> sanitising (prompt echoes, doubled phrases, Whisper noise hallucinations).
>
> Still open: Piper neural voice, proactivity, messaging, long-run soak test.

> **Build log — round 4 (22 Oct 2026, morning). Audit + fixes.**
>
> Overnight logs were reviewed before changing anything. Six real defects found:
>
> 1. **SQLite connections were never closed** (`with connect()` commits but does not close),
>    leaking handles and locking the database. Found by the new test suite: 23 tests took
>    271 s. Fixed with `jarvis/sqlite_safe.py`; the same suite now runs in **0.9 s**.
> 2. **File search walked the whole home directory** on every find/delete (~10 s). Now a
>    budgeted walk over Desktop/Documents/Downloads/Pictures with heavy dirs pruned.
> 3. **Always-ready listener ran away** — 40 transcriptions in 10 minutes, some 11–21 s.
>    Added a busy flag, a 20 s cooldown after speech not addressed to Jarvis, a 2.5 s
>    snippet, and greedy decoding for the name check.
> 4. **The orb's menu was quitting Jarvis** 13 s after start (`shutting down (requested by:
>    orb menu)`). Quit now lives only in the tray; the orb offers Hide, the tray Show orb.
> 5. **Hearing accuracy** — `tiny.en` misheard ordinary speech ("Hey, jobbies"). `base.en`
>    is downloaded and is now the transcription model (same ~2.4 s), `tiny.en` stays for
>    fast name checks.
> 6. **No calendar**, which the user asked for twice. New local calendar (`calendar_store`)
>    + natural-language date parser (`nlp_time`) + routing, briefing and tools.
>
> Also added `tests/test_core.py` (23 tests, 0.9 s) so "no errors" is measurable.

> **Build log — round 3 (21 Oct 2026, night).**
>
> Focus: **reliable all-day operation + proactivity**, and every claim tested by breaking
> something on purpose.
>
> - **Supervised start** (`run_jarvis.bat` loop): verified by killing the running process at
>   21:36:15 — it restarted within seconds as pid 6516, microphone alive, verdict healthy.
> - **Audio watchdog**: detects a stalled or lost microphone stream within ~10 s and reopens
>   it (headset unplugged, device sleep).
> - **Health heartbeat** `data/health.json` + `--status` CLI: pid, uptime, state, mic health,
>   verdict. (This is also the soak-test instrument for the next round.)
> - **Proactive briefing**: time, pending reminders, one remembered fact — on start and on
>   "brief me". Verified text and routing.
> - **Voice**: Piper's install stalls behind a ~60 KB/s link even with `--no-deps`; deferred.
>   Adaptive auto now = piper if present, else edge-tts under a 4.5 s budget, else SAPI —
>   with a start-up probe so the greeting is never delayed by a slow neural voice.
> - **Wake tuning**: 0.60 / 5 frames; Whisper noise hallucinations ("thank you", "bye")
>   discarded so a false wake cannot become a phantom command.
> **Build log (21 Oct 2026).** Phase 0 and Phase 1 are done and verified live: wake word
> scored 0.99, STT works, DeepSeek brain answers in 3.6 s, offline voice in 0.15–1.3 s,
> orb, tray, hotkeys, follow-up mode and barge-in all running. Phase 2's offline router is
> in: "open Chrome", "volume up", "lock the screen", "what time is it" need no network.
>
> Deviations forced by reality:
> - **Brain:** DeepSeek is primary (fast, reliable here); the Gemini free tier hit its
>   20-request/day cap on the primary model, so Gemini is backup + ears.
> - **Voice:** default is the **offline Windows voice** because edge-tts and Fish Audio
>   cost 14–40 s on this ~50 KB/s connection. `JARVIS_TTS=edge` or `fish` switches back
>   when the network is healthy; the Fish clone is verified working.
> - **Ears:** Gemini flash with Opus-compressed uploads; adding `GROQ_API_KEY` gives ~1 s
>   Whisper instead of 8–13 s.
> - **Art:** Gemini image generation is unavailable on the free tier (429), so the HUD and
>   orb are generated procedurally by `tools/make_concepts.py`.

---

## 1. The promise

You say **"Hey Jarvis"** and it wakes up. You talk, it talks back — a British-male voice,
in real conversation, about anything. And unlike a chatbot, it has **hands**: it opens
apps, reads your screen, types and clicks, finds files, changes system settings, and
sends messages. It remembers who you are between sessions.

Three non-negotiables for v1:

1. **Hands-free.** Wake word activation, no keyboard required.
2. **Real conversation.** Not a command menu — streaming, interruptible, remembers context.
3. **Safe.** It never deletes, sends, or shuts anything down without asking you out loud first.

---

## 2. What the machine probe found

These facts drive every decision below, so they are recorded here.

| Fact | Value | Consequence |
| --- | --- | --- |
| CPU | Intel i5-5300U, 2 cores / 4 threads (2015) | Local LLMs are out — a 3B model would give ~3 tok/s |
| RAM | 7.9 GB | One heavy process at a time; keep the footprint small |
| GPU | none (no CUDA) | Vision and reasoning must be cloud-side |
| Disk C: | 13 GB free, **NTFS** | Project lives here |
| Disk D: | 20 GB free, **exFAT** | exFAT has no hard links, so tooling that writes atomically fails there. Media/downloads only — not the project. |
| Mic | Realtek Microphone (OK) + Bluetooth headset mic | Device-selectable, with auto-fallback |
| Speakers | Realtek Speakers (OK), Philips TAT2139 | Fine for neural TTS |
| TTS voices already on PC | Microsoft Hazel (en-GB), Zira (en-US) | Free offline fallback voice, zero install |
| Python | 3.12.10 (recommended) and 3.14.8 | Use **3.12** — 3.14 lacks wheels for the audio/ML stack |
| ffmpeg | 9.0.2 installed | Decode TTS audio, convert formats, no extra dependency |
| Network | Gemini, Groq, OpenAI, Anthropic, ElevenLabs all reachable | Cloud brain is viable |
| API keys in environment | none | You need to create one free Gemini key (Phase 0) |
| Apps found | Chrome, VS Code, VLC, Start Menu index (91 shortcuts), WindowsApps | `Get-StartApps` gives us launchable AppIDs for everything |

---

## 2b. Verified live on this machine (21 Oct 2026)

Not assumptions — every line below is a real API call made from this laptop.

| Check | Result |
| --- | --- |
| Fish Audio voice lookup | `title=Jarvis`, `langs=en` — your cloned voice id is valid |
| Fish Audio TTS | HTTP 200, 93 KB mp3 generated at `s2.1-pro-free` |
| Fish Audio playback | Played through the Realtek speakers, ffplay exit 0 |
| Gemini key | Valid. The first key pasted was rejected (401 `ACCESS_TOKEN_TYPE_UNSUPPORTED`); the second authenticates |
| Gemini models | 63 visible, including `gemini-3.8-flash`, `gemini-3.5-transcribe`, `gemini-2.5-computer-use-preview-10-2025`, `gemini-3.8-flash-tts`, image models |
| Gemini chat | `gemini-3.8-flash` replied: *"I am online and fully operational, sir."* (15 in / 9 out tokens) |
| Gemini hearing | Transcribed the Fish clip exactly: *"Good evening sir. I am Jarvis. All systems are now online and at your service."* |
| Gemini tools | Returned `launch_app({"app":"chrome"})` when asked to open Chrome |
| Gemini image gen | **429 quota exceeded** — image models are not on the free tier. Backgrounds are generated procedurally instead (`tools/make_concepts.py`), which is free and animatable |
| Deprecated model discovered | `gemini-2.0-flash` is retired; API directs callers to `gemini-3.8-flash` |

**Consequence:** brain, ears, hands (tool calls) and voice are all proven. Phase 1 needs no unknowns.

---


One Python process, an async pipeline, a strict separation between *hearing*, *thinking*
and *acting*. Every box below is a swappable module.

```
   always-on, local, ~1% CPU
   +-------------------------------------------------------------+
   |  MIC (16 kHz mono)  ->  Wake word "Hey Jarvis"  ->  VAD      |
   |                         (openWakeWord, offline)   endpoint   |
   +---------------------------------+---------------------------+
                                     |  also: Ctrl+Space push-to-talk
                                     v
   +-------------------------------------------------------------+
   |  STT   Gemini audio-in  (fallback: faster-whisper tiny.en)   |
   +---------------------------------+---------------------------+
                                     v
   +-------------------------------------------------------------+
   |  FAST ROUTER (regex, no LLM, ~200 ms)                        |
   |    "open chrome", "volume 30", "lock the screen", "mute"     |
   |    -> matches? execute immediately, skip the brain           |
   +---------------------------------+---------------------------+
                                     | no match -> full brain
                                     v
   +-------------------------------------------------------------+
   |  BRAIN   Gemini flash + function calling + persona           |
   |          memory: SQLite (facts, prefs, aliases, summaries)   |
   +---------------------------------+---------------------------+
                                     v
   +-------------------------------------------------------------+
   |  SAFETY GATE    safe -> run        risky -> spoken confirm   |
   +-------------------------------------------------------------+
                                     v
   +-------------------------------------------------------------+
   |  SKILLS  apps · desktop(type/click) · vision · files ·       |
   |          system · messaging                                  |
   +---------------------------------+---------------------------+
                                     v
   +-------------------------------------------------------------+
   |  TTS   Fish Audio (YOUR cloned Jarvis voice) -> ffmpeg ->    |
   |        speakers, sentence-by-sentence                        |
   |        fallbacks: edge-tts en-GB-Ryan, SAPI Hazel offline    |
   |        interruptible: talk over it to make it stop           |
   +-------------------------------------------------------------+
```

### The voice loop, with a latency budget

| Stage | Budget | How |
| --- | --- | --- |
| Wake detection | 0.2 s | openWakeWord on 80 ms frames |
| End-of-speech | 0.7 s | Silero VAD, tunable |
| Speech → text | 0.6–1.2 s | Gemini audio input, single request |
| Thought → first token | 0.4–0.8 s | streaming, flash model |
| First audio out | 0.4 s | Fish Audio returns a complete clip per sentence; play chunk 1 while chunk 2 synthesises |
| **Total felt** | **~2.5–3.5 s** | simple commands via the fast router: **~1 s** |

Follow-up mode: after Jarvis answers it keeps the mic open for ~8 s, so you can reply
without saying "Hey Jarvis" again. Silence closes the conversation.

---

## 4. Stack decisions and why

| Layer | Choice | Why this one |
| --- | --- | --- |
| Wake word | `openwakeword` + `onnxruntime` | Has a pretrained **"hey jarvis"** model. Runs offline, ONNX on CPU, ~1% of one core. No account, no access key (unlike Porcupine). |
| VAD | Silero VAD (ONNX) | Ships as a file, no C compiler needed (webrtcvad would need a build). Reliable end-of-speech. |
| Audio capture | `sounddevice` | PortAudio is bundled in the wheel — nothing to install system-wide. |
| STT | **Gemini audio input** (primary), `faster-whisper` tiny.en int8 (offline fallback) | On 2 cores, local whisper tiny costs 2–3 s for a 4 s clip. Cloud is faster *and* far more accurate, and reuses the key we already need. |
| Brain | **Gemini flash-class**, configurable model name | Free tier; native function calling; multimodal so the same model does vision. Model ID lives in config so a rename never breaks us. |
| TTS | **Fish Audio** `s2.1-pro-free`, your cloned "Jarvis" voice (`FISH_VOICE_ID`), → ffmpeg → speakers. Fallbacks: edge-tts en-GB-Ryan, then SAPI Hazel | You already have the voice. It is the actual Jarvis clone, and it is a *paid-tier* service so it stays available when free tiers throttle. Two fallbacks mean the voice never dies. |
| Desktop control | `pyautogui`, `pygetwindow`, `pyperclip`, `keyboard`, `mss` | Mature, pure-Python, no admin rights needed. |
| App launching | `Get-StartApps` index + Start Menu `.lnk` scan + PATH, cached | Catches both Win32 apps and Store apps (WhatsApp), plus fuzzy aliases you teach it. |
| Tray UI | `pystray` + `pillow` | Shows state (idle / listening / thinking / speaking), quick mute, quit. |
| Memory | SQLite + FTS5 | Zero server, searchable "what did I tell you about X", survives restarts. |

Explicitly **rejected**: local LLMs (too slow here), PyTorch (2.5 GB, unaffordable disk),
Porcupine (needs an access key), Electron re-write (needless weight for a tray app).

---

## 5. The skill catalog — what Jarvis can actually do

Ordered by build priority, and by risk tier. **Tier A** runs on its own. **Tier B** always
asks for a spoken "yes" first. Nothing runs arbitrary shell commands.

| # | Skill | Example you'd say | Tier |
| --- | --- | --- | --- |
| 1 | Launch app / website | "open Chrome", "open VS Code", "go to youtube.com" | A |
| 2 | System control | "volume 30", "brighter", "lock the screen", "mute", "wi-fi off" | A (shutdown = B) |
| 3 | Type & keys | "type: meeting moved to 3", "press ctrl+s", "copy that" | A |
| 4 | Speak & remember | "remember I hate cilantro", "what do you know about me?" | A |
| 5 | Read the screen | "what does this error say?", "summarize this page" | A |
| 6 | Click by description | "click the Send button", "click the third result" | B (shows what it found first) |
| 7 | Files | "find my CV", "open the newest PDF in Downloads", "summarize this docx" | read = A, move/rename = B, delete = B |
| 8 | Messaging | "message Mum on WhatsApp: I'm running late" | B (reads the draft back before sending) |
| 9 | Shutdown / sleep / close apps | "shut down", "close everything" | B |

Vision works in two steps, which is what makes it reliable: **look** (screenshot → resize →
overlay a labelled grid → ask the model which cell the target is in) then **act**, then
**look again to verify**. Raw pixel-guessing fails; a labelled grid does not.

---

## 6. Safety model

- **Tier B gate:** Jarvis speaks the exact action ("Delete 4 files in Downloads. Confirm?"),
  listens for yes/no, and **times out to "no"** after 6 s.
- **Allowlist, not shell.** Skills call typed Python functions with validated arguments.
  There is no "run this command" tool.
- **No credentials.** Jarvis never types into password fields and refuses financial sites.
- **Panic stop:** `Ctrl+Alt+Esc` or saying "stop" instantly kills speech and any pending action.
- **Privacy default:** audio is never written to disk. The wake-word model is local, so
  nothing leaves the machine until you are actually talking to it. Logs record transcripts
  only, with an opt-out.

---

## 7. Build phases

Each phase ends in something you can actually use and a test that either passes or doesn't.
Roughly 5–6 working sessions total.

### Phase 0 — Foundations (15 min)
- Create the free **Gemini API key** at `aistudio.google.com/apikey` → paste into `.env`.
- Python 3.12 venv, install the stack, `git init`, `.gitignore` for `.env`.
- Smoke test: list mics/speakers, record 3 s, play it back, confirm key works with one API call.
- **Done when:** a recorded clip plays back and Gemini answers a text prompt.

### Phase 1 — The Voice (the heart)
- Wake word "Hey Jarvis" + `Ctrl+Space` push-to-talk, live state in the tray icon.
- Record → STT → Gemini → edge-tts, streaming, with barge-in and 8 s follow-up mode.
- British-butler persona prompt, latency instrumentation on every turn.
- **Done when:** "Hey Jarvis, tell me something interesting about Nairobi" gets a spoken
  answer in under 4 s, and talking over it stops it mid-sentence.

### Phase 2 — The Hands
- Fast router (regex) for the ~20 most common commands — no LLM round trip.
- Skills: launch app/site (fuzzy + aliases), system control, type/keys, clipboard.
- Tier-B confirmation gate wired in, panic stop.
- **Done when:** "open Chrome" is in front within 1.5 s; "volume 25" works; "type hello"
  types into the focused window; "shut down" refuses without a yes.

### Phase 3 — The Eyes
- `mss` screenshots, labelled-grid vision, click-by-description with pre-action confirmation.
- "What's this error?" and verify-after-action loop.
- **Done when:** with an error dialog on screen, Jarvis explains it; "click the OK button" clicks it.

### Phase 4 — Memory & Personality
- SQLite facts/preferences/aliases; rolling summary of old turns; FTS search.
- "Remember that…" / "what do you know about me?" / nickname resolution
  ("my CV folder" → the real path).
- **Done when:** a fact told today is recalled after a restart, and personality feels like Jarvis.

### Phase 5 — Files
- Search/read/rename/move/delete (with gates), and document summarising by reusing the
  existing Office tooling for `.docx` / `.pdf` / `.xlsx`.
- **Done when:** "find my latest CV and summarize it" works on the real files in Downloads.

### Phase 6 — Messaging
- WhatsApp Desktop / Telegram: launch, search contact, draft, **read the draft back**,
  send only on confirmation.
- **Done when:** a test message goes to a note-to-self chat with draft read-back.

### Phase 7 — Hardening & daily use
- Autostart with Windows, single-instance lock, log rotation, reconnect/backoff on API
  rate limits, mic hot-swap (Bluetooth headset), `README` + `run_jarvis.bat`.
- **Done when:** it survives an all-day run and starts cleanly after a reboot.

---

## 8. Cost

Gemini free tier is $0 within its rate limits, which comfortably cover one person talking
(~3–8 requests a minute at peak). edge-tts is free. openWakeWord/whisper/Silero are local
and free. Total expected spend: **$0/month**, with a rate-limit guard that degrades to a
canned clarification instead of erroring. If you ever want more headroom, Groq or Ollama
Cloud can be dropped in behind the same interface.

---

## 9. Risks, and what we do about them

| Risk | Mitigation |
| --- | --- |
| Free-tier rate limits during a burst | Queue + backoff; fast router answers common commands locally |
| Model names change under us | One `MODEL=` config value; on startup, verify against the key's model list and auto-pick the best flash model |
| edge-tts breaks (it has before) | Automatic fallback to offline SAPI Hazel; voice never goes silent |
| False wake words | Two-frame confirmation + noise floor; wake word is "Hey Jarvis", not "Jarvis" alone |
| Bluetooth headset grabs the mic | Device selection in config + auto-detect default change and recover |
| `pyautogui` can't touch elevated windows | Run non-elevated, and say so when a target window is admin-level |
| LLM tries something destructive | Tier-B gate is enforced in Python, *below* the model — the model cannot skip it |
| C: only 13 GB free | Light stack only (~600 MB); no PyTorch, no local LLM weights |

---

## 10. What I need from you to start

1. **The Gemini API key** (free, ~2 minutes): <https://aistudio.google.com/apikey> → "Create API key" → paste it to me, or drop it in `C:\Users\Brian\jarvis\.env` as `GEMINI_API_KEY=...`. It is never logged or committed.
2. Confirmation that **Phase 0 + Phase 1** is the first thing I build (it is the heart; everything else hangs off it).

---

## 11. File layout we're building toward

```
C:\Users\Brian\jarvis\
  .env                     # GEMINI_API_KEY (gitignored)
  .env.example
  requirements.txt
  run_jarvis.bat           # double-click to start (silent, via pythonw)
  PLAN.md                  # this file
  README.md                # how to use it
  jarvis/
    main.py                # entry point, single-instance lock, tray
    config.py              # settings, model IDs, device selection
    audio/ capture.py vad.py wake.py hotkey.py
    stt/   gemini_stt.py whisper_local.py
    tts/   edge_engine.py sapi_engine.py player.py
    brain/ llm.py tools.py memory.py persona.py router.py
    skills/ apps.py desktop.py vision.py files.py system.py messaging.py
    safety/ confirm.py policy.py
    ui/    tray.py
  data/  jarvis.db  logs/
  tests/ test_router.py test_policy.py test_tools.py ...
```
