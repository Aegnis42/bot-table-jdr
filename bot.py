import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
import os
import uuid
import asyncpg
import asyncio
import time
import json
import base64
import io

PARIS_TZ = ZoneInfo("Europe/Paris")

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

TOKEN        = os.environ.get("TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

TRIGGER_CHANNEL_ID    = 1490398403819995176
SPECTATE_CHANNEL_ID   = 1489613853594484866
STATS_CHANNEL_ID      = 1508044925407858818
TEXT_CHANNELS         = ["𝗟𝗲-𝗯𝗮𝘇𝗮𝗿", "𝗟𝗮𝗻𝗰𝗲́𝗲-𝗱𝗲-𝗱𝗲́𝘀", "𝗣𝗮𝗿𝘁𝗮𝗴𝗲-𝗿𝗲𝘀𝘀𝗼𝘂𝗿𝗰𝗲𝘀"]
VOICE_CHANNELS        = ["𝗩𝗼𝗰𝗮𝗹", "𝗣𝗿𝗶𝘃𝗲𝗿 𝗠𝗝"]
INACTIVITY_MINUTES    = 60
REFERENCE_CATEGORY_ID = 1455416092141813864
GUILD_ID              = 1455403810888617996
CALENDAR_CHANNEL_ID   = 1493991218470850681
FORUM_OS_ID           = 1455406081621758027
FORUM_CAMPAGNE_ID     = 1455406457829851148
ANNONCE_CHANNEL_ID    = 1491158533058855115

TAG_TO_ROLE = {
    "Semaine / Journee":  "Semaine / Journée",
    "Semaine / Journée":  "Semaine / Journée",
    "Semaine / Soir":     "Semaine / Soir",
    "Weekend / Journee":  "Weekend / Journée",
    "Weekend / Journée":  "Weekend / Journée",
    "Weekend / Soir":     "Weekend / Soir",
    "Novice":             "Novice",
    "D&D5":               "D&D5e",
    "D&D5e":              "D&D5e",
    "Cthullu":            "Cthulhu",
    "Cthulhu":            "Cthulhu",
    "WoD":                "WoD",
    "Cyberpunk":          "Cyberpunk",
    "Homebrew":           "Homebrew",
    "Autres":             "Autres",
}


@dataclass
class Session:
    id:           str
    mj_id:        int
    game:         str
    starts_at:    str
    player_ids:   list[int] = field(default_factory=list)
    created:      bool      = False
    cancelled:    bool      = False
    reminded_24h: bool      = False
    reminded_1h:  bool      = False


# ─────────────────────────────────────────────────────────
#  Bot
# ─────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states    = True
intents.guilds          = True
intents.members         = True
intents.reactions       = True
intents.message_content = True
intents.guild_messages  = True

GUILD = discord.Object(id=GUILD_ID)


class JDRBot(commands.Bot):
    async def setup_hook(self):
        await init_db()
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)
        print("[BOT] Commandes slash synchronisées")


bot  = JDRBot(command_prefix="!", intents=intents)
tree = bot.tree

# État en mémoire (cache reconstruit depuis la DB au démarrage)
db_pool:             asyncpg.Pool | None       = None
inactive_since:      dict[int, datetime]       = {}
active_members:      dict[int, set[int]]       = {}
category_creators:   dict[int, int]            = {}
spectate_messages:   dict[int, int]            = {}
category_texts:      dict[int, int]            = {}
category_spectators: dict[int, set[int]]       = {}
category_players:    dict[int, set[int]]       = {}
category_mj_chan:    dict[int, int]            = {}
pending_spectators:  dict[tuple[int,int], int] = {}
voice_join_times:    dict[tuple[int,int], datetime] = {}  # (cat_id, member_id) → heure d'entrée

_state_restored = False


