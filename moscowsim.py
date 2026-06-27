import asyncio
import logging
import os
import sys
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timedelta

import requests
import numpy as np
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set")
    sys.exit(1)
BASE_URL = "https://moscowsim.ru"
START_URL = BASE_URL

# FOR TESTING - hardcoded time
HARDCODED_TIME = "1:28.700"  # <-- ADD THIS LINE

# Хранилище состояний пользователей с временем создания
user_states: Dict[int, Dict[str, Any]] = {}

# Кэш для данных
cache = {}
cache_ttl = 300  # 5 минут

# Session
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
})

def get_cached_or_fetch(url: str, ttl: int = 300):
    """Get data from cache or fetch from URL"""
    now = datetime.now()
    if url in cache:
        data, timestamp = cache[url]
        if (now - timestamp).seconds < ttl:
            return data
    
    try:
        result = session.get(url)
        cache[url] = (result, now)
        return result
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

def cleanup_old_states():
    """Remove old user states older than 30 minutes"""
    now = datetime.now()
    to_delete = [uid for uid, data in user_states.items() 
                 if 'created_at' in data and (now - data['created_at']).seconds > 1800]
    for uid in to_delete:
        del user_states[uid]

def time_to_seconds(t: str) -> float:
    """Convert time string 'm:ss.xxx' to seconds"""
    try:
        t = t.strip().replace(",", ".")
        if ":" in t:
            m, s = t.split(":")
            return int(m) * 60 + float(s)
        else:
            return float(t)
    except:
        return float('inf')

def parse_racetime_row(tr) -> Optional[Dict[str, Any]]:
    """Parse sector times and best lap from a leaderboard row."""
    if tr.select_one("span.badge"):
        return None

    cells = tr.find_all("td", class_="racetime text-end")
    if len(cells) < 4:
        return None

    sector_texts = [cell.get_text(strip=True) for cell in cells[:3]]
    lap_text = cells[-1].get_text(strip=True)
    lap_sec = time_to_seconds(lap_text)

    if lap_sec == float('inf'):
        return None

    return {
        "lap": lap_text,
        "lap_sec": lap_sec,
        "sectors": sector_texts,
        "sector_secs": [time_to_seconds(s) for s in sector_texts],
    }

def get_links():
    """Get links and locations from main page"""
    resp = get_cached_or_fetch(START_URL)
    if not resp:
        return [], []
    
    soup = BeautifulSoup(resp.text, "html.parser")

    links = []
    locations = []

    for a in soup.select('.t-card__title a.t-card__link'):
        href = a.get("href")
        if not href:
            continue
        if "/clubs/" in href and "time-attack" in href:
            full_url = href if href.startswith("http") else BASE_URL + href
            links.append(full_url)
            locations.append(a.get_text(strip=True))

    return links, locations[1:]

def get_max_pages(soup):
    """Extract the maximum page number from the paginator select element"""
    try:
        paginator_select = soup.find("select", {"id": "id_results_paginator"})
        
        if paginator_select:
            options = paginator_select.find_all("option")
            page_numbers = []
            for option in options:
                value = option.get("value")
                if value and value.isdigit():
                    page_numbers.append(int(value))
            
            if page_numbers:
                return max(page_numbers)
    except Exception as e:
        logger.error(f"Error finding max pages: {e}")
    
    return 1

def find_users_by_last_name(last_name: str, base_url: str) -> list:
    """Find all users with given last name across all pages"""
    # Get first page to determine max pages
    try:
        first_page_url = base_url
        if '?' in base_url:
            first_page_url = f"{base_url}&page=1"
        else:
            first_page_url = f"{base_url}?page=1"
        
        resp = get_cached_or_fetch(first_page_url)
        if not resp:
            return []
            
        soup = BeautifulSoup(resp.text, "html.parser")
        max_page = get_max_pages(soup)
    except Exception as e:
        logger.error(f"Error fetching first page: {e}")
        max_page = 4
    
    found_users = []
    seen_ids = set()
    
    for page in range(1, max_page + 1):
        if '?' in base_url:
            url = f"{base_url}&page={page}"
        else:
            url = f"{base_url}?page={page}"

        try:
            resp = get_cached_or_fetch(url)
            if not resp:
                continue
                
            soup = BeautifulSoup(resp.text, "html.parser")

            for tr in soup.find_all("tr", {"data-id": True}):
                name_cell = tr.find("td", class_="first nowrap")
                if name_cell and name_cell.get_text(strip=True).lower().startswith(last_name.lower()):
                    entry = parse_racetime_row(tr)
                    if entry:
                        full_name = name_cell.get_text(strip=True)
                        tr_id = tr.get("data-id")
                        
                        if tr_id and tr_id not in seen_ids:
                            found_users.append({
                                'id': tr_id,
                                'name': full_name,
                                'time': entry['lap'],
                                'page': page
                            })
                            seen_ids.add(tr_id)
        except Exception as e:
            logger.error(f"Error on page {page}: {e}")
            continue
    
    return found_users

