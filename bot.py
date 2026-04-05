import discord
from discord.ext import commands, tasks
import asyncio
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
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# {category_id: datetime de la dernière déconnexion}
inactive_since: dict[int, datetime] = {}

# {category_id: set(member_id)}  — membres actuellement dans les vocaux de la catégorie
active_members: dict[int, set[int]] = {}


# ──────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────

def is_dynamic_category(category: discord.CategoryChannel) -> bool:
    """Renvoie True si la catégorie est gérée par le bot."""
    return category.id in active_members


def voice_channels_of(category: discord.CategoryChannel) -> list[discord.VoiceChannel]:
    return [c for c in category.channels if isinstance(c, discord.VoiceChannel)]


def count_members_in_category(category: discord.CategoryChannel) -> int:
    total = 0
    for vc in voice_channels_of(category):
        total += len(vc.members)
    return total


async def delete_category(category: discord.CategoryChannel):
    """Supprime tous les salons puis la catégorie."""
    print(f"[BOT] Suppression de la catégorie « {category.name} »")
    for channel in category.channels:
        try:
            await channel.delete(reason="Catégorie inactive depuis 1 heure")
        except Exception as e:
            print(f"  Erreur suppression salon {channel.name}: {e}")
    try:
        await category.delete(reason="Catégorie inactive depuis 1 heure")
    except Exception as e:
        print(f"  Erreur suppression catégorie: {e}")
    inactive_since.pop(category.id, None)
    active_members.pop(category.id, None)


# ──────────────────────────────────────────────────────────
#  Tâche de fond – vérifie l'inactivité toutes les minutes
# ──────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def check_inactivity():
    now = datetime.utcnow()
    threshold = timedelta(minutes=INACTIVITY_MINUTES)

    to_delete = []
    for cat_id, since in list(inactive_since.items()):
        if now - since >= threshold:
            to_delete.append(cat_id)

    for cat_id in to_delete:
        for guild in bot.guilds:
            cat = guild.get_channel(cat_id)
            if cat and isinstance(cat, discord.CategoryChannel):
                # Double-vérification : personne dedans ?
                if count_members_in_category(cat) == 0:
                    await delete_category(cat)
                else:
                    # Quelqu'un est revenu, on annule le timer
                    inactive_since.pop(cat_id, None)


# ──────────────────────────────────────────────────────────
#  Événement : changement d'état vocal
# ──────────────────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):

    # ── 1. Quelqu'un rejoint le salon déclencheur ──────────────
    if after.channel and after.channel.id == TRIGGER_CHANNEL_ID:
        guild = member.guild
        category_name = f"Session de {member.display_name}"

        # Récupère la position de la catégorie de référence
        ref_category = guild.get_channel(REFERENCE_CATEGORY_ID)
        position = ref_category.position if ref_category else 0

        # Crée la catégorie juste au-dessus de la catégorie de référence
        category = await guild.create_category(category_name, position=position)
        active_members[category.id] = set()

        # Crée les 3 salons texte
        for name in TEXT_CHANNELS:
            await guild.create_text_channel(name, category=category)

        # Crée les 2 salons vocaux
        voice_list = []
        for name in VOICE_CHANNELS:
            vc = await guild.create_voice_channel(name, category=category)
            voice_list.append(vc)

        # Déplace la personne dans le 1er vocal
        try:
            await member.move_to(voice_list[0])
            active_members[category.id].add(member.id)
        except Exception as e:
            print(f"[BOT] Impossible de déplacer {member}: {e}")

        print(f"[BOT] Catégorie « {category_name} » créée pour {member.display_name}")
        return

    # ── 2. Quelqu'un rejoint un vocal d'une catégorie dynamique ─
    if after.channel and after.channel.category:
        cat = after.channel.category
        if is_dynamic_category(cat):
            active_members[cat.id].add(member.id)
            # Annule le timer d'inactivité s'il était en cours
            inactive_since.pop(cat.id, None)

    # ── 3. Quelqu'un quitte un vocal d'une catégorie dynamique ──
    if before.channel and before.channel.category:
        cat = before.channel.category
        if is_dynamic_category(cat):
            active_members[cat.id].discard(member.id)

            # Plus personne dans la catégorie → démarre le timer
            if count_members_in_category(cat) == 0:
                inactive_since[cat.id] = datetime.utcnow()
                print(f"[BOT] Catégorie « {cat.name} » vide — timer {INACTIVITY_MINUTES} min démarré")


# ──────────────────────────────────────────────────────────
#  Démarrage
# ──────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    check_inactivity.start()
    print(f"[BOT] Connecté en tant que {bot.user} ✅")
    print(f"[BOT] Surveillance du salon vocal ID : {TRIGGER_CHANNEL_ID}")


bot.run(TOKEN)
