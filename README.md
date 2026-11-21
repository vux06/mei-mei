# Mei Mei — personal AI desktop assistant

Always-on, hands-free voice assistant for **Linux / Hyprland / Wayland**. Say **“Hey Mei Mei”**, speak a request,
and she answers out loud and can actually operate your desktop. Claude (via the **Claude Agent SDK**) is the brain;
speech recognition, wake-word detection and text-to-speech all run **locally and offline**.

```
 mic ─► Silero VAD ─► wake check (Whisper on GPU) ─► "Hey Mei Mei" ─► capture until you stop talking
                                                                          │
                      Piper TTS ◄── Claude Agent SDK (ONE persistent session) ◄── Whisper (GPU) text
                         │                  │
                  speakers (interruptible)  └─► mei tools: apps · windows (Hyprland) · files · system · screen ·
                                                mouse/keyboard · clipboard · terminal · memory   (permission-gated)
```

## Quick start
```bash
./install.sh            # idempotent: checks tools, builds .venv (Python 3.12), downloads models, verifies Claude+mic+audio
./run.sh                # launch (GUI + always-on listening)
./run.sh --hidden       # background only; ./run.sh again (or scripts/meimei_ctl.py show) brings the window up
./clear-context.sh      # clear the running assistant's conversation from a script
scripts/autostart.sh enable      # start with your desktop session (systemd --user); `disable` removes it
```
Closing the window hides it to the tray and **keeps listening** (if your bar has no tray, closing quits).
Pause/resume/disable listening from the GUI, the tray, voice (“pause listening”) or
`scripts/meimei_ctl.py listen pause|resume|disable|enable`.

## What you get
| Area | Implementation |
|---|---|
| **Brain** | `claude-agent-sdk`, one `ClaudeSDKClient` process for the whole conversation (no process per sentence). Model/effort in `config/settings.json`. |
| **Session** | One active session id persisted in `~/.local/share/meimei/session.json`, resumed after restart (`resume=`). Conversation log in SQLite. Context-% shown in the GUI (`get_context_usage`), warning at 70 %. |
| **Clearing context** | The SDK **cannot empty a session in place**, so clearing = archive the old id, mint a new UUID, start a fresh client with `session_id=<new>` (never `resume`). Personality + permanent memory are re-applied by the system prompt. Voice (“clear your context”), the **CLEAR CONTEXT** button, and `./clear-context.sh` (Unix socket, mode 0600, peer-uid checked; it reads the active id, clears *that* id only, and verifies the switch). |
| **Personality** | `config/personality.md` — edit it (or in Settings ▸ Personality); applied on every launch and every reset. |
| **STT** | `faster-whisper` `small.en`, float16 on the RTX 2050 (CUDA-12 libs from pip wheels; system CUDA 13 is not usable by CTranslate2), automatic CPU/int8 fallback. ≈0.17 s per utterance. Silero VAD decides when you're done (800 ms silence, configurable). No audio is stored. |
| **Wake word** | “Hey Mei Mei” (optionally bare “Mei Mei”), always on, local — see *How the wake word works*. |
| **TTS** | Piper (`en_GB-jenny_dioco-medium`), sentence-pipelined, queue-managed, never overlaps, `stop()` cuts audio in ~1 ms, speed/volume/voice in settings. Code/URLs/markdown are not read out; long answers are trimmed (“the rest is on screen”). |
| **Desktop control** | ~40 tools (list below). Hyprland via IPC (Lua dispatchers — Hyprland ≥ 0.55), `grim`, `ydotool`, `wl-clipboard`, `wpctl`, `playerctl`. |
| **Local fast path** | open/close app, volume, mute, time, stop, pause, clear-context are handled locally (no Claude round-trip). Claude is told what was handled (`[Handled locally: …]`) so “open the first result” still has context. |
| **Memory** | SQLite (`~/.local/share/meimei/meimei.db`): explicit facts, preferences, folders (permanent) — *separate* from conversation history. Only saved when you ask (“remember that …”) or in Settings ▸ Memory. Clearing context never touches it. |
| **GUI** | PySide6: animated orb (IDLE / LISTENING / PROCESSING / EXECUTING / SPEAKING / ERROR, driven by real mic/speaker levels), mic + wake-word + Claude status, session id, context %, transcript, text input, hold-to-talk, pause, stop-speaking, CLEAR CONTEXT, Settings, activity log, inline confirmation bar. |

