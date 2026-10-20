"""The Content-Security-Policy on every web page, and the page scripts that policy would otherwise break.

The AI's replies are shown as markdown, so a prompt-injected reply could carry an image that sends data to another
site. The policy stops the browser fetching it. Every test names the bug it would catch."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

# The fixtures and the PIN-page helper are the web tests' own: one way to build the app, not two.
from test_web import add_jonathan, enter, make_site, seeded_db, settings, site, start  # noqa: F401

TEMPLATES = Path(__file__).parent.parent / "src" / "karo" / "web" / "templates"
PAGE_TEMPLATES = sorted(p for p in TEMPLATES.glob("*.html") if p.name != "base.html")

POLICY_META = re.compile(r'<head>\s*<meta http-equiv="Content-Security-Policy" content="([^"]*)">')


def policy_of(page: str) -> dict[str, str]:
    """The directives of the policy meta, which must be the first element in <head>: it only covers what follows it."""
    found = POLICY_META.search(page)
    assert found, "no Content-Security-Policy meta as the first element in <head>"
    directives = (d.strip() for d in found.group(1).split(";") if d.strip())
    return {name: values for name, _, values in (d.partition(" ") for d in directives)}


def page_at(site, path: str) -> str:
    return site.client.get(path).text


def every_page(site):
    """A page of each kind: login, chat, the PIN page, a notice (expired link), the demo controls and the log."""
    site.login()
    token = start(site)
    return {
        "login": page_at(site, "/"),
        "chat": page_at(site, "/chat"),
        "confirm": page_at(site, f"/confirm/{token}"),
        "expired link": page_at(site, "/confirm/not-a-real-token"),
        "demo": page_at(site, "/demo"),
        "log": page_at(site, "/log"),
    }


# --- the policy ----------------------------------------------------------------------------------


def test_every_kind_of_page_blocks_images_and_requests_to_other_sites(site):
    # Catches: a page that was left out of the policy, so a markdown image in a reply can still send data away.
    for name, page in every_page(site).items():
        policy = policy_of(page)
        assert policy["img-src"] == "'self' data: blob:", name
        assert policy["connect-src"] == "'self'", name


def test_scripts_run_only_from_this_site_and_the_deep_chat_cdn(site):
    # Catches: 'unsafe-inline' or 'unsafe-eval' added to make a script work, which lets an injected script run.
    assert policy_of(page_at(site, "/"))["script-src"] == "'self' https://cdn.jsdelivr.net"


def test_the_policy_blocks_plugins_and_a_changed_base_address(site):
    # Catches: an <object> plugin or an injected <base href> pointing every relative link at another site.
    policy = policy_of(page_at(site, "/"))
    assert policy["object-src"] == "'none'"
    assert policy["base-uri"] == "'none'"


def test_the_policy_falls_back_to_this_site_for_anything_it_does_not_name(site):
    # Catches: a source type missing from the list (frames, media, workers) being open to every site.
    assert policy_of(page_at(site, "/"))["default-src"] == "'self'"


def test_the_policy_is_still_the_first_element_on_the_page_that_returns_to_the_chat(site):
    # Catches: the refresh meta placed ahead of the policy meta, which would leave that page outside the policy.
    site.login()
    page = enter(site, add_jonathan(site)).text
    assert 'http-equiv="refresh"' in page
    policy_of(page)


def test_every_page_template_gets_the_policy_from_the_base_template():
    # Catches: a new page that does not extend base.html, so it has no policy at all.
    for template in PAGE_TEMPLATES:
        assert re.match(r'\{% extends "base.html" %\}', template.read_text(encoding="utf-8")), template.name


# --- scripts the policy would block --------------------------------------------------------------


class Markup(HTMLParser):
    """Finds the page code a policy without 'unsafe-inline' silently stops: inline scripts, handlers, javascript: links."""

    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts = 0
        self.handlers: list[str] = []
        self.script_urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        names = dict(attrs)
        if tag == "script" and "src" not in names:
            self.inline_scripts += 1
        self.handlers += [f"<{tag} {name}>" for name in names if name.startswith("on")]
        self.script_urls += [f"<{tag} {name}>" for name, value in attrs if (value or "").strip().lower().startswith("javascript:")]


JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)


def markup_of(source: str) -> Markup:
    # Jinja goes first: a quote inside one of its strings (the "I'm Karo" in chat.html) would end an attribute early.
    markup = Markup()
    markup.feed(JINJA.sub("", source))
    return markup


def template_markup(template: Path) -> Markup:
    return markup_of(template.read_text(encoding="utf-8"))


def test_the_scan_finds_an_inline_script_and_skips_one_with_a_src():
    # Catches: a scan that passes every template because it cannot tell inline code from a script file.
    assert markup_of("<p>x</p><script>alert(1)</script>").inline_scripts == 1
    assert markup_of('<script src="/static/a.js"></script>').inline_scripts == 0


def test_the_scan_finds_an_inline_event_handler():
    # Catches: a scan that misses an onclick attribute, so the handler dies under the policy with no test failing.
    assert markup_of('<button onclick="go()">Go</button>').handlers == ["<button onclick>"]


def test_the_scan_finds_a_javascript_link():
    # Catches: a scan that misses href="javascript:..." in any letter case.
    assert markup_of('<a href=" JavaScript:go()">Go</a>').script_urls == ["<a href>"]


def test_the_scan_is_not_thrown_by_an_apostrophe_inside_a_jinja_value():
    # Catches: the "I'm Karo" in chat.html ending the attribute early, so words after it (like "only") read as attributes.
    source = """<deep-chat intro='{{ {"text": "I'm only here"}|tojson }}' onclick="go()"></deep-chat>"""
    assert markup_of(source).handlers == ["<deep-chat onclick>"]


