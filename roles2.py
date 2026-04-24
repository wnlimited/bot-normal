import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import sys
import time
import traceback
import logging
import random
import aiohttp
from datetime import datetime, timezone
from collections import defaultdict
import functools
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s » %(message)s', datefmt='%Y-%m-%d %H:%M:%S', handlers=[logging.StreamHandler(), logging.FileHandler('bot.log', encoding='utf-8')])
log = logging.getLogger('bot')
# ── Marca personalizada desactivada ─────────────────────────────────────────────
# Se deja el embed/footer tal como lo envíe el código del bot.
def _get_wm() -> str:
    return ""

def _get_sep() -> str:
    return " | "

WATERMARK = ""
WATERMARK_SEP = _get_sep()

def _enforce_watermark(embed: discord.Embed) -> discord.Embed:
    return embed

# No se parchea discord.Embed.set_footer ni send/edit/webhook.
# ────────────────────────────────────────────────────────────────────────────────

CONFIG_FILE = 'config.json'

def cargar_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    token_env = os.environ.get('DISCORD_TOKEN')
    if token_env:
        cfg['token'] = token_env
    if cfg.get('token') in ('', 'TU_TOKEN_AQUÍ', None):
        log.critical('No se encontró token.')
        sys.exit(1)
    return cfg
CONFIG = cargar_config()
TOKEN = CONFIG['token']
PREFIX = CONFIG.get('prefix', '!')
ROLES_STAFF_CFG = CONFIG.get('roles_staff', ['👑 Administración', '🛡️ Moderador'])
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
bot.remove_command('help')
_bot_messages_cache: list[discord.Message] = []
_CACHE_MAX = 50

def _cache_bot_message(msg: discord.Message):
    """."""
    _bot_messages_cache.append(msg)
    if len(_bot_messages_cache) > _CACHE_MAX:
        _bot_messages_cache.pop(0)
_patched_send_v1 = discord.abc.Messageable.send

@functools.wraps(_patched_send_v1)
async def _patched_send_v2(self, content=None, **kwargs):
    msg = await _patched_send_v1(self, content=content, **kwargs)
    if msg and (msg.embeds or (kwargs.get('embed') or kwargs.get('embeds'))):
        _cache_bot_message(msg)
    return msg
discord.abc.Messageable.send = _patched_send_v2

def es_admin(ctx) -> bool:
    return ctx.author.guild_permissions.administrator

def es_staff(ctx) -> bool:
    return ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_roles or any((r.name in ROLES_STAFF_CFG for r in ctx.author.roles))

def es_owner_o_admin(ctx) -> bool:
    return ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator
ANTINUKE_FILE = 'antinuke.json'
ANTINUKE_DEFAULT = {'activo': True, 'whitelist': [], 'owner_id': None, 'limites': {'ban': 3, 'kick': 3, 'roles': 3, 'canales': 3, 'webhooks': 3}, 'ventana': 10, 'accion': 'ban', 'log_channel': None, 'antiraid': {'activo': False, 'joins_limite': 10, 'joins_ventana': 10, 'accion': 'kick'}, 'antilinks': {'activo': False, 'whitelist_canales': [], 'whitelist_roles': []}, 'antispam': {'activo': False, 'mensajes_limite': 5, 'ventana': 5}, 'antibot': {'activo': False}, 'verificacion': {'activo': False, 'rol_verificado': None, 'rol_no_verificado': None, 'canal': None, 'emoji': '✅'}, 'warn_sistema': {}, 'mute_rol': None}