def parse_leaderboard_page(soup, my_tr_id: str, results: List, all_entries: List, seen_laps: set) -> Optional[Dict[str, Any]]:
    """Parse one leaderboard page into accumulated result lists."""
    my_entry = None

    for tr in soup.find_all("tr", {"data-id": True}):
        entry = parse_racetime_row(tr)
        if not entry:
            continue

        all_entries.append(entry)

        data_id = tr.get("data-id")
        if data_id == my_tr_id:
            my_entry = entry

        if entry["lap"] not in seen_laps:
            results.append(entry)
            seen_laps.add(entry["lap"])

    return my_entry

def parse_leaderboard(
    url: str,
    my_tr_id: str,
    stop_at_user: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Parse leaderboard pages; optionally stop once the user's row is found."""
    results = []
    all_entries = []
    my_entry = None
    seen_laps = set()
    max_page = 1

    try:
        first_page_url = f"{url}&page=1" if "?" in url else f"{url}?page=1"
        resp = get_cached_or_fetch(first_page_url)
        if not resp:
            return [], [], None

        soup = BeautifulSoup(resp.text, "html.parser")
        max_page = get_max_pages(soup)
        page_my_entry = parse_leaderboard_page(soup, my_tr_id, results, all_entries, seen_laps)
        if page_my_entry:
            my_entry = page_my_entry
            if stop_at_user:
                results.sort(key=lambda e: e["lap_sec"])
                return results, all_entries, my_entry
    except Exception as e:
        logger.error(f"Error fetching first page of {url}: {e}")

    for page in range(2, max_page + 1):
        page_url = f"{url}&page={page}" if "?" in url else f"{url}?page={page}"
        try:
            resp = get_cached_or_fetch(page_url)
            if not resp:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            page_my_entry = parse_leaderboard_page(soup, my_tr_id, results, all_entries, seen_laps)
            if page_my_entry:
                my_entry = page_my_entry
                if stop_at_user:
                    break
        except Exception as e:
            logger.error(f"Error on page {page} of {url}: {e}")
            continue

    results.sort(key=lambda e: e["lap_sec"])
    return results, all_entries, my_entry

def count_faster_than(entries: List[Dict[str, Any]], my_time_sec: float) -> int:
    """Count racers with a faster lap than the user's time."""
    if my_time_sec == float('inf'):
        return 0
    return sum(1 for e in entries if e["lap_sec"] < my_time_sec)

def apply_faster_counts(my_time_sec: float, overall_stats: Dict, recommendations: Dict) -> None:
    """Update faster-than counts using the user's reference lap time."""
    if overall_stats.get("all_entries") is not None:
        overall_my_sec = my_time_sec
        overall_my_entry = overall_stats.get("my_entry")
        if overall_my_entry:
            overall_my_sec = overall_my_entry["lap_sec"]
        faster_count = count_faster_than(overall_stats["all_entries"], overall_my_sec)
        overall_stats["faster_count"] = faster_count
        overall_stats["position"] = faster_count + 1 if overall_my_sec != float('inf') else None

    for data in recommendations.values():
        track_my_sec = my_time_sec
        track_my_entry = data.get("my_entry")
        if track_my_entry:
            track_my_sec = track_my_entry["lap_sec"]
        all_entries = data.get("all_entries", [])
        data["faster_count"] = count_faster_than(all_entries, track_my_sec)

def get_recommendations(my_tr_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], Dict, Dict]:
    """Get user's time, full entry with sectors, and per-track recommendations."""
    links, locations = get_links()
    
    if not links:
        return None, None, {}, {}
    
    # START OF CHANGES
    # Use hardcoded time if defined, otherwise set to None
    if 'HARDCODED_TIME' in globals():
        my_time = HARDCODED_TIME
    else:
        my_time = None
    # END OF CHANGES
    
    my_entry = None
    recommendations = {}
    overall_stats = {}

    for i, link in enumerate(links):
        entries, all_entries, my_entry_from_page = parse_leaderboard(
            link, my_tr_id, stop_at_user=(i == 0)
        )
        if my_entry_from_page and not my_entry:
            my_entry = my_entry_from_page
            # START OF CHANGES
            # Only set from page if not using hardcoded time
            if not my_time:
                my_time = my_entry_from_page["lap"]
            # END OF CHANGES

        if i == 0:
            overall_stats = {"all_entries": all_entries, "my_entry": my_entry_from_page}
        elif i - 1 < len(locations):
            top_entries = entries[:3]
            recommendations[locations[i - 1]] = {
                "top_times": [e["lap"] for e in top_entries],
                "top_entries": top_entries,
                "my_entry": my_entry_from_page,
                "all_entries": all_entries,
            }

    if my_time:
        apply_faster_counts(time_to_seconds(my_time), overall_stats, recommendations)

    return my_time, my_entry, recommendations, overall_stats

