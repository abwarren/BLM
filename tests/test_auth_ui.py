"""AUTHENTICATION — LOGIN UI CONTRACT (§1, §7, §8, §12).

The login screen is a production surface: these tests pin the elements the
directive requires (branding, subtitle, both fields, show/hide control,
remember me, sign-in button, error region, loading state, responsive CSS)
and the security constraints that apply to it (no secrets, no tokens, no
credentials in HTML/CSS/JS, no internal error strings rendered).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

STATIC = Path(__file__).resolve().parent.parent / "blm_v4" / "dashboard" / "static"
LOGIN_HTML = STATIC / "login.html"
LOGIN_CSS = STATIC / "login.css"
LOGIN_JS = STATIC / "login.js"
DASHBOARD_JS = STATIC / "dashboard.js"
INDEX_HTML = STATIC / "index.html"


@pytest.fixture(scope="module")
def html():
    return LOGIN_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css():
    return LOGIN_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js():
    return LOGIN_JS.read_text(encoding="utf-8")


# ── the page exists and is served ─────────────────────────────────
def test_login_page_is_served_at_login_without_a_session(auth_stack):
    r = auth_stack["client"].get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers.get("cache-control") == "no-store"


def test_login_page_never_leaks_internal_errors(auth_stack):
    body = auth_stack["client"].get("/login").text
    for forbidden in ("Traceback", "sqlite", "bcrypt", "File \"",
                      "Internal Server Error"):
        assert forbidden not in body


# ── §1 required elements ──────────────────────────────────────────
def test_login_page_carries_the_blm_branding_and_subtitle(html):
    assert "BLM" in html
    assert re.search(r"Basketball Live Markets", html, re.I)
    assert "brand-wordmark" in html


def test_login_page_has_a_username_field_and_a_password_field(html):
    assert re.search(r'id="identifier"[^>]*name="username"', html)
    assert re.search(r'id="password"[^>]*type="password"', html)
    assert "Email or Username" in html
    assert ">Password<" in html or ">Password</label>" in html


def test_login_page_has_a_show_hide_password_control(html, js):
    assert 'id="togglePw"' in html
    assert 'aria-pressed="false"' in html
    assert 'password.type' in js


def test_login_page_has_remember_me(html):
    assert re.search(r'type="checkbox"[^>]*id="remember"', html)
    assert "Remember me" in html


def test_login_page_has_a_sign_in_button(html):
    assert 'id="submitBtn"' in html
    assert re.search(r'<button[^>]*type="submit"[^>]*>', html)
    assert "Sign In" in html


def test_login_page_has_an_accessible_error_region(html):
    assert 'id="loginError"' in html
    assert 'role="alert"' in html
    assert 'aria-live="assertive"' in html


def test_login_page_has_a_loading_state(html, js):
    assert 'id="submitBusy"' in html
    assert "blm-spin" in html or "spinner" in html
    assert "setBusy" in js


def test_login_page_has_a_secure_access_footer(html):
    assert "Secure access" in html
    assert "POPIA" in html


def test_login_page_has_the_market_data_visual_treatment(html, css):
    assert "market-card" in html and "spark" in html
    assert "arena-veil" in html
    assert "blm-login-arena.jpg" in css


def test_login_page_does_not_load_the_heavy_dashboard_bundle(html):
    """§12: the login page must not pay for the operator dashboard."""
    for heavy in ("dashboard.js", "stats.js", "explorer.js", "chart.js",
                  "styles.css"):
        assert heavy not in html, f"login page loads {heavy}"


def test_login_page_sets_noindex_and_viewport(html):
    assert "noindex" in html
    assert "viewport" in html


# ── §1 responsive layout ──────────────────────────────────────────
def test_login_css_is_responsive_across_breakpoints(css):
    media = re.findall(r"@media[^{]+", css)
    joined = " ".join(media)
    assert "max-width: 1180px" in joined
    assert "max-width: 940px" in joined
    assert "max-width: 620px" in joined
    assert "grid-template-columns" in css        # two-column → one-column


def test_login_css_uses_the_dark_blue_red_palette(css):
    assert "#2f6bff" in css or "#2f6bff" in css.lower()
    assert "#ff3b47" in css.lower()
    assert "linear-gradient" in css
    assert "backdrop-filter" in css              # glass card


def test_login_css_respects_reduced_motion(css):
    assert "prefers-reduced-motion" in css


# ── §8 no secrets in the frontend ─────────────────────────────────
def test_login_page_contains_no_secret_material(html, css, js):
    """No credential material ships to the browser — the check is for
    material, not for the English word 'secret' in a comment."""
    blob = (html + css + js).lower()
    for forbidden in ("password_hash", "bcrypt", "$2b$", "bcrypt_sha256",
                      "set-cookie", "api_key", "client_secret",
                      "authorization:", "blm_session="):
        assert forbidden not in blob, f"{forbidden!r} present in login assets"
    # and no inline credential-value assignment of any name
    assert not re.search(r"(password|token|secret)\s*[:=]\s*[\"'][^\"']{6,}",
                         blob)


def test_login_js_never_persists_credentials_in_browser_storage(js):
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "indexedDB"):
        assert forbidden not in js, forbidden


def test_login_js_does_not_render_raw_server_error_text(js):
    """Client messages are a fixed map keyed by outcome, so an internal
    server string can never reach the operator's screen."""
    assert "MESSAGES" in js
    assert re.search(r"MESSAGES\.(invalid|validation|rate|unavailable)",
                     js)
    # no direct pass-through of a server-provided detail field
    assert "data.detail" not in js
    assert "detail" not in js


