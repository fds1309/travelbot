import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.io.img_tiles as cimgt
from PIL import Image
#from selenium import webdriver
#from selenium.webdriver.chrome.service import Service
#from selenium.webdriver.chrome.options import Options
#import io

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='travelbot.log'
)
logger = logging.getLogger(__name__)

# Read BOT_TOKEN from file
with open(os.path.join(os.path.dirname(__file__), 'bot_token.txt'), 'r') as f:
    BOT_TOKEN = f.read().strip()

# Read BOT_NAME from file
with open(os.path.join(os.path.dirname(__file__), 'bot_name.txt'), 'r', encoding='utf-8') as f:
    BOT_NAME = f.read().strip()

# Read MESSAGE from file
with open(os.path.join(os.path.dirname(__file__), 'message.txt'), 'r', encoding='utf-8') as f:
    MESSAGE = f.read().strip()

DB_PATH = 'travel_data.db'
TEMP_DIR = Path('temp')
TEMP_DIR.mkdir(exist_ok=True)

# Временное хранилище настроек пользователя (в памяти)
user_temp_options = {}

# --- Новый блок: пошаговый выбор настроек через инлайн-кнопки ---
# Состояния для выбора
MAP_SETTINGS_STATE = {}

BOT_VERSION = '0.7'

# BBOX для мира и континентов (min_lon, min_lat, max_lon, max_lat)
CONTINENT_BBOX = {
    'Europe':        (-10, 35, 40, 70),
    'Asia':          (40, 5, 180, 80),
    'Africa':        (-20, -35, 55, 35),
    'North America': (-170, 10, -50, 80),
    'South America': (-90, -60, -30, 15),
    'Australia':     (110, -50, 180, -10),
    'World':         (-200, -55, 160, 75)
}

def init_db():
    """Initialize the SQLite database with optimized settings."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS visited_places
                 (user_id INTEGER, place_name TEXT, latitude REAL, longitude REAL,
                  PRIMARY KEY (user_id, place_name))''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON visited_places(user_id)')
    # Таблица для хранения языка пользователя
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                 (user_id INTEGER PRIMARY KEY, lang TEXT)''')
    conn.commit()
    conn.close()

def get_geocoder():
    """Get a rate-limited geocoder instance."""
    geolocator = Nominatim(user_agent="travel_map_bot", timeout=10)
    return RateLimiter(geolocator.geocode, min_delay_seconds=1)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a welcome message with available commands."""
    await update.message.reply_text(
        f'Welcome to Travel Map Bot v{BOT_VERSION}! 🗺️\n'
        'Commands:\n'
        '/add [city] - Add a visited place\n'
        '/remove [city] - Remove a visited place\n'
        '/mapimg - Generate your travel map (Image)\n'
        '/list - List all visited places\n'
    )

async def add_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new place to the user's visited places."""
    if not context.args:
        await update.message.reply_text('City name is preferred. Usage: /add [city name] (or hotel, or any other place name)')
        return

    place_name = ' '.join(context.args)
    user_id = update.effective_user.id
    
    try:
        geocode = get_geocoder()
        locations = list(geocode(place_name, exactly_one=False, language="en", addressdetails=True, limit=10))
        if not locations:
            await update.message.reply_text('Could not find this city. Please try again with a different name.')
            return
        if len(locations) > 1:
            # Уточнение города (только уникальные по названию+региону+стране)
            def get_display_key(loc):
                address = loc.raw.get('address', {})
                city = address.get('city') or address.get('town') or address.get('village') or address.get('hamlet') or ''
                state = address.get('state') or ''
                country = address.get('country') or ''
                return (city.strip().lower(), state.strip().lower(), country.strip().lower())
            unique_locs = []
            seen_keys = set()
            for loc in locations:
                key = get_display_key(loc)
                if key not in seen_keys:
                    unique_locs.append(loc)
                    seen_keys.add(key)
            options = []
            for idx, loc in enumerate(unique_locs):
                address = loc.raw.get('display_name', loc.address)
                options.append(f"{idx+1}. {address}")
            context.user_data['city_candidates'] = unique_locs
            await update.message.reply_text(
                'Several cities found with this name. Please reply with the number of the correct one:\n' + '\n'.join(options)
            )
            context.user_data['add_city_pending'] = place_name
            return
        def get_city_key(loc):
            # Получаем "чистое" название города из addressdetails, если есть
            address = loc.raw.get('address', {})
            city = address.get('city') or address.get('town') or address.get('village') or address.get('hamlet') or ''
            # Округляем координаты для устойчивости
            lat = round(loc.latitude, 3)
            lon = round(loc.longitude, 3)
            return (city.lower(), lat, lon)

        unique = []
        seen = set()
        for loc in locations:
            key = get_city_key(loc)
            if key not in seen:
                unique.append(loc)
                seen.add(key)
        locations = unique
        location = locations[0]
        address_parts = [part.strip() for part in location.address.split(',')]
        city = address_parts[0]
        simplified_address = city
        # Store in database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        try:
            c.execute('INSERT INTO visited_places VALUES (?, ?, ?, ?)',
                     (user_id, simplified_address, location.latitude, location.longitude))
            conn.commit()
            await update.message.reply_text(f'Added {simplified_address} to your visited places!')
        except sqlite3.IntegrityError:
            await update.message.reply_text(f'{simplified_address} is already in your visited places!')
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error adding place: {str(e)}")
        await update.message.reply_text('Error adding place. Please try again.')

