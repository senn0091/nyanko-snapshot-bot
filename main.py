
import os
import io
import uuid
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

# ==============================
# 설정
# ==============================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
KEY = os.getenv("ENCRYPTION_KEY")

DB_PATH = Path(os.getenv("DB_PATH", "nyanko.db"))
SAVE_DIR = Path(os.getenv("SNAPSHOT_DIR", "snapshots"))

MAX_SIZE = 8 * 1024 * 1024

if not TOKEN or not KEY:
    raise RuntimeError("DISCORD_TOKEN 또는 ENCRYPTION_KEY가 없습니다.")

cipher = Fernet(KEY.encode())

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SAVE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nyanko")


# ==============================
# 데이터베이스
# ==============================

def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL,
            version TEXT NOT NULL,
            device TEXT NOT NULL,
            note TEXT
        )
        """)


# ==============================
# 백업 생성
# ==============================

def create_backup(user_id, data, version, device, note):

    if not data:
        raise ValueError("빈 파일입니다.")

    if len(data) > MAX_SIZE:
        raise ValueError("파일 크기 초과")

    backup_id = uuid.uuid4().hex[:12]
    filename = backup_id + ".enc"
    path = SAVE_DIR / filename

    encrypted = cipher.encrypt(data)
    temp = SAVE_DIR / (backup_id + ".tmp")

    try:
        temp.write_bytes(encrypted)
        os.replace(temp, path)

        created = datetime.now(timezone.utc).isoformat()

        with db_connect() as db:
            db.execute("""
            INSERT INTO snapshots
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                backup_id,
                str(user_id),
                filename,
                created,
                version,
                device,
                note
            ))

    except Exception:
        temp.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise

    return backup_id


# ==============================
# 백업 목록
# ==============================

def get_backups(user_id):

    with db_connect() as db:
        return db.execute("""
        SELECT id, created_at, version, device, note
        FROM snapshots
        WHERE user_id = ?
        ORDER BY created_at DESC
        """, (str(user_id),)).fetchall()


# ==============================
# 백업 불러오기
# ==============================

def load_backup(user_id, backup_id):

    with db_connect() as db:
        result = db.execute("""
        SELECT filename
        FROM snapshots
        WHERE user_id = ? AND id = ?
        """, (str(user_id), backup_id)).fetchone()

    if not result:
        raise ValueError("백업이 없습니다.")

    path = SAVE_DIR / result[0]

    if not path.is_file():
        raise FileNotFoundError("백업 파일이 없습니다.")

    try:
        return cipher.decrypt(path.read_bytes())
    except InvalidToken:
        raise ValueError("암호화 키가 일치하지 않습니다.")


# ==============================
# 백업 삭제
# ==============================

def delete_backup(user_id, backup_id):

    with db_connect() as db:
        result = db.execute("""
        SELECT filename
        FROM snapshots
        WHERE user_id = ? AND id = ?
        """, (str(user_id), backup_id)).fetchone()

        if not result:
            return False

        db.execute("""
        DELETE FROM snapshots
        WHERE user_id = ? AND id = ?
        """, (str(user_id), backup_id))

    (SAVE_DIR / result[0]).unlink(missing_ok=True)
    return True


# ==============================
# Discord 봇
# ==============================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


@bot.event
async def setup_hook():
    init_db()
    await bot.tree.sync()


@bot.event
async def on_ready():
    logger.info("봇 로그인 완료: %s", bot.user)


# ==============================
# 백업 입력창
# ==============================

class BackupModal(discord.ui.Modal):

    def __init__(self, attachment):
        super().__init__(title="냥코 Snapshot 백업")

        self.attachment = attachment

        self.version = discord.ui.TextInput(
            label="게임 버전",
            placeholder="예: 15.6.1",
            max_length=20
        )

        self.device = discord.ui.TextInput(
            label="기기",
            placeholder="Android 또는 iOS",
            max_length=10
        )

        self.note = discord.ui.TextInput(
            label="백업 메모",
            required=False,
            max_length=100
        )

        self.add_item(self.version)
        self.add_item(self.device)
        self.add_item(self.note)

    async def on_submit(self, interaction):

        await interaction.response.defer(ephemeral=True)

        try:
            data = await self.attachment.read()

            if len(data) != self.attachment.size:
                raise ValueError("파일 크기가 일치하지 않습니다.")

            device = self.device.value.strip().lower()

            if device not in ("android", "ios"):
                await interaction.followup.send(
                    "Android 또는 iOS를 입력해 주세요.",
                    ephemeral=True
                )
                return

            backup_id = create_backup(
                interaction.user.id,
                data,
                self.version.value,
                device,
                self.note.value
            )

            await interaction.followup.send(
                f"✅ 백업 완료!\n"
                f"백업 번호: `{backup_id}`\n"
                f"게임 버전: `{self.version.value}`\n"
                f"기기: `{device}`\n"
                f"국가: `kr`",
                ephemeral=True
            )

            del data

        except Exception:
            logger.exception("백업 실패")
            await interaction.followup.send(
                "백업에 실패했습니다.",
                ephemeral=True
            )


