"""Import recipes from a Paprika app export (.paprikarecipes.zip).

Paprika exports each recipe as one `.paprikarecipe` zip entry: a gzipped JSON
document with free-text `ingredients`/`directions` fields (Thermomix-style
recipes in most collections). This module parses that format and converts each
recipe into the structured payload create_recipe/publish_recipe expect:

  - ingredient lines like "30 g olive oil" are split into amount/unit/name
  - Thermomix step notation ("3 sec/speed 7", "5 min/120°C/speed 1",
    "212°F/reverse speed 1", "weigh 50 g") is mapped onto Monsieur Cuisine
    Smart device programs (customized / slow_cooking / roasting / steaming /
    kneading / scale)
  - prep/cook times, servings and the free-text nutritional_info block are
    parsed into numbers; the embedded base64 photo becomes a cover-image file
  - the Paprika categories and difficulty are mapped onto the
    language-specific MC category/complexity ids (fetched once per import via
    get_recipe_creation_options)

All three public helpers are exposed as MCP tools: list_paprika_recipes (what
is in the export), get_paprika_recipe (full conversion preview for one recipe,
no writes) and import_paprika_recipes (create + optionally publish, in bulk).
"""
import asyncio
import base64
import gzip
import io
import json
import logging
import os
import re
import tempfile
import zipfile
from collections import Counter

from ..config import MC_API_DEFAULT_LANGUAGE
from . import authoring, lifecycle
from .authoring import get_recipe_creation_options
from .client import MCRequestError

logger = logging.getLogger(__name__)

PAPRIKA_ENTRY_SUFFIX = ".paprikarecipe"


# --------------------------------------------------------------------------
# Loading the export
# --------------------------------------------------------------------------


def _load_paprika_recipes(zip_path: str) -> list[dict]:
    """Read every `.paprikarecipe` entry of a Paprika export archive.

    Each entry is a gzip-compressed JSON document (UTF-8). Raises FileNotFoundError
    or ValueError with a clear message when the path is not a Paprika export.
    """
    if not os.path.isfile(zip_path):
        raise FileNotFoundError(f"Paprika export not found: {zip_path}")
    recipes: list[dict] = []
    with zipfile.ZipFile(zip_path) as archive:
        names = [n for n in archive.namelist() if n.endswith(PAPRIKA_ENTRY_SUFFIX)]
        if not names:
            raise ValueError(
                f"No {PAPRIKA_ENTRY_SUFFIX} entries in {zip_path} - is this a "
                "Paprika export archive?"
            )
        for entry in sorted(names):
            raw = archive.read(entry)
            try:
                text = gzip.decompress(raw).decode("utf-8")
                recipes.append(json.loads(text))
            except Exception as exc:  # one corrupt entry must not sink the import
                logger.warning("Skipping unreadable Paprika entry %s: %s", entry, exc)
                recipes.append({"name": entry, "_parse_error": str(exc)})
    logger.info("Loaded %d Paprika recipes from %s", len(recipes), zip_path)
    return recipes


def list_paprika_recipes(zip_path: str) -> dict:
    """List the recipes inside a Paprika export archive (.paprikarecipes.zip).

    Read-only preview: one entry per recipe with its name, parsed servings,
    difficulty, Paprika categories and whether an embedded photo exists. Use
    get_paprika_recipe for the full converted payload of one recipe, or
    import_paprika_recipes to create/publish them on Monsieur Cuisine.
    """
    recipes = _load_paprika_recipes(zip_path)
    return {
        "total": len(recipes),
        "recipes": [
            {
                "index": i,
                "name": rec.get("name") or "(untitled)",
                "servings": rec.get("servings") or "",
                "difficulty": rec.get("difficulty") or "",
                "categories": rec.get("categories") or [],
                "prep_time": rec.get("prep_time") or "",
                "total_time": rec.get("total_time") or "",
                "has_photo": bool(rec.get("photo_data")),
                "parse_error": rec.get("_parse_error"),
            }
            for i, rec in enumerate(recipes, start=1)
        ],
    }


