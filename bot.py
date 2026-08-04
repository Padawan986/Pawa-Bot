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
    print(f"Event Loop Setup Warnung: {e}")

# --- BOT SETUP ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# ==========================================
# --- OWNER WHITELIST (FÜR ADMIN CMDS) ---
# ==========================================
OWNER_IDS = [1216316535006691348, 1304449108177588286]

def owner_only():
    """Custom Check: Lässt den Befehl komplett stillschweigend scheitern, wenn der User nicht in der Owner-Liste steht."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in OWNER_IDS:
            raise app_commands.CheckFailure() # Bricht still ab
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
            print(f"✅ {len(data)} Server-Datenbanken von GitHub geladen!")
    except Exception as e:
        print(f"Fehler beim Laden von GitHub: {e}")

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
        print("✅ Alle Server-Datenbanken auf GitHub gesichert!")
    except Exception as e:
        print(f"GitHub Sync Fehler: {e}")

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
            asyncio.run_coroutine_threadsafe(channel.send(f'🎵 Spielt jetzt: **{next_source.title}**'), bot.loop)

# ==========================================
# --- KEEP ALIVE WEB SERVER (FÜR RENDER) ---
# ==========================================
async def handle(request):
    return web.Response(text="Bot ist online!")
app = web.Application()
app.add_routes([web.get('/', handle)])

async def start_webserver():
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Webserver für Keep-Alive auf Port {port} gestartet.")

# ==========================================
# --- APPEAL SYSTEM (EINSPRÜCHE) ---
# ==========================================
class AppealModal(discord.ui.Modal, title='Einspruch einlegen'):
    def __init__(self, guild_id, action_type, reason, user_id):
        super().__init__()
        self.guild_id = guild_id
        self.action_type = action_type
        self.reason = reason
        self.user_id = user_id

    antwort = discord.ui.TextInput(label='Warum sollen wir die Strafe aufheben?', style=discord.TextStyle.paragraph, max_length=500)

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
        
        embed = discord.Embed(title="🚨 Neuer Einspruch", color=discord.Color.orange())
        embed.add_field(name="User", value=f"<@{self.user_id}> ({self.user_id})", inline=False)
        embed.add_field(name="Strafe", value=self.action_type, inline=False)
        embed.add_field(name="Originalgrund", value=self.reason, inline=False)
        embed.add_field(name="Einspruch des Users", value=self.antwort.value, inline=False)
        
        view = AppealDecisionView(self.user_id, self.action_type)
        await appeals_channel.send(embed=embed, view=view)
        await interaction.response.send_message("Dein Einspruch wurde erfolgreich eingereicht! Das Team wird sich bald darum kümmern.", ephemeral=True)

class DMAppealButton(discord.ui.Button):
    def __init__(self, guild_id, action_type, reason, user_id):
        super().__init__(label="Einspruch einlegen", style=discord.ButtonStyle.success, custom_id=f"appeal_{guild_id}_{action_type}")
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

    @discord.ui.button(label="Annehmen", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in OWNER_IDS:
            return # Nur Owner dürfen annehmen
        guild = interaction.guild
        user = await bot.fetch_user(self.user_id)
        try:
            if self.action_type == "ban": await guild.unban(user)
            elif self.action_type == "timeout":
                member = guild.get_member(self.user_id)
                if member: await member.timeout(None)
            channel = guild.system_channel or guild.text_channels[0]
            invite = await channel.create_invite(max_uses=1, unique=True)
            try: await user.send(f"✅ Dein Einspruch wurde angenommen! Du kannst hier wieder joinen: {invite.url}")
            except: pass
            await interaction.response.edit_message(content=f"✅ Angenommen von {interaction.user.mention}. User wurde benachrichtigt.", view=None)
        except Exception as e:
            await interaction.response.send_message(f"Fehler: {e}", ephemeral=True)

    @discord.ui.button(label="Ablehnen", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in OWNER_IDS:
            return # Nur Owner dürfen ablehnen
        user = await bot.fetch_user(self.user_id)
        try: await user.send("❌ Dein Einspruch wurde abgelehnt. Die Strafe bleibt bestehen.")
        except: pass
        await interaction.response.edit_message(content=f"❌ Abgelehnt von {interaction.user.mention}. User wurde benachrichtigt.", view=None)

# ==========================================
# --- EVENTS ---
# ==========================================
@bot.event
async def on_ready():
    print(f'🟢 MEGA BOT ONLINE: {bot.user.name}')
    load_db()
    try:
        synced = await bot.tree.sync()
        print(f'✅ {len(synced)} Slash Commands synchronisiert!')
    except Exception as e:
        print(f"Fehler beim Syncen der Commands: {e}")
    bot.spam_cache = {}
    if not backup_task.is_running(): backup_task.start()
    bot.loop.create_task(start_webserver())

@bot.event
async def on_message(message):
    global db_dirty
    if message.author.bot or not message.guild: return

    if "discord.gg" in message.content or "http://" in message.content:
        if message.author.id not in OWNER_IDS: # Normale User dürfen keine Links
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} Keine Links erlaubt!", delete_after=3)
            except: pass

    user_msgs = bot.spam_cache.setdefault(message.author.id, [])
    user_msgs.append(datetime.now())
    if len([t for t in user_msgs if t > datetime.now() - timedelta(seconds=5)]) > 5:
        try:
            await message.author.timeout(timedelta(minutes=1), reason="Spam")
            await message.channel.send(f"{message.author.mention} wurde wegen Spam gemutet.", delete_after=5)
        except: pass

    # Leveling für ALLE User
    guild_id = str(message.guild.id)
    user_id = str(message.author.id)
    if guild_id not in data: data[guild_id] = {}
    if user_id not in data[guild_id]: data[guild_id][user_id] = {"balance": 0, "xp": 0, "level": 0, "warns": 0}
    
    user_data = data[guild_id][user_id]
    user_data["xp"] += random.randint(5, 15)
    user_data["balance"] += 1
    
    xp_needed = user_data["level"] * 100
    if user_data["xp"] >= xp_needed:
        user_data["level"] += 1
        user_data["xp"] -= xp_needed
        await message.channel.send(f"🎉 {message.author.mention} ist Level {user_data['level']} aufgestiegen!")
    db_dirty = True

# ==========================================
# --- ADMIN MODERATION (NUR OWNER) ---
# ==========================================
@bot.tree.command(name="ban", description="Bannt einen User")
@owner_only()
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    await interaction.response.defer()
    embed = discord.Embed(title=f"🚨 Du wurdest auf {interaction.guild.name} gebannt!", color=discord.Color.red())
    embed.add_field(name="Grund", value=reason or "Kein Grund angegeben", inline=False)
    view = DMAppealView(interaction.guild_id, "ban", reason or "Kein Grund", member.id)
    try: await member.send(embed=embed, view=view)
    except: pass
    await member.ban(reason=reason)
    await interaction.followup.send(f'✅ {member} gebannt. Grund: {reason}')

@bot.tree.command(name="kick", description="Kickt einen User")
@owner_only()
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    await interaction.response.defer()
    embed = discord.Embed(title=f"🚨 Du wurdest auf {interaction.guild.name} gekickt!", color=discord.Color.red())
    embed.add_field(name="Grund", value=reason or "Kein Grund angegeben", inline=False)
    view = DMAppealView(interaction.guild_id, "kick", reason or "Kein Grund", member.id)
    try: await member.send(embed=embed, view=view)
    except: pass
    await member.kick(reason=reason)
    await interaction.followup.send(f'✅ {member} gekickt. Grund: {reason}')

@bot.tree.command(name="timeout", description="Gibt einem User einen Timeout")
@owner_only()
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = None):
    await interaction.response.defer()
    embed = discord.Embed(title=f"🚨 Du wurdest auf {interaction.guild.name} getimeoutet!", color=discord.Color.red())
    embed.add_field(name="Dauer", value=f"{minutes} Minuten", inline=False)
    embed.add_field(name="Grund", value=reason or "Kein Grund angegeben", inline=False)
    view = DMAppealView(interaction.guild_id, "timeout", reason or "Kein Grund", member.id)
    try: await member.send(embed=embed, view=view)
    except: pass
    await member.timeout(timedelta(minutes=minutes), reason=reason)
    await interaction.followup.send(f'⏳ {member.mention} wurde für {minutes} Minuten getimeoutet.')

@bot.tree.command(name="purge", description="Löscht Nachrichten")
@owner_only()
async def purge(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f'🧹 {amount} Nachrichten gelöscht.', ephemeral=True)

@bot.tree.command(name="warn", description="Verwarnt einen User")
@owner_only()
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    global db_dirty
    guild_id = str(interaction.guild.id)
    user_id = str(member.id)
    if guild_id not in data: data[guild_id] = {}
    if user_id not in data[guild_id]: data[guild_id][user_id] = {"balance": 0, "xp": 0, "level": 0, "warns": 0}
    data[guild_id][user_id]["warns"] += 1
    db_dirty = True
    try: await member.send(f"⚠️ Du wurdest auf {interaction.guild.name} verwarnt. Grund: {reason}")
    except: pass
    await interaction.response.send_message(f'⚠️ {member.mention} gewarnt. Grund: {reason}')

@bot.tree.command(name="setnick", description="Ändert den Namen eines Users")
@owner_only()
async def setnick(interaction: discord.Interaction, member: discord.Member, nick: str):
    await member.edit(nick=nick)
    await interaction.response.send_message(f'✅ Nickname von {member} zu {nick} geändert.')

# ==========================================
# --- ROLLEN VERWALTUNG (NUR OWNER) ---
# ==========================================
role_group = app_commands.Group(name="role", description="Verwaltet Rollen")

@role_group.command(name="add", description="Gibt einem User eine Rolle")
@owner_only()
async def role_add(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    try:
        await member.add_roles(role)
        await interaction.response.send_message("✅", ephemeral=True)
    except: pass

@role_group.command(name="remove", description="Entfernt eine Rolle von einem User")
@owner_only()
async def role_remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    try:
        await member.remove_roles(role)
        await interaction.response.send_message("✅", ephemeral=True)
    except: pass

# ==========================================
# --- MUSIC (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="play", description="Spielt Musik ab (YouTube, Spotify, SoundCloud)")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message('Du musst in einem Voice Channel sein!', ephemeral=True)
    await interaction.response.defer()
    if interaction.guild.voice_client is None:
        await interaction.user.voice.channel.connect()
    elif interaction.guild.voice_client.channel != interaction.user.voice.channel:
        return await interaction.followup.send("Ich bin bereits in einem anderen Channel!")

    try:
        result = await YTDLSource.from_url(query, loop=bot.loop)
        if isinstance(result, list):
            if not result: return await interaction.followup.send("Konnte keine Songs finden.")
            first_song = result.pop(0)
            queues.setdefault(interaction.guild.id, []).extend(result)
            if interaction.guild.voice_client.is_playing():
                await interaction.followup.send(f'➕ Playlist hinzugefügt: **{len(result)+1} Songs** in der Warteschlange!')
            else:
                interaction.guild.voice_client.play(first_song, after=lambda e: check_queue(interaction.guild.id, interaction.channel))
                await interaction.followup.send(f'🎵 Spielt jetzt: **{first_song.title}**\n➕ {len(result)} weitere Songs zur Warteschlange hinzugefügt!')
        else:
            if interaction.guild.voice_client.is_playing():
                queues.setdefault(interaction.guild.id, []).append(result)
                await interaction.followup.send(f'➕ Zur Queue: **{result.title}**')
            else:
                interaction.guild.voice_client.play(result, after=lambda e: check_queue(interaction.guild.id, interaction.channel))
                await interaction.followup.send(f'🎵 Spielt jetzt: **{result.title}**')
    except Exception as e:
        await interaction.followup.send(f"Fehler beim Abspielen: {e}")

@bot.tree.command(name="skip", description="Überspringt den Song")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.stop()
        await interaction.response.send_message('⏭️ Song übersprungen!')
    else: await interaction.response.send_message('Es läuft keine Musik!')

@bot.tree.command(name="stop", description="Stoppt die Musik")
async def stop(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        queues[interaction.guild.id] = []
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message('⏹️ Musik gestoppt. Tschüss!')
    else: await interaction.response.send_message('Ich bin in keinem Voice Channel.')

# ==========================================
# --- ECONOMY (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="balance", description="Zeigt deinen Kontostand")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    user_data = data.get(str(interaction.guild.id), {}).get(str(member.id), {"balance": 0})
    await interaction.response.send_message(f'💰 {member.name} hat {user_data["balance"]} Coins.')

@bot.tree.command(name="daily", description="Hole dir deine täglichen Coins")
async def daily(interaction: discord.Interaction):
    global db_dirty
    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    if guild_id not in data: data[guild_id] = {}
    if user_id not in data[guild_id]: data[guild_id][user_id] = {"balance": 0, "xp": 0, "level": 0, "warns": 0}
    data[guild_id][user_id]["balance"] += 500
    db_dirty = True
    await interaction.response.send_message('💰 Du hast deine 500 täglichen Coins abgeholt!')

@bot.tree.command(name="gamble", description="Spiele um deine Coins")
async def gamble(interaction: discord.Interaction, amount: int):
    global db_dirty
    if amount <= 0: return await interaction.response.send_message("Betrag muss > 0 sein.")
    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    if guild_id not in data: data[guild_id] = {}
    if user_id not in data[guild_id]: data[guild_id][user_id] = {"balance": 0, "xp": 0, "level": 0, "warns": 0}
    if data[guild_id][user_id]["balance"] < amount: return await interaction.response.send_message("Du hast nicht genug Coins.")
    if random.randint(1, 2) == 1:
        data[guild_id][user_id]["balance"] += amount
        await interaction.response.send_message(f'🎉 Du hast {amount*2} Coins gewonnen!')
    else:
        data[guild_id][user_id]["balance"] -= amount
        await interaction.response.send_message('💀 Du hast alles verloren.')
    db_dirty = True

# ==========================================
# --- LEVELING & STATS (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="rank", description="Zeigt dein Level")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    user_data = data.get(str(interaction.guild.id), {}).get(str(member.id), {"xp": 0, "level": 0})
    embed = discord.Embed(title=f"Rang von {member.name}", color=discord.Color.gold())
    embed.add_field(name="Level", value=user_data["level"])
    embed.add_field(name="XP", value=user_data["xp"])
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Top 5 Server Mitglieder")
async def leaderboard(interaction: discord.Interaction):
    guild_data = data.get(str(interaction.guild.id), {})
    sorted_users = sorted(guild_data.items(), key=lambda x: x[1].get("level", 0), reverse=True)[:5]
    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold())
    for i, (user_id, udata) in enumerate(sorted_users, 1):
        member = interaction.guild.get_member(int(user_id))
        name = member.name if member else "Unbekannt"
        embed.add_field(name=f"#{i} {name}", value=f"Level {udata.get('level', 0)} | {udata.get('xp', 0)} XP", inline=False)
    await interaction.response.send_message(embed=embed)

# ==========================================
# --- IMAGE MANIPULATION (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="image", description="Bearbeite ein Profilbild")
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
    await interaction.followup.send(file=discord.File(buf, filename='manipulated.png'))

# ==========================================
# --- FUN & UTILITY (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="meme", description="Zeigt ein zufälliges Meme")
async def meme(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        r = requests.get("https://meme-api.com/gimme")
        if r.status_code == 200:
            embed = discord.Embed(title=r.json()['title'], color=discord.Color.random())
            embed.set_image(url=r.json()['url'])
            await interaction.followup.send(embed=embed)
    except: await interaction.followup.send("Konnte gerade kein Meme laden.")

@bot.tree.command(name="hug", description="Umarme jemanden")
async def hug(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.send_message(f'🤗 {interaction.user.mention} umarmt {member.mention}!')

@bot.tree.command(name="weather", description="Zeigt das Wetter in Celsius")
async def weather(interaction: discord.Interaction, city: str):
    try:
        r = requests.get(f"https://wttr.in/{city}?format=%l:+%c+%t+%w&lang=de&m")
        await interaction.response.send_message(f'☁️ Wetter für {city}: {r.text}')
    except: await interaction.response.send_message("Wetterdaten konnten nicht abgerufen werden.")

@bot.tree.command(name="avatar", description="Zeigt das Avatar eines Users")
async def avatar(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title=f"Avatar von {member.name}")
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# ==========================================
# --- SUGGESTIONS & TICKETS (FÜR ALLE / OWNER) ---
# ==========================================
@bot.tree.command(name="suggest", description="Mache einen Vorschlag")
async def suggest(interaction: discord.Interaction, suggestion: str):
    embed = discord.Embed(title="💡 Neue Idee!", description=suggestion, color=discord.Color.blurple())
    embed.set_author(name=interaction.user.name, icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction('✅')
    await msg.add_reaction('❌')

class TicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Ticket erstellen", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        channel = await guild.create_text_channel(f'ticket-{interaction.user.name}', overwrites=overwrites)
        await channel.send(f'{interaction.user.mention} Willkommen im Ticket! Ein Teammitglied kümmert sich gleich.')
        await interaction.response.send_message(f'Ticket erstellt: {channel.mention}', ephemeral=True)

@bot.tree.command(name="ticket", description="Erstellt ein Ticket Panel")
@owner_only() # Nur Owner dürfen das Panel erstellen
async def ticket(interaction: discord.Interaction):
    embed = discord.Embed(title="🎟️ Support Tickets", description="Klicke auf den Button um ein Ticket zu öffnen!", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, view=TicketView())

# ==========================================
# --- GIVEAWAYS (NUR OWNER) ---
# ==========================================
@bot.tree.command(name="gstart", description="Startet ein Giveaway")
@owner_only()
async def gstart(interaction: discord.Interaction, minutes: int, prize: str):
    await interaction.response.defer()
    embed = discord.Embed(title="🎉 GIVEAWAY 🎉", description=f"Preis: **{prize}**\nEndet in {minutes} Minuten.", color=discord.Color.gold())
    await interaction.followup.send(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction('🎉')
    await asyncio.sleep(minutes * 60)
    msg = await interaction.channel.fetch_message(msg.id)
    users = [u async for u in msg.reactions[0].users() if not u.bot]
    if users:
        winner = random.choice(users)
        await interaction.channel.send(f'🎉 Glückwunsch {winner.mention}! Du hast **{prize}** gewonnen!')
    else: await interaction.channel.send('Niemand hat am Gewinnspiel teilgenommen.')

# ==========================================
# --- DASHBOARD & HELP (FÜR ALLE) ---
# ==========================================
@bot.tree.command(name="dashboard", description="Übersicht über alle Befehle")
async def dashboard(interaction: discord.Interaction):
    embed = discord.Embed(title="🛠️ Server Dashboard", description="Übersicht aller Slash-Features", color=discord.Color.blue())
    embed.add_field(name="Moderation (Nur Admins)", value="/ban, /kick, /timeout, /purge, /warn, /setnick", inline=False)
    embed.add_field(name="Rollen (Nur Admins)", value="/role add, /role remove", inline=False)
    embed.add_field(name="AutoMod", value="Anti-Spam, Anti-Link (Automatisch aktiv)", inline=False)
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
    # Wenn jemand nicht auf der Owner-Liste steht, einfach komplett ignorieren (kein Feedback)
    if isinstance(error, app_commands.CheckFailure):
        return
    else:
        print(f"Slash Command Error: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send('Ein Fehler ist aufgetreten.', ephemeral=True)
            else:
                await interaction.response.send_message('Ein Fehler ist aufgetreten.', ephemeral=True)
        except: pass

# ==========================================
# --- BOT START ---
# ==========================================
bot.tree.add_command(role_group)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    print("FEHLER: Token wurde nicht gefunden. Bitte stelle sicher, dass die Umgebungsvariable auf Render gesetzt ist.")
else:
    bot.run(TOKEN)
