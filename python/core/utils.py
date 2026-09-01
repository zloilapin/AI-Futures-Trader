import os
from datetime import datetime, timezone, timedelta
from core.config import config

def get_msk_status() -> tuple[bool, str]:
    """
    Checks if current time in MSK (UTC+3) is within quiet rest window (19:10 - 07:00 MSK).
    Returns (is_rest_period, current_msk_formatted_time).
    """
    offset = config.TIMEZONE_OFFSET
    msk_tz = timezone(timedelta(hours=offset))
    now_msk = datetime.now(msk_tz)
    
    start_parts = config.REST_START_TIME.split(":")
    end_parts = config.REST_END_TIME.split(":")
    
    start_mins = int(start_parts[0]) * 60 + int(start_parts[1])
    end_mins = int(end_parts[0]) * 60 + int(end_parts[1])
    cur_mins = now_msk.hour * 60 + now_msk.minute
    
    is_rest = cur_mins >= start_mins or cur_mins < end_mins
    return is_rest, now_msk.strftime("%H:%M:%S") + " MSK"

def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    for ch in ['_', '*', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, f'\\{ch}')
    return text
