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
SPECTATE_CHANNEL_ID   = 1489613853594484866 # salon où les tables en cours s'affichent
# Noms des channels créés dans chaque catégorie dynamique
TEXT_CHANNELS  = ["𝗟𝗲-𝗯𝗮𝘇𝗮𝗿", "𝗟𝗮𝗻𝗰𝗲́𝗲-𝗱𝗲-𝗱𝗲́𝘀", "𝗣𝗮𝗿𝘁𝗮𝗴𝗲-𝗿𝗲𝘀𝘀𝗼𝘂𝗿𝗰𝗲𝘀"]
VOICE_CHANNELS = ["𝗩𝗼𝗰𝗮𝗹", "𝗣𝗿𝗶𝘃𝗲𝗿 𝗠𝗝"]

INACTIVITY_MINUTES    = 60
REFERENCE_CATEGORY_ID = 1455416092141813864
GUILD_ID              = 1455403810888617996  # nouvelle catégorie créée juste au-dessus de celle-ci
# ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds       = True
intents.members      = True
intents.reactions    = True
intents.message_content = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
GUILD = discord.Object(id=GUILD_ID)

# ── Données en mémoire ─────────────────────────────────────
inactive_since:    dict[int, datetime]  = {}  # {cat_id: datetime}
active_members:    dict[int, set[int]]  = {}  # {cat_id: set(member_id)}
category_creators: dict[int, int]       = {}  # {cat_id: member_id}
spectate_messages: dict[int, int]       = {}  # {message_id: cat_id}
category_texts:    dict[int, int]       = {}  # {cat_id: first_text_channel_id}

# Demandes en attente de validation : {(cat_id, spectator_id): approval_message_id}
pending_spectators: dict[tuple[int,int], int] = {}


# ──────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────

def is_dynamic_category(cat: discord.CategoryChannel) -> bool:
    return cat.id in active_members

def voice_channels_of(cat: discord.CategoryChannel) -> list[discord.VoiceChannel]:
    return [c for c in cat.channels if isinstance(c, discord.VoiceChannel)]

def count_members_in_category(cat: discord.CategoryChannel) -> int:
    return sum(len(vc.members) for vc in voice_channels_of(cat))

async def grant_access(cat: discord.CategoryChannel, member: discord.Member):
    ow = discord.PermissionOverwrite(view_channel=True)
    await cat.set_permissions(member, overwrite=ow)
    for ch in cat.channels:
        await ch.set_permissions(member, overwrite=ow)

