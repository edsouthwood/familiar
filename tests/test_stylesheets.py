"""Guards for the shared palette (static/tokens.css).

The page chrome used to hand-copy the same twenty-odd hex values into three
stylesheets. These tests keep that from creeping back, and keep the shell
boundary honest in the other direction: a shell must not depend on the chrome's
tokens, because a shell is meant to be droppable into any page.

There are two palettes in that file now -- the dark table and the daylight
theme -- which raises a second way for the same old bug to come back: a token
the light block forgets keeps its dark value, so one word somewhere on the page
is near-white on parchment and nobody notices until a stranger reads it.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
TOKENS = STATIC / "tokens.css"

# The stylesheets that make up the page chrome, as opposed to a creature.
CHROME = ["familiar.css", "timeline.css", "cast.css", "nav.css", "theme.css",
          "button.css", "field.css"]

HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
VAR_USE_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)")
VAR_DEF_RE = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)


def _defined_tokens():
    return set(VAR_DEF_RE.findall(TOKENS.read_text()))


@pytest.mark.parametrize("name", CHROME)
def test_chrome_stylesheets_hold_no_raw_colours(name):
    text = (STATIC / name).read_text()
    assert HEX_RE.findall(text) == [], (
        f"{name} has raw hex colours; they belong in tokens.css"
    )


@pytest.mark.parametrize("name", CHROME)
def test_every_token_used_is_defined(name):
    defined = _defined_tokens()
    used = set(VAR_USE_RE.findall((STATIC / name).read_text()))
    assert used <= defined, f"{name} uses undefined tokens: {sorted(used - defined)}"


@pytest.mark.parametrize("name", CHROME)
def test_no_font_stack_is_repeated(name):
    """The body font is a token too -- only `inherit` should appear downstream."""
    text = (STATIC / name).read_text()
    assert "Georgia" not in text, f"{name} repeats the font stack; use var(--font-body)"


def _token_blocks():
    """Every rule in tokens.css, as (selector, {token: value})."""
    body = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(), flags=re.DOTALL)
    blocks = []
    for selector, declarations in re.findall(r"([^{}]+)\{([^}]*)\}", body):
        parsed = {}
        for declaration in filter(None, (d.strip() for d in declarations.split(";"))):
            name, _, value = declaration.partition(":")
            parsed[name.strip()] = value.strip()
        blocks.append((selector.strip(), parsed))
    return blocks


def test_tokens_file_defines_only_tokens():
    """tokens.css is a vocabulary, not a stylesheet. Nothing here should paint.

    Two rules rather than one since the daylight theme arrived, so the check is
    on their shape instead of their names: every rule is rooted at :root, and
    every declaration inside one is a custom property. A theme is a second set
    of answers to the same words, never a second place to style a page.
    """
    blocks = _token_blocks()
    assert blocks, "tokens.css defines nothing"
    for selector, declarations in blocks:
        assert selector.startswith(":root"), f"tokens.css styles {selector!r}"
        assert declarations, f"{selector} declares nothing"
        for name in declarations:
            assert name.startswith("--"), f"{selector} paints something: {name!r}"


LIGHT = ':root[data-theme="light"]'


def test_the_daylight_theme_answers_every_colour_the_dark_one_names():
    """The failure this guards is silent and one-word-wide: an unanswered token
    keeps its dark value, and a single label somewhere comes out near-white on
    parchment. Fonts are exempt -- a typeface does not change with the light --
    so the rule is drawn around values that are literally a colour."""
    blocks = dict(_token_blocks())
    dark, light = blocks[":root"], blocks[LIGHT]
    colours = {n for n, v in dark.items() if "#" in v or "rgb" in v}
    assert colours - set(light) == set(), (
        f"the daylight theme has no answer for: {sorted(colours - set(light))}"
    )


def test_the_daylight_theme_invents_no_words_of_its_own():
    """The other direction. A token defined only under the light theme is
    undefined on the dark one, which is the same invisible-text bug with the
    themes the other way round."""
    blocks = dict(_token_blocks())
    stray = set(blocks[LIGHT]) - set(blocks[":root"])
    assert not stray, f"only the daylight theme defines: {sorted(stray)}"


@pytest.mark.parametrize("shell", sorted(p.name for p in (STATIC / "shells").iterdir()))
def test_shells_do_not_depend_on_chrome_tokens(shell):
    """A shell carries its own colours so it can be dropped into any page."""
    css = (STATIC / "shells" / shell / "shell.css").read_text()
    leaked = set(VAR_USE_RE.findall(css)) & _defined_tokens()
    assert not leaked, f"shell {shell!r} reaches into the page palette: {sorted(leaked)}"


PAGES = sorted(p.name for p in STATIC.glob("*.html"))


@pytest.mark.parametrize("page", PAGES)
def test_tokens_are_linked_before_the_page_stylesheet(page):
    """A token used before it is defined falls back to nothing -- invisible text."""
    text = (STATIC / page).read_text()
    links = re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', text)
    assert links, f"{page} links no stylesheet"
    assert links[0] == "/tokens.css", f"{page} loads {links[0]} before tokens.css"


@pytest.mark.parametrize("page", PAGES)
def test_page_assets_are_root_absolute(page):
    """Pages are served from more than one depth -- /login.html at the root,
    index.html at /t/<slug>/, the timeline at /t/<slug>/timeline.html. A
    relative stylesheet or script href resolves against the *current* directory,
    so it silently 404s on the nested ones. Only links between sibling pages
    may be relative."""
    text = (STATIC / page).read_text()
    assets = re.findall(r'(?:<link[^>]*href|<script[^>]*src)="([^"]+)"', text)
    bad = [a for a in assets if not a.startswith(("/", "http"))]
    assert not bad, f"{page} has relative asset paths: {bad}"


# The shipped asset libraries. They are app-global -- one copy serves every
# table -- so a reference to one is always rooted, never relative to whichever
# page happens to be doing the referencing.
ASSET_DIRS = tuple(d.name for d in STATIC.iterdir() if d.is_dir())


@pytest.mark.parametrize("script", sorted(p.name for p in STATIC.glob("*.js")))
def test_asset_paths_built_in_js_are_root_absolute(script):
    """The same rule as above, for paths the browser never sees in the HTML.

    This is not hypothetical tidiness. index.html linked
    "/shells/cat/shell.css" correctly, and then loadShell() overwrote the href
    with "shells/cat/shell.css" on every single load -- which resolves to
    /t/<slug>/shells/... and 404s, so the familiar rendered with none of its
    state animations. The HTML-only check above could not see it, because by
    then the value came from a template literal.
    """
    text = (STATIC / script).read_text()
    bad = [m for m in re.findall(r'["\'`]([^"\'`\n]*)', text)
           if m.startswith(ASSET_DIRS) and not m.startswith("/")]
    assert not bad, f"{script} references an asset library relatively: {bad}"


def _rule(css: str, selector: str) -> str:
    """The declaration block for one top-level selector."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_the_settings_panel_bounds_and_scrolls_itself():
    """The stage cannot scroll -- `body { overflow: hidden }`, because it is a
    canvas holding a creature, not a document. The settings panel is fixed
    above the footer and grows upward from there, so if it does not bound its
    own height, everything past the top of the viewport is unreachable. That
    included the "Done" button.

    Both declarations are load-bearing and neither is enough alone: a
    max-height without an overflow clips the content away instead, and an
    overflow without a max-height never has anything to scroll.
    """
    rule = _rule((STATIC / "familiar.css").read_text(), "#settings")
    assert "max-height" in rule, "#settings can grow past the top of the screen"
    assert "overflow-y" in rule, "#settings clips instead of scrolling"


