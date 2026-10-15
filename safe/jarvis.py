import os
import sys
import json
import queue
import requests
import datetime
import pytz
import time
import subprocess
import textwrap
import threading
import collections
import numpy as np
import sounddevice as sd
import soundfile as sf
import webrtcvad
from openai import OpenAI
from flask import Flask, Response, send_from_directory

# ================================
# LOAD CENTRALIZED ENV FILE
# ================================

def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                os.environ[key] = value

load_env_file(os.path.expanduser("~/.jarvis_env"))

# ================================
# CONFIG
# ================================

WAKE_WORD    = "jarvis"
KILL_WORD    = "shutdown"
TIMEZONE     = "America/Los_Angeles"
DEFAULT_CITY = "Berkeley"
SAMPLERATE   = 44100
PLUG_IP      = "192.168.4.96"

client          = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
NEWS_API_KEY    = os.getenv("NEWS_API_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

current_player = None
is_speaking    = False
hud_asleep     = False   # True while the display is blanked via "Jarvis, sleep"

# ================================
# MIC DEVICE AUTO-DETECT
# ================================
# Device indices shift whenever hardware is plugged/unplugged (a monitor's
# HDMI audio, a USB speaker, etc. all insert themselves into the list), so
# we look the mic up by name instead of hardcoding an index.

def find_input_device(preferred_name_substrings=("USB", "usb")):
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and any(s in d["name"] for s in preferred_name_substrings):
            print(f"Using input device {i}: {d['name']}")
            return i
    # Fall back to the first device with any input channels at all.
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            print(f"USB mic not found by name; falling back to device {i}: {d['name']}")
            return i
    raise RuntimeError("No input (microphone) device found. Run "
                        "sd.query_devices() to check what's connected.")

INPUT_DEVICE = find_input_device()

# ================================
# HUD — local web page shown via a kiosk browser, replaces both the
# original physical I2C OLED screens and the earlier Tkinter window
# ================================
# jarvis.py runs a small local Flask server and pushes live updates to the
# browser over Server-Sent Events (SSE). The visuals themselves live in
# hud/index.html — this just feeds it status/conversation/image updates.

HUD_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud")
HUD_HOST = "127.0.0.1"
HUD_PORT = 5151

_hud_queue = queue.Queue()
_flask_app = Flask(__name__)

def _push_hud(payload):
    _hud_queue.put(payload)

@_flask_app.route("/")
def _hud_index():
    return send_from_directory(HUD_DIR, "index.html")

@_flask_app.route("/events")
def _hud_events():
    def stream():
        while True:
            payload = _hud_queue.get()
            yield f"data: {json.dumps(payload)}\n\n"
    return Response(stream(), mimetype="text/event-stream",
                     headers={"Cache-Control": "no-cache",
                              "X-Accel-Buffering": "no"})

def _start_hud_server():
    threading.Thread(
        target=lambda: _flask_app.run(
            host=HUD_HOST, port=HUD_PORT, debug=False,
            use_reloader=False, threaded=True
        ),
        daemon=True
    ).start()

def _launch_kiosk_browser():
    # Only the browser needs a display — jarvis.py itself stays headless-
    # capable, so this runs fine over plain SSH as long as DISPLAY points
    # at a live desktop session (e.g. `export DISPLAY=:0` before launching).
    url = f"http://{HUD_HOST}:{HUD_PORT}/"
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}
    for binary in ("chromium", "chromium-browser"):
        try:
            subprocess.Popen(
                [binary, "--kiosk", "--noerrdialogs", "--disable-infobars",
                 "--autoplay-policy=no-user-gesture-required", url],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print(f"Launched {binary} in kiosk mode at {url}")
            return
        except FileNotFoundError:
            continue
    print(f"Couldn't find chromium or chromium-browser installed. "
          f"Open {url} manually in a browser on the Pi's display.")

# ---- Panel 1 replacement (was Display 1 / show_display, show_status) ----

def show_display(top_label, top_text, bottom_label=None, bottom_text=None):
    _push_hud({"you": top_text, "jarvis": bottom_text if bottom_label else None})

def show_status(message):
    _push_hud({"status": message})

# ---- Panel 2 replacement (was Display 2 / image functions) ----

def show_image_on_screen2(image_b64):
    # gpt-image models only return base64-encoded image data (no hosted
    # URL), so this is handed straight to the browser as a data URL — no
    # local decoding/resizing needed, the browser scales it with CSS.
    _push_hud({"image": f"data:image/png;base64,{image_b64}"})

def show_screen2_status(message):
    _push_hud({"image_caption": message})

def show_screen2_prompt(prompt):
    _push_hud({"image_caption": f"Rendering: {prompt}"})

def clear_screen2():
    _push_hud({"image": None, "image_caption": None})

# ================================
# SMART PLUG CONTROL
# ================================

def control_plug(command):
    if "on" in command:
        url    = f"http://{PLUG_IP}/switch/kauf_plug/turn_on"
        action = "on"
    else:
        url    = f"http://{PLUG_IP}/switch/kauf_plug/turn_off"
        action = "off"
    try:
        requests.post(url, timeout=5)
        return f"Plug turned {action}."
    except Exception as e:
        print("Plug error:", e)
        return "I couldn't reach the plug."

# ================================
# IMAGE GENERATION (DALL-E)
# ================================

# An image now clears itself a few seconds after being shown instead of
# lingering until the next command. _image_token guards against a race: if
# a *new* image gets requested before the old one's timer fires, the old
# timer sees its token is stale and skips clearing (so it doesn't wipe out
# the newer image).
IMAGE_DISPLAY_SECONDS = 8
_image_token      = 0
_image_token_lock = threading.Lock()

def _auto_clear_image(token):
    time.sleep(IMAGE_DISPLAY_SECONDS)
    with _image_token_lock:
        if _image_token == token:
            clear_screen2()
            show_screen2_status("Ready.")

def generate_image(command):
    global _image_token

    prompt = command
    for trigger in ["draw", "generate", "create", "make", "paint", "show me"]:
        if prompt.startswith(trigger):
            prompt = prompt[len(trigger):].strip()
            break

    if not prompt:
        return "What would you like me to draw?"

    enhanced_prompt = (
        f"{prompt}. Highly detailed, rich color, realistic lighting and "
        f"texture, well-composed with a fitting background."
    )

    show_screen2_prompt(prompt)

    try:
        response = client.images.generate(
            model="gpt-image-2.5-flare",
            prompt=enhanced_prompt,
            size="1024x1024",
            quality="low",
            n=1
        )
        image_b64 = response.data[0].b64_json
        show_image_on_screen2(image_b64)
        show_screen2_status("Ready.")

        with _image_token_lock:
            _image_token += 1
            token = _image_token
        threading.Thread(target=_auto_clear_image, args=(token,), daemon=True).start()

        return f"Here's your image of {prompt}."
    except Exception as e:
        show_screen2_status("Ready.")
        print("DALL-E error:", e)
        return "Sorry, I couldn't generate that image."

# ================================
# AUDIO
# ================================

# ---- Voice-activity-detected recording ----
# Replaces the old fixed-length record_audio(duration=8): instead of always
# recording a blind 8-second window (slow when you're done early, and it can
# clip you if you start talking late in the window), this streams
# continuously and uses webrtcvad to start as soon as speech begins and stop
# shortly after it ends.

# This USB mic only supports its native rate (SAMPLERATE, 44100Hz) — it
# rejects 16000 and 48000 directly. webrtcvad, however, only accepts
# 8000/16000/32000/48000. So we record at the mic's native 44100Hz (full
# quality, what Whisper gets) and separately resample a throwaway copy of
# each small chunk down to 16000Hz purely so webrtcvad can classify it.
VAD_CLASSIFY_RATE      = 16000
VAD_FRAME_MS           = 30      # webrtcvad only accepts 10/20/30 ms frames
NATIVE_FRAME_SIZE      = int(SAMPLERATE * VAD_FRAME_MS / 1000)         # e.g. 1323 @ 44100Hz
CLASSIFY_FRAME_SIZE    = int(VAD_CLASSIFY_RATE * VAD_FRAME_MS / 1000)  # 480 @ 16000Hz
VAD_AGGRESSIVENESS     = 2       # 0 (permissive) - 3 (aggressive about cutting non-speech)
PREROLL_FRAMES         = 10      # ~300ms kept before speech is confirmed, so word onsets aren't clipped
SILENCE_FRAMES_TO_STOP = 27      # ~800ms of trailing silence ends the utterance
MAX_RECORD_SECONDS     = 12      # safety cap in case VAD never detects silence

_vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)

