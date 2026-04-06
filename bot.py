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

intents = discord.Intents.default()
intents.voice_states    = True
intents.guilds          = True
intents.members         = True
intents.reactions       = True
intents.message_content = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
GUILD = discord.Object(id=GUILD_ID)

# Donnees en memoire
inactive_since:      dict[int, datetime]      = {}  # {cat_id: datetime}
active_members:      dict[int, set[int]]      = {}  # {cat_id: set(member_id)} — en vocal
category_creators:   dict[int, int]           = {}  # {cat_id: member_id}
spectate_messages:   dict[int, int]           = {}  # {message_id: cat_id}
category_texts:      dict[int, int]           = {}  # {cat_id: first_text_channel_id}
category_spectators: dict[int, set[int]]      = {}  # {cat_id: set(spectator_id)}
pending_spectators:  dict[tuple[int,int], int] = {} # {(cat_id, member_id): msg_id}


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

async def grant_access(cat: discord.CategoryChannel, member: discord.Member):
    ow = discord.PermissionOverwrite(view_channel=True)
    await cat.set_permissions(member, overwrite=ow)
    for ch in cat.channels:
        await ch.set_permissions(member, overwrite=ow)

async def delete_category(cat: discord.CategoryChannel):
    print(f"[BOT] Suppression de la categorie {cat.name}")
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


# ─────────────────────────────────────────────────────────
#  Gestion du message spectate
# ─────────────────────────────────────────────────────────

def build_spectate_embed(guild: discord.Guild, cat: discord.CategoryChannel) -> discord.Embed:
    creator_id = category_creators.get(cat.id)
    creator    = guild.get_member(creator_id) if creator_id else None

    # Membres en vocal
    voice_members = get_voice_members(cat)
    voice_list    = "\n".join(f"• {m.display_name}" for m in voice_members) or "*Personne*"

    # Spectateurs (acces accorde mais pas en vocal)
    voice_ids    = {m.id for m in voice_members}
    spec_ids     = category_spectators.get(cat.id, set())
    spec_members = [guild.get_member(uid) for uid in spec_ids if uid not in voice_ids]
    spec_members = [m for m in spec_members if m]
    spec_list    = "\n".join(f"• {m.display_name}" for m in spec_members) or "*Personne*"

    embed = discord.Embed(
        title=f"Session de {creator.display_name if creator else 'Inconnu'}",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="En vocal", value=voice_list, inline=True)
    embed.add_field(name="Spectateurs", value=spec_list, inline=True)
    embed.set_footer(text="Reagis avec l'oeil pour demander a regarder")
    return embed

