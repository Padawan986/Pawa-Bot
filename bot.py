import discord
from discord.ext import commands, tasks
from discord import app_commands
import yt_dlp
import asyncio
import json
import os
import random
import requests
import base64
from PIL import Image, ImageFilter, ImageDraw, ImageOps
import io
from dotenv import load_dotenv
from datetime import datetime, timedelta
from aiohttp import web

# --- .env DATEI LADEN ---
load_dotenv()

# --- PYTHON 3.12+ EVENT LOOP FIX ---
try:
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
except Exception as e:
    print(f"Event Loop Setup Warning: {e}")

# --- BOT SETUP ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# ==========================================
# --- HELPER: CLEAN EMBED ---
# ==========================================
def clean_embed(title=None, description=None):
    """Creates an embed that blends seamlessly into the dark Discord background."""
    return discord.Embed(title=title, description=description, color=discord.Color.from_str("#2B2D31"))

# ==========================================
# --- OWNER WHITELIST ---
# ==========================================
OWNER_IDS = [1216316535006691348, 1304449108177588286]

def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in OWNER_IDS:
            raise app_commands.CheckFailure()
        return True
    return app_commands.check(predicate)

# ==========================================
# --- MULTI-SERVER JSON DATENBANK & GITHUB ---
# ==========================================
db_dirty = False
data = {}

def load_db():
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    if not token or not repo: return
    try:
        url = f"https://api.github.com/repos/{repo}/contents/"
        headers = {"Authorization": f"token {token}"}
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            for file in r.json():
                if file["name"].endswith("-db.json"):
                    file_content = requests.get(file["download_url"]).json()
                    data.update(file_content)
            print(f"Loaded {len(data)} server databases from GitHub!")
    except Exception as e:
        print(f"Error loading from GitHub: {e}")

def save_and_sync():
    global db_dirty
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    if not token or not repo: return
    try:
        for guild_id, guild_data in data.items():
            guild = bot.get_guild(int(guild_id))
            if guild:
                safe_name = "".join(c for c in guild.name if c.isalnum() or c in (' ', '-', '_')).rstrip().replace(" ", "_")
                filename = f"{safe_name}-db.json"
            else:
                filename = f"{guild_id}-db.json"
            
            content = json.dumps({guild_id: guild_data}, indent=4)
            with open(filename, "w") as f:
                f.write(content)
                
            api_url = f"https://api.github.com/repos/{repo}/contents/{filename}"
            headers = {"Authorization": f"token {token}"}
            r_get = requests.get(api_url, headers=headers)
            sha = r_get.json().get("sha") if r_get.status_code == 200 else None
            
            payload = {
                "message": f"Auto-Sync {filename}",
                "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
                "sha": sha
            }
            requests.put(api_url, headers=headers, json=payload)
        db_dirty = False
        print("All server databases synced to GitHub!")
    except Exception as e:
        print(f"GitHub Sync Error: {e}")

@tasks.loop(minutes=5)
async def backup_task():
    if db_dirty:
        save_and_sync()

# --- MUSIK SETUP ---
ytdl_format_options = {
    'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True,
    'default_search': 'auto', 'source_address': '0.0.0.0', 'noplaylist': False
}
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}
ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')

    @classmethod
    async def from_url(cls, search, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        if "open.spotify.com/track/" in search:
            try:
                oembed_url = f"https://open.spotify.com/oembed?url={search}"
                r = requests.get(oembed_url)
                if r.status_code == 200:
                    track_title = r.json().get("title")
                    artist = r.json().get("artist_name") or r.json().get("provider_name")
                    search = f"ytsearch:{track_title} {artist}"
            except Exception: pass

        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search, download=not stream))
        if 'entries' in data:
            sources = []
            for entry in data['entries']:
                if entry:
                    filename = entry['url'] if stream else ytdl.prepare_filename(entry)
                    sources.append(cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=entry))
            return sources
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

queues = {}
def check_queue(guild_id, channel):
    if guild_id in queues and queues[guild_id]:
        next_source = queues[guild_id].pop(0)
        guild = bot.get_guild(guild_id)
        if guild and guild.voice_client:
            guild.voice_client.play(next_source, after=lambda e: check_queue(guild_id, channel))
            embed = clean_embed(description=f"Now playing: **{next_source.title}**")
            asyncio.run_coroutine_threadsafe(channel.send(embed=embed), bot.loop)

