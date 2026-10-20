"""Import recipes from Cookidoo (Thermomix's official recipe platform).

Cookidoo recipes are already Thermomix recipes, so converting them to Monsieur
Cuisine Smart reuses the same Thermomix-notation -> device-program mapping as
the Paprika importer. Authentication uses the unofficial `cookidoo-api` package
(OAuth2 authorization code with PKCE, no client secret).

Credentials are read from environment variables (COOKIDOO_EMAIL,
COOKIDOO_PASSWORD, COOKIDOO_COUNTRY, COOKIDOO_LANGUAGE) or a .env file in the
project root, and the resulting auth token is cached in .cookidoo_token so
logins are not repeated on every call.
"""
import asyncio
import html
import os
import re
import sys
import tempfile
import urllib.request
from html.parser import HTMLParser

# aiohttp on Windows needs the selector event loop policy (see cookidoo-api docs).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import aiohttp
from dotenv import load_dotenv, set_key

from cookidoo_api import Cookidoo
from cookidoo_api import CookidooConfig
from cookidoo_api.helpers import get_localization_options

from ..config import MC_API_DEFAULT_LANGUAGE
from . import authoring, lifecycle, paprika
from .authoring import get_recipe_creation_options
from .client import MCRequestError

# Load .env from the project root (if present) so credentials/env config work
# without the caller exporting them.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENV_FILE = os.path.join(_PROJECT_ROOT, ".env")
_TOKEN_FILE = os.path.join(_PROJECT_ROOT, ".cookidoo_token")
load_dotenv(_ENV_FILE)


def _strip_html(text: str) -> str:
    """Strip HTML tags from a Cookidoo step's formattedText, leaving the
    readable instruction (including any '3 sec/speed 7' notation)."""
    if not text:
        return ""
    # Drop tags.
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self) -> str:
        return " ".join(self.parts)


