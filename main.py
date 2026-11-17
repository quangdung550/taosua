
import os
import re
import json
import time
import shutil
import asyncio
import sqlite3
import datetime
import logging

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()


# ==================================================
# CẤU HÌNH HỆ THỐNG & TOKEN
# ==================================================

raw_token = os.getenv("DISCORD_TOKEN", "").strip()
# Loại bỏ dấu ngoặc kép hoặc ngoặc đơn nếu người dùng lỡ dán vào Railway
TOKEN = raw_token.strip('"').strip("'").strip()

OWNER_ID = 1535146655999983698

# Danh sách ID được miễn trừ (không bao giờ tự động gửi nội quy khi vào voice)
EXCLUDED_USER_IDS = {
    1559538651979972779,
    1264245404287897711,
    1426024743282147358,
}

def parse_int_env(key: str, default: int = None) -> int | None:
    val = os.getenv(key, "").strip()
    if val and val.isdigit():
        return int(val)
    return default

# Hỗ trợ cấu hình trước khi deploy qua biến môi trường Railway
ENV_DEFAULT_VOICE = parse_int_env("DEFAULT_VOICE_CHANNEL_ID") or parse_int_env("VOICE_CHANNEL_ID")
ENV_DEFAULT_RULE = parse_int_env("DEFAULT_RULE_CHANNEL_ID") or parse_int_env("RULE_CHANNEL_ID")
ENV_DEFAULT_GUILD = parse_int_env("DEFAULT_GUILD_ID") or parse_int_env("GUILD_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("voice_lock_bot")

# Tự động nạp thư viện Opus nếu môi trường Linux/Railway yêu cầu
if not discord.opus.is_loaded():
    for opus_lib in ["libopus.so.0", "libopus.so", "opus", "libopus-0.x86_64.so"]:
        try:
            discord.opus.load_opus(opus_lib)
            logger.info("Đã nạp thư viện âm thanh Opus: %s", opus_lib)
            break
        except Exception:
            pass


# ==================================================
# HỆ THỐNG LƯU TRỮ ĐA TẦNG: FILE CONFIG.JSON (TẦNG 1 TĨNH)
# ==================================================

CONFIG_FILE = os.path.join(os.getcwd(), "config.json")

def load_json_config() -> dict:
    """Đọc cấu hình tĩnh từ file config.json (được bảo toàn qua mọi lần deploy)."""
    default_structure = {
        "_ghi_chu": "File này lưu trữ cấu hình TRƯỚC VÀ SAU KHI DEPLOY. Bạn có thể điền sẵn ID phòng vào pre_setup trước khi deploy để bot tự vào room ngay khi online mà không cần gõ lại lệnh!",
        "voice_targets": {},
        "rule_channels": {},
        "pre_setup": {
            "default_voice_channel_id": None,
            "default_rule_channel_id": None,
            "default_guild_id": None,
            "excluded_user_ids": [1559538651979972779, 1264245404287897711, 1426024743282147358]
        },
        "settings": {
            "self_deaf": True,
            "keepalive_silence": True,
            "auto_reconnect": True,
            "reconnect_interval_seconds": 15
        }
    }
    if not os.path.exists(CONFIG_FILE):
        save_json_config(default_structure)
        return default_structure

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default_structure
            for k, v in default_structure.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception:
        logger.exception("Không thể đọc file config.json, dùng cấu hình mặc định.")
        return default_structure

def save_json_config(data: dict):
    """Ghi cấu hình an toàn vào file config.json bằng kỹ thuật atomic write."""
    try:
        temp_file = CONFIG_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if os.path.exists(temp_file):
            shutil.move(temp_file, CONFIG_FILE)
    except Exception:
        logger.exception("Không thể ghi file config.json.")



# ==================================================
# HỆ THỐNG LƯU TRỮ 2 TẦNG (TẦNG 2: RAILWAY VOLUME SQLITE)
# ==================================================

def get_database_path() -> str:
    """Xác định đường dẫn lưu trữ database bền vững trên đĩa Railway Volume."""
    # 1. Đường dẫn cụ thể do người dùng cấu hình qua biến môi trường
    if os.getenv("DB_PATH"):
        return os.getenv("DB_PATH")

    # 2. Đường dẫn thư mục Volume Mount từ Railway
    vol_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if vol_path:
        return os.path.join(vol_path, "voice_lock_volume.sqlite3")

    # 3. Biến DATA_DIR cấu hình thư mục lưu trữ
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "voice_lock_volume.sqlite3")

    # 4. Thư mục mặc định của Railway Volume (/app/data)
    if os.path.exists("/app/data"):
        return "/app/data/voice_lock_volume.sqlite3"

    # 5. Môi trường Local (chạy thử trên máy tính)
    local_data = os.path.join(os.getcwd(), "data")
    return os.path.join(local_data, "voice_lock_volume.sqlite3")


DB_FILE = get_database_path()
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)

# Tự động di chuyển dữ liệu cũ nếu phát hiện database cũ ở thư mục gốc
old_db = os.path.join(os.getcwd(), "voice_lock.sqlite3")
if os.path.exists(old_db) and old_db != DB_FILE and not os.path.exists(DB_FILE):
    try:
        shutil.copy2(old_db, DB_FILE)
        logger.info("Đã tự động chuyển dữ liệu cũ từ %s sang %s", old_db, DB_FILE)
    except Exception:
        logger.exception("Không thể sao chép dữ liệu cũ sang Volume.")

# Khởi tạo kết nối SQLite với chế độ WAL cho tốc độ cao và an toàn trên Volume
db = sqlite3.connect(DB_FILE, check_same_thread=False)
try:
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA synchronous=NORMAL;")
except Exception:
    pass

# Bảng 1: Lưu phòng voice bị khóa (Voice Lock 24/7)
db.execute("""
CREATE TABLE IF NOT EXISTS voice_lock (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    configured_by INTEGER,
    updated_at TIMESTAMP,
    enabled INTEGER NOT NULL DEFAULT 1
)
""")

# Bảng 2: Lưu kênh văn bản nhận thông báo nội quy
db.execute("""
CREATE TABLE IF NOT EXISTS voice_rule (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    user_id INTEGER,
    user_name TEXT,
    updated_at TIMESTAMP,
    enabled INTEGER NOT NULL DEFAULT 1
)
""")

# Bảng 3: Lưu trữ lịch sử toàn bộ các lệnh từng được dùng trong server
db.execute("""
CREATE TABLE IF NOT EXISTS command_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT,
    command_name TEXT NOT NULL,
    args TEXT,
    executed_at TIMESTAMP NOT NULL,
    success INTEGER DEFAULT 1
)
""")

db.commit()

