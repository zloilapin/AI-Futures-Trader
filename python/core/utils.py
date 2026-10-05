import os
from datetime import datetime, timezone, timedelta
from core.config import config

def get_msk_status() -> tuple[bool, str]:
    """
    Checks if current time in MSK (UTC+3) is within quiet rest window.
    Returns (is_rest_period, current_msk_formatted_time).
    """
    offset = config.TIMEZONE_OFFSET
    msk_tz = timezone(timedelta(hours=offset))
    now_msk = datetime.now(msk_tz)
    
    def parse_time(time_str: str, default_h: int, default_m: int):
        try:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            if h >= 24: h, m = 23, 59
            return h, m
        except:
            return default_h, default_m

    start_h, start_m = parse_time(config.REST_START_TIME, 19, 0)
    end_h, end_m = parse_time(config.REST_END_TIME, 7, 0)
    
    current_mins = now_msk.hour * 60 + now_msk.minute
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    
    if start_mins > end_mins:
        is_rest = current_mins >= start_mins or current_mins < end_mins
    else:
        is_rest = start_mins <= current_mins < end_mins
        
    return is_rest, now_msk.strftime("%H:%M:%S") + " MSK"

def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    for ch in ['_', '*', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, f'\\{ch}')
    return text