def _select_recipes(recipes: list[dict], names: list[str] | None) -> list[dict]:
    """Filter the export by `names` (case-insensitive substrings of the recipe
    name). Raises ValueError when a name matches nothing, so a typo fails fast
    instead of silently importing the wrong set."""
    if not names:
        return list(recipes)
    selected: list[dict] = []
    for wanted in names:
        matches = [
            r for r in recipes if wanted.lower() in (r.get("name") or "").lower()
        ]
        if not matches:
            available = ", ".join(sorted((r.get("name") or "?") for r in recipes))
            raise ValueError(
                f"No recipe matching {wanted!r} in the export. Available: {available}"
            )
        for m in matches:
            if m not in selected:
                selected.append(m)
    return selected


# --------------------------------------------------------------------------
# Field parsing: ingredients, servings, times, nutrients
# --------------------------------------------------------------------------

# Leading quantity: mixed number ("1 1/2"), fraction ("1/2"), plain number
# ("2", "1.5") or range ("1 - 2", normalized to "1-2").
_QUANTITY_RE = re.compile(
    r"^\s*(?P<amount>"
    r"\d+\s+\d+\s*/\s*\d+"                            # 1 1/2
    r"|\d+\s*/\s*\d+\s*[-–]\s*\d+\s*/\s*\d+"          # 1/2 - 1
    r"|\d+\s*/\s*\d+\s*[-–]\s*\d+(?:[.,]\d+)?"        # 1/2 - 1
    r"|\d+\s*/\s*\d+"                                 # 1/2
    r"|\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?" # 2 / 1.5 / 1 - 2
    r")\s*"
)

# Quantity words that read as a unit in "juice of 2 lemons" style lines.
_OF_QUANTITY_RE = re.compile(
    r"^\s*(?:juice|zest|peel|rind)\s+of\s+(?P<amount>\d+(?:[.,]\d+)?)\s+", re.I
)

# Words after a leading quantity that are real units ("30 g olive oil") rather
# than adjectives ("1 brown onion"). Deliberately a whitelist - guessing wrong
# would steal the first word of the ingredient name.
_KNOWN_UNITS = {
    "g", "gram", "grams", "kg", "mg", "ml", "l", "dl", "cl",
    "liter", "liters", "litre", "litres",
    "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
    "cup", "cups", "pinch", "pinches", "dash", "dashes",
    "clove", "cloves", "slice", "slices", "sprig", "sprigs",
    "can", "cans", "tin", "tins", "packet", "packets", "bunch", "bunches",
    "bag", "bags", "head", "heads", "stick", "sticks", "cube", "cubes",
    "wedge", "wedges", "handful", "handfuls", "piece", "pieces",
    "drop", "drops", "sheet", "sheets", "jar", "jars", "bottle", "bottles",
    "oz", "lb", "lbs", "pound", "pounds",
}

# Lines with no quantity at all ("Salt and pepper") that clearly mean "as much
# as you like" - given an amount the publish-completeness check accepts.
_TASTE_RE = re.compile(
    r"\bto taste\b|\bas needed\b|\bfor (?:serving|garnish|decoration)\b|"
    r"\bgarnish\b|\bsalt\b|\bpepper\b|\boil to\b",
    re.I,
)

# Typographic fraction characters ("⅛ tsp", "1½ eggs") - expanded to decimals
# so the quantity regex can read them. "1½" becomes "1.5" (single-digit integer
# plus sub-1 fraction concatenates correctly as a decimal).
_UNICODE_FRACTIONS = {
    "¼": "0.25", "½": "0.5", "¾": "0.75",
    "⅛": "0.125", "⅜": "0.375", "⅝": "0.625", "⅞": "0.875",
}


def _expand_fractions(text: str) -> str:
    for char, decimal in _UNICODE_FRACTIONS.items():
        if char in text:
            text = re.sub(rf"(\d){char}", lambda m, d=decimal: m.group(1) + d, text)
            text = text.replace(char, f" {decimal}")
    return text


def _normalize_amount(amount: str) -> str:
    """Tidy a parsed quantity: "1 - 2" -> "1-2", "1,5" -> "1.5", keep
    fractions ("1 1/2") readable."""
    amount = amount.strip()
    amount = re.sub(r"\s*[-–]\s*", "-", amount)
    amount = amount.replace(",", ".")
    amount = re.sub(r"(?<=\d)\.(?=\d+\s+\d+/)", ".", amount)  # no-op guard
    return re.sub(r"\s+", " ", amount)