def calculate_stats(my_time: str, recommendations: Dict) -> Dict:
    """Calculate placement statistics"""
    if isinstance(my_time, str):
        my_time_sec = time_to_seconds(my_time)
    else:
        my_time_sec = my_time

    improvements = {'1st': [], '2nd': [], '3rd': []}
    location_improvements = {'1st': [], '2nd': [], '3rd': []}

    for loc, data in recommendations.items():
        top = data["top_times"]
        if len(top) < 3:
            continue
            
        top_sec = [time_to_seconds(t) for t in top]

        imp_1 = max(0, my_time_sec - top_sec[0])
        imp_2 = max(0, my_time_sec - top_sec[1])
        imp_3 = max(0, my_time_sec - top_sec[2])

        improvements['1st'].append(imp_1)
        improvements['2nd'].append(imp_2)
        improvements['3rd'].append(imp_3)

        location_improvements['1st'].append((loc, imp_1, top[0]))
        location_improvements['2nd'].append((loc, imp_2, top[1]))
        location_improvements['3rd'].append((loc, imp_3, top[2]))

    results = {}
    for position in ['1st', '2nd', '3rd']:
        if location_improvements[position]:
            sorted_locations = sorted(location_improvements[position], key=lambda x: x[1])
            top_3_easiest = sorted_locations[:3]

            results[position] = {
                'mean': np.mean(improvements[position]) if improvements[position] else 0,
                'easiest_locations': top_3_easiest
            }
        else:
            results[position] = {
                'mean': 0,
                'easiest_locations': []
            }

    return results

def calculate_sector_stats(recommendations: Dict) -> Dict:
    """Average sector delta vs top-3 on each track."""
    if not recommendations:
        return {}

    sector_labels = ["S1", "S2", "S3"]
    positions = ["1st", "2nd", "3rd"]
    results = {position: {label: [] for label in sector_labels} for position in positions}

    for data in recommendations.values():
        my_at_track = data.get("my_entry")
        top_entries = data.get("top_entries", [])
        if not my_at_track or len(top_entries) < 3:
            continue

        my_sectors = my_at_track["sector_secs"]
        for idx, position in enumerate(positions):
            rival_sectors = top_entries[idx].get("sector_secs", [])
            if len(rival_sectors) < 3:
                continue

            for s_idx, label in enumerate(sector_labels):
                results[position][label].append(my_sectors[s_idx] - rival_sectors[s_idx])

    sector_stats = {}
    for position in positions:
        sector_stats[position] = {
            label: np.mean(deltas) if deltas else None
            for label, deltas in results[position].items()
        }

    return sector_stats

def format_sector_delta(delta: Optional[float]) -> str:
    if delta is None:
        return "—"
    if abs(delta) < 0.001:
        return "✅"
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:.2f} сек"

