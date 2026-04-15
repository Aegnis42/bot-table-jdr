import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field, asdict
from typing import Optional
import os
import json
import uuid

PARIS_TZ = ZoneInfo("Europe/Paris")

# CONFIG
TOKEN = os.environ.get("TOKEN")
TRIGGER_CHANNEL_ID    = 1490398403819995176
SPECTATE_CHANNEL_ID   = 1489613853594484866

TEXT_CHANNELS  = ["𝗟𝗲-𝗯𝗮𝘇𝗮𝗿", "𝗟𝗮𝗻𝗰𝗲́𝗲-𝗱𝗲-𝗱𝗲́𝘀", "𝗣𝗮𝗿𝘁𝗮𝗴𝗲-𝗿𝗲𝘀𝘀𝗼𝘂𝗿𝗰𝗲𝘀"]
VOICE_CHANNELS = ["𝗩𝗼𝗰𝗮𝗹", "𝗣𝗿𝗶𝘃𝗲𝗿 𝗠𝗝"]

INACTIVITY_MINUTES    = 60
REFERENCE_CATEGORY_ID = 1455416092141813864
GUILD_ID              = 1455403810888617996

# Calendrier
CALENDAR_CHANNEL_ID = 1493991218470850681
SESSIONS_FILE       = "/app/sessions.json"

# Forums a surveiller
FORUM_OS_ID         = 1455406081621758027
FORUM_CAMPAGNE_ID   = 1455406457829851148
ANNONCE_CHANNEL_ID  = 1491158533058855115

# Correspondance tag -> nom du role a pinger
TAG_TO_ROLE = {
    "Semaine / Journee": "Semaine / Journée",
    "Semaine / Journée": "Semaine / Journée",
    "Semaine / Soir":    "Semaine / Soir",
    "Weekend / Journee": "Weekend / Journée",
    "Weekend / Journée": "Weekend / Journée",
    "Weekend / Soir":    "Weekend / Soir",
    "Novice":            "Novice",
    "D&D5":              "D&D5e",
    "D&D5e":             "D&D5e",
    "Cthullu":           "Cthulhu",
    "Cthulhu":           "Cthulhu",
    "WoD":               "WoD",
    "Cyberpunk":         "Cyberpunk",
    "Homebrew":          "Homebrew",
    "Autres":            "Autres",
}

intents = discord.Intents.default()
intents.voice_states    = True
intents.guilds          = True
intents.members         = True
intents.reactions       = True
intents.message_content = True
intents.guild_messages   = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
GUILD = discord.Object(id=GUILD_ID)

# Donnees en memoire
inactive_since:      dict[int, datetime]       = {}
active_members:      dict[int, set[int]]       = {}
category_creators:   dict[int, int]            = {}
spectate_messages:   dict[int, int]            = {}
category_texts:      dict[int, int]            = {}
category_spectators: dict[int, set[int]]       = {}  # ceux qui spectate (demande via reaction)
category_players:    dict[int, set[int]]       = {}  # joueurs invites via /join
category_mj_chan:    dict[int, int]             = {}  # {cat_id: mj_channel_id}
pending_spectators:  dict[tuple[int,int], int] = {}


# ─────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────

def is_dynamic_category(cat: discord.CategoryChannel) -> bool:
    return cat.id in active_members

def voice_channels_of(cat: discord.CategoryChannel) -> list[discord.VoiceChannel]:
    return [c for c in cat.channels if isinstance(c, discord.VoiceChannel)]

def count_members_in_category(cat: discord.CategoryChannel) -> int:
    return sum(len(vc.members) for vc in voice_channels_of(cat))

def get_voice_members(cat: discord.CategoryChannel) -> list[discord.Member]:
    members = []
    for vc in voice_channels_of(cat):
        members.extend(vc.members)
    return members

async def grant_access(cat: discord.CategoryChannel, member: discord.Member, spectator: bool = False):
    """Donne l'acces a la categorie.
    Si spectator=True : peut voir mais ne peut pas parler dans les vocaux.
    """
    # Acces en lecture a la categorie et aux salons texte
    ow_view = discord.PermissionOverwrite(view_channel=True)
    await cat.set_permissions(member, overwrite=ow_view)

    for ch in cat.channels:
        if isinstance(ch, discord.VoiceChannel):
            if spectator:
                # Spectateur : peut rejoindre et ecouter, mais ne peut pas parler
                ow_voice = discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=False,
                    stream=False,
                )
            else:
                # Membre normal : tous les droits
                ow_voice = discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                    stream=True,
                )
            await ch.set_permissions(member, overwrite=ow_voice)
        else:
            await ch.set_permissions(member, overwrite=ow_view)