# ==========================================
# --- KEEP ALIVE WEB SERVER (FÜR RENDER) ---
# ==========================================
async def handle(request):
    return web.Response(text="Bot is online!")
app = web.Application()
app.add_routes([web.get('/', handle)])

async def start_webserver():
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Keep-Alive webserver started on port {port}.")

# ==========================================
# --- APPEAL SYSTEM ---
# ==========================================
class AppealModal(discord.ui.Modal, title='Submit Appeal'):
    def __init__(self, guild_id, action_type, reason, user_id):
        super().__init__()
        self.guild_id = guild_id
        self.action_type = action_type
        self.reason = reason
        self.user_id = user_id

    antwort = discord.ui.TextInput(label='Why should we lift the punishment?', style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        guild = bot.get_guild(self.guild_id)
        if not guild: return
        appeals_channel = discord.utils.get(guild.text_channels, name="appeals")
        if not appeals_channel:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True)
            }
            for role in guild.roles:
                if role.permissions.manage_messages or role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True)
            appeals_channel = await guild.create_text_channel("appeals", overwrites=overwrites)
        
        embed = clean_embed(title="New Appeal")
        embed.add_field(name="User", value=f"<@{self.user_id}> ({self.user_id})", inline=False)
        embed.add_field(name="Punishment", value=self.action_type, inline=False)
        embed.add_field(name="Original Reason", value=self.reason, inline=False)
        embed.add_field(name="User's Appeal", value=self.antwort.value, inline=False)
        
        view = AppealDecisionView(self.user_id, self.action_type)
        await appeals_channel.send(embed=embed, view=view)
        
        resp_embed = clean_embed(description="Your appeal has been submitted successfully. The team will review it shortly.")
        await interaction.response.send_message(embed=resp_embed, ephemeral=True)

class DMAppealButton(discord.ui.Button):
    def __init__(self, guild_id, action_type, reason, user_id):
        super().__init__(label="Submit Appeal", style=discord.ButtonStyle.success, custom_id=f"appeal_{guild_id}_{action_type}")
        self.guild_id = guild_id
        self.action_type = action_type
        self.reason = reason
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        modal = AppealModal(self.guild_id, self.action_type, self.reason, self.user_id)
        await interaction.response.send_modal(modal)

class DMAppealView(discord.ui.View):
    def __init__(self, guild_id, action_type, reason, user_id):
        super().__init__(timeout=None)
        self.add_item(DMAppealButton(guild_id, action_type, reason, user_id))

class AppealDecisionView(discord.ui.View):
    def __init__(self, user_id, action_type):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.action_type = action_type

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in OWNER_IDS: return
        guild = interaction.guild
        user = await bot.fetch_user(self.user_id)
        try:
            if self.action_type == "ban": await guild.unban(user)
            elif self.action_type == "timeout":
                member = guild.get_member(self.user_id)
                if member: await member.timeout(None)
            channel = guild.system_channel or guild.text_channels[0]
            invite = await channel.create_invite(max_uses=1, unique=True)
            try: await user.send(f"Your appeal has been accepted! You can rejoin here: {invite.url}")
            except: pass
            
            embed = clean_embed(description=f"Appeal accepted by {interaction.user.mention}. User has been notified.")
            await interaction.response.edit_message(embed=embed, view=None)
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in OWNER_IDS: return
        user = await bot.fetch_user(self.user_id)
        try: await user.send("Your appeal has been rejected. The punishment remains in place.")
        except: pass
        
        embed = clean_embed(description=f"Appeal rejected by {interaction.user.mention}. User has been notified.")
        await interaction.response.edit_message(embed=embed, view=None)

# ==========================================
# --- EVENTS ---
# ==========================================
@bot.event
async def on_ready():
    print(f'MEGA BOT ONLINE: {bot.user.name}')
    load_db()
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} slash commands!')
    except Exception as e:
        print(f"Error syncing commands: {e}")
    bot.spam_cache = {}
    if not backup_task.is_running(): backup_task.start()
    bot.loop.create_task(start_webserver())

