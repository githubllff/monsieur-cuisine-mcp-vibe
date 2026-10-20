# Unofficial Monsieur Cuisine MCP

An unofficial [MCP](https://modelcontextprotocol.io/) server (built with
[FastMCP](https://github.com/jlowin/fastmcp)) that lets an AI assistant search,
read, **create**, edit, and publish recipes on the Monsieur Cuisine service
(Lidl's Monsieur Cuisine / LidlMix) using your own account.

> [!WARNING]
> **This is a vibe-coded prototype.** The whole thing was built conversationally,
> end to end. It works and it's been tested against a real account — but treat it
> as a proof-of-concept, not production-grade code. There are no unit tests, the
> API is reverse-engineered from the web app (so it can break any time Lidl
> changes something), and every "authoring" tool writes to your **real** account.
> Have fun with it, but don't build anything important on top of it.

> [!IMPORTANT]
> **Monsieur Cuisine Smart (MC3.0) only.** The recipe-authoring features (device
> programs and automatic step modes) are built and tested exclusively for the
> Monsieur Cuisine **Smart** (MC3.0). Other models expose a different, smaller
> program set that isn't supported here, so creating/editing recipes for them may
> fail or produce wrong steps.

## Disclaimer

Unofficial and not affiliated with, endorsed by, or supported by Lidl, Monsieur
Cuisine, or TecPal. "Monsieur Cuisine" and "Lidl" are trademarks of their
respective owners. Use at your own risk, and be nice to the servers.

> [!CAUTION]
> **You are responsible for complying with Monsieur Cuisine's Terms of Service.**
> This MCP was built for **personal use** — to get more enjoyment out of your own
> kitchen robot — **not** for disrupting the service or infringing anyone's
> intellectual property. Don't hammer the servers, don't scrape or redistribute
> content that isn't yours, and don't publish recipes or photos you don't own the
> rights to. How you use it is on you.

---

## What it does

Monsieur Cuisine has no official API. This server talks to the same endpoints the
web app uses — the public recipe API, plus the site's session-authenticated
proxy for your own recipes and favorites — and exposes them as clean MCP tools.
Point any MCP-capable assistant at it and you can say things like *"build me a
450-kcal high-protein lunch for two and save it privately"* and it'll actually do
it.

## Cool things to try

These are just natural-language prompts to your assistant — it picks the tools.

**🎯 Hit an exact calorie & macro target.**
> "Create a private recipe for a high-protein lunch bowl at about 450 kcal with
> 40 g protein, 30 g carbs and 15 g fat, 2 servings. Add realistic ingredient
> amounts and step-by-step Monsieur Cuisine smart instructions, then publish it
> privately."

**🧊 Cook from whatever's in the fridge.**
> "I've got chicken thighs, a zucchini, half an onion, garlic and some rice.
> Find recipes I can make on my Monsieur Cuisine — and if nothing fits, draft a
> new one that uses them and set the device programs for each step."

**⚖️ Rescale servings.**
> "Take my 'Zupa jarzynowa' recipe and change it from 3 servings to 6 — scale the
> ingredient amounts accordingly and bump the times if it needs it."

**🥗 Nutrition-tune an existing recipe.**
> "Edit my pancake recipe to bring it down to ~300 kcal per serving and raise
> protein to 20 g, keeping everything else the same."

**📸 Recipe with a cover photo, ready to publish.**
> "Create a draft for oatmeal with berries, attach `~/photos/oatmeal.jpg` as the
> cover, fill in nutrients and steps, then publish it privately."

**🧹 Tidy up.**
> "List all my recipes, publish the two soup drafts privately, and delete the
> empty test ones."

## Requirements

- **Python ≥ 3.10** (developed on 3.12)
- [**uv**](https://github.com/astral-sh/uv) for dependency management
- A real **Monsieur Cuisine / Lidl account** (login happens in a real browser)
- A **Monsieur Cuisine Smart (MC3.0)** — the recipe-authoring features target
  this model only
- Dependencies (installed via `uv sync`): `fastmcp`, `httpx`, `playwright`

## Setup

```bash
# 1. Install dependencies
uv sync

# 2. Install the browser used for the one-time login (once)
uv run playwright install chromium

# 3. Log in — a real browser window opens; sign in normally (CAPTCHA/2FA included)
uv run monsieur-cuisine-mcp-login
```

Lidl's login (`accounts.lidl.com`) is interactive and CAPTCHA-protected, so
there's no headless login — you log in **manually** in the browser window that
opens. Once the session is established, the cookies are saved to
`~/.monsieur-cuisine-mcp/storage_state.json` and reused until they expire.
Re-run the login command whenever the session lapses.

The login uses your installed **Google Chrome** when it's available (falling
back to the bundled Chromium) and keeps its own browser profile in
`~/.monsieur-cuisine-mcp/browser-profile`, so repeat logins look like the same
returning device. If a login fails, it writes a timestamped trace — navigations,
request statuses, console errors — to `~/.monsieur-cuisine-mcp/last-login.log`.
No passwords and no request/response bodies are ever recorded.

### Run the server

```bash
uv run monsieur-cuisine-mcp        # starts the MCP server over stdio
```

### Connect it to an MCP client

Claude Code:

```bash
claude mcp add monsieur-cuisine -- uv run --directory /path/to/Unofficial-Monsieur-Cuisine-MCP monsieur-cuisine-mcp
```

…or the generic JSON config most MCP clients understand:

```json
{
  "mcpServers": {
    "monsieur-cuisine": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/Unofficial-Monsieur-Cuisine-MCP", "monsieur-cuisine-mcp"]
    }
  }
}
```

Verbose logs: set `MC_LOG_LEVEL=DEBUG` or pass `--debug` to either command.

## Tools

Fourteen tools, grouped by what they touch.

### Discover & read — public, no login needed

| Tool | What it does |
| --- | --- |
| `search_recipes` | Search the public catalog with rich filters: category, complexity, duration, rating, device type, and even nutrient **ranges** (kcal / protein / carbs / fat). |
| `list_recipe_filters` | List the valid filter option ids (categories, complexity, …) for a language — feed these into `search_recipes`. |
| `get_recipe` | Full details for a recipe by id: ingredients, per-serving steps, and nutrients. Also reads **your own** drafts/private recipes when you're logged in. |

### Your account — login required

| Tool | What it does |
| --- | --- |
| `list_my_recipes` | Your own authored recipes, filterable by status (draft / in-review / private / published). |
| `list_favorite_recipes` | Your bookmarked recipes. |
| `add_favorite_recipe` / `remove_favorite_recipe` | Bookmark or un-bookmark a recipe. |
| `search_ingredients` | Search the ingredient database (returns the ingredient ids used to link steps to real ingredients). |

### Author recipes — login required

| Tool | What it does |
| --- | --- |
| `list_recipe_creation_options` | Valid complexity/category/unit/device ids **plus** the full device-program catalog (customized, steaming, sous-vide, cooking eggs, …). |
| `create_recipe` | Create a draft: title, description, servings, prep/ready times, whole-number nutrients, ingredient groups, steps with automatic device programs, and an optional cover photo. |
| `edit_recipe` | Change any fields of one of your recipes — only what you pass is updated, everything else is preserved. |
| `publish_recipe` | Publish a draft **privately**, or submit it for **public** editorial review. Also flips an already-published recipe between private and public. |
| `delete_recipe` | Move one of your recipes to trash (works from any status). |
| `restore_recipe` | Bring a trashed recipe back as a draft (undo a delete). |

## How it works

- **Public API** (`mc-api.tecpal.com`) — search, filters, and public recipe
  details. No auth.
- **Session proxy** (`/proxy-api`) — everything tied to your account (favorites,
  your own recipes, create/edit/publish/delete), authenticated with the browser
  cookies from the manual login.
- **WordPress admin-ajax** — the recipe-creation "guide" (valid categories,
  units, devices).

The code lives in a small package split by concern: `client` (the authenticated
proxy call + error handling), `search`, `favorites`, `ingredients`, `nutrients`,
`media` (cover-photo upload), `payloads` (recipe read→write mapping), `devices`
(the device-program catalog and step builder), `lifecycle` (status changes +
listing), and `authoring` (create/edit).

## Good to know / limitations

- **It's your real account.** Authoring tools create, change, publish, and trash
  real recipes. `delete_recipe` moves a recipe to trash — it vanishes from the
  site (which has **no trash view or restore button**, so treat it as permanent
  there), but the data is kept and `restore_recipe` can bring it back as a draft.
  There's no hard-delete, so trashed recipes linger server-side forever.
- **Nutrients must be whole numbers** — fractional values are rounded.
- **Every step needs a title** and **every ingredient group needs a name** to be
  publishable; the tools auto-fill sensible defaults ("Step 1", "General
  Ingredients") so drafts stay publishable.
- **Publishing is a one-way street away from "draft".** A published recipe can be
  switched between private and public, or deleted — but it **cannot be reverted
  to a draft** (the platform forbids it).
- **Public vs. private publish.** "Private" is instant; "public" submits the
  recipe for Monsieur Cuisine's editorial review and only goes live after they
  approve it.
- **Some categories aren't assignable.** `list_recipe_creation_options` returns a
  few categories the create API rejects; if you hit `Invalid category`, pick a
  different one.
- **Rate limiting.** Bursts of write calls can get temporarily throttled — space
  them out.

## Vibe-coded extensions in this fork

This fork (published as `monsieur-cuisine-mcp-vibe`) was built conversationally on
top of the original server — i.e. **vibe-coded**, end to end, with an AI coding
assistant driving the changes. It keeps the original tools intact and adds:

- **English (`en-US`) by default** for all Monsieur Cuisine operations (the
  upstream default is Polish `pl-PL`), including the matching login locale.
- **Paprika import** (`list_paprika_recipes`, `get_paprika_recipe`,
  `import_paprika_recipes`): reads a Paprika `.paprikarecipes.zip` export and
  converts each recipe — splitting ingredient lines into amount/unit/name,
  mapping Thermomix free-text notation (`3 sec/speed 7`, `15 min/100°C/speed 1`,
  `212°F/reverse speed 1`, `weigh 50 g`, oven/Varoma settings) onto Monsieur
  Cuisine Smart device programs (`customized`, `slow_cooking`, `roasting`,
  `steaming`, `kneading`, `scale`), parsing servings/times/nutrition, uploading
  the embedded photo as the cover image, mapping categories/difficulty onto the
  locale's ids, then creating and (optionally) publishing each recipe. Re-runs
  are idempotent via `skip_existing`.
- **Automatic `scale` weigh steps** wherever a recipe calls for weighing, so
  gram-weighted ingredients actually get a weighing step.
- **Pre-publish analysis** (`analyze_recipe`) and a publish gate: `publish_recipe`
  now runs the analyzer first and refuses to publish recipes with blocking
  issues (missing quantities, empty step titles, missing cover photo).
- **Cookidoo import** (`cookidoo_login`, `cookidoo_status`, `get_cookidoo_recipe`,
  `import_cookidoo_recipe`): logs in to Cookidoo (Thermomix's official recipe
  platform) via the unofficial `cookidoo-api` package and imports a recipe by id
  or URL, converting Thermomix steps to MC Smart programs, parsing nutrition,
  uploading the cover photo, and creating/publishing. Credentials via
  `COOKIDOO_EMAIL`/`COOKIDOO_PASSWORD` env vars or a `.env` file (gitignored);
  the auth token is cached in `.cookidoo_token`.

Treat this fork with the same caution as the upstream: reverse-engineered API,
no unit tests, writes to your real account.

## License

Apache-2.0.