def _resample_for_vad(chunk):
    """chunk: 1-D int16 array captured at SAMPLERATE. Returns a cheap linear-
    interpolation resample to VAD_CLASSIFY_RATE, good enough for speech/silence
    classification (not used for anything that needs audio fidelity)."""
    x_old = np.linspace(0, 1, len(chunk), endpoint=False)
    x_new = np.linspace(0, 1, CLASSIFY_FRAME_SIZE, endpoint=False)
    return np.interp(x_new, x_old, chunk.astype(np.float32)).astype(np.int16)

def listen_for_speech(filename="input.wav"):
    """Blocks until one spoken utterance has been captured (or MAX_RECORD_SECONDS
    elapses). Returns True if speech was captured and written to `filename`,
    False if nothing was said."""
    ring_buffer = collections.deque(maxlen=PREROLL_FRAMES)
    recorded_frames = []
    triggered = False
    silence_run = 0
    max_frames = int(MAX_RECORD_SECONDS * 1000 / VAD_FRAME_MS)

    with sd.InputStream(samplerate=SAMPLERATE, channels=1, dtype="int16",
                         device=INPUT_DEVICE, blocksize=NATIVE_FRAME_SIZE) as stream:
        for frame_count in range(max_frames):
            data, _ = stream.read(NATIVE_FRAME_SIZE)
            vad_chunk = _resample_for_vad(data.reshape(-1))
            is_speech = _vad.is_speech(vad_chunk.tobytes(), VAD_CLASSIFY_RATE)

            if not triggered:
                ring_buffer.append((data.copy(), is_speech))
                voiced = sum(1 for _, s in ring_buffer if s)
                if voiced > 0.6 * ring_buffer.maxlen:
                    triggered = True
                    show_status("Listening...")
                    recorded_frames.extend(f for f, _ in ring_buffer)
                    ring_buffer.clear()
            else:
                recorded_frames.append(data.copy())
                if is_speech:
                    silence_run = 0
                else:
                    silence_run += 1
                    if silence_run > SILENCE_FRAMES_TO_STOP:
                        break

    if not recorded_frames:
        return False

    audio = np.concatenate(recorded_frames, axis=0)
    sf.write(filename, audio, SAMPLERATE)
    return True

