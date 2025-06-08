import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from staticmap import StaticMap, CircleMarker
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from PIL import Image, ImageDraw, ImageFont
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
DB_PATH = 'travel_data.db'
TEMP_DIR = Path('temp')
TEMP_DIR.mkdir(exist_ok=True)

# Временное хранилище настроек пользователя (в памяти)
user_temp_options = {}

# --- Новый блок: пошаговый выбор настроек через инлайн-кнопки ---
# Состояния для выбора
MAP_SETTINGS_STATE = {}

BOT_VERSION = '0.6'

def init_db():
    """Initialize the SQLite database with optimized settings."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS visited_places
                 (user_id INTEGER, place_name TEXT, latitude REAL, longitude REAL,
                  PRIMARY KEY (user_id, place_name))''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON visited_places(user_id)')
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
        '/add [city] - Add a visited place (please send only the city name in English, without country or other objects)\n'
        '/remove [city] - Remove a visited place\n'
        '/mapimg - Generate your travel map (Image)\n'
        '/list - List all visited places\n'
    )

async def add_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new place to the user's visited places."""
    if not context.args:
        await update.message.reply_text('Please provide a city name. Usage: /add [city name] (in English, only the city!)')
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
            # Уточнение города
            options = []
            for idx, loc in enumerate(locations):
                address = loc.raw.get('display_name', loc.address)
                options.append(f"{idx+1}. {address}")
            context.user_data['city_candidates'] = locations
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