# Đảm bảo các cột mới tồn tại nếu database cũ được nâng cấp
for col_def in [
    ("voice_lock", "configured_by INTEGER"),
    ("voice_lock", "updated_at TIMESTAMP"),
    ("voice_rule", "user_id INTEGER"),
    ("voice_rule", "user_name TEXT"),
    ("voice_rule", "updated_at TIMESTAMP"),
]:
    try:
        db.execute(f"ALTER TABLE {col_def[0]} ADD COLUMN {col_def[1]}")
        db.commit()
    except sqlite3.OperationalError:
        pass


def save_lock(guild_id: int, channel_id: int, configured_by: int = None):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # 1. Lưu vào SQLite
    try:
        db.execute(
            """
            INSERT INTO voice_lock (guild_id, channel_id, configured_by, updated_at, enabled)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                channel_id = excluded.channel_id,
                configured_by = excluded.configured_by,
                updated_at = excluded.updated_at,
                enabled = 1
            """,
            (guild_id, channel_id, configured_by, now),
        )
        db.commit()
    except Exception:
        logger.exception("Không thể lưu voice_lock vào SQLite.")

    # 2. Đồng bộ lập tức vào file config.json tĩnh (không mất khi deploy)
    try:
        cfg = load_json_config()
        if "voice_targets" not in cfg:
            cfg["voice_targets"] = {}
        cfg["voice_targets"][str(guild_id)] = channel_id
        save_json_config(cfg)
    except Exception:
        logger.exception("Không thể đồng bộ voice_lock vào config.json.")


def remove_lock(guild_id: int):
    # 1. Xóa khỏi SQLite
    try:
        db.execute("DELETE FROM voice_lock WHERE guild_id = ?", (guild_id,))
        db.commit()
    except Exception:
        logger.exception("Không thể xóa voice_lock khỏi SQLite.")

    # 2. Đồng bộ xóa khỏi file config.json
    try:
        cfg = load_json_config()
        if "voice_targets" in cfg:
            cfg["voice_targets"].pop(str(guild_id), None)
            save_json_config(cfg)
    except Exception:
        logger.exception("Không thể xóa voice_lock khỏi config.json.")


def get_locks():
    try:
        rows = db.execute(
            """
            SELECT guild_id, channel_id
            FROM voice_lock
            WHERE enabled = 1
            """
        ).fetchall()
        return {guild_id: channel_id for guild_id, channel_id in rows}
    except Exception:
        logger.exception("Không thể đọc locks từ SQLite.")
        return {}


def save_rule_channel(guild_id: int, channel_id: int, user_id: int = None, user_name: str = None):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # 1. Lưu vào SQLite
    try:
        db.execute(
            """
            INSERT INTO voice_rule (guild_id, channel_id, user_id, user_name, updated_at, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                channel_id = excluded.channel_id,
                user_id = excluded.user_id,
                user_name = excluded.user_name,
                updated_at = excluded.updated_at,
                enabled = 1
            """,
            (guild_id, channel_id, user_id, user_name, now),
        )
        db.commit()
    except Exception:
        logger.exception("Không thể lưu voice_rule vào SQLite.")

    # 2. Đồng bộ lập tức vào file config.json tĩnh (không mất khi deploy)
    try:
        cfg = load_json_config()
        if "rule_channels" not in cfg:
            cfg["rule_channels"] = {}
        cfg["rule_channels"][str(guild_id)] = {
            "channel_id": channel_id,
            "user_id": user_id,
            "user_name": user_name,
            "updated_at": now,
        }
        save_json_config(cfg)
    except Exception:
        logger.exception("Không thể đồng bộ voice_rule vào config.json.")


def remove_rule_channel(guild_id: int):
    # 1. Xóa khỏi SQLite
    try:
        db.execute("DELETE FROM voice_rule WHERE guild_id = ?", (guild_id,))
        db.commit()
    except Exception:
        logger.exception("Không thể xóa voice_rule khỏi SQLite.")

    # 2. Đồng bộ xóa khỏi config.json
    try:
        cfg = load_json_config()
        if "rule_channels" in cfg:
            cfg["rule_channels"].pop(str(guild_id), None)
            save_json_config(cfg)
    except Exception:
        logger.exception("Không thể xóa voice_rule khỏi config.json.")


def get_rule_channels():
    rows = db.execute(
        """
        SELECT guild_id, channel_id, user_id, user_name, updated_at
        FROM voice_rule
        WHERE enabled = 1
        """
    ).fetchall()

    result = {}
    for row in rows:
        guild_id = row[0]
        channel_id = row[1]
        user_id = row[2] if len(row) > 2 else None
        user_name = row[3] if len(row) > 3 else None
        updated_at = row[4] if len(row) > 4 else None
        result[guild_id] = {
            "channel_id": channel_id,
            "user_id": user_id,
            "user_name": user_name,
            "updated_at": updated_at,
        }
    return result


def log_command(
    guild_id: int,
    channel_id: int,
    user_id: int,
    user_name: str,
    command_name: str,
    args: str = "",
    success: bool = True,
):
    """Ghi lại lịch sử lệnh đã chạy vào Tầng 2 (Volume SQLite) bền vững."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        db.execute(
            """
            INSERT INTO command_history (
                guild_id, channel_id, user_id, user_name, command_name, args, executed_at, success
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, user_id, user_name, command_name, args, now, 1 if success else 0),
        )
        db.commit()
    except Exception:
        logger.exception("Không thể ghi log lệnh vào SQLite.")