async def set_mute_in_category(cat: discord.CategoryChannel, member: discord.Member, muted: bool):
    """Server-mute comme clic droit -> Rendre muet sur le serveur."""
    try:
        await member.edit(mute=muted)
    except Exception as e:
        print(f"[BOT] Erreur server mute {member}: {e}")

async def delete_category(cat: discord.CategoryChannel):
    print(f"[BOT] Suppression de {cat.name}")
    for channel in cat.channels:
        try:    await channel.delete()
        except: pass
    try:    await cat.delete()
    except: pass
    inactive_since.pop(cat.id, None)
    active_members.pop(cat.id, None)
    category_creators.pop(cat.id, None)
    category_texts.pop(cat.id, None)
    category_spectators.pop(cat.id, None)
    category_players.pop(cat.id, None)
    category_mj_chan.pop(cat.id, None)


# ─────────────────────────────────────────────────────────
#  Message spectate
# ─────────────────────────────────────────────────────────

def build_spectate_embed(guild: discord.Guild, cat: discord.CategoryChannel) -> discord.Embed:
    creator_id  = category_creators.get(cat.id)
    creator     = guild.get_member(creator_id) if creator_id else None

    player_ids  = category_players.get(cat.id, set())
    spec_ids    = category_spectators.get(cat.id, set())

    # MJ (createur)
    mj_str      = creator.display_name if creator else "*Inconnu*"

    # Joueurs (/join) — exclu le createur
    player_members = [guild.get_member(uid) for uid in player_ids if uid != creator_id]
    player_members = [m for m in player_members if m]
    player_str  = "\n".join(f"• {m.display_name}" for m in player_members) or "*Personne*"

    # Spectateurs (demande via reaction)
    spec_members = [guild.get_member(uid) for uid in spec_ids]
    spec_members = [m for m in spec_members if m]
    spec_str    = "\n".join(f"• {m.display_name}" for m in spec_members) or "*Personne*"

    embed = discord.Embed(
        title=f"Session de {mj_str}",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="MJ", value=mj_str, inline=False)
    embed.add_field(name="Joueurs", value=player_str, inline=True)
    embed.add_field(name="Spectateurs", value=spec_str, inline=True)
    embed.set_footer(text="Reagis avec l'oeil pour demander a regarder")
    return embed

async def post_spectate_message(guild: discord.Guild, cat: discord.CategoryChannel):
    channel = guild.get_channel(SPECTATE_CHANNEL_ID)
    if not channel:
        return
    embed = build_spectate_embed(guild, cat)
    msg = await channel.send(embed=embed)
    await msg.add_reaction("👁️")
    spectate_messages[msg.id] = cat.id

async def update_spectate_message(guild: discord.Guild, cat: discord.CategoryChannel):
    channel = guild.get_channel(SPECTATE_CHANNEL_ID)
    if not channel:
        return
    for msg_id, cid in list(spectate_messages.items()):
        if cid == cat.id:
            try:
                msg   = await channel.fetch_message(msg_id)
                embed = build_spectate_embed(guild, cat)
                await msg.edit(embed=embed)
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
        # Acces en mode spectateur (muet par defaut)
        await grant_access(self.cat, self.spectator, spectator=True)

        if self.cat.id not in category_spectators:
            category_spectators[self.cat.id] = set()
        category_spectators[self.cat.id].add(self.spectator.id)
        pending_spectators.pop((self.cat.id, self.spectator.id), None)

        await interaction.response.edit_message(
            content=f"**{self.spectator.display_name}** peut maintenant regarder la session (en sourdine).",
            view=None
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
            content=f"Demande de **{self.spectator.display_name}** refusee.",
            view=None
        )
        try:
            await self.spectator.send(f"Ta demande de spectate pour **{self.cat.name}** a ete refusee.")
        except Exception:
            pass

    async def on_timeout(self):
        pending_spectators.pop((self.cat.id, self.spectator.id), None)



# ─────────────────────────────────────────────────────────
#  Bouton supprimer la categorie
# ─────────────────────────────────────────────────────────