def format_stats_message(
    my_time: str,
    last_name: str,
    recommendations: Dict,
    stats: Dict,
    sector_stats: Optional[Dict] = None,
    overall_stats: Optional[Dict] = None,
):
    """Format statistics into a single message"""
    
    message = (
        "🏁 *ГОНОЧНАЯ СТАТИСТИКА* 🏁\n\n"
        f"👤 *{last_name}:* `{my_time}`\n"
    )

    if overall_stats and overall_stats.get("position"):
        faster = overall_stats["faster_count"]
        message += f"📈 Быстрее вас: *{faster}* \n\n"

    message += "────────────────────" + "\n\n"

    # ========== БЛОК 1: ПОЗИЦИИ НА ПОДИУМЕ ==========
    
    podium_data = [
        ('1st', '🥇', '1-ое МЕСТО'),
        ('2nd', '🥈', '2-ое МЕСТО'),
        ('3rd', '🥉', '3-ье МЕСТО')
    ]

    for position, medal, title in podium_data:
        data = stats.get(position, {})

        message += f"{medal} *{title}*\n"
        message += f"   📊 Среднее отставание: {data.get('mean', 0):.2f} сек\n"

        if sector_stats:
            sector_means = sector_stats.get(position, {})
            if any(v is not None for v in sector_means.values()):
                message += (
                    f"   ⏱ Сектора (среднее): "
                    f"S1 {format_sector_delta(sector_means.get('S1'))} | "
                    f"S2 {format_sector_delta(sector_means.get('S2'))} | "
                    f"S3 {format_sector_delta(sector_means.get('S3'))}\n"
                )

        message += "\n"

        easiest = data.get('easiest_locations', [])
        if easiest:
            message += f"   🎯 Самые лёгкие локации:\n"
            for i, (loc, seconds, target_time) in enumerate(easiest, 1):
                loc_short = loc[:20] + "..." if len(loc) > 23 else loc
                message += f"      {i}. `{loc_short}` (`{target_time}`) → "
                if seconds < 0.001:
                    message += "✅ \n"
                else:
                    message += f"*{seconds:.2f}* сек\n"
        message += "\n"

    # ========== БЛОК 2: ЛУЧШИЕ ВРЕМЕНА ПО ТРАССАМ ==========
    
    message += "─" * 20 + "\n\n"
    message += "🏆 *ЛУЧШИЕ ВРЕМЕНА НА ЛОКАЦИЯХ* 🏆\n\n"

    # Сортируем трассы по среднему времени (самые быстрые сначала)
    tracks_with_avg = []
    for track, data in recommendations.items():
        top_times = data.get("top_times", [])
        if len(top_times) < 3:
            continue
            
        top_sec = [time_to_seconds(t) for t in top_times]
        avg_time_sec = np.mean(top_sec)
        avg_time_str = f"{int(avg_time_sec//60)}:{avg_time_sec%60:05.3f}"

        tracks_with_avg.append({
            'track': track,
            'top_times': top_times,
            'avg_sec': avg_time_sec,
            'avg_str': avg_time_str
        })

    tracks_sorted = sorted(tracks_with_avg, key=lambda x: x['avg_sec'], reverse=True)

    for item in tracks_sorted:
        track = item['track']
        track_name_short = track[:25] + "..." if len(track) > 28 else track
        top_times = item['top_times']
        avg_str = item['avg_str']
        track_data = recommendations.get(track, {})
        faster_count = track_data.get("faster_count")
        
        message += f"📍 *{track_name_short}*\n"
        message += f"   ⏰ {', '.join([f'`{t}`' for t in top_times[:3]])}\n"
        message += f"   📊 Среднее время подиума: `{avg_str}`\n"
        message += f"   🏎 Быстрее вас: *{faster_count}* \n\n"
    
    return [message]  # Возвращаем список с одним сообщением для совместимости