def _parse_ingredient_line(line: str) -> dict | None:
    """Split one Paprika ingredient line into {"amount", "unit", "name"}.

    Handles "30 g olive oil", "1 - 2 garlic cloves, to taste", "juice of
    2 lemons" and bare quantities. When the line carries no number at all, the
    amount falls back to "to taste" for seasoning-style lines and "1"
    otherwise - every ingredient needs a non-empty amount to be publishable.
    """
    text = line.strip().lstrip("-•· ").strip()
    if not text:
        return None
    text = _expand_fractions(text)

    m = _OF_QUANTITY_RE.match(text)
    if m:
        return {"amount": _normalize_amount(m.group("amount")), "unit": "", "name": text}

    m = _QUANTITY_RE.match(text)
    if m and m.group("amount").strip():
        amount = _normalize_amount(m.group("amount"))
        rest = text[m.end():]
        first, _, tail = rest.partition(" ")
        if first.lower().rstrip(",") in _KNOWN_UNITS:
            unit = first.rstrip(",")
            name = tail.strip() or rest.strip()
        else:
            unit, name = "", rest.strip()
        return {"amount": amount, "unit": unit, "name": name or text}

    # No leading quantity - keep any number buried in the line ("... with 2 tbsp").
    m = re.search(r"\b(\d+(?:[.,]\d+)?)\b", text)
    if m:
        return {"amount": _normalize_amount(m.group(1)), "unit": "", "name": text}
    return {
        "amount": "to taste" if _TASTE_RE.search(text) else "1",
        "unit": "",
        "name": text,
    }


def _parse_ingredients(raw: str) -> list[dict]:
    """Parse a Paprika multi-line ingredients block into ingredient dicts."""
    ingredients = []
    for line in (raw or "").split("\n"):
        parsed = _parse_ingredient_line(line)
        if parsed and parsed["name"]:
            ingredients.append(parsed)
    return ingredients


def _parse_servings(raw: str) -> int:
    """First plausible integer of a messy servings string ("12 portions" ->
    12, "2 or 4 servings" -> 2, "0"/garbage -> 4)."""
    m = re.search(r"\b(\d{1,2})\b", str(raw or ""))
    if m:
        value = int(m.group(1))
        if 1 <= value <= 99:
            return value
    return 4


_TIME_VALUE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I
)


def _parse_minutes(raw: str) -> int | None:
    """Parse a free-text duration ("1 h", "1 hour 45 minutes", "15 hrs 50 mins",
    "10 mins", bare "10") into total minutes. Returns None when no duration
    can be read (the API then gets its required minimum of 1 minute)."""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw)
    total = 0.0
    found = False
    for value, unit in _TIME_VALUE_RE.findall(text):
        amount = float(value)
        unit = unit.lower()
        if unit.startswith("h"):
            total += amount * 60
        elif unit.startswith("m"):
            total += amount
        else:
            total += amount / 60
        found = True
    if not found:
        m = re.fullmatch(r"\s*(\d+)\s*", text)
        if m:
            total = float(m.group(1))
            found = True
    return max(1, round(total)) if found else None


def _nutrient_number(labels: list[str], text: str) -> float | None:
    """First number following any of `labels` at the start of a line, e.g.
    "Protein: 1.3 g" or "Protein 5.4g 11%" -> 1.3 / 5.4."""
    for label in labels:
        m = re.search(
            rf"(?:^|[\n;])\s*{label}\s*:?\s*(\d+(?:[.,]\d+)?)", text, re.I | re.M
        )
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def _parse_nutrition(raw: str) -> dict:
    """Parse Paprika's free-text nutritional_info block into whole-number
    nutrients. Best effort - formats vary wildly between recipe sites, and the
    import only fills the values it can actually read."""
    text = str(raw or "")
    if not text.strip():
        return {}
    out: dict = {}

    m = re.search(r"(\d+(?:\.\d+)?)\s*kcal", text, re.I)
    if m:
        out["calories"] = float(m.group(1))
    else:
        value = _nutrient_number(["calories?"], text)
        if value is not None:
            # A calories line may lead with kJ ("Calories: 408.5 kJ / ...").
            line = re.search(r"(?:^|[\n;])\s*calories?\s*:?\s*\d+(?:[.,]\d+)?[^\n]*", text, re.I | re.M)
            if line and "kj" in line.group(0).lower() and "kcal" not in line.group(0).lower():
                value = value / 4.184
            out["calories"] = value

    for key, labels in (
        ("protein", ["protein"]),
        ("carbohydrate", ["carbohydrates?", "carbs?"]),
        ("fat", ["fats?"]),
    ):
        value = _nutrient_number(labels, text)
        if value is not None:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# Step conversion: Thermomix free text -> MC Smart device programs