class DeleteCategoryView(discord.ui.View):
    def __init__(self, cat: discord.CategoryChannel, creator: discord.Member):
        super().__init__(timeout=None)  # Pas de timeout, le bouton reste actif
        self.cat     = cat
        self.creator = creator

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.creator.id:
            await interaction.response.send_message(
                "Seul le createur de la session peut supprimer la categorie.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Supprimer la session", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Session en cours de suppression...", ephemeral=True)
        await delete_spectate_message(interaction.guild, self.cat.id)
        await delete_category(self.cat)

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
        msg = await channel.fetch_message(payload.message_id)
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

    view = SpectateApprovalView(cat, spectator, creator)
    approval_msg = await mj_chan.send(
        f"{creator.mention} — **{spectator.display_name}** demande a regarder ta session !",
        view=view
    )
    pending_spectators[(cat_id, spectator.id)] = approval_msg.id


# ─────────────────────────────────────────────────────────
#  Tache de fond – inactivite
# ─────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def check_inactivity():
    now       = datetime.utcnow()
    threshold = timedelta(minutes=INACTIVITY_MINUTES)
    to_delete = [cid for cid, since in list(inactive_since.items())
                 if now - since >= threshold]
    for cid in to_delete:
        for guild in bot.guilds:
            cat = guild.get_channel(cid)
            if isinstance(cat, discord.CategoryChannel):
                if count_members_in_category(cat) == 0:
                    await delete_category(cat)
                else:
                    inactive_since.pop(cid, None)


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
        category_players.setdefault(cat.id, set()).add(m.id)  # joueur, pas spectateur

    await interaction.response.send_message(f"Acces accorde a : {' '.join(m.mention for m in membres)}")
    await update_spectate_message(interaction.guild, cat)




# ─────────────────────────────────────────────────────────
#  Evenement vocal
# ─────────────────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after:  discord.VoiceState):

    # 1. Rejoint le salon declencheur
    if after.channel and after.channel.id == TRIGGER_CHANNEL_ID:
        guild    = member.guild
        cat_name = f"Session de {member.display_name}"

        ref_cat  = guild.get_channel(REFERENCE_CATEGORY_ID)
        position = ref_cat.position if ref_cat else 0

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member:             discord.PermissionOverwrite(view_channel=True, speak=True, stream=True),
        }

        cat = await guild.create_category(cat_name, position=position, overwrites=overwrites)
        active_members[cat.id]      = set()
        category_creators[cat.id]   = member.id
        category_spectators[cat.id] = set()
        category_players[cat.id]    = set()

        text_channels = []
        for name in TEXT_CHANNELS:
            tc = await guild.create_text_channel(name, category=cat)
            text_channels.append(tc)

        # Salon prive MJ : visible uniquement par le createur
        mj_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member:             discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        mj_channel = await guild.create_text_channel("mj-prive", category=cat, overwrites=mj_overwrites)
        category_mj_chan[cat.id] = mj_channel.id

        # Permissions createur sur les vocaux : peut muter et mettre en sourdine
        creator_vc_ow = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            mute_members=True,
            deafen_members=True,
        )

        voice_list = []
        for name in VOICE_CHANNELS:
            vc = await guild.create_voice_channel(
                name,
                category=cat,
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    member: creator_vc_ow,
                }
            )
            voice_list.append(vc)

        if text_channels:
            category_texts[cat.id] = text_channels[0].id

        try:
            await member.move_to(voice_list[0])
            active_members[cat.id].add(member.id)
        except Exception as e:
            print(f"[BOT] Deplacement impossible : {e}")

        # Message bot dans le salon MJ prive
        view = DeleteCategoryView(cat, member)
        await mj_channel.send(
            f"Bienvenue {member.mention} !\n"
            f"**Commandes disponibles :**\n"
            f"Inviter un joueur : `/join @Pseudo`\n"
            f"Les autres peuvent demander a regarder via <#{SPECTATE_CHANNEL_ID}>",
            view=view
        )

        await post_spectate_message(guild, cat)
        print(f"[BOT] Categorie {cat_name} creee pour {member.display_name}")
        return

    # 2. Rejoint un vocal d'une session dynamique
    if after.channel and after.channel.category:
        cat = after.channel.category
        if is_dynamic_category(cat):
            # Ignore si c'est juste le bot qui applique un mute (before et after = meme salon)
            just_mute_change = (
                before.channel and before.channel.id == after.channel.id
                and before.mute != after.mute
            )
            if just_mute_change:
                return

            active_members[cat.id].add(member.id)
            inactive_since.pop(cat.id, None)

            # Auto server-mute si spectateur ET il vient de null ou d'un salon hors categorie
            is_spectator = member.id in category_spectators.get(cat.id, set())
            coming_from_outside = (
                before.channel is None or
                before.channel.category_id != cat.id
            )
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
            # Ignore si c'est juste un changement de mute/deafen sans changer de salon
            if after.channel and after.channel.id == before.channel.id:
                return

            active_members[cat.id].discard(member.id)

            # Si c'etait un spectateur mute par le bot, on le demute en partant
            is_spectator = member.id in category_spectators.get(cat.id, set())
            was_muted = before.mute
            if is_spectator and was_muted:
                try:
                    await member.edit(mute=False)
                except Exception:
                    pass

            if count_members_in_category(cat) == 0:
                await delete_spectate_message(member.guild, cat.id)
                inactive_since[cat.id] = datetime.utcnow()
                print(f"[BOT] {cat.name} vide — timer {INACTIVITY_MINUTES} min demarre")
            else:
                await update_spectate_message(member.guild, cat)