def test_the_settings_panel_has_a_vh_fallback_before_dvh():
    """Order matters, not just presence. `dvh` is the small viewport and is what
    we want; browsers that predate it must still get a bounded panel, so the
    `vh` line is declared first and quietly overridden."""
    rule = _rule((STATIC / "familiar.css").read_text(), "#settings")
    heights = re.findall(r"max-height:\s*([^;]+);", rule)
    assert len(heights) == 2 and "vh" in heights[0] and "dvh" in heights[1], heights


def test_every_table_page_is_routed():
    """A page that shows one table's data must be reachable under /t/<slug>/.

    Note what this is *not* guarding. Flask serves static/ at the site root, so
    every one of these files is already fetchable bare and unauthenticated --
    /hub.html returns 200 to a stranger. That is harmless: with no slug in the
    path, table.js leaves TABLE_SLUG empty, every fetch goes to /api/t//... and
    404s, and the markup itself holds nothing. The membership check lives on
    the API and on the /t/<slug>/ URL, which is where the data is.

    What it does guard is a page left out of TABLE_PAGES being a 404 *at its
    table URL* -- so relative links from the nav strip break, and the only way
    to open it is the bare path where it silently does nothing. A page that
    works in isolation and is broken in place is exactly the failure a manual
    click-through misses.

    index.html is the exception: it is the familiar's screen, served at
    /t/<slug>/ itself by table_index() rather than by name.
    """
    import server

    scoped = {p.name for p in STATIC.glob("*.html")
              if '/table.js"' in p.read_text()}
    assert scoped, "no page loads table.js; this test has stopped meaning anything"
    assert scoped - {"index.html"} == set(server.TABLE_PAGES), (
        f"not routed: {sorted(scoped - {'index.html'} - set(server.TABLE_PAGES))}; "
        f"routed but absent: {sorted(set(server.TABLE_PAGES) - scoped)}"
    )