# ==============================
# /backup
# ==============================

@bot.tree.command(
    name="backup",
    description="냥코 세이브 파일 백업"
)
@app_commands.describe(
    save_file="백업할 세이브 파일"
)
async def backup(
    interaction: discord.Interaction,
    save_file: discord.Attachment
):

    if save_file.size <= 0 or save_file.size > MAX_SIZE:
        await interaction.response.send_message(
            "파일 크기가 올바르지 않습니다.",
            ephemeral=True
        )
        return

    await interaction.response.send_modal(
        BackupModal(save_file)
    )


# ==============================
# /snapshots
# ==============================

@bot.tree.command(
    name="snapshots",
    description="내 백업 목록 확인"
)
async def snapshots(interaction: discord.Interaction):

    rows = get_backups(interaction.user.id)

    if not rows:
        await interaction.response.send_message(
            "저장된 백업이 없습니다.",
            ephemeral=True
        )
        return

    result = []

    for backup_id, date, version, device, note in rows[:20]:

        result.append(
            f"**백업 번호:** `{backup_id}`\n"
            f"**날짜:** {date}\n"
            f"**버전:** {version}\n"
            f"**기기:** {device}\n"
            f"**메모:** {note or '없음'}"
        )

    await interaction.response.send_message(
        "\n\n".join(result),
        ephemeral=True
    )


# ==============================
# /restore
# ==============================

@bot.tree.command(
    name="restore",
    description="선택한 백업 파일을 DM으로 받기"
)
@app_commands.describe(
    backup_id="복원할 백업 번호"
)
async def restore(
    interaction: discord.Interaction,
    backup_id: str
):

    await interaction.response.defer(ephemeral=True)

    try:
        data = load_backup(
            interaction.user.id,
            backup_id
        )

        file = discord.File(
            io.BytesIO(data),
            filename=f"nyanko_{backup_id}.sav"
        )

        await interaction.user.send(
            content=(
                "📦 **냥코 Snapshot 백업 파일**\n"
                f"백업 번호: `{backup_id}`\n\n"
                "이 파일은 백업 당시 보관한 원본 데이터입니다. "
                "게임 계정에 자동으로 적용되지는 않습니다."
            ),
            file=file
        )

        await interaction.followup.send(
            "✅ 백업 파일을 개인 DM으로 전달했습니다.",
            ephemeral=True
        )

        del data

    except discord.Forbidden:
        await interaction.followup.send(
            "DM을 받을 수 없습니다. DM 설정을 확인해 주세요.",
            ephemeral=True
        )

    except FileNotFoundError:
        await interaction.followup.send(
            "백업 파일이 존재하지 않습니다.",
            ephemeral=True
        )

    except ValueError:
        await interaction.followup.send(
            "백업을 읽을 수 없습니다.",
            ephemeral=True
        )

    except Exception:
        logger.exception("복원 파일 전달 실패")
        await interaction.followup.send(
            "복원 파일 전달에 실패했습니다.",
            ephemeral=True
        )


# ==============================
# /backup-delete
# ==============================

@bot.tree.command(
    name="backup-delete",
    description="백업 삭제"
)
@app_commands.describe(
    backup_id="삭제할 백업 번호"
)
async def backup_delete(
    interaction: discord.Interaction,
    backup_id: str
):

    deleted = delete_backup(
        interaction.user.id,
        backup_id
    )

    await interaction.response.send_message(
        "🗑️ 백업을 삭제했습니다."
        if deleted
        else "백업을 찾을 수 없습니다.",
        ephemeral=True
    )


# ==============================
# 봇 실행
# ==============================

if __name__ == "__main__":
    bot.run(TOKEN)