def transcribe_audio(filename="input.wav"):
    with open(filename, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file
        )
    return transcript.text.strip().lower()

def play_beep():
    try:
        subprocess.Popen(
            ["mpg123", "-q", "/usr/share/sounds/alsa/Front_Left.wav"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        ).wait()
    except Exception:
        pass

def speak_text(text):
    # Streams raw PCM from the TTS API straight into aplay's stdin as each
    # chunk arrives, instead of waiting for the full file to synthesize and
    # download before playback starts (which is what was happening before,
    # and is the main source of the "silent pause before it talks" delay).
    global current_player

    # Pinned explicitly to the Pi's headphone jack (hw:0,0 — "bcm2835
    # Headphones" from `aplay -l`/sd.query_devices()). Without -D, aplay
    # falls back to ALSA's ambiguous "default" route, which can get
    # redirected toward HDMI audio once a monitor is plugged in — this is
    # why audio was coming out somewhere other than the headphones.
    player = subprocess.Popen(
        ["aplay", "-q", "-D", "plughw:0,0", "-r", "24000", "-f", "S16_LE", "-c", "1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    current_player = player

    try:
        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
            response_format="pcm",
        ) as response:
            for chunk in response.iter_bytes(chunk_size=4096):
                if player.stdin is None or player.stdin.closed:
                    break
                try:
                    player.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    break
    finally:
        if player.stdin and not player.stdin.closed:
            try:
                player.stdin.close()
            except OSError:
                pass

    return player

def stop_speaking():
    global current_player
    if current_player and current_player.poll() is None:
        current_player.terminate()
        current_player = None

# ================================
# LOCATION (IP AUTO DETECT)
# ================================

def get_ip_location():
    try:
        r = requests.get("http://ip-api.com/json/").json()
        return r["city"], r["lat"], r["lon"]
    except Exception:
        return DEFAULT_CITY, 37.87, -122.27

# ================================
# WEATHER (Open-Meteo)
# ================================

def get_weather(command):
    city, lat, lon = get_ip_location()

    if "in" in command:
        words = command.split("in", 1)[1].strip()
        geo = requests.get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={words}&count=1"
        ).json()
        if "results" in geo:
            lat  = geo["results"][0]["latitude"]
            lon  = geo["results"][0]["longitude"]
            city = geo["results"][0]["name"]

    forecast = requests.get(
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current_weather=true"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&temperature_unit=fahrenheit"
        f"&timezone=America%2FLos_Angeles"
    ).json()

    if "tomorrow" in command:
        max_temp = forecast["daily"]["temperature_2m_max"][1]
        min_temp = forecast["daily"]["temperature_2m_min"][1]
        return f"Tomorrow in {city}, high {max_temp}°F and low {min_temp}°F."
    else:
        temp = forecast["current_weather"]["temperature"]
        return f"It is currently {temp}°F in {city}."

# ================================
# WIKIPEDIA
# ================================

def get_wikipedia(command):
    query = command.replace("wikipedia", "").strip()
    url   = f"https://en.wikipedia.org/api/rest_v1/page/summary/{query}"
    r     = requests.get(url).json()
    if "extract" in r:
        return r["extract"]
    return "I couldn't find that on Wikipedia."

# ================================
# STOCKS (Finnhub)
# ================================

def get_stock(command):
    words  = command.split()
    ticker = words[-1].upper()

    if not FINNHUB_API_KEY:
        return "Finnhub API key not configured."

    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_API_KEY}",
            timeout=6
        ).json()
        price  = r["c"]   # current price
        change = r["dp"]  # percent change
        if price in (None, 0):
            return "I couldn't retrieve that stock."
        return f"{ticker} is trading at {price} dollars, {round(change, 2)} percent change today."
    except Exception:
        return "I couldn't retrieve that stock."