def _cargar_db_antinuke() -> dict:
    if os.path.exists(ANTINUKE_FILE):
        with open(ANTINUKE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _guardar_db_antinuke(db: dict):
    with open(ANTINUKE_FILE, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def cargar_antinuke(guild_id: int=None) -> dict:
    db = _cargar_db_antinuke()
    key = str(guild_id) if guild_id else '__global__'
    data = db.get(key, {})
    import copy
    resultado = copy.deepcopy(ANTINUKE_DEFAULT)
    for k, v in data.items():
        if k == 'limites' and isinstance(v, dict):
            resultado['limites'].update(v)
        else:
            resultado[k] = v
    return resultado

def guardar_antinuke(cfg: dict, guild_id: int=None):
    db = _cargar_db_antinuke()
    key = str(guild_id) if guild_id else '__global__'
    db[key] = cfg
    _guardar_db_antinuke(db)
_acciones = defaultdict(lambda: defaultdict(list))
_joins_recents = defaultdict(list)
_spam_tracker = defaultdict(lambda: defaultdict(list))

def registrar_accion(user_id: int, tipo: str, guild_id: int=0) -> int:
    cfg = cargar_antinuke(guild_id)
    ventana = cfg.get('ventana', 10)
    ahora = time.time()
    _acciones[guild_id][user_id] = [(t, a) for t, a in _acciones[guild_id][user_id] if ahora - t <= ventana]
    _acciones[guild_id][user_id].append((ahora, tipo))
    return sum((1 for _, a in _acciones[guild_id][user_id] if a == tipo))

def es_seguro(user_id: int, guild: discord.Guild) -> bool:
    cfg = cargar_antinuke(guild.id)
    if guild.owner_id == user_id:
        return True
    owner = cfg.get('owner_id')
    if owner and user_id == int(owner):
        return True
    return user_id in [int(x) for x in cfg.get('whitelist', [])]

def es_owner_an(ctx) -> bool:
    cfg = cargar_antinuke(ctx.guild.id)
    owner = cfg.get('owner_id')
    return ctx.author.id == ctx.guild.owner_id or (owner and ctx.author.id == int(owner))

async def ejecutar_castigo(guild: discord.Guild, member, razon: str, accion: str=None):
    cfg = cargar_antinuke(guild.id)
    if accion is None:
        accion = cfg.get('accion', 'ban')
    if isinstance(member, int):
        try:
            member = await guild.fetch_member(member)
        except Exception:
            try:
                user = await bot.fetch_user(member)
                if accion == 'ban':
                    await guild.ban(user, reason=f'[AntiNuke] {razon}', delete_message_days=0)
                    log.warning(f'[AntiNuke] BAN (por ID) a {user} — {razon}')
            except Exception as e:
                log.error(f'[AntiNuke] No pude castigar ID {member}: {e}')
            return
    try:
        if accion == 'ban':
            await guild.ban(member, reason=f'[AntiNuke] {razon}', delete_message_days=0)
        elif accion == 'kick':
            await guild.kick(member, reason=f'[AntiNuke] {razon}')
        elif accion == 'quitar_roles':
            roles = [r for r in member.roles if r != guild.default_role and (not r.managed)]
            if roles:
                await member.remove_roles(*roles, reason=f'[AntiNuke] {razon}')
        log.warning(f'[AntiNuke] {accion.upper()} a {member} — {razon}')
    except discord.Forbidden:
        log.error(f'[AntiNuke] Sin permisos para {accion} a {member}.')
    except Exception as e:
        log.error(f'[AntiNuke] No pude aplicar castigo a {member}: {e}')

async def log_antinuke(guild: discord.Guild, titulo: str, desc: str, color=16711680):
    cfg = cargar_antinuke(guild.id)
    canal_id = cfg.get('log_channel')
    if not canal_id:
        return
    canal = guild.get_channel(int(canal_id))
    if canal:
        embed = discord.Embed(title=f'🛡️ AntiNuke — {titulo}', description=desc, color=color, timestamp=datetime.now(timezone.utc))
        try:
            await canal.send(embed=embed)
        except Exception:
            pass

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    cfg = cargar_antinuke(guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, guild):
            return
        count = registrar_accion(autor.id, 'ban', guild.id)
        try:
            await guild.unban(user, reason=f'[AntiNuke] Ban no autorizado por {autor}')
            await log_antinuke(guild, '♻️ Ban Revertido', f'**Víctima:** {user.mention} (`{user.id}`)\n**Baneado por:** {autor.mention}\n**Acción:** Desbaneado automáticamente', color=65416)
        except Exception as e:
            log.error(f'[AntiNuke] No pude desbanear a {user}: {e}')
        try:
            m = guild.get_member(autor.id) or await guild.fetch_member(autor.id)
        except Exception:
            m = None
        if m:
            await ejecutar_castigo(guild, m, f'Ban no autorizado ({count} bans)')
            await log_antinuke(guild, '🔨 Ban No Autorizado Detectado', f"**Usuario:** {autor.mention} (`{autor.id}`)\n**Bans en ventana:** {count}\n**Acción:** `{cfg['accion']}`")
        else:
            try:
                await guild.ban(discord.Object(id=autor.id), reason=f'[AntiNuke] Ban no autorizado ({count} bans)')
                await log_antinuke(guild, '🔨 Ban No Autorizado (por ID)', f'**Usuario:** {autor.mention} (`{autor.id}`)\n**Bans:** {count}\n**Acción:** BAN por ID')
            except Exception as e:
                log.error(f'[AntiNuke] No pude banear a {autor} por ID: {e}')
    except Exception as e:
        log.error(f'[AntiNuke] on_member_ban: {e}')

@bot.event
async def on_member_remove(member: discord.Member):
    cfg = cargar_antinuke(member.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, member.guild):
            return
        if entries[0].target.id != member.id:
            return
        count = registrar_accion(autor.id, 'kick', member.guild.id)
        try:
            m = member.guild.get_member(autor.id) or await member.guild.fetch_member(autor.id)
        except Exception:
            m = None
        if m:
            await ejecutar_castigo(member.guild, m, f'Kick no autorizado ({count} kicks)')
            await log_antinuke(member.guild, '👢 Kick No Autorizado Detectado', f"**Usuario:** {autor.mention}\n**Kickeó a:** {member.mention}\n**Kicks en ventana:** {count}\n**Acción:** `{cfg['accion']}`")
        else:
            try:
                await member.guild.ban(discord.Object(id=autor.id), reason=f'[AntiNuke] Kick no autorizado ({count})')
                await log_antinuke(member.guild, '👢 Kick No Autorizado (por ID)', f'**Usuario:** {autor.mention} (`{autor.id}`)\n**Kicks:** {count}\n**Acción:** BAN por ID')
            except Exception as e:
                log.error(f'[AntiNuke] No pude castigar a {autor} por ID: {e}')
    except Exception as e:
        log.error(f'[AntiNuke] on_member_remove: {e}')

@bot.event
async def on_guild_role_delete(role: discord.Role):
    cfg = cargar_antinuke(role.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in role.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_delete)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, role.guild):
            return
        count = registrar_accion(autor.id, 'roles', role.guild.id)
        try:
            nuevo_rol = await role.guild.create_role(name=role.name, color=role.color, hoist=role.hoist, mentionable=role.mentionable, permissions=role.permissions, reason=f'[AntiNuke] Restaurando rol eliminado por {autor}')
            try:
                await nuevo_rol.edit(position=role.position)
            except Exception:
                pass
            await log_antinuke(role.guild, '♻️ Rol Restaurado', f'**Rol:** `{role.name}`\n**Eliminado por:** {autor.mention}\n**Restaurado:** {nuevo_rol.mention}', color=65416)
        except Exception as e:
            log.error(f'[AntiNuke] No pude restaurar rol {role.name}: {e}')
        if count >= cfg['limites']['roles']:
            m = role.guild.get_member(autor.id) or await role.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(role.guild, m, f'Borrado masivo de roles ({count})')
                await log_antinuke(role.guild, '🗑️ Borrado de Roles Detectado', f"**Usuario:** {autor.mention}\n**Roles borrados:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_role_delete: {e}')

@bot.event
async def on_guild_role_create(role: discord.Role):
    cfg = cargar_antinuke(role.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in role.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, role.guild):
            return
        count = registrar_accion(autor.id, 'roles', role.guild.id)
        try:
            await role.delete(reason=f'[AntiNuke] Rol no autorizado creado por {autor}')
            await log_antinuke(role.guild, '🗑️ Rol No Autorizado Eliminado', f'**Rol:** `{role.name}`\n**Creado por:** {autor.mention}\n**Acción:** Eliminado automáticamente', color=16746496)
        except Exception as e:
            log.error(f'[AntiNuke] No pude eliminar rol {role.name}: {e}')
        if count >= cfg['limites']['roles']:
            m = role.guild.get_member(autor.id) or await role.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(role.guild, m, f'Creación masiva de roles ({count})')
                await log_antinuke(role.guild, '🆕 Creación Masiva de Roles', f"**Usuario:** {autor.mention}\n**Roles creados:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_role_create: {e}')

@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    pass

@bot.event
async def on_guild_channel_delete(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_delete)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, 'canales', channel.guild.id)
        try:
            overwrites = channel.overwrites
            if isinstance(channel, discord.TextChannel):
                nuevo_canal = await channel.guild.create_text_channel(name=channel.name, topic=channel.topic, slowmode_delay=channel.slowmode_delay, nsfw=channel.nsfw, overwrites=overwrites, category=channel.category, reason=f'[AntiNuke] Restaurando canal eliminado por {autor}')
            elif isinstance(channel, discord.VoiceChannel):
                nuevo_canal = await channel.guild.create_voice_channel(name=channel.name, bitrate=channel.bitrate, user_limit=channel.user_limit, overwrites=overwrites, category=channel.category, reason=f'[AntiNuke] Restaurando canal eliminado por {autor}')
            elif isinstance(channel, discord.CategoryChannel):
                nuevo_canal = await channel.guild.create_category(name=channel.name, overwrites=overwrites, reason=f'[AntiNuke] Restaurando categoría eliminada por {autor}')
            else:
                nuevo_canal = await channel.guild.create_text_channel(name=channel.name, overwrites=overwrites, category=channel.category, reason=f'[AntiNuke] Restaurando canal eliminado por {autor}')
            try:
                await nuevo_canal.edit(position=channel.position)
            except Exception:
                pass
            await log_antinuke(channel.guild, '♻️ Canal Restaurado', f'**Canal:** `#{channel.name}`\n**Eliminado por:** {autor.mention}\n**Restaurado:** {nuevo_canal.mention}', color=65416)
        except Exception as e:
            log.error(f'[AntiNuke] No pude restaurar canal {channel.name}: {e}')
        if count >= cfg['limites']['canales']:
            m = channel.guild.get_member(autor.id) or await channel.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(channel.guild, m, f'Borrado masivo de canales ({count})')
                await log_antinuke(channel.guild, '🗑️ Borrado de Canales Detectado', f"**Usuario:** {autor.mention}\n**Canales borrados:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_channel_delete: {e}')

@bot.event
async def on_guild_channel_create(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, 'canales', channel.guild.id)
        try:
            nombre = channel.name
            await channel.delete(reason=f'[AntiNuke] Canal no autorizado creado por {autor}')
            await log_antinuke(channel.guild, '🗑️ Canal No Autorizado Eliminado', f'**Canal:** `#{nombre}`\n**Creado por:** {autor.mention}\n**Acción:** Eliminado automáticamente', color=16746496)
        except Exception as e:
            log.error(f'[AntiNuke] No pude eliminar canal {channel.name}: {e}')
        if count >= cfg['limites']['canales']:
            m = channel.guild.get_member(autor.id) or await channel.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(channel.guild, m, f'Creación masiva de canales ({count})')
                await log_antinuke(channel.guild, '🆕 Creación Masiva de Canales', f"**Usuario:** {autor.mention}\n**Canales creados:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f'[AntiNuke] on_guild_channel_create: {e}')

@bot.event
async def on_webhooks_update(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get('activo'):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.webhook_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, 'webhooks', channel.guild.id)
        if count >= cfg['limites']['webhooks']:
            m = channel.guild.get_member(autor.id) or await channel.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(channel.guild, m, f'Creación masiva de webhooks ({count})')
                await log_antinuke(channel.guild, '🕸️ Webhooks Masivos', f"**Usuario:** {autor.mention}\n**Webhooks:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f'[AntiNuke] on_webhooks_update: {e}')

@bot.event
async def on_member_join(member: discord.Member):
    cfg = cargar_antinuke(member.guild.id)
    if cfg.get('antibot', {}).get('activo') and member.bot:
        try:
            entry = await member.guild.audit_logs(limit=1, action=discord.AuditLogAction.bot_add).next()
            autor = entry.user
            if not es_seguro(autor.id, member.guild):
                await member.kick(reason='[AntiBot] Bot no autorizado')
                await log_antinuke(member.guild, '🤖 Bot No Autorizado', f'**Bot:** {member.mention}\n**Añadido por:** {autor.mention}', color=16755200)
                return
        except Exception:
            pass
    ar = cfg.get('antiraid', {})
    if ar.get('activo'):
        ahora = time.time()
        gid = member.guild.id
        ventana = ar.get('joins_ventana', 10)
        _joins_recents[gid].append(ahora)
        while _joins_recents[gid] and ahora - _joins_recents[gid][0] > ventana:
            _joins_recents[gid].pop(0)
        if len(_joins_recents[gid]) >= ar.get('joins_limite', 10):
            accion = ar.get('accion', 'kick')
            try:
                if accion == 'kick':
                    await member.kick(reason='[AntiRaid] Raid detectada')
                elif accion == 'ban':
                    await member.ban(reason='[AntiRaid] Raid detectada', delete_message_days=0)
            except Exception:
                pass
            await log_antinuke(member.guild, '🚨 Raid Detectada', f'**Joins en {ventana}s:** {len(_joins_recents[gid])}\n**Último:** {member.mention}\n**Acción:** `{accion}`', color=16729088)
    ver = cfg.get('verificacion', {})
    if ver.get('activo') and ver.get('rol_no_verificado'):
        rol = member.guild.get_role(int(ver['rol_no_verificado']))
        if rol:
            try:
                await member.add_roles(rol)
            except Exception:
                pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return
    cfg = cargar_antinuke(message.guild.id)
    al = cfg.get('antilinks', {})
    if al.get('activo'):
        wl_canales = [int(x) for x in al.get('whitelist_canales', [])]
        wl_roles = [int(x) for x in al.get('whitelist_roles', [])]
        tiene_link = any((x in message.content for x in ['http://', 'https://', 'discord.gg/', 'discord.com/invite/']))
        in_wl_canal = message.channel.id in wl_canales
        in_wl_rol = any((r.id in wl_roles for r in message.author.roles))
        es_safe_usr = es_seguro(message.author.id, message.guild)
        if tiene_link and (not in_wl_canal) and (not in_wl_rol) and (not es_safe_usr):
            try:
                await message.delete()
                await message.channel.send(f'🔗 {message.author.mention} No se permiten links aquí.', delete_after=5)
                await log_antinuke(message.guild, '🔗 Link Bloqueado', f'**Usuario:** {message.author.mention}\n**Canal:** {message.channel.mention}', color=16755200)
            except Exception:
                pass
            return
    asp = cfg.get('antispam', {})
    if asp.get('activo') and (not es_seguro(message.author.id, message.guild)):
        ahora = time.time()
        ventana = asp.get('ventana', 5)
        limite = asp.get('mensajes_limite', 5)
        gid = message.guild.id
        uid = message.author.id
        _spam_tracker[gid][uid] = [t for t in _spam_tracker[gid][uid] if ahora - t <= ventana]
        _spam_tracker[gid][uid].append(ahora)
        if len(_spam_tracker[gid][uid]) >= limite:
            try:
                import datetime as dt
                until = discord.utils.utcnow() + dt.timedelta(minutes=5)
                await message.author.timeout(until, reason='[AntiSpam] Spam detectado')
                await message.channel.send(f'🔇 {message.author.mention} fue silenciado por spam.', delete_after=5)
                _spam_tracker[gid][uid] = []
                await log_antinuke(message.guild, '💬 Spam Detectado', f'**Usuario:** {message.author.mention}\n**Canal:** {message.channel.mention}', color=16746496)
            except Exception:
                pass
    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    cfg = cargar_antinuke(payload.guild_id)
    ver = cfg.get('verificacion', {})
    if not ver.get('activo'):
        return
    canal_id = ver.get('canal')
    if not canal_id or payload.channel_id != int(canal_id):
        return
    if str(payload.emoji) != ver.get('emoji', '✅'):
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    rol_ver = ver.get('rol_verificado')
    rol_no = ver.get('rol_no_verificado')
    if rol_ver:
        r = guild.get_role(int(rol_ver))
        if r:
            try:
                await member.add_roles(r, reason='Verificación')
            except Exception:
                pass
    if rol_no:
        r = guild.get_role(int(rol_no))
        if r and r in member.roles:
            try:
                await member.remove_roles(r, reason='Verificación')
            except Exception:
                pass

@bot.command(name='antinuke')
@commands.check(es_owner_an)
async def antinuke_status(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    estado = '✅ Activo' if cfg['activo'] else '❌ Desactivado'
    wl = cfg.get('whitelist', [])
    wl_members = []
    for uid in wl:
        m = ctx.guild.get_member(int(uid))
        if m:
            wl_members.append(m.mention)
    wl_txt = ', '.join(wl_members) if wl_members else 'Nadie'
    embed = discord.Embed(title='🛡️ AntiNuke — Panel Completo', color=65416 if cfg['activo'] else 16711680)
    embed.add_field(name='Estado', value=estado, inline=True)
    embed.add_field(name='Acción', value=cfg.get('accion', 'ban').upper(), inline=True)
    embed.add_field(name='Ventana', value=f"{cfg.get('ventana', 10)}s", inline=True)
    lim = cfg.get('limites', {})
    embed.add_field(name='Límites', value='\n'.join((f'`{k}`: {v}' for k, v in lim.items())), inline=True)
    ar = cfg.get('antiraid', {})
    al = cfg.get('antilinks', {})
    asp = cfg.get('antispam', {})
    ab = cfg.get('antibot', {})
    embed.add_field(name='Módulos', value=f"AntiRaid: {('✅' if ar.get('activo') else '❌')}\nAntiLinks: {('✅' if al.get('activo') else '❌')}\nAntiSpam: {('✅' if asp.get('activo') else '❌')}\nAntiBot: {('✅' if ab.get('activo') else '❌')}", inline=True)
    embed.add_field(name=f'Whitelist ({len(wl_members)})', value=wl_txt, inline=False)
    log_ch = cfg.get('log_channel')
    embed.add_field(name='Canal logs', value=f'<#{log_ch}>' if log_ch else 'No configurado', inline=False)
    await ctx.send(embed=embed)

@bot.command(name='an_ayuda')
@commands.check(es_owner_an)
async def an_ayuda(ctx):
    p = PREFIX
    embed = discord.Embed(title='🛡️ AntiNuke — Comandos', color=65416)
    embed.add_field(name='⚙️ General', value=f'`{p}antinuke` — Panel de estado\n`{p}an_activar` / `{p}an_desactivar` — Activar/desactivar\n`{p}an_accion <ban|kick|quitar_roles>` — Acción al detectar\n`{p}an_limite <tipo> <n>` — Cambiar límite\n`{p}an_ventana <segundos>` — Ventana de tiempo\n`{p}an_whitelist @user` — Añadir/quitar de whitelist\n`{p}an_logs [#canal]` — Canal de logs\n`{p}an_owner @user` — Asignar owner del AN', inline=False)
    embed.add_field(name='🚨 AntiRaid', value=f'`{p}an_antiraid` — Ver estado\n`{p}an_antiraid_on` / `{p}an_antiraid_off` — Activar/desactivar\n`{p}an_antiraid_config <joins> <ventana> <accion>` — Configurar', inline=False)
    embed.add_field(name='🔗 AntiLinks', value=f'`{p}an_antilinks_on` / `{p}an_antilinks_off` — Activar/desactivar\n`{p}an_links_canal #canal` — Whitelist canal\n`{p}an_links_rol <rol>` — Whitelist rol', inline=False)
    embed.add_field(name='💬 AntiSpam', value=f'`{p}an_antispam_on` / `{p}an_antispam_off` — Activar/desactivar\n`{p}an_spam_config <mensajes> <ventana>` — Configurar', inline=False)
    embed.add_field(name='🤖 AntiBot / ✅ Verificación', value=f'`{p}an_antibot_on` / `{p}an_antibot_off` — Bloquear bots no autorizados\n`{p}an_ver_setup #canal @rol_verificado @rol_no_verificado` — Setup verificación\n`{p}an_ver_on` / `{p}an_ver_off` — Activar/desactivar verificación', inline=False)
    embed.add_field(name='⚠️ Warns', value=f'`{p}warn @user <razón>` — Advertir usuario\n`{p}warns @user` — Ver advertencias\n`{p}clearwarns @user` — Borrar advertencias', inline=False)
    await ctx.send(embed=embed)

@bot.command(name='an_activar')
@commands.check(es_owner_an)
async def an_activar(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('✅ AntiNuke **activado**.')

@bot.command(name='an_desactivar')
@commands.check(es_owner_an)
async def an_desactivar(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('⚠️ AntiNuke **desactivado**. El servidor queda sin protección.')

@bot.command(name='an_whitelist')
@commands.check(es_owner_an)
async def an_whitelist(ctx, member: discord.Member=None):
    cfg = cargar_antinuke(ctx.guild.id)
    wl = cfg.get('whitelist', [])
    if member is None:
        wl_members = []
        for uid in wl:
            m = ctx.guild.get_member(int(uid))
            if m:
                wl_members.append(f'{m.mention} (`{m.id}`)')
        embed = discord.Embed(title=f'🛡️ Whitelist — {ctx.guild.name}', description='\n'.join(wl_members) if wl_members else 'Nadie en la whitelist.', color=65416)
        return await ctx.send(embed=embed)
    uid = str(member.id)
    if uid in wl:
        wl.remove(uid)
        cfg['whitelist'] = wl
        guardar_antinuke(cfg, ctx.guild.id)
        embed = discord.Embed(title='🗑️ Quitado de Whitelist', description=f'{member.mention} ya **no está** en la whitelist de **{ctx.guild.name}**.', color=discord.Color.red())
    else:
        wl.append(uid)
        cfg['whitelist'] = wl
        guardar_antinuke(cfg, ctx.guild.id)
        embed = discord.Embed(title='✅ Añadido a Whitelist', description=f'{member.mention} ahora está en la whitelist de **{ctx.guild.name}**.\nEl AntiNuke lo ignorará en este servidor.', color=discord.Color.green())
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name='an_accion')
@commands.check(es_owner_an)
async def an_accion(ctx, accion: str):
    accion = accion.lower()
    if accion not in ('ban', 'kick', 'quitar_roles'):
        return await ctx.send('❌ Opciones: `ban`, `kick`, `quitar_roles`')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['accion'] = accion
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Acción → **{accion.upper()}**.')

@bot.command(name='an_limite')
@commands.check(es_owner_an)
async def an_limite(ctx, tipo: str, cantidad: int):
    tipos = list(ANTINUKE_DEFAULT['limites'].keys())
    if tipo not in tipos:
        return await ctx.send(f"❌ Tipos: {', '.join((f'`{t}`' for t in tipos))}")
    if not 0 <= cantidad <= 20:
        return await ctx.send('❌ Entre 0 y 20.')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['limites'][tipo] = cantidad
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Límite `{tipo}` → **{cantidad}**.')

@bot.command(name='an_ventana')
@commands.check(es_owner_an)
async def an_ventana(ctx, segundos: int):
    if not 5 <= segundos <= 120:
        return await ctx.send('❌ Entre 5 y 120 segundos.')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['ventana'] = segundos
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Ventana → **{segundos}s**.')

@bot.command(name='an_logs')
@commands.check(es_owner_an)
async def an_logs(ctx, canal: discord.TextChannel=None):
    cfg = cargar_antinuke(ctx.guild.id)
    if canal is None:
        cfg['log_channel'] = None
        guardar_antinuke(cfg, ctx.guild.id)
        return await ctx.send('🗑️ Canal de logs **eliminado**.')
    cfg['log_channel'] = str(canal.id)
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ Canal de logs → {canal.mention}.')

@bot.command(name='an_owner')
@commands.check(lambda ctx: ctx.author.id == ctx.guild.owner_id)
async def an_owner(ctx, member: discord.Member):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg['owner_id'] = str(member.id)
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ {member.mention} es ahora el **owner del AntiNuke**.')

@bot.command(name='an_antiraid')
@commands.check(es_owner_an)
async def an_antiraid_status(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    ar = cfg.get('antiraid', {})
    embed = discord.Embed(title='🚨 AntiRaid', color=65416 if ar.get('activo') else 16711680)
    embed.add_field(name='Estado', value='✅ Activo' if ar.get('activo') else '❌ Desactivado', inline=True)
    embed.add_field(name='Límite', value=f"{ar.get('joins_limite', 10)} joins", inline=True)
    embed.add_field(name='Ventana', value=f"{ar.get('joins_ventana', 10)}s", inline=True)
    embed.add_field(name='Acción', value=ar.get('accion', 'kick').upper(), inline=True)
    await ctx.send(embed=embed)

@bot.command(name='an_antiraid_on')
@commands.check(es_owner_an)
async def an_antiraid_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antiraid', {})['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('✅ AntiRaid **activado**.')

@bot.command(name='an_antiraid_off')
@commands.check(es_owner_an)
async def an_antiraid_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antiraid', {})['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('⚠️ AntiRaid **desactivado**.')

@bot.command(name='an_antiraid_config')
@commands.check(es_owner_an)
async def an_antiraid_config(ctx, joins: int, ventana: int, accion: str='kick'):
    if accion not in ('kick', 'ban'):
        return await ctx.send('❌ Acción: `kick` o `ban`')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antiraid', {}).update({'joins_limite': joins, 'joins_ventana': ventana, 'accion': accion})
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ AntiRaid → **{joins} joins** en **{ventana}s** → **{accion}**.')

@bot.command(name='an_antilinks_on')
@commands.check(es_owner_an)
async def an_antilinks_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antilinks', {})['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('✅ AntiLinks **activado**.')

@bot.command(name='an_antilinks_off')
@commands.check(es_owner_an)
async def an_antilinks_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antilinks', {})['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('⚠️ AntiLinks **desactivado**.')

@bot.command(name='an_links_canal')
@commands.check(es_owner_an)
async def an_links_canal(ctx, canal: discord.TextChannel):
    cfg = cargar_antinuke(ctx.guild.id)
    wl = cfg.setdefault('antilinks', {}).setdefault('whitelist_canales', [])
    cid = str(canal.id)
    if cid in wl:
        wl.remove(cid)
        accion = 'quitado de'
    else:
        wl.append(cid)
        accion = 'añadido a'
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ {canal.mention} **{accion}** la whitelist de links.')

@bot.command(name='an_links_rol')
@commands.check(es_owner_an)
async def an_links_rol(ctx, *, nombre_rol: str):
    rol = discord.utils.get(ctx.guild.roles, name=nombre_rol)
    if not rol:
        return await ctx.send(f'❌ Rol `{nombre_rol}` no encontrado.')
    cfg = cargar_antinuke(ctx.guild.id)
    wl = cfg.setdefault('antilinks', {}).setdefault('whitelist_roles', [])
    rid = str(rol.id)
    if rid in wl:
        wl.remove(rid)
        accion = 'quitado de'
    else:
        wl.append(rid)
        accion = 'añadido a'
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ **{rol.name}** **{accion}** la whitelist de links.')

@bot.command(name='an_antispam_on')
@commands.check(es_owner_an)
async def an_antispam_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antispam', {})['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('✅ AntiSpam **activado**.')

@bot.command(name='an_antispam_off')
@commands.check(es_owner_an)
async def an_antispam_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antispam', {})['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('⚠️ AntiSpam **desactivado**.')

@bot.command(name='an_spam_config')
@commands.check(es_owner_an)
async def an_spam_config(ctx, mensajes: int, ventana: int):
    if not 3 <= mensajes <= 20 or not 3 <= ventana <= 30:
        return await ctx.send('❌ mensajes: 3–20 | ventana: 3–30s')
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antispam', {}).update({'mensajes_limite': mensajes, 'ventana': ventana})
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f'✅ AntiSpam → **{mensajes} msgs** en **{ventana}s**.')

@bot.command(name='an_antibot_on')
@commands.check(es_owner_an)
async def an_antibot_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antibot', {})['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('✅ AntiBot **activado**. Bots no autorizados serán expulsados.')

@bot.command(name='an_antibot_off')
@commands.check(es_owner_an)
async def an_antibot_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('antibot', {})['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('⚠️ AntiBot **desactivado**.')

@bot.command(name='an_ver_setup')
@commands.check(es_owner_an)
async def an_ver_setup(ctx, canal: discord.TextChannel, rol_ver: discord.Role, rol_no_ver: discord.Role=None):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('verificacion', {}).update({'canal': str(canal.id), 'rol_verificado': str(rol_ver.id), 'rol_no_verificado': str(rol_no_ver.id) if rol_no_ver else None})
    guardar_antinuke(cfg, ctx.guild.id)
    embed = discord.Embed(title='✅ Verificación', description=f'Reacciona con ✅ para verificarte y acceder al servidor.', color=discord.Color.green())
    msg = await canal.send(embed=embed)
    await msg.add_reaction('✅')
    await ctx.send(f'✅ Verificación configurada en {canal.mention}.')

@bot.command(name='an_ver_on')
@commands.check(es_owner_an)
async def an_ver_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('verificacion', {})['activo'] = True
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('✅ Verificación **activada**.')

@bot.command(name='an_ver_off')
@commands.check(es_owner_an)
async def an_ver_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault('verificacion', {})['activo'] = False
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send('⚠️ Verificación **desactivada**.')
WARNS_FILE = 'warns.json'

def cargar_warns() -> dict:
    if os.path.exists(WARNS_FILE):
        with open(WARNS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def guardar_warns(data: dict):
    with open(WARNS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

@bot.command(name='warn')
@commands.check(es_staff)
async def warn(ctx, member: discord.Member, *, razon: str='Sin razón'):
    if member.guild_permissions.administrator:
        return await ctx.send('❌ No puedes advertir a un administrador.')
    data = cargar_warns()
    uid = str(member.id)
    if uid not in data:
        data[uid] = []
    data[uid].append({'razon': razon, 'por': str(ctx.author.id), 'fecha': datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')})
    guardar_warns(data)
    total = len(data[uid])
    embed = discord.Embed(title='⚠️ Advertencia', color=discord.Color.orange())
    embed.add_field(name='👤 Usuario', value=member.mention, inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='📊 Total', value=f'{total} warn(s)', inline=True)
    embed.add_field(name='👮 Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)
    if total >= 5:
        await ctx.guild.ban(member, reason='[AutoWarn] 5 advertencias')
        await ctx.send(f'🔨 {member.mention} fue baneado automáticamente por alcanzar 5 warns.')
    elif total >= 3:
        import datetime as dt
        until = discord.utils.utcnow() + dt.timedelta(hours=1)
        try:
            await member.timeout(until, reason='[AutoWarn] 3 advertencias')
            await ctx.send(f'🔇 {member.mention} muteado 1h por 3 warns.')
        except Exception:
            pass

@bot.command(name='warns')
@commands.check(es_staff)
async def ver_warns(ctx, member: discord.Member=None):
    member = member or ctx.author
    data = cargar_warns()
    lista = data.get(str(member.id), [])
    embed = discord.Embed(title=f'⚠️ Warns de {member.display_name}', color=discord.Color.orange())
    embed.set_thumbnail(url=member.display_avatar.url)
    if not lista:
        embed.description = '✅ Sin advertencias.'
    else:
        for i, w in enumerate(lista, 1):
            embed.add_field(name=f"#{i} — {w['fecha']}", value=f"**Razón:** {w['razon']}\n**Por:** <@{w['por']}>", inline=False)
    await ctx.send(embed=embed)

@bot.command(name='clearwarns', aliases=['limpiarwarns'])
@commands.check(es_admin)
async def clearwarns(ctx, member: discord.Member):
    data = cargar_warns()
    data.pop(str(member.id), None)
    guardar_warns(data)
    await ctx.send(f'✅ Warns de {member.mention} borrados.')

@bot.command(name='delwarn')
@commands.check(es_admin)
async def delwarn(ctx, member: discord.Member, numero: int):
    data = cargar_warns()
    uid = str(member.id)
    lista = data.get(uid, [])
    if numero < 1 or numero > len(lista):
        return await ctx.send(f'❌ Número inválido. Tiene {len(lista)} warn(s).')
    borrado = lista.pop(numero - 1)
    data[uid] = lista
    guardar_warns(data)
    await ctx.send(f"✅ Warn #{numero} de {member.mention} borrado. (`{borrado['razon']}`)")
PAREJAS_FILE = 'parejas.json'
FAMILIA_FILE = 'familia.json'

def cargar_parejas() -> dict:
    if os.path.exists(PAREJAS_FILE):
        with open(PAREJAS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def guardar_parejas(data: dict):
    with open(PAREJAS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def cargar_familia() -> dict:
    if os.path.exists(FAMILIA_FILE):
        with open(FAMILIA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def guardar_familia(data: dict):
    with open(FAMILIA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
propuestas_pendientes = {}

@bot.command(name='casar', aliases=['proponer', 'marry'])
async def casar(ctx, member: discord.Member):
    if member == ctx.author:
        return await ctx.send('❌ No puedes casarte contigo mismo.')
    if member.bot:
        return await ctx.send('❌ Los bots no se casan.')
    parejas = cargar_parejas()
    uid = str(ctx.author.id)
    mid = str(member.id)
    if uid in parejas:
        return await ctx.send(f'💍 Ya estás casado/a. Usa `{PREFIX}divorcio` primero.')
    if mid in parejas:
        return await ctx.send(f'💔 {member.mention} ya está casado/a.')
    propuestas_pendientes[member.id] = ctx.author.id
    embed = discord.Embed(title='💍 ¡Propuesta!', description=f'{ctx.author.mention} le propone a {member.mention}\nUsa `{PREFIX}aceptar` o `{PREFIX}rechazar` en 60s.', color=discord.Color.pink())
    await ctx.send(embed=embed)
    await asyncio.sleep(60)
    if propuestas_pendientes.get(member.id) == ctx.author.id:
        propuestas_pendientes.pop(member.id, None)
        await ctx.send(f'⌛ La propuesta a {member.mention} expiró.')

@bot.command(name='aceptar')
async def aceptar(ctx):
    if ctx.author.id not in propuestas_pendientes:
        return await ctx.send('❌ Sin propuesta pendiente.')
    autor_id = propuestas_pendientes.pop(ctx.author.id)
    parejas = cargar_parejas()
    mid = str(ctx.author.id)
    parejas[str(autor_id)] = mid
    parejas[mid] = str(autor_id)
    guardar_parejas(parejas)
    embed = discord.Embed(title='💒 ¡Se casaron!', description=f'{ctx.author.mention} y <@{autor_id}> ¡Felicidades! 🎉', color=discord.Color.gold())
    await ctx.send(embed=embed)

@bot.command(name='rechazar')
async def rechazar(ctx):
    if ctx.author.id not in propuestas_pendientes:
        return await ctx.send('❌ Sin propuesta pendiente.')
    autor_id = propuestas_pendientes.pop(ctx.author.id)
    await ctx.send(f'💔 {ctx.author.mention} rechazó a <@{autor_id}>.')

@bot.command(name='divorcio', aliases=['divorciar'])
async def divorcio(ctx):
    parejas = cargar_parejas()
    uid = str(ctx.author.id)
    if uid not in parejas:
        return await ctx.send('❌ No estás casado/a.')
    ex_id = parejas.pop(uid)
    parejas.pop(str(ex_id), None)
    guardar_parejas(parejas)
    embed = discord.Embed(title='💔 Divorcio', description=f'{ctx.author.mention} se divorció de <@{ex_id}>.', color=discord.Color.red())
    await ctx.send(embed=embed)

@bot.command(name='pareja', aliases=['esposo', 'esposa'])
async def ver_pareja(ctx, member: discord.Member=None):
    member = member or ctx.author
    parejas = cargar_parejas()
    uid = str(member.id)
    if uid not in parejas:
        return await ctx.send(f'💔 {member.display_name} no está casado/a.')
    embed = discord.Embed(title='💍 Estado Civil', description=f'{member.mention} está con <@{parejas[uid]}> 💕', color=discord.Color.pink())
    await ctx.send(embed=embed)

@bot.command(name='adoptar')
async def adoptar(ctx, member: discord.Member):
    if member == ctx.author or member.bot:
        return await ctx.send('❌ No puedes adoptarte a ti mismo ni a un bot.')
    familia = cargar_familia()
    uid = str(ctx.author.id)
    mid = str(member.id)
    hijos = familia.get(uid, [])
    if mid in hijos:
        return await ctx.send(f'❌ {member.mention} ya es tu hijo/a.')
    hijos.append(mid)
    familia[uid] = hijos
    guardar_familia(familia)
    embed = discord.Embed(title='👨\u200d👧 ¡Adopción!', description=f'{ctx.author.mention} adoptó a {member.mention} 💕', color=discord.Color.green())
    await ctx.send(embed=embed)

@bot.command(name='familia')
async def ver_familia(ctx, member: discord.Member=None):
    member = member or ctx.author
    familia = cargar_familia()
    parejas = cargar_parejas()
    uid = str(member.id)
    hijos = familia.get(uid, [])
    pareja = parejas.get(uid)
    embed = discord.Embed(title=f'👨\u200d👩\u200d👧 Familia de {member.display_name}', color=discord.Color.green())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='💍 Pareja', value=f'<@{pareja}>' if pareja else 'Soltero/a', inline=False)
    embed.add_field(name='👶 Hijos', value='\n'.join((f'<@{h}>' for h in hijos)) if hijos else 'Sin hijos', inline=False)
    await ctx.send(embed=embed)
SIGNOS = {'aries': ('♈', '21 mar – 19 abr', 'Valiente, apasionado y directo.'), 'tauro': ('♉', '20 abr – 20 may', 'Leal, paciente y determinado.'), 'geminis': ('♊', '21 may – 20 jun', 'Curioso, adaptable y comunicativo.'), 'cancer': ('♋', '21 jun – 22 jul', 'Intuitivo, protector y empático.'), 'leo': ('♌', '23 jul – 22 ago', 'Carismático, generoso y líder nato.'), 'virgo': ('♍', '23 ago – 22 sep', 'Analítico, detallista y perfeccionista.'), 'libra': ('♎', '23 sep – 22 oct', 'Justo, diplomático y encantador.'), 'escorpio': ('♏', '23 oct – 21 nov', 'Intenso, misterioso y poderoso.'), 'sagitario': ('♐', '22 nov – 21 dic', 'Aventurero, optimista y filosófico.'), 'capricornio': ('♑', '22 dic – 19 ene', 'Ambicioso, disciplinado y responsable.'), 'acuario': ('♒', '20 ene – 18 feb', 'Innovador, independiente y humanitario.'), 'piscis': ('♓', '19 feb – 20 mar', 'Compasivo, artístico y soñador.')}
PREDICCIONES = ['🌟 Un encuentro inesperado cambiará tu día.', '💰 El dinero fluye si actúas con confianza.', '❤️ El amor está más cerca de lo que crees.', '⚠️ Evita decisiones impulsivas hoy.', '🎯 Tu concentración está al máximo.', '🌈 Buen día para empezar algo nuevo.', '🤝 Una amistad te sorprenderá positivamente.', '🔥 Tu energía es imparable, úsala bien.', '🌙 La noche traerá claridad a tus dudas.']

@bot.command(name='horoscopo', aliases=['signo', 'zodiac'])
async def horoscopo(ctx, *, signo: str):
    signo = signo.lower().strip()
    if signo not in SIGNOS:
        return await ctx.send(f"❌ Opciones: {', '.join((f'`{s}`' for s in SIGNOS))}")
    emoji, fechas, desc = SIGNOS[signo]
    embed = discord.Embed(title=f'{emoji} {signo.capitalize()}', color=random.randint(8912896, 16777215))
    embed.add_field(name='📅 Fechas', value=fechas, inline=True)
    embed.add_field(name='🍀 Suerte', value=f'{random.randint(1, 100)}%', inline=True)
    embed.add_field(name='✨ Personalidad', value=desc, inline=False)
    embed.add_field(name='🔮 Predicción', value=random.choice(PREDICCIONES), inline=False)
    await ctx.send(embed=embed)
TIPOS_PERSONALIDAD = [('🔥 Alma de Fuego', 'Intenso/a, apasionado/a y siempre vas al frente.'), ('🌊 Espíritu del Agua', 'Tranquilo/a, profundo/a y adaptable.'), ('🌪️ Mente del Viento', 'Veloz, creativo/a y lleno/a de ideas.'), ('🌍 Corazón de Tierra', 'Estable, confiable y roca para todos.'), ('⚡ Rayo de Energía', 'Energía inagotable que contagia a todos.'), ('🌙 Alma Lunar', 'Misterioso/a, intuitivo/a y emocional.'), ('☀️ Espíritu Solar', 'Irradias positividad y alegría.'), ('❄️ Mente de Hielo', 'Frío/a bajo presión y muy analítico/a.')]

@bot.command(name='personalidad', aliases=['quiensoy', 'tipo'])
async def personalidad(ctx, member: discord.Member=None):
    member = member or ctx.author
    random.seed(member.id + datetime.now(timezone.utc).toordinal())
    tipo, desc = random.choice(TIPOS_PERSONALIDAD)
    random.seed()
    embed = discord.Embed(title=f'🔮 {member.display_name}', description=f'**{tipo}**\n\n{desc}', color=discord.Color.purple())
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name='compatibilidad', aliases=['compat', 'shipper'])
async def compatibilidad(ctx, member: discord.Member):
    ids = sorted([ctx.author.id, member.id])
    random.seed(ids[0] + ids[1])
    pct = random.randint(1, 100)
    random.seed()
    if pct >= 80:
        estado = '💞 ¡Almas gemelas!'
        color = discord.Color.pink()
    elif pct >= 60:
        estado = '💕 Buena compatibilidad'
        color = discord.Color.magenta()
    elif pct >= 40:
        estado = '🤝 Compatible con esfuerzo'
        color = discord.Color.yellow()
    else:
        estado = '💔 Difícil combinación'
        color = discord.Color.red()
    barra = '█' * (pct // 10) + '░' * (10 - pct // 10)
    embed = discord.Embed(title='💘 Compatibilidad', color=color)
    embed.add_field(name='👫 Pareja', value=f'{ctx.author.mention} & {member.mention}', inline=False)
    embed.add_field(name='📊 Resultado', value=f'`{barra}` **{pct}%**', inline=False)
    embed.add_field(name='💬 Estado', value=estado, inline=False)
    await ctx.send(embed=embed)
juegos_activos = {}
PREGUNTAS_TRIVIA = [{'p': '¿Cuántos lados tiene un hexágono?', 'r': '6', 'ops': ['4', '5', '6', '8']}, {'p': '¿Capital de Japón?', 'r': 'tokio', 'ops': ['osaka', 'tokio', 'beijing', 'seul']}, {'p': '¿Planetas en el sistema solar?', 'r': '8', 'ops': ['7', '8', '9', '10']}, {'p': '¿Año del hombre en la luna?', 'r': '1969', 'ops': ['1965', '1969', '1971', '1973']}, {'p': '¿Elemento más abundante en el universo?', 'r': 'hidrogeno', 'ops': ['oxigeno', 'helio', 'hidrogeno', 'carbono']}, {'p': '¿Colores del arcoíris?', 'r': '7', 'ops': ['5', '6', '7', '8']}, {'p': '¿Animal más rápido del mundo?', 'r': 'guepardo', 'ops': ['leon', 'guepardo', 'tigre', 'aguila']}, {'p': '¿Océano más grande?', 'r': 'pacifico', 'ops': ['atlantico', 'indico', 'pacifico', 'artico']}, {'p': '¿Huesos del cuerpo humano adulto?', 'r': '206', 'ops': ['180', '196', '206', '220']}, {'p': '¿País más grande del mundo?', 'r': 'rusia', 'ops': ['canada', 'china', 'rusia', 'eeuu']}, {'p': '¿Planeta rojo?', 'r': 'marte', 'ops': ['venus', 'marte', 'jupiter', 'saturno']}, {'p': '¿Cuánto es 15 x 15?', 'r': '225', 'ops': ['200', '215', '225', '250']}, {'p': '¿Metal más caro del mundo?', 'r': 'rodio', 'ops': ['oro', 'platino', 'rodio', 'iridio']}, {'p': '¿Continente de Brasil?', 'r': 'america del sur', 'ops': ['africa', 'america central', 'america del sur', 'europa']}, {'p': '¿Segundos en una hora?', 'r': '3600', 'ops': ['1200', '3000', '3600', '4800']}]

@bot.command(name='trivia')
async def trivia(ctx):
    if ctx.channel.id in juegos_activos:
        return await ctx.send('❌ Ya hay una trivia activa.')
    p = random.choice(PREGUNTAS_TRIVIA)
    ops = p['ops'].copy()
    random.shuffle(ops)
    nums = ['1️⃣', '2️⃣', '3️⃣', '4️⃣']
    desc = '\n'.join((f'{nums[i]} {op.capitalize()}' for i, op in enumerate(ops)))
    embed = discord.Embed(title='🃏 Trivia', description=f"**{p['p']}**\n\n{desc}", color=discord.Color.blurple())
    msg = await ctx.send(embed=embed)
    for emoji in nums[:len(ops)]:
        await msg.add_reaction(emoji)
    juegos_activos[ctx.channel.id] = True

    def check(r, u):
        return r.message.id == msg.id and (not u.bot) and (str(r.emoji) in nums[:len(ops)])
    try:
        reaction, user = await bot.wait_for('reaction_add', timeout=20.0, check=check)
        elegida = ops[nums.index(str(reaction.emoji))]
        if elegida.lower() == p['r'].lower():
            await ctx.send(f"✅ ¡{user.mention} acertó! Era **{p['r'].capitalize()}** 🎉")
        else:
            await ctx.send(f"❌ {user.mention} falló. Era **{p['r'].capitalize()}**.")
    except asyncio.TimeoutError:
        await ctx.send(f"⌛ Tiempo. Era **{p['r'].capitalize()}**.")
    finally:
        juegos_activos.pop(ctx.channel.id, None)

@bot.command(name='adivina', aliases=['guess', 'numero'])
async def adivina_numero(ctx, maximo: int=100):
    if ctx.channel.id in juegos_activos:
        return await ctx.send('❌ Ya hay un juego activo.')
    if not 5 <= maximo <= 1000:
        return await ctx.send('❌ Máximo entre 5 y 1000.')
    numero = random.randint(1, maximo)
    juegos_activos[ctx.channel.id] = True
    intentos = 0
    max_intentos = 5
    embed = discord.Embed(title='🔢 Adivina el Número', description=f'Número entre **1 y {maximo}**. Tienes **{max_intentos} intentos**.', color=discord.Color.blurple())
    await ctx.send(embed=embed)

    def check(m):
        return m.channel == ctx.channel and (not m.author.bot) and m.content.isdigit()
    while intentos < max_intentos:
        try:
            msg = await bot.wait_for('message', timeout=30.0, check=check)
        except asyncio.TimeoutError:
            juegos_activos.pop(ctx.channel.id, None)
            return await ctx.send(f'⌛ Tiempo. Era **{numero}**.')
        intento = int(msg.content)
        intentos += 1
        restantes = max_intentos - intentos
        if intento == numero:
            juegos_activos.pop(ctx.channel.id, None)
            return await ctx.send(f'🎉 ¡{msg.author.mention} acertó! Era **{numero}** en {intentos} intento(s)!')
        pista = '📈 **Mayor**.' if intento < numero else '📉 **Menor**.'
        if restantes > 0:
            await ctx.send(f'{pista} Quedan **{restantes}**.')
        else:
            await ctx.send(f'😢 Era **{numero}**.')
    juegos_activos.pop(ctx.channel.id, None)
FRASES_PERSONAJES = {'naruto': ['¡No voy a rendirme!', '¡Cree en ti mismo!', '¡Seré Hokage!', 'El dolor te hace más fuerte.'], 'goku': ['¡Soy un Saiyan de la Tierra!', '¡Kamehameha!', 'No puedo perder. Hay gente que me importa.'], 'luffy': ['¡Seré el Rey de los Piratas!', '¡Libertad!', '¡Un hombre que no protege a sus amigos no vale nada!'], 'zoro': ['Nada me sucede hasta que yo digo que algo me sucede.', '¡Nunca perderé de nuevo!', 'Solo hay un camino: adelante.'], 'eren': ['Si no luchas, no puedes ganar.', 'La libertad es lo único que he querido.'], 'levi': ['La única forma de encontrar la respuesta es elegir y no arrepentirte.', 'Tus camaradas confían en ti.'], 'light': ['Soy el nuevo dios de este mundo.', 'El que gana tiene razón.'], 'itachi': ['Eres débil porque te falta odio.', 'El perdón es la base de la paz.', 'Siempre seré tu hermano mayor.'], 'todoroki': ['Uso mi poder como quiero.', 'No te debo nada.'], 'bakugo': ['¡Ganaré y me convertiré en el número 1!', '¡No necesito tu ayuda!']}

@bot.command(name='frase_personaje', aliases=['fp', 'anime_quote'])
async def frase_personaje(ctx, *, personaje: str=None):
    pers = list(FRASES_PERSONAJES.keys())
    if personaje is None:
        personaje = random.choice(pers)
    personaje = personaje.lower().strip()
    if personaje not in FRASES_PERSONAJES:
        return await ctx.send(f"❌ Disponibles: {', '.join((f'`{p}`' for p in pers))}")
    frase = random.choice(FRASES_PERSONAJES[personaje])
    colores = [discord.Color.red(), discord.Color.blue(), discord.Color.green(), discord.Color.purple(), discord.Color.orange()]
    embed = discord.Embed(title=f'💬 {personaje.capitalize()}', description=f'*"{frase}"*', color=random.choice(colores))
    await ctx.send(embed=embed)

@bot.command(name='personajes_lista', aliases=['pl'])
async def personajes_lista(ctx):
    lista = ', '.join((f'`{p.capitalize()}`' for p in FRASES_PERSONAJES))
    embed = discord.Embed(title='💬 Personajes disponibles', description=lista, color=discord.Color.blurple())
    await ctx.send(embed=embed)

@bot.command(name='lock', aliases=['bloquear'])
@commands.check(es_admin)
async def lock(ctx, canal: discord.TextChannel=None, *, razon: str='Sin razón'):
    canal = canal or ctx.channel
    ow = canal.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    await canal.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f'[{ctx.author}] {razon}')
    embed = discord.Embed(title='🔒 Canal Bloqueado', description=f'{canal.mention}\n📋 {razon}', color=discord.Color.red())
    await canal.send(embed=embed)
    if canal != ctx.channel:
        await ctx.send(f'✅ {canal.mention} bloqueado.')

@bot.command(name='unlock', aliases=['desbloquear'])
@commands.check(es_admin)
async def unlock(ctx, canal: discord.TextChannel=None, *, razon: str='Sin razón'):
    canal = canal or ctx.channel
    ow = canal.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    await canal.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f'[{ctx.author}] {razon}')
    embed = discord.Embed(title='🔓 Canal Desbloqueado', description=f'{canal.mention}\n📋 {razon}', color=discord.Color.green())
    await canal.send(embed=embed)
    if canal != ctx.channel:
        await ctx.send(f'✅ {canal.mention} desbloqueado.')

@bot.command(name='lockall', aliases=['bloquear_todo'])
@commands.check(es_admin)
async def lockall(ctx, *, razon: str='Sin razón'):
    msg = await ctx.send('⏳ Bloqueando todos los canales...')
    count = 0
    for c in ctx.guild.text_channels:
        try:
            ow = c.overwrites_for(ctx.guild.default_role)
            ow.send_messages = False
            await c.set_permissions(ctx.guild.default_role, overwrite=ow)
            count += 1
        except Exception:
            pass
    embed = discord.Embed(title='🔒 Servidor Bloqueado', description=f'**{count}** canales bloqueados.\n📋 {razon}', color=discord.Color.red())
    await msg.edit(content=None, embed=embed)

@bot.command(name='unlockall', aliases=['desbloquear_todo'])
@commands.check(es_admin)
async def unlockall(ctx, *, razon: str='Sin razón'):
    msg = await ctx.send('⏳ Desbloqueando...')
    count = 0
    for c in ctx.guild.text_channels:
        try:
            ow = c.overwrites_for(ctx.guild.default_role)
            ow.send_messages = None
            await c.set_permissions(ctx.guild.default_role, overwrite=ow)
            count += 1
        except Exception:
            pass
    embed = discord.Embed(title='🔓 Servidor Desbloqueado', description=f'**{count}** canales.\n📋 {razon}', color=discord.Color.green())
    await msg.edit(content=None, embed=embed)

@bot.command(name='slowmode', aliases=['sm', 'modo_lento'])
@commands.check(es_admin)
async def slowmode(ctx, segundos: int=0, canal: discord.TextChannel=None):
    canal = canal or ctx.channel
    if not 0 <= segundos <= 21600:
        return await ctx.send('❌ Entre 0 y 21600.')
    await canal.edit(slowmode_delay=segundos)
    if segundos == 0:
        await ctx.send(f'✅ Modo lento **off** en {canal.mention}.')
    else:
        await ctx.send(f'🐌 Modo lento {canal.mention}: **{segundos}s**.')

@bot.command(name='hide', aliases=['ocultar'])
@commands.check(es_admin)
async def hide(ctx, canal: discord.TextChannel=None):
    canal = canal or ctx.channel
    ow = canal.overwrites_for(ctx.guild.default_role)
    ow.view_channel = False
    await canal.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f'👁️ {canal.mention} **oculto**.')

@bot.command(name='show', aliases=['mostrar'])
@commands.check(es_admin)
async def show(ctx, canal: discord.TextChannel=None):
    canal = canal or ctx.channel
    ow = canal.overwrites_for(ctx.guild.default_role)
    ow.view_channel = None
    await canal.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f'👁️ {canal.mention} **visible**.')

@bot.command(name='topic', aliases=['tema'])
@commands.check(es_admin)
async def topic(ctx, *, texto: str):
    await ctx.channel.edit(topic=texto)
    await ctx.send(f'✅ Tema: **{texto}**')

@bot.command(name='rename_canal', aliases=['rc'])
@commands.check(es_admin)
async def rename_canal(ctx, *, nombre: str):
    nombre = nombre.lower().replace(' ', '-')
    viejo = ctx.channel.name
    await ctx.channel.edit(name=nombre)
    await ctx.send(f'✅ **#{viejo}** → **#{nombre}**')

@bot.command(name='crear_canal', aliases=['cc'])
@commands.check(es_admin)
async def crear_canal(ctx, *, nombre: str):
    nombre = nombre.lower().replace(' ', '-')
    c = await ctx.guild.create_text_channel(nombre, reason=f'Creado por {ctx.author}')
    await ctx.send(f'✅ Canal: {c.mention}')

@bot.command(name='eliminar_canal', aliases=['ec'])
@commands.check(es_admin)
async def eliminar_canal(ctx, canal: discord.TextChannel=None):
    canal = canal or ctx.channel
    nombre = canal.name
    await canal.delete(reason=f'Eliminado por {ctx.author}')
    if canal != ctx.channel:
        await ctx.send(f'🗑️ **#{nombre}** eliminado.')

@bot.command(name='clonar_canal', aliases=['clone'])
@commands.check(es_admin)
async def clonar_canal(ctx, canal: discord.TextChannel=None):
    canal = canal or ctx.channel
    nuevo = await canal.clone(reason=f'Clonado por {ctx.author}')
    await ctx.send(f'✅ Clonado: {nuevo.mention}')

@bot.command(name='nsfw')
@commands.check(es_admin)
async def nsfw_toggle(ctx, canal: discord.TextChannel=None):
    canal = canal or ctx.channel
    nuevo = not canal.is_nsfw()
    await canal.edit(nsfw=nuevo)
    await ctx.send(f"NSFW **{('activado 🔞' if nuevo else 'desactivado ✅')}** en {canal.mention}.")

@bot.command(name='dar_rol', aliases=['dr'])
@commands.check(es_admin)
async def dar_rol(ctx, member: discord.Member, *, nombre_rol: str):
    rol = discord.utils.get(ctx.guild.roles, name=nombre_rol)
    if not rol:
        nombre_lower = nombre_rol.lower()
        for r in ctx.guild.roles:
            if r.name.lower() == nombre_lower:
                rol = r
                break
    if not rol:
        similares = [r.name for r in ctx.guild.roles if nombre_rol.lower() in r.name.lower()][:5]
        msg = f'❌ No encontré el rol `{nombre_rol}`.'
        if similares:
            msg += f"\n¿Quisiste decir? {', '.join((f'`{s}`' for s in similares))}"
        return await ctx.send(msg)
    if rol >= ctx.guild.me.top_role:
        return await ctx.send(f'❌ No puedo dar **{rol.name}** porque está por encima de mi rol en la jerarquía.')
    if rol in member.roles:
        return await ctx.send(f'⚠️ {member.mention} ya tiene **{rol.name}**.')
    try:
        await member.add_roles(rol, reason=f'Dado por {ctx.author}')
        embed = discord.Embed(title='✅ Rol Dado', color=rol.color)
        embed.add_field(name='👤 Usuario', value=member.mention, inline=True)
        embed.add_field(name='🎭 Rol', value=rol.mention, inline=True)
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send('❌ Sin permisos para dar ese rol. Verifica la jerarquía del bot.')

@bot.command(name='quitar_rol', aliases=['qr'])
@commands.check(es_admin)
async def quitar_rol(ctx, member: discord.Member, *, nombre_rol: str):
    rol = discord.utils.get(ctx.guild.roles, name=nombre_rol)
    if not rol:
        for r in ctx.guild.roles:
            if r.name.lower() == nombre_rol.lower():
                rol = r
                break
    if not rol:
        return await ctx.send(f'❌ No encontré `{nombre_rol}`.')
    if rol >= ctx.guild.me.top_role:
        return await ctx.send(f'❌ No puedo gestionar **{rol.name}** (jerarquía).')
    if rol not in member.roles:
        return await ctx.send(f'⚠️ {member.mention} no tiene **{rol.name}**.')
    try:
        await member.remove_roles(rol, reason=f'Quitado por {ctx.author}')
        embed = discord.Embed(title='✅ Rol Quitado', color=discord.Color.red())
        embed.add_field(name='👤 Usuario', value=member.mention, inline=True)
        embed.add_field(name='🎭 Rol', value=rol.name, inline=True)
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send('❌ Sin permisos para quitar ese rol.')

@bot.command(name='crear_rol', aliases=['cr'])
@commands.check(es_admin)
async def crear_rol(ctx, color: str='#99AAB5', *, nombre: str):
    try:
        color_obj = discord.Color.from_str(color)
    except Exception:
        return await ctx.send('❌ Color inválido. Usa `#RRGGBB`.')
    rol = await ctx.guild.create_role(name=nombre, color=color_obj, reason=f'Creado por {ctx.author}')
    await ctx.send(f'✅ Rol {rol.mention} creado.')

@bot.command(name='eliminar_rol', aliases=['er'])
@commands.check(es_admin)
async def eliminar_rol(ctx, *, nombre_rol: str):
    rol = discord.utils.get(ctx.guild.roles, name=nombre_rol)
    if not rol:
        for r in ctx.guild.roles:
            if r.name.lower() == nombre_rol.lower():
                rol = r
                break
    if not rol:
        return await ctx.send(f'❌ Rol `{nombre_rol}` no encontrado.')
    try:
        await rol.delete(reason=f'Eliminado por {ctx.author}')
        await ctx.send(f'🗑️ **{nombre_rol}** eliminado.')
    except discord.Forbidden:
        await ctx.send('❌ Sin permisos.')

@bot.command(name='roles_usuario', aliases=['ru'])
async def roles_usuario(ctx, member: discord.Member=None):
    member = member or ctx.author
    roles = [r.mention for r in reversed(member.roles) if r != ctx.guild.default_role]
    embed = discord.Embed(title=f'🎭 Roles de {member.display_name}', color=member.color)
    embed.description = ' '.join(roles) if roles else 'Sin roles'
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name='listar_roles', aliases=['lroles'])
@commands.check(es_admin)
async def listar_roles(ctx):
    roles = [r for r in reversed(ctx.guild.roles) if r != ctx.guild.default_role]
    if not roles:
        return await ctx.send('❌ Sin roles.')
    paginas = []
    chunk = ''
    for r in roles:
        linea = f'{r.mention} — `{r.id}`\n'
        if len(chunk) + len(linea) > 900:
            paginas.append(chunk)
            chunk = ''
        chunk += linea
    if chunk:
        paginas.append(chunk)
    for i, p in enumerate(paginas, 1):
        embed = discord.Embed(title=f'🎭 Roles ({i}/{len(paginas)})', description=p, color=discord.Color.blurple())
        await ctx.send(embed=embed)

@bot.command(name='anuncio', aliases=['ann'])
@commands.check(es_admin)
async def anuncio(ctx, canal: discord.TextChannel=None, *, mensaje: str):
    canal = canal or ctx.channel
    embed = discord.Embed(title='📢 Anuncio', description=mensaje, color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    await canal.send('@everyone', embed=embed)
    if canal != ctx.channel:
        await ctx.send(f'✅ Anuncio en {canal.mention}.')

@bot.command(name='embed_msg', aliases=['emb'])
@commands.check(es_admin)
async def embed_msg(ctx, canal: discord.TextChannel=None, titulo: str='Mensaje', *, mensaje: str):
    canal = canal or ctx.channel
    embed = discord.Embed(title=titulo, description=mensaje, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    await canal.send(embed=embed)
    if canal != ctx.channel:
        await ctx.send(f'✅ Embed en {canal.mention}.')

@bot.command(name='ban')
@commands.check(es_admin)
async def ban_cmd(ctx, member: discord.Member, *, razon: str='Sin razón'):
    if member == ctx.author:
        return await ctx.send('❌ No puedes banearte.')
    if member.guild_permissions.administrator:
        return await ctx.send('❌ No puedes banear a un admin.')
    try:
        await ctx.guild.ban(member, reason=f'[{ctx.author}] {razon}', delete_message_days=0)
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    embed = discord.Embed(title='🔨 Baneado', color=discord.Color.red())
    embed.add_field(name='👤 Usuario', value=f'{member} (`{member.id}`)', inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='👮 Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)

@bot.command(name='unban')
@commands.check(es_admin)
async def unban_cmd(ctx, *, usuario: str):
    bans = [entry async for entry in ctx.guild.bans()]
    objetivo = None
    for entry in bans:
        if str(entry.user.id) == usuario or str(entry.user) == usuario:
            objetivo = entry.user
            break
    if not objetivo:
        return await ctx.send(f'❌ No encontré `{usuario}` en los bans.')
    await ctx.guild.unban(objetivo, reason=f'Desbaneado por {ctx.author}')
    embed = discord.Embed(title='✅ Desbaneado', color=discord.Color.green())
    embed.add_field(name='👤 Usuario', value=f'{objetivo} (`{objetivo.id}`)', inline=True)
    embed.add_field(name='👮 Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)

@bot.command(name='kick')
@commands.check(es_admin)
async def kick_cmd(ctx, member: discord.Member, *, razon: str='Sin razón'):
    if member == ctx.author:
        return await ctx.send('❌ No puedes kickearte.')
    try:
        await ctx.guild.kick(member, reason=f'[{ctx.author}] {razon}')
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    embed = discord.Embed(title='👢 Expulsado', color=discord.Color.orange())
    embed.add_field(name='👤 Usuario', value=str(member), inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='👮 Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)

@bot.command(name='mute')
@commands.check(es_admin)
async def mute_cmd(ctx, member: discord.Member, minutos: int=10, *, razon: str='Sin razón'):
    if not 1 <= minutos <= 40320:
        return await ctx.send('❌ Entre 1 y 40320 minutos.')
    import datetime as dt
    try:
        until = discord.utils.utcnow() + dt.timedelta(minutes=minutos)
        await member.timeout(until, reason=f'[{ctx.author}] {razon}')
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    embed = discord.Embed(title='🔇 Muteado', color=discord.Color.dark_grey())
    embed.add_field(name='👤 Usuario', value=member.mention, inline=True)
    embed.add_field(name='⏰ Duración', value=f'{minutos} min', inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='👮 Por', value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)

@bot.command(name='unmute')
@commands.check(es_admin)
async def unmute_cmd(ctx, member: discord.Member):
    try:
        await member.timeout(None)
    except discord.Forbidden:
        return await ctx.send('❌ Sin permisos.')
    await ctx.send(f'✅ {member.mention} **desmuteado**.')

@bot.command(name='limpiar', aliases=['clear', 'purge'])
@commands.check(es_admin)
async def limpiar(ctx, cantidad: int=10):
    if not 1 <= cantidad <= 100:
        return await ctx.send('❌ Entre 1 y 100.')
    borrados = await ctx.channel.purge(limit=cantidad + 1)
    msg = await ctx.send(f'🗑️ **{len(borrados) - 1}** mensajes borrados.')
    await asyncio.sleep(3)
    await msg.delete()

@bot.command(name='limpiar_bots', aliases=['purgebots'])
@commands.check(es_admin)
async def limpiar_bots(ctx, cantidad: int=50):

    def es_bot_msg(m):
        return m.author.bot
    borrados = await ctx.channel.purge(limit=cantidad, check=es_bot_msg)
    msg = await ctx.send(f'🤖 **{len(borrados)}** mensajes de bots borrados.')
    await asyncio.sleep(3)
    await msg.delete()

@bot.command(name='limpiar_usuario', aliases=['purgeuser'])
@commands.check(es_admin)
async def limpiar_usuario(ctx, member: discord.Member, cantidad: int=50):

    def es_usuario(m):
        return m.author == member
    borrados = await ctx.channel.purge(limit=cantidad, check=es_usuario)
    msg = await ctx.send(f'🗑️ **{len(borrados)}** mensajes de {member.mention} borrados.')
    await asyncio.sleep(3)
    await msg.delete()

@bot.command(name='userinfo', aliases=['ui', 'whois'])
async def userinfo(ctx, member: discord.Member=None):
    member = member or ctx.author
    roles = [r.mention for r in member.roles if r != ctx.guild.default_role]
    embed = discord.Embed(title=f'👤 {member}', color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='🆔 ID', value=member.id, inline=True)
    embed.add_field(name='📅 Cuenta', value=member.created_at.strftime('%d/%m/%Y'), inline=True)
    embed.add_field(name='📥 Se unió', value=member.joined_at.strftime('%d/%m/%Y'), inline=True)
    embed.add_field(name='🎨 Color', value=str(member.color), inline=True)
    embed.add_field(name='🤖 Bot', value='Sí' if member.bot else 'No', inline=True)
    embed.add_field(name='🏆 Roles', value=' '.join(roles) if roles else 'Sin roles', inline=False)
    await ctx.send(embed=embed)

@bot.command(name='serverinfo', aliases=['si', 'servidor'])
async def serverinfo(ctx):
    g = ctx.guild
    embed = discord.Embed(title=f'🏠 {g.name}', color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name='🆔 ID', value=g.id, inline=True)
    embed.add_field(name='👑 Dueño', value=g.owner.mention, inline=True)
    embed.add_field(name='👥 Miembros', value=g.member_count, inline=True)
    embed.add_field(name='💬 Canales', value=len(g.channels), inline=True)
    embed.add_field(name='🎭 Roles', value=len(g.roles), inline=True)
    embed.add_field(name='📅 Creado', value=g.created_at.strftime('%d/%m/%Y'), inline=True)
    embed.add_field(name='📢 Nivel verificación', value=str(g.verification_level), inline=True)
    embed.add_field(name='💎 Boosts', value=g.premium_subscription_count, inline=True)
    await ctx.send(embed=embed)

@bot.command(name='nick', aliases=['apodo'])
@commands.check(es_admin)
async def nick(ctx, member: discord.Member, *, nuevo: str=None):
    try:
        viejo = member.display_name
        await member.edit(nick=nuevo)
        if nuevo:
            await ctx.send(f'✅ Nick de {member.mention}: **{viejo}** → **{nuevo}**')
        else:
            await ctx.send(f'✅ Nick de {member.mention} restablecido.')
    except discord.Forbidden:
        await ctx.send('❌ Sin permisos para cambiar ese nick.')

@bot.command(name='massnick')
@commands.check(es_admin)
async def massnick(ctx, *, nuevo: str):
    msg = await ctx.send(f'⏳ Cambiando nicks de **{ctx.guild.member_count}** miembros...')
    count = 0
    for m in ctx.guild.members:
        if not m.bot:
            try:
                await m.edit(nick=nuevo)
                count += 1
            except Exception:
                pass
    await msg.edit(content=f'✅ Nick cambiado a **{nuevo}** en **{count}** miembros.')
ROLES_POR_SERVIDOR = {1476763559982534829: {'dar': 1477556485092544532, 'quitar': 1479630235283624049}, 1473493322403414280: {'dar': 1473493514770972922, 'quitar': None}, 1480185559145250907: {'dar': 1473493514770972922, 'quitar': None}}

class BuscarRolModal(discord.ui.Modal):

    def __init__(self, tipo: str, view):
        super().__init__(title=f"{('🟢 Rol a DAR' if tipo == 'dar' else '🔴 Rol a QUITAR')}")
        self.tipo = tipo
        self.parent_view = view
        self.input = discord.ui.TextInput(label='Nombre del rol (parcial o completo)', placeholder='Ej: Members, sin acceso, Admin...', required=True, max_length=100)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        buscar = self.input.value.lower().strip()
        if self.tipo == 'quitar' and buscar in ('todos', 'all', 'todo'):
            self.parent_view.rol_quitar_id = 'ALL'
            await interaction.response.send_message('🗑️ Se quitarán **TODOS** los roles.', ephemeral=True)
            return
        coincidencias = [r for r in guild.roles if buscar in r.name.lower() and r != guild.default_role and (not r.managed) and (r < guild.me.top_role)]
        if not coincidencias:
            await interaction.response.send_message(f'❌ No encontré ningún rol con `{self.input.value}`. Intenta de nuevo.', ephemeral=True)
            return
        if len(coincidencias) == 1:
            rol = coincidencias[0]
            if self.tipo == 'dar':
                self.parent_view.rol_dar_id = rol.id
                await interaction.response.send_message(f'🟢 Rol a dar: **{rol.name}**', ephemeral=True)
            else:
                self.parent_view.rol_quitar_id = rol.id
                await interaction.response.send_message(f'🔴 Rol a quitar: **{rol.name}**', ephemeral=True)
        else:
            opts = [discord.SelectOption(label=r.name[:100], value=str(r.id)) for r in coincidencias[:25]]
            view_sel = SeleccionarRolView(opts, self.tipo, self.parent_view)
            await interaction.response.send_message(f'🔍 Encontré **{len(coincidencias)}** roles. Selecciona uno:', view=view_sel, ephemeral=True)

class SeleccionarRolView(discord.ui.View):

    def __init__(self, opciones, tipo, parent_view):
        super().__init__(timeout=30)
        self.tipo = tipo
        self.parent_view = parent_view
        sel = discord.ui.Select(placeholder='Selecciona el rol...', options=opciones)
        sel.callback = self.cb_sel
        self.add_item(sel)

    async def cb_sel(self, interaction: discord.Interaction):
        rol_id = int(interaction.data['values'][0])
        rol = interaction.guild.get_role(rol_id)
        if self.tipo == 'dar':
            self.parent_view.rol_dar_id = rol_id
            await interaction.response.send_message(f'🟢 Rol a dar: **{(rol.name if rol else rol_id)}**', ephemeral=True)
        else:
            self.parent_view.rol_quitar_id = rol_id
            await interaction.response.send_message(f'🔴 Rol a quitar: **{(rol.name if rol else rol_id)}**', ephemeral=True)
        self.stop()

class VerView(discord.ui.View):

    def __init__(self, ctx, member: discord.Member):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.member = member
        self.confirmado = False
        self.rol_dar_id = ROLES_POR_SERVIDOR.get(ctx.guild.id, {}).get('dar')
        self.rol_quitar_id = ROLES_POR_SERVIDOR.get(ctx.guild.id, {}).get('quitar', 'ALL')
        if self.rol_quitar_id is None:
            self.rol_quitar_id = 'ALL'
        btn_dar = discord.ui.Button(label='🟢 Cambiar rol a dar', style=discord.ButtonStyle.primary, row=0)
        btn_dar.callback = self.cb_abrir_dar
        self.add_item(btn_dar)
        btn_quitar = discord.ui.Button(label='🔴 Cambiar rol a quitar', style=discord.ButtonStyle.secondary, row=0)
        btn_quitar.callback = self.cb_abrir_quitar
        self.add_item(btn_quitar)
        btn_todos = discord.ui.Button(label='🗑️ Quitar todos los roles', style=discord.ButtonStyle.secondary, row=1)
        btn_todos.callback = self.cb_todos
        self.add_item(btn_todos)
        btn_ok = discord.ui.Button(label='✅ Confirmar', style=discord.ButtonStyle.success, row=2)
        btn_ok.callback = self.cb_confirmar
        self.add_item(btn_ok)
        btn_cancel = discord.ui.Button(label='❌ Cancelar', style=discord.ButtonStyle.danger, row=2)
        btn_cancel.callback = self.cb_cancelar
        self.add_item(btn_cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message('❌ Solo quien ejecutó el comando puede usar esto.', ephemeral=True)
            return False
        return True

    async def cb_abrir_dar(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BuscarRolModal('dar', self))

    async def cb_abrir_quitar(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BuscarRolModal('quitar', self))

    async def cb_todos(self, interaction: discord.Interaction):
        self.rol_quitar_id = 'ALL'
        await interaction.response.send_message('🗑️ Se quitarán **TODOS** los roles al confirmar.', ephemeral=True)

    async def cb_confirmar(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.confirmado = True
        self.stop()

    async def cb_cancelar(self, interaction: discord.Interaction):
        await interaction.response.send_message('❌ Cancelado.', ephemeral=True)
        self.stop()

@bot.command(name='v')
@commands.check(es_admin)
async def dar_rol_arn(ctx, member: discord.Member):
    cfg_srv = ROLES_POR_SERVIDOR.get(ctx.guild.id, {})
    dar_default = cfg_srv.get('dar')
    quitar_default = cfg_srv.get('quitar')
    rol_dar_nombre = ctx.guild.get_role(dar_default).name if dar_default and ctx.guild.get_role(dar_default) else 'Sin configurar'
    rol_quitar_nombre = ctx.guild.get_role(quitar_default).name if quitar_default and ctx.guild.get_role(quitar_default) else 'Todos los roles'
    embed = discord.Embed(title='🔑 Dar Acceso — Configuración', description=f'Configurando acceso para {member.mention}\n\n🟢 **Cambiar rol a dar** — escribe el nombre del rol\n🔴 **Cambiar rol a quitar** — escribe el nombre del rol\n🗑️ **Quitar todos** — elimina todos los roles del usuario\n\nO pulsa **✅ Confirmar** para usar los valores por defecto.', color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='👤 Usuario', value=member.mention, inline=True)
    embed.add_field(name='🟢 Rol a dar', value=f'**{rol_dar_nombre}**', inline=True)
    embed.add_field(name='🔴 Rol(es) a quitar', value=f'**{rol_quitar_nombre}**', inline=True)
    view = VerView(ctx, member)
    msg = await ctx.send(embed=embed, view=view)
    await view.wait()
    try:
        await msg.delete()
    except Exception:
        pass
    if not view.confirmado:
        return
    rol_dar_id = view.rol_dar_id
    rol_quitar_id = view.rol_quitar_id
    if not rol_dar_id:
        return await ctx.send('❌ No hay rol configurado para dar. Selecciona uno en el menú.')
    rol_dar = ctx.guild.get_role(rol_dar_id)
    if not rol_dar:
        return await ctx.send('❌ No encontré el rol a dar.')
    roles_quitados = []
    roles_fallidos = []
    if rol_quitar_id == 'ALL':
        roles_a_quitar = [r for r in member.roles if r != ctx.guild.default_role and (not r.managed) and (r < ctx.guild.me.top_role) and (r.id != rol_dar.id)]
    else:
        r = ctx.guild.get_role(rol_quitar_id)
        roles_a_quitar = [r] if r and r in member.roles else []
    if roles_a_quitar:
        try:
            await member.remove_roles(*roles_a_quitar, reason=f'!v — {ctx.author}')
            roles_quitados = roles_a_quitar
        except discord.Forbidden:
            for r in roles_a_quitar:
                try:
                    await member.remove_roles(r, reason=f'!v — {ctx.author}')
                    roles_quitados.append(r)
                except discord.Forbidden:
                    roles_fallidos.append(r)
    try:
        await member.add_roles(rol_dar, reason=f'!v — acceso por {ctx.author}')
    except discord.Forbidden:
        return await ctx.send(f'❌ No pude asignar **{rol_dar.name}**. Sube el rol del bot en la jerarquía.')
    embed_ok = discord.Embed(title='✅ Acceso Concedido', color=discord.Color.green())
    embed_ok.set_thumbnail(url=member.display_avatar.url)
    embed_ok.add_field(name='👤 Miembro', value=member.mention, inline=True)
    embed_ok.add_field(name='✅ Rol dado', value=f'**{rol_dar.name}**', inline=True)
    embed_ok.add_field(name='✍️ Por', value=ctx.author.mention, inline=True)
    if roles_quitados:
        embed_ok.add_field(name=f'🗑️ Roles quitados ({len(roles_quitados)})', value=', '.join((f'`{r.name}`' for r in roles_quitados)), inline=False)
    if roles_fallidos:
        embed_ok.add_field(name='⚠️ No se pudieron quitar', value=', '.join((f'`{r.name}`' for r in roles_fallidos)), inline=False)
    msg_ok = await ctx.send(embed=embed_ok)
    await asyncio.sleep(15)
    await msg_ok.delete()

@dar_rol_arn.error
async def dar_rol_arn_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f'❌ Uso: `{PREFIX}v @usuario`')
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send('❌ Usuario no encontrado.')
    elif isinstance(error, commands.CheckFailure):
        await ctx.send('🔒 Solo administradores.')
BOTCONFIG_FILE = 'botconfig.json'

def cargar_botconfig() -> dict:
    if os.path.exists(BOTCONFIG_FILE):
        with open(BOTCONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'prefix': PREFIX}

def guardar_botconfig(cfg: dict):
    with open(BOTCONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)

@bot.command(name='setprefix', aliases=['prefix', 'cambiar_prefijo'])
@commands.check(es_owner_o_admin)
async def setprefix(ctx, nuevo: str):
    if len(nuevo) > 3:
        return await ctx.send('❌ Máx 3 caracteres.')
    viejo = bot.command_prefix
    cfg = cargar_botconfig()
    cfg['prefix'] = nuevo
    guardar_botconfig(cfg)
    bot.command_prefix = nuevo
    await ctx.send(f'✅ Prefijo: `{viejo}` → `{nuevo}`')

@bot.command(name='ping')
async def ping(ctx):
    lat = round(bot.latency * 1000)
    color = discord.Color.green() if lat < 100 else discord.Color.yellow() if lat < 200 else discord.Color.red()
    await ctx.send(embed=discord.Embed(title='🏓 Pong!', description=f'**{lat}ms**', color=color))

@bot.command(name='say')
@commands.check(es_admin)
async def say(ctx, *, mensaje: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(mensaje, allowed_mentions=discord.AllowedMentions.all())

@bot.command(name='avatar', aliases=['av', 'foto'])
async def avatar(ctx, member: discord.Member=None):
    member = member or ctx.author
    embed = discord.Embed(title=f'🖼️ {member.display_name}', color=member.color)
    embed.set_image(url=member.display_avatar.url)
    embed.add_field(name='🔗 Link', value=f'[Descargar]({member.display_avatar.url})', inline=False)
    await ctx.send(embed=embed)

@bot.command(name='banner')
async def banner(ctx, member: discord.Member=None):
    member = member or ctx.author
    user = await bot.fetch_user(member.id)
    if not user.banner:
        return await ctx.send(f'❌ {member.display_name} no tiene banner.')
    embed = discord.Embed(title=f'🖼️ Banner de {member.display_name}', color=member.color)
    embed.set_image(url=user.banner.url)
    await ctx.send(embed=embed)

@bot.command(name='stats', aliases=['estadisticas'])
async def stats(ctx):
    g = ctx.guild
    total = g.member_count
    bots = sum((1 for m in g.members if m.bot))
    humanos = total - bots
    en_linea = sum((1 for m in g.members if m.status != discord.Status.offline and (not m.bot)))
    embed = discord.Embed(title=f'📊 {g.name}', color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name='👥 Total', value=total, inline=True)
    embed.add_field(name='🧑 Humanos', value=humanos, inline=True)
    embed.add_field(name='🤖 Bots', value=bots, inline=True)
    embed.add_field(name='🟢 En línea', value=en_linea, inline=True)
    embed.add_field(name='💬 Canales', value=len(g.text_channels), inline=True)
    embed.add_field(name='🔊 Voz', value=len(g.voice_channels), inline=True)
    embed.add_field(name='🎭 Roles', value=len(g.roles), inline=True)
    embed.add_field(name='😄 Emojis', value=len(g.emojis), inline=True)
    await ctx.send(embed=embed)

@bot.command(name='botinfo', aliases=['bot_info'])
async def botinfo(ctx):
    import platform
    embed = discord.Embed(title='🤖 Info del Bot', color=discord.Color.blurple())
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name='🏷️ Nombre', value=str(bot.user), inline=True)
    embed.add_field(name='🆔 ID', value=bot.user.id, inline=True)
    embed.add_field(name='🖥️ Python', value=platform.python_version(), inline=True)
    embed.add_field(name='📚 discord.py', value=discord.__version__, inline=True)
    embed.add_field(name='🏠 Servidores', value=len(bot.guilds), inline=True)
    embed.add_field(name='👥 Usuarios', value=len(bot.users), inline=True)
    embed.add_field(name='📜 Comandos', value=len(bot.commands), inline=True)
    embed.add_field(name='⚙️ Prefijo', value=f'`{bot.command_prefix}`', inline=True)
    await ctx.send(embed=embed)

@bot.command(name='invitar', aliases=['invite'])
async def invitar(ctx):
    url = f'https://discord.com/api/oauth2/authorize?client_id={bot.user.id}&permissions=8&scope=bot'
    embed = discord.Embed(title='🔗 Invitar', description=f'[Clic aquí]({url})', color=discord.Color.blurple())
    await ctx.send(embed=embed)

@bot.command(name='clima', aliases=['weather', 'tiempo'])
async def clima(ctx, *, ciudad: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://wttr.in/{ciudad.replace(' ', '+')}?format=j1") as resp:
                if resp.status != 200:
                    return await ctx.send('❌ Ciudad no encontrada.')
                data = await resp.json()
                actual = data['current_condition'][0]
                embed = discord.Embed(title=f'🌤️ {ciudad.title()}', color=discord.Color.blue())
                embed.add_field(name='🌡️ Temp', value=f"{actual['temp_C']}°C", inline=True)
                embed.add_field(name='🤔 Sensación', value=f"{actual['FeelsLikeC']}°C", inline=True)
                embed.add_field(name='💧 Humedad', value=f"{actual['humidity']}%", inline=True)
                embed.add_field(name='💨 Viento', value=f"{actual['windspeedKmph']} km/h", inline=True)
                embed.add_field(name='☁️ Estado', value=actual['weatherDesc'][0]['value'], inline=True)
                await ctx.send(embed=embed)
    except Exception:
        await ctx.send('❌ No pude obtener el clima.')

@bot.command(name='traducir', aliases=['translate', 'tr'])
async def traducir(ctx, idioma: str, *, texto: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://api.mymemory.translated.net/get?q={texto}&langpair=es|{idioma}') as resp:
                data = await resp.json()
                trad = data['responseData']['translatedText']
                embed = discord.Embed(title='🌍 Traducción', color=discord.Color.teal())
                embed.add_field(name='📝 Original', value=texto, inline=False)
                embed.add_field(name='✅ Traducido', value=trad, inline=False)
                embed.add_field(name='🌐 Idioma', value=idioma, inline=True)
                await ctx.send(embed=embed)
    except Exception:
        await ctx.send('❌ No pude traducir.')

@bot.command(name='calcular', aliases=['calc', 'matematica'])
async def calcular(ctx, *, expresion: str):
    try:
        if not all((c in '0123456789+-*/.() ' for c in expresion)):
            return await ctx.send('❌ Solo `+ - * / ( )`.')
        resultado = eval(expresion)
        embed = discord.Embed(title='🧮 Calculadora', color=discord.Color.green())
        embed.add_field(name='📝', value=f'`{expresion}`', inline=False)
        embed.add_field(name='✅', value=f'**{resultado}**', inline=False)
        await ctx.send(embed=embed)
    except ZeroDivisionError:
        await ctx.send('❌ División entre cero.')
    except Exception:
        await ctx.send('❌ Expresión inválida.')

@bot.command(name='color')
async def color_cmd(ctx, *, hex_color: str):
    hex_color = hex_color.strip('#')
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except Exception:
        return await ctx.send('❌ Usa `!color FF0000`')
    embed = discord.Embed(title=f'🎨 #{hex_color.upper()}', color=int(hex_color, 16))
    embed.add_field(name='R', value=r, inline=True)
    embed.add_field(name='G', value=g, inline=True)
    embed.add_field(name='B', value=b, inline=True)
    embed.set_thumbnail(url=f'https://singlecolorimage.com/get/{hex_color}/100x100')
    await ctx.send(embed=embed)

@bot.command(name='sugerencia', aliases=['suggest'])
async def sugerencia(ctx, canal: discord.TextChannel=None, *, texto: str):
    canal = canal or ctx.channel
    embed = discord.Embed(title='💡 Sugerencia', description=texto, color=discord.Color.yellow(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    msg = await canal.send(embed=embed)
    await msg.add_reaction('✅')
    await msg.add_reaction('❌')
    if canal != ctx.channel:
        await ctx.send(f'✅ Enviada en {canal.mention}.')

@bot.command(name='reporte', aliases=['report'])
async def reporte(ctx, member: discord.Member, *, razon: str):
    if member == ctx.author:
        return await ctx.send('❌ No puedes reportarte.')
    embed = discord.Embed(title='🚨 Reporte', color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name='👤 Reportado', value=f'{member.mention} (`{member.id}`)', inline=False)
    embed.add_field(name='📋 Razón', value=razon, inline=False)
    embed.add_field(name='📩 Por', value=ctx.author.mention, inline=False)
    embed.add_field(name='📍 Canal', value=ctx.channel.mention, inline=False)
    cfg = cargar_antinuke(ctx.guild.id)
    log_ch_id = cfg.get('log_channel')
    canal_destino = ctx.guild.get_channel(int(log_ch_id)) if log_ch_id else ctx.channel
    await canal_destino.send(embed=embed)
    try:
        await ctx.message.delete()
    except Exception:
        pass
    try:
        await ctx.author.send(f'✅ Reporte sobre **{member.display_name}** enviado.')
    except Exception:
        pass

@bot.command(name='dado_personalizado', aliases=['dp'])
async def dado_personalizado(ctx, cantidad: int=1, lados: int=6):
    if not 1 <= cantidad <= 20:
        return await ctx.send('❌ Entre 1 y 20 dados.')
    if not 2 <= lados <= 1000:
        return await ctx.send('❌ Entre 2 y 1000 lados.')
    resultados = [random.randint(1, lados) for _ in range(cantidad)]
    total = sum(resultados)
    embed = discord.Embed(title=f'🎲 {cantidad}d{lados}', color=discord.Color.blurple())
    embed.add_field(name='Resultados', value=' + '.join((f'`{r}`' for r in resultados)), inline=False)
    embed.add_field(name='Total', value=f'**{total}**', inline=True)
    if cantidad > 1:
        embed.add_field(name='Promedio', value=f'**{total / cantidad:.1f}**', inline=True)
    await ctx.send(embed=embed)

@bot.command(name='recordar', aliases=['remind', 'reminder'])
async def recordar(ctx, tiempo: str, *, mensaje: str):
    unidades = {'s': 1, 'm': 60, 'h': 3600}
    try:
        unidad = tiempo[-1].lower()
        cantidad = int(tiempo[:-1])
        if unidad not in unidades or not 1 <= cantidad <= 86400:
            raise ValueError
    except Exception:
        return await ctx.send('❌ Ej: `!recordar 10m mensaje` (s/m/h)')
    segundos = cantidad * unidades[unidad]
    nombres = {'s': 'segundo(s)', 'm': 'minuto(s)', 'h': 'hora(s)'}
    await ctx.send(f'⏰ Te recordaré en **{cantidad} {nombres[unidad]}**.')
    await asyncio.sleep(segundos)
    try:
        embed = discord.Embed(title='⏰ Recordatorio', description=mensaje, color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        await ctx.author.send(embed=embed)
    except Exception:
        pass
    await ctx.send(f'⏰ {ctx.author.mention} ¡Recordatorio! **{mensaje}**')
CUMPLE_FILE = 'cumpleanos.json'

def cargar_cumples() -> dict:
    if os.path.exists(CUMPLE_FILE):
        with open(CUMPLE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def guardar_cumples(data: dict):
    with open(CUMPLE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

@bot.command(name='cumple', aliases=['birthday'])
async def cumple(ctx, fecha: str=None):
    if fecha is None:
        cumples = cargar_cumples()
        uid = str(ctx.author.id)
        if uid in cumples:
            return await ctx.send(f'🎂 Tu cumpleaños: **{cumples[uid]}**.')
        return await ctx.send('❌ No tienes cumpleaños. Usa `!cumple DD/MM`.')
    try:
        dia, mes = map(int, fecha.split('/'))
        if not (1 <= dia <= 31 and 1 <= mes <= 12):
            raise ValueError
    except Exception:
        return await ctx.send('❌ Usa `DD/MM`. Ej: `!cumple 25/12`')
    cumples = cargar_cumples()
    cumples[str(ctx.author.id)] = f'{dia:02d}/{mes:02d}'
    guardar_cumples(cumples)
    await ctx.send(f'🎂 Registrado: **{dia:02d}/{mes:02d}**')

@bot.command(name='cumple_ver', aliases=['ver_cumple'])
async def cumple_ver(ctx, member: discord.Member=None):
    member = member or ctx.author
    cumples = cargar_cumples()
    uid = str(member.id)
    if uid not in cumples:
        return await ctx.send(f'❌ {member.display_name} sin cumpleaños.')
    fecha = cumples[uid]
    dia, mes = map(int, fecha.split('/'))
    hoy = datetime.now(timezone.utc)
    este = datetime(hoy.year, mes, dia, tzinfo=timezone.utc)
    if este < hoy:
        este = datetime(hoy.year + 1, mes, dia, tzinfo=timezone.utc)
    dias = (este - hoy).days
    embed = discord.Embed(title=f'🎂 {member.display_name}', color=discord.Color.gold())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name='📅 Fecha', value=fecha, inline=True)
    embed.add_field(name='⏰ Faltan', value=f'**{dias}** días', inline=True)
    await ctx.send(embed=embed)

@bot.command(name='cumples_lista', aliases=['lista_cumples'])
async def cumples_lista(ctx):
    cumples = cargar_cumples()
    if not cumples:
        return await ctx.send('❌ Nadie ha registrado cumpleaños.')
    hoy = datetime.now(timezone.utc)
    lista = []
    for uid, fecha in cumples.items():
        try:
            dia, mes = map(int, fecha.split('/'))
            este = datetime(hoy.year, mes, dia, tzinfo=timezone.utc)
            if este < hoy:
                este = datetime(hoy.year + 1, mes, dia, tzinfo=timezone.utc)
            lista.append(((este - hoy).days, uid, fecha))
        except Exception:
            pass
    lista.sort()
    embed = discord.Embed(title='🎂 Próximos Cumpleaños', color=discord.Color.gold())
    for dias, uid, fecha in lista[:10]:
        member = ctx.guild.get_member(int(uid))
        nombre = member.display_name if member else f'<@{uid}>'
        embed.add_field(name=f'🎉 {nombre}', value=f'**{fecha}** — en {dias} días', inline=False)
    await ctx.send(embed=embed)

@bot.command(name='dado', aliases=['dice', 'd6'])
async def dado(ctx, lados: int=6):
    if not 2 <= lados <= 100:
        return await ctx.send('❌ Entre 2 y 100.')
    resultado = random.randint(1, lados)
    embed = discord.Embed(title='🎲 Dado', color=discord.Color.blurple())
    embed.add_field(name=f'D{lados}', value=f'**{resultado}**', inline=True)
    await ctx.send(embed=embed)

@bot.command(name='moneda', aliases=['coin', 'flip'])
async def moneda(ctx):
    resultado = random.choice(['🪙 Cara', '🪙 Sello'])
    embed = discord.Embed(title='🪙 Moneda', description=f'**{resultado}**', color=discord.Color.gold())
    await ctx.send(embed=embed)

@bot.command(name='ruleta', aliases=['roulette'])
async def ruleta(ctx, *opciones):
    if len(opciones) < 2:
        return await ctx.send('❌ Al menos 2 opciones.')
    elegida = random.choice(opciones)
    embed = discord.Embed(title='🎡 Ruleta', color=discord.Color.red())
    embed.add_field(name='Opciones', value=' | '.join((f'`{o}`' for o in opciones)), inline=False)
    embed.add_field(name='🏆 Elegida', value=f'**{elegida}**', inline=False)
    await ctx.send(embed=embed)

@bot.command(name='8ball', aliases=['bola8'])
async def bola_ocho(ctx, *, pregunta: str):
    respuestas = ['✅ Sí, definitivamente.', '✅ Todo indica que sí.', '✅ Sin duda.', '🤔 No está claro.', '🤔 Concéntrate y pregunta de nuevo.', '❌ No cuentes con ello.', '❌ Mi respuesta es no.', '❌ Definitivamente no.']
    embed = discord.Embed(title='🎱 Bola Mágica', color=discord.Color.dark_purple())
    embed.add_field(name='❓ Pregunta', value=pregunta, inline=False)
    embed.add_field(name='🔮 Respuesta', value=random.choice(respuestas), inline=False)
    await ctx.send(embed=embed)

@bot.command(name='piedra', aliases=['rps'])
async def piedra_papel_tijera(ctx, eleccion: str):
    opciones = ['piedra', 'papel', 'tijera']
    eleccion = eleccion.lower()
    if eleccion not in opciones:
        return await ctx.send('❌ `piedra`, `papel` o `tijera`')
    bot_eleccion = random.choice(opciones)
    emojis = {'piedra': '🪨', 'papel': '📄', 'tijera': '✂️'}
    if eleccion == bot_eleccion:
        resultado = '🤝 Empate'
        color = discord.Color.yellow()
    elif eleccion == 'piedra' and bot_eleccion == 'tijera' or (eleccion == 'papel' and bot_eleccion == 'piedra') or (eleccion == 'tijera' and bot_eleccion == 'papel'):
        resultado = '🏆 ¡Ganaste!'
        color = discord.Color.green()
    else:
        resultado = '😈 ¡Perdiste!'
        color = discord.Color.red()
    embed = discord.Embed(title='🎮 RPS', description=resultado, color=color)
    embed.add_field(name='Tú', value=emojis[eleccion], inline=True)
    embed.add_field(name='Bot', value=emojis[bot_eleccion], inline=True)
    await ctx.send(embed=embed)

@bot.command(name='verdad_o_reto', aliases=['tor', 'verdad', 'reto'])
async def verdad_o_reto(ctx, member: discord.Member=None):
    member = member or ctx.author
    verdades = ['¿Cuál es tu mayor miedo?', '¿Qué es lo más embarazoso que te ha pasado?', '¿Tienes algún crush aquí?', '¿Cuál es tu secreto más oscuro?', '¿A quién de aquí considerarías como pareja?', '¿Cuál es tu mayor defecto?']
    retos = ["Cambia tu nick a 'Pollo Frito' por 1 hora.", 'Manda un meme al canal principal.', 'Di algo positivo sobre cada persona en el canal de voz.', 'Escribe un poema sobre el bot.', "Di 'yo amo a mi bot' 3 veces en el chat.", 'Manda una foto de tu escritorio/pantalla.']
    tipo = random.choice(['Verdad 🔮', 'Reto 💥'])
    contenido = random.choice(verdades) if 'Verdad' in tipo else random.choice(retos)
    color = discord.Color.purple() if 'Verdad' in tipo else discord.Color.orange()
    embed = discord.Embed(title=f'🎮 {tipo}', description=f'Para {member.mention}\n\n**{contenido}**', color=color)
    await ctx.send(embed=embed)

@bot.command(name='acertijo', aliases=['riddle'])
async def acertijo(ctx):
    acertijos = [('Tengo ciudades, pero no hay casas. Tengo montañas, pero no hay árboles. Tengo agua, pero no hay peces. ¿Qué soy?', 'Un mapa'), ('Cuanto más me seques, más mojado te quedas. ¿Qué soy?', 'Una toalla'), ('Tengo manos pero no puedo aplaudir. ¿Qué soy?', 'Un reloj'), ('Soy ligero como una pluma, pero ni el hombre más fuerte puede sostenerme más de unos minutos. ¿Qué soy?', 'El aliento'), ('Tengo un corazón que no late, tengo una boca que no habla. ¿Qué soy?', 'Una alcachofa'), ('Siempre delante de ti, pero no se puede ver. ¿Qué soy?', 'El futuro')]
    pregunta, respuesta = random.choice(acertijos)
    embed = discord.Embed(title='🧩 Acertijo', description=pregunta, color=discord.Color.purple())
    await ctx.send(embed=embed)

    def check(m):
        return m.channel == ctx.channel and (not m.author.bot)
    try:
        msg_r = await bot.wait_for('message', timeout=30.0, check=check)
        if respuesta.lower() in msg_r.content.lower():
            await ctx.send(f'✅ ¡{msg_r.author.mention} acertó! Era **{respuesta}** 🎉')
        else:
            await ctx.send(f'❌ Era **{respuesta}**.')
    except asyncio.TimeoutError:
        await ctx.send(f'⌛ Tiempo. Era **{respuesta}**.')
FRASES_MOTIVACION = ['El éxito no es definitivo, el fracaso no es fatal. — Churchill', 'El único modo de hacer un gran trabajo es amar lo que haces. — Jobs', 'La vida es 10% lo que te sucede y 90% cómo reaccionas. — Swindoll', 'El futuro pertenece a quienes creen en la belleza de sus sueños. — Roosevelt', 'Sé el cambio que quieres ver en el mundo. — Gandhi', 'No esperes oportunidades extraordinarias. Aprovecha las ordinarias.', 'Cree en ti mismo y todo lo demás vendrá solo.']
CHISTES = ['¿Por qué los pájaros vuelan al sur? Porque caminar es muy lejos 🐦', '¿Qué le dijo el 0 al 8? Bonito cinturón 😂', '¿Por qué el libro de matemáticas estaba triste? Porque tenía muchos problemas 📚', '¿Qué hace una abeja en el gimnasio? ¡Zum-ba! 🐝', '¿Por qué los esqueletos no pelean? No tienen agallas 💀']

@bot.command(name='frase', aliases=['motivacion', 'quote'])
async def frase_random(ctx):
    embed = discord.Embed(title='💬 Frase del día', description=f'*{random.choice(FRASES_MOTIVACION)}*', color=discord.Color.teal())
    await ctx.send(embed=embed)

@bot.command(name='chiste', aliases=['joke'])
async def chiste_random(ctx):
    embed = discord.Embed(title='😂 Chiste', description=random.choice(CHISTES), color=discord.Color.yellow())
    await ctx.send(embed=embed)

@bot.command(name='meme')
async def meme_random(ctx):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://meme-api.com/gimme') as resp:
                if resp.status == 200:
                    data = await resp.json()
                    embed = discord.Embed(title=data['title'], color=discord.Color.orange())
                    embed.set_image(url=data['url'])
                    await ctx.send(embed=embed)
                    return
    except Exception:
        pass
    await ctx.send('❌ No pude obtener un meme. Intenta más tarde.')

@bot.command(name='rng', aliases=['random', 'aleatorio'])
async def rng(ctx, minimo: int=1, maximo: int=100):
    if minimo >= maximo:
        return await ctx.send('❌ El mínimo debe ser menor que el máximo.')
    resultado = random.randint(minimo, maximo)
    embed = discord.Embed(title='🎲 Número Aleatorio', color=discord.Color.blurple())
    embed.add_field(name='Rango', value=f'`{minimo}` – `{maximo}`', inline=True)
    embed.add_field(name='Resultado', value=f'**{resultado}**', inline=True)
    await ctx.send(embed=embed)

@bot.command(name='buscar', aliases=['google', 'search'])
async def buscar(ctx, *, termino: str):
    url = f"https://www.google.com/search?q={termino.replace(' ', '+')}"
    embed = discord.Embed(title=f'🔍 Buscar: {termino}', description=f'[Haz clic para buscar en Google]({url})', color=discord.Color.blue())
    await ctx.send(embed=embed)

@bot.command(name='sorteo', aliases=['giveaway'])
@commands.check(es_staff)
async def sorteo(ctx, segundos: int, *, premio: str):
    if not 10 <= segundos <= 86400:
        return await ctx.send('❌ Entre 10s y 24h.')
    embed = discord.Embed(title='🎁 ¡SORTEO!', description=f'**Premio:** {premio}\nReacciona con 🎉\n⏰ **{segundos}s**', color=discord.Color.gold())
    msg = await ctx.send(embed=embed)
    await msg.add_reaction('🎉')
    await asyncio.sleep(segundos)
    msg = await ctx.channel.fetch_message(msg.id)
    reaction = discord.utils.get(msg.reactions, emoji='🎉')
    participantes = [u async for u in reaction.users() if not u.bot]
    if not participantes:
        embed_fin = discord.Embed(title='🎁 Sin participantes 😢', color=discord.Color.red())
    else:
        ganador = random.choice(participantes)
        embed_fin = discord.Embed(title='🎉 ¡Ganador!', description=f'**Premio:** {premio}\n🏆 {ganador.mention}', color=discord.Color.gold())
    await ctx.send(embed=embed_fin)

@bot.command(name='encuesta', aliases=['poll'])
async def encuesta(ctx, *, texto: str):
    partes = [p.strip() for p in texto.split('|')]
    if len(partes) < 2:
        return await ctx.send('❌ Formato: `!encuesta ¿Pregunta? | op1 | op2`')
    pregunta = partes[0]
    opciones = partes[1:]
    if len(opciones) > 9:
        return await ctx.send('❌ Máximo 9 opciones.')
    nums = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣']
    desc = '\n'.join((f'{nums[i]} {op}' for i, op in enumerate(opciones)))
    embed = discord.Embed(title=f'📊 {pregunta}', description=desc, color=discord.Color.blurple())
    msg = await ctx.send(embed=embed)
    for i in range(len(opciones)):
        await msg.add_reaction(nums[i])

@bot.command(name='encuesta_si_no', aliases=['yesno'])
async def encuesta_si_no(ctx, *, pregunta: str):
    embed = discord.Embed(title=f'📊 {pregunta}', color=discord.Color.blurple())
    msg = await ctx.send(embed=embed)
    await msg.add_reaction('✅')
    await msg.add_reaction('❌')
ANIME_ACCIONES = {'abrazar': {'emoji': '🤗', 'gif_tag': 'hug', 'msg': '{a} abraza a {b} 🤗', 'boton': 'Abrazar 🤗'}, 'pat': {'emoji': '👋', 'gif_tag': 'pat', 'msg': '{a} le da palmaditas a {b} 👋', 'boton': 'Palmaditas 👋'}, 'slap': {'emoji': '😤', 'gif_tag': 'slap', 'msg': '{a} cachetea a {b} 😤', 'boton': 'Devolver 😤'}, 'kiss': {'emoji': '💋', 'gif_tag': 'kiss', 'msg': '{a} besa a {b} 💋', 'boton': 'Beso 💋'}, 'cry': {'emoji': '😢', 'gif_tag': 'cry', 'msg': '{a} está llorando 😢', 'boton': 'Consolar 🫂'}, 'poke': {'emoji': '👉', 'gif_tag': 'poke', 'msg': '{a} toca a {b} 👉', 'boton': 'Devolver 👉'}, 'cuddle': {'emoji': '🥰', 'gif_tag': 'cuddle', 'msg': '{a} acurruca a {b} 🥰', 'boton': 'Acurrucarse 🥰'}, 'bite': {'emoji': '😬', 'gif_tag': 'bite', 'msg': '{a} muerde a {b} 😬', 'boton': 'Morder 😬'}, 'wave': {'emoji': '👋', 'gif_tag': 'wave', 'msg': '{a} saluda a {b} 👋', 'boton': 'Saludar 👋'}, 'dance': {'emoji': '💃', 'gif_tag': 'dance', 'msg': '{a} baila con {b} 💃', 'boton': 'Bailar 💃'}}
_contadores_anime = {}

def get_contador(uid1: int, uid2: int, accion: str) -> int:
    key = f'{min(uid1, uid2)}-{max(uid1, uid2)}-{accion}'
    _contadores_anime[key] = _contadores_anime.get(key, 0) + 1
    return _contadores_anime[key]

async def obtener_gif_anime(tag: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://nekos.best/api/v2/{tag}') as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['results'][0]['url']
    except Exception:
        pass
    return None

class AnimeView(discord.ui.View):

    def __init__(self, autor, target, accion, info):
        super().__init__(timeout=60)
        self.autor = autor
        self.target = target
        self.accion = accion
        self.info = info
        btn_r = discord.ui.Button(label=info['boton'], style=discord.ButtonStyle.primary)
        btn_x = discord.ui.Button(label='Rechazar ✖', style=discord.ButtonStyle.danger)

        async def r_cb(interaction):
            if interaction.user.id != self.target.id:
                return await interaction.response.send_message('❌ No es para ti.', ephemeral=True)
            count = get_contador(self.autor.id, self.target.id, self.accion)
            gif = await obtener_gif_anime(self.info['gif_tag'])
            msg = self.info['msg'].format(a=self.target.display_name, b=self.autor.display_name)
            embed = discord.Embed(description=msg, color=discord.Color.pink())
            if gif:
                embed.set_image(url=gif)
            await interaction.response.send_message(embed=embed)
            self.stop()

        async def x_cb(interaction):
            if interaction.user.id != self.target.id:
                return await interaction.response.send_message('❌ No es para ti.', ephemeral=True)
            await interaction.response.send_message(f'💔 **{self.target.display_name}** rechazó a **{self.autor.display_name}**.')
            self.stop()
        btn_r.callback = r_cb
        btn_x.callback = x_cb
        self.add_item(btn_r)
        self.add_item(btn_x)

def make_anime_cmd(accion, info):

    @bot.command(name=accion)
    async def _cmd(ctx, member: discord.Member=None):
        a = ctx.author.display_name
        b = member.display_name if member else 'todos'
        count = get_contador(ctx.author.id, member.id if member else 0, accion)
        msg = info['msg'].format(a=a, b=b)
        gif = await obtener_gif_anime(info['gif_tag'])
        embed = discord.Embed(description=f'**{msg}**', color=discord.Color.pink())
        if gif:
            embed.set_image(url=gif)
        if member and member != ctx.author:
            view = AnimeView(ctx.author, member, accion, info)
            await ctx.send(embed=embed, view=view)
        else:
            await ctx.send(embed=embed)
    _cmd.__name__ = accion
for _a, _i in ANIME_ACCIONES.items():
    make_anime_cmd(_a, _i)

def _build_ayuda_pages(p: str) -> list:
    secciones = [
        ('🌐', 'Generales',
         f'`{p}ping` `{p}avatar` `{p}banner` `{p}userinfo` `{p}serverinfo` `{p}stats` `{p}botinfo`\n'
         f'`{p}clima <ciudad>` `{p}tr <idioma> <texto>` `{p}calc <expr>` `{p}color <hex>`\n'
         f'`{p}buscar <texto>` `{p}rng [min] [max]` `{p}recordar <tiempo> <msg>`\n'
         f'`{p}sugerencia <txt>` `{p}reporte @user <razon>` `{p}invitar`'),
        ('🛡️', 'AntiNuke (Owner)',
         f'`{p}antinuke` — Panel completo\n`{p}an_ayuda` — Todos los comandos AntiNuke\n'
         f'AntiRaid | AntiLinks | AntiSpam | AntiBot | Verificacion'),
        ('🔒', 'Moderacion (Admin)',
         f'**Sanciones**\n`{p}ban @u [razon]` `{p}unban <user>` `{p}kick @u [razon]`\n'
         f'`{p}mute @u [minutos]` `{p}unmute @u`\n\n'
         f'**Mensajes**\n`{p}limpiar [n]` `{p}limpiar_bots` `{p}limpiar_usuario @u`\n\n'
         f'**Nicks**\n`{p}nick @u <nuevo>` `{p}massnick <nick>`'),
        ('⚠️', 'Warns (Staff)',
         f'`{p}warn @u <razon>` `{p}warns [@u]` `{p}clearwarns @u` `{p}delwarn @u <n>`'),
        ('💬', 'Canales (Admin)',
         f'`{p}lock` `{p}unlock` `{p}lockall` `{p}unlockall`\n'
         f'`{p}hide` `{p}show` `{p}slowmode [s]` `{p}topic <txt>`\n'
         f'`{p}cc <nombre>` `{p}ec` `{p}rc <nombre>` `{p}clone` `{p}nsfw`'),
        ('🎭', 'Roles (Admin)',
         f'`{p}dr @u <rol>` — Dar rol  |  `{p}qr @u <rol>` — Quitar rol\n'
         f'`{p}cr #color <nombre>` `{p}er <nombre>` `{p}lroles`\n'
         f'`{p}ru [@u]` `{p}ann [#c] <msg>` `{p}emb [#c] "titulo" <msg>`\n'
         f'`{p}v @u` — Dar acceso'),
        ('🎰', 'Juegos',
         f'`{p}trivia` `{p}adivina [max]` `{p}acertijo`\n'
         f'`{p}tor [@u]` — Verdad o Reto\n'
         f'`{p}dado [lados]` `{p}dp [n] [lados]` `{p}moneda`\n'
         f'`{p}ruleta op1 op2...` `{p}8ball <preg>` `{p}piedra`'),
        ('🎁', 'Sorteos y Encuestas (Staff)',
         f'`{p}sorteo <seg> <premio>`\n`{p}encuesta <preg> | op1 | op2`\n`{p}encuesta_si_no <preg>`'),
        ('🤝', 'Roleplay',
         f'`{p}casar @u` `{p}aceptar` `{p}rechazar` `{p}divorcio` `{p}pareja`\n'
         f'`{p}adoptar @u` `{p}familia`'),
        ('🔮', 'Fun',
         f'`{p}horoscopo <signo>` `{p}personalidad` `{p}compatibilidad @u`\n'
         f'`{p}fp [personaje]` `{p}pl` — Frases anime\n`{p}frase` `{p}chiste` `{p}meme`'),
        ('🐱', 'Anime',
         f'`{p}abrazar` `{p}pat` `{p}slap` `{p}kiss` `{p}poke`\n'
         f'`{p}cuddle` `{p}bite` `{p}wave` `{p}dance` `{p}cry`'),
        ('🎂', 'Cumpleanos y Recordatorios',
         f'`{p}cumple [DD/MM]` `{p}cumple_ver [@u]` `{p}cumples_lista`\n'
         f'`{p}recordar <10m/2h/30s> <msg>`'),
        ('⚙️', 'Config',
         f'`{p}setprefix <nuevo>` — Cambiar prefijo'),
    ]
    total = len(secciones)
    pages = []
    for i, (emoji, titulo, valor) in enumerate(secciones):
        embed = discord.Embed(title=f'{emoji} {titulo}', description=valor, color=discord.Color.blurple())
        embed.set_footer(text=f'Pagina {i+1}/{total} \u2022 Usa los botones para navegar')
        pages.append(embed)
    return pages

_AYUDA_GRUPOS = {
    'principales': [
        ('🌐', 'Generales',    0),
        ('🛡️', 'AntiNuke',    1),
        ('🔒', 'Moderacion',  2),
        ('⚠️', 'Warns',       3),
        ('💬', 'Canales',     4),
        ('🎭', 'Roles',       5),
    ],
    'extra': [
        ('🎰', 'Juegos',      6),
        ('🎁', 'Sorteos',     7),
        ('🤝', 'Roleplay',    8),
        ('🔮', 'Fun',         9),
        ('🐱', 'Anime',      10),
        ('🎂', 'Cumpleanos', 11),
        ('⚙️', 'Config',     12),
    ],
}

class AyudaView(discord.ui.View):
    def __init__(self, pages: list, author_id: int):
        super().__init__(timeout=120)
        self.pages     = pages
        self.author_id = author_id
        self.current   = 0
        self._add_selects()

    def _add_selects(self):
        for grupo, items in _AYUDA_GRUPOS.items():
            placeholder = '📋 Comandos Principales' if grupo == 'principales' else '🎮 Comandos Extra'
            opts = [
                discord.SelectOption(label=nombre, value=str(idx), emoji=emoji)
                for emoji, nombre, idx in items
            ]
            sel = discord.ui.Select(
                placeholder=placeholder,
                options=opts,
                custom_id=f'ayuda_sel_{grupo}',
                row=1 if grupo == 'principales' else 2,
            )
            sel.callback = self._select_callback
            self.add_item(sel)

    async def _select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message('❌ Este menu no es tuyo.', ephemeral=True)
        self.current = int(interaction.data['values'][0])
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message('❌ Este menu no es tuyo.', ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji='⏮️', style=discord.ButtonStyle.secondary, row=0)
    async def btn_first(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.current = 0
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(emoji='◀️', style=discord.ButtonStyle.primary, row=0)
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.current = (self.current - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(emoji='🗑️', style=discord.ButtonStyle.danger, row=0)
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        await interaction.message.delete()

    @discord.ui.button(emoji='▶️', style=discord.ButtonStyle.primary, row=0)
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.current = (self.current + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(emoji='⏭️', style=discord.ButtonStyle.secondary, row=0)
    async def btn_last(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction): return
        self.current = len(self.pages) - 1
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

@bot.command(name='ayuda', aliases=['help', 'h', 'comandos'])
async def ayuda(ctx):
    pages = _build_ayuda_pages(PREFIX)
    view  = AyudaView(pages, ctx.author.id)
    await ctx.send(embed=pages[0], view=view)

# ══════════════════════════════════════════════════════════════════
# SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════

# ── Helpers internos ──────────────────────────────────────────────
def _is_admin(i: discord.Interaction) -> bool:
    return i.user.guild_permissions.administrator

def _is_staff(i: discord.Interaction) -> bool:
    return (i.user.guild_permissions.administrator
            or i.user.guild_permissions.manage_roles
            or any(r.name in ROLES_STAFF_CFG for r in i.user.roles))

async def _no_perm(i: discord.Interaction):
    await i.response.send_message('🔒 No tienes permisos para ese comando.', ephemeral=True)

# ── GENERALES ─────────────────────────────────────────────────────
@bot.tree.command(name='ping', description='Muestra la latencia del bot')
async def slash_ping(i: discord.Interaction):
    lat = round(bot.latency * 1000)
    color = discord.Color.green() if lat < 100 else discord.Color.yellow() if lat < 200 else discord.Color.red()
    await i.response.send_message(embed=discord.Embed(title='🏓 Pong!', description=f'**{lat}ms**', color=color))

@bot.tree.command(name='say', description='Envía un mensaje como el bot')
@app_commands.describe(mensaje='Mensaje a enviar')
async def slash_say(i: discord.Interaction, mensaje: str):
    if not i.user.guild_permissions.administrator:
        return await _no_perm(i)
    await i.response.send_message(mensaje, allowed_mentions=discord.AllowedMentions.all())

@bot.tree.command(name='avatar', description='Muestra el avatar de un usuario')
@app_commands.describe(usuario='Usuario objetivo (opcional)')
async def slash_avatar(i: discord.Interaction, usuario: discord.Member = None):
    m = usuario or i.user
    embed = discord.Embed(title=f'🖼️ {m.display_name}', color=m.color)
    embed.set_image(url=m.display_avatar.url)
    embed.add_field(name='🔗 Link', value=f'[Descargar]({m.display_avatar.url})', inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='banner', description='Muestra el banner de un usuario')
@app_commands.describe(usuario='Usuario objetivo (opcional)')
async def slash_banner(i: discord.Interaction, usuario: discord.Member = None):
    m = usuario or i.user
    user = await bot.fetch_user(m.id)
    if not user.banner:
        return await i.response.send_message(f'❌ {m.display_name} no tiene banner.', ephemeral=True)
    embed = discord.Embed(title=f'🖼️ Banner de {m.display_name}', color=m.color)
    embed.set_image(url=user.banner.url)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='userinfo', description='Información de un usuario')
@app_commands.describe(usuario='Usuario objetivo (opcional)')
async def slash_userinfo(i: discord.Interaction, usuario: discord.Member = None):
    m = usuario or i.user
    roles = [r.mention for r in m.roles if r != i.guild.default_role]
    embed = discord.Embed(title=f'👤 {m}', color=m.color)
    embed.set_thumbnail(url=m.display_avatar.url)
    embed.add_field(name='🆔 ID', value=m.id, inline=True)
    embed.add_field(name='📅 Cuenta', value=m.created_at.strftime('%d/%m/%Y'), inline=True)
    embed.add_field(name='📥 Se unió', value=m.joined_at.strftime('%d/%m/%Y'), inline=True)
    embed.add_field(name='🎨 Color', value=str(m.color), inline=True)
    embed.add_field(name='🤖 Bot', value='Sí' if m.bot else 'No', inline=True)
    embed.add_field(name='🏆 Roles', value=' '.join(roles) if roles else 'Sin roles', inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='serverinfo', description='Información del servidor')
async def slash_serverinfo(i: discord.Interaction):
    g = i.guild
    embed = discord.Embed(title=f'🏠 {g.name}', color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name='🆔 ID', value=g.id, inline=True)
    embed.add_field(name='👑 Dueño', value=g.owner.mention, inline=True)
    embed.add_field(name='👥 Miembros', value=g.member_count, inline=True)
    embed.add_field(name='💬 Canales', value=len(g.channels), inline=True)
    embed.add_field(name='🎭 Roles', value=len(g.roles), inline=True)
    embed.add_field(name='📅 Creado', value=g.created_at.strftime('%d/%m/%Y'), inline=True)
    embed.add_field(name='📢 Verificación', value=str(g.verification_level), inline=True)
    embed.add_field(name='💎 Boosts', value=g.premium_subscription_count, inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='stats', description='Estadísticas del servidor')
async def slash_stats(i: discord.Interaction):
    g = i.guild
    total = g.member_count
    bots = sum(1 for m in g.members if m.bot)
    humanos = total - bots
    en_linea = sum(1 for m in g.members if m.status != discord.Status.offline and not m.bot)
    embed = discord.Embed(title=f'📊 {g.name}', color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name='👥 Total', value=total, inline=True)
    embed.add_field(name='🧑 Humanos', value=humanos, inline=True)
    embed.add_field(name='🤖 Bots', value=bots, inline=True)
    embed.add_field(name='🟢 En línea', value=en_linea, inline=True)
    embed.add_field(name='💬 Canales', value=len(g.text_channels), inline=True)
    embed.add_field(name='🔊 Voz', value=len(g.voice_channels), inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='botinfo', description='Información del bot')
async def slash_botinfo(i: discord.Interaction):
    import platform
    embed = discord.Embed(title='🤖 Info del Bot', color=discord.Color.blurple())
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name='🏷️ Nombre', value=str(bot.user), inline=True)
    embed.add_field(name='🆔 ID', value=bot.user.id, inline=True)
    embed.add_field(name='🖥️ Python', value=platform.python_version(), inline=True)
    embed.add_field(name='📚 discord.py', value=discord.__version__, inline=True)
    embed.add_field(name='🏠 Servidores', value=len(bot.guilds), inline=True)
    embed.add_field(name='👥 Usuarios', value=len(bot.users), inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='invitar', description='Link de invitación del bot')
async def slash_invitar(i: discord.Interaction):
    url = f'https://discord.com/api/oauth2/authorize?client_id={bot.user.id}&permissions=8&scope=bot'
    embed = discord.Embed(title='🔗 Invitar', description=f'[Clic aquí]({url})', color=discord.Color.blurple())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='clima', description='Muestra el clima de una ciudad')
@app_commands.describe(ciudad='Nombre de la ciudad')
async def slash_clima(i: discord.Interaction, ciudad: str):
    await i.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://wttr.in/{ciudad.replace(' ', '+')}?format=j1") as resp:
                if resp.status != 200:
                    return await i.followup.send('❌ Ciudad no encontrada.')
                data = await resp.json()
                actual = data['current_condition'][0]
                embed = discord.Embed(title=f'🌤️ {ciudad.title()}', color=discord.Color.blue())
                embed.add_field(name='🌡️ Temp', value=f"{actual['temp_C']}°C", inline=True)
                embed.add_field(name='🤔 Sensación', value=f"{actual['FeelsLikeC']}°C", inline=True)
                embed.add_field(name='💧 Humedad', value=f"{actual['humidity']}%", inline=True)
                embed.add_field(name='💨 Viento', value=f"{actual['windspeedKmph']} km/h", inline=True)
                embed.add_field(name='☁️ Estado', value=actual['weatherDesc'][0]['value'], inline=True)
                await i.followup.send(embed=embed)
    except Exception:
        await i.followup.send('❌ No pude obtener el clima.')

@bot.tree.command(name='traducir', description='Traduce un texto a otro idioma')
@app_commands.describe(idioma='Código de idioma (ej: en, fr, pt)', texto='Texto a traducir')
async def slash_traducir(i: discord.Interaction, idioma: str, texto: str):
    await i.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://api.mymemory.translated.net/get?q={texto}&langpair=es|{idioma}') as resp:
                data = await resp.json()
                trad = data['responseData']['translatedText']
                embed = discord.Embed(title='🌍 Traducción', color=discord.Color.teal())
                embed.add_field(name='📝 Original', value=texto, inline=False)
                embed.add_field(name='✅ Traducido', value=trad, inline=False)
                embed.add_field(name='🌐 Idioma', value=idioma, inline=True)
                await i.followup.send(embed=embed)
    except Exception:
        await i.followup.send('❌ No pude traducir.')

@bot.tree.command(name='calcular', description='Calcula una expresión matemática')
@app_commands.describe(expresion='Expresión (ej: 5+3*2)')
async def slash_calcular(i: discord.Interaction, expresion: str):
    try:
        if not all(c in '0123456789+-*/.() ' for c in expresion):
            return await i.response.send_message('❌ Solo `+ - * / ( )`.', ephemeral=True)
        resultado = eval(expresion)
        embed = discord.Embed(title='🧮 Calculadora', color=discord.Color.green())
        embed.add_field(name='📝', value=f'`{expresion}`', inline=False)
        embed.add_field(name='✅', value=f'**{resultado}**', inline=False)
        await i.response.send_message(embed=embed)
    except ZeroDivisionError:
        await i.response.send_message('❌ División entre cero.', ephemeral=True)
    except Exception:
        await i.response.send_message('❌ Expresión inválida.', ephemeral=True)

@bot.tree.command(name='color', description='Muestra información de un color HEX')
@app_commands.describe(hex_color='Color en formato hex (ej: FF0000)')
async def slash_color(i: discord.Interaction, hex_color: str):
    hex_color = hex_color.strip('#')
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except Exception:
        return await i.response.send_message('❌ Usa un hex válido (ej: FF0000)', ephemeral=True)
    embed = discord.Embed(title=f'🎨 #{hex_color.upper()}', color=int(hex_color, 16))
    embed.add_field(name='R', value=r, inline=True)
    embed.add_field(name='G', value=g, inline=True)
    embed.add_field(name='B', value=b, inline=True)
    embed.set_thumbnail(url=f'https://singlecolorimage.com/get/{hex_color}/100x100')
    await i.response.send_message(embed=embed)

@bot.tree.command(name='sugerencia', description='Envía una sugerencia')
@app_commands.describe(texto='Tu sugerencia', canal='Canal destino (opcional)')
async def slash_sugerencia(i: discord.Interaction, texto: str, canal: discord.TextChannel = None):
    canal = canal or i.channel
    embed = discord.Embed(title='💡 Sugerencia', description=texto, color=discord.Color.yellow(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=i.user.display_name, icon_url=i.user.display_avatar.url)
    await canal.send(embed=embed)
    await i.response.send_message('✅ Sugerencia enviada.', ephemeral=True)

@bot.tree.command(name='reporte', description='Reporta a un usuario')
@app_commands.describe(usuario='Usuario a reportar', razon='Razón del reporte')
async def slash_reporte(i: discord.Interaction, usuario: discord.Member, razon: str):
    embed = discord.Embed(title='🚨 Reporte', color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name='👤 Reportado', value=usuario.mention, inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='📢 Por', value=i.user.mention, inline=True)
    await i.channel.send(embed=embed)
    await i.response.send_message('✅ Reporte enviado.', ephemeral=True)

@bot.tree.command(name='rng', description='Número aleatorio en un rango')
@app_commands.describe(minimo='Valor mínimo', maximo='Valor máximo')
async def slash_rng(i: discord.Interaction, minimo: int = 1, maximo: int = 100):
    if minimo >= maximo:
        return await i.response.send_message('❌ El mínimo debe ser menor que el máximo.', ephemeral=True)
    resultado = random.randint(minimo, maximo)
    embed = discord.Embed(title='🎲 Número Aleatorio', color=discord.Color.blurple())
    embed.add_field(name='Rango', value=f'`{minimo}` – `{maximo}`', inline=True)
    embed.add_field(name='Resultado', value=f'**{resultado}**', inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='buscar', description='Busca en Google')
@app_commands.describe(termino='Término de búsqueda')
async def slash_buscar(i: discord.Interaction, termino: str):
    url = f"https://www.google.com/search?q={termino.replace(' ', '+')}"
    embed = discord.Embed(title=f'🔍 {termino}', description=f'[Buscar en Google]({url})', color=discord.Color.blue())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='ayuda', description='Muestra todos los comandos del bot')
async def slash_ayuda(i: discord.Interaction):
    pages = _build_ayuda_pages(PREFIX)
    view  = AyudaView(pages, i.user.id)
    await i.response.send_message(embed=pages[0], view=view)

# ── MODERACIÓN ────────────────────────────────────────────────────
@bot.tree.command(name='ban', description='Banea a un usuario')
@app_commands.describe(usuario='Usuario a banear', razon='Razón del ban')
async def slash_ban(i: discord.Interaction, usuario: discord.Member, razon: str = 'Sin razón'):
    if not _is_admin(i):
        return await _no_perm(i)
    if usuario == i.user:
        return await i.response.send_message('❌ No puedes banearte.', ephemeral=True)
    if usuario.guild_permissions.administrator:
        return await i.response.send_message('❌ No puedes banear a un admin.', ephemeral=True)
    try:
        await i.guild.ban(usuario, reason=f'[{i.user}] {razon}', delete_message_days=0)
    except discord.Forbidden:
        return await i.response.send_message('❌ Sin permisos.', ephemeral=True)
    embed = discord.Embed(title='🔨 Baneado', color=discord.Color.red())
    embed.add_field(name='👤 Usuario', value=f'{usuario} (`{usuario.id}`)', inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='👮 Por', value=i.user.mention, inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='unban', description='Desbanea a un usuario por ID o nombre')
@app_commands.describe(usuario='ID o nombre#tag del usuario baneado')
async def slash_unban(i: discord.Interaction, usuario: str):
    if not _is_admin(i):
        return await _no_perm(i)
    bans = [entry async for entry in i.guild.bans()]
    objetivo = None
    for entry in bans:
        if str(entry.user.id) == usuario or str(entry.user) == usuario:
            objetivo = entry.user
            break
    if not objetivo:
        return await i.response.send_message(f'❌ No encontré `{usuario}` en los bans.', ephemeral=True)
    await i.guild.unban(objetivo, reason=f'Desbaneado por {i.user}')
    embed = discord.Embed(title='✅ Desbaneado', color=discord.Color.green())
    embed.add_field(name='👤 Usuario', value=f'{objetivo} (`{objetivo.id}`)', inline=True)
    embed.add_field(name='👮 Por', value=i.user.mention, inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='kick', description='Expulsa a un usuario')
@app_commands.describe(usuario='Usuario a expulsar', razon='Razón')
async def slash_kick(i: discord.Interaction, usuario: discord.Member, razon: str = 'Sin razón'):
    if not _is_admin(i):
        return await _no_perm(i)
    if usuario == i.user:
        return await i.response.send_message('❌ No puedes kickearte.', ephemeral=True)
    try:
        await i.guild.kick(usuario, reason=f'[{i.user}] {razon}')
    except discord.Forbidden:
        return await i.response.send_message('❌ Sin permisos.', ephemeral=True)
    embed = discord.Embed(title='👢 Expulsado', color=discord.Color.orange())
    embed.add_field(name='👤 Usuario', value=str(usuario), inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='👮 Por', value=i.user.mention, inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='mute', description='Silencia a un usuario')
@app_commands.describe(usuario='Usuario a mutear', minutos='Duración en minutos', razon='Razón')
async def slash_mute(i: discord.Interaction, usuario: discord.Member, minutos: int = 10, razon: str = 'Sin razón'):
    if not _is_admin(i):
        return await _no_perm(i)
    if not 1 <= minutos <= 40320:
        return await i.response.send_message('❌ Entre 1 y 40320 minutos.', ephemeral=True)
    import datetime as dt
    try:
        until = discord.utils.utcnow() + dt.timedelta(minutes=minutos)
        await usuario.timeout(until, reason=f'[{i.user}] {razon}')
    except discord.Forbidden:
        return await i.response.send_message('❌ Sin permisos.', ephemeral=True)
    embed = discord.Embed(title='🔇 Muteado', color=discord.Color.dark_grey())
    embed.add_field(name='👤 Usuario', value=usuario.mention, inline=True)
    embed.add_field(name='⏰ Duración', value=f'{minutos} min', inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='👮 Por', value=i.user.mention, inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='unmute', description='Desmutea a un usuario')
@app_commands.describe(usuario='Usuario a desmutear')
async def slash_unmute(i: discord.Interaction, usuario: discord.Member):
    if not _is_admin(i):
        return await _no_perm(i)
    try:
        await usuario.timeout(None)
    except discord.Forbidden:
        return await i.response.send_message('❌ Sin permisos.', ephemeral=True)
    await i.response.send_message(f'✅ {usuario.mention} **desmuteado**.')

@bot.tree.command(name='limpiar', description='Borra mensajes del canal')
@app_commands.describe(cantidad='Cantidad de mensajes (1-100)')
async def slash_limpiar(i: discord.Interaction, cantidad: int = 10):
    if not _is_admin(i):
        return await _no_perm(i)
    if not 1 <= cantidad <= 100:
        return await i.response.send_message('❌ Entre 1 y 100.', ephemeral=True)
    await i.response.defer(ephemeral=True)
    borrados = await i.channel.purge(limit=cantidad)
    await i.followup.send(f'🗑️ **{len(borrados)}** mensajes borrados.', ephemeral=True)

@bot.tree.command(name='limpiar_bots', description='Borra mensajes de bots del canal')
@app_commands.describe(cantidad='Mensajes a revisar (máx 50)')
async def slash_limpiar_bots(i: discord.Interaction, cantidad: int = 50):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.response.defer(ephemeral=True)
    borrados = await i.channel.purge(limit=cantidad, check=lambda m: m.author.bot)
    await i.followup.send(f'🤖 **{len(borrados)}** mensajes de bots borrados.', ephemeral=True)

@bot.tree.command(name='limpiar_usuario', description='Borra mensajes de un usuario')
@app_commands.describe(usuario='Usuario objetivo', cantidad='Mensajes a revisar (máx 50)')
async def slash_limpiar_usuario(i: discord.Interaction, usuario: discord.Member, cantidad: int = 50):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.response.defer(ephemeral=True)
    borrados = await i.channel.purge(limit=cantidad, check=lambda m: m.author == usuario)
    await i.followup.send(f'🗑️ **{len(borrados)}** mensajes de {usuario.mention} borrados.', ephemeral=True)

@bot.tree.command(name='nick', description='Cambia el nick de un usuario')
@app_commands.describe(usuario='Usuario objetivo', nuevo_nick='Nuevo apodo (vacío para resetear)')
async def slash_nick(i: discord.Interaction, usuario: discord.Member, nuevo_nick: str = None):
    if not _is_admin(i):
        return await _no_perm(i)
    try:
        viejo = usuario.display_name
        await usuario.edit(nick=nuevo_nick)
        if nuevo_nick:
            await i.response.send_message(f'✅ Nick de {usuario.mention}: **{viejo}** → **{nuevo_nick}**')
        else:
            await i.response.send_message(f'✅ Nick de {usuario.mention} restablecido.')
    except discord.Forbidden:
        await i.response.send_message('❌ Sin permisos para cambiar ese nick.', ephemeral=True)

@bot.tree.command(name='massnick', description='Cambia el nick de todos los miembros')
@app_commands.describe(nuevo_nick='Nuevo nick para todos')
async def slash_massnick(i: discord.Interaction, nuevo_nick: str):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.response.defer()
    count = 0
    for m in i.guild.members:
        if not m.bot:
            try:
                await m.edit(nick=nuevo_nick)
                count += 1
            except Exception:
                pass
    await i.followup.send(f'✅ Nick cambiado a **{nuevo_nick}** en **{count}** miembros.')

# ── WARNS ─────────────────────────────────────────────────────────
@bot.tree.command(name='warn', description='Advierte a un usuario')
@app_commands.describe(usuario='Usuario a advertir', razon='Razón')
async def slash_warn(i: discord.Interaction, usuario: discord.Member, razon: str = 'Sin razón'):
    if not _is_staff(i):
        return await _no_perm(i)
    if usuario.guild_permissions.administrator:
        return await i.response.send_message('❌ No puedes advertir a un administrador.', ephemeral=True)
    data = cargar_warns()
    uid = str(usuario.id)
    if uid not in data:
        data[uid] = []
    data[uid].append({'razon': razon, 'por': str(i.user.id), 'fecha': datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')})
    guardar_warns(data)
    total = len(data[uid])
    embed = discord.Embed(title='⚠️ Advertencia', color=discord.Color.orange())
    embed.add_field(name='👤 Usuario', value=usuario.mention, inline=True)
    embed.add_field(name='📋 Razón', value=razon, inline=True)
    embed.add_field(name='📊 Total', value=f'{total} warn(s)', inline=True)
    embed.add_field(name='👮 Por', value=i.user.mention, inline=True)
    await i.response.send_message(embed=embed)
    if total >= 5:
        await i.guild.ban(usuario, reason='[AutoWarn] 5 advertencias')
        await i.channel.send(f'🔨 {usuario.mention} fue baneado automáticamente por 5 warns.')
    elif total >= 3:
        import datetime as dt
        until = discord.utils.utcnow() + dt.timedelta(hours=1)
        try:
            await usuario.timeout(until, reason='[AutoWarn] 3 advertencias')
            await i.channel.send(f'🔇 {usuario.mention} muteado 1h por 3 warns.')
        except Exception:
            pass

@bot.tree.command(name='warns', description='Ver las advertencias de un usuario')
@app_commands.describe(usuario='Usuario objetivo (opcional)')
async def slash_warns(i: discord.Interaction, usuario: discord.Member = None):
    if not _is_staff(i):
        return await _no_perm(i)
    m = usuario or i.user
    data = cargar_warns()
    lista = data.get(str(m.id), [])
    embed = discord.Embed(title=f'⚠️ Warns de {m.display_name}', color=discord.Color.orange())
    embed.set_thumbnail(url=m.display_avatar.url)
    if not lista:
        embed.description = '✅ Sin advertencias.'
    else:
        for idx, w in enumerate(lista, 1):
            embed.add_field(name=f"#{idx} — {w['fecha']}", value=f"**Razón:** {w['razon']}\n**Por:** <@{w['por']}>", inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='clearwarns', description='Borra todos los warns de un usuario')
@app_commands.describe(usuario='Usuario objetivo')
async def slash_clearwarns(i: discord.Interaction, usuario: discord.Member):
    if not _is_admin(i):
        return await _no_perm(i)
    data = cargar_warns()
    data.pop(str(usuario.id), None)
    guardar_warns(data)
    await i.response.send_message(f'✅ Warns de {usuario.mention} borrados.')

@bot.tree.command(name='delwarn', description='Borra un warn específico de un usuario')
@app_commands.describe(usuario='Usuario objetivo', numero='Número del warn a borrar')
async def slash_delwarn(i: discord.Interaction, usuario: discord.Member, numero: int):
    if not _is_admin(i):
        return await _no_perm(i)
    data = cargar_warns()
    uid = str(usuario.id)
    lista = data.get(uid, [])
    if numero < 1 or numero > len(lista):
        return await i.response.send_message(f'❌ Número inválido. Tiene {len(lista)} warn(s).', ephemeral=True)
    borrado = lista.pop(numero - 1)
    data[uid] = lista
    guardar_warns(data)
    await i.response.send_message(f"✅ Warn #{numero} de {usuario.mention} borrado. (`{borrado['razon']}`)")

# ── CANALES ───────────────────────────────────────────────────────
@bot.tree.command(name='lock', description='Bloquea el canal actual')
async def slash_lock(i: discord.Interaction):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.channel.set_permissions(i.guild.default_role, send_messages=False)
    await i.response.send_message(f'🔒 {i.channel.mention} bloqueado.')

@bot.tree.command(name='unlock', description='Desbloquea el canal actual')
async def slash_unlock(i: discord.Interaction):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.channel.set_permissions(i.guild.default_role, send_messages=True)
    await i.response.send_message(f'🔓 {i.channel.mention} desbloqueado.')

@bot.tree.command(name='slowmode', description='Activa el modo lento en el canal')
@app_commands.describe(segundos='Segundos (0 para desactivar)')
async def slash_slowmode(i: discord.Interaction, segundos: int = 0):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.channel.edit(slowmode_delay=segundos)
    if segundos == 0:
        await i.response.send_message('✅ Modo lento desactivado.')
    else:
        await i.response.send_message(f'⏱️ Modo lento: **{segundos}s**')

@bot.tree.command(name='hide', description='Oculta el canal actual')
async def slash_hide(i: discord.Interaction):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.channel.set_permissions(i.guild.default_role, view_channel=False)
    await i.response.send_message('🙈 Canal ocultado.', ephemeral=True)

@bot.tree.command(name='show', description='Muestra el canal actual')
async def slash_show(i: discord.Interaction):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.channel.set_permissions(i.guild.default_role, view_channel=True)
    await i.response.send_message(f'👁️ {i.channel.mention} ahora es visible.')

@bot.tree.command(name='topic', description='Cambia el tema del canal')
@app_commands.describe(texto='Nuevo tema')
async def slash_topic(i: discord.Interaction, texto: str):
    if not _is_admin(i):
        return await _no_perm(i)
    await i.channel.edit(topic=texto)
    await i.response.send_message(f'✅ Tema actualizado.')

@bot.tree.command(name='rename_canal', description='Renombra el canal actual')
@app_commands.describe(nombre='Nuevo nombre')
async def slash_rename_canal(i: discord.Interaction, nombre: str):
    if not _is_admin(i):
        return await _no_perm(i)
    viejo = i.channel.name
    await i.channel.edit(name=nombre)
    await i.response.send_message(f'✅ Canal renombrado: **{viejo}** → **{nombre}**')

@bot.tree.command(name='crear_canal', description='Crea un nuevo canal de texto')
@app_commands.describe(nombre='Nombre del canal')
async def slash_crear_canal(i: discord.Interaction, nombre: str):
    if not _is_admin(i):
        return await _no_perm(i)
    canal = await i.guild.create_text_channel(nombre)
    await i.response.send_message(f'✅ Canal {canal.mention} creado.')

@bot.tree.command(name='eliminar_canal', description='Elimina el canal actual')
async def slash_eliminar_canal(i: discord.Interaction):
    if not _is_admin(i):
        return await _no_perm(i)
    nombre = i.channel.name
    await i.response.send_message(f'🗑️ Canal **#{nombre}** eliminado.')
    await i.channel.delete()

@bot.tree.command(name='clonar_canal', description='Clona el canal actual')
async def slash_clonar_canal(i: discord.Interaction):
    if not _is_admin(i):
        return await _no_perm(i)
    nuevo = await i.channel.clone()
    await i.response.send_message(f'✅ Canal clonado: {nuevo.mention}')

@bot.tree.command(name='nsfw', description='Activa/desactiva NSFW en el canal')
async def slash_nsfw(i: discord.Interaction):
    if not _is_admin(i):
        return await _no_perm(i)
    estado = not i.channel.is_nsfw()
    await i.channel.edit(nsfw=estado)
    await i.response.send_message(f'{"🔞 NSFW activado" if estado else "✅ NSFW desactivado"} en {i.channel.mention}.')

# ── ROLES ─────────────────────────────────────────────────────────
@bot.tree.command(name='dar_rol', description='Da un rol a un usuario (busca por nombre)')
@app_commands.describe(usuario='Usuario objetivo', rol='Nombre del rol')
async def slash_dar_rol(i: discord.Interaction, usuario: discord.Member, rol: str):
    if not _is_admin(i):
        return await _no_perm(i)
    coincidencias = [r for r in i.guild.roles if rol.lower() in r.name.lower() and r != i.guild.default_role]
    if not coincidencias:
        return await i.response.send_message(f'❌ No encontré rol con `{rol}`.', ephemeral=True)
    r = coincidencias[0]
    await usuario.add_roles(r)
    await i.response.send_message(f'✅ Rol **{r.name}** dado a {usuario.mention}.')

@bot.tree.command(name='quitar_rol', description='Quita un rol a un usuario')
@app_commands.describe(usuario='Usuario objetivo', rol='Nombre del rol')
async def slash_quitar_rol(i: discord.Interaction, usuario: discord.Member, rol: str):
    if not _is_admin(i):
        return await _no_perm(i)
    coincidencias = [r for r in usuario.roles if rol.lower() in r.name.lower()]
    if not coincidencias:
        return await i.response.send_message(f'❌ {usuario.mention} no tiene ese rol.', ephemeral=True)
    r = coincidencias[0]
    await usuario.remove_roles(r)
    await i.response.send_message(f'✅ Rol **{r.name}** quitado a {usuario.mention}.')

@bot.tree.command(name='crear_rol', description='Crea un nuevo rol')
@app_commands.describe(nombre='Nombre del rol', color='Color hex (ej: FF0000)')
async def slash_crear_rol(i: discord.Interaction, nombre: str, color: str = '000000'):
    if not _is_admin(i):
        return await _no_perm(i)
    try:
        c = discord.Color(int(color.strip('#'), 16))
    except Exception:
        c = discord.Color.default()
    r = await i.guild.create_role(name=nombre, color=c)
    await i.response.send_message(f'✅ Rol {r.mention} creado.')

@bot.tree.command(name='eliminar_rol', description='Elimina un rol del servidor')
@app_commands.describe(rol='Nombre del rol a eliminar')
async def slash_eliminar_rol(i: discord.Interaction, rol: str):
    if not _is_admin(i):
        return await _no_perm(i)
    coincidencias = [r for r in i.guild.roles if rol.lower() in r.name.lower() and r != i.guild.default_role]
    if not coincidencias:
        return await i.response.send_message(f'❌ No encontré rol con `{rol}`.', ephemeral=True)
    r = coincidencias[0]
    await r.delete()
    await i.response.send_message(f'🗑️ Rol **{r.name}** eliminado.')

@bot.tree.command(name='roles_usuario', description='Lista los roles de un usuario')
@app_commands.describe(usuario='Usuario objetivo (opcional)')
async def slash_roles_usuario(i: discord.Interaction, usuario: discord.Member = None):
    m = usuario or i.user
    roles = [r.mention for r in m.roles if r != i.guild.default_role]
    embed = discord.Embed(title=f'🎭 Roles de {m.display_name}', color=m.color)
    embed.description = ' '.join(roles) if roles else 'Sin roles'
    await i.response.send_message(embed=embed)

@bot.tree.command(name='listar_roles', description='Lista todos los roles del servidor')
async def slash_listar_roles(i: discord.Interaction):
    roles = [r.mention for r in i.guild.roles if r != i.guild.default_role]
    paginas = [roles[x:x+20] for x in range(0, len(roles), 20)]
    embed = discord.Embed(title=f'🎭 Roles de {i.guild.name} ({len(roles)})', color=discord.Color.blurple())
    embed.description = ' '.join(paginas[0]) if paginas else 'Sin roles'
    await i.response.send_message(embed=embed)

@bot.tree.command(name='anuncio', description='Envía un anuncio a un canal')
@app_commands.describe(mensaje='Mensaje del anuncio', canal='Canal destino (opcional)')
async def slash_anuncio(i: discord.Interaction, mensaje: str, canal: discord.TextChannel = None):
    if not _is_admin(i):
        return await _no_perm(i)
    destino = canal or i.channel
    embed = discord.Embed(description=mensaje, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=i.guild.name, icon_url=i.guild.icon.url if i.guild.icon else None)
    await destino.send(embed=embed)
    await i.response.send_message('✅ Anuncio enviado.', ephemeral=True)

# ── JUEGOS ────────────────────────────────────────────────────────
@bot.tree.command(name='dado', description='Lanza un dado')
@app_commands.describe(lados='Número de lados (2-100)')
async def slash_dado(i: discord.Interaction, lados: int = 6):
    if not 2 <= lados <= 100:
        return await i.response.send_message('❌ Entre 2 y 100.', ephemeral=True)
    resultado = random.randint(1, lados)
    embed = discord.Embed(title='🎲 Dado', color=discord.Color.blurple())
    embed.add_field(name=f'D{lados}', value=f'**{resultado}**', inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='dado_personalizado', description='Lanza varios dados')
@app_commands.describe(cantidad='Cantidad de dados', lados='Lados por dado')
async def slash_dado_personalizado(i: discord.Interaction, cantidad: int = 2, lados: int = 6):
    if not 1 <= cantidad <= 20 or not 2 <= lados <= 100:
        return await i.response.send_message('❌ Cantidad 1-20, lados 2-100.', ephemeral=True)
    resultados = [random.randint(1, lados) for _ in range(cantidad)]
    embed = discord.Embed(title=f'🎲 {cantidad}D{lados}', color=discord.Color.blurple())
    embed.add_field(name='Resultados', value=' | '.join(f'**{r}**' for r in resultados), inline=False)
    embed.add_field(name='Total', value=f'**{sum(resultados)}**', inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='moneda', description='Lanza una moneda')
async def slash_moneda(i: discord.Interaction):
    resultado = random.choice(['🪙 Cara', '🪙 Sello'])
    embed = discord.Embed(title='🪙 Moneda', description=f'**{resultado}**', color=discord.Color.gold())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='ruleta', description='Elige aleatoriamente entre opciones')
@app_commands.describe(opciones='Opciones separadas por coma (ej: pizza, sushi, burger)')
async def slash_ruleta(i: discord.Interaction, opciones: str):
    lista = [o.strip() for o in opciones.split(',') if o.strip()]
    if len(lista) < 2:
        return await i.response.send_message('❌ Al menos 2 opciones separadas por coma.', ephemeral=True)
    elegida = random.choice(lista)
    embed = discord.Embed(title='🎡 Ruleta', color=discord.Color.red())
    embed.add_field(name='Opciones', value=' | '.join(f'`{o}`' for o in lista), inline=False)
    embed.add_field(name='🏆 Elegida', value=f'**{elegida}**', inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='8ball', description='Pregunta a la bola mágica')
@app_commands.describe(pregunta='Tu pregunta')
async def slash_8ball(i: discord.Interaction, pregunta: str):
    respuestas = ['✅ Sí, definitivamente.', '✅ Todo indica que sí.', '✅ Sin duda.', '🤔 No está claro.', '🤔 Concéntrate y pregunta de nuevo.', '❌ No cuentes con ello.', '❌ Mi respuesta es no.', '❌ Definitivamente no.']
    embed = discord.Embed(title='🎱 Bola Mágica', color=discord.Color.dark_purple())
    embed.add_field(name='❓ Pregunta', value=pregunta, inline=False)
    embed.add_field(name='🔮 Respuesta', value=random.choice(respuestas), inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='piedra', description='Piedra, papel o tijera contra el bot')
@app_commands.describe(eleccion='Tu elección')
@app_commands.choices(eleccion=[
    app_commands.Choice(name='Piedra 🪨', value='piedra'),
    app_commands.Choice(name='Papel 📄', value='papel'),
    app_commands.Choice(name='Tijera ✂️', value='tijera'),
])
async def slash_piedra(i: discord.Interaction, eleccion: str):
    opciones = ['piedra', 'papel', 'tijera']
    bot_eleccion = random.choice(opciones)
    emojis = {'piedra': '🪨', 'papel': '📄', 'tijera': '✂️'}
    if eleccion == bot_eleccion:
        resultado, color = '🤝 Empate', discord.Color.yellow()
    elif (eleccion == 'piedra' and bot_eleccion == 'tijera') or (eleccion == 'papel' and bot_eleccion == 'piedra') or (eleccion == 'tijera' and bot_eleccion == 'papel'):
        resultado, color = '🏆 ¡Ganaste!', discord.Color.green()
    else:
        resultado, color = '😈 ¡Perdiste!', discord.Color.red()
    embed = discord.Embed(title='🎮 RPS', description=resultado, color=color)
    embed.add_field(name='Tú', value=emojis[eleccion], inline=True)
    embed.add_field(name='Bot', value=emojis[bot_eleccion], inline=True)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='verdad_o_reto', description='Verdad o reto para un usuario')
@app_commands.describe(usuario='Usuario objetivo (opcional)')
async def slash_verdad_o_reto(i: discord.Interaction, usuario: discord.Member = None):
    m = usuario or i.user
    verdades = ['¿Cuál es tu mayor miedo?', '¿Qué es lo más embarazoso que te ha pasado?', '¿Tienes algún crush aquí?', '¿Cuál es tu mayor defecto?']
    retos = ["Cambia tu nick a 'Pollo Frito' por 1 hora.", 'Manda un meme al canal principal.', 'Escribe un poema sobre el bot.']
    tipo = random.choice(['Verdad 🔮', 'Reto 💥'])
    contenido = random.choice(verdades) if 'Verdad' in tipo else random.choice(retos)
    color = discord.Color.purple() if 'Verdad' in tipo else discord.Color.orange()
    embed = discord.Embed(title=f'🎮 {tipo}', description=f'Para {m.mention}\n\n**{contenido}**', color=color)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='acertijo', description='Presenta un acertijo')
async def slash_acertijo(i: discord.Interaction):
    acertijos = [('Tengo ciudades, pero no hay casas. Tengo montañas, pero no hay árboles. Tengo agua, pero no hay peces. ¿Qué soy?', 'Un mapa'), ('Cuanto más me seques, más mojado te quedas. ¿Qué soy?', 'Una toalla'), ('Tengo manos pero no puedo aplaudir. ¿Qué soy?', 'Un reloj'), ('Siempre delante de ti, pero no se puede ver. ¿Qué soy?', 'El futuro')]
    pregunta, respuesta = random.choice(acertijos)
    embed = discord.Embed(title='🧩 Acertijo', description=f'{pregunta}\n\n||**Respuesta:** {respuesta}||', color=discord.Color.purple())
    await i.response.send_message(embed=embed)

# ── FUN ───────────────────────────────────────────────────────────
@bot.tree.command(name='frase', description='Frase motivacional aleatoria')
async def slash_frase(i: discord.Interaction):
    embed = discord.Embed(title='💬 Frase del día', description=f'*{random.choice(FRASES_MOTIVACION)}*', color=discord.Color.teal())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='chiste', description='Chiste aleatorio')
async def slash_chiste(i: discord.Interaction):
    embed = discord.Embed(title='😂 Chiste', description=random.choice(CHISTES), color=discord.Color.yellow())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='meme', description='Meme aleatorio')
async def slash_meme(i: discord.Interaction):
    await i.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://meme-api.com/gimme') as resp:
                if resp.status == 200:
                    data = await resp.json()
                    embed = discord.Embed(title=data['title'], color=discord.Color.orange())
                    embed.set_image(url=data['url'])
                    return await i.followup.send(embed=embed)
    except Exception:
        pass
    await i.followup.send('❌ No pude obtener un meme. Intenta más tarde.')

@bot.tree.command(name='horoscopo', description='Horóscopo de un signo zodiacal')
@app_commands.describe(signo='Tu signo del zodiaco')
@app_commands.choices(signo=[app_commands.Choice(name=s, value=s) for s in ['Aries','Tauro','Géminis','Cáncer','Leo','Virgo','Libra','Escorpio','Sagitario','Capricornio','Acuario','Piscis']])
async def slash_horoscopo(i: discord.Interaction, signo: str):
    predicciones = ['Hoy es un gran día para tomar decisiones importantes.', 'Alguien cercano te sorprenderá gratamente.', 'El universo conspira a tu favor. Confía en el proceso.', 'Un reto te ayudará a crecer. No le temas.', 'La fortuna sonríe a los valientes hoy.']
    embed = discord.Embed(title=f'🔮 {signo}', description=random.choice(predicciones), color=discord.Color.purple())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='personalidad', description='Descubre tu tipo de personalidad')
async def slash_personalidad(i: discord.Interaction):
    tipos = ['🧠 Analítico — Lógico y detallista', '❤️ Empático — Sensible y comprensivo', '🦁 Líder — Decidido y carismático', '🎨 Creativo — Imaginativo e innovador', '🧘 Tranquilo — Paciente y reflexivo']
    embed = discord.Embed(title=f'🔮 {i.user.display_name}', description=random.choice(tipos), color=discord.Color.purple())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='compatibilidad', description='Compatibilidad entre dos usuarios')
@app_commands.describe(usuario='Usuario con quien comparar')
async def slash_compatibilidad(i: discord.Interaction, usuario: discord.Member):
    pct = random.randint(0, 100)
    color = discord.Color.green() if pct >= 70 else discord.Color.yellow() if pct >= 40 else discord.Color.red()
    barra = '█' * (pct // 10) + '░' * (10 - pct // 10)
    embed = discord.Embed(title='💞 Compatibilidad', color=color)
    embed.add_field(name='Pareja', value=f'{i.user.mention} & {usuario.mention}', inline=False)
    embed.add_field(name='Resultado', value=f'`{barra}` **{pct}%**', inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name='frase_personaje', description='Frase de un personaje de anime')
@app_commands.describe(personaje='Nombre del personaje (opcional)')
async def slash_frase_personaje(i: discord.Interaction, personaje: str = None):
    if personaje:
        frases = FRASES_PERSONAJES.get(personaje.lower())
        if not frases:
            return await i.response.send_message(f'❌ Personaje no encontrado. Usa `/personajes_lista`.', ephemeral=True)
    else:
        personaje, frases = random.choice(list(FRASES_PERSONAJES.items()))
    embed = discord.Embed(title=f'🎌 {personaje.title()}', description=f'*"{random.choice(frases)}"*', color=discord.Color.red())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='personajes_lista', description='Lista personajes de anime disponibles')
async def slash_personajes_lista(i: discord.Interaction):
    lista = ', '.join(f'`{p}`' for p in FRASES_PERSONAJES.keys())
    embed = discord.Embed(title='🎌 Personajes', description=lista, color=discord.Color.red())
    await i.response.send_message(embed=embed)

# ── SORTEOS ───────────────────────────────────────────────────────
@bot.tree.command(name='sorteo', description='Inicia un sorteo')
@app_commands.describe(segundos='Duración en segundos (10-86400)', premio='Premio del sorteo')
async def slash_sorteo(i: discord.Interaction, segundos: int, premio: str):
    if not _is_staff(i):
        return await _no_perm(i)
    if not 10 <= segundos <= 86400:
        return await i.response.send_message('❌ Entre 10s y 24h.', ephemeral=True)
    embed = discord.Embed(title='🎁 SORTEO', description=f'**Premio:** {premio}\n**Duración:** {segundos}s\nReacciona con 🎉 para participar!', color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f'Organizado por {i.user.display_name}')
    await i.response.send_message(embed=embed)
    msg = await i.original_response()
    await msg.add_reaction('🎉')
    await asyncio.sleep(segundos)
    msg = await i.channel.fetch_message(msg.id)
    reaccion = discord.utils.get(msg.reactions, emoji='🎉')
    usuarios = [u async for u in reaccion.users() if not u.bot] if reaccion else []
    if not usuarios:
        await i.channel.send('😢 Nadie participó en el sorteo.')
    else:
        ganador = random.choice(usuarios)
        await i.channel.send(f'🎉 ¡{ganador.mention} ganó **{premio}**! Felicidades!')

@bot.tree.command(name='encuesta_si_no', description='Crea una encuesta de Sí/No')
@app_commands.describe(pregunta='Pregunta de la encuesta')
async def slash_encuesta_si_no(i: discord.Interaction, pregunta: str):
    if not _is_staff(i):
        return await _no_perm(i)
    embed = discord.Embed(title='📊 Encuesta', description=pregunta, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f'Por {i.user.display_name}')
    await i.response.send_message(embed=embed)
    msg = await i.original_response()
    await msg.add_reaction('✅')
    await msg.add_reaction('❌')

# ── ROLEPLAY ──────────────────────────────────────────────────────
@bot.tree.command(name='pareja', description='Ver tu pareja actual')
async def slash_pareja(i: discord.Interaction):
    data = cargar_parejas()
    uid = str(i.user.id)
    if uid not in data:
        return await i.response.send_message('💔 No tienes pareja.', ephemeral=True)
    pareja_id = data[uid]
    pareja = i.guild.get_member(int(pareja_id))
    embed = discord.Embed(title='💑 Pareja', description=f'{i.user.mention} ❤️ {pareja.mention if pareja else f"<@{pareja_id}>"}', color=discord.Color.pink())
    await i.response.send_message(embed=embed)

@bot.tree.command(name='divorcio', description='Divorciarte de tu pareja')
async def slash_divorcio(i: discord.Interaction):
    data = cargar_parejas()
    uid = str(i.user.id)
    if uid not in data:
        return await i.response.send_message('💔 No tienes pareja.', ephemeral=True)
    pareja_id = data.pop(uid)
    data.pop(pareja_id, None)
    guardar_parejas(data)
    await i.response.send_message(f'💔 {i.user.mention} se divorció.')

# ── CUMPLEAÑOS ────────────────────────────────────────────────────
@bot.tree.command(name='cumple', description='Registra tu cumpleaños')
@app_commands.describe(fecha='Fecha en formato DD/MM (ej: 25/12). Vacío para ver el tuyo.')
async def slash_cumple(i: discord.Interaction, fecha: str = None):
    try:
        data = cargar_cumples()
    except Exception:
        data = {}
    uid = str(i.user.id)
    if not fecha:
        c = data.get(uid)
        if c:
            return await i.response.send_message(f'🎂 Tu cumpleaños: **{c}**', ephemeral=True)
        return await i.response.send_message('❌ No tienes cumpleaños registrado.', ephemeral=True)
    try:
        dia, mes = map(int, fecha.split('/'))
        assert 1 <= dia <= 31 and 1 <= mes <= 12
    except Exception:
        return await i.response.send_message('❌ Formato inválido. Usa DD/MM.', ephemeral=True)
    data[uid] = fecha
    try:
        guardar_cumples(data)
    except Exception:
        pass
    await i.response.send_message(f'✅ Cumpleaños registrado: **{fecha}**', ephemeral=True)

@bot.tree.command(name='cumple_ver', description='Ver el cumpleaños de un usuario')
@app_commands.describe(usuario='Usuario objetivo')
async def slash_cumple_ver(i: discord.Interaction, usuario: discord.Member):
    try:
        data = cargar_cumples()
    except Exception:
        data = {}
    c = data.get(str(usuario.id))
    if not c:
        return await i.response.send_message(f'❌ {usuario.display_name} no tiene cumpleaños registrado.', ephemeral=True)
    await i.response.send_message(f'🎂 Cumpleaños de {usuario.mention}: **{c}**')

@bot.tree.command(name='recordar', description='Programa un recordatorio')
@app_commands.describe(tiempo='Tiempo (ej: 10m, 2h, 30s)', mensaje='Mensaje del recordatorio')
async def slash_recordar(i: discord.Interaction, tiempo: str, mensaje: str):
    try:
        unidad = tiempo[-1].lower()
        valor = int(tiempo[:-1])
        segundos = valor * {'s': 1, 'm': 60, 'h': 3600}.get(unidad, 0)
        if segundos <= 0 or segundos > 86400:
            raise ValueError
    except Exception:
        return await i.response.send_message('❌ Formato: `30s`, `5m`, `2h` (máx 24h).', ephemeral=True)
    await i.response.send_message(f'⏰ Recordatorio en **{tiempo}**: {mensaje}', ephemeral=True)
    await asyncio.sleep(segundos)
    await i.user.send(f'⏰ **Recordatorio:** {mensaje}')

# ── ANIME ─────────────────────────────────────────────────────────
def _make_anime_slash(accion: str, info: dict):
    @bot.tree.command(name=accion, description=info['msg'].replace('{a}', 'tú').replace('{b}', 'alguien'))
    @app_commands.describe(usuario='Usuario objetivo (opcional)')
    @app_commands.rename(usuario='usuario')
    async def _slash_anime(i: discord.Interaction, usuario: discord.Member = None):
        a = i.user.display_name
        b = usuario.display_name if usuario else 'todos'
        msg = info['msg'].format(a=a, b=b)
        gif = await obtener_gif_anime(info['gif_tag'])
        embed = discord.Embed(description=f'**{msg}**', color=discord.Color.pink())
        if gif:
            embed.set_image(url=gif)
        await i.response.send_message(embed=embed)
    _slash_anime.__name__ = f'slash_{accion}'

for _a, _i in ANIME_ACCIONES.items():
    _make_anime_slash(_a, _i)

# ── CONFIG ────────────────────────────────────────────────────────
@bot.tree.command(name='setprefix', description='Cambia el prefijo del bot')
@app_commands.describe(nuevo_prefijo='Nuevo prefijo (ej: ?, ., $)')
async def slash_setprefix(i: discord.Interaction, nuevo_prefijo: str):
    if not i.user.guild_permissions.administrator:
        return await _no_perm(i)
    global PREFIX
    PREFIX = nuevo_prefijo
    bot.command_prefix = nuevo_prefijo
    cfg = cargar_config()
    cfg['prefix'] = nuevo_prefijo
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    await i.response.send_message(f'✅ Prefijo cambiado a `{nuevo_prefijo}`')

# ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f'Bot conectado: {bot.user} (ID: {bot.user.id})')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f'{PREFIX}ayuda | AntiNuke'))
    try:
        synced = await bot.tree.sync()
        log.info(f'Slash commands sincronizados: {len(synced)} comandos')
    except Exception as e:
        log.error(f'Error al sincronizar slash commands: {e}')

@tasks.loop(minutes=30)
async def watermark_guardian():
    """
    
    """
    mensajes_a_revisar = list(_bot_messages_cache)
    for msg in mensajes_a_revisar:
        try:
            if not msg.embeds:
                continue
            necesita_edicion = any((_get_wm() not in (e.footer.text or '') for e in msg.embeds if e.footer)) or any((not e.footer or not e.footer.text for e in msg.embeds))
            if necesita_edicion:
                embeds_corregidos = [_enforce_watermark(e) for e in msg.embeds]
                await _original_msg_edit(msg, embeds=embeds_corregidos)
                log.info(f'[Guardian] Marca restaurada en msg {msg.id}')
        except (discord.NotFound, discord.Forbidden):
            _bot_messages_cache.remove(msg)
        except Exception as e:
            log.error(f'[Guardian] Error revisando msg {msg.id}: {e}')

@watermark_guardian.before_loop
async def before_guardian():
    await bot.wait_until_ready()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send('🔒 No tienes permisos para ese comando.')
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send('❌ Miembro no encontrado.')
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f'❌ Argumento inválido. Usa `{PREFIX}ayuda`.')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f'❌ Falta un argumento. Usa `{PREFIX}ayuda`.')
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.error(f"Error en '{ctx.command}': {error}\n{traceback.format_exc()}")
        await ctx.send(f'⚠️ Error: `{error}`')
if __name__ == '__main__':
    while True:
        try:
            log.info('Iniciando bot...')
            bot.run(TOKEN, reconnect=True)
        except discord.LoginFailure:
            log.critical('TOKEN INVÁLIDO')
            sys.exit(1)
        except KeyboardInterrupt:
            log.info('Detenido.')
            sys.exit(0)
        except Exception:
            log.error(f'Error:\n{traceback.format_exc()}')
            log.info('Reiniciando en 5s...')
            time.sleep(5)
