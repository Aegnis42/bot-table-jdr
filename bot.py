import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import os
import re

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
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# {category_id: datetime}       — timer d'inactivité
inactive_since: dict[int, datetime] = {}

# {category_id: set(member_id)} — membres connectés dans les vocaux
active_members: dict[int, set[int]] = {}

# {category_id: member_id}      — créateur de la catégorie
category_creators: dict[int, int] = {}


# ──────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────

def is_dynamic_category(category: discord.CategoryChannel) -> bool:
    return category.id in active_members


def voice_channels_of(category: discord.CategoryChannel) -> list[discord.VoiceChannel]:
    return [c for c in category.channels if isinstance(c, discord.VoiceChannel)]


def count_members_in_category(category: discord.CategoryChannel) -> int:
    return sum(len(vc.members) for vc in voice_channels_of(category))


async def delete_category(category: discord.CategoryChannel):
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
    category_creators.pop(category.id, None)


async def add_member_visibility(category: discord.CategoryChannel, member: discord.Member):
    """Donne la visibilité de la catégorie et de tous ses salons à un membre."""
    overwrite = discord.PermissionOverwrite(view_channel=True)
    await category.set_permissions(member, overwrite=overwrite)
    for channel in category.channels:
        await channel.set_permissions(member, overwrite=overwrite)


# ──────────────────────────────────────────────────────────
#  Tâche de fond – vérifie l'inactivité toutes les minutes
# ──────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def check_inactivity():
    now = datetime.utcnow()
    threshold = timedelta(minutes=INACTIVITY_MINUTES)
    to_delete = [cat_id for cat_id, since in list(inactive_since.items())
                 if now - since >= threshold]

    for cat_id in to_delete:
        for guild in bot.guilds:
            cat = guild.get_channel(cat_id)
            if cat and isinstance(cat, discord.CategoryChannel):
                if count_members_in_category(cat) == 0:
                    await delete_category(cat)
                else:
                    inactive_since.pop(cat_id, None)


# ──────────────────────────────────────────────────────────
#  Commande slash /join
# ──────────────────────────────────────────────────────────

@tree.command(name="join", description="Invite des membres à voir ta session")
@app_commands.describe(membres="Mentionne les membres à inviter : @Pseudo1 @Pseudo2 ...")
async def join_command(interaction: discord.Interaction, membres: str):
    channel = interaction.channel

    # Vérifie que la commande est dans une catégorie dynamique
    if not channel.category or not is_dynamic_category(channel.category):
        await interaction.response.send_message(
            "❌ Cette commande ne fonctionne que dans une session active.", ephemeral=True
        )
        return

    category = channel.category

    # Vérifie que c'est le créateur qui utilise la commande
    creator_id = category_creators.get(category.id)
    if interaction.user.id != creator_id:
        await interaction.response.send_message(
            "❌ Seul le créateur de cette session peut inviter des membres.", ephemeral=True
        )
        return

    # Extrait les IDs depuis les mentions (<@123456> ou <@!123456>)
    mentioned_ids = re.findall(r"<@!?(\d+)>", membres)

    if not mentioned_ids:
        await interaction.response.send_message(
            "❌ Mentionne au moins un membre : `/join @Pseudo1 @Pseudo2`", ephemeral=True
        )
        return

    guild = interaction.guild
    invited = []
    not_found = []

    for uid in mentioned_ids:
        member = guild.get_member(int(uid))
        if member:
            await add_member_visibility(category, member)
            invited.append(member.display_name)
        else:
            not_found.append(f"<@{uid}>")

    lines = []
    if invited:
        lines.append(f"✅ Accès accordé à : **{', '.join(invited)}**")
    if not_found:
        lines.append(f"⚠️ Membres introuvables : {', '.join(not_found)}")

    await interaction.response.send_message("\n".join(lines))
    print(f"[BOT] /join : {interaction.user} a invité {invited} dans « {category.name} »")


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
        category_name = f"🎮 Session de {member.display_name}"

        ref_category = guild.get_channel(REFERENCE_CATEGORY_ID)
        position = ref_category.position if ref_category else 0

        nouveau_role = discord.utils.get(guild.roles, name="Nouveau")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                mention_everyone=False,
            ),
        }
        if nouveau_role:
            overwrites[nouveau_role] = discord.PermissionOverwrite(view_channel=False)
        else:
            print("[BOT] ⚠️ Rôle 'Nouveau' introuvable — vérifier le nom exact du rôle")

        category = await guild.create_category(category_name, position=position, overwrites=overwrites)
        active_members[category.id] = set()
        category_creators[category.id] = member.id  # ← enregistre le créateur

        for name in TEXT_CHANNELS:
            await guild.create_text_channel(name, category=category)

        voice_list = []
        for name in VOICE_CHANNELS:
            vc = await guild.create_voice_channel(name, category=category)
            voice_list.append(vc)

        try:
            await member.move_to(voice_list[0])
            active_members[category.id].add(member.id)
        except Exception as e:
            print(f"[BOT] Impossible de déplacer {member}: {e}")

        # Message d'accueil avec instructions dans le 1er salon texte
        first_text = discord.utils.get(category.channels, name=TEXT_CHANNELS[0])
        if first_text:
            await first_text.send(
                f"👋 Session créée par **{member.display_name}** !\n"
                f"Pour inviter des membres ayant le rôle **Nouveau**, utilise dans n'importe quel salon de cette catégorie :\n"
                f"```\n/join @Pseudo1 @Pseudo2 ...\n```"
            )

        print(f"[BOT] Catégorie « {category_name} » créée pour {member.display_name}")
        return

    # ── 2. Quelqu'un rejoint un vocal d'une catégorie dynamique ─
    if after.channel and after.channel.category:
        cat = after.channel.category
        if is_dynamic_category(cat):
            active_members[cat.id].add(member.id)
            inactive_since.pop(cat.id, None)

    # ── 3. Quelqu'un quitte un vocal d'une catégorie dynamique ──
    if before.channel and before.channel.category:
        cat = before.channel.category
        if is_dynamic_category(cat):
            active_members[cat.id].discard(member.id)
            if count_members_in_category(cat) == 0:
                inactive_since[cat.id] = datetime.utcnow()
                print(f"[BOT] Catégorie « {cat.name} » vide — timer {INACTIVITY_MINUTES} min démarré")


# ──────────────────────────────────────────────────────────
#  Démarrage
# ──────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await tree.sync()
    check_inactivity.start()
    print(f"[BOT] Connecté en tant que {bot.user} ✅")
    print(f"[BOT] Commandes slash synchronisées ✅")
    print(f"[BOT] Surveillance du salon vocal ID : {TRIGGER_CHANNEL_ID}")


bot.run(TOKEN)