# ================================
# NEWS
# ================================

def get_news():
    if not NEWS_API_KEY:
        return "News API key not configured."

    r = requests.get(
        f"https://newsapi.org/v2/top-headlines?country=us&pageSize=3&apiKey={NEWS_API_KEY}"
    ).json()

    headlines = [a["title"] for a in r["articles"]]
    return "Top headlines: " + "; ".join(headlines)

# ================================
# HUD PASSIVE DATA WIDGETS (weather / news / stock)
# ================================
# These run on their own schedule in a background thread and feed the
# right-hand side of the HUD even when nobody's actively talking to Jarvis.
# Kept separate from the voice-command versions above so a slow or failed
# fetch here never blocks listening — each one is wrapped in its own
# try/except and simply skips a cycle on failure.

# Lists to rotate through — each gets fetched and joined into one
# continuous scrolling string (same pattern as the headline ticker), so the
# marquee naturally "rotates" through all of them as it scrolls. Edit these
# to whatever cities/tickers you want tracked.
WATCH_CITIES  = ["Berkeley", "Oakland", "San Francisco", "San Jose", "Palo Alto"]
WATCH_TICKERS = ["AAPL", "TSLA", "NVDA", "GOOG"]

HUD_WEATHER_REFRESH_SEC = 600      # 10 min
HUD_NEWS_REFRESH_SEC    = 1800     # 30 min (NewsAPI's free tier has a daily cap)
HUD_STOCK_REFRESH_SEC   = 120      # 2 min