@bot.event
async def on_message(message):
    global db_dirty
    if message.author.bot or not message.guild: return

    guild_id = str(message.guild.id)
    user_id = str(message.author.id)
    
    if guild_id not in data: data[guild_id] = {}
    if "automod_settings" not in data[guild_id]: 
        data[guild_id]["automod_settings"] = {"anti_link": True, "anti_spam": True}
    
    settings = data[guild_id]["automod_settings"]

    # --- AUTO MODERATION ---
    if settings.get("anti_link", True):
        if "discord.gg" in message.content or "http://" in message.content or "https://" in message.content:
            if message.author.id not in OWNER_IDS:
                try:
                    await message.delete()
                    embed = clean_embed(description=f"{message.author.mention}, links are not allowed here.")
                    await message.channel.send(embed=embed, delete_after=3)
                except: pass

    if settings.get("anti_spam", True):
        user_msgs = bot.spam_cache.setdefault(message.author.id, [])
        user_msgs.append(datetime.now())
        if len([t for t in user_msgs if t > datetime.now() - timedelta(seconds=5)]) > 5:
            try:
                await message.author.timeout(timedelta(minutes=1), reason="Spam")
                embed = clean_embed(description=f"{message.author.mention} has been muted for spamming.")
                await message.channel.send(embed=embed, delete_after=5)
            except: pass

    # --- LEVELING & ECONOMY ---
    if "automod_settings" != user_id:
        if user_id not in data[guild_id]: data[guild_id][user_id] = {"balance": 0, "xp": 0, "level": 0, "warns": 0}
        
        user_data = data[guild_id][user_id]
        user_data["xp"] += random.randint(5, 15)
        user_data["balance"] += 1
        
        xp_needed = user_data["level"] * 100
        if user_data["xp"] >= xp_needed:
            user_data["level"] += 1
            user_data["xp"] -= xp_needed
            embed = clean_embed(description=f"{message.author.mention} has reached Level {user_data['level']}!")
            await message.channel.send(embed=embed)
        db_dirty = True

# ==========================================
# --- AUTOMOD CONFIGURATION (NUR OWNER) ---
# ==========================================
automod_group = app_commands.Group(name="automod", description="Configure AutoMod settings")