async def ask_map_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, is_image=False):
    user_id = update.effective_user.id
    # Сбросить временные настройки
    user_temp_options[user_id] = {'labels': True, 'scale': 'auto', 'continent': None}
    # Первый шаг — подписи
    keyboard = [
        [InlineKeyboardButton("Show labels", callback_data=f"labels_on|{is_image}")],
        [InlineKeyboardButton("Hide labels", callback_data=f"labels_off|{is_image}")]
    ]
    await update.message.reply_text(
        "Choose if you want to show city labels on the map:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    MAP_SETTINGS_STATE[user_id] = {'step': 'labels', 'is_image': is_image}

async def map_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_map_settings(update, context, is_image=False)


async def mapimg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_map_settings(update, context, is_image=True)

async def map_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state = MAP_SETTINGS_STATE.get(user_id, {})
    data = query.data
    # Обработка выбора подписи
    if state.get('step') == 'labels':
        if data.startswith('labels_on'):
            user_temp_options[user_id]['labels'] = True
        elif data.startswith('labels_off'):
            user_temp_options[user_id]['labels'] = False
        # Следующий шаг — масштаб
        keyboard = [
            [InlineKeyboardButton("Auto", callback_data=f"scale_auto|{state.get('is_image', False)}")],
            [InlineKeyboardButton("World", callback_data=f"scale_world|{state.get('is_image', False)}")],
            [InlineKeyboardButton("Continent", callback_data=f"scale_continent|{state.get('is_image', False)}")]
        ]
        await query.edit_message_text(
            "Choose map scale:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        MAP_SETTINGS_STATE[user_id]['step'] = 'scale'
        return
    # Обработка выбора масштаба
    if state.get('step') == 'scale':
        if data.startswith('scale_auto'):
            user_temp_options[user_id]['scale'] = 'auto'
            user_temp_options[user_id]['continent'] = None
            await query.edit_message_text("Generating map...")
            await send_map_with_options(query, context, user_id, state.get('is_image', False))
            MAP_SETTINGS_STATE.pop(user_id, None)
            return
        elif data.startswith('scale_world'):
            user_temp_options[user_id]['scale'] = 'world'
            user_temp_options[user_id]['continent'] = None
            await query.edit_message_text("Generating map...")
            await send_map_with_options(query, context, user_id, state.get('is_image', False))
            MAP_SETTINGS_STATE.pop(user_id, None)
            return
        elif data.startswith('scale_continent'):
            user_temp_options[user_id]['scale'] = 'continent'
            # Следующий шаг — выбор континента
            keyboard = [[InlineKeyboardButton(c, callback_data=f"continent_{c}|{state.get('is_image', False)}")] for c in ["Europe", "Asia", "Africa", "North America", "South America", "Australia"]]
            await query.edit_message_text(
                "Choose continent:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            MAP_SETTINGS_STATE[user_id]['step'] = 'continent'
            return
    # Обработка выбора континента
    if state.get('step') == 'continent':
        if data.startswith('continent_'):
            cont = data.split('_', 1)[1].split('|')[0]
            user_temp_options[user_id]['continent'] = cont
            await query.edit_message_text("Generating map...")
            await send_map_with_options(query, context, user_id, state.get('is_image', False))
            MAP_SETTINGS_STATE.pop(user_id, None)
            return

async def generate_map(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    opts = user_temp_options.get(user_id, {'labels': True, 'scale': 'auto', 'continent': None})

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT place_name, latitude, longitude FROM visited_places WHERE user_id = ?', (user_id,))
    places = c.fetchall()
    conn.close()

    if not places:
        await update.message.reply_text('You haven\'t added any places yet! Use /add [city] to start.')
        return

    filtered_places = filter_places_by_scale(places, opts)
    map_location, map_zoom = get_map_center_zoom(filtered_places, opts)

    m = folium.Map(
        location=map_location,
        zoom_start=map_zoom,
        prefer_canvas=True,
        tiles='CartoDB positron',
        control_scale=False,
        zoom_control=False
    )

    for place_name, lat, lon in filtered_places:
        folium.Marker(
            location=[lat, lon],
            popup=place_name if opts.get('labels', True) else None,
            tooltip=place_name if opts.get('labels', True) else None,
            icon=folium.Icon(color='red', icon='info-sign')
        ).add_to(m)

    m.get_root().html.add_child(folium.Element("""<style>.leaflet-control-attribution {display: none !important;}</style>"""))

    map_file = TEMP_DIR / f'user_map_{user_id}.html'
    m.save(str(map_file))

    try:
        with open(map_file, 'rb') as f:
            await update.message.reply_document(
                document=f,
                filename=f'travel_map_{user_id}.html'
            )
    finally:
        map_file.unlink(missing_ok=True)

async def generate_map_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    opts = user_temp_options.get(user_id, {'labels': True, 'scale': 'auto', 'continent': None})

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT place_name, latitude, longitude FROM visited_places WHERE user_id = ?', (user_id,))
    places = c.fetchall()
    conn.close()

    if not places:
        await update.message.reply_text('You haven\'t added any places yet! Use /add [city] to start.')
        return

    filtered_places = filter_places_by_scale(places, opts)
    m = StaticMap(800, 400, url_template='http://a.tile.openstreetmap.org/{z}/{x}/{y}.png')
    marker_coords = []
    for place_name, lat, lon in filtered_places:
        m.add_marker(CircleMarker((lon, lat), 'red', 12))
        marker_coords.append((place_name, lon, lat))
    image = m.render()

    # Добавляем подписи через Pillow, если нужно
    if opts.get('labels', True):
        draw = ImageDraw.Draw(image)
        # Попробуем найти системный шрифт, иначе используем дефолтный
        try:
            font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 14)
        except Exception:
            font = ImageFont.load_default()
        for place_name, lon, lat in marker_coords:
            x, y = m.coordinate_to_pixel(lon, lat)
            draw.text((x + 10, y - 10), place_name, font=font, fill='black')

    image_file = TEMP_DIR / f'user_map_{user_id}.png'
    image.save(image_file)
    with open(image_file, 'rb') as f:
        await update.message.reply_photo(photo=f)
    image_file.unlink(missing_ok=True)

async def send_map_with_options(query, context, user_id, is_image):
    # Получаем update.message для передачи в старые функции
    class DummyMessage:
        def __init__(self, chat_id):
            self.chat_id = chat_id
        async def reply_document(self, *args, **kwargs):
            kwargs.setdefault('chat_id', self.chat_id)
            await context.bot.send_document(*args, **kwargs)
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
    if is_image:
        await generate_map_image(dummy_update, context)
    else:
        await generate_map(dummy_update, context)

async def list_places(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all visited places for the user."""
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT place_name FROM visited_places WHERE user_id = ? ORDER BY place_name', (user_id,))
    places = c.fetchall()
    conn.close()

    if not places:
        await update.message.reply_text('You haven\'t added any places yet!')
        return

    place_list = '\n'.join([f"📍 {place[0]}" for place in places])
    await update.message.reply_text(f'Your visited places:\n{place_list}')

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

# --- Вспомогательные функции ---
def filter_places_by_scale(places, opts):
    # places: [(name, lat, lon), ...]
    scale = opts.get('scale', 'auto')
    continent = opts.get('continent')
    country = opts.get('country')
    if scale == 'world' or scale == 'auto':
        return places
    if scale == 'continent' and continent:
        # Фильтрация по континенту (простая проверка по координатам)
        # Для production лучше использовать geo-библиотеку
        return [p for p in places if is_in_continent(p[1], p[2], continent)]
    if scale == 'country' and country:
        return [p for p in places if is_in_country(p[1], p[2], country)]
    return places

def get_map_center_zoom(places, opts):
    # places: [(name, lat, lon), ...]
    if not places:
        return [0, 0], 2
    scale = opts.get('scale', 'auto')
    if scale == 'world':
        return [0, 0], 2
    if scale == 'continent':
        # Центры континентов (примерно)
        centers = {
            'Europe': ([54, 15], 4),
            'Asia': ([34, 100], 3),
            'Africa': ([0, 20], 3),
            'North America': ([54, -105], 3),
            'South America': ([-15, -60], 3),
            'Australia': ([-25, 135], 4),
        }
        c = opts.get('continent')
        if c in centers:
            return centers[c]
        return [0, 0], 2
    if scale == 'country':
        # Центры некоторых стран (примерно)
        centers = {
            'Russia': ([60, 90], 3),
            'China': ([35, 105], 4),
            'USA': ([39, -98], 4),
            'Australia': ([-25, 135], 4),
        }
        c = opts.get('country')
        if c in centers:
            return centers[c]
        # Если страна не в списке — автоцентр по точкам
    # Автоцентр и зум по всем точкам
    lats = [lat for _, lat, _ in places]
    lons = [lon for _, _, lon in places]
    center = [sum(lats)/len(lats), sum(lons)/len(lons)]
    # Примерная оценка зума: чем больше разброс, тем меньше зум
    lat_range = max(lats) - min(lats)
    lon_range = max(lons) - min(lons)
    max_range = max(lat_range, lon_range)
    if max_range < 1:
        zoom = 10
    elif max_range < 5:
        zoom = 7
    elif max_range < 15:
        zoom = 5
    elif max_range < 40:
        zoom = 3
    else:
        zoom = 2
    return center, zoom

def is_in_continent(lat, lon, continent):
    # Примитивная фильтрация по координатам (для production лучше geo-библиотеку)
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

def is_in_country(lat, lon, country):
    # Для production лучше использовать geo-библиотеку или API
    # Здесь только для особых стран
    if country == 'Russia':
        return 40 <= lat <= 75 and 20 <= lon <= 180
    if country == 'China':
        return 18 <= lat <= 54 and 73 <= lon <= 135
    if country == 'USA':
        return 24 <= lat <= 49 and -125 <= lon <= -66
    if country == 'Australia':
        return -44 <= lat <= -10 and 112 <= lon <= 154
    return False


def main():
    """Initialize and start the bot."""
    # Initialize database
    init_db()
    
    # Initialize bot
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_place))
    application.add_handler(CommandHandler("map", map_command))
    application.add_handler(CommandHandler("list", list_places))
    application.add_handler(CommandHandler("remove", remove_place))
    application.add_handler(CommandHandler("mapimg", mapimg_command))
    application.add_handler(CallbackQueryHandler(map_settings_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city_choice))
    
    # Start bot
    application.run_polling()

if __name__ == '__main__':
    main()    