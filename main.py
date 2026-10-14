import os
import re
import time
import tempfile
import asyncio
import flet as ft
import edge_tts

# =========================================================
# CONFIGURATION & VOICE DATA
# =========================================================

APP_TITLE = "Myanmar TTS Studio Pro"
WORDS_PER_SUBTITLE = 20
MAX_CHUNK_CHARACTERS = 1800
DEFAULT_VOICE_ID = "it-IT-GiuseppeMultilingualNeural"
DEFAULT_SPEED = 30

PINNED_VOICES = {
    "my-MM-NilarNeural",
    "my-MM-ThihaNeural",
    "it-IT-GiuseppeMultilingualNeural",
}

VOICE_DISPLAY_NAMES = {
    "it-IT-GiuseppeMultilingualNeural": "နေတိုး (Default)",
    "my-MM-NilarNeural": "ဝတ်မှုန်ရွှေရည်",
    "my-MM-ThihaNeural": "ပြေတီဦး",
}

VOICE_ALIAS_NAMES = [
    "အကယ်ဒမီမင်းမော်ကွန်း", "ပိုင်တံခွန်", "ရဲတိုက်", "မြင့်မြတ်", "နေမင်း",
    "အောင်ရဲလင်း", "ကျော်ကျော်ဗို", "လွင်မိုး", "ဖွေးဖွေး", "အိန္ဒြာကျော်ဇင်",
    "သက်မွန်မြင့်", "မိုးဟေကို", "ရွှေမှုံရတီ", "သင်ဇာဝင့်ကျော်", "ပိုင်ဖြိုးသု",
    "ခင်ဝင့်ဝါ", "အေးမြတ်သူ", "အိချောပို", "နန်းဆုဦး", "အေးဝတ်ရည်သောင်း",
    "ဆုပန်ထွာ", "မေမြင့်မိုရ်", "ဝါဆိုမိုးဦး", "သံသာမိုးသိမ့်", "မြတ်နိုးအေး",
    "ခင်သဇင်", "ယွန်းယွန်း", "မင်းဦး", "ခန့်စည်သူ", "ထွန်းထွန်း", "ဒေါင်း",
    "အောင်လေး", "ဇေရဲထက်", "ဟိန်းဝေယံ", "လူမင်း", "ခိုင်သင်းကြည်", "စိုးမြတ်သူဇာ",
]

OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "mm_tts_studio")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# =========================================================
# TEXT & SRT PROCESSING (Pure Python)
# =========================================================

def normalize_script(text):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join([line for line in lines if line])

def split_long_text(text, max_chars=MAX_CHUNK_CHARACTERS):
    words = text.split()
    chunks, current_words, current_length = [], [], 0
    for word in words:
        added_length = len(word) + (1 if current_words else 0)
        if current_words and current_length + added_length > max_chars:
            chunks.append(" ".join(current_words))
            current_words, current_length = [word], len(word)
        else:
            current_words.append(word)
            current_length += added_length
    if current_words:
        chunks.append(" ".join(current_words))
    return chunks

def split_for_tts(text):
    text = (text or "").strip()
    if not text:
        return []
    parts = re.findall(r"[^။]*။|[^။]+$", text, flags=re.DOTALL)
    chunks = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) > MAX_CHUNK_CHARACTERS:
            chunks.extend(split_long_text(part, MAX_CHUNK_CHARACTERS))
        else:
            chunks.append(part)
    return chunks

def split_for_srt(text, words_per_subtitle=WORDS_PER_SUBTITLE):
    words = text.split()
    return [
        " ".join(words[i : i + words_per_subtitle])
        for i in range(0, len(words), words_per_subtitle)
    ]

def speed_to_rate(speed):
    speed = max(-50, min(100, int(speed)))
    return f"+{speed}%" if speed >= 0 else f"{speed}%"

