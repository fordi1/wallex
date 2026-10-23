import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from pishro.config import load_config

cfg = load_config()
BUILD = Path("video/build")
VOICES_DIR = BUILD / "voice"
VOICES_DIR.mkdir(parents=True, exist_ok=True)

# 13 Narration Segments corresponding to the 13 scenes
SEGMENTS = [
    {
        "id": "s1",
        "start": 1.5,
        "text": "به ویدیوی معرفی پیشرو، ربات هوشمند معاملات الگوریتمی والکس خوش آمدید. این سامانه با ترکیب ریاضیات بازگشت پریمیوم، یادگیری ماشین کالیبره و هوش مصنوعی عامل، برای چالش استارکوچ توسعه یافته است."
    },
    {
        "id": "s2",
        "start": 13.5,
        "text": "صورت مسئله چالش: مدیریت پنج میلیون تومان سرمایه اولیه طی سه روز، بدون دخالت دست. هدف ما کسب بیشترین بازده با کمترین افت سرمایه و تصمیماتی کاملاً شفاف و توضیح‌پذیر است."
    },
    {
        "id": "s3",
        "start": 32.0,
        "text": "چرا این روش؟ در بازارهای رمزارز داخلی، به دلیل هیجانات آنی، قیمت‌ها از ارزش منصفانه جهانی فاصله می‌گیرند. ما ارزش منصفانه را از ضرب قیمت بایننس در تتر والکس به دست می‌آوریم و با ثبت سفارش‌های میکر، دارایی‌ها را در تخفیف عمیق خریده و در بازگشت به تعادل، با سود می‌فروشیم."
    },
    {
        "id": "s4",
        "start": 79.0,
        "text": "حل مشکل تاخیر: در یک پایپ‌لاین سریع دو ثانیه‌ای، داده‌های بیست و دو نماد والکس و قیمت‌های جهانی بایننس تجمیع می‌شوند. با استقرار روی سرورهای ابری لیارا در ایران، پینگ ارسال سفارشات به شصت میلی‌ثانیه کاهش یافته است."
    },
    {
        "id": "s5",
        "start": 105.0,
        "text": "هوش مصنوعی اول: مدل لایت‌جی‌بی‌ام کالیبره‌شده با رگرسیون ایزوتونیک، هفده متغیر ریزساختار بازار را می‌سنجد و احتمال سود خالص پس از کارمزد را محاسبه کرده و حجم ورود را هوشمندانه تنظیم می‌کند."
    },
    {
        "id": "s6",
        "start": 139.0,
        "text": "هوش مصنوعی دوم: ایجنت استراتژیست کلود اوپوس با دسترسی به یازده ابزار زنده، وضعیت بازار و اخبار را رصد کرده و سیاست ریسک را تعیین می‌کند؛ اما سقف‌های سخت در کد، مانع از خطای هوش مصنوعی می‌شوند."
    },
    {
        "id": "s7",
        "start": 177.0,
        "text": "مدیریت ریسک آهنین: افت شش درصدی حساب، بلافاصله مدارشکن را فعال کرده و تمام دارایی‌ها را نقد می‌کند. همچنین سقف ریسک هر معامله تنها هشت دهم درصد است و بیست درصد سرمایه همیشه نقد می‌ماند."
    },
    {
        "id": "s8",
        "start": 208.0,
        "text": "نتایج بک‌تست واک فوروارد: روی داده‌های ندیده، پیشرو به نرخ برد هشتاد و نه ممیز شش دهم درصد، فاکتور سود سه ممیز هفت و شارپ چهارده ممیز هفت دست یافته و افت آن تنها یک ممیز هفت دهم درصد بوده است."
    },
    {
        "id": "s9",
        "start": 237.0,
        "text": "در آزمون مونت‌کارلو با بیست هزار بار شبیه‌سازی چالش، بیش از نود و هفت درصد سناریوها با سود مثبت به پایان رسیدند و احتمال فعال شدن حد ضرر حساب نزدیک به صفر بود."
    },
    {
        "id": "s10",
        "start": 256.0,
        "text": "در تست‌های زنده والکس، ربات موفق به ثبت دقیق سفارشات میکر در نمادهای پامپ، آربیتروم و دوج با کمترین کارمزد و بدون هیچ‌گونه ناهماهنگی دفترداری شد."
    },
    {
        "id": "s11",
        "start": 276.0,
        "text": "تمام این فرآیند در داشبورد فین‌تک پیشرو به صورت شفاف نمایش داده می‌شود؛ از دلایل خرید و فروش گرفته تا منحنی لحظه‌ای سرمایه و کنترل‌های اضطراری."
    },
    {
        "id": "s12",
        "start": 288.0,
        "text": "تکرارپذیری کامل: کل پروژه با یک دستور روی کانتینرهای لیارا دیپلوی شده و به دیسک دائمی برای حفظ ژورنال معاملات مجهز است."
    },
    {
        "id": "s13",
        "start": 299.0,
        "text": "پیشرو، فراتر از یک الگوریتم ساده؛ سامانه‌ای هوشمند، مطمئن و پایدار برای پیروزی در چالش والکس."
    }
]

def synthesize_segment(seg: dict) -> Path:
    out_file = VOICES_DIR / f"{seg['id']}.mp3"
    if out_file.exists() and out_file.stat().st_size > 1000:
        return out_file
    
    print(f"Synthesizing {seg['id']}...")
    req = urllib.request.Request(
        f"{cfg.secrets.llm_base_url}/audio/speech",
        data=json.dumps({
            "model": "openai/tts-1",
            "input": seg["text"],
            "voice": "onyx",
            "speed": 1.05
        }).encode(),
        headers={
            "Authorization": f"Bearer {cfg.secrets.llm_api_key}",
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out_file.write_bytes(resp.read())
    return out_file

def build_voice_track(total_duration: float = 306.0) -> Path:
    voice_track = BUILD / "voice_track.wav"
    filter_inputs = []
    filter_parts = []
    
    for i, seg in enumerate(SEGMENTS):
        mp3_path = synthesize_segment(seg)
        filter_inputs.extend(["-i", str(mp3_path)])
        # Delay audio in milliseconds
        delay_ms = int(seg["start"] * 1000)
        filter_parts.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[a{i}]")
    
    amix_inputs = "".join(f"[a{i}]" for i in range(len(SEGMENTS)))
    filter_graph = f"{';'.join(filter_parts)};{amix_inputs}amix=inputs={len(SEGMENTS)}:normalize=0:dropout_transition=0,apad=whole_dur={int(total_duration)}[outa]"
    
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *filter_inputs,
        "-filter_complex", filter_graph,
        "-map", "[outa]",
        "-c:a", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        str(voice_track)
    ]
    subprocess.run(cmd, check=True)
    print("Voice track built successfully:", voice_track)
    return voice_track

if __name__ == "__main__":
    build_voice_track()
