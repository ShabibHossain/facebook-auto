import requests
import subprocess
import os
import random
import html
import json
from collections import Counter

# ─── CONFIG FROM ENV ──────────────────────────────────────────────────────────
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
FB_PAGE_ID      = os.environ["FB_PAGE_ID"]
FB_ACCESS_TOKEN = os.environ["FB_ACCESS_TOKEN"]
GROQ_API_KEY    = os.environ["GROQ_API_KEY"]

DOWNLOAD_PATH       = "animal_bot_tmp.mp4"
HISTORY_FILE        = "posted_history.json"

MAX_PER_CHANNEL     = 3
CHANNEL_RESET_EVERY = 10
CANDIDATES          = 10   # pull this many, Groq picks best

ANIMAL_KEYWORDS = [
    "cute animals", "animal rescue", "funny pets",
    "baby animals", "dog cat funny", "wildlife animals",
    "pet compilation", "adorable animals"
]
# ──────────────────────────────────────────────────────────────────────────────


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def already_posted(url, history):
    return any(entry["url"] == url for entry in history)


def get_channel_counts(history):
    total = len(history)
    window_start = (total // CHANNEL_RESET_EVERY) * CHANNEL_RESET_EVERY
    window = history[window_start:]
    return Counter(entry.get("channel", "") for entry in window)


def channel_allowed(channel, history):
    counts = get_channel_counts(history)
    return counts.get(channel, 0) < MAX_PER_CHANNEL


def add_to_history(history, video, fb_video_id, caption):
    history.append({
        "url": video["url"],
        "title": video["title"],
        "channel": video.get("channel", ""),
        "source": video["source"],
        "fb_video_id": fb_video_id,
        "caption": caption,
        "post_number": len(history) + 1,
    })


def fetch_youtube_candidates(history, count=5):
    candidates = []
    random.shuffle(ANIMAL_KEYWORDS)
    for keyword in ANIMAL_KEYWORDS:
        if len(candidates) >= count:
            break
        resp = requests.get("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "videoCategoryId": "15",
            "videoDuration": "short",
            "videoDefinition": "high",
            "order": "viewCount",
            "maxResults": 10,
            "key": YOUTUBE_API_KEY,
            "relevanceLanguage": "en",
        }, timeout=10)
        items = resp.json().get("items", [])
        for item in items:
            if len(candidates) >= count:
                break
            url = f"https://www.youtube.com/watch?v={item['id']['videoId']}"
            channel = html.unescape(item["snippet"]["channelTitle"])
            if already_posted(url, history):
                continue
            if not channel_allowed(channel, history):
                continue
            candidates.append({
                "source": "youtube",
                "url": url,
                "title": html.unescape(item["snippet"]["title"]),
                "channel": channel,
                "description": html.unescape(item["snippet"].get("description", "")),
            })
    return candidates


def fetch_dailymotion_candidates(history, count=5):
    candidates = []
    resp = requests.get("https://api.dailymotion.com/videos", params={
        "search": "cute animals rescue pets",
        "sort": "trending",
        "limit": 20,
        "fields": "id,title,owner.screenname",
        "language": "en",
    }, timeout=15)
    items = resp.json().get("list", [])
    for item in items:
        if len(candidates) >= count:
            break
        url = f"https://www.dailymotion.com/video/{item['id']}"
        channel = item.get("owner.screenname", "the original creator")
        if already_posted(url, history):
            continue
        if not channel_allowed(channel, history):
            continue
        candidates.append({
            "source": "dailymotion",
            "url": url,
            "title": html.unescape(item["title"]),
            "channel": channel,
            "description": "",
        })
    return candidates


def groq_call(prompt, max_tokens=100):
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "qwen/qwen3.8-27b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        },
        timeout=20,
    )
    return r.json()["choices"][0]["message"]["content"].strip()


def groq_pick_best(candidates):
    """Give all candidates to Groq, let it pick the best one."""
    lines = []
    for i, v in enumerate(candidates):
        lines.append(f"{i+1}. Title: {v['title']} | Channel: {v['channel']} | Source: {v['source']}")

    prompt = (
        "You are a social media manager for a cute animal Facebook page.\n"
        "Here are animal video candidates:\n\n"
        + "\n".join(lines) +
        "\n\nPick the single best video for maximum engagement on Facebook. "
        "Consider cuteness, emotional appeal, shareability, and variety. "
        "Reply with ONLY the number (e.g. 3). No explanation."
    )
    answer = groq_call(prompt, max_tokens=5)
    # extract number
    for ch in answer:
        if ch.isdigit():
            idx = int(ch) - 1
            if 0 <= idx < len(candidates):
                print(f"[Groq pick] Chose #{idx+1}: {candidates[idx]['title']}")
                return candidates[idx]
    # fallback
    return candidates[0]