def _html_to_text(text: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(text or "")
    except Exception:
        return _strip_html(text)
    return re.sub(r"\s+", " ", parser.get_text()).strip() or _strip_html(text)


def _recipe_id_from_ref(ref: str) -> str:
    """Accept either a bare id ('r59322') or a Cookidoo URL and return the id."""
    ref = (ref or "").strip()
    m = re.search(r"\b(r\d+)\b", ref)
    if m:
        return m.group(1)
    # Some URLs use the id as the last path segment without the 'r' prefix.
    m = re.search(r"/([A-Za-z0-9-]{4,})/?$", ref)
    if m:
        return m.group(1)
    raise ValueError(f"Could not extract a Cookidoo recipe id from {ref!r}.")


def _credentials() -> tuple[str | None, str | None, str, str]:
    email = os.environ.get("COOKIDOO_EMAIL")
    password = os.environ.get("COOKIDOO_PASSWORD")
    country = os.environ.get("COOKIDOO_COUNTRY", "gb")
    language = os.environ.get("COOKIDOO_LANGUAGE", "en-GB")
    return email, password, country, language


async def _login(
    email: str | None = None,
    password: str | None = None,
    country: str | None = None,
    language: str | None = None,
) -> Cookidoo:
    """Log in (or reuse a saved token) and return an authenticated Cookidoo
    client. Raises ValueError if credentials are missing."""
    env_email, env_password, env_country, env_language = _credentials()
    email = email or env_email
    password = password or env_password
    country = country or env_country
    language = language or env_language

    if not email or not password:
        raise ValueError(
            "Cookidoo credentials missing. Set COOKIDOO_EMAIL and "
            "COOKIDOO_PASSWORD (env vars or a .env file in the project root), "
            "or pass email/password to cookidoo_login."
        )

    jar = aiohttp.CookieJar(unsafe=True)
    session = aiohttp.ClientSession(cookie_jar=jar)
    localization = (await get_localization_options(country=country, language=language))[0]

    cookidoo = Cookidoo(
        session,
        cfg=CookidooConfig(
            email=email, password=password, localization=localization
        ),
    )

    cookidoo.on_auth_data_update = lambda _auth: cookidoo.save_token(_TOKEN_FILE)
    try:
        cookidoo.load_token(_TOKEN_FILE)
    except Exception:
        await cookidoo.login()
    return cookidoo


async def _fetch_recipe(recipe_ref: str, email=None, password=None, country=None, language=None):
    cookidoo = await _login(email, password, country, language)
    try:
        recipe_id = _recipe_id_from_ref(recipe_ref)
        details = await cookidoo.get_recipe_details(recipe_id)
        return cookidoo, details
    except Exception:
        await cookidoo.session.close()
        raise


def _map_nutrition(details) -> dict:
    """Extract calories/protein/carbohydrate/fat from Cookidoo nutrition groups.

    Cookidoo nutrition is per `quantity` (usually the serving size). We take
    the first group whose quantity matches the serving size, else the first
    group, and read the kcal/protein/fat/carbohydrates numbers."""
    calories = protein = carbohydrate = fat = None
    groups = details.nutrition_groups or []
    # Prefer the group matching the serving size.
    chosen = None
    for g in groups:
        for rn in g.recipe_nutritions:
            if rn.quantity == details.serving_size:
                chosen = g
                break
        if chosen:
            break
    if chosen is None and groups:
        chosen = groups[0]

    if chosen:
        for rn in chosen.recipe_nutritions:
            for n in rn.nutritions:
                t = (n.type or "").lower()
                val = n.number
                if t in ("kcal", "calories", "energy", "kcalorie"):
                    if n.unittype and "kj" in n.unittype.lower():
                        val = val / 4.184
                    if calories is None:
                        calories = val
                elif t in ("protein",):
                    if protein is None:
                        protein = val
                elif t in ("fat", "fats", "totalfat"):
                    if fat is None:
                        fat = val
                elif t in ("carbohydrates", "carbohydrate", "carbs", "kh", "kohlenhydrate"):
                    if carbohydrate is None:
                        carbohydrate = val
    return {"calories": calories, "protein": protein, "carbohydrate": carbohydrate, "fat": fat}


def _parse_ingredients(details) -> list[dict]:
    ingredients = []
    for ing in (details.ingredients or []):
        name = ing.name or ""
        if ing.preparation:
            name = f"{name}, {ing.preparation}".strip(", ")
        desc = (ing.description or "").strip()
        # description is like "100 g" or "100" or "".
        amount = ""
        unit = ""
        if desc:
            m = re.match(r"^\s*(\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?)\s*(.*)$", desc)
            if m:
                amount = m.group(1).replace(",", ".")
                unit = m.group(2).strip()
            else:
                amount = desc
        if not amount:
            amount = "1"
        ingredients.append({"name": name or desc or "ingredient", "amount": amount, "unit": unit})
    return ingredients


def _map_step(step, language: str) -> dict:
    text = _html_to_text(step.formatted_text or "")
    title = (step.title or "").strip()
    program, params = paprika._detect_program(text)
    out = {"text": text}
    if title:
        out["title"] = title
    if program:
        out["program"] = program
        out.update(params)
    return out


def _prepare_recipe(details, options: dict) -> dict:
    title = details.name
    mc_categories = options.get("categories") or []
    mc_levels = options.get("complexity_levels") or []

    difficulty_raw = (details.difficulty or "").strip().capitalize()
    complexity_id = paprika._map_complexity(difficulty_raw, mc_levels)

    category_names = [c.name for c in (details.categories or [])]
    category_ids = paprika._map_categories(category_names, title, mc_categories)

    prep_minutes = max(1, round((details.active_time or 0) / 60)) if details.active_time else None
    total_minutes = max(1, round((details.total_time or 0) / 60)) if details.total_time else None

    ingredients = _parse_ingredients(details)
    steps = [_map_step(s, MC_API_DEFAULT_LANGUAGE) for sg in (details.step_groups or []) for s in (sg.recipe_steps or [])]

    nutrition = _map_nutrition(details)

    # Download cover image.
    image_path = None
    img_url = details.image or details.thumbnail
    if img_url:
        try:
            directory = os.path.join(tempfile.gettempdir(), "cookidoo_import")
            os.makedirs(directory, exist_ok=True)
            safe = re.sub(r"[^\w-]", "_", str(details.id))
            path = os.path.join(directory, f"{safe}.jpg")
            urllib.request.urlretrieve(img_url, path)
            image_path = path
        except Exception:
            image_path = None

    servings = details.serving_size or 4

    description_parts = []
    if details.notes:
        description_parts.extend(details.notes)
    description = "\n".join(description_parts).strip()[:500] or None

    create_kwargs = {
        "title": title,
        "description": description,
        "complexity_id": complexity_id,
        "category_ids": category_ids,
        "servings_amount": servings,
        "servings_unit": "portions",
        "prep_time_minutes": prep_minutes,
        "ready_time_minutes": total_minutes,
        "calories": nutrition.get("calories"),
        "protein": nutrition.get("protein"),
        "carbohydrate": nutrition.get("carbohydrate"),
        "fat": nutrition.get("fat"),
        "ingredients": ingredients,
        "steps": steps,
        "image_path": image_path,
    }
    return {"create_kwargs": create_kwargs, "nutrition": nutrition}


async def cookidoo_login(
    email: str | None = None,
    password: str | None = None,
    country: str | None = None,
    language: str | None = None,
) -> dict:
    """Log in to Cookidoo (or validate a saved token) and cache the token.

    Credentials can be passed explicitly or set via COOKIDOO_EMAIL /
    COOKIDOO_PASSWORD (and optionally COOKIDOO_COUNTRY / COOKIDOO_LANGUAGE)
    environment variables or a .env file in the project root. The auth token is
    cached in .cookidoo_token so subsequent calls reuse it.
    """
    if email:
        os.environ["COOKIDOO_EMAIL"] = email
        set_key(_ENV_FILE, "COOKIDOO_EMAIL", email)
    if password:
        os.environ["COOKIDOO_PASSWORD"] = password
        set_key(_ENV_FILE, "COOKIDOO_PASSWORD", password)
    if country:
        os.environ["COOKIDOO_COUNTRY"] = country
        set_key(_ENV_FILE, "COOKIDOO_COUNTRY", country)
    if language:
        os.environ["COOKIDOO_LANGUAGE"] = language
        set_key(_ENV_FILE, "COOKIDOO_LANGUAGE", language)

    cookidoo = await _login(email, password, country, language)
    try:
        info = await cookidoo.get_user_info()
        await cookidoo.session.close()
        return {
            "logged_in": True,
            "email": getattr(info, "email", None) or email or _credentials()[0],
            "country": country or _credentials()[2],
            "language": language or _credentials()[3],
            "token_file": _TOKEN_FILE,
        }
    except Exception as exc:
        await cookidoo.session.close()
        raise RuntimeError(f"Cookidoo login failed: {exc}") from exc


async def cookidoo_status() -> dict:
    """Report whether Cookidoo credentials/token are available."""
    email, password, country, language = _credentials()
    token_exists = os.path.isfile(_TOKEN_FILE)
    return {
        "has_credentials": bool(email and password),
        "has_token": token_exists,
        "country": country,
        "language": language,
        "env_file": _ENV_FILE,
        "token_file": _TOKEN_FILE,
    }


async def get_cookidoo_recipe(
    recipe_ref: str,
    email: str | None = None,
    password: str | None = None,
    country: str | None = None,
    language: str | None = None,
    mc_language: str = MC_API_DEFAULT_LANGUAGE,
) -> dict:
    """Preview the MC conversion of ONE Cookidoo recipe, without creating
    anything. Accepts a recipe id ('r59322') or a full Cookidoo URL."""
    cookidoo, details = await _fetch_recipe(recipe_ref, email, password, country, language)
    try:
        options = await get_recipe_creation_options(mc_language)
        prepared = _prepare_recipe(details, options)
        return {
            "cookidoo_id": details.id,
            "cookidoo_name": details.name,
            "cookidoo_url": details.url,
            "cookidoo": {
                "servings": details.serving_size,
                "difficulty": details.difficulty,
                "categories": [c.name for c in (details.categories or [])],
                "active_time_minutes": round((details.active_time or 0) / 60),
                "total_time_minutes": round((details.total_time or 0) / 60),
                "nutrition": _map_nutrition(details),
                "notes": details.notes,
            },
            "converted": {
                "complexity_id": prepared["create_kwargs"]["complexity_id"],
                "category_ids": prepared["create_kwargs"]["category_ids"],
                "servings_amount": prepared["create_kwargs"]["servings_amount"],
                "prep_time_minutes": prepared["create_kwargs"]["prep_time_minutes"],
                "ready_time_minutes": prepared["create_kwargs"]["ready_time_minutes"],
                "nutrients": {
                    k: prepared["create_kwargs"][k]
                    for k in ("calories", "protein", "carbohydrate", "fat")
                    if prepared["create_kwargs"][k] is not None
                },
                "ingredients": prepared["create_kwargs"]["ingredients"],
                "steps": prepared["create_kwargs"]["steps"],
                "image_path": prepared["create_kwargs"]["image_path"],
            },
        }
    finally:
        await cookidoo.session.close()


async def import_cookidoo_recipe(
    recipe_ref: str,
    publish: bool = True,
    private: bool = True,
    email: str | None = None,
    password: str | None = None,
    country: str | None = None,
    language: str | None = None,
    mc_language: str = MC_API_DEFAULT_LANGUAGE,
) -> dict:
    """Import ONE Cookidoo recipe into the user's Monsieur Cuisine recipes.

    Fetches the recipe from Cookidoo (Thermomix settings included), converts
    it (ingredients, Thermomix -> MC Smart device programs, times, servings,
    nutrition, cover photo), creates the recipe and (publish=True) publishes it.
    """
    cookidoo, details = await _fetch_recipe(recipe_ref, email, password, country, language)
    try:
        options = await get_recipe_creation_options(mc_language)
        prepared = _prepare_recipe(details, options)
        created = await authoring.create_recipe(**prepared["create_kwargs"], language=mc_language)
        entry = {
            "cookidoo_id": details.id,
            "name": details.name,
            "recipe_id": created["recipe_id"],
            "status": created["status"],
            "steps": len(prepared["create_kwargs"]["steps"]),
            "ingredients": len(prepared["create_kwargs"]["ingredients"]),
            "image": bool(prepared["create_kwargs"]["image_path"]),
        }
        if publish:
            try:
                result = await lifecycle.publish_recipe(created["recipe_id"], private=private, language=mc_language)
                entry["status"] = result["status"]
                entry["published"] = True
            except (MCRequestError, ValueError) as exc:
                entry["published"] = False
                entry["publish_error"] = str(exc)[:400]
        return entry
    finally:
        await cookidoo.session.close()