@automod_group.command(name="status", description="View current AutoMod settings")
@owner_only()
async def automod_status(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    settings = data.get(guild_id, {}).get("automod_settings", {"anti_link": True, "anti_spam": True})
    
    embed = clean_embed(title="AutoMod Status")
    embed.add_field(name="Anti-Link", value="Enabled" if settings.get("anti_link", True) else "Disabled", inline=True)
    embed.add_field(name="Anti-Spam", value="Enabled" if settings.get("anti_spam", True) else "Disabled", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@automod_group.command(name="toggle", description="Turn AutoMod features on or off")
@owner_only()
@app_commands.choices(feature=[
    app_commands.Choice(name="Anti-Link", value="anti_link"),
    app_commands.Choice(name="Anti-Spam", value="anti_spam")
])
@app_commands.choices(state=[
    app_commands.Choice(name="On", value="true"),
    app_commands.Choice(name="Off", value="false")
])
async def automod_toggle(interaction: discord.Interaction, feature: app_commands.Choice[str], state: app_commands.Choice[str]):
    global db_dirty
    guild_id = str(interaction.guild.id)
    if guild_id not in data: data[guild_id] = {}
    if "automod_settings" not in data[guild_id]: 
        data[guild_id]["automod_settings"] = {"anti_link": True, "anti_spam": True}
        
    data[guild_id]["automod_settings"][feature.value] = (state.value == "true")
    db_dirty = True
    
    embed = clean_embed(title="AutoMod Updated")
    embed.add_field(name="Feature", value=feature.name, inline=True)
    embed.add_field(name="New State", value=state.name, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==========================================
# --- ADMIN MODERATION (NUR OWNER) ---
# ==========================================
@bot.tree.command(name="ban", description="Bans a user")
@owner_only()
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    await interaction.response.defer()
    
    dm_embed = clean_embed(title=f"You have been banned from {interaction.guild.name}!")
    dm_embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    view = DMAppealView(interaction.guild_id, "ban", reason or "No reason", member.id)
    try: await member.send(embed=dm_embed, view=view)
    except: pass

    try:
        await member.ban(reason=reason)
        succ, unsucc = 1, 0
    except:
        succ, unsucc = 0, 1
        
    embed = clean_embed(title="Ban Executed")
    embed.add_field(name="Successful", value=str(succ), inline=True)
    embed.add_field(name="Unsuccessful", value=str(unsucc), inline=True)
    embed.add_field(name="Reason", value=reason or "No reason.", inline=False)
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="unban", description="Unbans a user by ID")
@owner_only()
async def unban(interaction: discord.Interaction, user_id: str, reason: str = None):
    await interaction.response.defer()
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user, reason=reason)
        succ, unsucc = 1, 0
    except:
        succ, unsucc = 0, 1
        
    embed = clean_embed(title="Unban Executed")
    embed.add_field(name="Successful", value=str(succ), inline=True)
    embed.add_field(name="Unsuccessful", value=str(unsucc), inline=True)
    embed.add_field(name="Reason", value=reason or "No reason.", inline=False)
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="kick", description="Kicks a user")
@owner_only()
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    await interaction.response.defer()
    
    dm_embed = clean_embed(title=f"You have been kicked from {interaction.guild.name}!")
    dm_embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    view = DMAppealView(interaction.guild_id, "kick", reason or "No reason", member.id)
    try: await member.send(embed=dm_embed, view=view)
    except: pass

    try:
        await member.kick(reason=reason)
        succ, unsucc = 1, 0
    except:
        succ, unsucc = 0, 1
        
    embed = clean_embed(title="Kick Executed")
    embed.add_field(name="Successful", value=str(succ), inline=True)
    embed.add_field(name="Unsuccessful", value=str(unsucc), inline=True)
    embed.add_field(name="Reason", value=reason or "No reason.", inline=False)
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="timeout", description="Times out a user")
@owner_only()
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = None):
    await interaction.response.defer()
    
    dm_embed = clean_embed(title=f"You have been timed out in {interaction.guild.name}!")
    dm_embed.add_field(name="Duration", value=f"{minutes} minutes", inline=False)
    dm_embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    view = DMAppealView(interaction.guild_id, "timeout", reason or "No reason", member.id)
    try: await member.send(embed=dm_embed, view=view)
    except: pass

    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        succ, unsucc = 1, 0
    except:
        succ, unsucc = 0, 1
        
    embed = clean_embed(title="Timeout Executed")
    embed.add_field(name="Successful", value=str(succ), inline=True)
    embed.add_field(name="Unsuccessful", value=str(unsucc), inline=True)
    embed.add_field(name="Duration", value=f"{minutes} minutes", inline=True)
    embed.add_field(name="Reason", value=reason or "No reason.", inline=False)
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="purge", description="Deletes messages")
@owner_only()
async def purge(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.purge(limit=amount)
    embed = clean_embed(description=f"Deleted {amount} messages.")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="warn", description="Warns a user")
@owner_only()
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    global db_dirty
    await interaction.response.defer()
    guild_id = str(interaction.guild.id)
    user_id = str(member.id)
    if guild_id not in data: data[guild_id] = {}
    if user_id not in data[guild_id]: data[guild_id][user_id] = {"balance": 0, "xp": 0, "level": 0, "warns": 0}
    data[guild_id][user_id]["warns"] += 1
    db_dirty = True
    try: 
        dm_embed = clean_embed(description=f"You have been warned in {interaction.guild.name}. Reason: {reason}")
        await member.send(embed=dm_embed)
    except: pass

    embed = clean_embed(title="Warning Issued")
    embed.add_field(name="Successful", value="1", inline=True)
    embed.add_field(name="Unsuccessful", value="0", inline=True)
    embed.add_field(name="Reason", value=reason or "No reason.", inline=False)
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="setnick", description="Changes a user's nickname")
@owner_only()
async def setnick(interaction: discord.Interaction, member: discord.Member, nick: str):
    await interaction.response.defer()
    try:
        await member.edit(nick=nick)
        embed = clean_embed(description=f"Nickname of {member} has been changed to {nick}.")
        await interaction.followup.send(embed=embed)
    except:
        embed = clean_embed(description="Missing permissions to change this nickname.")
        await interaction.followup.send(embed=embed)

