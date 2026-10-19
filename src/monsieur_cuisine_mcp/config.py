import os
from pathlib import Path

# Application-wide logging verbosity. Override with the MC_LOG_LEVEL env var,
# e.g. `MC_LOG_LEVEL=DEBUG uv run monsieur-cuisine-mcp-login` for detailed
# per-event logs (login progress, request URLs and query params). Session
# secrets are never logged: cookies are only ever reported as a count.
LOG_LEVEL = os.environ.get("MC_LOG_LEVEL", "INFO").upper()

MC_BASE_URL = "https://www.monsieur-cuisine.com"
MC_LOGIN_ENTRY_URL = f"{MC_BASE_URL}/pl/require-login"
MC_DOMAIN = "monsieur-cuisine.com"

STATE_DIR = Path.home() / ".monsieur-cuisine-mcp"
STORAGE_STATE_PATH = STATE_DIR / "storage_state.json"

# Manual, browser-based authentication mechanism. The timeout covers the whole
# manual login (SSO redirect, credentials, any CAPTCHA/2FA and the redirect
# back), so it is generous - the user is typing.
MC_AUTHENTICATION_TIMEOUT_SECONDS = 600
MC_AUTHENTICATION_SSO_DOMAIN = "accounts.lidl.com"

# Browser profile reused by the manual login. Keeping one profile, instead of a
# throwaway context per attempt, means the Lidl device id and the SSO/bot
# protection cookies survive between attempts, so each login looks like the same
# returning device rather than a brand-new one.
MC_BROWSER_PROFILE_DIR = STATE_DIR / "browser-profile"

# A failed login dumps a timestamped trace here (navigations, console errors,
# request statuses) so the failure can be diagnosed instead of guessed at.
# Credentials are never recorded: no request or response bodies are captured.
MC_LOGIN_DIAGNOSTICS_PATH = STATE_DIR / "last-login.log"

# Locale for the login browser, matching MC_LOGIN_ENTRY_URL's language.
MC_BROWSER_LOCALE = "pl-PL"

# Presence of any of these cookies on MC_DOMAIN means the session is
# established. Polling for them is more reliable than matching the landing URL,
# which varies with the SSO round trip.
MC_SESSION_COOKIE_PREFIXES = ("wordpress_logged_in_", "lidl_sso_id_token")

# Public recipe search API (no authentication required)
MC_API_BASE_URL = "https://mc-api.tecpal.com"
MC_API_DEFAULT_LANGUAGE = "en-US"
MC_API_REQUEST_TIMEOUT_SECONDS = 15.0

# Same-origin proxy used by the web app for endpoints that require a logged-in
# session (e.g. favorite/bookmarked recipes). It forwards {"endpoint", "lang",
# "method"} to the underlying mc-api.tecpal.com API, authenticated via the
# browser session cookies saved by the manual login flow (see auth.py).
MC_PROXY_API_URL = f"{MC_BASE_URL}/proxy-api"

# WordPress admin-ajax endpoint used only to load the "recipe creation guide"
# (valid complexity/category/device/unit options for the create-recipe form).
# It derives the response language from the Referer path prefix (e.g. /pl/),
# not from a parameter, so callers must send a matching Referer header.
MC_WP_AJAX_URL = f"{MC_BASE_URL}/wp-admin/admin-ajax.php"
MC_CREATE_RECIPE_PATH = "create-recipe"

# Recipe creation is longer-running server-side than reads (it resolves
# ingredients and recomputes nutrition), so it gets a more generous timeout.
MC_API_WRITE_TIMEOUT_SECONDS = 45.0

# Browser-like User-Agent sent with same-origin proxy-api / admin-ajax calls.
# The site's Akamai bot protection is stricter on write requests (POST/PUT/
# PATCH) and tends to return 403 for requests that don't look like they came
# from a real browser (missing User-Agent / Origin / Referer), so authenticated
# calls mirror the headers a same-origin fetch from the web app would send.
MC_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
