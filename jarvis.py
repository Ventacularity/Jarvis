import os
import io
import requests
import datetime
import pytz
import time
import subprocess
import textwrap
import sounddevice as sd
import soundfile as sf
from openai import OpenAI
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas
from PIL import Image

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

client       = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

current_player = None
is_speaking    = False

# ================================
# DISPLAY 1 — Text (I2C-1, pins 3+5)
# ================================

serial1  = i2c(port=1, address=0x3C)
display1 = ssd1306(serial1)

def wrap_text(text, max_chars=18):
    return textwrap.wrap(text, width=max_chars)

def show_display(top_label, top_text, bottom_label=None, bottom_text=None):
    lines = []
    lines.append(top_label)
    lines += wrap_text(top_text)

    if bottom_label and bottom_text:
        lines.append("")
        lines.append(bottom_label)
        lines += wrap_text(bottom_text)

    max_lines = 6
    if len(lines) <= max_lines:
        with canvas(display1) as draw:
            for i, line in enumerate(lines):
                draw.text((0, i * 10), line, fill="white")
    else:
        for start in range(0, len(lines), max_lines):
            chunk = lines[start:start + max_lines]
            with canvas(display1) as draw:
                for i, line in enumerate(chunk):
                    draw.text((0, i * 10), line, fill="white")
            time.sleep(2.5)

def show_status(message):
    with canvas(display1) as draw:
        draw.text((0, 28), message, fill="white")

# ================================
# DISPLAY 2 — Image screen (I2C-3, pins 7+29)
# ================================

serial2  = i2c(port=3, address=0x3C)
display2 = ssd1306(serial2)

def show_image_on_screen2(image_url):
    try:
        r   = requests.get(image_url)
        img = Image.open(io.BytesIO(r.content))
        img = img.convert("RGB")
        img = img.resize((128, 64), Image.LANCZOS)
        img = img.convert("1")
        display2.display(img)
    except Exception as e:
        show_screen2_status("Image error.")
        print("Display 2 error:", e)

def show_screen2_status(message):
    with canvas(display2) as draw:
        draw.text((0, 28), message, fill="white")

def show_screen2_prompt(prompt):
    lines = wrap_text(prompt)
    with canvas(display2) as draw:
        draw.text((0, 0), "Drawing:", fill="white")
        for i, line in enumerate(lines[:5]):
            draw.text((0, (i + 1) * 10), line, fill="white")

def clear_screen2():
    with canvas(display2) as draw:
        pass

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

def generate_image(command):
    prompt = command
    for trigger in ["draw", "generate", "create", "make", "paint", "show me"]:
        if prompt.startswith(trigger):
            prompt = prompt[len(trigger):].strip()
            break

    if not prompt:
        return "What would you like me to draw?"

    enhanced_prompt = (
        f"Simple minimalist black and white line drawing of {prompt}. "
        f"Bold thick outlines only, no shading, no details, no background. "
        f"High contrast, like a simple icon or pictogram. "
        f"Optimized for a tiny 128x64 monochrome pixel display."
    )

    show_screen2_prompt(prompt)

    try:
        response = client.images.generate(
            model="dall-e-3",
            prompt=enhanced_prompt,
            size="1024x1024",
            quality="standard",
            n=1
        )
        image_url = response.data[0].url
        show_image_on_screen2(image_url)
        time.sleep(10)
        show_screen2_status("Ready.")
        return f"Here's your image of {prompt}."
    except Exception as e:
        show_screen2_status("Ready.")
        print("DALL-E error:", e)
        return "Sorry, I couldn't generate that image."

# ================================
# AUDIO
# ================================

def record_audio(filename="input.wav", duration=8, samplerate=SAMPLERATE):
    audio = sd.rec(
        int(duration * samplerate),
        samplerate=samplerate,
        channels=1,
        dtype="int16",
        device=1
    )
    sd.wait()
    sf.write(filename, audio, samplerate)

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
    global current_player

    response = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=text
    )

    with open("response.mp3", "wb") as f:
        f.write(response.content)

    current_player = subprocess.Popen(
        ["mpg123", "-q", "response.mp3"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    return current_player

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
# STOCKS (Yahoo Finance)
# ================================

def get_stock(command):
    words  = command.split()
    ticker = words[-1].upper()

    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}"
        ).json()
        result = r["quoteResponse"]["result"][0]
        price  = result["regularMarketPrice"]
        change = result["regularMarketChangePercent"]
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

print("Jarvis online.")
show_status("Jarvis online.")
show_screen2_status("Ready.")

while True:

    if is_speaking:
        time.sleep(0.1)
        continue

    show_status("Listening...")
    record_audio(duration=8)
    user_text = transcribe_audio()

    if not user_text:
        continue

    if KILL_WORD in user_text:
        stop_speaking()
        show_status("Shutting down.")
        clear_screen2()
        speak_text("Shutting down.")
        time.sleep(1)
        display1.cleanup()
        display2.cleanup()
        break

    if WAKE_WORD not in user_text:
        continue

    after_wake = user_text.split(WAKE_WORD, 1)[1].strip().lstrip(".,!? ")

    if after_wake:
        command = after_wake
    else:
        show_status("Go ahead...")
        play_beep()
        record_audio(duration=8)
        command = transcribe_audio()

    if not command:
        continue

    print("You:", command)
    show_display("You:", command)

    if command in ["stop", "be quiet"]:
        stop_speaking()
        show_status("Stopped.")
        continue

    intent = route_intent(command)

    if intent == "image":
        response = generate_image(command)
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