def test_login_page_forms_post_to_the_auth_endpoint(html):
    assert 'action="/api/auth/login"' in html
    assert 'method="post"' in html
    assert 'name="username"' in html and 'name="password"' in html


# ── §7 dashboard integration ──────────────────────────────────────
def test_dashboard_attaches_the_session_cookie_and_csrf_header():
    text = DASHBOARD_JS.read_text(encoding="utf-8")
    assert "X-CSRF-Token" in text
    assert 'credentials' in text
    assert "/api/auth/me" in text
    assert "/api/auth/logout" in text


def test_dashboard_redirects_to_login_on_session_expiry():
    text = DASHBOARD_JS.read_text(encoding="utf-8")
    assert 'res.status === 401' in text
    assert "location.replace(LOGIN_URL)" in text or "/login" in text


def test_dashboard_exposes_a_sign_out_control():
    html_text = INDEX_HTML.read_text(encoding="utf-8")
    js_text = DASHBOARD_JS.read_text(encoding="utf-8")
    assert 'id="signOutBtn"' in html_text
    assert "signOutBtn" in js_text


def test_dashboard_does_not_store_tokens_in_local_storage():
    """The existing UI preferences may use localStorage; the AUTH token
    must not.  Assert the auth layer never writes there."""
    text = DASHBOARD_JS.read_text(encoding="utf-8")
    auth_block = text[text.index("AUTH TRANSPORT (BLM login layer)"):
                      text.index("const POLL_MS")]
    # assert on WRITES/TELEMETRY, not on the prose that explains they are
    # absent — the neighbouring comment legitimately names the mechanism
    for forbidden in ("localStorage.setItem", "sessionStorage.setItem",
                      "localStorage.getItem", "sessionStorage.getItem",
                      "document.cookie", "indexedDB"):
        assert forbidden not in auth_block, (
            f"auth transport touches {forbidden}")


def test_static_assets_contain_no_credential_values():
    """No password, hash or token literal ships in any served asset."""
    for name in ("login.html", "login.css", "login.js", "dashboard.js",
                 "index.html", "styles.css"):
        path = STATIC / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "bcrypt_sha256$" not in text, name
        assert not re.search(r"\$2[aby]\$\d\d\$", text), name


# ── environment banner (staging safety) ───────────────────────────
def test_login_page_carries_the_test_banner_only_when_configured():
    from blm_v4.auth.api import _inject_banner
    body = _inject_banner(
        (STATIC / "login.html").read_text(encoding="utf-8"),
        "\U0001F534 TEST ENVIRONMENT")
    assert 'id="envBanner"' in body
    assert "TEST ENVIRONMENT" in body


def test_no_banner_is_injected_when_unset():
    from blm_v4.auth.api import _inject_banner
    original = (STATIC / "login.html").read_text(encoding="utf-8")
    assert _inject_banner(original, "") == original
