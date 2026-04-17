import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import asyncio
import re
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CREATOR_ROLE_NAME = os.getenv("CREATOR_ROLE_NAME", "Creator")
COMMISSION_CATEGORY_ID = int(os.getenv("COMMISSION_CATEGORY_ID", 0))
GUILD_ID = int(os.getenv("GUILD_ID", 0))

# ─────────────────────────────────────────────
# Persistent state — survives restarts
#
# Structure:
# {
#   "locked": [creator_id, ...],
#   "listings": {
#     "<creator_id>": [
#       {"channel_id": int, "message_id": int},
#       ...  ← a creator can have multiple listings
#     ]
#   }
# }
# ─────────────────────────────────────────────
# Use /data if it exists (Railway persistent volume), otherwise local for dev
_DATA_DIR = "/data" if os.path.isdir("/data") else "."
STATE_FILE = os.path.join(_DATA_DIR, "commission_state.json")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return {
                "locked": set(data.get("locked", [])),
                # Support old "embed_registry" format by migrating it
                "listings": _migrate_listings(data),
            }
    return {"locked": set(), "listings": {}}


def _migrate_listings(data: dict) -> dict:
    """Migrate old single-entry embed_registry to new multi-listing format."""
    listings = data.get("listings", {})
    old_registry = data.get("embed_registry", {})
    for creator_id_str, entry in old_registry.items():
        if creator_id_str not in listings:
            listings[creator_id_str] = [entry]
    return listings


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "locked": list(state["locked"]),
                "listings": state["listings"],
            },
            f,
            indent=2,
        )


def register_listing(creator_id: int, channel_id: int, message_id: int):
    """Add a listing entry for a creator. Keeps all their listings."""
    key = str(creator_id)
    if key not in state["listings"]:
        state["listings"][key] = []
    state["listings"][key].append({
        "channel_id": channel_id,
        "message_id": message_id,
    })
    save_state()


def remove_listing(creator_id: int, message_id: int):
    """Remove a specific listing entry by message ID."""
    key = str(creator_id)
    if key in state["listings"]:
        state["listings"][key] = [
            e for e in state["listings"][key] if e["message_id"] != message_id
        ]
        if not state["listings"][key]:
            del state["listings"][key]
        save_state()


state = load_state()


# ─────────────────────────────────────────────
# Helper — rebuild the commission embed
# ─────────────────────────────────────────────
def build_commission_embed(
    creator: discord.Member,
    title: str,
    description: str,
    price: str,
    turnaround: str,
    portfolio_url: str | None,
    locked: bool,
) -> discord.Embed:
    if locked:
        color = 0xED4245
        status_line = "🔴 **Status: CLOSED** — Not accepting commissions right now."
    else:
        color = 0x57F287
        status_line = "🟢 **Status: OPEN** — Click below to commission!"

    embed = discord.Embed(
        title=f"🎨 {title}",
        description=f"{status_line}\n\n{description}",
        color=color,
    )
    embed.set_author(
        name=creator.display_name,
        icon_url=creator.display_avatar.url,
    )
    embed.add_field(name="💰 Pricing", value=price, inline=True)
    embed.add_field(name="⏱️ Turnaround", value=turnaround, inline=True)
    if portfolio_url:
        embed.add_field(name="🔗 Portfolio", value=f"[View here]({portfolio_url})", inline=False)
    embed.set_footer(text=f"creator_id:{creator.id}")
    return embed


# ─────────────────────────────────────────────
# Helper — extract fields from an existing embed
# ─────────────────────────────────────────────
def parse_embed_data(old_embed: discord.Embed) -> dict:
    raw_desc = old_embed.description or ""
    lines = raw_desc.split("\n\n", 1)
    description = lines[1] if len(lines) > 1 else raw_desc

    fields = {f.name: f.value for f in old_embed.fields}
    price = fields.get("💰 Pricing", "N/A")
    turnaround = fields.get("⏱️ Turnaround", "N/A")

    portfolio_url = None
    portfolio_field = fields.get("🔗 Portfolio")
    if portfolio_field:
        m = re.search(r'\((.+?)\)', portfolio_field)
        if m:
            portfolio_url = m.group(1)

    title = old_embed.title or "🎨 Commission"
    if title.startswith("🎨 "):
        title = title[len("🎨 "):]

    return {
        "title": title,
        "description": description,
        "price": price,
        "turnaround": turnaround,
        "portfolio_url": portfolio_url,
    }