# --------------------------------------------------------------------------


def _split_step_texts(text: str) -> list[str]:
    """Split a directions block into one string per intended step.

    Splits on newlines first; long paragraphs further on sentence boundaries
    (". " before a capital) and on ", then"/"; then". A short line ending in
    ":" ("For the sauce:") is treated as a heading and glued onto the step
    that follows it."""
    chunks: list[str] = []
    pending_prefix = ""
    for para in re.split(r"\n+", text or ""):
        para = para.strip()
        if not para:
            continue
        if para.endswith(":") and len(para) < 60:
            pending_prefix = para
            continue
        if len(para) > 240:
            parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(])|(?<=[,;])\s+then\s+", para)
        else:
            parts = re.split(r"(?<=[,;])\s+then\s+", para)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if pending_prefix:
                part = f"{pending_prefix} {part}"
                pending_prefix = ""
            chunks.append(part)
    return chunks


_WEIGH_RE = re.compile(
    r"\bweigh\b|\bon(?:to)? the (?:kitchen )?scale\b|\buse the scale\b|"
    r"\bplace .* on(?:to)? the scale\b",
    re.I,
)
_GRAMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:g|gram|grams)\b", re.I)
_OUNCES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:oz|ounces?)\b", re.I)
_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(secs?|seconds?|mins?|minutes?)\b", re.I)
_TEMP_C_RE = re.compile(r"(\d{2,3})\s*°?\s*(?:c\b|celsius\b)", re.I)
_TEMP_F_RE = re.compile(r"(\d{2,3})\s*°?\s*(?:f\b|fahrenheit\b)", re.I)
_SPEED_RE = re.compile(r"speed\s*(\d+(?:\.\d+)?)", re.I)
_REVERSE_RE = re.compile(r"\breverse\b|counter-?clockwise", re.I)


_DIRECTIVE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:secs?|seconds?|mins?|minutes?)\s*/\s*"
    r"(?:\d{2,3}\s*°?\s*(?:C|F|celsius|fahrenheit)\s*/\s*)?"
    r"(?:speed\s*)?\d+(?:\.\d+)?",
    re.I,
)