@pytest.mark.parametrize("template", PAGE_TEMPLATES + [TEMPLATES / "base.html"], ids=lambda p: p.name)
def test_no_template_has_an_inline_script(template):
    # Catches: a later inline <script> that the policy blocks, so a page feature dies with only a console message.
    assert template_markup(template).inline_scripts == 0


@pytest.mark.parametrize("template", PAGE_TEMPLATES + [TEMPLATES / "base.html"], ids=lambda p: p.name)
def test_no_template_has_an_inline_event_handler(template):
    # Catches: an onclick/onsubmit attribute, which the policy blocks, so the button or the confirm prompt does nothing.
    assert template_markup(template).handlers == []


@pytest.mark.parametrize("template", PAGE_TEMPLATES + [TEMPLATES / "base.html"], ids=lambda p: p.name)
def test_no_template_has_a_javascript_link(template):
    # Catches: href="javascript:..." which the policy blocks the same way as an inline script.
    assert template_markup(template).script_urls == []


# --- the moved page scripts ----------------------------------------------------------------------


def test_the_chat_page_runs_its_script_while_the_page_loads_not_deferred(site):
    # Catches: `defer` running the script after Deep Chat has started, so onComponentRender is set too late and the
    # inbox poll that delivers the PIN link never starts.
    site.login()
    assert '<script src="/static/chat.js"></script>' in page_at(site, "/chat")


def test_the_chat_script_is_served_as_javascript(site):
    # Catches: a server that labels .js as text/plain, which the browser refuses to run as a script.
    response = site.client.get("/static/chat.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_the_chat_script_still_sets_the_message_id_and_polls_the_inbox(site):
    # Catches: the move dropping the request handler (a retry would look like a new message) or the inbox polling
    # (the confirm link would never reach the chat).
    script = site.client.get("/static/chat.js").text
    assert "requestInterceptor" in script and "message_id" in script
    assert "customElements.whenDefined" in script and "onComponentRender" in script


def test_the_chat_script_takes_the_inbox_address_from_the_page(site):
    # Catches: the script and the page naming the inbox address twice, and one of them being changed alone.
    site.login()
    assert 'data-inbox-url="/api/inbox"' in page_at(site, "/chat")
    assert "dataset.inboxUrl" in site.client.get("/static/chat.js").text


def test_the_demo_page_still_asks_before_it_resets(site):
    # Catches: the reset button deleting everything at once because the onsubmit prompt was removed with nothing to replace it.
    site.login()
    page = page_at(site, "/demo")
    assert re.search(r'<form[^>]*action="/demo/reset"[^>]*data-confirm="Start the demo again', page)
    assert '<script src="/static/demo.js" defer></script>' in page


def test_the_demo_script_stops_a_form_the_person_does_not_confirm(site):
    # Catches: a prompt that is shown but whose answer is ignored, so "Cancel" still resets the demo.
    response = site.client.get("/static/demo.js")
    assert "confirm(" in response.text and "preventDefault" in response.text
    assert "javascript" in response.headers["content-type"]


def test_the_chat_sets_its_own_font_so_deep_chat_adds_no_google_fonts_stylesheet(site):
    # Catches: Deep Chat's default font loading a Google Fonts stylesheet that the policy blocks: an error on every page load.
    site.login()
    assert re.search(r'<deep-chat[^>]*\sstyle="[^"]*font-family:', page_at(site, "/chat"))