def get_recent_commands(guild_id: int = None, limit: int = 10):
    """Lấy danh sách các lệnh gần nhất từ Tầng 2."""
    try:
        if guild_id:
            rows = db.execute(
                """
                SELECT command_name, user_name, user_id, channel_id, args, executed_at, success
                FROM command_history
                WHERE guild_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT command_name, user_name, user_id, channel_id, args, executed_at, success
                FROM command_history
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows
    except Exception:
        logger.exception("Không thể đọc lịch sử lệnh.")
        return []


# ==================================================
# NỘI DUNG VÀ EMBED NỘI QUY XINH XẮN
# ==================================================

RULE_CONTENT = """> 🕊️ *Hãy thể hiện mình tử tế .*
>
> 💙 **Th.Thu** 💙
> Ở đây có Táo 🍏.
>
> ----------------------🌤️🌧️
> ----------------------
>
> *Giây phút -*
>
> *Ai rồi cũng sẽ phá vỡ Quy tắc vì đó -*"""


def make_rule_embed(guild: discord.Guild = None) -> discord.Embed:
    """Tạo bảng nội quy dạng Embed xinh xắn, chuẩn theo đúng ảnh mẫu."""
    desc = (
        "> 🕊️ *Hãy thể hiện mình tử tế .*\n"
        ">\n"
        "> 💙 **Th.Thu** 💙\n"
        "> Ở đây có Táo 🍏.\n"
        ">\n"
        "> ----------------------🌤️🌧️\n"
        "> ----------------------\n"
        ">\n"
        "> *Giây phút -*\n"
        ">\n"
        "> *Ai rồi cũng sẽ phá vỡ Quy tắc vì đó -*"
    )
    embed = discord.Embed(
        title="🌸 ‧ Lời Nhắn Nho Nhỏ ‧ 🌤️🌧️",
        description=desc,
        color=0xFFA6C9,  # Màu hồng phấn Pastel
        timestamp=discord.utils.utcnow(),
    )
    if guild and guild.icon:
        embed.set_author(name=f"{guild.name} • Nhà Của Táo 🍏", icon_url=guild.icon.url)
    else:
        embed.set_author(name="Nhà Của Táo 🍏")

    embed.set_footer(text="✨ Chúc bạn có những phút giây thật vui vẻ nhen ~ 🌷")
    return embed


# ==================================================
# INTENTS
# ==================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True


# ==================================================
# NGUỒN ÂM THANH KEEPALIVE 24/7 (CHỐNG RỚT KẾT NỐI UDP & RTC)
# ==================================================

class SilenceAudioSource(discord.AudioSource):
    """
    Nguồn âm thanh tĩnh Opus chuẩn RFC 6716 (Discord Voice).
    - Phát gói tin Opus tĩnh tiêu chuẩn 3 bytes: b"\xf8\xff\xfe" (20ms silence frame).
    - is_opus() = True giúp Discord.py truyền trực tiếp gói tin qua UDP socket
      mà KHÔNG cần nạp thư viện libopus bên ngoài và KHÔNG tốn CPU để encode âm thanh.
    - Giữ liên tục luồng gói tin UDP qua NAT firewall của Railway/Docker/VPS,
      chống timeout cổng mạng và chống Discord Voice RTC ngắt kết nối vì lý do idle.
    """
    SILENCE_PACKET = b"\xf8\xff\xfe"

    def read(self) -> bytes:
        return self.SILENCE_PACKET

    def is_opus(self) -> bool:
        return True


# ==================================================
# BOT CLASS (QUẢN LÝ LƯU TRỮ ĐA TẦNG & TREO ROOM 24/7 SIÊU ỔN ĐỊNH)
# ==================================================

class VoiceLockBot(commands.Bot):

    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

        # TẦNG 1: BỘ NHỚ RAM (Cache tức thì cho tốc độ siêu nhanh)
        self.voice_targets = {}
        self.rule_channels = {}
        self.last_rule_send = {}
        self.reconnect_locks = {}
        self.voice_connected_at = {}
        self._recovery_tasks = {}
        self.storage_path = DB_FILE

        # Cài đặt kết nối voice 24/7
        self.self_deaf_enabled = True
        self.keepalive_silence_enabled = False

        # Nạp dữ liệu đa tầng ngay khi khởi tạo
        self.hydrate_from_storage()

    def get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        """Đảm bảo chỉ có duy nhất 1 tiến trình kết nối/xử lý voice cho mỗi server tại 1 thời điểm."""
        if guild_id not in self.reconnect_locks:
            self.reconnect_locks[guild_id] = asyncio.Lock()
        return self.reconnect_locks[guild_id]

    def hydrate_from_storage(self):
        """
        Nạp cấu hình ĐA TẦNG:
        1. Đọc từ file config.json (được lưu vĩnh viễn trong code/repo GitHub, không mất khi deploy)
        2. Đọc từ Railway Volume SQLite (nếu có volume)
        3. Hợp nhất cả hai tầng và đồng bộ ngược lại để dữ liệu luôn toàn vẹn
        """
        cfg = load_json_config()

        # 1. Lấy dữ liệu từ config.json
        json_locks = {}
        for k, v in cfg.get("voice_targets", {}).items():
            if str(k).isdigit() and isinstance(v, int):
                json_locks[int(k)] = v

        json_rules = {}
        for k, v in cfg.get("rule_channels", {}).items():
            if str(k).isdigit() and isinstance(v, dict):
                json_rules[int(k)] = v

        # 2. Lấy dữ liệu từ SQLite
        sqlite_locks = get_locks()
        sqlite_rules = get_rule_channels()

        # 3. Hợp nhất: SQLite và JSON bổ trợ lẫn nhau
        self.voice_targets = {**json_locks, **sqlite_locks}
        self.rule_channels = {**json_rules, **sqlite_rules}

        # Đọc settings nếu có
        settings = cfg.get("settings", {})
        self.self_deaf_enabled = settings.get("self_deaf", True)
        self.keepalive_silence_enabled = settings.get("keepalive_silence", False)

        logger.info(
            "Đã phục hồi cấu hình đa tầng: %s phòng voice khóa, %s kênh nội quy. (File: %s | DB: %s)",
            len(self.voice_targets),
            len(self.rule_channels),
            CONFIG_FILE,
            self.storage_path,
        )

    async def setup_hook(self):
        try:
            synced = await self.tree.sync()
            logger.info("Đã đồng bộ %s lệnh toàn cục.", len(synced))
        except Exception:
            logger.exception("Không thể đồng bộ lệnh toàn cục.")

        self.voice_monitor.start()

    async def on_ready(self):
        logger.info(
            "Bot online: %s | ID: %s",
            self.user,
            self.user.id if self.user else "unknown",
        )
        logger.info("Đường dẫn lưu trữ Volume: %s", self.storage_path)

        # Nạp lại dữ liệu đảm bảo đồng bộ hoàn toàn
        self.hydrate_from_storage()

        # KIỂM TRA CẤU HÌNH ĐÃ SETUP TRƯỚC KHI DEPLOY (PRE-SETUP / ENV)
        cfg = load_json_config()
        pre_setup = cfg.get("pre_setup", {})
        def_voice_id = ENV_DEFAULT_VOICE or pre_setup.get("default_voice_channel_id")
        def_rule_id = ENV_DEFAULT_RULE or pre_setup.get("default_rule_channel_id")

        for guild in self.guilds:
            # 1. Tự động nhận diện và gán phòng voice setup trước khi deploy nếu chưa có khóa
            if guild.id not in self.voice_targets and def_voice_id:
                ch = guild.get_channel(def_voice_id)
                if ch or len(self.guilds) == 1:
                    logger.info("Áp dụng phòng voice setup trước deploy cho server %s: %s", guild.name, def_voice_id)
                    self.voice_targets[guild.id] = def_voice_id
                    save_lock(guild.id, def_voice_id)

            # 2. Tự động nhận diện và gán kênh nội quy setup trước khi deploy nếu chưa có
            if guild.id not in self.rule_channels and def_rule_id:
                ch = guild.get_channel(def_rule_id)
                if ch or len(self.guilds) == 1:
                    logger.info("Áp dụng kênh nội quy setup trước deploy cho server %s: %s", guild.name, def_rule_id)
                    self.rule_channels[guild.id] = {
                        "channel_id": def_rule_id,
                        "user_id": OWNER_ID,
                        "user_name": "Cài đặt trước deploy",
                    }
                    save_rule_channel(guild.id, def_rule_id, OWNER_ID, "Cài đặt trước deploy")

            # 3. Đồng bộ lệnh Slash tức thì cho từng server
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("Đã đồng bộ lệnh Slash tức thì cho server %s (%s).", guild.name, guild.id)
            except Exception:
                logger.exception("Không thể đồng bộ lệnh cho server %s.", guild.id)

            # 4. TỰ ĐỘNG PHỤC HỒI KẾT NỐI VOICE 24/7 SAU MỖI LẦN DEPLOY (CHẠY SONG SONG)
            if guild.id in self.voice_targets:
                logger.info("Server %s: Tự động phục hồi kết nối voice đã khóa...", guild.name)
                asyncio.create_task(self.try_reconnect(guild.id))

    async def on_resumed(self):
        """Khi Discord Gateway kết nối lại sau sự cố mạng, lập tức kiểm tra và giữ voice 24/7."""
        logger.info("Discord Gateway đã Resumed kết nối lại. Kiểm tra trạng thái voice 24/7...")
        # Đợi 2 giây để các kết nối voice nội bộ của Discord.py ổn định
        await asyncio.sleep(2.0)
        for guild_id in list(self.voice_targets.keys()):
            self.trigger_voice_recovery(guild_id)

    def start_keepalive(self, voice_client: discord.VoiceClient):
        """Kích hoạt luồng phát âm thanh tĩnh keepalive để duy trì kết nối UDP vĩnh viễn."""
        if not self.keepalive_silence_enabled:
            return
        if not voice_client or not voice_client.is_connected():
            return

        guild = voice_client.guild
        if guild.me and guild.me.voice:
            if guild.me.voice.mute or guild.me.voice.suppress:
                return

        try:
            if not voice_client.is_playing():
                voice_client.play(
                    SilenceAudioSource(),
                    after=lambda err: self.on_keepalive_ended(voice_client, err),
                )
                logger.info("Đã kích hoạt keepalive audio 24/7 cho phòng %s.", voice_client.channel.name)
        except Exception:
            logger.exception("Không thể khởi động keepalive audio cho phòng %s.", getattr(voice_client.channel, 'name', 'unknown'))

    def on_keepalive_ended(self, voice_client: discord.VoiceClient, error):
        """Tự động phát lại keepalive trên main event loop nếu luồng bị ngắt."""
        if error:
            logger.warning("Keepalive stream kết thúc với cảnh báo: %s", error)
        if self.is_closed():
            return
        self.loop.call_soon_threadsafe(self._restart_keepalive_safe, voice_client)

    def _restart_keepalive_safe(self, voice_client: discord.VoiceClient):
        if voice_client and voice_client.is_connected() and not voice_client.is_playing():
            try:
                if isinstance(voice_client.channel, discord.StageChannel):
                    guild = voice_client.guild
                    if guild.me and guild.me.voice and guild.me.voice.suppress:
                        return
                voice_client.play(
                    SilenceAudioSource(),
                    after=lambda err: self.on_keepalive_ended(voice_client, err),
                )
            except Exception:
                pass

    async def try_reconnect(self, guild_id: int):
        """Kết nối và giữ bot trong phòng voice 24/7 với độ ổn định tối đa."""
        channel_id = self.voice_targets.get(guild_id)
        if channel_id is None:
            return

        lock = self.get_guild_lock(guild_id)
        try:
            async with asyncio.timeout(45.0):
                async with lock:
                    await self._do_reconnect(guild_id, channel_id)
        except asyncio.TimeoutError:
            logger.warning("Timeout khi chờ lock kết nối cho server %s", guild_id)
        except Exception:
            logger.exception("Lỗi không mong muốn trong tiến trình try_reconnect cho server %s", guild_id)

    async def _do_reconnect(self, guild_id: int, channel_id: int):
        guild = self.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                pass

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            logger.warning("Không tìm thấy phòng voice đã khóa: %s (Server: %s)", channel_id, guild.name)
            return

        member = guild.me
        if member is None:
            return

        permissions = channel.permissions_for(member)
        if not permissions.view_channel or not permissions.connect:
            logger.warning("Bot không có quyền truy cập/kết nối phòng %s (%s).", channel.name, channel_id)
            return

        voice_client = guild.voice_client

        # 1. Đang kết nối và ở đúng phòng đã khóa
        if voice_client and voice_client.is_connected():
            if voice_client.channel.id == channel.id:
                # Đảm bảo self_deaf luôn bật
                if self.self_deaf_enabled and member.voice and not member.voice.self_deaf:
                    try:
                        await guild.change_voice_state(channel=channel, self_deaf=True)
                    except Exception:
                        pass
                # Đảm bảo keepalive stream đang phát nếu bật
                if self.keepalive_silence_enabled and not voice_client.is_playing():
                    self.start_keepalive(voice_client)
                if guild_id not in self.voice_connected_at:
                    self.voice_connected_at[guild_id] = datetime.datetime.now(datetime.timezone.utc)
                return
            else:
                # Bị chuyển sang phòng khác: di chuyển thẳng về phòng khóa mà không cần disconnect
                logger.info("Bot đang ở phòng khác, di chuyển về phòng khóa %s...", channel.name)
                try:
                    await voice_client.move_to(channel)
                    await asyncio.sleep(0.5)
                    if self.keepalive_silence_enabled and not voice_client.is_playing():
                        self.start_keepalive(voice_client)
                    return
                except Exception:
                    logger.exception("Không thể di chuyển về phòng %s, sẽ thử kết nối lại...", channel.name)

        # 2. Nếu voice_client đang trong quá trình tự kết nối lại (reconnecting)
        if voice_client and not voice_client.is_connected():
            for _ in range(3):
                await asyncio.sleep(1.0)
                if voice_client.is_connected():
                    if voice_client.channel.id == channel.id:
                        self.start_keepalive(voice_client)
                        return
                    else:
                        try:
                            await voice_client.move_to(channel)
                            self.start_keepalive(voice_client)
                            return
                        except Exception:
                            pass

            logger.info("Dọn dẹp voice client không phản hồi trước khi kết nối lại...")
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                pass
            await asyncio.sleep(1.0)

        # 3. Kết nối vào phòng voice với retry và exponential backoff
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                if guild.voice_client:
                    try:
                        await guild.voice_client.disconnect(force=True)
                    except Exception:
                        pass
                try:
                    key_id, _ = channel._get_voice_client_key()
                    channel._state._remove_voice_client(key_id)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

                logger.info("Đang kết nối vào phòng %s (Lần thử %s/%s)...", channel.name, attempt, max_retries)
                vc = await channel.connect(
                    timeout=20.0,
                    reconnect=True,
                    self_deaf=self.self_deaf_enabled,
                )
                self.voice_connected_at[guild_id] = datetime.datetime.now(datetime.timezone.utc)

                if isinstance(channel, discord.StageChannel):
                    try:
                        await guild.me.edit(suppress=False)
                    except Exception:
                        pass

                if self.keepalive_silence_enabled:
                    self.start_keepalive(vc)
                logger.info("✅ Bot đã kết nối thành công và khóa vị trí 24/7 tại %s!", channel.name)
                return
            except discord.ClientException as e:
                logger.warning("ClientException khi kết nối phòng %s (Lần %s): %s", channel.name, attempt, e)
                await asyncio.sleep(1.5 * attempt)
            except asyncio.TimeoutError:
                logger.warning("Hết thời gian chờ kết nối phòng %s (Lần %s)", channel.name, attempt)
                await asyncio.sleep(2.0 * attempt)
            except Exception as e:
                logger.exception("Lỗi khi kết nối phòng %s (Lần %s): %s", channel.name, attempt, e)
                await asyncio.sleep(2.5 * attempt)

    def trigger_voice_recovery(self, guild_id: int):
        """Kích hoạt khôi phục voice có debounce chống bão sự kiện."""
        existing = self._recovery_tasks.get(guild_id)
        if existing and not existing.done():
            return
        self._recovery_tasks[guild_id] = asyncio.create_task(self.recover_voice_connection(guild_id))

    async def recover_voice_connection(self, guild_id: int):
        """Khôi phục kết nối tức thì khi bot bị đá hoặc rớt mạng mà không cần chờ đến chu kỳ monitor."""
        await asyncio.sleep(2.0)
        await self.try_reconnect(guild_id)

    @tasks.loop(seconds=30)
    async def voice_monitor(self):
        """Vòng lặp giám sát định kỳ kiểm tra và duy trì kết nối voice 24/7."""
        for guild_id in list(self.voice_targets.keys()):
            try:
                guild = self.get_guild(guild_id)
                if not guild:
                    continue
                target_ch_id = self.voice_targets.get(guild_id)
                vc = guild.voice_client
                # Nếu đã kết nối đúng phòng đã khóa: bỏ qua
                if vc and vc.is_connected() and vc.channel and vc.channel.id == target_ch_id:
                    continue
                # Nếu có bất kỳ bất thường nào (mất kết nối, sai phòng): phục hồi
                await self.try_reconnect(guild_id)
            except Exception:
                logger.exception("Lỗi trong vòng lặp giám sát voice cho server %s", guild_id)

    @voice_monitor.before_loop
    async def before_voice_monitor(self):
        await self.wait_until_ready()

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if self.user is None:
            return

        # --------------------------------------------------
        # 1. Quản lý trạng thái Voice của chính Bot (Tự động phục hồi ngay lập tức)
        # --------------------------------------------------
        if member.id == self.user.id:
            guild_id = member.guild.id
            if guild_id not in self.voice_targets:
                return

            target_id = self.voice_targets[guild_id]

            # Case A: Bot bị ngắt kết nối hoàn toàn khỏi phòng thoại
            if after.channel is None:
                logger.warning(
                    "⚠️ Bot bị ngắt kết nối khỏi phòng thoại tại server %s! Đang tự động kết nối lại ngay...",
                    member.guild.name,
                )
                self.voice_connected_at.pop(guild_id, None)
                self.trigger_voice_recovery(guild_id)
                return

            # Case B: Bot bị kéo/chuyển sang phòng voice khác
            if after.channel.id != target_id:
                logger.warning(
                    "⚠️ Bot bị chuyển sang phòng khác (%s) tại server %s! Đang quay lại phòng đã khóa...",
                    after.channel.name,
                    member.guild.name,
                )
                self.trigger_voice_recovery(guild_id)
                return

            # Case C: Bot vẫn ở đúng phòng đã khóa (cập nhật mute/deaf/suppress)
            if after.channel.id == target_id:
                # Đảm bảo duy trì Self-Deaf để tiết kiệm băng thông
                if self.self_deaf_enabled and not after.self_deaf:
                    try:
                        await member.guild.change_voice_state(channel=after.channel, self_deaf=True)
                    except Exception:
                        pass

                # Đảm bảo keepalive vẫn đang phát nếu bật
                if self.keepalive_silence_enabled:
                    vc = member.guild.voice_client
                    if vc and vc.is_connected() and not vc.is_playing():
                        self.start_keepalive(vc)
            return

        # --------------------------------------------------
        # 2. Xử lý gửi bảng nội quy cho User vào phòng
        # --------------------------------------------------
        # Bỏ qua nếu là bot hoặc ID nằm trong danh sách loại trừ / chủ bot
        if member.bot or member.id == OWNER_ID or member.id in EXCLUDED_USER_IDS:
            return

        # Chỉ xử lý khi người dùng kết nối mới hoặc chuyển sang phòng voice
        if before.channel == after.channel or after.channel is None:
            return

        guild = member.guild
        rule_info = self.rule_channels.get(guild.id)
        if not rule_info:
            return

        rule_channel_id = rule_info.get("channel_id")
        rule_user_id = rule_info.get("user_id")

        # QUY TẮC QUAN TRỌNG: Ngoại trừ người dùng lệnh ra
        if rule_user_id and member.id == rule_user_id:
            logger.info("Bỏ qua gửi nội quy cho người dùng lệnh: %s (%s)", member.display_name, member.id)
            return

        rule_channel = guild.get_channel(rule_channel_id)
        if not rule_channel or not hasattr(rule_channel, "send"):
            return

        # Kiểm tra xem user có mới vào room hay không
        target_voice_id = self.voice_targets.get(guild.id)
        has_entered = False

        if target_voice_id is not None:
            # Nếu bot có khóa phòng cụ thể: chỉ gửi khi user vào đúng phòng đó
            if after.channel.id == target_voice_id:
                has_entered = True
        else:
            # Nếu chưa khóa phòng nào: gửi khi user vào bất kỳ phòng thoại nào
            has_entered = True

        if not has_entered:
            return

        # Debounce 3 giây tránh Discord gửi lặp sự kiện
        now = asyncio.get_event_loop().time()
        last_time = self.last_rule_send.get(member.id, 0)
        if now - last_time < 3:
            return
        self.last_rule_send[member.id] = now

        try:
            embed = make_rule_embed(guild)
            try:
                await rule_channel.send(content=f"{member.mention} 🌸", embed=embed)
            except discord.Forbidden:
                await rule_channel.send(f"{member.mention}\n{RULE_CONTENT}")

            logger.info(
                "Đã gửi nội quy cho %s (%s) tại kênh #%s.",
                member.display_name,
                member.id,
                rule_channel.name,
            )
        except discord.Forbidden:
            logger.warning("Bot thiếu quyền gửi tin nhắn ở kênh %s.", rule_channel_id)
        except Exception:
            logger.exception("Lỗi khi gửi tin nhắn nội quy.")

bot = VoiceLockBot()


# ==================================================
# KIỂM TRA QUYỀN
# ==================================================

def is_authorized(user: discord.User | discord.Member, guild: discord.Guild = None) -> bool:
    # Chỉ duy nhất chủ bot (OWNER_ID) mới có quyền sử dụng lệnh,
    # ngoài người đó ra không ai có thể dùng được lệnh kể cả chủ server.
    return user.id == OWNER_ID


def authorized_only():
    async def predicate(interaction: discord.Interaction):
        if not is_authorized(interaction.user, interaction.guild):
            raise app_commands.CheckFailure("Bạn không có quyền sử dụng lệnh này.")
        return True

    return app_commands.check(predicate)



# ==================================================
# /join — KHÓA VỊ TRÍ PHÒNG THOẠI 24/7 (LƯU BỀN VỮNG ĐA TẦNG)
# ==================================================

@bot.tree.command(name="join", description="Khóa vị trí phòng thoại của bot 24/7 (tự phục hồi sau deploy).")
@authorized_only()
async def join(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("🌸 Lệnh này chỉ dùng được trong server thôi nè.", ephemeral=True)
        return

    member = interaction.guild.get_member(interaction.user.id)
    if member is None or member.voice is None:
        await interaction.response.send_message("🌸 Bạn cần ở trong một phòng voice trước khi dùng /join nhen ~", ephemeral=True)
        return

    channel = member.voice.channel
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        await interaction.response.send_message("🌸 Bot chỉ có thể khóa vị trí ở phòng thoại thôi nè.", ephemeral=True)
        return

    bot_member = interaction.guild.me
    if bot_member is None:
        await interaction.response.send_message("🌸 Không thể lấy thông tin bot trong server.", ephemeral=True)
        return

    permissions = channel.permissions_for(bot_member)
    if not permissions.view_channel or not permissions.connect:
        await interaction.response.send_message("🌸 Bot chưa có quyền vào phòng thoại này rùi.", ephemeral=True)
        return

    await interaction.response.defer()

    # Cập nhật Tầng 1 (RAM) và Tầng 2 (config.json tĩnh + Volume SQLite)
    bot.voice_targets[interaction.guild.id] = channel.id
    save_lock(interaction.guild.id, channel.id, interaction.user.id)
    log_command(
        interaction.guild.id,
        interaction.channel.id,
        interaction.user.id,
        str(interaction.user),
        "/join",
        f"channel={channel.name} ({channel.id})",
    )

    try:
        await bot.try_reconnect(interaction.guild.id)
        embed = discord.Embed(
            description=(
                f"🔒 **Đã khóa vị trí phòng voice tại** {channel.mention}\n"
                f"└ 🍏 *Bot sẽ ở đây 24/7 và tự động vào lại khi khởi động nhen ~*"
            ),
            color=0xFFA6C9,
        )
        await interaction.followup.send(embed=embed)
    except Exception:
        logger.exception("Không thể kết nối phòng thoại.")
        await interaction.followup.send(
            "🌸 Đã lưu phòng cần khóa rùi, nhưng bot chưa kết nối được ngay. Bạn kiểm tra lại quyền Connect của bot nhen ~"
        )


# ==================================================
# /setup — CÀI ĐẶT PHÒNG VOICE 24/7 & NỘI QUY TRONG 1 BƯỚC
# ==================================================

@bot.tree.command(name="setup", description="Cài đặt phòng Voice 24/7 và Kênh Nội Quy trong 1 thao tác duy nhất.")
@app_commands.describe(
    phong_voice="Chọn phòng voice bot sẽ treo 24/7",
    kenh_noi_quy="(Tùy chọn) Chọn kênh văn bản gửi bảng nội quy khi có người vào room"
)
@authorized_only()
async def slash_setup(
    interaction: discord.Interaction,
    phong_voice: discord.VoiceChannel | discord.StageChannel,
    kenh_noi_quy: discord.TextChannel = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("🌸 Lệnh chỉ dùng được trong server thôi nè.", ephemeral=True)
        return

    await interaction.response.defer()

    # Lưu phòng voice vào RAM + config.json + SQLite
    bot.voice_targets[interaction.guild.id] = phong_voice.id
    save_lock(interaction.guild.id, phong_voice.id, interaction.user.id)

    # Lưu kênh nội quy nếu có
    rule_msg = ""
    if kenh_noi_quy:
        bot.rule_channels[interaction.guild.id] = {
            "channel_id": kenh_noi_quy.id,
            "user_id": interaction.user.id,
            "user_name": str(interaction.user),
        }
        save_rule_channel(interaction.guild.id, kenh_noi_quy.id, interaction.user.id, str(interaction.user))
        rule_msg = f"\n> 📜 **Kênh Lời Nhắn:** {kenh_noi_quy.mention} *(Ngoại trừ: {interaction.user.mention})*"

    log_command(
        interaction.guild.id,
        interaction.channel.id,
        interaction.user.id,
        str(interaction.user),
        "/setup",
        f"voice={phong_voice.name} ({phong_voice.id}), rule={kenh_noi_quy.name if kenh_noi_quy else 'None'}",
    )

    # Kết nối và bật keepalive 24/7
    await bot.try_reconnect(interaction.guild.id)

    embed = discord.Embed(
        title="🌸 ‧ Cài Đặt Hoàn Tất ‧ 🍏",
        description=(
            f"> 🎙️ **Phòng Voice 24/7:** {phong_voice.mention}"
            f"{rule_msg}\n"
            f"> 🕊️ *Cấu hình đã được lưu vĩnh viễn, bot sẽ luôn giữ phòng 24/7.*"
        ),
        color=0xFFA6C9,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="✨ Chúc bạn có những phút giây thật vui vẻ nhen ~ 🌷")
    await interaction.followup.send(embed=embed)


# ==================================================
# /leave — TẮT KHÓA VỊ TRÍ
# ==================================================

@bot.tree.command(name="leave", description="Tắt khóa vị trí và cho bot rời phòng thoại.")
@authorized_only()
async def leave(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("🌸 Lệnh này chỉ dùng được trong server thôi nè.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    bot.voice_targets.pop(guild_id, None)
    remove_lock(guild_id)
    log_command(interaction.guild.id, interaction.channel.id, interaction.user.id, str(interaction.user), "/leave")

    voice_client = interaction.guild.voice_client
    if voice_client and (voice_client.is_connected() or voice_client.is_connecting()):
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            logger.exception("Lỗi khi ngắt kết nối voice.")
            await interaction.response.send_message("🌸 Đã tắt khóa vị trí rùi, nhưng bot gặp xíu trục trặc khi rời phòng nè.", ephemeral=True)
            return

    embed = discord.Embed(
        description="🔓 **Đã mở khóa và rời khỏi phòng thoại rùi nhen ~**",
        color=0xFFA6C9,
    )
    await interaction.response.send_message(embed=embed)


# ==================================================
# /sen — GỬI TIN NHẮN
# ==================================================

@bot.tree.command(name="sen", description="Gửi tin nhắn vào kênh hiện tại.")
@app_commands.describe(noi_dung="Nội dung tin nhắn muốn gửi")
@authorized_only()
async def sen(interaction: discord.Interaction, noi_dung: str):
    if interaction.guild is None:
        await interaction.response.send_message("🌸 Lệnh này chỉ dùng được trong server thôi nè.", ephemeral=True)
        return

    if not interaction.channel or not hasattr(interaction.channel, "send"):
        await interaction.response.send_message("🌸 Không thể gửi tin nhắn ở kênh này rùi.", ephemeral=True)
        return

    if len(noi_dung) > 2000:
        await interaction.response.send_message("🌸 Nội dung dài quá 2000 ký tự rùi nè.", ephemeral=True)
        return

    try:
        await interaction.channel.send(noi_dung, allowed_mentions=discord.AllowedMentions.none())
        log_command(
            interaction.guild.id,
            interaction.channel.id,
            interaction.user.id,
            str(interaction.user),
            "/sen",
            noi_dung[:100],
        )
        await interaction.response.send_message("🌸 Đã gửi tin nhắn thành công nhen ~", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("🌸 Bot không có quyền gửi tin nhắn ở kênh này rùi.", ephemeral=True)
    except discord.HTTPException:
        logger.exception("Không thể gửi tin nhắn.")
        await interaction.response.send_message("🌸 Gặp sự cố một chút xíu khi gửi tin nhắn rùi.", ephemeral=True)


# ==================================================
# NHÓM LỆNH /rule (SLASH COMMANDS)
# ==================================================

rule_group = app_commands.Group(name="rule", description="Cài đặt thông báo nội quy phòng thoại")


@rule_group.command(name="on", description="Kích hoạt gửi nội quy tại kênh hiện tại khi có người vào room.")
@authorized_only()
async def slash_rule_on(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("🌸 Lệnh chỉ dùng được trong server thôi nè.", ephemeral=True)
        return

    # Cập nhật Tầng 1 (RAM)
    bot.rule_channels[interaction.guild.id] = {
        "channel_id": interaction.channel.id,
        "user_id": interaction.user.id,
        "user_name": str(interaction.user),
    }
    # Lưu bền vững Tầng 2 (Railway Volume SQLite)
    save_rule_channel(interaction.guild.id, interaction.channel.id, interaction.user.id, str(interaction.user))
    log_command(interaction.guild.id, interaction.channel.id, interaction.user.id, str(interaction.user), "/rule on")

    embed = discord.Embed(
        description=(
            f"🌸 **Đã bật gửi bảng lời nhắn tại** {interaction.channel.mention}\n"
            f"└ 🕊️ *Tự động gửi khi có người vào room (ngoại trừ {interaction.user.mention})*"
        ),
        color=0xFFA6C9,
    )
    await interaction.response.send_message(embed=embed)


@rule_group.command(name="off", description="Hủy kích hoạt gửi thông báo nội quy.")
@authorized_only()
async def slash_rule_off(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("🌸 Lệnh chỉ dùng được trong server thôi nè.", ephemeral=True)
        return

    bot.rule_channels.pop(interaction.guild.id, None)
    remove_rule_channel(interaction.guild.id)
    log_command(interaction.guild.id, interaction.channel.id, interaction.user.id, str(interaction.user), "/rule off")

    embed = discord.Embed(
        description="🌷 **Đã tắt tính năng tự động gửi lời nhắn rùi nhen.**",
        color=0xFFA6C9,
    )
    await interaction.response.send_message(embed=embed)


@rule_group.command(name="user", description="Gửi riêng bảng nội quy cho 1 user tại kênh này.")
@app_commands.describe(user="Thành viên nhận bảng nội quy")
@authorized_only()
async def slash_rule_user(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh chỉ dùng được trong server.", ephemeral=True)
        return

    log_command(
        interaction.guild.id,
        interaction.channel.id,
        interaction.user.id,
        str(interaction.user),
        "/rule user",
        f"target={user} ({user.id})",
    )
    embed = make_rule_embed(interaction.guild)
    try:
        await interaction.response.send_message(content=f"{user.mention} 🌸", embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(f"{user.mention}\n{RULE_CONTENT}")



bot.tree.add_command(rule_group)


# ==================================================
# /history — XEM LỊCH SỬ LỆNH ĐÃ DÙNG (TẦNG 2 VOLUME)
# ==================================================

@bot.tree.command(name="history", description="Xem lịch sử các lệnh đã dùng trong server (lưu bền vững trên Volume).")
@app_commands.describe(gioi_han="Số lượng lệnh muốn xem (mặc định 10, tối đa 25)")
@authorized_only()
async def slash_history(interaction: discord.Interaction, gioi_han: int = 10):
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh chỉ dùng được trong server.", ephemeral=True)
        return

    gioi_han = max(1, min(gioi_han, 25))
    rows = get_recent_commands(interaction.guild.id, limit=gioi_han)

    if not rows:
        await interaction.response.send_message("🌸 Chưa có lịch sử dùng lệnh nào được ghi lại nè.", ephemeral=True)
        return

    desc_lines = []
    for r in rows:
        cmd_name, u_name, u_id, ch_id, args, exec_at, success = r
        status_icon = "✨" if success else "❌"
        try:
            dt = datetime.datetime.fromisoformat(exec_at)
            ts_str = f"<t:{int(dt.timestamp())}:R>"
        except Exception:
            ts_str = str(exec_at)

        arg_str = f" `{args}`" if args else ""
        desc_lines.append(
            f"{status_icon} **`{cmd_name}`**{arg_str}\n"
            f"└ Bởi <@{u_id}> ({u_name}) tại <#{ch_id}> • {ts_str}"
        )

    embed = discord.Embed(
        title="📜 ‧ Lịch Sử Sử Dụng Lệnh ‧ 🌸",
        description="\n\n".join(desc_lines),
        color=0xFFA6C9,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"✨ Hiển thị {len(rows)} lệnh gần nhất • Nhà Của Táo 🍏")
    await interaction.response.send_message(embed=embed)


# ==================================================
# /status — KIỂM TRA TRẠNG THÁI HỆ THỐNG & TREO ROOM 24/7
# ==================================================

@bot.tree.command(name="status", description="Kiểm tra trạng thái Voice 24/7, Nội quy và lưu trữ đa tầng.")
@authorized_only()
async def slash_status(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Lệnh chỉ dùng được trong server.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    locked_ch_id = bot.voice_targets.get(guild_id)
    rule_data = bot.rule_channels.get(guild_id)

    # Kiểm tra trạng thái Voice Client thực tế
    vc = interaction.guild.voice_client
    is_connected = vc and vc.is_connected()
    is_playing_keepalive = vc and vc.is_playing()

    if locked_ch_id:
        ch = interaction.guild.get_channel(locked_ch_id)
        ch_str = ch.mention if ch else f"ID: {locked_ch_id}"
        conn_str = "🟢 Đang kết nối ổn định" if is_connected else "🟡 Đang kích hoạt kết nối lại"
        keepalive_str = "🟢 Trực tuyến 24/7 (Đã tối ưu)" if is_connected else "🟡 Đang kết nối lại"

        # Tính toán thời gian kết nối liên tục (Uptime)
        uptime_str = "Vừa kết nối"
        if guild_id in bot.voice_connected_at:
            delta = datetime.datetime.now(datetime.timezone.utc) - bot.voice_connected_at[guild_id]
            total_sec = int(delta.total_seconds())
            hours, remainder = divmod(total_sec, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"

        voice_str = (
            f"🔒 **Phòng khóa 24/7:** {ch_str}\n"
            f"• **Trạng thái:** {conn_str}\n"
            f"• **Chế độ treo phòng:** {keepalive_str}\n"
            f"• **Thời gian treo liên tục:** `{uptime_str}`\n"
            f"• **Chế độ Self-Deaf (tiết kiệm băng thông):** {'Bật ✅' if bot.self_deaf_enabled else 'Tắt'}"
        )
    else:
        voice_str = "🔓 Chưa khóa phòng nào"

    # Kiểm tra trạng thái Rule
    if rule_data:
        r_ch = interaction.guild.get_channel(rule_data.get("channel_id"))
        r_user_id = rule_data.get("user_id")
        rule_str = (
            f"✅ Bật tại {r_ch.mention if r_ch else rule_data.get('channel_id')}\n"
            f"└ 👤 Ngoại trừ người dùng lệnh: <@{r_user_id}>"
        )
    else:
        rule_str = "❌ Chưa kích hoạt"

    # Đọc cấu hình trước deploy từ config.json và biến môi trường
    cfg = load_json_config()
    pre_setup = cfg.get("pre_setup", {})
    def_voice = ENV_DEFAULT_VOICE or pre_setup.get("default_voice_channel_id")
    def_rule = ENV_DEFAULT_RULE or pre_setup.get("default_rule_channel_id")

    pre_info = (
        f"• Voice ID mặc định: `{def_voice if def_voice else 'Chưa đặt'}`\n"
        f"• Rule ID mặc định: `{def_rule if def_rule else 'Chưa đặt'}`"
    )

    embed = discord.Embed(
        title="🌸 ‧ Trạng Thái Hoạt Động ‧ 🎀",
        color=0xFFA6C9,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="🎙️ Phòng Voice 24/7",
        value=(
            f"> {ch_str}\n"
            f"> • Trạng thái: {conn_str}\n"
            f"> • Thời gian treo liên tục: `{uptime_str}`\n"
            f"> • Chế độ Self-Deaf: {'Bật ✅' if bot.self_deaf_enabled else 'Tắt'}"
        ) if locked_ch_id else "> 🔓 *Chưa khóa phòng nào*",
        inline=False,
    )
    embed.add_field(
        name="📜 Bảng Lời Nhắn",
        value=(
            f"> • Kênh nhận thông báo: {r_ch.mention if r_ch else rule_data.get('channel_id')}\n"
            f"> • Người miễn trừ: <@{r_user_id}>"
        ) if rule_data else "> ❌ *Chưa kích hoạt*",
        inline=False,
    )
    embed.add_field(
        name="💾 Trạng Thái Hệ Thống",
        value=(
            f"> • Tự phục hồi sau deploy: **Bật vĩnh viễn ✅**\n"
            f"> • Độ trễ bot (Ping): `{round(bot.latency * 1000)}ms`"
        ),
        inline=False,
    )
    embed.set_footer(text="✨ Nhà Của Táo 🍏 • Treo phòng 24/7 ổn định")
    await interaction.response.send_message(embed=embed)


# ==================================================
# /backup — XEM CẤU HÌNH CONFIG.JSON
# ==================================================

@bot.tree.command(name="backup", description="Xem file cấu hình JSON hiện tại để backup hoặc xem cài đặt.")
@authorized_only()
async def slash_backup(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("🌸 Lệnh chỉ dùng được trong server thôi nè.", ephemeral=True)
        return

    cfg = load_json_config()
    content = json.dumps(cfg, ensure_ascii=False, indent=2)
    if len(content) > 1700:
        content = content[:1700] + "\n... (đã rút gọn vì quá dài)"

    embed = discord.Embed(
        title="💾 ‧ Dữ Liệu Cấu Hình ‧ 🍏",
        description=(
            f"```json\n{content}\n```\n"
            f"> 💡 *Cấu hình này đã được lưu vĩnh viễn và tự động nạp lại sau mỗi lần deploy.*"
        ),
        color=0xFFA6C9,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================================================
# /help — SỔ TAY HƯỚNG DẪN CÁC LỆNH SLASH
# ==================================================

@bot.tree.command(name="help", description="Xem sổ tay hướng dẫn các lệnh của bot.")
@authorized_only()
async def slash_help(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("🌸 Lệnh chỉ dùng được trong server thôi nè.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🌸 ‧ Sổ Tay Hướng Dẫn Slash Command ‧ 🌷",
        description=(
            "> 🎙️ **Phòng Voice 24/7**\n"
            "> • `/setup` : Cài nhanh phòng voice 24/7 & kênh lời nhắn\n"
            "> • `/join` : Khóa vị trí phòng voice hiện tại\n"
            "> • `/leave` : Mở khóa & cho bot rời phòng voice\n\n"
            "> 📜 **Bảng Lời Nhắn & Nội Quy**\n"
            "> • `/rule on` : Bật gửi lời nhắn tự động khi có người vào room\n"
            "> • `/rule off` : Tắt tính năng tự động gửi lời nhắn\n"
            "> • `/rule user` : Gửi riêng bảng lời nhắn cho một bạn\n\n"
            "> ✨ **Hệ Thống & Tiện Ích**\n"
            "> • `/status` : Xem trạng thái treo room & thời gian online\n"
            "> • `/history` : Xem lịch sử các lệnh đã dùng trong server\n"
            "> • `/sen` : Nhờ bot gửi tin nhắn vào kênh hiện tại\n"
            "> • `/backup` : Xem file cấu hình JSON hiện tại"
        ),
        color=0xFFA6C9,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="✨ Nhà Của Táo 🍏 • Chúc bạn luôn vui vẻ nhen ~")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================================================
# XỬ LÝ LỖI SLASH COMMAND
# ==================================================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = "🌷 Bạn chưa có quyền sử dụng lệnh này nè."
    else:
        logger.exception("Lỗi slash command: %s", error)
        message = "🌸 Lệnh gặp sự cố một chút xíu rùi, bạn thử lại sau nhen."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Không thể gửi thông báo lỗi.")


# ==================================================
# CHẠY BOT
# ==================================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Thiếu biến môi trường DISCORD_TOKEN. Hãy cài đặt trong tab Variables trên Railway.")
    if TOKEN.lower() in ("your_bot_token_here", "your_token_here", "token"):
        raise RuntimeError("Bạn đang để token mẫu! Hãy vào Discord Developer Portal -> Bot -> Reset Token để lấy token thật.")

    bot.run(TOKEN)