def _detect_program(text: str) -> tuple[str | None, dict]:
    """Map one instruction sentence to an MC Smart device program.

    Returns (program, params) where params uses the create_recipe step keys
    (time_seconds, temperature_celsius, speed, weight_grams, reverse).
    Returns (None, {}) for plain text steps - guessing is preferred over
    inventing a program the sentence never asked for.

    Mapping rules (Thermomix notation -> MC Smart):
      - any "speed N" -> customized (carries temperature/time/reverse)
      - temperature + time, "oven/bake/roast" -> roasting
      - temperature + time <= 100C -> slow_cooking
      - hotter temperature + time without speed -> customized at speed 1
      - "steam" + time -> steaming; "knead/dough" + time -> kneading
      - "weigh ... N g" -> scale with that weight
    """
    lower = text.lower()

    # Weighing instruction - the scale program (the scale weighs in grams, so
    # ounce instructions are converted).
    if _WEIGH_RE.search(lower):
        grams = _GRAMS_RE.search(lower)
        if grams:
            return "scale", {"weight_grams": int(round(float(grams.group(1))))}
        ounces = _OUNCES_RE.search(lower)
        if ounces:
            return "scale", {"weight_grams": round(float(ounces.group(1)) * 28.3495)}

    # Prefer a contiguous Thermomix directive so we never mix numbers from two
    # directives that share one sentence ("chop 3 sec/speed 7, then sauté
    # 3 min/120°C/speed 1").
    directive_match = _DIRECTIVE_RE.search(lower)
    if directive_match:
        chunk = directive_match.group(0)
        time_seconds = None
        m = _TIME_RE.search(chunk)
        if m:
            value = float(m.group(1))
            time_seconds = int(round(value * (60 if m.group(2).lower().startswith("min") else 1)))
        temperature = None
        m = _TEMP_C_RE.search(chunk)
        if m:
            temperature = int(m.group(1))
        else:
            m = _TEMP_F_RE.search(chunk)
            if m:
                temperature = round((int(m.group(1)) - 32) * 5 / 9)
        if temperature is None and re.search(r"\bvaroma\b", chunk):
            temperature = 120
        m = _SPEED_RE.search(chunk)
        speed = float(m.group(1)) if m else None
        reverse = bool(_REVERSE_RE.search(lower))

        if speed is not None:
            params = {}
            if time_seconds is not None:
                params["time_seconds"] = time_seconds
            if temperature is not None:
                params["temperature_celsius"] = temperature
            params["speed"] = int(round(speed))
            if reverse:
                params["reverse"] = True
            return "customized", params
        if temperature is not None and time_seconds is not None:
            params = {"temperature_celsius": temperature, "time_seconds": time_seconds}
            if re.search(r"\boven\b|\bbake|\broast", lower):
                return "roasting", params
            if re.search(r"\bsteam", lower):
                return "steaming", {"time_seconds": time_seconds}
            if temperature <= 100:
                return "slow_cooking", params
            return "customized", {**params, "speed": 1}

    # Fallback: independent extraction (Varoma-only, steam-without-slash, etc.)
    time_seconds = None
    m = _TIME_RE.search(lower)
    if m:
        value = float(m.group(1))
        time_seconds = int(round(value * (60 if m.group(2).lower().startswith("min") else 1)))
    temperature = None
    m = _TEMP_C_RE.search(lower)
    if m:
        temperature = int(m.group(1))
    else:
        m = _TEMP_F_RE.search(lower)
        if m:
            temperature = round((int(m.group(1)) - 32) * 5 / 9)
    if temperature is None and re.search(r"\bvaroma\b", lower):
        temperature = 120
    m = _SPEED_RE.search(lower)
    speed = float(m.group(1)) if m else None
    reverse = bool(_REVERSE_RE.search(lower))

    if speed is not None:
        params = {}
        if time_seconds is not None:
            params["time_seconds"] = time_seconds
        if temperature is not None:
            params["temperature_celsius"] = temperature
        params["speed"] = int(round(speed))
        if reverse:
            params["reverse"] = True
        return "customized", params
    if temperature is not None and time_seconds is not None:
        params = {"temperature_celsius": temperature, "time_seconds": time_seconds}
        if re.search(r"\boven\b|\bbake|\broast", lower):
            return "roasting", params
        if re.search(r"\bsteam", lower):
            return "steaming", {"time_seconds": time_seconds}
        if temperature <= 100:
            return "slow_cooking", params
        return "customized", {**params, "speed": 1}
    if time_seconds is not None:
        if re.search(r"\bsteam", lower):
            return "steaming", {"time_seconds": time_seconds}
        if re.search(r"knead|dough", lower):
            return "kneading", {"time_seconds": time_seconds}

    return None, {}



def _map_directions_to_steps(text: str) -> list[dict]:
    """Convert a Paprika directions block into create_recipe step dicts
    ({"text", ...program fields}). One sentence/paragraph per step; the step
    title is left out so create_recipe fills the publishable "Step N" default."""
    steps = []
    for chunk in _split_step_texts(text):
        program, params = _detect_program(chunk)
        step = {"text": chunk}
        if program:
            step["program"] = program
            step.update(params)
        steps.append(step)
    return steps


# --------------------------------------------------------------------------
# Category / complexity mapping
# --------------------------------------------------------------------------

# Paprika category/title keywords -> MC category name patterns (first match
# per rule, up to _MAX_CATEGORIES ids in total).
_CATEGORY_KEYWORDS = [
    (r"soup|stew|broth|bisque|consomm", r"soup"),
    (r"dessert|sweet|cake|cookie|biscuit|pudding|chocolate|mousse|ice\s*cream|"
     r"tart|\bpie\b|brownie|macaroon|sorbet|custard|trifle", r"dessert|baking"),
    (r"bread|pastry|dough|scone|loaf|pizza|yeast|bun\b|rolls?\b", r"baking|bread"),
    (r"vegan", r"vegan"),
    (r"vegetarian|vege\b|meatless", r"vegetarian"),
    (r"salad", r"salad"),
    (r"sauce|paste|dip|chutney|jam|relish|condiment|extras?\b", r"sauce|paste|dip|spread|extra"),
    (r"breakfast|brunch|pancake|porridge|muesli|scramble|omelette|omelet", r"breakfast"),
    (r"drink|beverage|smoothie|juice|\btea\b|coffee|cocktail|lemonade", r"drink|beverage"),
    (r"snack|bite|finger food|nibble", r"snack"),
    (r"main|dinner|chicken|beef|pork|lamb|fish|salmon|turkey|meat|curry|"
     r"pasta|rice|noodle|casserole|grill|\broast\b|stew|bolognese|tagine", r"main|dish|course"),
]
_MAX_CATEGORIES = 3