# City name -> (lat, lon), geocoded once on first use and cached — avoids
# re-geocoding every single refresh cycle.
_city_coords = {}

def _geocode_city(city):
    if city in _city_coords:
        return _city_coords[city]
    geo = requests.get(
        f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1",
        timeout=6
    ).json()
    if "results" not in geo:
        return None
    coords = (geo["results"][0]["latitude"], geo["results"][0]["longitude"])
    _city_coords[city] = coords
    return coords

def _hud_fetch_weather():
    parts = []
    for city in WATCH_CITIES:
        try:
            coords = _geocode_city(city)
            if coords is None:
                continue
            lat, lon = coords
            forecast = requests.get(
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&current_weather=true"
                f"&temperature_unit=fahrenheit"
                f"&timezone=America%2FLos_Angeles",
                timeout=6
            ).json()
            temp = forecast["current_weather"]["temperature"]
            parts.append(f"{city} {temp}°F")
        except Exception as e:
            print(f"HUD weather fetch error ({city}):", e)
    return "     •     ".join(parts) if parts else "—"

def _hud_fetch_news():
    if not NEWS_API_KEY:
        return []
    r = requests.get(
        f"https://newsapi.org/v2/top-headlines?country=us&pageSize=3&apiKey={NEWS_API_KEY}",
        timeout=6
    ).json()
    return [a["title"] for a in r.get("articles", [])]

def _hud_fetch_stock():
    parts = []
    for ticker in WATCH_TICKERS:
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_API_KEY}",
                timeout=6
            ).json()
            price  = r.get("c")   # current price
            change = r.get("dp")  # percent change
            if price in (None, 0):
                continue
            arrow = "▲" if change >= 0 else "▼"
            parts.append(f"{ticker} ${price} {arrow}{abs(round(change, 2))}%")
        except Exception as e:
            print(f"HUD stock fetch error ({ticker}):", e)
    return "     •     ".join(parts) if parts else "—"

def _hud_data_loop():
    last = {"weather": 0.0, "news": 0.0, "stock": 0.0}
    while True:
        now = time.time()
        if now - last["weather"] > HUD_WEATHER_REFRESH_SEC:
            try:
                _push_hud({"hud_weather": _hud_fetch_weather()})
            except Exception as e:
                print("HUD weather fetch error:", e)
            last["weather"] = now
        if now - last["news"] > HUD_NEWS_REFRESH_SEC:
            try:
                _push_hud({"hud_news": _hud_fetch_news()})
            except Exception as e:
                print("HUD news fetch error:", e)
            last["news"] = now
        if now - last["stock"] > HUD_STOCK_REFRESH_SEC:
            try:
                _push_hud({"hud_stock": _hud_fetch_stock()})
            except Exception as e:
                print("HUD stock fetch error:", e)
            last["stock"] = now
        time.sleep(5)

def _start_hud_data_loop():
    threading.Thread(target=_hud_data_loop, daemon=True).start()

# ================================
# TIME
# ================================

def get_time():
    tz  = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(tz)
    return f"The time is {now.strftime('%I:%M %p')}."

# ================================
# GPT FALLBACK (Concise)
# ================================

