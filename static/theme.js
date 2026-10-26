/* Dark table or daylight, and the lamp that switches between them.
 *
 * Two jobs, and the order of them is the whole file. First, before the body is
 * parsed, stamp data-theme on <html> so the correct palette in tokens.css is
 * the *first* thing the page paints -- this script is deliberately loaded in
 * the <head> without defer, because a theme applied after first paint is a
 * white flash on a dark page, once per navigation, forever. Second, after the
 * DOM exists, hang a button somewhere the reader can find it.
 *
 * Every name here is prefixed. Classic <script> tags share one lexical scope,
 * and on the familiar's stage this file sits alongside familiar.js and its
 * forty-odd top-level bindings; a collision there breaks the creature, not the
 * lamp. nav.js carries the same scar and the same prefix.
 *
 * Until somebody presses the button there is no stored choice and the page
 * follows the operating system, changing under it live if the machine does.
 * Pressing it is what makes the choice this reader's, on this device -- which
 * is the same bargain as the voice settings in familiar.js: a table might have
 * a tablet in a dark room and a laptop by a window, and they are allowed to
 * disagree.
 */

const THEME_KEY = "familiar-theme";
const THEME_DARK = "dark";
const THEME_LIGHT = "light";

/* localStorage throws rather than returning null in a Safari private window,
   and reading it is not worth breaking a page over. */
function themeStoredChoice() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return saved === THEME_DARK || saved === THEME_LIGHT ? saved : null;
  } catch (err) {
    return null;
  }
}

function themeRememberChoice(theme) {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch (err) {
    /* The theme still applies for this page; it just will not survive. */
  }
}

/* What the machine is set to. Dark unless it says otherwise: the site's own
   default, and the answer on any browser too old to have an opinion. */
function themeFromSystem() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
    ? THEME_LIGHT
    : THEME_DARK;
}

function themeApply(theme) {
  document.documentElement.dataset.theme = theme;
}

function themeCurrent() {
  return document.documentElement.dataset.theme === THEME_LIGHT ? THEME_LIGHT : THEME_DARK;
}

themeApply(themeStoredChoice() || themeFromSystem());

/* The sun and the moon, drawn rather than typed. The navigation toggle learned
   this the hard way with its hamburger: ☰ and ☼ render at wildly different
   weights across the platforms this runs on, and on two of them a moon glyph
   turns into a colour emoji. Both icons are always in the button and CSS shows
   one, so switching theme costs no DOM work. */
const THEME_ICONS = {
  sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.6v2.6M12 18.8v2.6M2.6 12h2.6M18.8 12h2.6'
    + 'M5.4 5.4l1.8 1.8M16.8 16.8l1.8 1.8M18.6 5.4l-1.8 1.8M7.2 16.8l-1.8 1.8"/>',
  moon: '<path d="M20 14.4A8.6 8.6 0 0 1 9.6 4a8.4 8.4 0 1 0 10.4 10.4z"/>',
};

function themeIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `theme-icon theme-icon-${name}`);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = THEME_ICONS[name];
  return svg;
}

/* The button says where it is going, not where it is: a sun on the dark theme
   means "take me to daylight". The label has to say the same thing out loud,
   because the icon alone is a coin-flip for anyone reading it aloud. */
function themeLabel(button) {
  button.setAttribute(
    "aria-label",
    themeCurrent() === THEME_LIGHT ? "Switch to the dark theme" : "Switch to the daylight theme",
  );
}

/* Most pages get a fixed button in the top-right corner -- the opposite corner
   from the navigation toggle, and out of the way of the stage, which centres a
   creature and must not be given anything in its flow to push around.
   A page with somewhere better to put it says so with #theme-slot -- a page
   with a header row, say, where on a phone a floating button would land on
   top of a link. */
function themeMountToggle() {
  const button = document.createElement("button");
  button.type = "button";
  button.id = "theme-toggle";
  button.className = "theme-toggle";
  button.append(themeIcon("sun"), themeIcon("moon"));
  themeLabel(button);

  button.addEventListener("click", () => {
    const next = themeCurrent() === THEME_LIGHT ? THEME_DARK : THEME_LIGHT;
    themeApply(next);
    themeRememberChoice(next);
    themeLabel(button);
  });

  const slot = document.getElementById("theme-slot");
  if (slot) {
    button.classList.add("in-slot");
    slot.append(button);
  } else {
    document.body.append(button);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", themeMountToggle);
} else {
  themeMountToggle();
}

/* The machine changing its mind. Only heeded while the reader has not made a
   choice of their own -- an explicit press outranks the operating system, and
   goes on outranking it at sunset. */
if (window.matchMedia) {
  const themeSystemQuery = window.matchMedia("(prefers-color-scheme: light)");
  const themeOnSystemChange = () => {
    if (!themeStoredChoice()) {
      themeApply(themeFromSystem());
      const button = document.getElementById("theme-toggle");
      if (button) themeLabel(button);
    }
  };
  if (themeSystemQuery.addEventListener) {
    themeSystemQuery.addEventListener("change", themeOnSystemChange);
  } else if (themeSystemQuery.addListener) {
    themeSystemQuery.addListener(themeOnSystemChange);   // Safari before 14
  }
}
