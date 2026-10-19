"""Recipe lifecycle: status changes (delete/publish) and listing own recipes."""
import asyncio
import logging
import re
import urllib.parse

from ..config import MC_API_DEFAULT_LANGUAGE, MC_API_WRITE_TIMEOUT_SECONDS
from .client import MCRequestError, _call_proxy_api
from . import analysis

logger = logging.getLogger(__name__)


async def _get_own_recipe(recipe_id: int, language: str, *, action: str) -> dict:
    """Fetch one of the user's own recipes, raising a clear error if the id is
    not one of theirs. A missing/foreign id comes back as HTTP 200 with an
    empty/null body rather than a 4xx, so guard explicitly."""
    current = await _call_proxy_api(
        f"api/v3/auth/user/recipes/{recipe_id}", method="GET", language=language
    )
    recipe = (current.get("data") or {}).get("recipe") or {}
    if not recipe:
        raise MCRequestError(
            f"Recipe {recipe_id} was not found among your recipes. Check the id "
            f"(it must be one of your own recipes) - nothing was {action}."
        )
    return recipe


async def _patch_recipe_status(recipe_id: int, status: str, language: str, *, action: str) -> None:
    """PATCH a recipe's status (the one-field lifecycle change the web app uses
    for publish/delete), turning API failures into actionable errors.

    Publish transitions are validated server-side: an incomplete recipe returns
    HTTP 400 (code 40004, "recipe data is not complete - <field>"); disallowed
    transitions return HTTP 409 (code 40002, "Not allowed to change status").
    """
    try:
        result = await _call_proxy_api(
            f"api/v3/auth/user/recipes/{recipe_id}", method="PATCH", language=language,
            payload={"status": status}, timeout=MC_API_WRITE_TIMEOUT_SECONDS,
        )
    except MCRequestError as exc:
        if "not complete" in str(exc):
            detail = re.search(r"not complete[^\"}]*", str(exc))
            raise MCRequestError(
                f"Cannot publish recipe {recipe_id}: "
                f"{detail.group(0) if detail else 'the recipe is not complete'}. "
                "Complete it first - it needs a cover photo, complexity, categories, "
                "a serving size, at least one ingredient, and a non-empty title on "
                "every step and ingredient group - then publish."
            ) from exc
        raise
    # The proxy returns HTTP 200 with a business status code; the web app treats
    # only code 0 as success, so mirror that instead of assuming 200 == done.
    if result.get("code") not in (0, None):
        raise MCRequestError(
            f"Recipe {recipe_id} could not be {action}: {result.get('message') or result}"
        )


async def _set_recipe_status(
    recipe_id: int, status: str, language: str, *, action: str
) -> dict:
    """Fetch a user recipe (validating ownership) and change its status,
    reporting the title and prior status. `action` only words error messages."""
    recipe = await _get_own_recipe(recipe_id, language, action=action)
    previous_status = recipe.get("status")
    title = recipe.get("title")
    await _patch_recipe_status(recipe_id, status, language, action=action)
    logger.info("Recipe %s (%r) %s: %s -> %s", recipe_id, title, action, previous_status, status)
    return {
        "recipe_id": recipe_id,
        "title": title,
        "previous_status": previous_status,
        "status": status,
    }


