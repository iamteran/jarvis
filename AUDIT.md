# JARVIS Project Audit and Implementation Plan

## 1. What Was Found in the Original Project

### Architecture
- **Language**: Python 3.12+
- **Architecture**: Single-process voice assistant with a state machine (BOOTING, IDLE, LISTENING, TRANSCRIBING, THINKING, SPEAKING, FOLLOWUP, MUTED, ERROR)
- **Entry Points**: `run_jarvis.bat` (supervised startup with restart on crash), `python -m jarvis` (with CLI diagnostics)
- **Threading Model**: 
  - Audio thread: ONLY consumer of microphone blocks (wake word, VAD, barge-in, follow-up)
  - Turn threads: One per utterance (transcribe, think, speak) - never touch microphone
  - UI threads: Tkinter (orb) and pystray (tray icon)

### Existing Features (Working)

#### Voice Pipeline
- **Wake Word**: openWakeWord (ONNX, offline) - "Hey Jarvis" with configurable threshold
- **VAD**: Energy-based endpointing with configurable silence detection
- **STT Chain**: Local Whisper (faster-whisper) → Groq → Gemini (with quota rollover)
- **TTS Chain**: Fish Audio (cloned voice) → edge-tts → SAPI (offline fallback)
- **Barge-in**: Wake word detection during speech stops TTS
- **Follow-up**: 8-second window after answer for continued conversation

#### Fast Router (Offline, No LLM)
- App launching (fuzzy matching with accent repair)
- System control (volume, mute, lock, time, date, media)
- Window management (switch, list, read screen via UIA)
- Click/type in focused window
- Calendar (local SQLite, natural language dates)
- Reminders (spoken at due time)
- Memory (persistent SQLite facts/aliases)
- File operations (find, read, delete with confirmation)
- Email (Gmail IMAP, draft-only replies)
- Vision (screenshot → Grok/Gemini description)

#### LLM Brain with Tool Calling
- Provider chain: Grok → Groq → DeepSeek → Gemini → Ollama (local)
- Circuit breaker per model (cooldown after 2 failures)
- Tool calling with up to 4 rounds
- Memory injection into every prompt
- Streaming text fallback when tools unavailable

#### Telegram Bridge
- Long-poll with exponential backoff
- Authorized user ID only
- Command execution through same brain/tools path
- File sending capability

#### Video Downloader
- yt-dlp for Instagram, TikTok, X (Twitter), YouTube
- URL validation, platform detection
- Progress tracking, retry with backoff
- SQLite download history
- Watermark-free preference for TikTok
- MP4 conversion via FFmpeg

#### Instagram Stories Publisher
- Graph API for Business/Creator accounts
- Video validation (60s, 1080x1920, <100MB)
- Transcoding with FFmpeg
- Manual fallback for Personal accounts
- Explicit confirmation required

#### WhatsApp Status Publisher
- Desktop automation via pywinauto (best effort)
- Video validation (30s, 1080x1920, <16MB)
- Manual fallback with prepared file
- Explicit confirmation required

#### Reliability Features
- Single instance mutex
- Crash supervisor (run_jarvis.bat restarts within 5s)
- Audio watchdog (detects stalled mic in ~10s)
- Health heartbeat (data/health.json, --status CLI)
- Log rotation (3 files max)

#### Configuration
- Centralized in `config.py` via `.env` file
- All tunables as environment variables with defaults
- Model chains configurable

#### Tests
- 29 tests covering core functionality
- Private temp databases (real data never touched)
- Run in under 1 second

---

## 2. Existing Bugs/Issues Identified

### From PLAN.md Build Logs (Already Fixed)
1. ✅ SQLite connections never closed - Fixed with `sqlite_safe.py`
2. ✅ File search walked whole home directory - Now budgeted walk
3. ✅ Always-ready listener ran away - Busy flag, cooldown, snippet limit added
4. ✅ Orb menu quitting Jarvis - Quit moved to tray only
5. ✅ Hearing accuracy - `base.en` model for transcription, `tiny.en` for fast checks
6. ✅ No calendar - Added local calendar with nlp_time parser

### Remaining Issues to Address (from Master Prompt)

#### Voice Recognition
- [ ] Voice recognition cutting off sentences or commands
- [ ] Incorrect transcription and commands interpreted too early
- [ ] Barge-in and interruption problems (currently only wake word, not general speech)
- [ ] Duplicate voice triggers and accidental wake-word activation

#### Spoken Responses
- [ ] Spoken responses getting interrupted, duplicated, or stuck
- [ ] Need better cancellation when user interrupts

#### Task Management
- [ ] Telegram commands arriving while another task is running (no queue)
- [ ] No persistent/recoverable task queue
- [ ] No task IDs, status, progress, timestamps
- [ ] No cancellation/pause/resume
- [ ] No duplicate-task detection

#### AI Provider
- [ ] AI-provider quota errors, timeouts, invalid responses - fallback chain exists but could be improved
- [ ] Need local deterministic functions as first priority (before any LLM)

#### Resource Management
- [ ] Crashes, hanging tasks, orphaned processes, unreleased resources
- [ ] No bounded retries with backoff for all operations
- [ ] Large local model (Whisper) kept loaded permanently - should unload after idle

#### Command Routing
- [ ] Incorrect command routing or assistant claiming success without evidence

---

## 3. Implementation Plan (Per Master Prompt Phases)

### Phase 1 — Audit and Stabilization (Current)
**Goal**: Fix voice handling, task management, provider fallbacks, resource leaks

