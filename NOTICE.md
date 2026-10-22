# NOTICE — third-party code and ideas

Jarvis is a personal project. Some parts were **adapted from other open-source work**,
as listed here. Keep this file if you copy or redistribute Jarvis.

---

## Mark-LVI — https://github.com/FatihMakes/Mark-LVI

Author: **FatihMakes** · Licence: **Creative Commons Attribution-NonCommercial 4.0
International (CC BY-NC 4.0)** · Non-commercial use only.

The following ideas were read in Mark-LVI and **re-implemented for Jarvis's own threading
model**, not copied verbatim:

| Jarvis module | Adapted from | What was taken |
| --- | --- | --- |
| `jarvis/echo.py` | `core/echo.py` | Band-energy echo cancellation + self-calibrating echo gain, used to tell the user's voice from Jarvis's own playback. |
| `jarvis/undo.py` | `core/undo.py` | One shared undo stack: act immediately, remember how to reverse it, "undo" beats "are you sure?". |
| `jarvis/skills/screen.py` | `actions/computer_control.py`, `actions/game_updater.py` | Driving the Windows accessibility tree (pywinauto/UIA) to find windows and press named controls without a vision model. |
| `jarvis/skills/apps.py` | `actions/open_app.py` | Preferring an exact/short app name, and the "already installed vs web version" fallback. |
| `jarvis/ears.py`, `jarvis/asr.py` | `providers/stt_whisper.py`, `providers/registry.py` | Provider-chain design for hearing (local Whisper → Groq → Gemini) with per-provider fallback. |
| Design notes in `PLAN.md` | `core/confirm.py`, `core/undo.py`, `readme.md` | Confirmation tokens must be issued by the interface, never by the model; undo for anything reversible; confirm only what cannot be undone. |

**Commercial use:** Mark-LVI is CC BY-NC. Jarvis as it stands is a personal, non-commercial
assistant, which the licence permits. If you ever sell, license or commercially distribute
Jarvis, you must remove or independently rewrite the adapted parts above.

---

## Other components

| Component | Licence / terms |
| --- | --- |
| openWakeWord ("hey jarvis" model) | Apache-2.0 |
| Google Gemini API | Google API terms (free tier) |
| DeepSeek API | DeepSeek terms |
| Groq API (optional) | Groq terms |
| Fish Audio (voice clone) | Fish Audio terms — your own voice model |
| edge-tts | GPL-3.0 (used only as an optional voice engine) |
| Microsoft SAPI5 voices | Part of Windows |
| python-docx / python-pptx / openpyxl (Office phases) | MIT / BSD |
| Mark-LVI | CC BY-NC 4.0 — see above |
