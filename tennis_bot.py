import os
import json
import requests
from datetime import datetime, timedelta
import sys

# Configuration
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
WEATHER_API_KEY = os.environ.get('WEATHER_API_KEY')
CITY = "Edinburgh"
STATE_FILE = "state.json"

# Weather thresholds
MAX_WIND_SPEED_MPH = 15
PLAYING_HOURS_START = 9  # 9am
PLAYING_HOURS_END = 22   # 10pm
HOURS_BEFORE_SUNSET = 1

def send_telegram_message(message):
    """Send a message via Telegram bot"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    response = requests.post(url, data=data)
    return response.json()

def load_state():
    """Load bot state from file"""
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"booking": None}

def save_state(state):
    """Save bot state to file"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_weather_forecast():
    """Get 5-day weather forecast from OpenWeatherMap"""
    url = "http://api.openweathermap.org/data/2.5/forecast"
    params = {
        "q": f"{CITY},GB",
        "appid": WEATHER_API_KEY,
        "units": "metric"
    }
    response = requests.get(url, params=params)
    return response.json()

def get_sunset_time(lat, lon, date):
    """Get sunset time for a specific date"""
    timestamp = int(date.timestamp())
    url = "http://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": WEATHER_API_KEY
    }
    response = requests.get(url, params=params)
    data = response.json()
    return datetime.fromtimestamp(data['sys']['sunset'])

def analyze_day_weather(day_forecasts, day_name, sunset_time):
    """Analyze weather for a specific day and find playable windows"""
    results = {
        "playable": False,
        "windows": [],
        "reasons": [],
        "temp_range": [999, -999],
        "max_wind": 0
    }
    
    cutoff_time = sunset_time - timedelta(hours=HOURS_BEFORE_SUNSET)
    
    # First pass: enrich all forecasts with calculated fields
    for forecast in day_forecasts:
        dt = datetime.fromtimestamp(forecast['dt'])
        hour = dt.hour
        
        temp = forecast['main']['temp']
        wind_speed_ms = forecast['wind']['speed']
        wind_speed_mph = wind_speed_ms * 2.237  # Convert m/s to mph
        rain_prob = forecast.get('pop', 0) * 100  # Probability of precipitation
        will_rain = rain_prob > 30 or 'rain' in forecast.get('weather', [{}])[0].get('main', '').lower()
        
        forecast['hour'] = hour
        forecast['will_rain'] = will_rain
        forecast['rain_prob'] = rain_prob
        forecast['wind_mph'] = wind_speed_mph
        forecast['temp'] = temp
    
    # Second pass: analyze playing hours
    for forecast in day_forecasts:
        dt = datetime.fromtimestamp(forecast['dt'])
        hour = forecast['hour']
        
        # Only consider playing hours and before sunset cutoff
        if hour < PLAYING_HOURS_START or hour >= PLAYING_HOURS_END or dt > cutoff_time:
            continue
        
        # Track stats
        results['temp_range'][0] = min(results['temp_range'][0], forecast['temp'])
        results['temp_range'][1] = max(results['temp_range'][1], forecast['temp'])
        results['max_wind'] = max(results['max_wind'], forecast['wind_mph'])
        
        # Check conditions
        if forecast['wind_mph'] > MAX_WIND_SPEED_MPH:
            if f"wind too high" not in results['reasons']:
                results['reasons'].append(f"wind speeds up to {forecast['wind_mph']:.0f}mph")
    
    # Find dry windows
    dry_windows = []
    current_window = None
    
    sorted_forecasts = sorted(day_forecasts, key=lambda x: x['dt'])
    
    for forecast in sorted_forecasts:
        dt = datetime.fromtimestamp(forecast['dt'])
        hour = dt.hour
        
        if hour < PLAYING_HOURS_START or hour >= PLAYING_HOURS_END or dt > cutoff_time:
            continue
            
        if not forecast['will_rain'] and forecast['wind_mph'] <= MAX_WIND_SPEED_MPH:
            if current_window is None:
                current_window = {
                    "start": hour,
                    "end": hour + 3,
                    "temp": forecast['temp'],
                    "wind": forecast['wind_mph']
                }
            else:
                current_window['end'] = hour + 3
        else:
            if current_window:
                dry_windows.append(current_window)
                current_window = None
    
    if current_window:
        dry_windows.append(current_window)
    
    # Analyze windows
    has_morning_rain = any(f['will_rain'] and f.get('hour', 0) < 12 for f in day_forecasts)
    has_afternoon_rain = any(f['will_rain'] and 12 <= f.get('hour', 0) < 18 for f in day_forecasts)
    has_evening_rain = any(f['will_rain'] and f.get('hour', 0) >= 18 for f in day_forecasts)
    
    for window in dry_windows:
        window_desc = f"{window['start']:02d}:00-{window['end']:02d}:00"
        
        if window['start'] < 12 and has_afternoon_rain:
            window_desc += " (before rain)"
        elif window['start'] >= 18 and (has_morning_rain or has_afternoon_rain):
            window_desc += " (after rain)"
        
        results['windows'].append({
            "time": window_desc,
            "temp": window['temp'],
            "wind": window['wind']
        })
    
    results['playable'] = len(results['windows']) > 0 and results['max_wind'] <= MAX_WIND_SPEED_MPH
    
    return results

