import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import os


# ─────────────────────────────────────────────
#  CONFIG  –  modifie ces valeurs
# ─────────────────────────────────────────────
TOKEN = os.environ.get("TOKEN")         # token lu depuis les variables Railway
TRIGGER_CHANNEL_ID = 1490398403819995176  # ID du salon vocal déclencheur

# Noms des channels créés dans chaque catégorie dynamique
TEXT_CHANNELS  = ["𝗟𝗲-𝗯𝗮𝘇𝗮𝗿", "𝗟𝗮𝗻𝗰𝗲́𝗲-𝗱𝗲-𝗱𝗲́𝘀", "𝗣𝗮𝗿𝘁𝗮𝗴𝗲-𝗿𝗲𝘀𝘀𝗼𝘂𝗿𝗰𝗲𝘀"]
VOICE_CHANNELS = ["𝗩𝗼𝗰𝗮𝗹", "𝗣𝗿𝗶𝘃𝗲𝗿 𝗠𝗝"]

INACTIVITY_MINUTES = 1   # délai avant suppression (en minutes)
REFERENCE_CATEGORY_ID = 1455416092141813864  # nouvelle catégorie créée juste au-dessus de celle-ci
# ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds       = True
intents.members      = True               # nécessaire pour voir tous les membres

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

inactive_since:     dict[int, datetime]  = {}  # {category_id: datetime}
active_members:     dict[int, set[int]]  = {}  # {category_id: set(member_id)}
category_creators:  dict[int, int]       = {}  # {category_id: member_id}


# ──────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────

def is_dynamic_category(cat: discord.CategoryChannel) -> bool:
    return cat.id in active_members

def voice_channels_of(cat: discord.CategoryChannel) -> list[discord.VoiceChannel]:
    return [c for c in cat.channels if isinstance(c, discord.VoiceChannel)]

def count_members_in_category(cat: discord.CategoryChannel) -> int:
    return sum(len(vc.members) for vc in voice_channels_of(cat))

async def delete_category(cat: discord.CategoryChannel):
    print(f"[BOT] Suppression « {cat.name} »")
    for channel in cat.channels:
        try:    await channel.delete()
        except: pass
    try:    await cat.delete()
    except: pass
    inactive_since.pop(cat.id, None)
    active_members.pop(cat.id, None)
    category_creators.pop(cat.id, None)

async def grant_access(cat: discord.CategoryChannel, member: discord.Member):
    """Donne l'accès à la catégorie et tous ses salons."""
    ow = discord.PermissionOverwrite(view_channel=True)
    await cat.set_permissions(member, overwrite=ow)
    for ch in cat.channels:
        await ch.set_permissions(member, overwrite=ow)


# ──────────────────────────────────────────────────────────
#  Tâche de fond – inactivité
# ──────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────
#  Commande /join  — jusqu'à 5 membres avec sélecteur natif
# ──────────────────────────────────────────────────────────

@tree.command(name="join", description="Invite des membres à rejoindre ta session")
@app_commands.describe(
    membre1="1er membre à inviter",
    membre2="2ème membre (optionnel)",
    membre3="3ème membre (optionnel)",
    membre4="4ème membre (optionnel)",
    membre5="5ème membre (optionnel)",
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

    # Vérifie qu'on est dans une session dynamique
    if not channel.category or not is_dynamic_category(channel.category):
        await interaction.response.send_message(
            "❌ Cette commande ne fonctionne que dans une session active.", ephemeral=True
        )
        return

    cat = channel.category

    # Seul le créateur peut inviter
    if interaction.user.id != category_creators.get(cat.id):
        await interaction.response.send_message(
            "❌ Seul le créateur de cette session peut inviter des membres.", ephemeral=True
        )
        return

    membres = [m for m in [membre1, membre2, membre3, membre4, membre5] if m is not None]
    invited = []

    for m in membres:
        await grant_access(cat, m)
        invited.append(m.mention)

    await interaction.response.send_message(
        f"✅ Accès accordé à : {' '.join(invited)}"
    )
    print(f"[BOT] /join par {interaction.user} → {[m.display_name for m in membres]} dans « {cat.name} »")


# ──────────────────────────────────────────────────────────
#  Événement vocal
# ──────────────────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after:  discord.VoiceState):

    # ── 1. Rejoint le salon déclencheur ───────────────────────
    if after.channel and after.channel.id == TRIGGER_CHANNEL_ID:
        guild = member.guild
        cat_name = f"🎮 Session de {member.display_name}"

        ref_cat  = guild.get_channel(REFERENCE_CATEGORY_ID)
        position = ref_cat.position if ref_cat else 0

        # Catégorie invisible pour tout le monde par défaut
        # Seul le créateur a accès au départ
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member:             discord.PermissionOverwrite(view_channel=True),
        }

        cat = await guild.create_category(cat_name, position=position, overwrites=overwrites)
        active_members[cat.id]    = set()
        category_creators[cat.id] = member.id

        # Salons texte et vocaux héritent des permissions de la catégorie
        text_channels = []
        for name in TEXT_CHANNELS:
            tc = await guild.create_text_channel(name, category=cat)
            text_channels.append(tc)

        voice_list = []
        for name in VOICE_CHANNELS:
            vc = await guild.create_voice_channel(name, category=cat)
            voice_list.append(vc)

        # Déplace le créateur dans le 1er vocal
        try:
            await member.move_to(voice_list[0])
            active_members[cat.id].add(member.id)
        except Exception as e:
            print(f"[BOT] Déplacement impossible : {e}")

        # Message d'accueil
        if text_channels:
            await text_channels[0].send(
                f"👋 Session créée par {member.mention} !\n"
                f"La catégorie est **privée** — pour inviter quelqu'un :\n"
                f"```\n/join @Pseudo1 @Pseudo2 ...\n```"
            )

        print(f"[BOT] Catégorie « {cat_name} » créée pour {member.display_name}")
        return

    # ── 2. Rejoint un vocal d'une session dynamique ───────────
    if after.channel and after.channel.category:
        cat = after.channel.category
        if is_dynamic_category(cat):
            active_members[cat.id].add(member.id)
            inactive_since.pop(cat.id, None)

    # ── 3. Quitte un vocal d'une session dynamique ────────────
    if before.channel and before.channel.category:
        cat = before.channel.category
        if is_dynamic_category(cat):
            active_members[cat.id].discard(member.id)
            if count_members_in_category(cat) == 0:
                inactive_since[cat.id] = datetime.utcnow()
                print(f"[BOT] « {cat.name} » vide — timer {INACTIVITY_MINUTES} min démarré")


# ──────────────────────────────────────────────────────────
#  Démarrage
# ──────────────────────────────────────────────────────────

GUILD = discord.Object(id=1455403810888617996)

@bot.event
async def on_ready():
    tree.copy_global_to(guild=GUILD)
    await tree.sync(guild=GUILD)   # sync instantané sur le serveur
    check_inactivity.start()
    print(f"[BOT] Connecté en tant que {bot.user} ✅")
    print(f"[BOT] Commandes slash synchronisées instantanément ✅")
    print(f"[BOT] Membres chargés : {sum(g.member_count for g in bot.guilds)}")


bot.run(TOKEN)