# ==========================================
# --- ADMIN ABUSE REPORT (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="adminabuse", description="Report an admin for abuse")
async def adminabuse(interaction: discord.Interaction, member: discord.Member, reason: str):
    await interaction.response.defer(ephemeral=True)
    
    guild = interaction.guild
    reports_channel = discord.utils.get(guild.text_channels, name="admin-reports")
    
    if not reports_channel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True)
        }
        for role in guild.roles:
            if role.permissions.manage_guild or role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True)
        reports_channel = await guild.create_text_channel("admin-reports", overwrites=overwrites)
        
    embed = clean_embed(title="Admin Abuse Report")
    embed.add_field(name="Reported Admin", value=f"{member.mention} ({member.name})", inline=False)
    embed.add_field(name="Reported by", value=interaction.user.mention, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    
    await reports_channel.send(embed=embed)
    
    confirm_embed = clean_embed(description="Your report has been submitted to the administration team. They will review it shortly.")
    await interaction.followup.send(embed=confirm_embed, ephemeral=True)

# ==========================================
# --- ROLLEN VERWALTUNG (NUR OWNER) ---
# ==========================================
role_group = app_commands.Group(name="role", description="Manages roles")

@role_group.command(name="add", description="Adds a role to a user")
@owner_only()
async def role_add(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    try:
        await member.add_roles(role)
        embed = clean_embed(description=f"Added {role.name} to {member.name}.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except:
        embed = clean_embed(description="Error (Role higher than bot?)")
        await interaction.followup.send(embed=embed, ephemeral=True)

@role_group.command(name="remove", description="Removes a role from a user")
@owner_only()
async def role_remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    try:
        await member.remove_roles(role)
        embed = clean_embed(description=f"Removed {role.name} from {member.name}.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except:
        embed = clean_embed(description="Error")
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="create-role", description="Creates a role with presets and hierarchy")
@owner_only()
@app_commands.choices(preset=[
    app_commands.Choice(name="Admin (All permissions)", value="admin"),
    app_commands.Choice(name="Moderator (Kick, Mute, Manage)", value="moderator"),
    app_commands.Choice(name="Member (Standard)", value="member"),
    app_commands.Choice(name="Muted (Muted)", value="muted")
])
@app_commands.choices(position=[
    app_commands.Choice(name="Bottom", value="bottom"),
    app_commands.Choice(name="Middle", value="middle"),
    app_commands.Choice(name="Top (below bot)", value="top")
])
async def create_role(interaction: discord.Interaction, name: str, preset: app_commands.Choice[str] = None, position: app_commands.Choice[str] = None, color: str = None):
    await interaction.response.defer(ephemeral=True)
    
    role_color = discord.Color.default()
    if color:
        try: role_color = discord.Color(int(color.replace("#", ""), 16))
        except: 
            embed = clean_embed(description="Invalid color! Please use a hex code (e.g., `FF0000` for red).")
            return await interaction.followup.send(embed=embed, ephemeral=True)
    
    try: new_role = await interaction.guild.create_role(name=name, color=role_color, reason=f"Created by {interaction.user}")
    except Exception as e: 
        embed = clean_embed(description=f"Error creating role: {e}")
        return await interaction.followup.send(embed=embed, ephemeral=True)
        
    permissions = discord.Permissions.none()
    preset_val = preset.value if preset else "member"
    
    if preset_val == "admin":
        permissions.administrator = True
    elif preset_val == "moderator":
        permissions.view_channel = True; permissions.send_messages = True; permissions.read_message_history = True
        permissions.manage_messages = True; permissions.kick_members = True; permissions.moderate_members = True
        permissions.manage_channels = True; permissions.view_audit_log = True
    elif preset_val == "member":
        permissions.view_channel = True; permissions.send_messages = True; permissions.read_message_history = True
        permissions.connect = True; permissions.speak = True
    elif preset_val == "muted":
        permissions.view_channel = True; permissions.read_message_history = True
        permissions.send_messages = False; permissions.add_reactions = False; permissions.connect = False; permissions.speak = False
            
    try: await new_role.edit(permissions=permissions)
    except: pass
            
    pos_val = position.value if position else "bottom"
    try:
        bot_member = interaction.guild.me
        bot_highest_pos = bot_member.top_role.position
        if pos_val == "top": await new_role.edit(position=bot_highest_pos - 1)
        elif pos_val == "middle": await new_role.edit(position=max(1, bot_highest_pos // 2))
    except: pass
            
    embed = clean_embed(title="Role Created")
    embed.add_field(name="Name", value=name, inline=True)
    embed.add_field(name="Preset", value=preset_val, inline=True)
    embed.add_field(name="Position", value=pos_val, inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ==========================================
# --- MUSIC (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="play", description="Plays music (YouTube, Spotify, SoundCloud)")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        embed = clean_embed(description="You must be in a voice channel!")
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    await interaction.response.defer()
    if interaction.guild.voice_client is None:
        await interaction.user.voice.channel.connect()
    elif interaction.guild.voice_client.channel != interaction.user.voice.channel:
        embed = clean_embed(description="I am already in another channel!")
        return await interaction.followup.send(embed=embed)

    try:
        result = await YTDLSource.from_url(query, loop=bot.loop)
        if isinstance(result, list):
            if not result: 
                embed = clean_embed(description="Could not find any songs.")
                return await interaction.followup.send(embed=embed)
            first_song = result.pop(0)
            queues.setdefault(interaction.guild.id, []).extend(result)
            if interaction.guild.voice_client.is_playing():
                embed = clean_embed(description=f"Playlist added: **{len(result)+1} songs** in the queue!")
                await interaction.followup.send(embed=embed)
            else:
                interaction.guild.voice_client.play(first_song, after=lambda e: check_queue(interaction.guild.id, interaction.channel))
                embed = clean_embed(description=f"Now playing: **{first_song.title}**\nAdded {len(result)} more songs to the queue.")
                await interaction.followup.send(embed=embed)
        else:
            if interaction.guild.voice_client.is_playing():
                queues.setdefault(interaction.guild.id, []).append(result)
                embed = clean_embed(description=f"Added to queue: **{result.title}**")
                await interaction.followup.send(embed=embed)
            else:
                interaction.guild.voice_client.play(result, after=lambda e: check_queue(interaction.guild.id, interaction.channel))
                embed = clean_embed(description=f"Now playing: **{result.title}**")
                await interaction.followup.send(embed=embed)
    except Exception as e:
        embed = clean_embed(description=f"Error playing song: {e}")
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="skip", description="Skips the current song")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.stop()
        embed = clean_embed(description="Song skipped.")
        await interaction.response.send_message(embed=embed)
    else: 
        embed = clean_embed(description="No music is playing!")
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="stop", description="Stops the music")
async def stop(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        queues[interaction.guild.id] = []
        await interaction.guild.voice_client.disconnect()
        embed = clean_embed(description="Music stopped. Goodbye!")
        await interaction.response.send_message(embed=embed)
    else: 
        embed = clean_embed(description="I am not in a voice channel.")
        await interaction.response.send_message(embed=embed)

# ==========================================
# --- ECONOMY (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="balance", description="Shows your balance")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    user_data = data.get(str(interaction.guild.id), {}).get(str(member.id), {"balance": 0})
    embed = clean_embed(title="Balance")
    embed.add_field(name="User", value=member.mention, inline=True)
    embed.add_field(name="Coins", value=str(user_data["balance"]), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="daily", description="Claim your daily coins")
async def daily(interaction: discord.Interaction):
    global db_dirty
    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    if guild_id not in data: data[guild_id] = {}
    if user_id not in data[guild_id]: data[guild_id][user_id] = {"balance": 0, "xp": 0, "level": 0, "warns": 0}
    data[guild_id][user_id]["balance"] += 500
    db_dirty = True
    embed = clean_embed(description="You have claimed your 500 daily coins!")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="gamble", description="Gamble your coins")
async def gamble(interaction: discord.Interaction, amount: int):
    global db_dirty
    if amount <= 0: 
        embed = clean_embed(description="Amount must be greater than 0.")
        return await interaction.response.send_message(embed=embed)
    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    if guild_id not in data: data[guild_id] = {}
    if user_id not in data[guild_id]: data[guild_id][user_id] = {"balance": 0, "xp": 0, "level": 0, "warns": 0}
    if data[guild_id][user_id]["balance"] < amount: 
        embed = clean_embed(description="You do not have enough coins.")
        return await interaction.response.send_message(embed=embed)
    if random.randint(1, 2) == 1:
        data[guild_id][user_id]["balance"] += amount
        embed = clean_embed(description=f"You won {amount*2} coins!")
        await interaction.response.send_message(embed=embed)
    else:
        data[guild_id][user_id]["balance"] -= amount
        embed = clean_embed(description="You lost everything.")
        await interaction.response.send_message(embed=embed)
    db_dirty = True

# ==========================================
# --- LEVELING & STATS (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="rank", description="Shows your level")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    user_data = data.get(str(interaction.guild.id), {}).get(str(member.id), {"xp": 0, "level": 0})
    embed = clean_embed(title=f"Rank of {member.name}")
    embed.add_field(name="Level", value=str(user_data["level"]), inline=True)
    embed.add_field(name="XP", value=str(user_data["xp"]), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Top 5 server members")
async def leaderboard(interaction: discord.Interaction):
    guild_data = data.get(str(interaction.guild.id), {})
    user_items = [(k, v) for k, v in guild_data.items() if k.isdigit()]
    sorted_users = sorted(user_items, key=lambda x: x[1].get("level", 0), reverse=True)[:5]
    
    embed = clean_embed(title="Leaderboard")
    if not sorted_users:
        embed.description = "No data available yet."
    for i, (user_id, udata) in enumerate(sorted_users, 1):
        member = interaction.guild.get_member(int(user_id))
        name = member.name if member else "Unknown"
        embed.add_field(name=f"#{i} {name}", value=f"Level {udata.get('level', 0)} | {udata.get('xp', 0)} XP", inline=False)
    await interaction.response.send_message(embed=embed)

# ==========================================
# --- IMAGE MANIPULATION (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="image", description="Manipulate a profile picture")
@app_commands.choices(effect=[
    app_commands.Choice(name="Blur", value="blur"),
    app_commands.Choice(name="Invert", value="invert"),
    app_commands.Choice(name="Greyscale", value="greyscale")
])
async def image(interaction: discord.Interaction, member: discord.Member = None, effect: app_commands.Choice[str] = None):
    member = member or interaction.user
    effect_val = effect.value if effect else "blur"
    await interaction.response.defer()
    response = requests.get(member.display_avatar.url)
    img = Image.open(io.BytesIO(response.content))
    if effect_val == "blur": img = img.filter(ImageFilter.BLUR)
    elif effect_val == "invert":
        if img.mode == 'RGBA':
            r,g,b,a = img.split()
            inverted = ImageOps.invert(Image.merge('RGB', (r,g,b)))
            img = Image.merge('RGBA', inverted.split()+(a,))
        else: img = ImageOps.invert(img)
    elif effect_val == "greyscale": img = img.convert('L')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    
    embed = clean_embed()
    embed.set_image(url="attachment://manipulated.png")
    await interaction.followup.send(embed=embed, file=discord.File(buf, filename='manipulated.png'))

# ==========================================
# --- FUN & UTILITY (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="meme", description="Shows a random meme")
async def meme(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        r = requests.get("https://meme-api.com/gimme")
        if r.status_code == 200:
            embed = clean_embed(title=r.json()['title'])
            embed.set_image(url=r.json()['url'])
            await interaction.followup.send(embed=embed)
    except: 
        embed = clean_embed(description="Could not load a meme right now.")
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="hug", description="Hug someone")
async def hug(interaction: discord.Interaction, member: discord.Member):
    embed = clean_embed(description=f"{interaction.user.mention} hugged {member.mention}!")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="weather", description="Shows the weather in Celsius")
async def weather(interaction: discord.Interaction, city: str):
    try:
        r = requests.get(f"https://wttr.in/{city}?format=%l:+%c+%t+%w&lang=en&m")
        embed = clean_embed(title=f"Weather for {city}", description=r.text)
        await interaction.response.send_message(embed=embed)
    except: 
        embed = clean_embed(description="Could not fetch weather data.")
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="avatar", description="Shows a user's avatar")
async def avatar(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = clean_embed(title=f"Avatar of {member.name}")
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# ==========================================
# --- SUGGESTIONS & TICKETS ---
# ==========================================
@bot.tree.command(name="suggest", description="Make a suggestion")
async def suggest(interaction: discord.Interaction, suggestion: str):
    embed = clean_embed(title="New Suggestion", description=suggestion)
    embed.set_author(name=interaction.user.name, icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

class TicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        channel = await guild.create_text_channel(f'ticket-{interaction.user.name}', overwrites=overwrites)
        embed = clean_embed(description=f"Welcome to your ticket {interaction.user.mention}. A team member will assist you shortly.")
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)

@bot.tree.command(name="ticket", description="Creates a ticket panel")
@owner_only()
async def ticket(interaction: discord.Interaction):
    embed = clean_embed(title="Support Tickets", description="Click the button below to open a ticket!")
    await interaction.response.send_message(embed=embed, view=TicketView())

# ==========================================
# --- GIVEAWAYS (NUR OWNER) ---
# ==========================================
class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.participants = []

    @discord.ui.button(label="Enter Giveaway", style=discord.ButtonStyle.primary, custom_id="enter_giveaway")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.participants:
            self.participants.append(interaction.user.id)
            await interaction.response.send_message("You have entered the giveaway!", ephemeral=True)
        else:
            await interaction.response.send_message("You have already entered!", ephemeral=True)

@bot.tree.command(name="gstart", description="Starts a giveaway")
@owner_only()
async def gstart(interaction: discord.Interaction, minutes: int, prize: str):
    await interaction.response.defer()
    view = GiveawayView()
    embed = clean_embed(title="Giveaway", description=f"Prize: **{prize}**\nEnds in {minutes} minutes.\nClick the button to enter!")
    await interaction.followup.send(embed=embed, view=view)
    msg = await interaction.original_response()
    
    await asyncio.sleep(minutes * 60)
    msg = await interaction.channel.fetch_message(msg.id)
    
    if view.participants:
        winner_id = random.choice(view.participants)
        winner = await bot.fetch_user(winner_id)
        win_embed = clean_embed(title="Giveaway Ended", description=f"Winner: {winner.mention}\nPrize: **{prize}**")
        await interaction.channel.send(embed=win_embed)
    else:
        no_win_embed = clean_embed(title="Giveaway Ended", description="No one entered the giveaway.")
        await interaction.channel.send(embed=no_win_embed)

# ==========================================
# --- DASHBOARD & HELP (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="dashboard", description="Overview of all commands")
async def dashboard(interaction: discord.Interaction):
    embed = clean_embed(title="Server Dashboard", description="Overview of all slash commands")
    embed.add_field(name="AutoMod (Admins)", value="/automod status, /automod toggle", inline=False)
    embed.add_field(name="Moderation (Admins)", value="/ban, /unban, /kick, /timeout, /purge, /warn, /setnick", inline=False)
    embed.add_field(name="Roles (Admins)", value="/role add, /role remove, /create-role", inline=False)
    embed.add_field(name="Reporting", value="/adminabuse", inline=False)
    embed.add_field(name="Music", value="/play, /skip, /stop", inline=False)
    embed.add_field(name="Economy", value="/balance, /daily, /gamble", inline=False)
    embed.add_field(name="Leveling", value="/rank, /leaderboard", inline=False)
    embed.add_field(name="Fun & Utility", value="/image, /meme, /hug, /weather, /avatar", inline=False)
    embed.add_field(name="Server", value="/suggest, /ticket, /gstart", inline=False)
    await interaction.response.send_message(embed=embed)

# ==========================================
# --- ERROR HANDLER (STILL FÜR OWNER CHECK) ---
# ==========================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        return
    else:
        print(f"Slash Command Error: {error}")
        try:
            if interaction.response.is_done():
                embed = clean_embed(description="An error occurred.")
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                embed = clean_embed(description="An error occurred.")
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except: pass

# ==========================================
# --- BOT START ---
# ==========================================
bot.tree.add_command(role_group)
bot.tree.add_command(automod_group)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    print("ERROR: Token not found. Please ensure the environment variable is set on Render.")
else:
    bot.run(TOKEN)