def _map_categories(
    paprika_categories: list[str], title: str, mc_categories: list[dict]
) -> list[int]:
    """Map Paprika category labels (plus the title) onto MC category ids.

    First a direct case-insensitive name match (a Paprika label "Dessert "
    against the MC category "Desserts"), then the keyword table. Falls back to
    the main-course category so the recipe still satisfies the publish
    completeness rule (at least one category)."""
    chosen: list[int] = []
    hay_labels = " ".join(paprika_categories or []).lower()
    hay_all = f"{hay_labels} {(title or '').lower()}".strip()

    for cat in mc_categories:
        name = str(cat.get("name") or "").lower()
        if name and name.strip() in hay_labels and cat["id"] not in chosen:
            chosen.append(cat["id"])

    for keyword, mc_pattern in _CATEGORY_KEYWORDS:
        if len(chosen) >= _MAX_CATEGORIES:
            break
        if re.search(keyword, hay_all):
            for cat in mc_categories:
                name = str(cat.get("name") or "").lower()
                if re.search(mc_pattern, name) and cat["id"] not in chosen:
                    chosen.append(cat["id"])
                    break

    if not chosen:
        for cat in mc_categories:
            if re.search(r"main|dish|course", str(cat.get("name") or ""), re.I):
                chosen.append(cat["id"])
                break
    if not chosen and mc_categories:
        chosen.append(mc_categories[0]["id"])
    return chosen[:_MAX_CATEGORIES]


def _map_complexity(difficulty: str, levels: list[dict]) -> int | None:
    """Map a Paprika difficulty ("Easy"/"Medium") onto a language-specific MC
    complexity id; defaults to Easy (falling back to the lowest level)."""
    wanted = str(difficulty or "").strip().lower()
    for lvl in levels:
        if str(lvl.get("name") or "").strip().lower() == wanted:
            return lvl["id"]
    for fallback in ("easy", "leicht"):
        for lvl in levels:
            if str(lvl.get("name") or "").strip().lower() == fallback:
                return lvl["id"]
    return levels[0]["id"] if levels else None


def _pick_serving_unit(serving_units: list[str]) -> str:
    """Choose the serving unit: a portion-style unit if the locale has one,
    otherwise the first unit the locale offers."""
    for unit in serving_units:
        if "portion" in unit.lower():
            return unit
    return serving_units[0] if serving_units else "portions"


# --------------------------------------------------------------------------
# Photo extraction
# --------------------------------------------------------------------------


def _extract_photo(rec: dict) -> str | None:
    """Write the recipe's embedded base64 photo (photo_data) to a temp JPEG
    file and return its path, or None when there is no readable photo."""
    photo_data = rec.get("photo_data")
    if not photo_data:
        return None
    try:
        data = base64.b64decode(photo_data)
    except Exception as exc:
        logger.warning("Undecodable photo_data for %r: %s", rec.get("name"), exc)
        return None
    if data[:3] != b"\xff\xd8\xff":  # JPEG magic - the upload accepts JPEG/PNG/WebP
        logger.warning("photo_data for %r is not JPEG, skipping", rec.get("name"))
        return None
    directory = os.path.join(tempfile.gettempdir(), "paprika_import")
    os.makedirs(directory, exist_ok=True)
    uid = re.sub(r"[^\w-]", "_", str(rec.get("uid") or rec.get("name") or "recipe"))
    path = os.path.join(directory, f"{uid}.jpg")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


# --------------------------------------------------------------------------
# Conversion of one recipe
# --------------------------------------------------------------------------