**Tasks**:
1. ✅ Audit complete - documented above
2. Create Git backup/checkpoint
3. Improve voice session state management
4. Add configurable wake-word and STT thresholds (already configurable via .env)
5. Improve silence detection and end-of-utterance handling
6. Add cancellation of ongoing speech on interruption (barge-in enhancement)
7. Add timeouts, bounded retries, backoff for all transient failures
8. Improve provider abstraction with configurable fallback chain (already exists, enhance)
9. Add lightweight deterministic command handler for simple commands (router exists, enhance)
10. Structured logs with rotating log files (already exists)
11. **NEW**: Implement persistent task queue with SQLite
12. **NEW**: Add task IDs, status, progress, timestamps, error details
13. **NEW**: Implement safe cancellation and pause/resume
14. **NEW**: Configurable concurrency limits
15. **NEW**: Recovery of interrupted jobs after restart
16. **NEW**: Duplicate-task detection
17. **NEW**: Progress notifications through Telegram and voice

### Phase 2 — Video Downloading (Already Largely Implemented)
**Goal**: Enhance and test Instagram, TikTok, X URL handling

**Tasks**:
1. Validate existing implementation works
2. Add better error reporting for unsupported URLs
3. Improve download validation
4. Test optional conversion
5. Safe file naming and overwrite prevention
6. Clear error reporting

### Phase 3 — Instagram and WhatsApp Publishing (Already Implemented)
**Goal**: Test and improve publishing workflows

**Tasks**:
1. Test Instagram Graph API eligibility check
2. Test WhatsApp Desktop detection and automation
3. Ensure explicit confirmation before publishing
4. Reliable manual fallbacks
5. Keep TikTok publishing excluded

### Phase 4 — Reusable Skills and Self-Healing
**Goal**: Add skill creation, sandboxed testing, permissions, health checks

**Tasks**:
1. Skill system with name, description, triggers, actions, permissions
2. Skill generation from task description
3. Review/validation workflow
4. Sandbox testing
5. Permission system (Low/Medium/High risk)
6. Health checks for all components
7. Watchdog with bounded restart attempts
8. Circuit breakers for failing services
9. Diagnostic command "Jarvis, run a health check"
10. Concise diagnostic report

### Phase 5 — Visual Understanding, Knowledge Vault, Coding Agent
**Goal**: Integrate modules without loading every model continuously

**Tasks**:
1. Visual desktop understanding (screenshot → analyze → plan → act → verify)
2. Local knowledge vault (SQLite + FTS, optional embeddings)
3. Coding agent workflow (inspect, edit, test, commit, push)
4. On-demand model loading/unloading

### Phase 6 — Full Regression Testing
**Goal**: Test old and new features, document results

**Tasks**:
1. Run all existing tests
2. Test new features
3. Document actual results
4. Remove unused dependencies and dead code

---

## 4. Files to Modify/Create

### New Files
- `jarvis/task_queue.py` - Persistent task queue with SQLite
- `jarvis/skills/` directory - Skill system
- `jarvis/health.py` - Health monitoring and self-healing
- `jarvis/knowledge_vault.py` - Local document indexing
- `jarvis/coding_agent.py` - Coding/GitHub workflow
- `jarvis/permission_system.py` - Three-level permission system

### Modified Files
- `jarvis/app.py` - Integrate task queue, health monitoring
- `jarvis/main.py` - Add health check CLI command
- `jarvis/config.py` - Add new configuration options
- `jarvis/tools.py` - Add new tools for task queue, skills, health
- `jarvis/router.py` - Add new voice commands for task management
- `jarvis/telegram_bridge.py` - Add task queue commands
- `jarvis/video_downloader.py` - Enhance error handling
- `jarvis/instagram_publisher.py` - Enhance validation
- `jarvis/whatsapp_publisher.py` - Enhance UI verification

---

## 5. Windows Commands for Install, Start, Test, Stop

### Install
```bat
cd C:\Users\Brian\jarvis
install_jarvis.bat
```

### Start
```bat
cd C:\Users\Brian\jarvis
run_jarvis.bat
```

### Test (Diagnostics)
```bat
cd C:\Users\Brian\jarvis
.venv\Scripts\python.exe -m jarvis --check
.venv\Scripts\python.exe -m jarvis --status
.venv\Scripts\python.exe -m jarvis --devices
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

### Stop
- Right-click tray icon → Quit
- Or `Ctrl+Alt+Esc` (panic stop)
- Or `python -m jarvis --say "shut down"` (with confirmation)

---

## 6. Priority Implementation Order

1. **Task Queue System** - Foundation for all async operations
2. **Health Monitoring** - Self-healing and diagnostics
3. **Skill System** - Extensibility
4. **Permission System** - Safety
5. **Knowledge Vault** - Local document intelligence
6. **Coding Agent** - Development workflow
7. **Enhanced Visual Understanding** - Desktop reasoning
8. **Regression Testing** - Verify everything works

---

## 7. Configuration Additions Needed

Add to `.env.example`:
```
# Task Queue
JARVIS_TASK_QUEUE_MAX_CONCURRENT=2
JARVIS_TASK_QUEUE_RETRY_ATTEMPTS=3
JARVIS_TASK_QUEUE_RETRY_BACKOFF=2.0

# Health Monitoring
JARVIS_HEALTH_CHECK_INTERVAL=30
JARVIS_WATCHDOG_MAX_RESTARTS=3
JARVIS_WATCHDOG_RESTART_WINDOW=300

# Skills
JARVIS_SKILLS_DIR=data/skills
JARVIS_SKILL_SANDBOX=true

# Knowledge Vault
JARVIS_KNOWLEDGE_PATHS=Documents,Downloads,Desktop
JARVIS_KNOWLEDGE_EXCLUDE=.git,__pycache__,node_modules,.venv
JARVIS_KNOWLEDGE_EMBEDDINGS=false

# Permissions
JARVIS_PERMISSION_MODE=interactive  # interactive | auto-approve-low | dry-run
```