# ─────────────────────────────────────────────────────────
#  Surveillance des forums
# ─────────────────────────────────────────────────────────

@bot.event
async def on_thread_create(thread: discord.Thread):
    """Detecte un nouveau post dans les forums surveilles."""
    parent_id = thread.parent_id
    if parent_id not in (FORUM_OS_ID, FORUM_CAMPAGNE_ID):
        return

    guild = thread.guild
    annonce_channel = guild.get_channel(ANNONCE_CHANNEL_ID)
    if not annonce_channel:
        return

    # Type de partie
    is_os = (parent_id == FORUM_OS_ID)
    type_label = "One-Shot" if is_os else "Campagne"
    type_emoji = "⚔️" if is_os else "📜"

    # Attend un court instant pour que les tags soient charges
    await discord.utils.sleep_until(
        datetime.utcnow().replace(microsecond=0).__class__.utcnow()
    )
    # Recupere le thread avec ses tags
    try:
        thread = await guild.fetch_channel(thread.id)
    except Exception:
        pass

    # Recupere les roles a pinger depuis les tags du post
    applied_tags = getattr(thread, 'applied_tags', [])
    roles_to_ping = []
    for tag in applied_tags:
        role_name = TAG_TO_ROLE.get(tag.name)
        if role_name:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                roles_to_ping.append(role)

    ping_str = " ".join(r.mention for r in roles_to_ping) if roles_to_ping else ""

    # Auteur du post
    author = thread.owner
    if not author:
        try:
            author = await guild.fetch_member(thread.owner_id)
        except Exception:
            author = None

    desc = (
        f"**Type :** {type_label}\n"
        f"**Auteur :** {author.mention if author else 'Inconnu'}\n"
        f"**Voir le post :** {thread.mention}"
    )
    embed = discord.Embed(
        title=f"{type_emoji} Nouveau post : {thread.name}",
        description=desc,
        color=discord.Color.blue() if is_os else discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    if applied_tags:
        embed.add_field(
            name="Tags",
            value=" • ".join(t.name for t in applied_tags),
            inline=False
        )

    # Ajoute le ping @Joueur en plus des pings de tags
    joueur_role = discord.utils.get(guild.roles, name="Joueur")
    joueur_ping = joueur_role.mention if joueur_role else ""
    full_ping   = f"{joueur_ping} {ping_str}".strip() if ping_str else joueur_ping
    await annonce_channel.send(content=full_ping or None, embed=embed)
    print(f"[BOT] Annonce forum : {thread.name} ({type_label})")


# ─────────────────────────────────────────────────────────
#  Systeme de calendrier JDR
# ─────────────────────────────────────────────────────────

@dataclass
class Session:
    id:          str
    mj_id:       int
    game:        str
    starts_at:   str          # ISO format UTC
    player_ids:  list[int]    = field(default_factory=list)
    created:     bool         = False
    cancelled:   bool         = False
    cal_msg_id:  Optional[int] = None  # ID du message dans le salon calendrier


def sessions_load() -> list[Session]:
    try:
        with open(SESSIONS_FILE, "r") as f:
            data = json.load(f)
        return [Session(**s) for s in data]
    except Exception:
        return []

def sessions_save(sessions: list[Session]):
    with open(SESSIONS_FILE, "w") as f:
        json.dump([asdict(s) for s in sessions], f, indent=2)

def sessions_upcoming(sessions: list[Session]) -> list[Session]:
    now = datetime.utcnow()
    return sorted(
        [s for s in sessions if not s.cancelled and not s.created
         and datetime.fromisoformat(s.starts_at) > now],
        key=lambda s: s.starts_at
    )


def build_session_embed(guild: discord.Guild, session: Session) -> discord.Embed:
    mj      = guild.get_member(session.mj_id)
    players = [guild.get_member(uid) for uid in session.player_ids]
    players = [m for m in players if m]

    dt = datetime.fromisoformat(session.starts_at)
    # Discord timestamp for local time display
    ts = int(dt.timestamp())

    embed = discord.Embed(
        title=f"🎲 {session.game}",
        color=discord.Color.purple(),
        timestamp=dt
    )
    embed.add_field(name="MJ",      value=mj.mention if mj else "Inconnu",         inline=True)
    embed.add_field(name="Date",    value=f"<t:{ts}:F>",                            inline=True)
    embed.add_field(name="Dans",    value=f"<t:{ts}:R>",                            inline=True)
    if players:
        embed.add_field(
            name="Joueurs",
            value="\n".join(f"• {m.display_name}" for m in players),
            inline=False
        )
    embed.set_footer(text=f"ID session : {session.id[:8]}")
    return embed


JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

def get_week_bounds() -> tuple[datetime, datetime]:
    """Retourne le debut (lundi 00:00 UTC) et la fin (dimanche 23:59 UTC) de la semaine courante."""
    now     = datetime.utcnow()
    monday  = now - timedelta(days=now.weekday(), hours=now.hour, minutes=now.minute, seconds=now.second, microseconds=now.microsecond)
    sunday  = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return monday, sunday


def build_calendar_embed(guild: discord.Guild, sessions: list[Session]) -> discord.Embed:
    monday, sunday = get_week_bounds()

    # Sessions de la semaine (non annulees)
    week_sessions = [
        s for s in sessions
        if not s.cancelled
        and monday <= datetime.fromisoformat(s.starts_at) <= sunday
    ]
    week_sessions.sort(key=lambda s: s.starts_at)

    ts_monday = int(monday.timestamp())
    ts_sunday = int(sunday.timestamp())

    embed = discord.Embed(
        title="📅 Agenda de la semaine",
        description=f"<t:{ts_monday}:D> — <t:{ts_sunday}:D>",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )

    # Groupe par jour
    days: dict[int, list[Session]] = {i: [] for i in range(7)}  # 0=lundi
    for s in week_sessions:
        dt      = datetime.fromisoformat(s.starts_at)
        weekday = dt.weekday()
        days[weekday].append(s)

    has_any = False
    for day_idx in range(7):
        day_sessions = days[day_idx]
        if not day_sessions:
            continue
        has_any = True
        lines = []
        for s in day_sessions:
            mj      = guild.get_member(s.mj_id)
            dt      = datetime.fromisoformat(s.starts_at)
            ts      = int(dt.timestamp())
            players = [guild.get_member(uid) for uid in s.player_ids]
            players = [m for m in players if m]
            player_str = ", ".join(m.display_name for m in players) or "Aucun"
            status  = "✅" if s.created else "🕐"
            lines.append(
                f"{status} **{s.game}** — <t:{ts}:t>\n"
                f"MJ : {mj.display_name if mj else '?'}  |  Joueurs : {player_str}\n"
                f"`ID : {s.id[:8]}`"
            )
        embed.add_field(
            name=f"📆 {JOURS_FR[day_idx]}",
            value="\n\n".join(lines),
            inline=False
        )

    if not has_any:
        embed.add_field(name="Cette semaine", value="*Aucune partie planifiee.*", inline=False)

    embed.set_footer(text="Mis a jour")
    return embed


async def refresh_calendar(guild: discord.Guild, sessions: list[Session]):
    """Met a jour le message epingle de l agenda hebdomadaire."""
    channel = guild.get_channel(CALENDAR_CHANNEL_ID)
    if not channel:
        return
    embed = build_calendar_embed(guild, sessions)
    # Cherche un message existant du bot a editer
    async for msg in channel.history(limit=20):
        if msg.author == bot.user and msg.embeds:
            try:
                await msg.edit(embed=embed)
                return
            except Exception:
                pass
    # Aucun message : en cree un nouveau et l epingle
    msg = await channel.send(embed=embed)
    try:
        await msg.pin()
    except Exception:
        pass


# Tache : rafraichit l'agenda toutes les heures
@tasks.loop(hours=1)
async def refresh_weekly_calendar():
    sessions = sessions_load()
    for guild in bot.guilds:
        await refresh_calendar(guild, sessions)
    print("[BOT] Agenda rafraichi")


# Tache : cree les tables 10 min avant la partie
@tasks.loop(minutes=1)
async def check_sessions():
    sessions = sessions_load()
    now      = datetime.utcnow()
    modified = False

    for session in sessions:
        if session.cancelled or session.created:
            continue
        starts = datetime.fromisoformat(session.starts_at)
        delta  = (starts - now).total_seconds()

        if 0 <= delta <= 600:  # dans les 10 prochaines minutes
            session.created = True
            modified = True

            for guild in bot.guilds:
                mj = guild.get_member(session.mj_id)
                if not mj:
                    continue

                ref_cat  = guild.get_channel(REFERENCE_CATEGORY_ID)
                position = ref_cat.position if ref_cat else 0
                cat_name = f"Session de {mj.display_name}"

                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    mj:                 discord.PermissionOverwrite(view_channel=True, speak=True, stream=True),
                }

                cat = await guild.create_category(cat_name, position=position, overwrites=overwrites)
                active_members[cat.id]      = set()
                category_creators[cat.id]   = mj.id
                category_spectators[cat.id] = set()
                category_players[cat.id]    = set()

                text_channels = []
                for name in TEXT_CHANNELS:
                    tc = await guild.create_text_channel(name, category=cat)
                    text_channels.append(tc)

                mj_overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    mj:                 discord.PermissionOverwrite(view_channel=True, send_messages=True),
                }
                mj_channel = await guild.create_text_channel("mj-prive", category=cat, overwrites=mj_overwrites)
                category_mj_chan[cat.id]  = mj_channel.id
                category_texts[cat.id]   = text_channels[0].id if text_channels else None

                creator_vc_ow = discord.PermissionOverwrite(
                    view_channel=True, connect=True, speak=True,
                    stream=True, mute_members=True, deafen_members=True,
                )
                voice_list = []
                for name in VOICE_CHANNELS:
                    vc = await guild.create_voice_channel(
                        name, category=cat,
                        overwrites={
                            guild.default_role: discord.PermissionOverwrite(view_channel=False),
                            mj: creator_vc_ow,
                        }
                    )
                    voice_list.append(vc)

                # Invite les joueurs
                for uid in session.player_ids:
                    player = guild.get_member(uid)
                    if player:
                        await grant_access(cat, player, spectator=False)
                        category_players[cat.id].add(uid)

                # Deplace le MJ dans le vocal
                try:
                    await mj.move_to(voice_list[0])
                    active_members[cat.id].add(mj.id)
                except Exception:
                    pass

                # Message dans mj-prive
                view = DeleteCategoryView(cat, mj)
                await mj_channel.send(
                    f"Ta session **{session.game}** commence dans moins de 10 minutes !\n"
                    f"Inviter un joueur : `/join @Pseudo`\n"
                    f"Les autres peuvent demander a regarder via <#{SPECTATE_CHANNEL_ID}>",
                    view=view
                )

                await post_spectate_message(guild, cat)
                # Met a jour le calendrier
                await refresh_calendar(guild, sessions)
                print(f"[BOT] Table auto-creee pour session {session.id[:8]} ({session.game})")

    # Nettoyage : supprime les sessions des semaines passees, garde la semaine en cours et le futur
    monday, _ = get_week_bounds()
    before    = len(sessions)
    sessions  = [
        s for s in sessions
        if datetime.fromisoformat(s.starts_at) >= monday
    ]
    if len(sessions) < before:
        modified = True
        print(f"[BOT] {before - len(sessions)} session(s) de semaines passees supprimee(s) du fichier")

    if modified:
        sessions_save(sessions)


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
    # Parse date et heure (Europe/Paris, gere automatiquement heure ete/hiver)
    try:
        dt_naive  = datetime.strptime(f"{date} {heure}", "%d/%m/%Y %H:%M")
        dt_paris  = dt_naive.replace(tzinfo=PARIS_TZ)
        dt_utc    = dt_paris.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        await interaction.response.send_message(
            "Format invalide. Utilise JJ/MM/AAAA et HH:MM\nEx: `/planifier D&D5e 25/12/2025 20:00`",
            ephemeral=True
        )
        return

    if dt_utc < datetime.utcnow():
        await interaction.response.send_message("Cette date est dans le passe !", ephemeral=True)
        return

    joueurs = [m for m in [joueur1, joueur2, joueur3, joueur4, joueur5] if m is not None]

    session = Session(
        id         = str(uuid.uuid4()),
        mj_id      = interaction.user.id,
        game       = jeu,
        starts_at  = dt_utc.isoformat(),
        player_ids = [m.id for m in joueurs],
    )

    sessions = sessions_load()
    sessions.append(session)
    sessions_save(sessions)

    await refresh_calendar(interaction.guild, sessions)

    ts = int(dt_utc.timestamp())
    player_str = ", ".join(m.mention for m in joueurs) if joueurs else "Aucun"
    await interaction.response.send_message(
        f"✅ Session planifiee !\n"
        f"**Jeu :** {jeu}\n"
        f"**Date :** <t:{ts}:F>\n"
        f"**Joueurs :** {player_str}\n"
        f"**ID :** `{session.id[:8]}`\n"
        f"La table sera creee automatiquement 10 min avant.",
        ephemeral=True
    )
    print(f"[BOT] Session planifiee : {jeu} par {interaction.user} le {date} {heure}")