async def delete_recipe(recipe_id: int, language: str = MC_API_DEFAULT_LANGUAGE) -> dict:
    """Delete one of the logged-in user's own recipes (move it to trash).

    Requires a saved login session (run `uv run monsieur-cuisine-mcp-login`
    once beforehand) - raises auth.NotLoggedInError if none is found.

    This mirrors the "Delete" action in the web app's "My recipes" list ("..."
    menu): it sets the recipe's status to "trash", so it disappears from your
    recipe list. **The web app has no trash view and no restore button, so on the
    website this is effectively permanent.** It is technically a soft delete,
    though: the recipe data is kept server-side (status "trash") and can be
    brought back with restore_recipe. There is no hard-delete endpoint, so a
    trashed recipe stays in trash indefinitely. Works for the user's own recipes
    in any status; it cannot delete recipes that are not the user's own.

    The recipe is fetched first so the result can report what was deleted (and so
    a bad/again-deleted id fails with a clear error before any write happens).
    """
    recipe = await _get_own_recipe(recipe_id, language, action="deleted")
    previous_status = recipe.get("status")
    title = recipe.get("title")

    # A recipe in editorial review ("pending") cannot go straight to trash; the
    # backend only allows pending -> private-publish -> trash. Step via private
    # so delete works from any status.
    if previous_status == "pending":
        await _patch_recipe_status(recipe_id, "private-publish", language, action="deleted")
        await asyncio.sleep(2)  # brief spacing between writes (Akamai bot protection)

    await _patch_recipe_status(recipe_id, "trash", language, action="deleted")
    logger.info("Trashed recipe %s (%r, was %s)", recipe_id, title, previous_status)
    return {
        "recipe_id": recipe_id,
        "title": title,
        "previous_status": previous_status,
        "status": "trash",
        "deleted": True,
    }


async def restore_recipe(recipe_id: int, language: str = MC_API_DEFAULT_LANGUAGE) -> dict:
    """Restore a trashed recipe (undo delete_recipe), bringing it back as a DRAFT.

    Requires a saved login session (run `uv run monsieur-cuisine-mcp-login`
    once beforehand) - raises auth.NotLoggedInError if none is found.

    delete_recipe only sets a recipe's status to "trash"; the data is kept, so
    this can bring it back. The web app can't do this (it has no trash view), but
    the API allows trash -> draft. The recipe returns as a DRAFT regardless of
    what it was before (its pre-trash status isn't recoverable), so re-publish it
    with publish_recipe if it was published. Find trashed recipes with
    list_my_recipes(status="trash").

    Meaningful only on a trashed recipe. On an already-published recipe it fails
    (the backend won't move a live recipe back to draft); on one that is already
    a draft it's a harmless no-op.
    """
    return await _set_recipe_status(recipe_id, "draft", language, action="restored")


async def publish_recipe(
    recipe_id: int, private: bool = False, language: str = MC_API_DEFAULT_LANGUAGE
) -> dict:
    """Publish one of the logged-in user's own DRAFT recipes.

    Requires a saved login session (run `uv run monsieur-cuisine-mcp-login`
    once beforehand) - raises auth.NotLoggedInError if none is found.

    Two modes, mirroring the web app's publish dialog:
      - private=False (default): PUBLIC publish. Submits the recipe for editorial
        review (status "pending"); it becomes publicly visible only after the
        Monsieur Cuisine team approves it. In the web app this step requires
        confirming the recipe and its photo are your own original content -
        publishing here asserts the same.
      - private=True: PRIVATE publish (status "private-publish"). The recipe is
        published only to the user's own account, with no editorial review.

    Works both to publish a draft and to switch an already-published recipe
    between public and private (pending <-> private-publish are both allowed).

    The recipe must be complete or the server refuses to publish it (a clear
    "not complete" error is raised listing what is missing): it needs a cover
    photo, complexity, categories, a serving size, at least one ingredient, and
    a non-empty title on every step and ingredient group. Build the draft with
    create_recipe first, then publish it here.

    NOTE: a published recipe cannot be turned back into a draft - the platform
    forbids it (there is no "unpublish to draft"). To take a recipe down, either
    switch it to private (private=True) or remove it with delete_recipe.
    """
    report = analysis.analyze_recipe_details(await _get_own_recipe(recipe_id, language, action="published"))
    if report["issues"]:
        raise MCRequestError(
            f"Recipe {recipe_id} has blocking issues before publish: "
            f"{'; '.join(report['issues'])}. Fix them first (run analyze_recipe)."
        )
    status = "private-publish" if private else "pending"
    result = await _set_recipe_status(recipe_id, status, language, action="published")
    return {
        **result,
        "published": True,
        "visibility": "private" if private else "public",
        "in_review": not private,
        "analysis": report,
    }