# ─────────────────────────────────────────────────────────
#  Base de données
# ─────────────────────────────────────────────────────────

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          TEXT      PRIMARY KEY,
                mj_id       BIGINT    NOT NULL,
                game        TEXT      NOT NULL,
                starts_at   TIMESTAMP NOT NULL,
                player_ids  BIGINT[]  NOT NULL DEFAULT '{}',
                created     BOOLEAN   NOT NULL DEFAULT FALSE,
                cancelled   BOOLEAN   NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS reminded_24h BOOLEAN NOT NULL DEFAULT FALSE")
        await conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS reminded_1h  BOOLEAN NOT NULL DEFAULT FALSE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS active_categories (
                cat_id         BIGINT    PRIMARY KEY,
                guild_id       BIGINT    NOT NULL,
                creator_id     BIGINT    NOT NULL,
                mj_chan_id     BIGINT,
                text_chan_id   BIGINT,
                inactive_since TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS spectate_msgs (
                msg_id BIGINT PRIMARY KEY,
                cat_id BIGINT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS category_members (
                cat_id    BIGINT NOT NULL,
                member_id BIGINT NOT NULL,
                role      TEXT   NOT NULL,
                PRIMARY KEY (cat_id, member_id, role)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS table_logs (
                id         SERIAL    PRIMARY KEY,
                cat_id     BIGINT    NOT NULL,
                mj_id      BIGINT    NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS play_time (
                id         SERIAL    PRIMARY KEY,
                member_id  BIGINT    NOT NULL,
                cat_id     BIGINT    NOT NULL,
                is_mj      BOOLEAN   NOT NULL DEFAULT FALSE,
                joined_at  TIMESTAMP NOT NULL,
                left_at    TIMESTAMP NOT NULL,
                duration_s INTEGER   NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS session_templates (
                id         TEXT     PRIMARY KEY,
                creator_id BIGINT   NOT NULL,
                name       TEXT     NOT NULL,
                game       TEXT     NOT NULL,
                player_ids BIGINT[] NOT NULL DEFAULT '{}',
                messages   JSONB    NOT NULL DEFAULT '[]',
                UNIQUE (creator_id, name)
            )
        """)
    print("[DB] Tables initialisées")


async def restore_state():
    async with db_pool.acquire() as conn:
        for row in await conn.fetch("SELECT * FROM active_categories"):
            cid = row['cat_id']
            active_members[cid]      = set()
            category_creators[cid]   = row['creator_id']
            category_mj_chan[cid]    = row['mj_chan_id']
            category_texts[cid]      = row['text_chan_id']
            category_spectators[cid] = set()
            category_players[cid]    = set()
            if row['inactive_since']:
                inactive_since[cid] = row['inactive_since']

        for row in await conn.fetch("SELECT * FROM category_members"):
            cid = row['cat_id']
            if row['role'] == 'spectator' and cid in category_spectators:
                category_spectators[cid].add(row['member_id'])
            elif row['role'] == 'player' and cid in category_players:
                category_players[cid].add(row['member_id'])

        for row in await conn.fetch("SELECT * FROM spectate_msgs"):
            spectate_messages[row['msg_id']] = row['cat_id']

    # Supprime les catégories supprimées sur Discord pendant l'arrêt du bot
    async with db_pool.acquire() as conn:
        for cid in list(active_members.keys()):
            exists = any(guild.get_channel(cid) is not None for guild in bot.guilds)
            if not exists:
                await conn.execute("DELETE FROM category_members  WHERE cat_id=$1", cid)
                await conn.execute("DELETE FROM spectate_msgs      WHERE cat_id=$1", cid)
                await conn.execute("DELETE FROM active_categories  WHERE cat_id=$1", cid)
                for d in (active_members, category_creators, category_mj_chan,
                          category_texts, category_spectators, category_players, inactive_since):
                    d.pop(cid, None)
                print(f"[DB] Catégorie fantôme {cid} nettoyée")

    # Reconstruit active_members depuis les vocaux Discord + initialise les join times
    now = utcnow()
    for guild in bot.guilds:
        for cid in list(active_members.keys()):
            cat = guild.get_channel(cid)
            if isinstance(cat, discord.CategoryChannel):
                for vc in voice_channels_of(cat):
                    for m in vc.members:
                        active_members[cid].add(m.id)
                        voice_join_times[(cid, m.id)] = now

    print(f"[DB] État restauré : {len(active_members)} catégorie(s) active(s)")


# ─── Helpers DB sessions ──────────────────────────────────

async def db_session_insert(s: Session):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (id, mj_id, game, starts_at, player_ids) VALUES ($1,$2,$3,$4,$5)",
            s.id, s.mj_id, s.game, datetime.fromisoformat(s.starts_at), s.player_ids,
        )

async def db_session_update(s: Session):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE sessions
               SET created=$1, cancelled=$2, reminded_24h=$3, reminded_1h=$4
               WHERE id=$5""",
            s.created, s.cancelled, s.reminded_24h, s.reminded_1h, s.id,
        )

async def db_session_update_full(s: Session):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE sessions
               SET game=$1, starts_at=$2, player_ids=$3,
                   created=$4, cancelled=$5, reminded_24h=$6, reminded_1h=$7
               WHERE id=$8""",
            s.game, datetime.fromisoformat(s.starts_at), s.player_ids,
            s.created, s.cancelled, s.reminded_24h, s.reminded_1h, s.id,
        )

async def db_sessions_all() -> list[Session]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM sessions")
    return [Session(
        id           = r['id'],
        mj_id        = r['mj_id'],
        game         = r['game'],
        starts_at    = r['starts_at'].isoformat(),
        player_ids   = list(r['player_ids']),
        created      = r['created'],
        cancelled    = r['cancelled'],
        reminded_24h = r['reminded_24h'],
        reminded_1h  = r['reminded_1h'],
    ) for r in rows]

async def db_sessions_cleanup(monday: datetime) -> int:
    cutoff_90 = utcnow() - timedelta(days=90)
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            """DELETE FROM sessions
               WHERE (starts_at < $1 AND (cancelled = TRUE OR created = FALSE))
                  OR (starts_at < $2 AND created = TRUE)""",
            monday, cutoff_90,
        )
    return int(result.split()[-1])


# ─── Helpers DB catégories ────────────────────────────────

async def db_cat_register(cat_id: int, guild_id: int, creator_id: int,
                           mj_chan_id: int | None, text_chan_id: int | None):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO active_categories (cat_id, guild_id, creator_id, mj_chan_id, text_chan_id)
               VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""",
            cat_id, guild_id, creator_id, mj_chan_id, text_chan_id,
        )

async def db_cat_unregister(cat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM category_members WHERE cat_id=$1", cat_id)
        await conn.execute("DELETE FROM spectate_msgs     WHERE cat_id=$1", cat_id)
        await conn.execute("DELETE FROM active_categories WHERE cat_id=$1", cat_id)

async def db_cat_set_inactive(cat_id: int, since: datetime):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE active_categories SET inactive_since=$1 WHERE cat_id=$2", since, cat_id
        )

async def db_cat_clear_inactive(cat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE active_categories SET inactive_since=NULL WHERE cat_id=$1", cat_id
        )

async def db_spectate_add(msg_id: int, cat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO spectate_msgs (msg_id, cat_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            msg_id, cat_id,
        )

async def db_spectate_remove_by_cat(cat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM spectate_msgs WHERE cat_id=$1", cat_id)

async def db_member_add(cat_id: int, member_id: int, role: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO category_members (cat_id, member_id, role) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            cat_id, member_id, role,
        )

async def db_member_remove(cat_id: int, member_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM category_members WHERE cat_id=$1 AND member_id=$2",
            cat_id, member_id,
        )


# ─── Helpers DB stats ─────────────────────────────────────

async def db_stats_fetch() -> dict:
    async with db_pool.acquire() as conn:
        now         = utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        total      = await conn.fetchval("SELECT COUNT(*) FROM table_logs")
        this_month = await conn.fetchval(
            "SELECT COUNT(*) FROM table_logs WHERE created_at >= $1", month_start
        )
        top_mjs = await conn.fetch(
            """SELECT member_id, SUM(duration_s) AS total_s
               FROM play_time
               WHERE is_mj = TRUE AND joined_at >= $1
               GROUP BY member_id ORDER BY total_s DESC LIMIT 3""",
            month_start,
        )
        top_players = await conn.fetch(
            """SELECT member_id, SUM(duration_s) AS total_s
               FROM play_time
               WHERE is_mj = FALSE AND joined_at >= $1
               GROUP BY member_id ORDER BY total_s DESC LIMIT 3""",
            month_start,
        )
    return {
        "total":       total,
        "this_month":  this_month,
        "top_mjs":     [(r['member_id'], r['total_s']) for r in top_mjs],
        "top_players": [(r['member_id'], r['total_s']) for r in top_players],
    }


# ─── Helpers DB templates ─────────────────────────────────

async def db_template_insert(tmpl_id: str, creator_id: int, name: str,
                              game: str, player_ids: list[int], messages: list[dict]):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO session_templates (id, creator_id, name, game, player_ids, messages)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb)""",
            tmpl_id, creator_id, name, game, player_ids, json.dumps(messages),
        )

async def db_template_get(creator_id: int, name: str) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM session_templates WHERE creator_id=$1 AND name=$2",
            creator_id, name,
        )
    if not row:
        return None
    return {
        "id":         row['id'],
        "creator_id": row['creator_id'],
        "name":       row['name'],
        "game":       row['game'],
        "player_ids": list(row['player_ids']),
        "messages":   json.loads(row['messages']) if isinstance(row['messages'], str) else row['messages'],
    }

async def db_table_log(cat_id: int, mj_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO table_logs (cat_id, mj_id, created_at) VALUES ($1,$2,$3)",
            cat_id, mj_id, utcnow(),
        )

async def db_play_time_insert(member_id: int, cat_id: int, is_mj: bool,
                               joined_at: datetime, left_at: datetime, duration_s: int):
    if duration_s < 10:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO play_time (member_id, cat_id, is_mj, joined_at, left_at, duration_s)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            member_id, cat_id, is_mj, joined_at, left_at, duration_s,
        )

async def db_template_update(tmpl_id: str, game: str, player_ids: list[int], messages: list[dict]):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE session_templates SET game=$1, player_ids=$2, messages=$3::jsonb WHERE id=$4",
            game, player_ids, json.dumps(messages), tmpl_id,
        )

async def db_template_delete(tmpl_id: str):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM session_templates WHERE id=$1", tmpl_id)

async def db_templates_list(creator_id: int) -> list[dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM session_templates WHERE creator_id=$1 ORDER BY name", creator_id
        )
    return [{
        "id":         r['id'],
        "creator_id": r['creator_id'],
        "name":       r['name'],
        "game":       r['game'],
        "player_ids": list(r['player_ids']),
        "messages":   json.loads(r['messages']) if isinstance(r['messages'], str) else r['messages'],
    } for r in rows]


# ─────────────────────────────────────────────────────────
#  Helpers généraux
# ─────────────────────────────────────────────────────────

def is_dynamic_category(cat: discord.CategoryChannel) -> bool:
    return cat.id in active_members

def voice_channels_of(cat: discord.CategoryChannel) -> list[discord.VoiceChannel]:
    return [c for c in cat.channels if isinstance(c, discord.VoiceChannel)]

def count_members_in_category(cat: discord.CategoryChannel) -> int:
    return sum(len(vc.members) for vc in voice_channels_of(cat))

async def grant_access(cat: discord.CategoryChannel, member: discord.Member, spectator: bool = False):
    ow_view = discord.PermissionOverwrite(view_channel=True)
    await cat.set_permissions(member, overwrite=ow_view)
    for ch in cat.channels:
        if isinstance(ch, discord.VoiceChannel):
            ow = discord.PermissionOverwrite(
                view_channel=True, connect=True,
                speak=not spectator, stream=not spectator,
            )
            await ch.set_permissions(member, overwrite=ow)
        else:
            await ch.set_permissions(member, overwrite=ow_view)

async def delete_category(cat: discord.CategoryChannel):
    print(f"[BOT] Suppression de {cat.name}")
    left_at = utcnow()
    for vc in voice_channels_of(cat):
        for m in vc.members:
            key = (cat.id, m.id)
            if key in voice_join_times:
                duration = int((left_at - voice_join_times.pop(key)).total_seconds())
                is_mj = (m.id == category_creators.get(cat.id))
                await db_play_time_insert(m.id, cat.id, is_mj, left_at - timedelta(seconds=duration), left_at, duration)
    await delete_spectate_message(cat.guild, cat.id)
    for channel in cat.channels:
        try:
            await channel.delete()
        except Exception as e:
            print(f"[BOT] Erreur suppression salon {channel.name}: {e}")
    try:
        await cat.delete()
    except Exception as e:
        print(f"[BOT] Erreur suppression catégorie {cat.name}: {e}")
    for d in (inactive_since, active_members, category_creators,
              category_texts, category_spectators, category_players, category_mj_chan):
        d.pop(cat.id, None)
    await db_cat_unregister(cat.id)
    for guild in bot.guilds:
        await refresh_stats(guild)


async def create_session_category(
    guild: discord.Guild,
    mj: discord.Member,
    players: list[discord.Member] | None = None,
) -> tuple[discord.CategoryChannel, discord.TextChannel, list[discord.VoiceChannel]]:
    ref_cat  = guild.get_channel(REFERENCE_CATEGORY_ID)
    position = ref_cat.position if ref_cat else 0

    cat = await guild.create_category(
        f"Session de {mj.display_name}",
        position=position,
        overwrites={
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            mj:                 discord.PermissionOverwrite(view_channel=True, speak=True, stream=True),
        },
    )
    active_members[cat.id]      = set()
    category_creators[cat.id]   = mj.id
    category_spectators[cat.id] = set()
    category_players[cat.id]    = set()

    text_channels = [
        await guild.create_text_channel(name, category=cat)
        for name in TEXT_CHANNELS
    ]

    mj_channel = await guild.create_text_channel(
        "mj-prive", category=cat,
        overwrites={
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            mj:                 discord.PermissionOverwrite(view_channel=True, send_messages=True),
        },
    )
    category_mj_chan[cat.id] = mj_channel.id

    creator_vc_ow = discord.PermissionOverwrite(
        view_channel=True, connect=True, speak=True,
        stream=True, mute_members=True, deafen_members=True,
    )
    voice_list = [
        await guild.create_voice_channel(
            name, category=cat,
            overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
                mj: creator_vc_ow,
            },
        )
        for name in VOICE_CHANNELS
    ]

    if text_channels:
        category_texts[cat.id] = text_channels[0].id

    for player in (players or []):
        await grant_access(cat, player, spectator=False)
        category_players[cat.id].add(player.id)

    await db_cat_register(
        cat.id, guild.id, mj.id,
        mj_channel.id,
        text_channels[0].id if text_channels else None,
    )
    for player in (players or []):
        await db_member_add(cat.id, player.id, 'player')
    await db_table_log(cat.id, mj.id)

    return cat, mj_channel, voice_list


# ─────────────────────────────────────────────────────────
#  Message spectate
# ─────────────────────────────────────────────────────────

def build_spectate_embed(guild: discord.Guild, cat: discord.CategoryChannel) -> discord.Embed:
    creator_id   = category_creators.get(cat.id)
    creator      = guild.get_member(creator_id) if creator_id else None
    mj_str       = creator.display_name if creator else "*Inconnu*"

    player_members = [guild.get_member(uid) for uid in category_players.get(cat.id, set()) if uid != creator_id]
    player_members = [m for m in player_members if m]
    player_str     = "\n".join(f"• {m.display_name}" for m in player_members) or "*Personne*"

    spec_members = [guild.get_member(uid) for uid in category_spectators.get(cat.id, set())]
    spec_members = [m for m in spec_members if m]
    spec_str     = "\n".join(f"• {m.display_name}" for m in spec_members) or "*Personne*"

    embed = discord.Embed(title=cat.name, color=discord.Color.green(), timestamp=utcnow())
    embed.add_field(name="MJ",          value=mj_str,    inline=False)
    embed.add_field(name="Joueurs",     value=player_str, inline=True)
    embed.add_field(name="Spectateurs", value=spec_str,   inline=True)
    embed.set_footer(text="Reagis avec l'oeil pour demander a regarder")
    return embed

async def post_spectate_message(guild: discord.Guild, cat: discord.CategoryChannel):
    channel = guild.get_channel(SPECTATE_CHANNEL_ID)
    if not channel:
        return
    msg = await channel.send(embed=build_spectate_embed(guild, cat))
    await msg.add_reaction("👁️")
    spectate_messages[msg.id] = cat.id
    await db_spectate_add(msg.id, cat.id)

async def update_spectate_message(guild: discord.Guild, cat: discord.CategoryChannel):
    channel = guild.get_channel(SPECTATE_CHANNEL_ID)
    if not channel:
        return
    for msg_id, cid in list(spectate_messages.items()):
        if cid == cat.id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=build_spectate_embed(guild, cat))
            except Exception as e:
                print(f"[BOT] Erreur update spectate: {e}")
            return

async def delete_spectate_message(guild: discord.Guild, cat_id: int):
    channel = guild.get_channel(SPECTATE_CHANNEL_ID)
    if not channel:
        return
    for msg_id, cid in list(spectate_messages.items()):
        if cid == cat_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.delete()
            except Exception:
                pass
            spectate_messages.pop(msg_id, None)
    await db_spectate_remove_by_cat(cat_id)


# ─────────────────────────────────────────────────────────
#  Stats
# ─────────────────────────────────────────────────────────

def format_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m:02d}" if h else f"{m}min"


async def refresh_stats(guild: discord.Guild):
    channel = guild.get_channel(STATS_CHANNEL_ID)
    if not channel:
        return
    try:
        stats = await db_stats_fetch()
    except Exception as e:
        print(f"[BOT] Erreur fetch stats: {e}")
        return

    mj_lines = []
    for i, (mid, total_s) in enumerate(stats["top_mjs"], 1):
        member = guild.get_member(mid)
        name   = member.display_name if member else f"<@{mid}>"
        mj_lines.append(f"{i}. {name} — {format_duration(total_s)}")

    player_lines = []
    for i, (mid, total_s) in enumerate(stats["top_players"], 1):
        member = guild.get_member(mid)
        name   = member.display_name if member else f"<@{mid}>"
        player_lines.append(f"{i}. {name} — {format_duration(total_s)}")

    embed = discord.Embed(
        title="📊 Statistiques des sessions JDR",
        color=discord.Color.gold(),
        timestamp=utcnow(),
    )
    embed.add_field(
        name="Sessions jouées",
        value=f"**{stats['total']}** all time  |  **{stats['this_month']}** ce mois-ci",
        inline=False,
    )
    embed.add_field(
        name="🏆 Top MJ — temps de jeu (mois)",
        value="\n".join(mj_lines) or "*Aucune donnée*",
        inline=True,
    )
    embed.add_field(
        name="🎮 Top joueurs — temps de jeu (mois)",
        value="\n".join(player_lines) or "*Aucune donnée*",
        inline=True,
    )
    embed.set_footer(text="Mis à jour")

    async for msg in channel.history(limit=10):
        if msg.author == bot.user and msg.embeds:
            try:
                await msg.edit(embed=embed)
                return
            except Exception:
                pass
    msg = await channel.send(embed=embed)
    try:
        await msg.pin()
    except Exception:
        pass


@tasks.loop(hours=1)
async def refresh_stats_task():
    for guild in bot.guilds:
        await refresh_stats(guild)


# ─────────────────────────────────────────────────────────
#  Rappels automatiques
# ─────────────────────────────────────────────────────────

async def _send_session_reminders(session: Session, guild: discord.Guild, label: str):
    ts  = int(datetime.fromisoformat(session.starts_at).timestamp())
    if label == "24h":
        text = f"⏰ Ta session **{session.game}** commence dans 24h — <t:{ts}:F>"
    else:
        text = f"🔔 Ta session **{session.game}** commence dans 1h ! — <t:{ts}:F>"

    mj = guild.get_member(session.mj_id)
    if mj:
        try:
            await mj.send(text)
        except Exception:
            pass

    for uid in session.player_ids:
        player = guild.get_member(uid)
        if player:
            try:
                await player.send(text)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────
#  Boutons accepter / refuser spectateur
# ─────────────────────────────────────────────────────────

class SpectateApprovalView(discord.ui.View):
    def __init__(self, cat: discord.CategoryChannel, spectator: discord.Member, creator: discord.Member):
        super().__init__(timeout=300)
        self.cat       = cat
        self.spectator = spectator
        self.creator   = creator

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.creator.id:
            await interaction.response.send_message("Seul le createur peut repondre.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await grant_access(self.cat, self.spectator, spectator=True)
        category_spectators.setdefault(self.cat.id, set()).add(self.spectator.id)
        pending_spectators.pop((self.cat.id, self.spectator.id), None)
        await db_member_add(self.cat.id, self.spectator.id, 'spectator')

        await interaction.response.edit_message(
            content=f"**{self.spectator.display_name}** peut maintenant regarder la session (en sourdine).",
            view=None,
        )
        await update_spectate_message(interaction.guild, self.cat)
        try:
            await self.spectator.send(
                f"Ta demande de spectate pour **{self.cat.name}** a ete acceptee !\n"
                f"Note : tu es muet par defaut dans les vocaux."
            )
        except Exception:
            pass

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger, emoji="❌")
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_spectators.pop((self.cat.id, self.spectator.id), None)
        await interaction.response.edit_message(
            content=f"Demande de **{self.spectator.display_name}** refusee.", view=None
        )
        try:
            await self.spectator.send(f"Ta demande de spectate pour **{self.cat.name}** a ete refusee.")
        except Exception:
            pass

    async def on_timeout(self):
        pending_spectators.pop((self.cat.id, self.spectator.id), None)


# ─────────────────────────────────────────────────────────
#  Templates — collecte des messages (partagée)
# ─────────────────────────────────────────────────────────

async def _collect_channel_messages(cat: discord.CategoryChannel) -> list[dict]:
    messages = []
    for ch in cat.channels:
        if not isinstance(ch, discord.TextChannel):
            continue
        if ch.name == "mj-prive":
            continue
        async for m in ch.history(limit=50, oldest_first=True):
            if m.author.id == bot.user.id:
                continue
            content     = m.content.strip()
            attachments = []
            for a in m.attachments[:5]:
                if a.size > 4 * 1024 * 1024:
                    attachments.append({"filename": a.filename, "data": None, "too_large": True})
                else:
                    try:
                        raw = await a.read()
                        attachments.append({
                            "filename": a.filename,
                            "data": base64.b64encode(raw).decode("ascii"),
                        })
                    except Exception:
                        attachments.append({"filename": a.filename, "data": None, "too_large": False})
            embed_parts = []
            for e in m.embeds:
                label = e.title or (e.description[:80] if e.description else "") or "[embed]"
                embed_parts.append(f"[Embed : {label}]")
            if embed_parts:
                content = (content + "\n" + " | ".join(embed_parts)).strip()
            if not content and not attachments:
                continue
            messages.append({
                "channel":     ch.name,
                "author":      m.author.display_name,
                "content":     content[:500],
                "ts":          m.created_at.strftime("%d/%m %H:%M"),
                "is_bot":      m.author.bot,
                "attachments": attachments[:5],
            })
    return messages


# ─────────────────────────────────────────────────────────
#  Templates — restauration
# ─────────────────────────────────────────────────────────

async def _restore_template(
    interaction: discord.Interaction,
    cat: discord.CategoryChannel,
    mj: discord.Member,
    tmpl: dict,
):
    guild = interaction.guild
    await cat.edit(name=tmpl["name"])

    old_player_ids = set(category_players.get(cat.id, set()))
    new_player_ids = set(tmpl["player_ids"])
    to_add = new_player_ids - old_player_ids

    for uid in to_add:
        member = guild.get_member(uid)
        if member:
            await grant_access(cat, member, spectator=False)
            category_players[cat.id].add(uid)
            await db_member_add(cat.id, uid, 'player')

    # Poster les messages archivés dans les salons texte
    msgs_by_channel: dict[str, list[dict]] = {}
    for m in tmpl["messages"]:
        msgs_by_channel.setdefault(m["channel"], []).append(m)

    for ch in cat.channels:
        if not isinstance(ch, discord.TextChannel):
            continue
        if ch.name == "mj-prive":
            continue
        history = msgs_by_channel.get(ch.name)
        if not history:
            continue
        header = discord.Embed(
            title=f"📜 Archives — {tmpl['name']}",
            color=discord.Color.dark_grey(),
        )
        await ch.send(embed=header)
        for entry in history:
            prefix = "🤖" if entry.get("is_bot") else "💬"
            text   = f"{prefix} **[{entry['author']}]** `{entry['ts']}`"
            if entry["content"]:
                text += f" : {entry['content']}"

            files = []
            for att in entry.get("attachments", []):
                if att.get("data"):
                    raw = base64.b64decode(att["data"])
                    files.append(discord.File(io.BytesIO(raw), filename=att["filename"]))
                elif att.get("too_large"):
                    text += f"\n📎 ~~{att['filename']}~~ *(trop grand, non sauvegardé)*"

            try:
                await ch.send(content=text[:2000], files=files or discord.utils.MISSING)
            except discord.HTTPException:
                await ch.send(content=text[:2000])

    await update_spectate_message(guild, cat)
    await interaction.followup.send(f"✅ Table **{tmpl['name']}** restaurée.", ephemeral=True)


class RestoreTemplateSelect(discord.ui.Select):
    def __init__(self, cat: discord.CategoryChannel, mj: discord.Member, templates: list[dict]):
        self.cat      = cat
        self.mj       = mj
        self.tmpl_map = {t["id"]: t for t in templates}
        options = [
            discord.SelectOption(
                label=t["name"][:100],
                description=f"{t['game']} — {len(t['player_ids'])} joueur(s)"[:100],
                value=t["id"],
            )
            for t in templates[:25]
        ]
        super().__init__(placeholder="Choisir une table...", options=options)

    async def callback(self, interaction: discord.Interaction):
        tmpl = self.tmpl_map[self.values[0]]
        await interaction.response.defer(ephemeral=True)
        await _restore_template(interaction, self.cat, self.mj, tmpl)


class RestoreSelectView(discord.ui.View):
    def __init__(self, cat: discord.CategoryChannel, mj: discord.Member, templates: list[dict]):
        super().__init__(timeout=60)
        self.add_item(RestoreTemplateSelect(cat, mj, templates))


# ─────────────────────────────────────────────────────────
#  Modal + boutons contrôle de session
# ─────────────────────────────────────────────────────────

class RenameSessionModal(discord.ui.Modal, title="Nommer la session"):
    nom = discord.ui.TextInput(
        label="Nom de la session",
        placeholder="Ex: Donjon du Dragon Rouge",
        max_length=50,
        required=True,
    )

    def __init__(self, cat: discord.CategoryChannel):
        super().__init__()
        self.cat = cat

    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.nom.value.strip()
        try:
            await self.cat.edit(name=new_name)
            await update_spectate_message(interaction.guild, self.cat)
            await interaction.response.send_message(
                f"Session renommée en **{new_name}**.", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"Erreur : {e}", ephemeral=True)


class SaveTemplateModal(discord.ui.Modal, title="Sauvegarder la table"):
    nom = discord.ui.TextInput(
        label="Nom du template",
        placeholder="Ex: Donjon du Dragon Rouge",
        max_length=50,
        required=True,
    )

    def __init__(self, cat: discord.CategoryChannel, creator: discord.Member):
        super().__init__()
        self.cat     = cat
        self.creator = creator

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        name = self.nom.value.strip()

        existing = await db_template_get(self.creator.id, name)
        if existing:
            await interaction.followup.send(
                f"Un template nommé **{name}** existe déjà. Choisis un autre nom.", ephemeral=True
            )
            return

        messages   = await _collect_channel_messages(self.cat)
        player_ids = list(category_players.get(self.cat.id, set()))
        game       = self.cat.name

        await db_template_insert(str(uuid.uuid4()), self.creator.id, name, game, player_ids, messages)
        await interaction.followup.send(
            f"✅ Sauvegarde **{name}** créée avec {len(messages)} message(s).", ephemeral=True
        )


class OverwriteTemplateSelect(discord.ui.Select):
    def __init__(self, cat: discord.CategoryChannel, templates: list[dict]):
        self.cat      = cat
        self.tmpl_map = {t["id"]: t for t in templates}
        options = [
            discord.SelectOption(label=t["name"][:100], value=t["id"])
            for t in templates[:25]
        ]
        super().__init__(placeholder="Choisir la sauvegarde à écraser...", options=options)

    async def callback(self, interaction: discord.Interaction):
        tmpl = self.tmpl_map[self.values[0]]
        await interaction.response.defer(ephemeral=True)
        messages   = await _collect_channel_messages(self.cat)
        player_ids = list(category_players.get(self.cat.id, set()))
        game       = self.cat.name
        await db_template_update(tmpl["id"], game, player_ids, messages)
        await interaction.followup.send(
            f"✅ Sauvegarde **{tmpl['name']}** écrasée avec {len(messages)} message(s).", ephemeral=True
        )


class OverwriteSelectView(discord.ui.View):
    def __init__(self, cat: discord.CategoryChannel, templates: list[dict]):
        super().__init__(timeout=60)
        self.add_item(OverwriteTemplateSelect(cat, templates))


class DeleteTemplateSelect(discord.ui.Select):
    def __init__(self, templates: list[dict]):
        self.tmpl_map = {t["id"]: t for t in templates}
        options = [
            discord.SelectOption(
                label=t["name"][:100],
                description=f"{t['game']} — {len(t['player_ids'])} joueur(s)"[:100],
                value=t["id"],
            )
            for t in templates[:25]
        ]
        super().__init__(placeholder="Choisir la sauvegarde à supprimer...", options=options)

    async def callback(self, interaction: discord.Interaction):
        tmpl = self.tmpl_map[self.values[0]]
        await db_template_delete(tmpl["id"])
        await interaction.response.send_message(
            f"🗑️ Sauvegarde **{tmpl['name']}** supprimée.", ephemeral=True
        )


class DeleteTemplateSelectView(discord.ui.View):
    def __init__(self, templates: list[dict]):
        super().__init__(timeout=60)
        self.add_item(DeleteTemplateSelect(templates))


class SaveOptionsView(discord.ui.View):
    def __init__(self, cat: discord.CategoryChannel, creator: discord.Member, templates: list[dict]):
        super().__init__(timeout=60)
        self.cat       = cat
        self.creator   = creator
        self.templates = templates

    @discord.ui.button(label="Nouvelle sauvegarde", style=discord.ButtonStyle.primary, emoji="💾")
    async def new_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SaveTemplateModal(self.cat, self.creator))

    @discord.ui.button(label="Écraser une existante", style=discord.ButtonStyle.secondary, emoji="♻️")
    async def overwrite_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = OverwriteSelectView(self.cat, self.templates)
        await interaction.response.send_message(
            "Choisir la sauvegarde à écraser :", view=view, ephemeral=True
        )

    @discord.ui.button(label="Supprimer une sauvegarde", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = DeleteTemplateSelectView(self.templates)
        await interaction.response.send_message(
            "Choisir la sauvegarde à supprimer :", view=view, ephemeral=True
        )


class SessionControlView(discord.ui.View):
    def __init__(self, cat: discord.CategoryChannel, creator: discord.Member):
        super().__init__(timeout=None)
        self.cat     = cat
        self.creator = creator

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.creator.id:
            await interaction.response.send_message(
                "Seul le créateur de la session peut faire ça.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Nommer la session", style=discord.ButtonStyle.primary, emoji="✏️")
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RenameSessionModal(self.cat))

    @discord.ui.button(label="Sauvegarder", style=discord.ButtonStyle.secondary, emoji="💾")
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        templates = await db_templates_list(self.creator.id)
        if not templates:
            await interaction.response.send_modal(SaveTemplateModal(self.cat, self.creator))
        else:
            view = SaveOptionsView(self.cat, self.creator, templates)
            await interaction.response.send_message(
                "Que veux-tu faire ?", view=view, ephemeral=True
            )

    @discord.ui.button(label="Charger une table", style=discord.ButtonStyle.secondary, emoji="📂")
    async def load_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        templates = await db_templates_list(self.creator.id)
        if not templates:
            await interaction.response.send_message(
                "Tu n'as aucun template sauvegardé.", ephemeral=True
            )
            return
        view = RestoreSelectView(self.cat, self.creator, templates)
        await interaction.response.send_message(
            "Choisis une table à restaurer :", view=view, ephemeral=True
        )

    @discord.ui.button(label="Supprimer la session", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Session en cours de suppression...", ephemeral=True)
        await delete_category(self.cat)


DeleteCategoryView = SessionControlView


# ─────────────────────────────────────────────────────────
#  Reaction spectate
# ─────────────────────────────────────────────────────────

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if payload.channel_id != SPECTATE_CHANNEL_ID:
        return
    if str(payload.emoji) != "👁️":
        return

    cat_id = spectate_messages.get(payload.message_id)
    if cat_id is None:
        return

    guild     = bot.get_guild(payload.guild_id)
    spectator = guild.get_member(payload.user_id)
    cat       = guild.get_channel(cat_id)
    if not spectator or not cat:
        return

    try:
        channel = guild.get_channel(SPECTATE_CHANNEL_ID)
        msg     = await channel.fetch_message(payload.message_id)
        await msg.remove_reaction("👁️", spectator)
    except Exception:
        pass

    if (cat_id, spectator.id) in pending_spectators:
        return

    creator_id = category_creators.get(cat_id)
    creator    = guild.get_member(creator_id) if creator_id else None
    if not creator:
        return

    mj_chan_id = category_mj_chan.get(cat_id)
    mj_chan    = guild.get_channel(mj_chan_id) if mj_chan_id else None
    if not mj_chan:
        return

    view         = SpectateApprovalView(cat, spectator, creator)
    approval_msg = await mj_chan.send(
        f"{creator.mention} — **{spectator.display_name}** demande a regarder ta session !",
        view=view,
    )
    pending_spectators[(cat_id, spectator.id)] = approval_msg.id


# ─────────────────────────────────────────────────────────
#  Tâche de fond – inactivité
# ─────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def check_inactivity():
    now       = utcnow()
    threshold = timedelta(minutes=INACTIVITY_MINUTES)
    to_delete = [cid for cid, since in list(inactive_since.items()) if now - since >= threshold]
    for cid in to_delete:
        for guild in bot.guilds:
            cat = guild.get_channel(cid)
            if isinstance(cat, discord.CategoryChannel):
                if count_members_in_category(cat) == 0:
                    await delete_category(cat)
                else:
                    inactive_since.pop(cid, None)
                    await db_cat_clear_inactive(cid)


# ─────────────────────────────────────────────────────────
#  Commande /join
# ─────────────────────────────────────────────────────────

@tree.command(name="join", description="Invite des membres a rejoindre ta session")
@app_commands.describe(
    membre1="1er membre",
    membre2="2eme membre (optionnel)",
    membre3="3eme membre (optionnel)",
    membre4="4eme membre (optionnel)",
    membre5="5eme membre (optionnel)",
)
async def join_command(
    interaction: discord.Interaction,
    membre1: discord.Member,
    membre2: discord.Member | None = None,
    membre3: discord.Member | None = None,
    membre4: discord.Member | None = None,
    membre5: discord.Member | None = None,
):
    channel = interaction.channel
    if not channel.category or not is_dynamic_category(channel.category):
        await interaction.response.send_message("Commande uniquement disponible dans une session.", ephemeral=True)
        return

    cat = channel.category
    if interaction.user.id != category_creators.get(cat.id):
        await interaction.response.send_message("Seul le createur peut inviter.", ephemeral=True)
        return

    membres = [m for m in [membre1, membre2, membre3, membre4, membre5] if m is not None]
    for m in membres:
        await grant_access(cat, m, spectator=False)
        category_players.setdefault(cat.id, set()).add(m.id)
        await db_member_add(cat.id, m.id, 'player')

    await interaction.response.send_message(f"Acces accorde a : {' '.join(m.mention for m in membres)}")
    await update_spectate_message(interaction.guild, cat)


# ─────────────────────────────────────────────────────────
#  Commande /kick
# ─────────────────────────────────────────────────────────

@tree.command(name="kick", description="Expulse un membre de ta session")
@app_commands.describe(membre="Le membre à expulser")
async def kick_command(interaction: discord.Interaction, membre: discord.Member):
    channel = interaction.channel
    if not channel.category or not is_dynamic_category(channel.category):
        await interaction.response.send_message("Commande uniquement disponible dans une session.", ephemeral=True)
        return

    cat = channel.category
    if interaction.user.id != category_creators.get(cat.id):
        await interaction.response.send_message("Seul le créateur peut expulser.", ephemeral=True)
        return

    is_player    = membre.id in category_players.get(cat.id, set())
    is_spectator = membre.id in category_spectators.get(cat.id, set())
    if not is_player and not is_spectator:
        await interaction.response.send_message("Ce membre ne fait pas partie de ta session.", ephemeral=True)
        return

    await cat.set_permissions(membre, overwrite=None)
    for ch in cat.channels:
        await ch.set_permissions(membre, overwrite=None)

    for vc in voice_channels_of(cat):
        if membre in vc.members:
            try:
                await membre.move_to(None)
            except Exception:
                pass
            break

    category_players.get(cat.id, set()).discard(membre.id)
    category_spectators.get(cat.id, set()).discard(membre.id)
    active_members.get(cat.id, set()).discard(membre.id)

    await db_member_remove(cat.id, membre.id)
    await update_spectate_message(interaction.guild, cat)

    try:
        await membre.send(f"Tu as été expulsé de la session **{cat.name}**.")
    except Exception:
        pass

    await interaction.response.send_message(
        f"**{membre.display_name}** a été expulsé de la session.", ephemeral=True
    )


# ─────────────────────────────────────────────────────────
#  Événement vocal
# ─────────────────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after:  discord.VoiceState):

    # 1. Rejoint le salon déclencheur
    if after.channel and after.channel.id == TRIGGER_CHANNEL_ID:
        cat, mj_channel, voice_list = await create_session_category(member.guild, member)
        try:
            await member.move_to(voice_list[0])
            active_members[cat.id].add(member.id)
        except Exception as e:
            print(f"[BOT] Déplacement impossible : {e}")

        view = DeleteCategoryView(cat, member)
        await mj_channel.send(
            f"Bienvenue {member.mention} !\n"
            f"**Commandes disponibles :**\n"
            f"Inviter un joueur : `/join @Pseudo`\n"
            f"Les autres peuvent demander a regarder via <#{SPECTATE_CHANNEL_ID}>",
            view=view,
        )
        await post_spectate_message(member.guild, cat)
        print(f"[BOT] Catégorie créée pour {member.display_name}")
        return

    # 2. Rejoint un vocal d'une session dynamique
    if after.channel and after.channel.category:
        cat = after.channel.category
        if is_dynamic_category(cat):
            just_mute_change = (
                before.channel and before.channel.id == after.channel.id
                and before.mute != after.mute
            )
            if just_mute_change:
                return

            active_members[cat.id].add(member.id)
            inactive_since.pop(cat.id, None)
            await db_cat_clear_inactive(cat.id)

            coming_from_outside = (before.channel is None or before.channel.category_id != cat.id)
            if coming_from_outside:
                voice_join_times[(cat.id, member.id)] = utcnow()

            is_spectator = member.id in category_spectators.get(cat.id, set())
            if is_spectator and coming_from_outside:
                try:
                    await member.edit(mute=True)
                except Exception:
                    pass

            await update_spectate_message(member.guild, cat)

    # 3. Quitte un vocal d'une session dynamique
    if before.channel and before.channel.category:
        cat = before.channel.category
        if is_dynamic_category(cat):
            if after.channel and after.channel.id == before.channel.id:
                return

            active_members[cat.id].discard(member.id)

            leaving_category = (after.channel is None or after.channel.category_id != cat.id)
            if leaving_category:
                key = (cat.id, member.id)
                if key in voice_join_times:
                    left_at  = utcnow()
                    duration = int((left_at - voice_join_times.pop(key)).total_seconds())
                    is_mj    = (member.id == category_creators.get(cat.id))
                    await db_play_time_insert(member.id, cat.id, is_mj,
                                              left_at - timedelta(seconds=duration), left_at, duration)

            is_spectator = member.id in category_spectators.get(cat.id, set())
            if is_spectator and before.mute:
                try:
                    await member.edit(mute=False)
                except Exception:
                    pass

            if count_members_in_category(cat) == 0:
                now = utcnow()
                inactive_since[cat.id] = now
                await db_cat_set_inactive(cat.id, now)
                print(f"[BOT] {cat.name} vide — timer {INACTIVITY_MINUTES} min démarré")
            else:
                await update_spectate_message(member.guild, cat)


# ─────────────────────────────────────────────────────────
#  Surveillance des forums
# ─────────────────────────────────────────────────────────

@bot.event
async def on_thread_create(thread: discord.Thread):
    parent_id = thread.parent_id
    if parent_id not in (FORUM_OS_ID, FORUM_CAMPAGNE_ID):
        return

    guild           = thread.guild
    annonce_channel = guild.get_channel(ANNONCE_CHANNEL_ID)
    if not annonce_channel:
        return

    is_os      = (parent_id == FORUM_OS_ID)
    type_label = "One-Shot" if is_os else "Campagne"
    type_emoji = "⚔️" if is_os else "📜"

    await asyncio.sleep(1)
    try:
        thread = await guild.fetch_channel(thread.id)
    except Exception:
        pass

    applied_tags  = getattr(thread, 'applied_tags', [])
    roles_to_ping = []
    for tag in applied_tags:
        role_name = TAG_TO_ROLE.get(tag.name)
        if role_name:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                roles_to_ping.append(role)

    ping_str = " ".join(r.mention for r in roles_to_ping)

    author = thread.owner
    if not author:
        try:
            author = await guild.fetch_member(thread.owner_id)
        except Exception:
            author = None

    embed = discord.Embed(
        title=f"{type_emoji} Nouveau post : {thread.name}",
        description=(
            f"**Type :** {type_label}\n"
            f"**Auteur :** {author.mention if author else 'Inconnu'}\n"
            f"**Voir le post :** {thread.mention}"
        ),
        color=discord.Color.blue() if is_os else discord.Color.gold(),
        timestamp=utcnow(),
    )
    if applied_tags:
        embed.add_field(name="Tags", value=" • ".join(t.name for t in applied_tags), inline=False)

    joueur_role = discord.utils.get(guild.roles, name="Joueur")
    joueur_ping = joueur_role.mention if joueur_role else ""
    full_ping   = f"{joueur_ping} {ping_str}".strip() if ping_str else joueur_ping
    await annonce_channel.send(content=full_ping or None, embed=embed)
    print(f"[BOT] Annonce forum : {thread.name} ({type_label})")


# ─────────────────────────────────────────────────────────
#  Calendrier JDR
# ─────────────────────────────────────────────────────────

JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

def get_week_bounds() -> tuple[datetime, datetime]:
    now    = utcnow()
    monday = now - timedelta(
        days=now.weekday(), hours=now.hour,
        minutes=now.minute, seconds=now.second, microseconds=now.microsecond,
    )
    return monday, monday + timedelta(days=6, hours=23, minutes=59, seconds=59)


def build_calendar_embed(guild: discord.Guild, sessions: list[Session]) -> discord.Embed:
    monday, sunday = get_week_bounds()
    week_sessions  = sorted(
        [s for s in sessions
         if not s.cancelled and monday <= datetime.fromisoformat(s.starts_at) <= sunday],
        key=lambda s: s.starts_at,
    )
    embed = discord.Embed(
        title="📅 Agenda de la semaine",
        description=f"<t:{int(monday.timestamp())}:D> — <t:{int(sunday.timestamp())}:D>",
        color=discord.Color.blurple(),
        timestamp=utcnow(),
    )

    days: dict[int, list[Session]] = {i: [] for i in range(7)}
    for s in week_sessions:
        days[datetime.fromisoformat(s.starts_at).weekday()].append(s)

    has_any = False
    for day_idx in range(7):
        if not days[day_idx]:
            continue
        has_any = True
        lines = []
        for s in days[day_idx]:
            mj         = guild.get_member(s.mj_id)
            ts         = int(datetime.fromisoformat(s.starts_at).timestamp())
            players    = [m for uid in s.player_ids if (m := guild.get_member(uid))]
            player_str = ", ".join(m.display_name for m in players) or "Aucun"
            status     = "✅" if s.created else "🕐"
            lines.append(
                f"{status} **{s.game}** — <t:{ts}:t>\n"
                f"MJ : {mj.display_name if mj else '?'}  |  Joueurs : {player_str}\n"
                f"`ID : {s.id[:8]}`"
            )
        embed.add_field(name=f"📆 {JOURS_FR[day_idx]}", value="\n\n".join(lines), inline=False)

    if not has_any:
        embed.add_field(name="Cette semaine", value="*Aucune partie planifiée.*", inline=False)
    embed.set_footer(text="Mis à jour")
    return embed


async def refresh_calendar(guild: discord.Guild, sessions: list[Session]):
    channel = guild.get_channel(CALENDAR_CHANNEL_ID)
    if not channel:
        return
    embed = build_calendar_embed(guild, sessions)
    async for msg in channel.history(limit=20):
        if msg.author == bot.user and msg.embeds:
            try:
                await msg.edit(embed=embed)
                return
            except Exception:
                pass
    msg = await channel.send(embed=embed)
    try:
        await msg.pin()
    except Exception:
        pass


@tasks.loop(hours=1)
async def refresh_weekly_calendar():
    sessions = await db_sessions_all()
    for guild in bot.guilds:
        await refresh_calendar(guild, sessions)
    print("[BOT] Agenda rafraîchi")


@tasks.loop(minutes=1)
async def check_sessions():
    sessions = await db_sessions_all()
    now      = utcnow()

    # Rappels automatiques
    for session in sessions:
        if session.cancelled or session.created:
            continue
        delta = (datetime.fromisoformat(session.starts_at) - now).total_seconds()

        if not session.reminded_24h and 23 * 3600 - 600 <= delta <= 25 * 3600:
            for guild in bot.guilds:
                await _send_session_reminders(session, guild, "24h")
            session.reminded_24h = True
            await db_session_update(session)

        if not session.reminded_1h and 3000 <= delta <= 4200:
            for guild in bot.guilds:
                await _send_session_reminders(session, guild, "1h")
            session.reminded_1h = True
            await db_session_update(session)

    # Création automatique des tables
    for session in sessions:
        if session.cancelled or session.created:
            continue
        delta = (datetime.fromisoformat(session.starts_at) - now).total_seconds()
        if not (0 <= delta <= 600):
            continue

        session.created = True
        await db_session_update(session)

        for guild in bot.guilds:
            mj = guild.get_member(session.mj_id)
            if not mj:
                continue
            players = [p for uid in session.player_ids if (p := guild.get_member(uid))]
            cat, mj_channel, _ = await create_session_category(guild, mj, players)

            view = DeleteCategoryView(cat, mj)
            await mj_channel.send(
                f"Ta session **{session.game}** commence dans moins de 10 minutes !\n"
                f"Inviter un joueur : `/join @Pseudo`\n"
                f"Les autres peuvent demander a regarder via <#{SPECTATE_CHANNEL_ID}>",
                view=view,
            )
            await post_spectate_message(guild, cat)
            await refresh_calendar(guild, sessions)
            await refresh_stats(guild)
            print(f"[BOT] Table auto-créée pour {session.id[:8]} ({session.game})")

    monday, _ = get_week_bounds()
    deleted   = await db_sessions_cleanup(monday)
    if deleted:
        print(f"[BOT] {deleted} session(s) supprimée(s) par cleanup")


# ─────────────────────────────────────────────────────────
#  Commandes calendrier
# ─────────────────────────────────────────────────────────

@tree.command(name="planifier", description="Planifie une session de JDR")
@app_commands.describe(
    jeu="Systeme de jeu (ex: D&D5e, Cthulhu...)",
    date="Date au format JJ/MM/AAAA",
    heure="Heure au format HH:MM (heure de Paris)",
    joueur1="1er joueur",
    joueur2="2eme joueur (optionnel)",
    joueur3="3eme joueur (optionnel)",
    joueur4="4eme joueur (optionnel)",
    joueur5="5eme joueur (optionnel)",
)
async def planifier_command(
    interaction: discord.Interaction,
    jeu:     str,
    date:    str,
    heure:   str,
    joueur1: discord.Member,
    joueur2: discord.Member | None = None,
    joueur3: discord.Member | None = None,
    joueur4: discord.Member | None = None,
    joueur5: discord.Member | None = None,
):
    try:
        dt_naive = datetime.strptime(f"{date} {heure}", "%d/%m/%Y %H:%M")
        dt_paris = dt_naive.replace(tzinfo=PARIS_TZ)
        dt_utc   = dt_paris.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        await interaction.response.send_message(
            "Format invalide. Utilise JJ/MM/AAAA et HH:MM\nEx: `/planifier D&D5e 25/12/2025 20:00`",
            ephemeral=True,
        )
        return

    if dt_utc < utcnow():
        await interaction.response.send_message("Cette date est dans le passé !", ephemeral=True)
        return

    joueurs = [m for m in [joueur1, joueur2, joueur3, joueur4, joueur5] if m is not None]
    session = Session(
        id         = str(uuid.uuid4()),
        mj_id      = interaction.user.id,
        game       = jeu,
        starts_at  = dt_utc.isoformat(),
        player_ids = [m.id for m in joueurs],
    )
    await db_session_insert(session)

    sessions = await db_sessions_all()
    await refresh_calendar(interaction.guild, sessions)

    ts         = int(dt_utc.timestamp())
    player_str = ", ".join(m.mention for m in joueurs) if joueurs else "Aucun"
    await interaction.response.send_message(
        f"✅ Session planifiée !\n"
        f"**Jeu :** {jeu}\n"
        f"**Date :** <t:{ts}:F>\n"
        f"**Joueurs :** {player_str}\n"
        f"**ID :** `{session.id[:8]}`\n"
        f"La table sera créée automatiquement 10 min avant.",
        ephemeral=True,
    )
    print(f"[BOT] Session planifiée : {jeu} par {interaction.user} le {date} {heure}")


@tree.command(name="modifier-session", description="Modifie une session planifiée")
@app_commands.describe(
    session_id="Les 8 premiers caractères de l'ID de la session",
    jeu="Nouveau système de jeu (optionnel)",
    date="Nouvelle date JJ/MM/AAAA (optionnel)",
    heure="Nouvelle heure HH:MM Paris (optionnel)",
    joueur1="Nouveau joueur 1 (optionnel)",
    joueur2="Nouveau joueur 2 (optionnel)",
    joueur3="Nouveau joueur 3 (optionnel)",
    joueur4="Nouveau joueur 4 (optionnel)",
    joueur5="Nouveau joueur 5 (optionnel)",
)
async def modifier_session_command(
    interaction: discord.Interaction,
    session_id: str,
    jeu:     str | None = None,
    date:    str | None = None,
    heure:   str | None = None,
    joueur1: discord.Member | None = None,
    joueur2: discord.Member | None = None,
    joueur3: discord.Member | None = None,
    joueur4: discord.Member | None = None,
    joueur5: discord.Member | None = None,
):
    sessions = await db_sessions_all()
    session  = next(
        (s for s in sessions if s.id.startswith(session_id) and s.mj_id == interaction.user.id),
        None,
    )
    if not session:
        await interaction.response.send_message("Session introuvable ou tu n'en es pas le MJ.", ephemeral=True)
        return
    if session.cancelled:
        await interaction.response.send_message("Cette session est annulée.", ephemeral=True)
        return
    if session.created:
        await interaction.response.send_message("Cette session a déjà commencé.", ephemeral=True)
        return

    date_changed = False

    if jeu:
        session.game = jeu

    if date or heure:
        current_dt   = datetime.fromisoformat(session.starts_at)
        current_paris = current_dt.replace(tzinfo=timezone.utc).astimezone(PARIS_TZ)
        date_str     = date  or current_paris.strftime("%d/%m/%Y")
        heure_str    = heure or current_paris.strftime("%H:%M")
        try:
            dt_naive = datetime.strptime(f"{date_str} {heure_str}", "%d/%m/%Y %H:%M")
            dt_paris = dt_naive.replace(tzinfo=PARIS_TZ)
            dt_utc   = dt_paris.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            await interaction.response.send_message(
                "Format de date/heure invalide. Utilise JJ/MM/AAAA et HH:MM", ephemeral=True
            )
            return
        if dt_utc < utcnow():
            await interaction.response.send_message("Cette date est dans le passé !", ephemeral=True)
            return
        session.starts_at    = dt_utc.isoformat()
        session.reminded_24h = False
        session.reminded_1h  = False
        date_changed         = True

    new_joueurs = [m for m in [joueur1, joueur2, joueur3, joueur4, joueur5] if m is not None]
    if new_joueurs:
        old_ids = set(session.player_ids)
        new_ids = {m.id for m in new_joueurs}

        removed_ids = old_ids - new_ids
        added_ids   = new_ids - old_ids

        ts = int(datetime.fromisoformat(session.starts_at).timestamp())
        for uid in removed_ids:
            player = interaction.guild.get_member(uid)
            if player:
                try:
                    await player.send(
                        f"Tu as été retiré de la session **{session.game}** prévue le <t:{ts}:F>."
                    )
                except Exception:
                    pass
        for uid in added_ids:
            player = interaction.guild.get_member(uid)
            if player:
                try:
                    await player.send(
                        f"Tu as été ajouté à la session **{session.game}** prévue le <t:{ts}:F>."
                    )
                except Exception:
                    pass

        session.player_ids = list(new_ids)

    await db_session_update_full(session)
    sessions = await db_sessions_all()
    await refresh_calendar(interaction.guild, sessions)

    changes = []
    if jeu:
        changes.append(f"jeu → **{jeu}**")
    if date_changed:
        ts = int(datetime.fromisoformat(session.starts_at).timestamp())
        changes.append(f"date → <t:{ts}:F>")
    if new_joueurs:
        changes.append("joueurs mis à jour")

    await interaction.response.send_message(
        f"✅ Session modifiée : {', '.join(changes) or 'aucun changement'}.", ephemeral=True
    )


@tree.command(name="annuler-session", description="Annule une session planifiee")
@app_commands.describe(session_id="Les 8 premiers caracteres de l'ID de la session")
async def annuler_session_command(interaction: discord.Interaction, session_id: str):
    sessions = await db_sessions_all()
    session  = next(
        (s for s in sessions if s.id.startswith(session_id) and s.mj_id == interaction.user.id),
        None,
    )
    if not session:
        await interaction.response.send_message("Session introuvable ou tu n'en es pas le MJ.", ephemeral=True)
        return
    if session.cancelled:
        await interaction.response.send_message("Cette session est déjà annulée.", ephemeral=True)
        return
    if session.created:
        await interaction.response.send_message("Cette session a déjà commencé.", ephemeral=True)
        return

    session.cancelled = True
    await db_session_update(session)
    sessions = await db_sessions_all()
    await refresh_calendar(interaction.guild, sessions)
    await refresh_stats(interaction.guild)

    dt      = datetime.fromisoformat(session.starts_at)
    ts      = int(dt.timestamp())
    dm_ok, dm_fail = [], []
    for uid in session.player_ids:
        player = interaction.guild.get_member(uid)
        if player:
            try:
                await player.send(
                    f"❌ La session **{session.game}** prévue le <t:{ts}:F> "
                    f"a été annulée par **{interaction.user.display_name}**."
                )
                dm_ok.append(player.display_name)
            except Exception:
                dm_fail.append(player.display_name)

    recap = f"Session **{session.game}** annulée."
    if dm_ok:
        recap += f"\nMP envoyé à : {', '.join(dm_ok)}"
    if dm_fail:
        recap += f"\nImpossible de contacter : {', '.join(dm_fail)} (MP fermés)"
    await interaction.response.send_message(recap, ephemeral=True)


@tree.command(name="calendrier", description="Affiche les sessions a venir")
async def calendrier_command(interaction: discord.Interaction):
    sessions = await db_sessions_all()
    embed    = build_calendar_embed(interaction.guild, sessions)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────
#  Démarrage
# ─────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _state_restored
    if not _state_restored:
        await restore_state()
        _state_restored = True
    if not check_inactivity.is_running():
        check_inactivity.start()
    if not check_sessions.is_running():
        check_sessions.start()
    if not refresh_weekly_calendar.is_running():
        refresh_weekly_calendar.start()
    if not refresh_stats_task.is_running():
        refresh_stats_task.start()
    for guild in bot.guilds:
        await refresh_stats(guild)
    print(f"[BOT] Connecté en tant que {bot.user}")
    print(f"[BOT] Membres chargés : {sum(g.member_count for g in bot.guilds)}")


MAX_RETRIES = 5
retry_delay = 30

for attempt in range(1, MAX_RETRIES + 1):
    try:
        bot.run(TOKEN)
        break
    except discord.errors.HTTPException as e:
        if e.status == 429:
            print(f"[BOT] Rate limited (tentative {attempt}/{MAX_RETRIES}). Attente {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 600)
        else:
            raise
    except Exception as e:
        print(f"[BOT] Erreur inattendue : {e}")
        raise