@tree.command(name="annuler-session", description="Annule une session planifiee")
@app_commands.describe(session_id="Les 8 premiers caracteres de l'ID de la session")
async def annuler_session_command(interaction: discord.Interaction, session_id: str):
    sessions = sessions_load()
    session  = next((s for s in sessions if s.id.startswith(session_id) and s.mj_id == interaction.user.id), None)

    if not session:
        await interaction.response.send_message(
            "Session introuvable ou tu n'en es pas le MJ.", ephemeral=True
        )
        return

    if session.cancelled:
        await interaction.response.send_message("Cette session est deja annulee.", ephemeral=True)
        return

    if session.created:
        await interaction.response.send_message("Cette session a deja commence.", ephemeral=True)
        return

    session.cancelled = True
    sessions_save(sessions)
    await refresh_calendar(interaction.guild, sessions)

    # DM tous les joueurs pour les prevenir
    mj      = interaction.user
    dt      = datetime.fromisoformat(session.starts_at)
    ts      = int(dt.timestamp())
    dm_ok   = []
    dm_fail = []
    for uid in session.player_ids:
        player = interaction.guild.get_member(uid)
        if player:
            try:
                await player.send(
                    f"❌ La session **{session.game}** prevue le <t:{ts}:F> "
                    f"a ete annulee par **{mj.display_name}**."
                )
                dm_ok.append(player.display_name)
            except Exception:
                dm_fail.append(player.display_name if player else str(uid))

    recap = f"Session **{session.game}** annulee."
    if dm_ok:
        recap += f"\nMP envoye a : {', '.join(dm_ok)}"
    if dm_fail:
        recap += f"\nImpossible de contacter : {', '.join(dm_fail)} (MP fermes)"

    await interaction.response.send_message(recap, ephemeral=True)


