"""Matching engine for validating Google Maps search candidates against target cafe records."""

from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import re
from typing import Optional, List, Tuple
from urllib.parse import urlparse

STOP_WORDS = {
    'کافه', 'رستوران', 'کافی', 'شاپ', 'قهوه', 'بار', 'لانژ', 'فست', 'فود', 'عمارت', 'خانه', 'سرای',
    'cafe', 'café', 'restaurant', 'coffee', 'shop', 'bar', 'lounge', 'fast', 'food', 'bakery',
    'شعبه', 'ایرانی', 'سنتی', 'مدرن', 'اصلی', 'مرکزی', 'بین', 'الملل', 'بین‌المللی',
    'تهران', 'اصفهان', 'مشهد', 'شیراز', 'تبریز', 'اهواز', 'کرج', 'قم',
    'رشت', 'کرمانشاه', 'ارومیه', 'زاهدان', 'یزد', 'قزوین', 'خرمشهر', 'آبادان',
    'ساری', 'گرگان', 'همدان', 'بندرعباس', 'بوشهر', 'زنجان', 'سنندج', 'کیش'
}

PERSIAN_TO_LATIN = {
    'آ': 'a', 'ا': 'a', 'ب': 'b', 'پ': 'p', 'ت': 't', 'ث': 's',
    'ج': 'j', 'چ': 'ch', 'ح': 'h', 'خ': 'kh', 'د': 'd', 'ذ': 'z',
    'ر': 'r', 'ز': 'z', 'ژ': 'zh', 'س': 's', 'ش': 'sh', 'ص': 's',
    'ض': 'z', 'ط': 't', 'ظ': 'z', 'ع': 'a', 'غ': 'gh', 'ف': 'f',
    'ق': 'gh', 'ک': 'k', 'گ': 'g', 'ل': 'l', 'م': 'm', 'ن': 'n',
    'و': 'o', 'ه': 'h', 'ی': 'i', 'ي': 'i', 'ك': 'k', 'ة': 'h',
    'ء': '', 'ئ': 'i', 'ؤ': 'o'
}

COMMON_TRANSLATIONS = {
    'مستر': 'mr', 'دوباره': 'dobiro', 'ناین': 'nine', 'بل': 'bell',
    'بیک': 'bake', 'پرسنت': 'percent', 'آیبیک': 'ibake', 'آواکو': 'avako',
    'بناک': 'bonak', 'آویژه': 'avizhe', 'سلوبون': 'celebon', 'گستو': 'gusto',
    'اگومان': 'agoman', 'کارزین': 'karzin', 'عباس': 'abbas', 'اقا': 'agha',
    'میلاد': 'milad', 'دنیز': 'deniz', 'روژه': 'rojeh', 'ویش': 'wish',
    'لیار': 'leymer', 'دیگر': 'andorra', 'نیلی': 'nili', 'پروچلو': 'prochelo'
}