def test_the_navigation_is_on_every_table_page():
    """It replaced a cycle in which each page linked to exactly one other, so
    there was no way to see where you were or what else existed. A page without
    it is a dead end again.

    index.html is included now, and that is the point of the drawer: the
    familiar's own screen had no navigation at all, only four links buried at
    the bottom of the settings panel."""
    import server

    for page in server.TABLE_PAGES + ("index.html",):
        text = (STATIC / page).read_text()
        assert 'id="table-nav"' in text, page
        assert "/nav.js" in text, page


def test_the_navigation_stylesheet_goes_wherever_the_navigation_does():
    """The bug this file did not catch, turned into a rule.

    The strip's container was styled in hub.css and its tabs in cast.css.
    timeline.html loaded neither, so its navigation painted as bare blue links;
    cast, places and search loaded cast.css but not hub.css, so they got tabs
    with no container. Splitting one component across two sheets that no page
    was obliged to load together is the whole failure."""
    for page in sorted(STATIC.glob("*.html")):
        text = page.read_text()
        if 'id="table-nav"' not in text:
            continue
        assert "/nav.css" in text, f"{page.name} has the navigation but not its stylesheet"


@pytest.mark.parametrize("page", PAGES)
def test_the_theme_is_on_every_page(page):
    """The lamp is not a feature of the signed-in pages. A reader who set the
    daylight theme on the sign-in page and then signed in must not walk into a
    dark room, and the page they cannot change it back from is the bug."""
    text = (STATIC / page).read_text()
    assert "/theme.css" in text, f"{page} has no theme stylesheet"
    assert "/theme.js" in text, f"{page} has no theme script"


@pytest.mark.parametrize("page", PAGES)
def test_the_theme_is_chosen_before_the_body_is_painted(page):
    """theme.js stamps data-theme on <html>, and tokens.css keys the palette
    off it. Loaded at the foot of the body, or deferred, that stamp lands after
    first paint -- which is a flash of the wrong theme on every single
    navigation. In the head, and blocking, is the whole point of the file."""
    text = (STATIC / page).read_text()
    head = text[: text.index("</head>")]
    tag = re.search(r"<script[^>]*/theme\.js[^>]*>", head)
    assert tag, f"{page} loads theme.js after the head"
    assert "defer" not in tag.group(0) and "async" not in tag.group(0), (
        f"{page} defers theme.js, which is a flash of the wrong palette"
    )


def test_only_the_theme_stylesheet_styles_the_toggle():
    """One component, one sheet -- the same rule the navigation keeps below,
    learned from the same bug."""
    for sheet in sorted(STATIC.glob("*.css")):
        if sheet.name == "theme.css":
            continue
        assert ".theme-toggle" not in sheet.read_text(), (
            f"{sheet.name} styles the theme toggle; theme.css is where that lives"
        )