async def post_spectate_message(guild: discord.Guild, cat: discord.CategoryChannel):
    channel = guild.get_channel(SPECTATE_CHANNEL_ID)
    if not channel:
        return
    embed = build_spectate_embed(guild, cat)
    msg   = await channel.send(embed=embed)
    await msg.add_reaction("👁️")
    spectate_messages[msg.id] = cat.id
    print(f"[BOT] Message spectate poste pour {cat.name}")

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
                print(f"[BOT] Erreur update message spectate: {e}")
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
            await interaction.response.send_message(
                "Seul le createur peut repondre.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await grant_access(self.cat, self.spectator)
        # Enregistre le spectateur
        if self.cat.id not in category_spectators:
            category_spectators[self.cat.id] = set()
        category_spectators[self.cat.id].add(self.spectator.id)
        pending_spectators.pop((self.cat.id, self.spectator.id), None)

        await interaction.response.edit_message(
            content=f"**{self.spectator.display_name}** peut maintenant voir la session !",
            view=None
        )
        # Met a jour le message spectate
        await update_spectate_message(interaction.guild, self.cat)

        try:
            await self.spectator.send(
                f"Ta demande de spectate pour **{self.cat.name}** a ete acceptee !"
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
            await self.spectator.send(
                f"Ta demande de spectate pour **{self.cat.name}** a ete refusee."
            )
        except Exception:
            pass

    async def on_timeout(self):
        pending_spectators.pop((self.cat.id, self.spectator.id), None)


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

    # Retire la reaction
    try:
        channel = guild.get_channel(SPECTATE_CHANNEL_ID)
        msg = await channel.fetch_message(payload.message_id)
        await msg.remove_reaction("👁️", spectator)
    except Exception:
        pass

    # Demande deja en cours ?
    if (cat_id, spectator.id) in pending_spectators:
        return

    creator_id = category_creators.get(cat_id)
    creator    = guild.get_member(creator_id) if creator_id else None
    if not creator:
        return

    first_text_id = category_texts.get(cat_id)
    first_text    = guild.get_channel(first_text_id) if first_text_id else None
    if not first_text:
        return

    view = SpectateApprovalView(cat, spectator, creator)
    approval_msg = await first_text.send(
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
    membre1="1er membre a inviter",
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
        await interaction.response.send_message(
            "Cette commande ne fonctionne que dans une session active.", ephemeral=True
        )
        return

    cat = channel.category
    if interaction.user.id != category_creators.get(cat.id):
        await interaction.response.send_message(
            "Seul le createur de cette session peut inviter des membres.", ephemeral=True
        )
        return

    membres = [m for m in [membre1, membre2, membre3, membre4, membre5] if m is not None]
    for m in membres:
        await grant_access(cat, m)
        if cat.id not in category_spectators:
            category_spectators[cat.id] = set()
        category_spectators[cat.id].add(m.id)

    await interaction.response.send_message(
        f"Acces accorde a : {' '.join(m.mention for m in membres)}"
    )
    # Met a jour le message spectate
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
            member:             discord.PermissionOverwrite(view_channel=True),
        }

        cat = await guild.create_category(cat_name, position=position, overwrites=overwrites)
        active_members[cat.id]      = set()
        category_creators[cat.id]   = member.id
        category_spectators[cat.id] = set()

        text_channels = []
        for name in TEXT_CHANNELS:
            tc = await guild.create_text_channel(name, category=cat)
            text_channels.append(tc)

        voice_list = []
        for name in VOICE_CHANNELS:
            vc = await guild.create_voice_channel(name, category=cat)
            voice_list.append(vc)

        if text_channels:
            category_texts[cat.id] = text_channels[0].id

        try:
            await member.move_to(voice_list[0])
            active_members[cat.id].add(member.id)
        except Exception as e:
            print(f"[BOT] Deplacement impossible : {e}")

        if text_channels:
            await text_channels[0].send(
                f"Session creee par {member.mention} !\n"
                f"La categorie est privee.\n"
                f"Pour inviter directement : `/join @Pseudo`\n"
                f"Les autres peuvent demander a regarder via <#{SPECTATE_CHANNEL_ID}>"
            )

        await post_spectate_message(guild, cat)
        print(f"[BOT] Categorie {cat_name} creee pour {member.display_name}")
        return

    # 2. Rejoint un vocal d'une session dynamique
    if after.channel and after.channel.category:
        cat = after.channel.category
        if is_dynamic_category(cat):
            active_members[cat.id].add(member.id)
            inactive_since.pop(cat.id, None)
            await update_spectate_message(member.guild, cat)

    # 3. Quitte un vocal d'une session dynamique
    if before.channel and before.channel.category:
        cat = before.channel.category
        if is_dynamic_category(cat):
            active_members[cat.id].discard(member.id)
            if count_members_in_category(cat) == 0:
                # Supprime le message spectate immediatement
                await delete_spectate_message(member.guild, cat.id)
                # Demarre le timer d'inactivite
                inactive_since[cat.id] = datetime.utcnow()
                print(f"[BOT] {cat.name} vide — message supprime, timer {INACTIVITY_MINUTES} min demarre")
            else:
                await update_spectate_message(member.guild, cat)


# ─────────────────────────────────────────────────────────
#  Demarrage
# ─────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    tree.copy_global_to(guild=GUILD)
    await tree.sync(guild=GUILD)
    check_inactivity.start()
    print(f"[BOT] Connecte en tant que {bot.user}")
    print(f"[BOT] Commandes slash synchronisees")
    print(f"[BOT] Membres charges : {sum(g.member_count for g in bot.guilds)}")


bot.run(TOKEN)
