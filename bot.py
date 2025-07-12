import os
import re
import asyncio
import mimetypes
import shutil
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
from pymongo import MongoClient

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

MONGO_URI = os.getenv("MONGO_URI")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip()]
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/downloads")
ARIA2C_PATH = os.getenv("ARIA2C_PATH", "/usr/bin/aria2c")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))

# === DATABASE ===
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["torrentbot"]
admin_collection = db["admins"]

existing = admin_collection.find_one({"_id": "admins"})
if not existing:
    admin_collection.insert_one({"_id": "admins", "users": ADMINS})
else:
    all_admins = list(set(existing["users"] + ADMINS))
    admin_collection.update_one(
        {"_id": "admins"},
        {"$set": {"users": all_admins}}
    )

def is_admin(user_id: int) -> bool:
    data = admin_collection.find_one({"_id": "admins"})
    return user_id in data["users"]

# Track running downloads
active_downloads = {}

app = Client(
    "torrent_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

def is_magnet_link(text: str) -> bool:
    return text.startswith("magnet:?")

def is_torrent_url(text: str) -> bool:
    return re.match(r'^https?://.*\.torrent($|\?)', text)

def make_bar(percent, color="blue"):
    blocks = 10
    filled_count = int((percent / 100) * blocks)
    empty_count = blocks - filled_count

    emojis = {
        "blue": ("🔵", "⚪"),
        "green": ("🟢", "⚪"),
        "star": ("⭐", "✩"),
        "fire": ("🔥", "·"),
    }

    filled_emoji, empty_emoji = emojis.get(color, ("⬛", "⬜"))
    filled = filled_emoji * filled_count
    empty = empty_emoji * empty_count
    return filled + empty

def find_largest_file(path):
    largest = None
    max_size = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            fpath = os.path.join(root, f)
            if not os.path.isfile(fpath):
                continue
            mime, _ = mimetypes.guess_type(fpath)
            if mime and any(mime.startswith(x) for x in ["video", "audio", "image", "application"]):
                size = os.path.getsize(fpath)
                if size > max_size and size <= MAX_FILE_SIZE_MB * 1024 * 1024:
                    largest = fpath
                    max_size = size
    return largest

async def run_aria2c(link_or_path, user_id, progress_msg):
    proc = await asyncio.create_subprocess_exec(
        ARIA2C_PATH,
        "--bt-stop-timeout=10",
        "-d", DOWNLOAD_PATH,
        link_or_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    active_downloads[user_id] = proc

    last_progress = ""
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            text_line = line.decode("utf-8").strip()
            match = re.search(r'(\d+)%', text_line)
            if match:
                percent = match.group(1)
                if percent != last_progress:
                    last_progress = percent
                    bar = make_bar(int(percent), color="blue")
                    try:
                        await progress_msg.edit(
                            f"Downloading 🔥❄️\n{bar}  {percent}%"
                        )
                    except Exception:
                        pass

        await asyncio.wait_for(proc.wait(), timeout=300)

    except asyncio.TimeoutError:
        await progress_msg.edit("⚠️ aria2c timed out. Trying to proceed anyway.")
        proc.kill()

    finally:
        active_downloads.pop(user_id, None)

    file_path = find_largest_file(DOWNLOAD_PATH)
    if not file_path:
        await progress_msg.edit("❌ No suitable media file found.")
        return

    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    # Fake upload progress bar
    for p in range(0, 101, 10):
        bar = make_bar(p, color="green")
        await progress_msg.edit(f"Uploading 💧\n{bar}  {p}%")
        await asyncio.sleep(0.2)

    await progress_msg.delete()

    await app.send_document(
        chat_id=user_id,
        document=file_path,
        caption=f"✅ Download complete: `{os.path.basename(file_path)}`"
    )

    os.remove(file_path)
    shutil.rmtree(DOWNLOAD_PATH)
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)

@app.on_message(filters.command("start"))
async def start(client, message: Message):
    await message.reply_text(
        "**👋 Hello!**\n\n"
        "I can download torrents for you. Send me a magnet link or a .torrent file.\n\n"
        "Use /help for commands."
    )

@app.on_message(filters.command("help"))
async def help_cmd(client, message: Message):
    await message.reply_text(
        "**ℹ️ Help Menu**\n\n"
        "/start - Greet the bot\n"
        "/help - Show help\n"
        "/in <magnet/torrent-url> - Start a torrent download\n"
        "/cancel - Cancel your current download\n"
        "Or send .torrent file directly.\n"
        "Only admins can trigger downloads."
    )

@app.on_message(filters.command("cancel"))
async def cancel_cmd(client, message: Message):
    user_id = message.from_user.id
    proc = active_downloads.get(user_id)

    if proc:
        proc.kill()
        active_downloads.pop(user_id, None)
        await message.reply_text("✅ Download cancelled.")
    else:
        await message.reply_text("ℹ️ No active download to cancel.")

@app.on_message(filters.command("in"))
async def in_cmd(client, message: Message):
    user_id = message.from_user.id

    if not is_admin(user_id):
        await message.reply_text("🚫 You are not authorized to download torrents.")
        return

    if len(message.command) < 2:
        await message.reply_text("Usage:\n`/in <magnet link or torrent URL>`")
        return

    link = message.command[1]

    if not (is_magnet_link(link) or is_torrent_url(link)):
        await message.reply_text("❌ Not a valid magnet or torrent URL.")
        return

    progress_msg = await message.reply_text("Downloading 🔥❄️")
    await run_aria2c(link, user_id, progress_msg)

@app.on_message(filters.document)
async def handle_torrent_file(client, message: Message):
    user_id = message.from_user.id

    if not is_admin(user_id):
        await message.reply_text("🚫 You are not authorized to download torrents.")
        return

    file_path = await message.download(file_name="temp.torrent")
    progress_msg = await message.reply_text("Downloading 🔥❄️")
    await run_aria2c(file_path, user_id, progress_msg)
    os.remove(file_path)

if __name__ == "__main__":
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
    app.run()
