#!/bin/bash

# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install requirements
pip install -r requirements.txt

# Create necessary directories
mkdir -p ~/travelbot
cp trip-bot.py ~/travelbot/
cp requirements.txt ~/travelbot/

echo "Setup completed! To start the bot:"
echo "1. cd ~/travelbot"
echo "2. source venv/bin/activate"
echo "3. python trip-bot.py" 