import os
import subprocess
import re
import asyncio
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
        "/help - Show this help\n"
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

    # Prepare aria2c process
    proc = await asyncio.create_subprocess_exec(
        ARIA2C_PATH,
        "--bt-stop-timeout=10",
        "-d", DOWNLOAD_PATH,
        link,
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
                    try:
                        await progress_msg.edit(
                            f"Downloading 🔥❄️\nProgress: {percent}%"
                        )
                    except Exception:
                        pass

        await asyncio.wait_for(proc.wait(), timeout=300)

    except asyncio.TimeoutError:
        await progress_msg.edit("⚠️ aria2c timed out. Trying to proceed anyway.")
        proc.kill()

    finally:
        active_downloads.pop(user_id, None)

    files = os.listdir(DOWNLOAD_PATH)
    if not files:
        await progress_msg.edit("❌ Download failed or no files found.")
        return

    for fname in files:
        fpath = os.path.join(DOWNLOAD_PATH, fname)
        size_mb = os.path.getsize(fpath) / (1024 * 1024)

        if size_mb > MAX_FILE_SIZE_MB:
            await progress_msg.edit(
                f"⚠️ File `{fname}` is too big ({int(size_mb)}MB). Skipped."
            )
            os.remove(fpath)
            continue

        await progress_msg.edit("Uploading 💧")
        await message.reply_document(
            fpath,
            caption=f"✅ Download complete: `{fname}`"
        )
        os.remove(fpath)

    await progress_msg.delete()

@app.on_message(filters.document)
async def handle_torrent_file(client, message: Message):
    user_id = message.from_user.id

    if not is_admin(user_id):
        await message.reply_text("🚫 You are not authorized to download torrents.")
        return

    file_path = await message.download(file_name="temp.torrent")

    progress_msg = await message.reply_text("Downloading 🔥❄️")

    proc = await asyncio.create_subprocess_exec(
        ARIA2C_PATH,
        "--bt-stop-timeout=10",
        "-d", DOWNLOAD_PATH,
        file_path,
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
                    try:
                        await progress_msg.edit(
                            f"Downloading 🔥❄️\nProgress: {percent}%"
                        )
                    except Exception:
                        pass

        await asyncio.wait_for(proc.wait(), timeout=300)

    except asyncio.TimeoutError:
        await progress_msg.edit("⚠️ aria2c timed out. Trying to proceed anyway.")
        proc.kill()

    finally:
        active_downloads.pop(user_id, None)

    os.remove(file_path)

    files = os.listdir(DOWNLOAD_PATH)
    if not files:
        await progress_msg.edit("❌ Download failed or no files found.")
        return

    for fname in files:
        fpath = os.path.join(DOWNLOAD_PATH, fname)
        size_mb = os.path.getsize(fpath) / (1024 * 1024)

        if size_mb > MAX_FILE_SIZE_MB:
            await progress_msg.edit(
                f"⚠️ File `{fname}` is too big ({int(size_mb)}MB). Skipped."
            )
            os.remove(fpath)
            continue

        await progress_msg.edit("Uploading 💧")
        await message.reply_document(
            fpath,
            caption=f"✅ Download complete: `{fname}`"
        )
        os.remove(fpath)

    await progress_msg.delete()

if __name__ == "__main__":
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
    app.run()