# ─────────────────────────────────────────────
# Helper — refresh ALL of a creator's listing embeds
# ─────────────────────────────────────────────
async def refresh_all_listings(creator_id: int, locked: bool):
    """Edit every listing embed for this creator to reflect the new lock state."""
    key = str(creator_id)
    entries = state["listings"].get(key, [])
    if not entries:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    creator = guild.get_member(creator_id)
    if creator is None:
        return

    stale = []
    for entry in entries:
        try:
            channel = guild.get_channel(entry["channel_id"])
            if channel is None:
                channel = await guild.fetch_channel(entry["channel_id"])
            message = await channel.fetch_message(entry["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            stale.append(entry)
            continue

        if not message.embeds:
            stale.append(entry)
            continue

        data = parse_embed_data(message.embeds[0])
        new_embed = build_commission_embed(creator=creator, locked=locked, **data)
        view = ListingView(creator_id=creator_id, locked=locked)
        # Register with the bot before editing so Discord routes button clicks correctly
        bot.add_view(view, message_id=message.id)
        try:
            await message.edit(embed=new_embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            stale.append(entry)

    # Clean up any listings that no longer exist
    if stale:
        state["listings"][key] = [e for e in entries if e not in stale]
        if not state["listings"][key]:
            del state["listings"][key]
        save_state()


# ─────────────────────────────────────────────
# Helper — strip a member's access from a channel
# ─────────────────────────────────────────────
async def remove_member_from_channel(channel: discord.TextChannel, member: discord.Member):
    await channel.set_permissions(member, overwrite=None)


# ─────────────────────────────────────────────
# Intents & Bot setup
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ─────────────────────────────────────────────
# Listing view — Commission Me + Lock/Unlock toggle
# ─────────────────────────────────────────────
class ListingView(discord.ui.View):
    def __init__(self, creator_id: int, locked: bool):
        super().__init__(timeout=None)
        self.creator_id = creator_id

        toggle = discord.ui.Button(
            label="🟢 Open Commissions" if locked else "🔒 Close Commissions",
            style=discord.ButtonStyle.success if locked else discord.ButtonStyle.secondary,
            custom_id=f"toggle_commissions_{creator_id}",
            row=1,
        )
        toggle.callback = self.toggle_commissions
        self.add_item(toggle)

    @discord.ui.button(
        label="💼 Commission Me",
        style=discord.ButtonStyle.primary,
        custom_id="commission_me_button",
        row=0,
    )
    async def commission_me(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        guild = interaction.guild
        commissioner = interaction.user

        creator_id = None
        if interaction.message.embeds:
            footer = interaction.message.embeds[0].footer
            if footer and footer.text and footer.text.startswith("creator_id:"):
                try:
                    creator_id = int(footer.text.split(":")[1])
                except ValueError:
                    pass

        if creator_id is None:
            await interaction.response.send_message(
                "⚠️ Could not identify the creator. Please contact an admin.",
                ephemeral=True,
            )
            return

        if creator_id in state["locked"]:
            await interaction.response.send_message(
                "🔒 This creator is not accepting commissions right now.",
                ephemeral=True,
            )
            return

        creator = guild.get_member(creator_id)
        if creator is None:
            await interaction.response.send_message(
                "⚠️ The creator no longer seems to be in this server.",
                ephemeral=True,
            )
            return

        if commissioner.id == creator.id:
            await interaction.response.send_message(
                "❌ You can't commission yourself!", ephemeral=True
            )
            return

        category = guild.get_channel(COMMISSION_CATEGORY_ID)
        if category is None or not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name="Commissions")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            creator: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                embed_links=True, attach_files=True,
            ),
            commissioner: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                embed_links=True, attach_files=True,
            ),
            guild.me: discord.PermissionOverwrite(
                read_messages=True, send_messages=True,
                manage_channels=True, manage_permissions=True,
            ),
        }

        safe_name = (
            f"commission-{commissioner.name}-x-{creator.name}"
            .lower().replace(" ", "-")[:100]
        )

        try:
            channel = await guild.create_text_channel(
                name=safe_name,
                category=category,
                overwrites=overwrites,
                reason=f"Commission opened by {commissioner} for {creator}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to create channels. Please ask an admin to check my permissions.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📋 New Commission",
            description=(
                f"Hey {creator.mention}! "
                f"{commissioner.mention} has commissioned you and will be with you shortly.\n\n"
                "Use this channel to discuss project details, pricing, and deadlines.\n\n"
                "When you're done, click **Close Commission** below or use `/close_commission`."
            ),
            color=0x5865F2,
        )
        embed.set_footer(text=f"creator_id:{creator.id} commissioner_id:{commissioner.id}")

        close_view = CloseCommissionView()
        await channel.send(
            content=f"{creator.mention} {commissioner.mention}",
            embed=embed,
            view=close_view,
        )

        await interaction.response.send_message(
            f"✅ Your commission channel has been created: {channel.mention}",
            ephemeral=True,
        )

    async def toggle_commissions(self, interaction: discord.Interaction):
        creator_id = None
        if interaction.message.embeds:
            footer = interaction.message.embeds[0].footer
            if footer and footer.text and footer.text.startswith("creator_id:"):
                try:
                    creator_id = int(footer.text.split(":")[1])
                except ValueError:
                    pass

        if creator_id is None or interaction.user.id != creator_id:
            await interaction.response.send_message(
                "❌ Only the creator of this listing can use this button.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        creator = guild.get_member(creator_id)
        if creator is None:
            await interaction.response.send_message(
                "⚠️ Could not find your member data. Please try again.",
                ephemeral=True,
            )
            return

        currently_locked = creator_id in state["locked"]

        # Defer so we have time to edit all listing embeds
        await interaction.response.defer(ephemeral=True)

        if currently_locked:
            state["locked"].discard(creator_id)
            save_state()
            await refresh_all_listings(creator_id, locked=False)
            await interaction.followup.send(
                "🟢 Your commissions are now **open**! All your listings have been updated.",
                ephemeral=True,
            )
        else:
            state["locked"].add(creator_id)
            save_state()
            await refresh_all_listings(creator_id, locked=True)
            await interaction.followup.send(
                "🔒 Your commissions are now **closed**. All your listings have been updated.",
                ephemeral=True,
            )


# ─────────────────────────────────────────────
# Close button inside commission channel
# ─────────────────────────────────────────────
class CloseCommissionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label=" Close Commission",
        style=discord.ButtonStyle.danger,
        custom_id="close_commission_button",
    )
    async def close_commission(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        channel = interaction.channel
        guild = interaction.guild

        creator_id = None
        commissioner_id = None
        async for msg in channel.history(limit=10, oldest_first=True):
            if msg.author == guild.me and msg.embeds:
                footer_text = msg.embeds[0].footer.text or ""
                c = re.search(r'creator_id:(\d+)', footer_text)
                u = re.search(r'commissioner_id:(\d+)', footer_text)
                if c:
                    creator_id = int(c.group(1))
                if u:
                    commissioner_id = int(u.group(1))
                break

        for member_id in filter(None, [creator_id, commissioner_id]):
            member = guild.get_member(member_id)
            if member:
                try:
                    await remove_member_from_channel(channel, member)
                except discord.Forbidden:
                    pass

        button.disabled = True
        button.label = " Commission Closed"
        await interaction.message.edit(view=self)

        await interaction.response.send_message(
            f" Commission closed by {interaction.user.mention}. "
            "Both parties have been removed from this channel."
        )


# ─────────────────────────────────────────────
# Slash command: /post_commission
# ─────────────────────────────────────────────
@tree.command(name="post_commission", description="Post your commission embed in this forum channel.")
@app_commands.describe(
    title="Title of your commission post",
    description="Describe what you're offering",
    price="Your pricing (e.g. $10/hr, starts at $25, etc.)",
    turnaround="Estimated delivery time",
    portfolio_url="Link to your portfolio or examples (optional)",
)
async def post_commission(
    interaction: discord.Interaction,
    title: str,
    description: str,
    price: str,
    turnaround: str,
    portfolio_url: str = None,
):
    creator_role = discord.utils.get(interaction.guild.roles, name=CREATOR_ROLE_NAME)
    if creator_role is None or creator_role not in interaction.user.roles:
        await interaction.response.send_message(
            f" You need the **{CREATOR_ROLE_NAME}** role to post a commission listing.",
            ephemeral=True,
        )
        return

    # Defer immediately to avoid Discord's 3-second timeout
    await interaction.response.defer()

    locked = interaction.user.id in state["locked"]

    embed = build_commission_embed(
        creator=interaction.user,
        title=title,
        description=description,
        price=price,
        turnaround=turnaround,
        portfolio_url=portfolio_url,
        locked=locked,
    )

    view = ListingView(creator_id=interaction.user.id, locked=locked)

    # Send as a regular channel message (not a webhook followup) so that
    # message.edit() works correctly when refreshing the embed later.
    # We acknowledge the deferred interaction with a silent followup first.
    await interaction.followup.send(" Posting your listing…", ephemeral=True)
    channel = interaction.channel
    sent_message = await channel.send(embed=embed, view=view)

    # Register the view with the bot tied to this specific message
    bot.add_view(view, message_id=sent_message.id)

    # Register this listing — supports multiple listings per creator
    register_listing(
        creator_id=interaction.user.id,
        channel_id=interaction.channel_id,
        message_id=sent_message.id,
    )


# ─────────────────────────────────────────────
# Slash command: /close_commission (manual)
# ─────────────────────────────────────────────
@tree.command(name="close_commission", description="Close this commission — removes both parties' access.")
async def close_commission_cmd(interaction: discord.Interaction):
    channel = interaction.channel
    if not channel.name.startswith("commission-"):
        await interaction.response.send_message(
            " This command can only be used inside a commission channel.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    creator_id = None
    commissioner_id = None

    async for msg in channel.history(limit=10, oldest_first=True):
        if msg.author == guild.me and msg.embeds:
            footer_text = msg.embeds[0].footer.text or ""
            c = re.search(r'creator_id:(\d+)', footer_text)
            u = re.search(r'commissioner_id:(\d+)', footer_text)
            if c:
                creator_id = int(c.group(1))
            if u:
                commissioner_id = int(u.group(1))
            break

    for member_id in filter(None, [creator_id, commissioner_id]):
        member = guild.get_member(member_id)
        if member:
            try:
                await remove_member_from_channel(channel, member)
            except discord.Forbidden:
                pass

    await interaction.response.send_message(
        f" Commission closed by {interaction.user.mention}. "
        "Both parties have been removed from this channel."
    )


# ─────────────────────────────────────────────
# Slash command: /lock_commissions
# ─────────────────────────────────────────────
@tree.command(name="lock_commissions", description="Stop accepting new commissions on your listings.")
async def lock_commissions(interaction: discord.Interaction):
    creator_role = discord.utils.get(interaction.guild.roles, name=CREATOR_ROLE_NAME)
    if creator_role is None or creator_role not in interaction.user.roles:
        await interaction.response.send_message(
            f" You need the **{CREATOR_ROLE_NAME}** role to use this command.",
            ephemeral=True,
        )
        return

    if interaction.user.id in state["locked"]:
        await interaction.response.send_message(
            " Your commissions are already locked.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    state["locked"].add(interaction.user.id)
    save_state()
    await refresh_all_listings(interaction.user.id, locked=True)
    await interaction.followup.send(
        " Your commissions are now **locked**. All your listings have been updated.",
        ephemeral=True,
    )


# ─────────────────────────────────────────────
# Slash command: /unlock_commissions
# ─────────────────────────────────────────────
@tree.command(name="unlock_commissions", description="Start accepting commissions again on your listings.")
async def unlock_commissions(interaction: discord.Interaction):
    creator_role = discord.utils.get(interaction.guild.roles, name=CREATOR_ROLE_NAME)
    if creator_role is None or creator_role not in interaction.user.roles:
        await interaction.response.send_message(
            f" You need the **{CREATOR_ROLE_NAME}** role to use this command.",
            ephemeral=True,
        )
        return

    if interaction.user.id not in state["locked"]:
        await interaction.response.send_message(
            " Your commissions are already open.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    state["locked"].discard(interaction.user.id)
    save_state()
    await refresh_all_listings(interaction.user.id, locked=False)
    await interaction.followup.send(
        " Your commissions are now **open**. All your listings have been updated.",
        ephemeral=True,
    )


# ─────────────────────────────────────────────
# Slash command: /commission_status
# ─────────────────────────────────────────────
@tree.command(name="commission_status", description="Check whether your commissions are open or locked.")
async def commission_status(interaction: discord.Interaction):
    creator_role = discord.utils.get(interaction.guild.roles, name=CREATOR_ROLE_NAME)
    if creator_role is None or creator_role not in interaction.user.roles:
        await interaction.response.send_message(
            f" You need the **{CREATOR_ROLE_NAME}** role to use this command.",
            ephemeral=True,
        )
        return

    key = str(interaction.user.id)
    listing_count = len(state["listings"].get(key, []))

    if interaction.user.id in state["locked"]:
        await interaction.response.send_message(
            f" Your commissions are currently **locked** ({listing_count} active listing(s)). "
            "Use `/unlock_commissions` to open them.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f" Your commissions are currently **open** ({listing_count} active listing(s)). "
            "Use `/lock_commissions` to stop accepting new ones.",
            ephemeral=True,
        )


# ─────────────────────────────────────────────
# Bot events
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    bot.add_view(CloseCommissionView())

    # Re-register a ListingView for every stored listing so buttons survive restarts
    registered_creators = set()
    for creator_id_str in state["listings"]:
        creator_id = int(creator_id_str)
        if creator_id not in registered_creators:
            locked = creator_id in state["locked"]
            bot.add_view(ListingView(creator_id=creator_id, locked=locked))
            registered_creators.add(creator_id)

    g = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=g)
    await tree.sync(guild=g)

    print(f"   Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Creator role  : {CREATOR_ROLE_NAME}")
    print(f"   Category ID   : {COMMISSION_CATEGORY_ID}")
    print(f"   Guild ID      : {GUILD_ID}")
    print(f"   Listings loaded: {sum(len(v) for v in state['listings'].values())}")


if __name__ == "__main__":
    bot.run(TOKEN)