### Tools Claude can use (`mcp__mei__*`)
open_app, close_app, switch_to_app, is_app_running, open_url, web_search_in_browser · list_windows, active_window,
window_action (focus/minimize/restore/maximize/fullscreen/float/tile/close), move_resize_window, switch_workspace ·
list_directory, search_files, read_text_file, file_info, make_folder, move_path, write_text_file, delete_path (→ trash),
organize_directory · system_status, disk_usage, battery_status, network_status, list_processes, listening_ports, volume,
media_control, brightness · run_command · screenshot (image goes to Claude), mouse_click, type_text, press_keys, scroll,
clipboard · remember, recall, forget. Built-in `WebSearch`/`WebFetch` are enabled; Bash/Write/Edit are **not** exposed.

## Security model
* No root. `sudo` commands need confirmation and run with `sudo -n` (they fail rather than prompt for a password).
* **AUTO**: open apps, window control, read-only system info, screenshots, volume, navigation, read-only shell commands.
* **CONFIRM** (button in the GUI **or say “yes”/“no”** — silence for 60 s = no): deleting (to trash), overwriting,
  moving over an existing file, moving > 10 files, privileged commands, any command not on the read-only list, typing /
  pressing Enter in a terminal window.
* **DENY** (even if you agree): `rm -rf /`·`~`, `mkfs`, `dd of=/dev/…`, fork bombs, wiping disks, reading/writing SSH keys,
  GPG, cloud creds, browser logins, password stores.
* **Prompt injection**: tool results (web pages, files, screenshots, terminal output, clipboard) mark the turn
  *tainted*; while tainted, even AUTO actions that change state (clicks, typing, writes, commands) ask for confirmation.
  The personality also instructs Claude to treat such content as data. This reduces the risk; it is not a guarantee.
* Everything is shown in the Activity log (also `~/.local/share/meimei/activity.log`).
* Privacy: raw audio never leaves the machine and is never written to disk. Only the transcribed text of a request
  made after the wake phrase goes to Claude.

## How the wake word works (and its honest limits)
There is **no pre-trained “Hey Mei Mei” keyword model** (openWakeWord ships only a handful of fixed phrases), and I did
not substitute another phrase. Instead:
1. Silero VAD runs continuously on the mic stream (cheap).
2. Each speech segment's first ~3.5 s are transcribed by Whisper (the same GPU model as STT, primed with the phrase).
3. The opening words are fuzzy-matched with a phonetic normaliser (“hey may may”, “hey mei-mei”, “a mei mei” ✔; “hey mom”,
   “hey siri” ✘). `wake_word.sensitivity` (0–1) moves the threshold.

Trade-offs vs a dedicated keyword spotter: it wakes the GPU for every speech segment in earshot (≈0.2 s each), adds
~0.3 s latency, and can mis-hear in heavy noise. Measured on synthetic speech (3 voices × 2 speeds × 3 noise levels incl.
8 dB SNR): **30/30 detected, 0/36 false triggers**; the tiny.en-on-CPU variant scored 28/30 with 1–3 false triggers, which is
why the GPU model is shared. Real human speech and your room may differ — tune sensitivity in Settings.

### Training a dedicated wake-word model (optional, not done here)
For lower power/latency you can train an openWakeWord model for “hey mei mei”: use the
[openWakeWord training notebook](https://github.com/dscripka/openWakeWord) (synthetic data from Piper/TTS voices +
negative datasets such as ACAV100M features; a few GPU-hours), export `hey_mei_mei.onnx`, `pip install openwakeword`,
and replace the VAD→Whisper stage in `voice/pipeline.py` with a frame-wise `Model.predict()` threshold. This repo does
not ship such a model, so `wake_word` here is the Whisper-phrase method above.