async def send_long_message(update: Update, text_messages: list, reply_markup=None):
    """Отправляет длинное сообщение по частям с поддержкой Markdown"""
    for i, msg in enumerate(text_messages):
        # Только к последнему сообщению добавляем кнопку обновления
        markup = reply_markup if i == len(text_messages) - 1 else None
        
        if update.callback_query:
            if i == 0:
                await update.callback_query.edit_message_text(
                    msg, 
                    reply_markup=markup,
                    parse_mode='Markdown'
                )
            else:
                await update.callback_query.message.reply_text(
                    msg, 
                    reply_markup=markup,
                    parse_mode='Markdown'
                )
        else:
            await update.message.reply_text(
                msg, 
                reply_markup=markup,
                parse_mode='Markdown'
            )
        
        # Небольшая задержка между сообщениями
        if i < len(text_messages) - 1:
            await asyncio.sleep(0.3)

def get_refresh_keyboard(last_name: str = None, user_id: str = None) -> InlineKeyboardMarkup:
    """Create refresh button with user data"""
    keyboard = []
    
    # Сохраняем данные пользователя в callback_data
    if last_name and user_id:
        callback_data = f"refresh_{last_name}_{user_id}"
    else:
        callback_data = "refresh"
    
    keyboard.append([InlineKeyboardButton("🔄 Обновить статистику", callback_data=callback_data)])
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when /start is issued."""
    user_id = update.effective_user.id
    cleanup_old_states()  # Очищаем старые состояния
    
    await update.message.reply_text(
        "👋 Привет! Я бот для анализа статистики заездов на MoscowSim.ru\n\n"
        "📝 Для начала анализа отправь свою фамилию (например: Иванов)\n\n"
        "ℹ️ Доступные команды:\n"
        "/start - Показать это сообщение\n"
        "/help - Помощь"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when /help is issued."""
    await update.message.reply_text(
        "📋 Инструкция:\n\n"
        "1. Отправь свою фамилию\n"
        "2. Если найдено несколько пользователей, выбери нужного с помощью кнопок\n"
        "3. Бот покажет подробную статистику твоих заездов\n\n"
        "Также можно использовать /start для повторного приветствия"
    )