def test_only_the_navigation_stylesheet_styles_the_navigation():
    """Stops the split re-forming. One component, one sheet."""
    for sheet in sorted(STATIC.glob("*.css")):
        if sheet.name == "nav.css":
            continue
        assert "#table-nav" not in sheet.read_text(), (
            f"{sheet.name} styles the navigation; nav.css is where that lives"
        )


def test_the_settings_panel_holds_no_navigation():
    """A cog is for changing how something works, not for getting somewhere
    else. It used to carry links to the timeline, the cast, the places and the
    search -- the only way off the familiar's screen, filed under Settings."""
    text = (STATIC / "index.html").read_text()
    panel = text[text.index('<div id="settings"'):]
    panel = panel[:panel.index("<script")]
    assert "<a " not in panel, "the settings panel has links in it again"


# ===== Contrast =====
#
# The palette is checked for *shape* above -- every token answered in both
# themes. These check what the values actually measure, because the failure
# they guard is invisible to every other test in this file: a border that
# parses, applies, and cannot be seen.


def _relative_luminance(hex_colour):
    """WCAG 2.1's L, for a #rrggbb string."""
    channels = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a, b):
    high, low = sorted((_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _palettes():
    blocks = dict(_token_blocks())
    return {"dark": blocks[":root"], "daylight": blocks[LIGHT]}


# Everything a control can be drawn on top of. A dropdown sits on a card, in a
# form panel, and directly on the page, depending on which one it is.
SURFACES = ["--surface-lift", "--surface", "--bg-deep", "--bg-void"]

# The tokens that are spent as the *edge of something you operate*, as opposed
# to a rule between two things. --line is deliberately not one of them.
EDGE_TOKENS = ["--edge", "--mark-edge", "--accent"]


@pytest.mark.parametrize("theme", ["dark", "daylight"])
@pytest.mark.parametrize("token", EDGE_TOKENS)
def test_a_control_edge_can_be_seen_against_what_it_sits_on(theme, token):
    """WCAG 1.4.11: the boundary of a user interface component needs 3:1.

    This is the rule --line quietly failed for every field and outlined button
    in the app -- 1.31:1 in daylight at its worst, which is a text box you
    cannot see the edge of. --edge exists to pass it; the test is here so the
    next person to reach for a prettier value finds out before a user does.
    """
    palette = _palettes()[theme]
    worst = min((_contrast(palette[token], palette[surface]), surface)
                for surface in SURFACES)
    assert worst[0] >= 3.0, (
        f"{theme}: {token} is only {worst[0]:.2f}:1 against {worst[1]}"
    )


@pytest.mark.parametrize("theme", ["dark", "daylight"])
def test_no_control_draws_its_edge_in_a_fill_colour(theme):
    """The way this last went wrong, and it is an easy mistake to repeat.

    field.css reached for --control-hover on `select:hover` -- the obvious
    token, given the name -- and it is a *fill*. The border dropped to 1.26:1
    in daylight, so hovering a dropdown made its outline fade out. A fill
    colour has no contrast obligation and must not be spent as an edge.
    """
    fills = ["--control", "--control-hover", "--chip", "--field", "--surface",
             "--surface-lift", "--mark-bg"]
    pattern = re.compile(
        r"border(?:-color)?:\s*(?:1px (?:solid|dashed)\s+)?var\((--[a-z0-9-]+)\)")
    interactive = re.compile(
        r"\b(button|select|textarea|input|\.btn|\.tab-btn|\.nav-toggle"
        r"|\.theme-toggle|\.gap|\.cta)\b")
    offenders = []
    for name in CHROME + ["hub.css", "search.css", "page.css"]:
        css = re.sub(r"/\*.*?\*/", "", (STATIC / name).read_text(), flags=re.DOTALL)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if not interactive.search(selectors):
                continue
            for token in pattern.findall(body):
                if token in fills:
                    offenders.append(f"{name}: {' '.join(selectors.split())} -> {token}")
    assert not offenders, "fill colours used as control edges: " + "; ".join(offenders)


# Every surface a filled control can be laid on. --control is a *button*, and a
# button turns up on a card, in a form panel and directly on the page.
FILLED_ON = SURFACES


@pytest.mark.parametrize("theme", ["dark", "daylight"])
def test_a_filled_button_can_be_told_apart_from_its_page(theme):
    """--control was #4a3a80, the same violet as --line: 1.53:1 against the
    page here and 1.07:1 in daylight. A filled button you cannot find the
    edges of is identifiable only by reading the words in it."""
    palette = _palettes()[theme]
    for token in ("--control", "--control-hover"):
        worst = min((_contrast(palette[token], palette[surface]), surface)
                    for surface in FILLED_ON)
        assert worst[0] >= 3.0, (
            f"{theme}: {token} is only {worst[0]:.2f}:1 against {worst[1]}"
        )


@pytest.mark.parametrize("theme", ["dark", "daylight"])
def test_a_filled_buttons_label_survives_its_fill(theme):
    """The other half of the same squeeze, and the reason --on-control exists.

    A fill bright enough to clear 3:1 on the dark table leaves no violet dark
    enough for a pale label to sit on -- searched exhaustively, a light-labelled
    primary button has no workable value on this palette. So the button inverts
    against its page and the label inverts with it. If somebody later 'fixes'
    --on-control back to --text, this is what says no.
    """
    palette = _palettes()[theme]
    for fill in ("--control", "--control-hover"):
        got = _contrast(palette["--on-control"], palette[fill])
        assert got >= 4.5, f"{theme}: --on-control on {fill} is {got:.2f}:1"


def _declared_pairs(name):
    """Every (background token, colour token) a rule sets on itself."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / name).read_text(), flags=re.DOTALL)
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        background = re.search(r"background(?:-color)?:\s*var\((--[a-z0-9-]+)\)", body)
        colour = re.search(r"(?<!-)\bcolor:\s*var\((--[a-z0-9-]+)\)", body)
        if background and colour:
            yield " ".join(selectors.split()), background.group(1), colour.group(1)


@pytest.mark.parametrize("theme", ["dark", "daylight"])
def test_text_is_readable_on_whatever_the_same_rule_puts_behind_it(theme):
    """The general form of the two tests above.

    Where one rule sets both a background and a colour it has stated the whole
    pair, and the pair is checkable without guessing at the DOM. This is what
    caught .kind-badge sitting at 3.18:1 -- deliberately quiet, quietly
    unreadable -- which no amount of looking at the palette would have shown,
    because both tokens in it were perfectly good on their own.
    """
    palette = _palettes()[theme]
    offenders = []
    for name in CHROME + ["hub.css", "search.css", "page.css"]:
        for selector, background, colour in _declared_pairs(name):
            if background not in palette or colour not in palette:
                continue
            got = _contrast(palette[colour], palette[background])
            if got < 4.5:
                offenders.append(f"{name}: {selector} -> {got:.2f}:1")
    assert not offenders, "text below 4.5:1 on its own background: " + "; ".join(offenders)


def test_every_campaign_section_says_what_it_is_for():
    """A field the form shows with no guidance is a box a DM has to guess at.

    campaign.py holds both halves so a renamed field cannot leave its hint
    behind; this checks the pairing survives, and that the server actually
    hands the hints over -- a hint nobody serves is a hint nobody reads.
    """
    import campaign as campaign_notes

    assert set(campaign_notes.HINTS) == set(campaign_notes.FIELD_KEYS)
    assert all(campaign_notes.HINTS[key].strip() for key in campaign_notes.FIELD_KEYS)
    server_source = (STATIC.parent / "server.py").read_text()
    assert "campaign_notes.HINTS" in server_source, "the hints are never sent"


def test_the_long_form_boxes_are_not_sized_by_a_short_form_rule():
    """The persona editor reuses .amend-form as its container, and .amend-form's
    two-line min-height and textarea.prose have the same specificity -- so
    whichever sheet loads second wins. It was timeline.css, and a whole persona
    was being written through a slot two lines tall. The `:not(.prose)` is what
    stops that, and it is invisible enough to be 'tidied' away."""
    timeline = (STATIC / "timeline.css").read_text()
    assert ".amend-form textarea:not(.prose)" in timeline, (
        "the amend box's min-height must exempt .prose or it re-shrinks the "
        "persona editor"
    )
    hub = (STATIC / "hub.css").read_text()
    assert not re.search(r"\.field textarea[^{]*\{[^}]*min-height", hub), (
        "hub.css is setting a min-height that would beat textarea.prose"
    )


# Every box somebody writes paragraphs into, and the file that builds it. These
# are the three the familiar reads in full before it answers anything -- which
# is what makes them prose rather than form fields, and why they get the
# long-form treatment instead of a six-row window.
PROSE_BOXES = [
    ("hub.js", "the campaign notes"),
    ("hub-familiar.js", "a familiar's persona"),
    ("hub-party.js", "a character's background"),
]


def _code_only(script):
    """Source with its comments removed.

    Written the first time as a plain substring search over the whole file,
    which passed happily against a *comment* naming the helper -- the test went
    green with the call deleted. A check that a prose comment can satisfy is
    not a check.
    """
    text = (STATIC / script).read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


@pytest.mark.parametrize("script,what", PROSE_BOXES)
def test_long_form_boxes_get_the_long_form_treatment(script, what):
    """Each of these built a textarea and left it at whatever height the
    surrounding form happened to impose -- two lines for the persona, three for
    the campaign, six fixed rows for a background. The class is what opts into
    the prose sizing and the auto-grow is what keeps it right afterwards;
    either one alone leaves somebody writing through a slot."""
    code = _code_only(script)
    assert 'className = "prose"' in code or '.className = "prose"' in code, (
        f"{what} is not marked as a prose box"
    )
    calls = [m for m in re.findall(r"(\w+\s+)?autoGrowTextarea\s*\(", code)
             if m.strip() != "function"]
    assert calls, f"{what} does not grow as you type"


def _familiar_icon_keys():
    """The shell ids hub-familiar.js has a drawing for."""
    source = (STATIC / "hub-familiar.js").read_text()
    block = re.search(r"const FAMILIAR_ICONS = \{(.*?)\n\};", source, re.DOTALL)
    assert block, "hub-familiar.js has no icon table"
    return set(re.findall(r"^\s*([a-z0-9_]+):", block.group(1), re.MULTILINE))


def test_every_shipped_shell_has_an_icon():
    """A familiar wears its own shape beside its name. A shell added to
    static/shells/ with no drawing here still renders -- it falls back to the
    paw -- so this is not load-bearing, which is exactly why it would go
    unnoticed: the new creature would quietly be a paw forever."""
    shells = {p.name for p in (STATIC / "shells").iterdir() if p.is_dir()}
    missing = shells - _familiar_icon_keys()
    assert not missing, f"shells with no icon: {sorted(missing)}"


def test_the_icon_table_keeps_its_fallback():
    """The other half. Without `paw` an unknown shell renders as nothing at
    all, and an empty <svg> beside a name is harder to spot than a wrong one."""
    assert "paw" in _familiar_icon_keys(), "no fallback icon"


def test_the_persona_toggle_is_a_button():
    """It was an <a href="#"> that called preventDefault: announced as a link,
    offering a meaningless target to "copy link address", and deaf to the space
    bar. It opens a panel on the page, so it is a disclosure button and has to
    say so -- in the element and in aria-expanded."""
    code = _code_only("hub-familiar.js")
    persona = re.search(r"const persona = document\.createElement\(\"(\w+)\"\)", code)
    assert persona and persona.group(1) == "button", (
        "the persona toggle is not a <button>"
    )
    assert 'persona.setAttribute("aria-expanded"' in code, (
        "a disclosure button must say whether it is open"
    )


# ===== The shells =====
#
# find familiar names fifteen forms and then stops. All fifteen are drawn.
SPELL_FORMS = {
    "bat", "cat", "crab", "frog", "hawk", "lizard", "octopus", "owl",
    "snake", "fish", "rat", "raven", "seahorse", "spider", "weasel",
}

SHELL_IDS = sorted(p.name for p in (STATIC / "shells").iterdir() if p.is_dir())


def test_every_form_the_spell_allows_is_drawn():
    """Find familiar offers fifteen shapes; the picker should offer all of them."""
    assert set(SHELL_IDS) == SPELL_FORMS, (
        f"missing: {sorted(SPELL_FORMS - set(SHELL_IDS))}, "
        f"unexpected: {sorted(set(SHELL_IDS) - SPELL_FORMS)}"
    )


@pytest.mark.parametrize("shell", SHELL_IDS)
def test_a_shell_keeps_its_half_of_the_contract(shell):
    """What familiar.js and the state machine rely on, checked per creature.

    A shell that loads but has no .eyelid never blinks; one with no #mouth-open
    is a familiar that talks without moving. Neither throws, and neither is
    visible in a test that only checks the file parses.
    """
    svg = (STATIC / "shells" / shell / "shell.svg").read_text()
    assert re.search(r'<svg[^>]*id="familiar"', svg), "the root must be #familiar"
    assert re.search(r'<svg[^>]*viewBox=', svg), "the root needs a viewBox"
    for hook in ('class="eyelid"', 'id="closed-eyes"', 'class="pupil"',
                 'id="sparkles"', 'id="zzz"'):
        assert hook in svg, f"{shell} is missing {hook}"

    # A speaking creature needs something that opens, but not a particular
    # name for it: the owl has a #beak-open where the cat has a #mouth-open,
    # and insisting on one word would be the chrome telling a shell what its
    # own anatomy is called. What matters is that the pair exists.
    opens = set(re.findall(r'id="([a-z-]+)-open"', svg))
    closes = set(re.findall(r'id="([a-z-]+)-closed"', svg))
    assert opens & closes, f"{shell} has nothing that opens when it speaks"

    css = (STATIC / "shells" / shell / "shell.css").read_text()
    for state in ("asleep", "listening", "thinking", "speaking"):
        assert f'body[data-state="{state}"]' in css, (
            f"{shell} has nothing to say in the {state} state"
        )


@pytest.mark.parametrize("shell", SHELL_IDS)
def test_a_shell_is_right_opened_on_its_own(shell):
    """The rest style, and the reason it is inside the drawing.

    The shape picker previews every creature with <img src=".../shell.svg">,
    which loads the file and none of the CSS beside it. Without these defaults
    a preview renders with its closed eyes drawn over its open ones, its mouth
    hanging open and its sleeping z's in the air -- fifteen familiars all
    apparently unconscious. It is also what makes the file correct opened in a
    browser tab, which is how anybody would check a drawing.
    """
    svg = (STATIC / "shells" / shell / "shell.svg").read_text()
    style = re.search(r"<style>(.*?)</style>", svg, re.DOTALL)
    assert style, f"{shell} has no resting style, so it previews asleep"
    body = style.group(1)
    assert "scaleY(0)" in body, "eyelids must start open"
    for hidden in ("#closed-eyes", "#sparkles", "#zzz"):
        assert hidden in body, f"{hidden} must start hidden"


@pytest.mark.parametrize("shell", SHELL_IDS)
def test_a_shell_is_a_real_svg_document_on_its_own(shell):
    """Well-formed is not enough: the root has to be in the SVG namespace.

    This is the bug that shipped fifteen blank previews. familiar.js mounts a
    shell with `innerHTML`, and the *HTML* parser adopts a bare <svg> into the
    SVG namespace for free -- so the stage worked and nothing looked wrong. The
    shape picker loads the same file through <img src>, which is parsed as
    *XML*, where an element with no namespace is not SVG and renders nothing at
    all.

    The first version of this test called ET.fromstring() and passed, because a
    namespace-less document is perfectly good XML. Checking the root's tag is
    what actually asks the question.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring((STATIC / "shells" / shell / "shell.svg").read_text())
    assert root.tag == "{http://www.w3.org/2000/svg}svg", (
        f"{shell}/shell.svg has no xmlns, so it renders only when inlined "
        f"into HTML -- never as an <img> (root tag was {root.tag!r})"
    )


@pytest.mark.parametrize("shell", SHELL_IDS)
def test_a_shells_animations_aim_at_something_that_exists(shell):
    """Every id and class the stylesheet targets is in the drawing beside it.

    This is the failure mode a shell has that nothing else catches. A rule
    aimed at #wing-left when the file calls it #wings does not throw, does not
    warn, and does not show up in any check that the files parse -- it is
    simply a creature that never moves, discovered weeks later by somebody
    wondering why the crab is the only one that does not react.

    It found four real ones on the way in: the side-on shells carried a
    blink-offset rule for a second eye they do not have.
    """
    directory = STATIC / "shells" / shell
    svg = (directory / "shell.svg").read_text()
    css = re.sub(r"/\*.*?\*/", "", (directory / "shell.css").read_text(), flags=re.DOTALL)
    # Keyframe bodies name percentages, not elements.
    css = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)

    present_ids = set(re.findall(r'id="([\w-]+)"', svg))
    present_classes = set()
    for value in re.findall(r'class="([^"]+)"', svg):
        present_classes.update(value.split())

    # A selector starts with a letter. `0.7s` and `1.5` are durations.
    wanted_ids = set(re.findall(r"#([A-Za-z][\w-]*)", css))
    wanted_classes = set(re.findall(r"\.([A-Za-z][\w-]*)", css))

    assert not wanted_ids - present_ids, (
        f"{shell}.css styles ids that are not in the drawing: "
        f"{sorted(wanted_ids - present_ids)}"
    )
    assert not wanted_classes - present_classes, (
        f"{shell}.css styles classes that are not in the drawing: "
        f"{sorted(wanted_classes - present_classes)}"
    )


@pytest.mark.parametrize("shell", SHELL_IDS)
def test_no_animated_part_carries_its_own_transform(shell):
    """A mirrored limb must not be the same element the stylesheet animates.

    Half the creatures are drawn once and mirrored with
    transform="translate(400,0) scale(-1,1)" as a presentation attribute. CSS
    `transform` overrides a presentation attribute outright -- so the moment a
    state rule fires, the mirror is discarded and the limb jumps to its
    unmirrored position, which is most of the way across the canvas. Asleep and
    awake then sit at opposite ends of a transition, so the part visibly flies
    off and back.

    The fix is a wrapper: the mirror goes on a group with no id and no class,
    which the stylesheet has no way to select, and the animated group sits
    inside it. This checks the two never merge back together.
    """
    directory = STATIC / "shells" / shell
    svg = (directory / "shell.svg").read_text()
    css = re.sub(r"/\*.*?\*/", "", (directory / "shell.css").read_text(), flags=re.DOTALL)

    moved = set()
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"(?<!-)\btransform:", body) or "animation:" in body:
            moved.update(re.findall(r"[#.][\w-]+", selectors))

    for tag in re.findall(r"<g\b[^>]*>", svg):
        if "transform=" not in tag:
            continue
        names = set()
        found = re.search(r'id="([\w-]+)"', tag)
        if found:
            names.add("#" + found.group(1))
        for value in re.findall(r'class="([^"]+)"', tag):
            names.update("." + name for name in value.split())
        clash = names & moved
        assert not clash, (
            f"{shell}: {sorted(clash)} carries a transform attribute and is also "
            f"animated -- the attribute will be discarded and the part will jump"
        )


@pytest.mark.parametrize("shell", SHELL_IDS)
def test_a_pixel_pivot_says_which_box_its_pixels_are_in(shell):
    """`transform-origin: 250px 92px` means nothing on its own.

    Pixels are measured against the transform-box, and the two answers are a
    long way apart: under `view-box` they are canvas coordinates, under
    `fill-box` they are an offset into the element's own bounding box. The
    owl's right ear tuft is 64px wide and sits at x=228, so the same 250px is
    either its proper pivot or a point at x=478 -- off the side of a 400-wide
    drawing, which throws the tuft off-screen when it rotates.

    `view-box` is the initial value in the current spec and what Chrome and
    Firefox do, so this is invisible there; WebKit has historically resolved
    SVG pivots against the bounding box instead. Saying it costs one
    declaration and removes the question.
    """
    css = re.sub(r"/\*.*?\*/", "",
                 (STATIC / "shells" / shell / "shell.css").read_text(), flags=re.DOTALL)
    bare = [" ".join(selectors.split())
            for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
            if re.search(r"transform-origin:\s*[-\d.]+px", body)
            and "transform-box:" not in body]
    assert not bare, (
        f"{shell}: pixel transform-origin with no transform-box in {bare}"
    )