def get_gpt_response(prompt):
    system_prompt = "You are a concise home assistant. Respond in under 3 sentences."

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt}
        ],
        max_tokens=150,
        temperature=0.6
    )

    return response.choices[0].message.content.strip()

# ================================
# ROUTER
# ================================

def route_intent(command):
    if command.startswith("sleep") or command == "go to sleep":
        return "sleep"
    if command.startswith("wake"):
        return "wake"
    if command.startswith("draw") or command.startswith("generate") or \
       command.startswith("create") or command.startswith("paint") or \
       command.startswith("make") or command.startswith("show me"):
        return "image"
    if "power" in command and ("turn on" in command or "turn off" in command):
        return "plug"
    if "weather" in command:
        return "weather"
    if "stock" in command or "price" in command:
        return "stock"
    if "wikipedia" in command:
        return "wiki"
    if "news" in command:
        return "news"
    if "time" in command:
        return "time"
    return "general"

# ================================
# MAIN LOOP
# ================================

_start_hud_server()
time.sleep(0.6)  # give Flask a moment to bind before the browser requests it
_launch_kiosk_browser()
_start_hud_data_loop()

print("Jarvis online.")
show_status("Jarvis online.")
show_screen2_status("Ready.")

try:
    while True:

        if is_speaking:
            time.sleep(0.1)
            continue

        show_status("Listening...")
        if not listen_for_speech():
            continue
        user_text = transcribe_audio()

        if not user_text:
            continue

        if KILL_WORD in user_text:
            stop_speaking()
            show_status("Shutting down.")
            clear_screen2()
            speak_text("Shutting down.")
            time.sleep(1)
            # Exit code 42 tells jarvis.service this was a deliberate
            # shutdown, not a crash, so systemd should leave it stopped
            # instead of restarting it.
            sys.exit(42)

        if WAKE_WORD not in user_text:
            continue

        after_wake = user_text.split(WAKE_WORD, 1)[1].strip().strip(".,!? ")

        if after_wake:
            command = after_wake
        else:
            show_status("Go ahead...")
            play_beep()
            if not listen_for_speech():
                continue
            command = transcribe_audio()

        # Whisper often appends trailing punctuation ("wake up.") that broke
        # exact-match intents like sleep/wake/stop further down — strip it
        # here so every intent check sees clean text either way.
        command = command.strip().strip(".,!? ")

        if not command:
            continue

        print("You:", command)
        show_display("You:", command)

        if command in ["stop", "be quiet"]:
            stop_speaking()
            show_status("Stopped.")
            continue

        intent = route_intent(command)

        if intent != "image":
            # Clear the leftover image from a previous "draw X" request so
            # it doesn't linger on screen once the conversation has moved on.
            clear_screen2()
            show_screen2_status("Ready.")

        if intent == "image":
            response = generate_image(command)
        elif intent == "sleep":
            hud_asleep = True
            _push_hud({"hud_sleep": True})
            response = "Going to sleep. Say wake up when you need me."
        elif intent == "wake":
            hud_asleep = False
            _push_hud({"hud_sleep": False})
            response = "I'm back online."
        elif intent == "plug":
            response = control_plug(command)
        elif intent == "weather":
            response = get_weather(command)
        elif intent == "stock":
            response = get_stock(command)
        elif intent == "wiki":
            response = get_wikipedia(command)
        elif intent == "news":
            response = get_news()
        elif intent == "time":
            response = get_time()
        else:
            response = get_gpt_response(command)

        print("Jarvis:", response)
        show_display("You:", command, "Jarvis:", response)

        is_speaking = True
        player = speak_text(response)

        while player.poll() is None:
            time.sleep(0.1)

        time.sleep(2.5)
        is_speaking = False

except KeyboardInterrupt:
    pass

# A manual Ctrl+C counts as deliberate too — don't let systemd
# bring it right back.
sys.exit(42)