@tree.command(name="calendrier", description="Affiche les sessions a venir")
async def calendrier_command(interaction: discord.Interaction):
    sessions = sessions_load()
    embed    = build_calendar_embed(interaction.guild, sessions)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─────────────────────────────────────────────────────────
#  Demarrage
# ─────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    tree.copy_global_to(guild=GUILD)
    await tree.sync(guild=GUILD)
    check_inactivity.start()
    check_sessions.start()
    refresh_weekly_calendar.start()
    print(f"[BOT] Connecte en tant que {bot.user}")
    print(f"[BOT] Commandes slash synchronisees")
    print(f"[BOT] Calendrier JDR actif")
    print(f"[BOT] Membres charges : {sum(g.member_count for g in bot.guilds)}")


import time

MAX_RETRIES = 5
retry_delay = 30  # secondes

for attempt in range(1, MAX_RETRIES + 1):
    try:
        bot.run(TOKEN)
        break  # sortie propre
    except discord.errors.HTTPException as e:
        if e.status == 429:
            print(f"[BOT] Rate limited par Discord (tentative {attempt}/{MAX_RETRIES}). Attente {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 600)  # backoff exponentiel, max 10 min
        else:
            raise
    except Exception as e:
        print(f"[BOT] Erreur inattendue : {e}")
        raise