def format_srt_time(milliseconds):
    milliseconds = max(0, int(milliseconds))
    hours, rem = divmod(milliseconds, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"

def make_srt_entries(subtitle_texts, total_duration_ms):
    if not subtitle_texts or total_duration_ms <= 0:
        return []
    weights = [max(1, len(t.replace(" ", ""))) for t in subtitle_texts]
    total_weight = sum(weights)
    entries, elapsed = [], 0
    for idx, (text, weight) in enumerate(zip(subtitle_texts, weights)):
        start = elapsed
        if idx == len(subtitle_texts) - 1:
            end = total_duration_ms
        else:
            elapsed += round(total_duration_ms * weight / total_weight)
            end = elapsed
        entries.append({"text": text, "start": start, "end": end})
    return entries

def build_srt(entries):
    blocks = [
        f"{i}\n{format_srt_time(e['start'])} --> {format_srt_time(e['end'])}\n{e['text']}"
        for i, e in enumerate(entries, start=1)
    ]
    return "\n\n".join(blocks) + "\n"

# =========================================================
# EDGE TTS STREAMING ENGINE
# =========================================================

async def synthesize_and_merge(chunks, voice, rate, output_mp3_path, on_progress):
    total_duration_ms = 0
    total_chunks = len(chunks)

    with open(output_mp3_path, "wb") as outfile:
        for idx, chunk_text in enumerate(chunks):
            await on_progress(idx, total_chunks)
            communicate = edge_tts.Communicate(text=chunk_text, voice=voice, rate=rate)
            chunk_duration_ms = 0

            async for message in communicate.stream():
                if message["type"] == "audio":
                    outfile.write(message["data"])
                elif message["type"] == "WordBoundary":
                    end_ms = (message["offset"] + message["duration"]) / 10000.0
                    if end_ms > chunk_duration_ms:
                        chunk_duration_ms = end_ms

            if chunk_duration_ms == 0:
                chunk_duration_ms = max(1200, len(chunk_text) * 85)

            total_duration_ms += chunk_duration_ms

    return int(total_duration_ms)

# =========================================================
# MODERN STUDIO UI (FLET)
# =========================================================

async def main(page: ft.Page):
    page.title = APP_TITLE
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#0B0F19"
    page.padding = 0
    page.scroll = ft.ScrollMode.HIDDEN

    CARD_BG = "#151C2C"
    BORDER_COLOR = "#263147"
    ACCENT_PURPLE = "#7C3AED"
    ACCENT_CYAN = "#06B6D4"
    TEXT_MUTED = "#94A3B8"

    voice_map = {
        "နေတိုး (Default)": "it-IT-GiuseppeMultilingualNeural",
        "ဝတ်မှုန်ရွှေရည်": "my-MM-NilarNeural",
        "ပြေတီဦး": "my-MM-ThihaNeural",
    }

    current_mp3_path = None
    current_srt_path = None
    is_playing = False

    audio_player = ft.Audio(autoplay=False)
    page.overlay.append(audio_player)

    header_banner = ft.Container(
        gradient=ft.LinearGradient(
            begin=ft.alignment.top_left,
            end=ft.alignment.bottom_right,
            colors=["#4F46E5", "#7C3AED", "#06B6D4"],
        ),
        border_radius=ft.border_radius.only(bottom_left=28, bottom_right=28),
        padding=ft.padding.only(left=20, right=20, top=42, bottom=22),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Column(
                    spacing=2,
                    controls=[
                        ft.Row(
                            spacing=8,
                            controls=[
                                ft.Icon(ft.icons.GRAPHIC_EQ_ROUNDED, color="#FFFFFF", size=28),
                                ft.Text("MM TTS STUDIO", size=21, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
                            ],
                        ),
                        ft.Text("AI Voiceover & Auto SRT Generator", size=12, color="#E2E8F0"),
                    ],
                ),
                ft.Container(
                    bgcolor="#33FFFFFF",
                    border_radius=20,
                    padding=ft.padding.symmetric(horizontal=12, vertical=6),
                    content=ft.Text("PRO", size=11, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
                ),
            ],
        ),
    )

    voice_dropdown = ft.Dropdown(
        options=[ft.dropdown.Option(k) for k in voice_map.keys()],
        value="နေတိုး (Default)",
        bgcolor="#0B0F19",
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_CYAN,
        border_radius=12,
        text_size=14,
        expand=True,
        content_padding=ft.padding.symmetric(horizontal=14, vertical=10),
    )

    voice_badge = ft.Text("3 Voices Ready", size=11, color=ACCENT_CYAN)

    async def load_voices(e=None):
        voice_badge.value = "🔄 Loading voices..."
        page.update()
        try:
            voices = await edge_tts.list_voices()
            voice_map.clear()
            used_names = set()
            alias_idx = 0

            for v_id, v_name in VOICE_DISPLAY_NAMES.items():
                voice_map[v_name] = v_id
                used_names.add(v_name)

            for v in voices:
                s_name = v.get("ShortName", "")
                if "MultilingualNeural" in s_name and s_name not in PINNED_VOICES:
                    while alias_idx < len(VOICE_ALIAS_NAMES):
                        candidate = VOICE_ALIAS_NAMES[alias_idx]
                        alias_idx += 1
                        if candidate not in used_names:
                            voice_map[candidate] = s_name
                            used_names.add(candidate)
                            break

            voice_dropdown.options = [ft.dropdown.Option(k) for k in voice_map.keys()]
            voice_dropdown.value = "နေတိုး (Default)"
            voice_badge.value = f"✨ {len(voice_map)} Voices Active"
        except Exception:
            voice_badge.value = "⚠️ Offline Fallback Voices"
        page.update()

    refresh_btn = ft.IconButton(
        icon=ft.icons.REFRESH_ROUNDED,
        icon_color=ACCENT_CYAN,
        bgcolor="#0B0F19",
        tooltip="Voice List ပြန်ယူမယ်",
        on_click=load_voices,
    )

    speed_badge = ft.Container(
        bgcolor="#1E293B",
        border_radius=8,
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
        content=ft.Text(f"+{DEFAULT_SPEED}%", size=12, weight=ft.FontWeight.BOLD, color=ACCENT_CYAN),
    )

    def on_speed_change(e):
        val = int(e.control.value)
        sign = "+" if val >= 0 else ""
        speed_badge.content.value = f"{sign}{val}%"
        page.update()

    speed_slider = ft.Slider(
        min=-50,
        max=100,
        divisions=30,
        value=DEFAULT_SPEED,
        active_color=ACCENT_CYAN,
        inactive_color="#0B0F19",
        on_change=on_speed_change,
    )

    voice_card = ft.Container(
        bgcolor=CARD_BG,
        border=ft.border.all(1, BORDER_COLOR),
        border_radius=18,
        padding=16,
        content=ft.Column(
            spacing=12,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Row(
                            spacing=6,
                            controls=[
                                ft.Icon(ft.icons.RECORD_VOICE_OVER_ROUNDED, color=ACCENT_PURPLE, size=18),
                                ft.Text("အသံဇာတ်ကောင် ရွေးချယ်ရန်", size=14, weight=ft.FontWeight.W_600),
                            ],
                        ),
                        voice_badge,
                    ],
                ),
                ft.Row([voice_dropdown, refresh_btn], spacing=10),
                ft.Divider(height=1, color=BORDER_COLOR),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Row(
                            spacing=6,
                            controls=[
                                ft.Icon(ft.icons.SPEED_ROUNDED, color=ACCENT_CYAN, size=18),
                                ft.Text("အသံအမြန်နှုန်း (Speed)", size=14, weight=ft.FontWeight.W_600),
                            ],
                        ),
                        speed_badge,
                    ],
                ),
                speed_slider,
            ],
        ),
    )

    char_count_text = ft.Text("0 chars • 0 ။", size=12, color=TEXT_MUTED)

    def update_stats(e=None):
        txt = script_input.value or ""
        sentences = txt.count("။")
        char_count_text.value = f"{len(txt)} chars • {sentences} ။"
        page