async def handle_city_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'add_city_pending' in context.user_data and 'city_candidates' in context.user_data:
        try:
            idx = int(update.message.text.strip()) - 1
            locations = context.user_data['city_candidates']
            if 0 <= idx < len(locations):
                location = locations[idx]
                user_id = update.effective_user.id
                address_parts = [part.strip() for part in location.address.split(',')]
                city = address_parts[0]
                simplified_address = city
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                try:
                    c.execute('INSERT INTO visited_places VALUES (?, ?, ?, ?)',
                             (user_id, simplified_address, location.latitude, location.longitude))
                    conn.commit()
                    await update.message.reply_text(f'Added {simplified_address} to your visited places!')
                except sqlite3.IntegrityError:
                    await update.message.reply_text(f'{simplified_address} is already in your visited places!')
                finally:
                    conn.close()
                del context.user_data['add_city_pending']
                del context.user_data['city_candidates']
            else:
                await update.message.reply_text('Invalid number. Please try again.')
        except Exception:
            await update.message.reply_text('Please reply with the number of the correct city.')

async def ask_map_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_temp_options[user_id] = {'scale': 'auto', 'continent': None}
    keyboard = [
        [InlineKeyboardButton("Auto", callback_data="scale_auto")],
        [InlineKeyboardButton("World", callback_data="scale_world")],
        [InlineKeyboardButton("Continent", callback_data="scale_continent")]
    ]
    await update.message.reply_text(
        "Choose map scale:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    MAP_SETTINGS_STATE[user_id] = {'step': 'scale'}

async def mapimg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_map_settings(update, context)

async def map_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state = MAP_SETTINGS_STATE.get(user_id, {})
    data = query.data
    if state.get('step') == 'scale':
        if data == 'scale_auto':
            user_temp_options[user_id]['scale'] = 'auto'
            user_temp_options[user_id]['continent'] = None
            await query.edit_message_text("Generating map...")
            await send_map_with_options(query, context, user_id)
            MAP_SETTINGS_STATE.pop(user_id, None)
            return
        elif data == 'scale_world':
            user_temp_options[user_id]['scale'] = 'world'
            user_temp_options[user_id]['continent'] = None
            await query.edit_message_text("Generating map...")
            await send_map_with_options(query, context, user_id)
            MAP_SETTINGS_STATE.pop(user_id, None)
            return
        elif data == 'scale_continent':
            keyboard = [[InlineKeyboardButton(c, callback_data=f"continent_{c}")] for c in ["Europe", "Asia", "Africa", "North America", "South America", "Australia"]]
            await query.edit_message_text(
                "Choose continent:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            MAP_SETTINGS_STATE[user_id]['step'] = 'continent'
            return
    if state.get('step') == 'continent':
        if data.startswith('continent_'):
            cont = data.split('_', 1)[1].strip().title()
            user_temp_options[user_id]['continent'] = cont
            await query.edit_message_text("Generating map...")
            await send_map_with_options(query, context, user_id)
            MAP_SETTINGS_STATE.pop(user_id, None)
            return

async def generate_map_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    opts = user_temp_options.get(user_id, {'scale': 'auto', 'continent': None})

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT place_name, latitude, longitude FROM visited_places WHERE user_id = ?', (user_id,))
    places = c.fetchall()
    conn.close()

    if not places:
        await update.message.reply_text('You haven\'t added any places yet! Use /add [city] to start.')
        return

    scale = opts.get('scale', 'auto')
    continent = opts.get('continent')

    # --- КОРРЕКТНОЕ ОПРЕДЕЛЕНИЕ bbox и filtered_places ---
    if scale == 'continent' and continent and continent in CONTINENT_BBOX:
        bbox = CONTINENT_BBOX[continent]
        filtered_places = [p for p in places if is_in_continent(p[1], p[2], continent)]
    elif scale == 'world':
        bbox = CONTINENT_BBOX['World']
        filtered_places = places
    else:  # auto
        lats = [lat for _, lat, _ in places]
        lons = [lon for _, _, lon in places]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        dlat = (max_lat - min_lat) * 0.2 or 1
        dlon = (max_lon - min_lon) * 0.2 or 1
        bbox = (min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat)
        # Для auto всегда все точки
        filtered_places = places

    min_lon, min_lat, max_lon, max_lat = bbox

    # Для авто-режима делаем bbox квадратным и картинку квадратной
    if scale == 'auto':
        min_lon, min_lat, max_lon, max_lat = make_bbox_square(min_lon, min_lat, max_lon, max_lat)
        fig = plt.figure(figsize=(12, 12))  # квадратная картинка
        dpi = 200
    else:
        fig = plt.figure(figsize=(16, 8))   # для мира и континентов
        dpi = 300

    # Выбор zoom в зависимости от масштаба
    if scale == 'world':
        zoom = 3
    elif scale == 'continent':
        zoom = 4
    else:
        lat_range = max_lat - min_lat
        lon_range = max_lon - min_lon
        max_range = max(lat_range, lon_range)
        if max_range < 1:
            zoom = 8
        elif max_range < 5:
            zoom = 6
        elif max_range < 15:
            zoom = 5
        elif max_range < 40:
            zoom = 4
        else:
            zoom = 3

    tiler = cimgt.GoogleTiles()
    ax = plt.axes(projection=tiler.crs)
    ax.set_extent([min_lon, max_lon, min_lat, max_lat], crs=ccrs.PlateCarree())
    ax.add_image(tiler, zoom)

    # Рисуем маркеры
    for place_name, lat, lon in filtered_places:
        ax.plot(lon, lat, marker='o', color='red', markersize=8, transform=ccrs.PlateCarree())

    plt.tight_layout()
    ax.text(0.99, 0.01, BOT_NAME, fontsize=18, color='gray', alpha=0.7,
            ha='right', va='bottom', transform=ax.transAxes, fontweight='bold',
            bbox=dict(facecolor='white', edgecolor='none', alpha=0.8, boxstyle='round,pad=0.2'))

    image_file = TEMP_DIR / f'user_map_{user_id}.png'
    plt.savefig(image_file, bbox_inches='tight', dpi=dpi)
    plt.close(fig)

    with Image.open(image_file) as img:
        max_dim = 1280
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            img.save(image_file)

    with open(image_file, 'rb') as f:
        await update.message.reply_photo(photo=f, caption=MESSAGE)
    image_file.unlink(missing_ok=True)

async def send_map_with_options(query, context, user_id):
    class DummyMessage:
        def __init__(self, chat_id):
            self.chat_id = chat_id
        async def reply_photo(self, *args, **kwargs):
            kwargs.setdefault('chat_id', self.chat_id)
            await context.bot.send_photo(*args, **kwargs)
        async def reply_text(self, *args, **kwargs):
            kwargs.setdefault('chat_id', self.chat_id)
            await context.bot.send_message(*args, **kwargs)
    dummy_update = type('DummyUpdate', (), {
        'message': DummyMessage(query.message.chat_id),
        'effective_user': type('User', (), {'id': user_id})
    })()
    await generate_map_image(dummy_update, context)

async def list_places(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all visited places for the user."""
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT place_name, latitude, longitude FROM visited_places WHERE user_id = ? ORDER BY place_name', (user_id,))
    places = c.fetchall()
    conn.close()

    if not places:
        await update.message.reply_text('You haven\'t added any places yet!')
        return

    from geopy.geocoders import Nominatim
    geolocator = Nominatim(user_agent="travel_map_bot", timeout=10)
    result_lines = []
    for place_name, lat, lon in places:
        # Если place_name уже содержит запятую и страну, используем как есть
        if ',' in place_name:
            result_lines.append(f"\U0001F4CD {place_name}")
        else:
            # Получаем страну по координатам через reverse
            try:
                location = geolocator.reverse((lat, lon), exactly_one=True, language="en", addressdetails=True)
                country = ''
                if location and hasattr(location, 'raw'):
                    address = location.raw.get('address', {})
                    country = address.get('country', '')
                if country:
                    result_lines.append(f"\U0001F4CD {place_name}, {country}")
                else:
                    result_lines.append(f"\U0001F4CD {place_name}")
            except Exception:
                result_lines.append(f"\U0001F4CD {place_name}")
    await update.message.reply_text('Your visited places:\n' + '\n'.join(result_lines))

async def remove_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a place from the user's visited places."""
    if not context.args:
        await update.message.reply_text('Please provide a place name to remove. Usage: /remove [place name]')
        return

    place_name = ' '.join(context.args)
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Find matching places
    c.execute('SELECT place_name FROM visited_places WHERE user_id = ? AND place_name LIKE ?', 
             (user_id, f'%{place_name}%'))
    places = c.fetchall()
    
    if not places:
        await update.message.reply_text('Place not found in your visited places.')
        conn.close()
        return
    
    if len(places) > 1:
        places_list = '\n'.join([f"📍 {place[0]}" for place in places])
        await update.message.reply_text(
            f'Multiple matches found. Please be more specific:\n{places_list}'
        )
        conn.close()
        return
    
    # Remove the place
    c.execute('DELETE FROM visited_places WHERE user_id = ? AND place_name = ?',
             (user_id, places[0][0]))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f'Removed {places[0][0]} from your visited places!')

def get_bbox_for_scale(scale, continent, places):
    if scale == 'world':
        return CONTINENT_BBOX['World']
    if scale == 'continent' and continent in CONTINENT_BBOX:
        return CONTINENT_BBOX[continent]
    # Авто: по точкам с небольшим отступом
    lats = [lat for _, lat, _ in places]
    lons = [lon for _, _, lon in places]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    dlat = (max_lat - min_lat) * 0.2 or 1
    dlon = (max_lon - min_lon) * 0.2 or 1
    return (min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat)

def make_bbox_square(min_lon, min_lat, max_lon, max_lat):
    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2
    size = max(max_lon - min_lon, max_lat - min_lat)
    half = size / 2
    return (
        center_lon - half,
        center_lat - half,
        center_lon + half,
        center_lat + half
    )

def is_in_continent(lat, lon, continent):
    if continent == 'Europe':
        return 35 <= lat <= 70 and -10 <= lon <= 40
    if continent == 'Asia':
        return 5 <= lat <= 80 and 40 <= lon <= 180
    if continent == 'Africa':
        return -35 <= lat <= 35 and -20 <= lon <= 55
    if continent == 'North America':
        return 10 <= lat <= 80 and -170 <= lon <= -50
    if continent == 'South America':
        return -60 <= lat <= 15 and -90 <= lon <= -30
    if continent == 'Australia':
        return -50 <= lat <= -10 and 110 <= lon <= 180
    return False

async def set_bot_commands(application):
    commands = [
        BotCommand('start', 'Show welcome message and help'),
        BotCommand('add', 'Add a visited city'),
        BotCommand('remove', 'Remove a visited city'),
        BotCommand('mapimg', 'Generate your travel map (Image)'),
        BotCommand('list', 'List all visited cities')
    ]
    await application.bot.set_my_commands(commands)

def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).post_init(set_bot_commands).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_place))
    application.add_handler(CommandHandler("mapimg", mapimg_command))
    application.add_handler(CommandHandler("list", list_places))
    application.add_handler(CommandHandler("remove", remove_place))
    application.add_handler(CallbackQueryHandler(map_settings_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city_choice))
    application.run_polling()

if __name__ == '__main__':
    main()    