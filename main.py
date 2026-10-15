import os
import re
import time
import tempfile
import shutil
import flet as ft
import flet_audio as fta
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
    is_paused = False
    playback_completed = False

    # Audio is a non-visual Flet service. Register it in page.services,
    # never in page.overlay, so it cannot add a giant blank rectangle to UI.
    audio_player = None

    def attach_audio_player(player):
        page.services.append(player)

    def detach_audio_player(player):
        if player is None:
            return
        try:
            if player in page.services:
                page.services.remove(player)
        except (ValueError, AttributeError):
            pass

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
        page.update()

    output_name_input = ft.TextField(
        label="ဖိုင်အမည် (MP3 / SRT)",
        hint_text="ဥပမာ - my_movie_voice",
        value="myanmar_tts",
        prefix_icon=ft.icons.DRIVE_FILE_RENAME_OUTLINED,
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_PURPLE,
        border_radius=12,
        text_size=14,
    )

    script_input = ft.TextField(
        hint_text="အသံထုတ်လိုသော မြန်မာစာသားကို ဒီနေရာမှာ ရိုက်ထည့်ပါ...",
        hint_style=ft.TextStyle(color="#475569", size=13),
        multiline=True,
        min_lines=7,
        max_lines=12,
        bgcolor="#0B0F19",
        border_color=BORDER_COLOR,
        focused_border_color=ACCENT_PURPLE,
        border_radius=12,
        text_size=14,
        on_change=update_stats,
    )

    async def paste_clipboard(e):
        clip = await page.get_clipboard_async()
        if clip:
            script_input.value = (script_input.value or "") + clip
            update_stats()

    def clear_script(e):
        script_input.value = ""
        update_stats()

    script_card = ft.Container(
        bgcolor=CARD_BG,
        border=ft.border.all(1, BORDER_COLOR),
        border_radius=18,
        padding=16,
        content=ft.Column(
            spacing=10,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Row(
                            spacing=6,
                            controls=[
                                ft.Icon(ft.icons.DESCRIPTION_ROUNDED, color=ACCENT_PURPLE, size=18),
                                ft.Text("Script စာသား", size=14, weight=ft.FontWeight.W_600),
                            ],
                        ),
                        ft.Row(
                            spacing=4,
                            controls=[
                                ft.TextButton(
                                    "Paste",
                                    icon=ft.icons.CONTENT_PASTE_ROUNDED,
                                    style=ft.ButtonStyle(color=ACCENT_CYAN),
                                    on_click=paste_clipboard,
                                ),
                                ft.TextButton(
                                    "Clear",
                                    icon=ft.icons.DELETE_SWEEP_ROUNDED,
                                    style=ft.ButtonStyle(color="#EF4444"),
                                    on_click=clear_script,
                                ),
                            ],
                        ),
                    ],
                ),
                output_name_input,
                script_input,
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("💡 ပုဒ်မ (။) တစ်ခုချင်းစီအလိုက် အသံဖြတ်ထုတ်ပေးပါသည်", size=11, color=TEXT_MUTED),
                        char_count_text,
                    ],
                ),
            ],
        ),
    )

    progress_bar = ft.ProgressBar(
        value=0,
        color=ACCENT_CYAN,
        bgcolor="#0B0F19",
        height=6,
        border_radius=4,
        visible=False,
    )
    status_label = ft.Text("အသံထုတ်ရန် အသင့်ရှိပါသည်။", size=13, color=TEXT_MUTED)

    play_btn = ft.IconButton(
        icon=ft.icons.PLAY_CIRCLE_FILL_ROUNDED,
        icon_color=ACCENT_CYAN,
        icon_size=42,
        disabled=True,
    )

    result_info = ft.Text("No audio generated yet", size=12, color=TEXT_MUTED)

    async def toggle_play(e):
        nonlocal is_playing, is_paused, playback_completed
        if not current_mp3_path or audio_player is None:
            return
        try:
            if is_playing:
                await audio_player.pause()
                is_playing = False
                is_paused = True
                play_btn.icon = ft.icons.PLAY_CIRCLE_FILL_ROUNDED
            elif is_paused and not playback_completed:
                await audio_player.resume()
                is_playing = True
                is_paused = False
                play_btn.icon = ft.icons.PAUSE_CIRCLE_FILLED_ROUNDED
            else:
                # Play from position zero after completion so long files can
                # be replayed, using the native player's actual state event.
                await audio_player.play(position=0)
                is_playing = True
                is_paused = False
                playback_completed = False
                play_btn.icon = ft.icons.PAUSE_CIRCLE_FILLED_ROUNDED
        except Exception as ex:
            status_label.value = f"❌ Audio Error: {str(ex)[:100]}"
            status_label.color = "#EF4444"
        page.update()

    play_btn.on_click = toggle_play

    async def save_output_file(path, label, extension):
        """Rename/copy the generated file into Android Downloads when possible."""
        nonlocal current_mp3_path, current_srt_path
        if not path or not os.path.isfile(path):
            status_label.value = f"⚠️ {label} ဖိုင် မတွေ့ပါ။ အရင်ထုတ်ပေးပါ။"
            status_label.color = "#F59E0B"
            page.update()
            return

        try:
            raw_name = (output_name_input.value or "myanmar_tts").strip()
            # Keep a safe filename on Android and remove any user-entered extension.
            safe_name = re.sub(r'[\\/:*?"<>|]+', "_", raw_name).strip(" .")
            safe_name = re.sub(r"\s+", "_", safe_name) or "myanmar_tts"
            if safe_name.lower().endswith("." + extension.lower()):
                safe_name = safe_name[:-(len(extension) + 1)].rstrip(" .") or "myanmar_tts"
            filename = f"{safe_name}.{extension}"
            downloads_dir = "/storage/emulated/0/Download"
            source_abs = os.path.abspath(path)
            downloads_abs = os.path.abspath(downloads_dir)

            if os.path.isdir(downloads_dir) and os.access(downloads_dir, os.W_OK):
                destination = os.path.join(downloads_dir, filename)
                if source_abs != os.path.abspath(destination):
                    shutil.copyfile(path, destination)
                if label == "MP3":
                    current_mp3_path = destination
                else:
                    current_srt_path = destination
                status_label.value = f"✅ {filename} ကို Downloads ထဲ သိမ်းပြီးပါပြီ။"
                status_label.color = "#10B981"
            elif source_abs.startswith(downloads_abs + os.sep):
                destination = os.path.join(os.path.dirname(path), filename)
                if source_abs != os.path.abspath(destination):
                    shutil.move(path, destination)
                if label == "MP3":
                    current_mp3_path = destination
                else:
                    current_srt_path = destination
                status_label.value = f"✅ {filename} အမည်ပြောင်းပြီးပါပြီ။"
                status_label.color = "#10B981"
            else:
                status_label.value = (
                    f"⚠️ {label} ကို app ထဲမှာ ထုတ်ပြီးပါပြီ၊ ဒါပေမဲ့ Android Downloads ခွင့်ပြုချက်မရှိပါ။ "
                    "ဖိုင်အမည်ပြောင်းပြီး Downloads ထဲသိမ်းဖို့ Android storage permission လိုပါတယ်။"
                )
                status_label.color = "#F59E0B"
        except Exception as ex:
            status_label.value = f"❌ {label} သိမ်းမရပါ: {str(ex)[:100]}"
            status_label.color = "#EF4444"
        page.update()

    async def share_mp3(e):
        await save_output_file(current_mp3_path, "MP3", "mp3")

    async def share_srt(e):
        await save_output_file(current_srt_path, "SRT", "srt")

    result_card = ft.Container(
        bgcolor=CARD_BG,
        border=ft.border.all(1, BORDER_COLOR),
        border_radius=18,
        padding=16,
        visible=False,
        content=ft.Column(
            spacing=12,
            controls=[
                ft.Row(
                    spacing=12,
                    controls=[
                        play_btn,
                        ft.Column(
                            spacing=2,
                            expand=True,
                            controls=[
                                ft.Text("🎧 Output Studio Player", size=14, weight=ft.FontWeight.BOLD),
                                result_info,
                            ],
                        ),
                    ],
                ),
                ft.Row(
                    spacing=10,
                    controls=[
                        ft.ElevatedButton(
                            "MP3 သိမ်းမယ် / ပို့မယ်",
                            icon=ft.icons.AUDIO_FILE_ROUNDED,
                            bgcolor="#1E293B",
                            color="#FFFFFF",
                            expand=True,
                            on_click=share_mp3,
                        ),
                        ft.ElevatedButton(
                            "SRT သိမ်းမယ် / ပို့မယ်",
                            icon=ft.icons.SUBTITLES_ROUNDED,
                            bgcolor="#1E293B",
                            color=ACCENT_CYAN,
                            expand=True,
                            on_click=share_srt,
                        ),
                    ],
                ),
            ],
        ),
    )

    btn_text = ft.Text("🎙️ AUDIO + SRT စတင်ထုတ်မယ်", size=15, weight=ft.FontWeight.BOLD, color="#FFFFFF")

    def on_audio_state_changed(e):
        nonlocal is_playing, is_paused, playback_completed
        state = str(getattr(e, "data", "") or getattr(e, "state", "")).lower()
        if any(word in state for word in ("completed", "ended")):
            is_playing = False
            is_paused = False
            playback_completed = True
            play_btn.icon = ft.icons.PLAY_CIRCLE_FILL_ROUNDED
            page.update()
        elif "paused" in state:
            is_playing = False
            is_paused = True
            play_btn.icon = ft.icons.PLAY_CIRCLE_FILL_ROUNDED
            page.update()
        elif "playing" in state:
            is_playing = True
            is_paused = False
            playback_completed = False
            play_btn.icon = ft.icons.PAUSE_CIRCLE_FILLED_ROUNDED
            page.update()

    async def start_generation(e):
        nonlocal current_mp3_path, current_srt_path, is_playing, is_paused, playback_completed, audio_player
        script = normalize_script(script_input.value)
        if not script:
            status_label.value = "⚠️ ကျေးဇူးပြု၍ Script စာသား အရင်ထည့်ပေးပါ။"
            status_label.color = "#F59E0B"
            page.update()
            return

        selected_label = voice_dropdown.value
        voice_id = voice_map.get(selected_label, DEFAULT_VOICE_ID)
        rate = speed_to_rate(speed_slider.value)
        chunks = split_for_tts(script)

        generate_container.disabled = True
        btn_text.value = "⏳ အသံထုတ်နေပါသည်..."
        progress_bar.visible = True
        progress_bar.value = 0.05
        result_card.visible = False
        status_label.color = ACCENT_CYAN
        page.update()

        timestamp = int(time.time())
        raw_name = (output_name_input.value or "myanmar_tts").strip()
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", raw_name).strip(" .")
        safe_name = re.sub(r"\s+", "_", safe_name) or "myanmar_tts"
        safe_name = re.sub(r"\.(mp3|srt)$", "", safe_name, flags=re.IGNORECASE).strip(" .") or "myanmar_tts"
        dl_dir = "/storage/emulated/0/Download"
        target_dir = dl_dir if (os.path.exists(dl_dir) and os.access(dl_dir, os.W_OK)) else OUTPUT_DIR

        mp3_file = os.path.join(target_dir, f"{safe_name}_{timestamp}.mp3")
        srt_file = os.path.join(target_dir, f"{safe_name}_{timestamp}.srt")

        async def update_prog(idx, total):
            progress_bar.value = (idx + 1) / total
            status_label.value = f"🎙️ အသံထုတ်နေသည်... အပိုင်း ({idx + 1}/{total})"
            page.update()

        try:
            duration_ms = await synthesize_and_merge(
                chunks, voice_id, rate, mp3_file, update_prog
            )

            status_label.value = "📝 SRT Subtitle ဖိုင် ပြုလုပ်နေသည်..."
            page.update()

            sub_texts = split_for_srt(script)
            srt_entries = make_srt_entries(sub_texts, duration_ms)
            with open(srt_file, "w", encoding="utf-8-sig", newline="\n") as f:
                f.write(build_srt(srt_entries))

            current_mp3_path = mp3_file
            current_srt_path = srt_file

            # Release the old player and replace it after both files exist.
            if audio_player is not None:
                try:
                    await audio_player.release()
                except Exception:
                    pass
            detach_audio_player(audio_player)
            audio_player = fta.Audio(
                src=current_mp3_path,
                autoplay=False,
                on_state_change=on_audio_state_changed,
            )
            attach_audio_player(audio_player)

            play_btn.disabled = False
            play_btn.icon = ft.icons.PLAY_CIRCLE_FILL_ROUNDED
            is_playing = False
            is_paused = False
            playback_completed = False

            sec = round(duration_ms / 1000, 1)
            result_info.value = f"{selected_label} ({rate}) • {sec}s • {len(sub_texts)} SRT lines"
            result_card.visible = True
            status_label.value = (
                f"✅ ပြီးပါပြီ။ MP3 ({os.path.basename(mp3_file)}) နဲ့ "
                f"SRT ({os.path.basename(srt_file)}) ထုတ်ပြီးပါပြီ။ "
                "ဖိုင်အမည်ပြောင်းပြီး Downloads ထဲသိမ်းရန် အောက်က MP3 / SRT ခလုတ်ကိုနှိပ်ပါ။"
            )
            status_label.color = "#10B981"

        except Exception as ex:
            status_label.value = f"❌ Error: {str(ex)[:120]}"
            status_label.color = "#EF4444"
        finally:
            generate_container.disabled = False
            btn_text.value = "🎙️ AUDIO + SRT စတင်ထုတ်မယ်"
            progress_bar.visible = False
            page.update()

    generate_container = ft.Container(
        gradient=ft.LinearGradient(
            colors=["#7C3AED", "#4F46E5", "#06B6D4"],
        ),
        border_radius=16,
        padding=ft.padding.symmetric(vertical=16),
        alignment=ft.alignment.center,
        ink=True,
        on_click=start_generation,
        content=btn_text,
    )

    body = ft.ListView(
        expand=True,
        spacing=14,
        padding=ft.padding.only(left=16, right=16, top=14, bottom=30),
        controls=[
            voice_card,
            script_card,
            generate_container,
            progress_bar,
            ft.Container(alignment=ft.alignment.center, content=status_label),
            result_card,
        ],
    )

    page.add(
        ft.Column(
            expand=True,
            spacing=0,
            controls=[header_banner, body],
        )
    )

    await load_voices()

ft.app(target=main)
