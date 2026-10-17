# Jarvis

A voice-controlled home assistant running on a Raspberry Pi 4, with a web-based HUD, a smart-plug–controlled lamp, and a live-streaming projector trick.

## What it does

Jarvis listens for the wake word **"jarvis"** and handles things like:
- Answering questions, weather, stocks, news, Wikipedia lookups, the time
- Turning the lamp on or off (smart plug)
- Drawing AI-generated pictures
- "Drop my needle" — plays music/video live on the projector, dims the lamp, shows an idle picture when nothing's playing

Responses show up on a local HUD page (weather, headlines, stocks, last command, generated images) instead of physical OLED screens.

The projector checks it's actually connected before trying to use it, so things degrade gracefully if it's unplugged. Needle audio comes out of the Pi's headphone jack.

## Running it

Runs as a systemd service so it survives reboots/crashes:

```bash
sudo systemctl status jarvis.service
sudo systemctl restart jarvis.service
journalctl -u jarvis.service -f      # live logs
```

### Requirements

- Python 3 with: `requests`, `pytz`, `numpy`, `sounddevice`, `soundfile`, `webrtcvad`, `openai`, `flask`
- `mpv` + an up-to-date `yt-dlp` (`pip install --break-system-packages yt-dlp`, not `apt` — keeps it upgradable when YouTube breaks older versions)
- `chromium`/`chromium-browser` for the kiosk HUD display
- A `~/.jarvis_env` file with `OPENAI_API_KEY`, `NEWS_API_KEY`, `FINNHUB_API_KEY`

### Config (top of `jarvis.py`)

- `PLUG_IP` — smart plug's IP
- `NEEDLE_URL` — what plays for "drop my needle"
- `PROJECTOR_GEOMETRY` / `PROJECTOR_OUTPUT_NAME` — projector placement
- `DEFAULT_CITY`, `TIMEZONE` — weather/time fallback

## Repo layout

```
jarvis.py         — main assistant process
hud/index.html    — HUD page
Ventacularity.png — idle image on the projector
safe/             — older backup copy
```