async def handle_last_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user's last name input."""
    user_id = update.effective_user.id
    last_name = update.message.text.strip()
    
    if not last_name:
        await update.message.reply_text("❌ Пожалуйста, введите корректную фамилию")
        return
    
    searching_msg = await update.message.reply_text(f"🔍 Ищу пользователей с фамилией '{last_name}'...")
    
    links, _ = get_links()
    if not links:
        await searching_msg.edit_text("❌ Не удалось получить данные с сайта")
        return
    
    users = find_users_by_last_name(last_name.lower(), links[0])
    
    if not users:
        await searching_msg.edit_text(f"❌ Пользователь с фамилией '{last_name}' не найден.")
        return
    
    await searching_msg.delete()
    
    if len(users) == 1:
        # Found single user
        user = users[0]
        await update.message.reply_text(f"✅ Найден пользователь: {user['name']} с временем {user['time']}")
        
        # Show loading indicator
        status_msg = await update.message.reply_text("📊 Загружаю статистику...")
        
        # Get recommendations
        my_time, my_entry, recommendations, overall_stats = get_recommendations(user['id'])
        if not my_time:
            my_time = user['time']
        apply_faster_counts(time_to_seconds(my_time), overall_stats, recommendations)
        
        if my_time and recommendations:
            stats = calculate_stats(my_time, recommendations)
            sector_stats = calculate_sector_stats(recommendations)
            stats_messages = format_stats_message(
                my_time, user['name'], recommendations, stats, sector_stats, overall_stats
            )
            
            await status_msg.delete()
            
            # Сохраняем данные пользователя в context.user_data для обновления
            context.user_data['last_name'] = last_name
            context.user_data['user_id'] = user['id']
            context.user_data['user_name'] = user['name']
            
            # Отправляем сообщения
            await send_long_message(update, stats_messages, get_refresh_keyboard(last_name, user['id']))
        else:
            await status_msg.edit_text("❌ Не удалось загрузить статистику")
    
    else:
        # Multiple users found
        keyboard = []
        for i, user in enumerate(users, 1):
            keyboard.append([InlineKeyboardButton(
                f"{i}. {user['name']} ({user['time']})", 
                callback_data=f"select_{user['id']}"
            )])
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        
        user_states[user_id] = {
            'users': users,
            'last_name': last_name,
            'created_at': datetime.now()
        }
        
        message = f"🔍 Найдено несколько пользователей с фамилией '{last_name}':\n\nВыберите нужного:"
        
        await update.message.reply_text(message, reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries from inline keyboards."""
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    
    # Обработка кнопки обновления
    if query.data == "refresh" or query.data.startswith("refresh_"):
        # Извлекаем данные из callback_data
        if query.data.startswith("refresh_"):
            parts = query.data.split("_")
            if len(parts) >= 3:
                last_name = parts[1]
                user_id_selected = parts[2]
                user_name = context.user_data.get('user_name', last_name)  # Получаем имя или используем фамилию
            else:
                # Если данные не сохранились, используем сохраненные в context
                last_name = context.user_data.get('last_name')
                user_id_selected = context.user_data.get('user_id')
                user_name = context.user_data.get('user_name', last_name)
        else:
            last_name = context.user_data.get('last_name')
            user_id_selected = context.user_data.get('user_id')
            user_name = context.user_data.get('user_name', last_name)
        
        if not last_name or not user_id_selected:
            await query.edit_message_text("❌ Не удалось обновить данные. Пожалуйста, введите фамилию заново.")
            return
        
        # Показываем обновление
        await query.edit_message_text("🔄 Обновляю статистику...")
        
        # Получаем свежие данные
        my_time, my_entry, recommendations, overall_stats = get_recommendations(user_id_selected)
        
        if my_time and recommendations:
            stats = calculate_stats(my_time, recommendations)
            sector_stats = calculate_sector_stats(recommendations)
            stats_messages = format_stats_message(
                my_time, user_name, recommendations, stats, sector_stats, overall_stats
            )
            
            # Сохраняем данные для будущих обновлений
            context.user_data['last_name'] = last_name
            context.user_data['user_id'] = user_id_selected
            context.user_data['user_name'] = user_name
            
            # Отправляем обновленную статистику
            await send_long_message(update, stats_messages, get_refresh_keyboard(last_name, user_id_selected))
        else:
            await query.edit_message_text("❌ Не удалось обновить статистику. Попробуйте позже.")
        return
    
    # Обработка отмены
    if query.data == "cancel":
        await query.edit_message_text("❌ Выбор отменён")
        if user_id in user_states:
            del user_states[user_id]
        return
    
    # Обработка выбора пользователя
    if query.data.startswith("select_"):
        user_id_selected = query.data.replace("select_", "")
        
        # Find the user in stored data
        user_data = user_states.get(user_id, {})
        users = user_data.get('users', [])
        last_name = user_data.get('last_name', '')
        
        selected_user = None
        for user in users:
            if user['id'] == user_id_selected:
                selected_user = user
                break
        
        if not selected_user:
            await query.edit_message_text("❌ Пользователь не найден")
            return
        
        await query.edit_message_text(f"✅ Найден пользователь: {selected_user['name']} с временем {selected_user['time']}")
        
        # Show loading indicator
        status_msg = await query.message.reply_text("📊 Загружаю статистику...")
        
        # Get recommendations
        my_time, my_entry, recommendations, overall_stats = get_recommendations(selected_user['id'])
        if not my_time:
            my_time = selected_user['time']
        apply_faster_counts(time_to_seconds(my_time), overall_stats, recommendations)
        
        if my_time and recommendations:
            stats = calculate_stats(my_time, recommendations)
            sector_stats = calculate_sector_stats(recommendations)
            stats_messages = format_stats_message(
                my_time, selected_user['name'], recommendations, stats, sector_stats, overall_stats
            )
            
            await status_msg.delete()
            
            # Сохраняем данные для обновления
            context.user_data['last_name'] = last_name
            context.user_data['user_id'] = selected_user['id']
            context.user_data['user_name'] = selected_user['name']
            
            # Отправляем сообщения
            await send_long_message(update, stats_messages, get_refresh_keyboard(last_name, selected_user['id']))
        else:
            await status_msg.edit_text("❌ Не удалось загрузить статистику")
        
        # Clean up user state
        if user_id in user_states:
            del user_states[user_id]

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors."""
    logger.error(f"Exception while handling an update: {context.error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ Произошла ошибка при обработке запроса. Пожалуйста, попробуйте позже."
        )

def main():
    """Start the bot."""
    # Create Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_last_name))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    # Start the bot
    print("🤖 Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()