def _prepare_recipe(rec: dict, options: dict) -> dict:
    """Convert one parsed Paprika recipe into create_recipe keyword arguments.

    Returns {"create_kwargs": {...}, "program_counts": {...}} - a pure function
    (plus writing the temp photo file), so get_paprika_recipe can preview the
    exact payload import_paprika_recipes would send."""
    title = rec.get("name") or "(untitled)"
    categories = _map_categories(
        rec.get("categories") or [], title, options.get("categories") or []
    )
    nutrition = _parse_nutrition(rec.get("nutritional_info"))
    prep = _parse_minutes(rec.get("prep_time"))
    total = _parse_minutes(rec.get("total_time"))
    cook = _parse_minutes(rec.get("cook_time"))
    if total is None and prep is not None and cook is not None:
        total = prep + cook

    steps = _map_directions_to_steps(rec.get("directions") or "")
    program_counts = Counter(step.get("program") for step in steps if step.get("program"))

    notes = (rec.get("notes") or "").strip()
    description = notes[:500] if notes else None

    create_kwargs = {
        "title": title,
        "description": description,
        "complexity_id": _map_complexity(rec.get("difficulty"), options.get("complexity_levels") or []),
        "category_ids": categories,
        "servings_amount": _parse_servings(rec.get("servings")),
        "servings_unit": _pick_serving_unit(options.get("serving_units") or []),
        "prep_time_minutes": prep,
        "ready_time_minutes": total,
        "calories": nutrition.get("calories"),
        "protein": nutrition.get("protein"),
        "carbohydrate": nutrition.get("carbohydrate"),
        "fat": nutrition.get("fat"),
        "ingredients": _parse_ingredients(rec.get("ingredients") or ""),
        "steps": steps,
        "image_path": _extract_photo(rec),
    }
    return {"create_kwargs": create_kwargs, "program_counts": dict(program_counts)}


async def get_paprika_recipe(
    zip_path: str, name: str, language: str = MC_API_DEFAULT_LANGUAGE
) -> dict:
    """Preview the full MC conversion of ONE recipe from a Paprika export,
    without creating anything.

    Returns the parsed Paprika fields (servings, times, nutrition, categories,
    source) plus the converted payload: split ingredients (amount/unit/name),
    steps with detected device programs (customized/slow_cooking/scale/...),
    mapped MC category ids and complexity id, and the extracted cover-image
    file path. Use this to review a tricky conversion before importing; plain
    import_paprika_recipes does the same automatically for every recipe.
    """
    recipes = _load_paprika_recipes(zip_path)
    matches = [r for r in recipes if name.lower() in (r.get("name") or "").lower()]
    if not matches:
        raise ValueError(
            f"No recipe matching {name!r} in the export. Use list_paprika_recipes "
            "to see the available names."
        )
    rec = matches[0]
    if rec.get("_parse_error"):
        raise ValueError(f"Recipe entry {rec.get('name')!r} is unreadable: {rec['_parse_error']}")
    options = await get_recipe_creation_options(language)
    prepared = _prepare_recipe(rec, options)
    return {
        "name": rec.get("name"),
        "paprika": {
            "servings": rec.get("servings"),
            "difficulty": rec.get("difficulty"),
            "categories": rec.get("categories") or [],
            "prep_time": rec.get("prep_time"),
            "cook_time": rec.get("cook_time"),
            "total_time": rec.get("total_time"),
            "nutritional_info": rec.get("nutritional_info"),
            "source": rec.get("source"),
            "source_url": rec.get("source_url"),
        },
        "converted": {
            "complexity_id": prepared["create_kwargs"]["complexity_id"],
            "category_ids": prepared["create_kwargs"]["category_ids"],
            "servings_amount": prepared["create_kwargs"]["servings_amount"],
            "servings_unit": prepared["create_kwargs"]["servings_unit"],
            "prep_time_minutes": prepared["create_kwargs"]["prep_time_minutes"],
            "ready_time_minutes": prepared["create_kwargs"]["ready_time_minutes"],
            "nutrients": {
                k: prepared["create_kwargs"][k]
                for k in ("calories", "protein", "carbohydrate", "fat")
                if prepared["create_kwargs"][k] is not None
            },
            "ingredients": prepared["create_kwargs"]["ingredients"],
            "steps": prepared["create_kwargs"]["steps"],
            "device_programs": prepared["program_counts"],
            "image_path": prepared["create_kwargs"]["image_path"],
        },
    }