## Echo, self-triggering and interrupting her
* While she speaks, wake detection stays armed (that's what makes “Hey Mei Mei, stop” work) with a stricter bar: the *full*
  phrase is required, matches are checked on a sliding 3 s window, and anything that is mostly her own words (text overlap
  with what she is saying) is rejected as echo. Disable barge-in (`voice.barge_in=false`) and she is deaf while talking.
* Optional **PipeWire WebRTC echo cancellation**: Settings ▸ Voice ▸ *Echo cancellation* (`audio.echo_cancel`). It loads a
  `module-echo-cancel` pair only while Mei Mei runs and never changes your default devices. It is **off by default** because
  I could only validate it with virtual devices, where it also suppressed the near-end speech in one topology; try it with
  your real speakers and turn it off if recognition gets worse. Headphones avoid the problem entirely.
* Stop button / `scripts/meimei_ctl.py stop` always works; bind it to a key in your Hyprland config if you like, e.g.
  `hl.bind("SUPER + Escape", hl.dsp.exec_cmd("/path/to/meimei/scripts/meimei_ctl.py stop"))` (not added automatically).
* Mic unplug / default-device change / stalled stream are detected within ~2 s and re-opened automatically; a missing
  preferred mic falls back to the system default and switches back when it returns. The GUI shows the real status.

## Configuration
`config/settings.json` (Settings dialog edits it). Highlights: `audio.input_source` (PipeWire source name, null = default),
`wake_word.sensitivity`, `wake_word.allow_bare_name`, `vad.end_silence_ms`, `voice.followup_window_s` (listen again after a
reply without the wake word; 0 = off), `stt.model` (`small.en` default; `medium.en`/`distil-large-v3` fit 4 GB too),
`tts.voice|speed|volume`, `claude.model|effort`, `permissions.terminal_extra_allow_prefixes`.
Environment overrides: `MEIMEI_SETTINGS`, `MEIMEI_DATA_DIR`, `MEIMEI_MODELS_DIR`, `MEIMEI_SOCKET`.
Models/data live outside the repo in `~/.local/share/meimei/`.

## Tests (all in `tests/`, run with `.venv/bin/python -m pytest`)
* `test_core.py`, `test_wake_word.py`, `test_permission_flow.py` — sessions, memory separation, permission policy,
  command classifier, intent router, filesystem tools, wake-phrase matching (83 tests).
* `test_live_desktop.py` — real Hyprland: focus/float/move/resize/maximize/minimize/close, app detection, **ydotool mouse click
  and keyboard typing**, screenshots, clipboard, volume (uses a throwaway kitty window).
* `test_gui.py` — drives the real widgets offscreen: all six states, status pills, text round-trip with Claude, CLEAR CONTEXT
  button, confirmation buttons, settings dialog.
* `e2e_handsfree.py` — **the hands-free flow over a real PipeWire capture path** (a virtual “room” sink: speech is played
  into it, Mei Mei's real `MicStream` records its monitor; her TTS is routed back into the room): launch → wake → request →
  spoken reply → back to listening → second turn with context → barge-in → self-trigger rejection → noise/false wake
  phrases → wake-then-silence → pause/resume → mic unplug/replug → push-to-talk → disable.
* `e2e_flow.py` — the spec's story with the real Claude session and real desktop (open Firefox → search → open first result →
  “clear your context” → old context gone, persona + permanent memory intact → `./clear-context.sh` → restart recovery).

### Known limitations (please read)
* I could not speak into your microphone. Voice tests use **synthetic speech (Piper voices different from Mei Mei's)**
  played into virtual PipeWire devices. Please do a real-voice check: run `./run.sh`, watch the WAKE WORD pill, say “Hey Mei Mei”.
* Barge-in / echo behaviour was tested with an attenuated, delayed loopback, **not with real speakers in a real room**.
* Your internal digital mic is very hot (low-frequency rumble, peaks at full scale). A 120 Hz high-pass filter is applied;
  if recognition is poor, lower that source's volume (`wpctl set-volume <id> 0.46`) or pick another mic in Settings.
* Window tools use Hyprland's **Lua** dispatch syntax (Hyprland ≥ 0.55). Older hyprlang-only versions need edits in `tools/windows.py`.
* “Open the first result” relies on Claude looking at a screenshot and clicking; it works but can be wrong on busy pages.
* Typing non-ASCII text goes through the clipboard (restored afterwards). ydotool needs `ydotoold` and `input`-group membership.
* Each Claude turn takes ~2–7 s (network + model); speech recognition adds ~0.2 s. Usage counts against your Claude login.

## Moving to a new PC
1. Clone this repo and run `./install.sh`.
2. Run `extras/install_extras.sh` to set up the reaction-GIF overlay (needs quickshell). Optionally add your own
   Giphy key to `~/.local/share/meimei/secrets/giphy_key` and run `download_pack.sh`.
3. Authenticate Claude Code on the new machine, then run `./run.sh`.
Permanent memory, session data and secrets are intentionally not included.
