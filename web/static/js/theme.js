// Appearance: Light / Dark / System (the OS's preference), as data-theme on
// <html>; web/static/css/app.css defines both themes. The user's choice is
// saved on their profile; this browser also remembers the last one, so the
// page is drawn in it before anyone is signed in.

const THEME_KEY = "rag.theme";
const THEMES = ["system", "light", "dark"];

// Light / Dark / System (the OS's preference): the user's choice, saved on
// their profile; this browser also remembers the last one, so the page is
// drawn in it before anyone is signed in.
export function applyTheme(theme) {
  const value = THEMES.includes(theme) ? theme : "system";
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem(THEME_KEY, value); } catch { /* per-browser nicety */ }
}
try { applyTheme(localStorage.getItem(THEME_KEY)); } catch { applyTheme("system"); }
