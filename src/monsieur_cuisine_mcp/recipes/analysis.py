"""Pre-publish analysis for Monsieur Cuisine recipes.

Weighs ingredients, validates device steps, checks completeness.
"""


def analyze_recipe_details(recipe: dict) -> dict:
    """Return structured analysis of a recipe payload."""
    issues = []
    warnings = []
    serving = (recipe.get("servingSizes") or [{}])[0]
    steps = serving.get("steps") or []
    groups = serving.get("ingredientGroups") or []
    nutrients = recipe.get("nutrients") or []
    # Ingredient weighing counts
    gram_items = 0
    missing_amount = 0
    for g in groups:
        for ing in (g.get("ingredients") or []):
            amt = ing.get("amount")
            unit = str(ing.get("unit") or "").lower()
            if amt is None or str(amt).strip() == "":
                missing_amount += 1
            if "g" in unit or "gram" in unit:
                gram_items += 1
    if missing_amount:
        issues.append(f"{missing_amount} ingredient(s) missing quantity.")
    # Scale step check
    has_scale = any(s.get("mode") and s["mode"].get("type") == "scale" for s in steps)
    if gram_items > 0 and not has_scale:
        warnings.append("Gram-weighted ingredients present but no scale step found.")
    # Step titles / descriptions
    empty_titles = 0
    short_descs = 0
    device_steps = 0
    for i, s in enumerate(steps, 1):
        title = (s.get("title") or s.get("description") or "").strip()
        if not title:
            empty_titles += 1
        desc = s.get("description") or s.get("text") or ""
        if len(desc) < 10:
            short_descs += 1
        if s.get("mode"):
            device_steps += 1
    if empty_titles:
        issues.append(f"{empty_titles} step(s) missing title.")
    # Nutrients (list of {name, amount, unit})
    nutrients = recipe.get("nutrients") or []
    if isinstance(nutrients, list) and not any(
        isinstance(n, dict) and n.get("name") == "calories" for n in nutrients
    ):
        warnings.append("Nutrients (calories) not set.")
    # Image
    thumb = (recipe.get("thumbnail") or recipe.get("detailsImage") or {})
    if not (thumb.get("landscapeMedia") or thumb.get("url") or thumb.get("media")):
        issues.append("Cover photo missing.")
    # Time total
    prep = serving.get("preparationTime") or recipe.get("preparationTime") or 0
    ready = serving.get("readyInTime") or recipe.get("readyInTime") or 0
    return {
        "recipe_id": recipe.get("id"),
        "title": recipe.get("title"),
        "status": recipe.get("status"),
        "step_count": len(steps),
        "device_step_count": device_steps,
        "ingredient_groups": len(groups),
        "gram_ingredients": gram_items,
        "has_scale_step": has_scale,
        "prep_minutes": prep,
        "ready_minutes": ready,
        "issues": issues,
        "warnings": warnings,
        "complete": len(issues) == 0,
    }