# Statuses a user's own recipe can have. The "My recipes" list's default ("all")
# section shows every status except trashed (mirroring the web app's status
# switch); "approved" and "trash" are valid explicit filters but not in the
# default set. Filter by "trash" to find deleted recipes to restore_recipe.
_MY_RECIPE_ALL_STATUSES = ["draft", "pending", "private-publish", "publish", "rejected"]
_MY_RECIPE_VALID_STATUSES = {*_MY_RECIPE_ALL_STATUSES, "approved", "trash"}


def _summarize_my_recipe(rec: dict) -> dict:
    """Reduce a raw "My recipes" list entry to the fields worth surfacing."""
    thumb = (rec.get("thumbnail") or {}).get("landscapeMedia") or {}
    details = (rec.get("detailsImage") or {}).get("landscapeMedia") or {}
    complexity = rec.get("complexity")
    return {
        "id": rec.get("id"),
        "title": rec.get("title"),
        "status": rec.get("status"),
        "complexity": complexity.get("name") if isinstance(complexity, dict) else complexity,
        "categories": [
            c.get("name") for c in (rec.get("categories") or []) if isinstance(c, dict)
        ],
        "preparation_time": rec.get("preparationTime"),
        "ready_in_time": rec.get("readyInTime"),
        "image_url": thumb.get("url") or details.get("url") or None,
        "created_at": rec.get("createdAt"),
        "updated_at": rec.get("updatedAt"),
        "rating": rec.get("rating"),
    }


async def list_my_recipes(
    status: str | list[str] | None = None,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    sort_field: str = "title",
    sort_direction: str = "ASC",
    language: str = MC_API_DEFAULT_LANGUAGE,
) -> dict:
    """List the logged-in user's OWN recipes (the web app's "My recipes" page),
    across every status by default: drafts, in-review, published and rejected.

    Requires a saved login session (run `uv run monsieur-cuisine-mcp-login`
    once beforehand) - raises auth.NotLoggedInError if none is found. This is
    the user's own authored recipes, which is different from
    list_favorite_recipes (recipes the user bookmarked).

    Filters by status via `filters[status]`, exactly like the "My recipes"
    section tabs. `status` may be a single status or a list; when omitted it
    defaults to the "all" section (everything except trashed/deleted). Trashed
    recipes (deleted via delete_recipe) are never returned.
    """
    if status is None:
        statuses = list(_MY_RECIPE_ALL_STATUSES)
    else:
        statuses = [status] if isinstance(status, str) else list(status)
        invalid = [s for s in statuses if s not in _MY_RECIPE_VALID_STATUSES]
        if invalid:
            raise ValueError(
                f"Invalid recipe status {invalid}, expected any of "
                f"{sorted(_MY_RECIPE_VALID_STATUSES)}"
            )

    # Build the query the way the web app does: bracketed, unencoded array keys
    # (filters[status][]=draft&filters[status][]=pending&...). The endpoint is a
    # value inside the proxy body, not a URL the client re-encodes, so keep the
    # brackets literal and only percent-encode the free-text keyword.
    query = (
        f"size={page_size}&page={page}"
        f"&sortBy[0][field]={sort_field}&sortBy[0][direction]={sort_direction}"
    )
    for state in statuses:
        query += f"&filters[status][]={state}"
    if keyword:
        query += f"&q={urllib.parse.quote(keyword)}"

    data = await _call_proxy_api(
        f"api/v3/auth/user/recipes?{query}", method="GET", language=language
    )
    result = data.get("data", {})
    recipes = result.get("recipes", [])
    logger.debug(
        "Fetched %d/%s own recipes (page %s/%s, statuses=%s)",
        len(recipes), result.get("total"), result.get("currentPage"),
        result.get("totalPage"), statuses,
    )
    return {
        "total": result.get("total"),
        "total_pages": result.get("totalPage"),
        "page": result.get("currentPage"),
        "page_size": result.get("pageSize"),
        "count": len(recipes),
        "recipes": [_summarize_my_recipe(rec) for rec in recipes],
    }
