"""Recipe operations for the Monsieur Cuisine MCP server.

Split into cohesive modules (client, devices, search, favorites, ingredients,
nutrients, media, payloads, lifecycle, authoring). This re-exports the public
functions the MCP tools call, so `from . import recipes; recipes.create_recipe`
keeps working unchanged.
"""
from .authoring import create_recipe, edit_recipe, get_recipe_creation_options
from .client import MCRequestError
from .favorites import (
    add_favorite_recipe,
    list_favorite_recipes,
    remove_favorite_recipe,
)
from .ingredients import search_ingredients
from .lifecycle import (
    delete_recipe,
    list_my_recipes,
    publish_recipe,
    restore_recipe,
)
from .paprika import (
    get_paprika_recipe,
    import_paprika_recipes,
    list_paprika_recipes,
)
from .search import get_recipe, get_recipe_filters, search_recipes

__all__ = [
    "MCRequestError",
    "add_favorite_recipe",
    "create_recipe",
    "delete_recipe",
    "edit_recipe",
    "get_paprika_recipe",
    "get_recipe",
    "get_recipe_creation_options",
    "get_recipe_filters",
    "import_paprika_recipes",
    "list_paprika_recipes",
    "list_favorite_recipes",
    "list_my_recipes",
    "publish_recipe",
    "remove_favorite_recipe",
    "restore_recipe",
    "search_ingredients",
    "search_recipes",
]
