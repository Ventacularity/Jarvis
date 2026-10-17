# Jarvis

A voice-controlled home assistant running on a Raspberry Pi 4, with a web-based HUD, a smart-plug–controlled lamp, and a "drop the needle" live-streaming party trick for a projector.

## What it does

Jarvis listens continuously on a USB microphone for the wake word **"jarvis"**, transcribes your command, and routes it to a handler. Status and responses are shown on a local HUD page (served over Flask + Server-Sent Events, displayed full-screen in kiosk Chromium) instead of physical OLED screens.

### Voice commands

| Say... | Does |
|---|---|
| "Jarvis, what's the weather" | Current weather for your city (IP-geolocated, falls back to Berkeley) |
| "Jarvis, what's the news" | Latest headlines |
| "Jarvis, what's the stock price of ..." | Quote via Finnhub |
| "Jarvis, wikipedia ..." | Wikipedia summary |
| "Jarvis, what time is it" | Current time |
| "Jarvis, draw/generate/create/paint/make/show me ..." | AI image, shown on the HUD |
| "Jarvis, turn the power on/off" | Toggles the smart plug (lamp) |
| "Jarvis, drop my needle" | Plays a configured YouTube video live on the projector; kills the lamp if it's on |
| "Jarvis, pause" | Pause/resume the needle |
| "Jarvis, kill" | Stop the needle, restore the idle image |
| "Jarvis, sleep" / "Jarvis, wake" | Blank/restore the HUD |
| "shutdown" (anywhere in speech) | Stops any speech playback |

### HUD

A single-page dashboard (`hud/index.html`) showing:
- Live weather, headlines, and stock ticker (refreshed on a background loop)
- The last heard command and Jarvis's reply
- AI-generated images on request
- A pulsing "NOW PLAYING ON PROJECTOR" banner while the needle is active

### Projector / "drop my needle"

- Streams the configured YouTube URL live via `mpv`'s built-in `yt-dlp` support — no download/caching step.
- Prefers H.264 (`vcodec^=avc1`) for Pi 4 hardware decoding; falls back to best available.
- Checks `xrandr` for the projector's actual connection state (`HDMI-A-2`) before doing anything, so commands degrade gracefully (just says it's not connected) instead of trying to render to a dead output.
- Shows a static idle image (`Ventacularity.png`) on the projector whenever the needle isn't playing.
- Turns the lamp off when the needle drops (no-op if it's already off); does not touch the lamp on stop.
- Audio is routed to the Pi's headphone jack (`plughw:0,0`), same device used for Jarvis's own speech.

### Smart plug

Controls a Kauf smart plug over its local HTTP API (ESPHome-style `turn_on`/`turn_off` endpoints) at a configured static IP.

## Running it

Jarvis runs as a systemd service (`jarvis.service`) on the Pi so it survives reboots and restarts on crash.

```bash
sudo systemctl status jarvis.service
sudo systemctl restart jarvis.service
journalctl -u jarvis.service -f      # live logs
```

> Note: restarting mid-stream will kill any in-flight `mpv`/`yt-dlp` process too — systemd's default `KillMode` stops the whole cgroup.

### Requirements

- Python 3 with: `requests`, `pytz`, `numpy`, `sounddevice`, `soundfile`, `webrtcvad`, `openai`, `flask`
- `mpv` and an up-to-date `yt-dlp` (install via `pip install --break-system-packages yt-dlp`, **not** `apt`, so it can be upgraded cleanly when YouTube changes break older versions)
- `chromium` or `chromium-browser` for the kiosk HUD display
- A `~/.jarvis_env` file (key=value per line, loaded at startup) providing:
  - `OPENAI_API_KEY`
  - `NEWS_API_KEY`
  - `FINNHUB_API_KEY`

### Config (top of `jarvis.py`)

- `PLUG_IP` — smart plug's local IP
- `NEEDLE_URL` — YouTube URL to stream for "drop my needle"
- `PROJECTOR_GEOMETRY` / `PROJECTOR_OUTPUT_NAME` — projector's pixel offset and `xrandr` output name, for exact window placement (`mpv --no-border --ontop --geometry=...`, which is robust across window managers, unlike `--fs`)
- `DEFAULT_CITY`, `TIMEZONE` — weather/time fallback

## Repo layout

```
jarvis.py        — main assistant process
hud/index.html   — the HUD page served to the kiosk browser
Ventacularity.png — idle image shown on the projector when nothing's playing
safe/            — older backup copy of jarvis.py / index.html
```