def groq_caption(video):
    prompt = (
        "Write a fun, engaging English-only Facebook caption for an animal video.\n"
        f"Video title: {video['title']}\n"
        f"Channel: {video.get('channel', '')}\n\n"
        "Rules:\n"
        "- English only\n"
        "- 1-2 sentences max\n"
        "- Fun and warm tone\n"
        "- Include 2-3 relevant emojis\n"
        "- No hashtags\n"
        "- Do not mention the channel name\n"
        "- Just the caption text, nothing else"
    )
    return groq_call(prompt, max_tokens=80)


def download_video(url):
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]",
        "-o", DOWNLOAD_PATH,
        "--merge-output-format", "mp4",
        "--no-update",
        url
    ]
    ret = subprocess.run(cmd)
    if ret.returncode != 0:
        print("[-] yt-dlp download failed")
        return False
    return os.path.exists(DOWNLOAD_PATH)


def upload_to_fb_reels(video_path, caption):
    init_url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/video_reels"

    r1 = requests.post(init_url, data={
        "upload_phase": "start",
        "access_token": FB_ACCESS_TOKEN,
    })
    data = r1.json()
    if "video_id" not in data:
        print(f"[-] FB init failed: {data}")
        return None
    video_id   = data["video_id"]
    upload_url = data["upload_url"]
    print(f"[+] FB upload session started, video_id={video_id}")

    file_size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        r2 = requests.post(upload_url, headers={
            "Authorization": f"OAuth {FB_ACCESS_TOKEN}",
            "offset": "0",
            "file_size": str(file_size),
        }, data=f)
        if r2.status_code != 200:
            print(f"[-] Upload failed: {r2.text}")
            return None
    print("[+] Video uploaded to FB")

    r3 = requests.post(init_url, data={
        "video_id": video_id,
        "upload_phase": "finish",
        "video_state": "PUBLISHED",
        "description": caption,
        "access_token": FB_ACCESS_TOKEN,
    })
    result = r3.json()
    if result.get("success"):
        print(f"[+] Reel published!")
        return video_id
    else:
        print(f"[-] Publish failed: {result}")
        return None


def cleanup():
    if os.path.exists(DOWNLOAD_PATH):
        os.remove(DOWNLOAD_PATH)
        print("[+] Temp file cleaned up")


def run():
    print("=== Animal Bot Starting ===")
    history = load_history()
    print(f"[+] {len(history)} videos in history")

    # Gather candidates
    print(f"[+] Fetching {CANDIDATES} candidates...")
    half = CANDIDATES // 2
    yt_candidates = fetch_youtube_candidates(history, count=half)
    dm_candidates = fetch_dailymotion_candidates(history, count=CANDIDATES - half)
    all_candidates = yt_candidates + dm_candidates
    random.shuffle(all_candidates)

    if not all_candidates:
        print("[-] No candidates found. Exiting.")
        return

    print(f"[+] Got {len(all_candidates)} candidates:")
    for i, v in enumerate(all_candidates):
        print(f"  {i+1}. [{v['source']}] {v['title']}")

    # Groq picks best
    video = groq_pick_best(all_candidates)

    # Generate caption
    print(f"[+] Generating caption...")
    caption_text = groq_caption(video)
    channel = video.get("channel", "the original creator")
    caption = f"{caption_text}\n\nAll credits to @{channel} for this video 🐾\n\n#animals #pets #animalrescue #cute #aww"
    print(f"[+] Caption: {caption}")

    # Download
    print(f"[+] Downloading: {video['url']}")
    if not download_video(video["url"]):
        print("[-] Download failed. Exiting.")
        return

    # Upload
    fb_video_id = upload_to_fb_reels(DOWNLOAD_PATH, caption)
    cleanup()

    if fb_video_id:
        add_to_history(history, video, fb_video_id, caption)
        save_history(history)
        print(f"[+] Saved to history. Total: {len(history)}")
        print("=== Done! Reel posted. ===")
    else:
        print("=== Failed to post. ===")


if __name__ == "__main__":
    run()