def wednesday_check():
    """Check weather on Wednesday and send recommendation"""
    print("Running Wednesday weather check...")
    
    weather_data = get_weather_forecast()
    
    if 'list' not in weather_data:
        send_telegram_message("❌ Error fetching weather data")
        return
    
    # Get coordinates for sunset calculation
    lat = weather_data['city']['coord']['lat']
    lon = weather_data['city']['coord']['lon']
    
    # Find Saturday and Sunday forecasts
    today = datetime.now()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    sunday = saturday + timedelta(days=1)
    
    saturday_forecasts = []
    sunday_forecasts = []
    
    for forecast in weather_data['list']:
        dt = datetime.fromtimestamp(forecast['dt'])
        if dt.date() == saturday.date():
            saturday_forecasts.append(forecast)
        elif dt.date() == sunday.date():
            sunday_forecasts.append(forecast)
    
    # Get sunset times
    sat_sunset = get_sunset_time(lat, lon, saturday)
    sun_sunset = get_sunset_time(lat, lon, sunday)
    
    # Analyze both days
    sat_analysis = analyze_day_weather(saturday_forecasts, "Saturday", sat_sunset)
    sun_analysis = analyze_day_weather(sunday_forecasts, "Sunday", sun_sunset)
    
    # Build message
    message = f"🎾 <b>Edinburgh Tennis Weather - Weekend Forecast</b>\n\n"
    
    def format_day_report(day_name, analysis, sunset):
        report = f"<b>{day_name}</b>\n"
        
        if analysis['playable']:
            report += "✅ Good for tennis!\n"
            for window in analysis['windows']:
                report += f"  • {window['time']}: {window['temp']:.0f}°C, wind {window['wind']:.0f}mph\n"
        else:
            report += "❌ Not ideal\n"
            if analysis['reasons']:
                for reason in analysis['reasons']:
                    report += f"  • {reason}\n"
            if not analysis['windows']:
                report += "  • Rain forecast throughout playing hours\n"
        
        if analysis['temp_range'][0] != 999:
            report += f"Temp range: {analysis['temp_range'][0]:.0f}-{analysis['temp_range'][1]:.0f}°C\n"
        
        report += f"Sunset: {sunset.strftime('%H:%M')}\n"
        return report
    
    message += format_day_report("Saturday", sat_analysis, sat_sunset)
    message += "\n"
    message += format_day_report("Sunday", sun_analysis, sun_sunset)
    
    if sat_analysis['playable'] or sun_analysis['playable']:
        message += "\n💬 Reply with your booking (e.g., 'Booked for Sunday at 15:00') or 'stop' to skip this week."
    else:
        message += "\n😔 No tennis this week - try again next Wednesday!"
    
    send_telegram_message(message)