# --------------------------------------------------------------------------
# Bulk import
# --------------------------------------------------------------------------


async def import_paprika_recipes(
    zip_path: str,
    names: list[str] | None = None,
    publish: bool = True,
    private: bool = True,
    skip_existing: bool = True,
    limit: int | None = None,
    language: str = MC_API_DEFAULT_LANGUAGE,
) -> dict:
    """Import recipes from a Paprika export into the user's Monsieur Cuisine
    recipes, converting Thermomix free text to MC Smart device programs.

    For each selected recipe this creates a draft (ingredients split, steps
    mapped to device programs, times/servings/nutrition parsed, embedded photo
    uploaded as cover image, categories/complexity mapped) and - with
    publish=True (default) - publishes it right away (private=True by default,
    matching an import of the user's own collection; no editorial review).

    Args:
        zip_path: Path to the .paprikarecipes.zip export file.
        names: Optional list of recipe-name substrings to import only those
            recipes (e.g. ["chicken", "soup"]); omit to import everything.
        publish: Publish each imported recipe (default True).
        private: Publish privately to the user's own account (default True).
        skip_existing: Skip recipes whose title already exists among the
            user's own recipes, so a re-run never creates duplicates.
        limit: Optional cap on the number of recipes imported in this call -
            useful to work through a large export in batches.
        language: Locale, e.g. "en-US".

    Returns a per-recipe summary: created recipe_id, status, step/ingredient
    counts, detected device programs, and any publish error (a recipe that
    fails the publish-completeness check stays as a draft for fixing).
    """
    recipes = _load_paprika_recipes(zip_path)
    selected = _select_recipes(recipes, names)
    unreadable = [r for r in selected if r.get("_parse_error")]
    selected = [r for r in selected if not r.get("_parse_error")]
    if limit is not None:
        selected = selected[: max(0, limit)]

    options = await get_recipe_creation_options(language)

    existing_titles: set[str] = set()
    if skip_existing:
        page = 1
        while True:
            listing = await lifecycle.list_my_recipes(page=page, page_size=100, language=language)
            for item in listing.get("recipes") or []:
                existing_titles.add(str(item.get("title") or "").strip().lower())
            if page >= (listing.get("total_pages") or 1):
                break
            page += 1

    results: list[dict] = []
    imported = published = skipped = 0
    for rec in selected:
        title = rec.get("name") or "(untitled)"
        if skip_existing and title.strip().lower() in existing_titles:
            skipped += 1
            results.append({"name": title, "skipped": "already among your recipes"})
            continue
        try:
            prepared = _prepare_recipe(rec, options)
            created = await authoring.create_recipe(
                **prepared["create_kwargs"], language=language
            )
            entry: dict = {
                "name": title,
                "recipe_id": created["recipe_id"],
                "status": created["status"],
                "steps": len(prepared["create_kwargs"]["steps"]),
                "ingredients": len(prepared["create_kwargs"]["ingredients"]),
                "device_programs": prepared["program_counts"],
                "image": bool(prepared["create_kwargs"]["image_path"]),
            }
            imported += 1
            if publish:
                try:
                    result = await lifecycle.publish_recipe(
                        created["recipe_id"], private=private, language=language
                    )
                    entry["status"] = result["status"]
                    entry["published"] = True
                    published += 1
                except (MCRequestError, ValueError) as exc:
                    # Stays a draft - surfaced so it can be fixed and published.
                    entry["published"] = False
                    entry["publish_error"] = str(exc)[:400]
            results.append(entry)
        except Exception as exc:  # keep going: one bad recipe must not stop the batch
            logger.exception("Import of %r failed", title)
            results.append({"name": title, "error": f"{type(exc).__name__}: {exc}"[:400]})
        # Small pause between recipes (several writes each; Akamai bot protection).
        await asyncio.sleep(1.5)

    return {
        "total_in_export": len(recipes),
        "selected": len(selected) + len(unreadable),
        "imported": imported,
        "published": published,
        "skipped_existing": skipped,
        "failed": len(results) - imported - skipped + len(unreadable),
        "results": results,
        "unreadable": [{"name": r.get("name"), "error": r.get("_parse_error")} for r in unreadable],
    }