async def post_spectate_message(guild: discord.Guild, cat: discord.CategoryChannel):
    """Poste ou met à jour le message de la table dans le salon spectate."""
    channel = guild.get_channel(SPECTATE_CHANNEL_ID)
    if not channel:
        return
    creator_id = category_creators.get(cat.id)
    creator    = guild.get_member(creator_id) if creator_id else None
    creator_name = creator.display_name if creator else "Inconnu"

    embed = discord.Embed(
        title=f"🎮 {cat.name}",
        description=(
            f"**Créateur :** {creator.mention if creator else creator_name}\n"
            f"**Statut :** En cours 🟢\n\n"
            f"Réagis avec 👁️ pour demander à regarder cette session !"
        ),
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    msg = await channel.send(embed=embed)
    await msg.add_reaction("👁️")
    spectate_messages[msg.id] = cat.id
    print(f"[BOT] Message spectate posté (msg_id={msg.id}) pour « {cat.name} »")
    return msg

async def delete_spectate_message(guild: discord.Guild, cat_id: int):
    """Supprime le message de la table dans le salon spectate."""
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


# ──────────────────────────────────────────────────────────
#  Vue : boutons Accepter / Refuser pour le créateur
# ──────────────────────────────────────────────────────────

class SpectateApprovalView(discord.ui.View):
    def __init__(self, cat: discord.CategoryChannel, spectator: discord.Member, creator: discord.Member):
        super().__init__(timeout=300)  # expire après 5 min
        self.cat       = cat
        self.spectator = spectator
        self.creator   = creator

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.creator.id:
            await interaction.response.send_message(
                "❌ Seul le créateur de la session peut répondre.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Accepter", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await grant_access(self.cat, self.spectator)
        pending_spectators.pop((self.cat.id, self.spectator.id), None)
        await interaction.response.edit_message(
            content=f"✅ **{self.spectator.display_name}** peut maintenant voir la session !",
            view=None
        )
        # Notifie le spectateur
        try:
            await self.spectator.send(
                f"✅ Ta demande de spectate pour **{self.cat.name}** a été **acceptée** !"
            )
        except Exception:
            pass
        print(f"[BOT] Spectate accepté : {self.spectator} → « {self.cat.name} »")

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger)
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_spectators.pop((self.cat.id, self.spectator.id), None)
        await interaction.response.edit_message(
            content=f"❌ Demande de **{self.spectator.display_name}** refusée.",
            view=None
        )
        try:
            await self.spectator.send(
                f"❌ Ta demande de spectate pour **{self.cat.name}** a été **refusée**."
            )
        except Exception:
            pass
        print(f"[BOT] Spectate refusé : {self.spectator} → « {self.cat.name} »")

    async def on_timeout(self):
        pending_spectators.pop((self.cat.id, self.spectator.id), None)


# ──────────────────────────────────────────────────────────
#  Événement : réaction sur le salon spectate
# ──────────────────────────────────────────────────────────

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Ignore les réactions du bot lui-même
    if payload.user_id == bot.user.id:
        return

    # Vérifie que c'est bien dans le salon spectate et avec l'emoji 👁️
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

    # Évite les demandes en double
    if (cat_id, spectator.id) in pending_spectators:
        try:
            channel = guild.get_channel(SPECTATE_CHANNEL_ID)
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction("👁️", spectator)
        except Exception:
            pass
        return

    # Retire la réaction pour garder le message propre
    try:
        channel = guild.get_channel(SPECTATE_CHANNEL_ID)
        msg = await channel.fetch_message(payload.message_id)
        await msg.remove_reaction("👁️", spectator)
    except Exception:
        pass

    creator_id = category_creators.get(cat_id)
    creator    = guild.get_member(creator_id) if creator_id else None
    if not creator:
        return

    # Envoie la demande dans le 1er salon texte de la session
    first_text_id = category_texts.get(cat_id)
    first_text    = guild.get_channel(first_text_id) if first_text_id else None
    if not first_text:
        return

    view = SpectateApprovalView(cat, spectator, creator)
    approval_msg = await first_text.send(
        f"👁️ {creator.mention} — **{spectator.display_name}** demande à regarder ta session !",
        view=view
    )

    pending_spectators[(cat_id, spectator.id)] = approval_msg.id
    print(f"[BOT] Demande spectate : {spectator} → « {cat.name} » (créateur: {creator})")


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
                    await delete_spectate_message(guild, cid)
                    await delete_category(cat)
                else:
                    inactive_since.pop(cid, None)

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
    category_texts.pop(cat.id, None)


# ──────────────────────────────────────────────────────────
#  Commande /join
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
    if not channel.category or not is_dynamic_category(channel.category):
        await interaction.response.send_message(
            "❌ Cette commande ne fonctionne que dans une session active.", ephemeral=True
        )
        return

    cat = channel.category
    if interaction.user.id != category_creators.get(cat.id):
        await interaction.response.send_message(
            "❌ Seul le créateur de cette session peut inviter des membres.", ephemeral=True
        )
        return

    membres = [m for m in [membre1, membre2, membre3, membre4, membre5] if m is not None]
    for m in membres:
        await grant_access(cat, m)

    await interaction.response.send_message(
        f"✅ Accès accordé à : {' '.join(m.mention for m in membres)}"
    )


# ──────────────────────────────────────────────────────────
#  Événement vocal
# ──────────────────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after:  discord.VoiceState):

    # ── 1. Rejoint le salon déclencheur ───────────────────────
    if after.channel and after.channel.id == TRIGGER_CHANNEL_ID:
        guild    = member.guild
        cat_name = f"🎮 Session de {member.display_name}"

        ref_cat  = guild.get_channel(REFERENCE_CATEGORY_ID)
        position = ref_cat.position if ref_cat else 0

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member:             discord.PermissionOverwrite(view_channel=True),
        }

        cat = await guild.create_category(cat_name, position=position, overwrites=overwrites)
        active_members[cat.id]    = set()
        category_creators[cat.id] = member.id

        text_channels = []
        for name in TEXT_CHANNELS:
            tc = await guild.create_text_channel(name, category=cat)
            text_channels.append(tc)

        voice_list = []
        for name in VOICE_CHANNELS:
            vc = await guild.create_voice_channel(name, category=cat)
            voice_list.append(vc)

        # Enregistre le 1er salon texte pour les demandes de spectate
        if text_channels:
            category_texts[cat.id] = text_channels[0].id

        try:
            await member.move_to(voice_list[0])
            active_members[cat.id].add(member.id)
        except Exception as e:
            print(f"[BOT] Déplacement impossible : {e}")

        # Message d'accueil
        if text_channels:
            await text_channels[0].send(
                f"👋 Session créée par {member.mention} !\n"
                f"La catégorie est **privée**.\n"
                f"• Pour inviter directement : `/join @Pseudo`\n"
                f"• Les autres peuvent demander à regarder via le salon <#{SPECTATE_CHANNEL_ID}>"
            )

        # Poste la table dans le salon spectate
        await post_spectate_message(guild, cat)

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

@bot.event
async def on_ready():
    tree.copy_global_to(guild=GUILD)
    await tree.sync(guild=GUILD)
    check_inactivity.start()
    print(f"[BOT] Connecté en tant que {bot.user} ✅")
    print(f"[BOT] Commandes slash synchronisées ✅")
    print(f"[BOT] Membres chargés : {sum(g.member_count for g in bot.guilds)}")


bot.run(TOKEN)
