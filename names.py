"""Korean -> English names: dictionary for common words, romanization as a fallback."""
import json
import os
import re
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)  # persistent files live here
CACHE_FILE = DATA_DIR / "names.json"

# Longest first, applied before romanization
TOKENS = [
    ("디 올 뉴", "The All New"), ("더 올 뉴", "The All New"), ("디 뉴", "The New"), ("더 뉴", "The New"),
    ("올 뉴", "All New"), ("올뉴", "All New"), ("뉴", "New"), ("더", "The"),
    ("하이브리드", "Hybrid"), ("플러그인", "Plug-in"), ("일렉트릭", "Electric"), ("전기차", "EV"),
    ("페이스리프트", "Facelift"), ("풀체인지", "Full Change"), ("클래스", "Class"), ("시리즈", "Series"),
    ("그란쿠페", "Gran Coupe"), ("쿠페", "Coupe"), ("컨버터블", "Convertible"), ("왜건", "Wagon"),
    ("디젤", "Diesel"), ("가솔린", "Gasoline"), ("터보", "Turbo"), ("스포츠", "Sport"),
    ("프리미엄", "Premium"), ("스페셜", "Special"), ("에디션", "Edition"), ("택시", "Taxi"),
    ("노블레스", "Noblesse"), ("시그니처", "Signature"), ("프레스티지", "Prestige"), ("럭셔리", "Luxury"),
    ("익스클루시브", "Exclusive"), ("모던", "Modern"), ("스마트", "Smart"), ("인스퍼레이션", "Inspiration"),
    ("캘리그래피", "Calligraphy"), ("프레스티지", "Prestige"), ("가솔린", "Gasoline"),
]

_INIT = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_VOWEL = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we",
          "wi", "yu", "eu", "ui", "i"]
_FINAL = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p",
          "t", "t", "ng", "t", "t", "k", "t", "p", "t"]
_HANGUL = re.compile(r"[가-힣]+")


def has_hangul(text: str) -> bool:
    return bool(_HANGUL.search(text))


def romanize(text: str) -> str:
    def word(m: re.Match) -> str:
        out = ""
        for ch in m.group():
            i = ord(ch) - 0xAC00
            out += _INIT[i // 588] + _VOWEL[i % 588 // 28] + _FINAL[i % 28]
        return out.capitalize()

    return _HANGUL.sub(word, text)


def translate(text: str) -> str:
    """Generation / trim names: dictionary first, then romanize whatever Korean is left."""
    text = re.sub(r"(\d+)\s*세대", r"Gen \1", text)
    for ko, en in TOKENS:
        text = text.replace(ko, f" {en} ")
    return re.sub(r"-\s+", "-", re.sub(r"\s+", " ", romanize(text))).strip()


def generation(value: str, group_ko: str = "", group_en: str = "") -> str:
    """'더 뉴 그랜저 IG' + ('그랜저', 'Grandeur') -> 'The New Grandeur IG'."""
    if group_ko and group_en and group_ko in value:
        value = value.replace(group_ko, f" {group_en} ")
    return translate(value)


def load() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}


def save(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf8")
