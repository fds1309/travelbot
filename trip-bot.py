import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import folium
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from PIL import Image
import io

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='travelbot.log'
)
logger = logging.getLogger(__name__)

# Constants
BOT_TOKEN = '7748034407:AAEnFOPSbyoYLnOlUrpW-rUexAhCwB2vE90'
DB_PATH = 'travel_data.db'
TEMP_DIR = Path('temp')
TEMP_DIR.mkdir(exist_ok=True)

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
        'Welcome to Travel Map Bot! 🗺️\n'
        'Commands:\n'
        '/add [city] - Add a visited place\n'
        '/remove [city] - Remove a visited place\n'
        '/map - Generate your travel map (HTML)\n'
        '/mapimg - Generate your travel map (Image)\n'
        '/list - List all visited places'
    )

async def add_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new place to the user's visited places."""
    if not context.args:
        await update.message.reply_text('Please provide a place name. Usage: /add [place name]')
        return

    place_name = ' '.join(context.args)
    user_id = update.effective_user.id
    
    try:
        location = get_geocoder()(place_name, language="en")
        if not location:
            await update.message.reply_text('Could not find this place. Please try again with a different name.')
            return

        # Get simplified address
        address_parts = [part.strip() for part in location.address.split(',')]
        simplified_address = f"{address_parts[0]}, {address_parts[-1]}"
        
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

async def generate_map(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate an HTML map of visited places."""
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT place_name, latitude, longitude FROM visited_places WHERE user_id = ?', (user_id,))
    places = c.fetchall()
    conn.close()

    if not places:
        await update.message.reply_text('You haven\'t added any places yet! Use /add [place] to start.')
        return

    # Create map with optimized settings
    m = folium.Map(
        location=[0, 0],
        zoom_start=2,
        prefer_canvas=True,
        tiles='CartoDB positron'  # Lighter tile set
    )
    
    for place_name, lat, lon in places:
        folium.Marker(
            location=[lat, lon],
            popup=place_name,
            icon=folium.Icon(color='red', icon='info-sign')
        ).add_to(m)

    # Save map
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
    """Generate a PNG image of the visited places map."""
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT place_name, latitude, longitude FROM visited_places WHERE user_id = ?', (user_id,))
    places = c.fetchall()
    conn.close()

    if not places:
        await update.message.reply_text('You haven\'t added any places yet! Use /add [place] to start.')
        return

    # Create map
    m = folium.Map(
        location=[0, 0],
        zoom_start=2,
        prefer_canvas=True,
        tiles='CartoDB positron'
    )
    
    for place_name, lat, lon in places:
        folium.Marker(
            location=[lat, lon],
            popup=place_name,
            icon=folium.Icon(color='red', icon='info-sign')
        ).add_to(m)

    # Save map
    map_file = TEMP_DIR / f'user_map_{user_id}.html'
    m.save(str(map_file))
    
    try:
        # Configure Chrome options
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--window-size=1280,720')
        
        # Use system ChromeDriver instead of WebDriver Manager
        service = Service('/usr/local/bin/chromedriver')
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        try:
            # Load and screenshot map
            driver.get(f'file://{map_file.absolute()}')
            png = driver.get_screenshot_as_png()
            
            # Convert to PIL Image
            image = Image.open(io.BytesIO(png))
            
            # Save as temporary file
            image_file = TEMP_DIR / f'user_map_{user_id}.png'
            image.save(image_file, optimize=True, quality=85)
            
            # Send image
            with open(image_file, 'rb') as f:
                await update.message.reply_photo(photo=f)
                
        finally:
            driver.quit()
            
    except Exception as e:
        logger.error(f"Error generating map image: {str(e)}")
        await update.message.reply_text('Error generating map image. Please try again.')
    finally:
        # Cleanup
        map_file.unlink(missing_ok=True)
        if 'image_file' in locals():
            image_file.unlink(missing_ok=True)

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

def main():
    """Initialize and start the bot."""
    # Initialize database
    init_db()
    
    # Initialize bot
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_place))
    application.add_handler(CommandHandler("map", generate_map))
    application.add_handler(CommandHandler("list", list_places))
    application.add_handler(CommandHandler("remove", remove_place))
    application.add_handler(CommandHandler("mapimg", generate_map_image))
    
    # Start bot
    application.run_polling()

if __name__ == '__main__':
    main()

    