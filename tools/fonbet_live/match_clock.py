"""Conservative sports clock normalization; no inference of finish from absence."""
import re


def api_period(status):
    return {'1H':'regulation_1h','2H':'regulation_2h','ET':'extra_time',
            'BT':'extra_time','P':'shootout','FT':'finished','AET':'finished',
            'PEN':'finished'}.get(status,'unknown')


def fonbet_clock(clock, label):
    text=str(label or '').casefold().strip()
    match=re.fullmatch(r"(\d{1,3})(?:\s*\+\s*(\d{1,2}))?(?::[0-5]\d)?[′'’]?",str(clock or '').strip())
    minute=int(match[1]) if match else None
    extra=int(match[2]) if match and match[2] else None
    if re.search(r'отмен[её]н',text):return 'CANC',minute,extra,'unknown'
    if re.search(r'прерван',text):return 'ABD',minute,extra,'unknown'
    if re.search(r'окончен|заверш',text):return 'FT',minute,extra,'finished'
    if re.search(r'пенальти',text):return 'P',minute,extra,'shootout'
    if re.search(r'доп|экстра',text):return 'ET',minute,extra,'extra_time'
    if re.search(r'перерыв',text):return 'HT',minute,extra,'unknown'
    # Explicit period labels prevent 45+N and ET from being guessed as 2H.
    second=bool(re.search(r'2\s*(?:[-–]?й\s*)?тайм|второй',text))
    first=bool(re.search(r'1\s*(?:[-–]?й\s*)?тайм|первый',text))
    if minute is not None:
        if second and 45<=minute<=90:return '2H',minute,extra,'regulation_2h'
        if first and minute<=45:return '1H',minute,extra,'regulation_1h'
        if not text and extra is None:
            if 46<=minute<=90:return '2H',minute,extra,'regulation_2h'
            if minute<=45:return '1H',minute,extra,'regulation_1h'
    return 'unknown',minute,extra,'unknown'