@dataclass
class MatchResult:
    is_match: bool
    composite_score: float
    distance_meters: float
    name_score: float
    phone_matched: bool
    web_matched: bool
    rejection_reason: Optional[str] = None


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates haversine distance in meters between two coordinates."""
    if None in (lat1, lon1, lat2, lon2):
        return float('inf')
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (ValueError, TypeError):
        return float('inf')

    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return R * c


def normalize_text(text: str) -> str:
    """Normalize Persian/Arabic characters, digits, and remove diacritics."""
    if not text:
        return ""
    t = text.lower()
    t = t.replace('ي', 'ی').replace('ك', 'ک').replace('ة', 'ه').replace('ۀ', 'ه')
    t = t.replace('آ', 'ا').replace('أ', 'ا').replace('إ', 'ا').replace('ؤ', 'و').replace('ئ', 'ی')
    t = re.sub(r'[\u064B-\u065F\u0670]', '', t)
    digits_map = {
        '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4', '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
        '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9'
    }
    for k, v in digits_map.items():
        t = t.replace(k, v)
    t = re.sub(r'[\(\)\[\]\{\}\-_,\.\+:;/\&\\\|\'\"`!?]', ' ', t)
    return ' '.join(t.split())


def clean_tokens(text: str) -> List[str]:
    norm = normalize_text(text)
    tokens = norm.split()
    filtered = [w for w in tokens if w not in STOP_WORDS]
    return filtered if filtered else tokens


def to_phonetic_latin(tokens: List[str]) -> List[str]:
    latin_tokens = []
    for tok in tokens:
        if tok in COMMON_TRANSLATIONS:
            latin_tokens.append(COMMON_TRANSLATIONS[tok])
            continue
        if all(ord(c) < 128 for c in tok):
            latin_tokens.append(tok)
            continue
        res = ''.join(PERSIAN_TO_LATIN.get(c, c) for c in tok)
        res = re.sub(r'[^a-z0-9]', '', res)
        res = re.sub(r'(.)\1+', r'\1', res)
        if res:
            latin_tokens.append(res)
    return latin_tokens


def compute_name_similarity(name1: str, name2: str) -> float:
    """Computes similarity between two venue names using direct, joined, and phonetic matching."""
    if not name1 or not name2:
        return 0.0

    tok1 = clean_tokens(name1)
    tok2 = clean_tokens(name2)

    # 1. Direct Persian text ratio (with spaces)
    s1 = ' '.join(tok1)
    s2 = ' '.join(tok2)
    direct_ratio = SequenceMatcher(None, s1, s2).ratio()

    # 2. Joined Persian text ratio (handles compound words like پروچلو vs پرو چلو)
    joined_s1 = ''.join(tok1)
    joined_s2 = ''.join(tok2)
    joined_ratio = SequenceMatcher(None, joined_s1, joined_s2).ratio()

    # 3. Phonetic Latin ratio
    lat1 = to_phonetic_latin(tok1)
    lat2 = to_phonetic_latin(tok2)
    lat_s1 = ''.join(lat1)
    lat_s2 = ''.join(lat2)
    phonetic_ratio = SequenceMatcher(None, lat_s1, lat_s2).ratio() if (lat_s1 and lat_s2) else 0.0

    # 4. Token containment
    set1, set2 = set(lat1), set(lat2)
    if set1 and set2:
        overlap = len(set1 & set2) / min(len(set1), len(set2))
        containment = 1.0 if (set1.issubset(set2) or set2.issubset(set1)) else overlap
    else:
        containment = 0.0

    return round(max(direct_ratio, joined_ratio, phonetic_ratio, containment * 0.9), 2)


def evaluate_candidate(
    target_name: str,
    target_lat: float,
    target_lng: float,
    candidate_title: str,
    candidate_lat: Optional[float],
    candidate_lng: Optional[float],
    max_radius: float = 300.0,
    match_threshold: float = 0.75
) -> MatchResult:
    """Evaluates whether a candidate Google Maps place is a match for the target cafe."""
    name_score = compute_name_similarity(target_name, candidate_title)

    if candidate_lat is not None and candidate_lng is not None:
        dist = haversine_distance(target_lat, target_lng, candidate_lat, candidate_lng)
    else:
        dist = float('inf')

    # Distance veto: hard reject if candidate is further than max_radius
    if dist > max_radius:
        return MatchResult(
            is_match=False,
            composite_score=0.0,
            distance_meters=dist,
            name_score=name_score,
            phone_matched=False,
            web_matched=False,
            rejection_reason=f"Exceeded distance cutoff ({round(dist, 1)}m > {max_radius}m)"
        )

    # Distance score: 1.0 if <= 30m, decaying to 0.5 at max_radius
    if dist <= 30.0:
        dist_score = 1.0
    else:
        dist_score = 1.0 - 0.5 * ((dist - 30.0) / (max_radius - 30.0))

    # Composite score: 60% name similarity + 40% proximity
    composite_score = (0.60 * name_score) + (0.40 * dist_score)

    is_match = (composite_score >= match_threshold) or (name_score >= 0.85 and dist <= max_radius)

    return MatchResult(
        is_match=is_match,
        composite_score=round(composite_score, 2),
        distance_meters=dist,
        name_score=name_score,
        phone_matched=False,
        web_matched=False,
        rejection_reason=None if is_match else f"Score {round(composite_score, 2)} below threshold {match_threshold}"
    )