def friday_reminder():
    """Send Friday reminder if booking exists"""
    print("Running Friday reminder check...")
    
    state = load_state()
    booking = state.get('booking')
    
    if not booking:
        print("No booking found, skipping reminder")
        return
    
    # Parse booking
    day = booking.get('day')  # 'saturday' or 'sunday'
    time = booking.get('time')  # '15:00'
    
    weather_data = get_weather_forecast()
    
    if 'list' not in weather_data:
        send_telegram_message("❌ Error fetching weather update")
        return
    
    # Find the booked day
    today = datetime.now()
    if day.lower() == 'saturday':
        target_day = today + timedelta(days=(5 - today.weekday()) % 7)
    else:  # sunday
        target_day = today + timedelta(days=(6 - today.weekday()) % 7)
    
    # Find forecasts for that day around the booked time
    target_hour = int(time.split(':')[0])
    relevant_forecasts = []
    
    for forecast in weather_data['list']:
        dt = datetime.fromtimestamp(forecast['dt'])
        if dt.date() == target_day.date():
            hour_diff = abs(dt.hour - target_hour)
            if hour_diff <= 2:  # Within 2 hours of booking
                relevant_forecasts.append(forecast)
    
    if not relevant_forecasts:
        send_telegram_message(f"⚠️ Reminder: You're booked for {day.capitalize()} at {time}\n\nCouldn't get detailed forecast for that time.")
        return
    
    # Analyze conditions
    closest = min(relevant_forecasts, key=lambda x: abs(datetime.fromtimestamp(x['dt']).hour - target_hour))
    
    temp = closest['main']['temp']
    wind_speed_mph = closest['wind']['speed'] * 2.237
    rain_prob = closest.get('pop', 0) * 100
    will_rain = rain_prob > 30 or 'rain' in closest.get('weather', [{}])[0].get('main', '').lower()
    
    message = f"🎾 <b>Tennis Reminder - {day.capitalize()} at {time}</b>\n\n"
    message += f"🌡️ Temperature: {temp:.0f}°C\n"
    message += f"💨 Wind: {wind_speed_mph:.0f}mph\n"
    message += f"🌧️ Rain chance: {rain_prob:.0f}%\n\n"
    
    if will_rain:
        message += "⚠️ Rain is likely - might want to cancel!\n"
    elif wind_speed_mph > MAX_WIND_SPEED_MPH:
        message += f"⚠️ High winds ({wind_speed_mph:.0f}mph) - might be tricky!\n"
    else:
        message += "✅ Conditions look good!\n"
    
    message += f"\nSee you on court! 🎾"
    
    send_telegram_message(message)
    
    # Clear booking after reminder
    state['booking'] = None
    save_state(state)

def check_for_bookings():
    """Check for new booking messages from user"""
    print("Checking for booking messages...")
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": -1, "limit": 10}
    
    response = requests.get(url, params=params)
    data = response.json()
    
    if not data.get('result'):
        return
    
    state = load_state()
    
    for update in data['result']:
        if 'message' not in update:
            continue
            
        message = update['message'].get('text', '').lower()
        
        if 'stop' in message:
            state['booking'] = None
            save_state(state)
            send_telegram_message("👍 Stopped - no booking this week.")
            return
        
        # Parse booking: "booked for sunday at 15:00" or "booked for saturday at 3pm"
        if 'booked' in message:
            day = 'saturday' if 'saturday' in message or 'sat' in message else 'sunday'
            
            # Extract time
            import re
            time_match = re.search(r'(\d{1,2}):?(\d{2})?(?:\s*(?:am|pm))?', message)
            
            if time_match:
                hour = int(time_match.group(1))
                minute = time_match.group(2) or '00'
                
                # Handle pm/am
                if 'pm' in message and hour < 12:
                    hour += 12
                elif 'am' in message and hour == 12:
                    hour = 0
                elif hour < 9 and 'am' not in message and 'pm' not in message:
                    # Assume pm if hour is small and no am/pm specified
                    hour += 12
                
                time_str = f"{hour:02d}:{minute}"
                
                state['booking'] = {
                    "day": day,
                    "time": time_str
                }
                save_state(state)
                
                send_telegram_message(f"✅ Booked! I'll remind you on Friday about {day.capitalize()} at {time_str}.")
                return

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "wednesday"
    
    if mode == "wednesday":
        wednesday_check()
    elif mode == "friday":
        friday_reminder()
    elif mode == "check_bookings":
        check_for_bookings()
    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python tennis_bot.py [wednesday|friday|check_bookings]")
