"""The operator console's design system: tokens, shell, and shared components.

Server-rendered, no build step, no framework -- the whole surface is one stylesheet
plus a handful of pure render helpers, so it deploys with the app and nothing else.

The palette is a calm clinical green on cool neutrals: a tool a front-desk operator
reads at arm's length between phone calls, so legibility and a quiet, trustworthy feel
matter more than flourish. Colour is carried by one committed accent and a dark side
rail; the content surface stays white so dense data reads cleanly. All body text meets
WCAG AA against its background.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from clinic_agent.config import ClinicConfig

# --- design tokens + components -------------------------------------------------
#
# One stylesheet. OKLCH throughout; the primary hue (155) anchors the whole system and
# tints the neutrals a hair toward it so the greys feel of a piece with the brand.

CSS = """
:root {
  color-scheme: light;

  --bg:        oklch(0.985 0.003 165);
  --surface:   oklch(1 0 0);
  --surface-2: oklch(0.975 0.004 165);
  --rail:      oklch(0.24 0.018 165);
  --rail-2:    oklch(0.29 0.02 165);
  --rail-ink:  oklch(0.93 0.008 165);
  --rail-mute: oklch(0.72 0.014 165);

  --ink:       oklch(0.24 0.012 245);
  --ink-2:     oklch(0.44 0.012 245);
  --ink-3:     oklch(0.52 0.011 245);

  --line:      oklch(0.918 0.005 245);
  --line-2:    oklch(0.86 0.006 245);

  --primary:       oklch(0.505 0.115 155);
  --primary-hover: oklch(0.44 0.115 155);
  --primary-ink:   oklch(0.99 0.01 155);
  --primary-weak:  oklch(0.955 0.032 155);
  --primary-weak-ink: oklch(0.36 0.09 155);
  --ring:          oklch(0.55 0.12 155 / 0.45);

  --ok-bg:   oklch(0.945 0.05 150);   --ok-ink:   oklch(0.37 0.09 150);
  --warn-bg: oklch(0.95 0.075 85);    --warn-ink: oklch(0.42 0.085 66);
  --bad-bg:  oklch(0.945 0.045 25);   --bad-ink:  oklch(0.46 0.15 25);
  --info-bg: oklch(0.945 0.04 245);   --info-ink: oklch(0.43 0.1 255);
  --mute-bg: oklch(0.95 0.004 245);   --mute-ink: oklch(0.46 0.01 245);

  --radius:   10px;
  --radius-sm: 7px;
  --shadow:   0 1px 2px oklch(0.24 0.03 245 / 0.06), 0 4px 14px oklch(0.24 0.03 245 / 0.05);
  --rail-w:   240px;
  --ease:     cubic-bezier(0.22, 1, 0.36, 1);

  --z-sticky: 100;
  --z-toast:  400;

  --font: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
}

* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.5;
  color: var(--ink);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
h1, h2, h3 { margin: 0; font-weight: 600; letter-spacing: -0.01em; }

/* --- app shell --------------------------------------------------------------- */
.app { display: grid; grid-template-columns: var(--rail-w) 1fr; min-height: 100vh; }

.rail {
  background: var(--rail);
  color: var(--rail-ink);
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
  padding: 18px 14px;
}
.brand { display: flex; align-items: center; gap: 10px; padding: 6px 8px 18px; }
.brand .mark {
  width: 30px; height: 30px; border-radius: 8px; flex: none;
  background: linear-gradient(150deg, oklch(0.6 0.13 155), oklch(0.46 0.12 155));
  display: grid; place-items: center; color: var(--primary-ink);
  box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.25);
}
.brand .name { font-weight: 600; font-size: 14px; line-height: 1.15; color: #fff; }
.brand .sub { font-size: 11.5px; color: var(--rail-mute); }

.nav { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; }
.nav a {
  display: flex; align-items: center; gap: 11px;
  padding: 9px 10px; border-radius: var(--radius-sm);
  color: var(--rail-mute); font-weight: 500; font-size: 13.5px;
  transition: background 0.15s var(--ease), color 0.15s var(--ease);
}
.nav a:hover { background: var(--rail-2); color: var(--rail-ink); text-decoration: none; }
.nav a.on { background: var(--rail-2); color: #fff; }
.nav a.on svg { color: oklch(0.72 0.12 155); }
.nav svg { width: 17px; height: 17px; flex: none; color: currentColor; }
.nav .count {
  margin-left: auto; font-size: 11px; font-weight: 600;
  background: oklch(0.6 0.13 155); color: #06231a;
  border-radius: 999px; padding: 1px 7px; min-width: 20px; text-align: center;
}

.rail-foot { margin-top: auto; padding-top: 14px; display: flex; flex-direction: column; gap: 10px; }
.rail-status { font-size: 11.5px; color: var(--rail-mute); padding: 0 8px; display: flex; align-items: center; gap: 7px; }
.dot { width: 7px; height: 7px; border-radius: 999px; flex: none; }
.dot.live { background: oklch(0.72 0.14 150); }
.dot.dry  { background: oklch(0.78 0.13 85); }
.dot.off  { background: var(--rail-mute); }
.rail-foot form { margin: 0; }
.logout {
  width: 100%; display: flex; align-items: center; gap: 9px;
  background: transparent; border: 1px solid oklch(1 0 0 / 0.14); color: var(--rail-mute);
  padding: 8px 10px; border-radius: var(--radius-sm); font: inherit; font-size: 13px;
  cursor: pointer; transition: border-color 0.15s var(--ease), color 0.15s var(--ease);
}
.logout:hover { color: var(--rail-ink); border-color: oklch(1 0 0 / 0.3); }
.logout svg { width: 16px; height: 16px; }

/* --- main -------------------------------------------------------------------- */
.main { min-width: 0; display: flex; flex-direction: column; }
.topbar {
  position: sticky; top: 0; z-index: var(--z-sticky);
  background: oklch(0.985 0.003 165 / 0.85); backdrop-filter: saturate(1.4) blur(8px);
  border-bottom: 1px solid var(--line);
  padding: 16px 26px; display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
}
.topbar h1 { font-size: 18px; }
.topbar .lead { color: var(--ink-3); font-size: 13px; }
.content { padding: 22px 26px 40px; max-width: 1160px; width: 100%; }

.banner {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 11px 14px; border-radius: var(--radius); font-size: 13px;
  background: var(--warn-bg); color: var(--warn-ink); border: 1px solid oklch(0.85 0.08 80);
  margin-bottom: 20px;
}
.banner svg { width: 17px; height: 17px; flex: none; margin-top: 1px; }
.banner code { background: oklch(1 0 0 / 0.5); padding: 0 4px; border-radius: 4px; font-size: 12px; }

.section-head { display: flex; align-items: baseline; justify-content: space-between; margin: 4px 0 12px; gap: 12px; }
.section-head h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-3); font-weight: 600; }
.section-head .meta { font-size: 12.5px; color: var(--ink-3); }

/* --- table ------------------------------------------------------------------- */
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
/* Wide data tables stay reachable on narrow screens by scrolling horizontally rather
   than being clipped by the card. */
.card:has(> table) { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; min-width: 640px; }
thead th {
  text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--ink-3); font-weight: 600; padding: 11px 14px;
  background: var(--surface-2); border-bottom: 1px solid var(--line);
  position: sticky; top: 0;
}
tbody td { padding: 11px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
tbody tr { transition: background 0.12s var(--ease); }
tbody tr:hover { background: var(--surface-2); }
td.num { text-align: right; }
.mono { font-variant-numeric: tabular-nums; white-space: nowrap; }
.subtle { color: var(--ink-3); }

/* --- badges ------------------------------------------------------------------ */
.tag {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600;
  white-space: nowrap; line-height: 1.5;
}
.tag::before { content: ""; width: 5px; height: 5px; border-radius: 999px; background: currentColor; opacity: 0.85; }
.tag.ok   { background: var(--ok-bg);   color: var(--ok-ink); }
.tag.warn { background: var(--warn-bg); color: var(--warn-ink); }
.tag.bad  { background: var(--bad-bg);  color: var(--bad-ink); }
.tag.info { background: var(--info-bg); color: var(--info-ink); }
.tag.mute { background: var(--mute-bg); color: var(--mute-ink); }

/* --- transcript -------------------------------------------------------------- */
.transcript { margin: 0; font-size: 13px; display: flex; flex-direction: column; gap: 3px; max-width: 62ch; }
.transcript .who {
  display: inline-block; min-width: 46px; padding-right: 10px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-3);
}
.transcript .agent .who { color: var(--primary); }

/* --- empty state ------------------------------------------------------------- */
.empty { padding: 46px 24px; text-align: center; color: var(--ink-3); }
.empty .ic { width: 40px; height: 40px; margin: 0 auto 12px; color: var(--line-2); }
.empty h3 { font-size: 15px; color: var(--ink-2); margin-bottom: 4px; }
.empty p { margin: 0 auto; max-width: 40ch; font-size: 13px; }

/* --- buttons + fields -------------------------------------------------------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  font: inherit; font-weight: 600; font-size: 13.5px; cursor: pointer;
  padding: 9px 16px; border-radius: var(--radius-sm); border: 1px solid transparent;
  transition: background 0.15s var(--ease), border-color 0.15s var(--ease), transform 0.05s var(--ease);
}
.btn:active { transform: translateY(1px); }
.btn-primary { background: var(--primary); color: var(--primary-ink); }
.btn-primary:hover { background: var(--primary-hover); }
.btn-primary:disabled { background: var(--mute-ink); opacity: 0.5; cursor: not-allowed; }
.btn-ghost { background: var(--surface); color: var(--ink-2); border-color: var(--line-2); }
.btn-ghost:hover { background: var(--surface-2); }
.btn svg { width: 16px; height: 16px; }

.field { display: block; width: 100%; }
label.lbl { display: block; font-size: 12.5px; font-weight: 600; color: var(--ink-2); margin-bottom: 6px; }
input[type=text], input[type=password], input[type=tel], textarea {
  width: 100%; font: inherit; color: var(--ink);
  background: var(--surface); border: 1px solid var(--line-2); border-radius: var(--radius-sm);
  padding: 10px 12px; transition: border-color 0.15s var(--ease), box-shadow 0.15s var(--ease);
}
input::placeholder, textarea::placeholder { color: var(--ink-3); }
input:focus, textarea:focus, .btn:focus-visible, .nav a:focus-visible, .logout:focus-visible {
  outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px var(--ring);
}
textarea { resize: vertical; min-height: 76px; line-height: 1.45; }

/* --- inbox ------------------------------------------------------------------- */
.inbox { display: grid; grid-template-columns: 320px 1fr; gap: 18px; align-items: start; }
.threads { display: flex; flex-direction: column; }
.threads .card { padding: 0; }
.thread-row {
  display: flex; gap: 11px; padding: 12px 14px; border-bottom: 1px solid var(--line);
  transition: background 0.12s var(--ease); align-items: flex-start;
}
.thread-row:last-child { border-bottom: none; }
.thread-row:hover { background: var(--surface-2); text-decoration: none; }
.thread-row.on { background: var(--primary-weak); }
.avatar {
  width: 34px; height: 34px; border-radius: 999px; flex: none; display: grid; place-items: center;
  background: var(--primary-weak); color: var(--primary-weak-ink); font-weight: 600; font-size: 13px;
}
.thread-row .who2 { min-width: 0; flex: 1; }
.thread-row .nm { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.thread-row .nm b { font-size: 13.5px; color: var(--ink); font-weight: 600; }
.thread-row .nm time { font-size: 11px; color: var(--ink-3); flex: none; }
.thread-row .prev { font-size: 12.5px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.thread-row .prev .you { color: var(--ink-2); font-weight: 500; }

.convo { display: flex; flex-direction: column; min-height: 60vh; }
.convo-head { display: flex; align-items: center; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--line); }
.convo-head b { font-size: 14.5px; }
.convo-head .num { font-size: 12.5px; color: var(--ink-3); }
.stream { padding: 20px 18px; display: flex; flex-direction: column; gap: 10px; }
.bubble { max-width: 74%; padding: 9px 13px; border-radius: 14px; font-size: 13.5px; line-height: 1.45; position: relative; }
.bubble .stamp { display: block; font-size: 10.5px; margin-top: 4px; opacity: 0.7; }
.bubble.in  { align-self: flex-start; background: var(--surface-2); border: 1px solid var(--line); border-bottom-left-radius: 5px; color: var(--ink); }
.bubble.out { align-self: flex-end; background: var(--primary); color: var(--primary-ink); border-bottom-right-radius: 5px; }
.bubble.out.reminder { background: oklch(0.5 0.055 245); }
.bubble.out.failed  { background: var(--bad-bg); color: var(--bad-ink); border: 1px solid oklch(0.82 0.09 25); }
.bubble.out.blocked { background: var(--mute-bg); color: var(--mute-ink); border: 1px dashed var(--line-2); }
.kindtag { font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.8; margin-right: 6px; }
.composer { margin-top: auto; padding: 14px 18px; border-top: 1px solid var(--line); background: var(--surface-2); }
.composer form { display: flex; gap: 10px; align-items: flex-end; }
.composer .grow { flex: 1; }
.composer textarea { background: var(--surface); min-height: 42px; }
.notice { padding: 10px 14px; border-radius: var(--radius-sm); font-size: 13px; margin: 0 18px 14px; }
.notice.ok  { background: var(--ok-bg);  color: var(--ok-ink); }
.notice.bad { background: var(--bad-bg); color: var(--bad-ink); }
.optout-bar { padding: 10px 18px; background: var(--bad-bg); color: var(--bad-ink); font-size: 12.5px; display: flex; align-items: center; gap: 8px; }
.optout-bar svg, .notice svg, .convo-head svg { width: 16px; height: 16px; flex: none; }

/* --- test console ------------------------------------------------------------ */
.test { display: grid; grid-template-columns: 320px 1fr; gap: 18px; align-items: start; }
.test-panel { display: flex; flex-direction: column; align-items: center; gap: 16px; padding: 28px 22px; text-align: center; }
.talk-btn {
  width: 108px; height: 108px; border-radius: 999px; border: none; cursor: pointer;
  background: var(--primary); color: var(--primary-ink); display: grid; place-items: center;
  box-shadow: 0 8px 24px oklch(0.5 0.115 155 / 0.35); transition: background 0.15s var(--ease), transform 0.08s var(--ease);
}
.talk-btn:hover { background: var(--primary-hover); }
.talk-btn:active { transform: scale(0.97); }
.talk-btn svg { width: 40px; height: 40px; }
.talk-btn:disabled { background: var(--mute-ink); opacity: 0.5; cursor: not-allowed; box-shadow: none; }
.talk-btn.live { background: var(--bad-ink); box-shadow: 0 0 0 0 oklch(0.55 0.2 25 / 0.5); animation: pulse 1.6s var(--ease) infinite; }
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 oklch(0.55 0.2 25 / 0.45); }
  70% { box-shadow: 0 0 0 22px oklch(0.55 0.2 25 / 0); }
  100% { box-shadow: 0 0 0 0 oklch(0.55 0.2 25 / 0); }
}
.test-status { font-size: 14px; font-weight: 600; color: var(--ink-2); }
.test-hint { font-size: 12.5px; color: var(--ink-3); max-width: 34ch; }
.level { width: 180px; height: 6px; border-radius: 999px; background: var(--line); overflow: hidden; }
.level > i { display: block; height: 100%; width: 0%; background: var(--primary); transition: width 0.08s linear; }
.test-convo { min-height: 60vh; }
.test-convo .stream { min-height: 46vh; }

/* --- login ------------------------------------------------------------------- */
.login-wrap { min-height: 100vh; display: grid; place-items: center; padding: 24px;
  background:
    radial-gradient(120% 90% at 50% -10%, oklch(0.955 0.03 155) 0%, transparent 55%),
    var(--bg); }
.login-card { width: 100%; max-width: 380px; }
.login-card .card { padding: 30px 28px; }
.login-brand { display: flex; flex-direction: column; align-items: center; gap: 14px; margin-bottom: 22px; }
.login-brand .mark { width: 46px; height: 46px; border-radius: 12px;
  background: linear-gradient(150deg, oklch(0.6 0.13 155), oklch(0.46 0.12 155));
  display: grid; place-items: center; color: var(--primary-ink);
  box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.25), var(--shadow); }
.login-brand h1 { font-size: 19px; text-align: center; }
.login-brand p { margin: 0; font-size: 13px; color: var(--ink-3); text-align: center; }
.login-card .btn-primary { width: 100%; margin-top: 4px; }
.login-err { background: var(--bad-bg); color: var(--bad-ink); font-size: 13px; padding: 9px 12px; border-radius: var(--radius-sm); margin-bottom: 16px; }
.login-foot { text-align: center; font-size: 11.5px; color: var(--ink-3); margin-top: 18px; }

/* --- responsive -------------------------------------------------------------- */
@media (max-width: 900px) {
  .inbox { grid-template-columns: 1fr; }
}
@media (max-width: 820px) {
  .app { grid-template-columns: 1fr; }
  .rail {
    position: static; height: auto; flex-direction: row; align-items: center;
    padding: 10px 14px; gap: 4px; overflow-x: auto;
  }
  .brand { padding: 0 12px 0 4px; }
  .brand .sub { display: none; }
  .nav { flex-direction: row; margin: 0; }
  .nav a { padding: 8px 11px; }
  .nav a span.t { display: none; }
  .nav .count { margin-left: 4px; }
  .rail-foot { margin: 0 0 0 auto; flex-direction: row; align-items: center; padding: 0; }
  .rail-status { display: none; }
  .logout span { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; scroll-behavior: auto !important; }
}
"""

# Inline line-icons (stroke = currentColor). Kept tiny and consistent.
_ICONS = {
    "inbox": '<path d="M3 12h5l2 3h4l2-3h5"/><path d="M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z"/>',
    "phone": '<path d="M4 5c0 8 7 15 15 15l-1-4-4-1-2 2a12 12 0 0 1-5-5l2-2-1-4z"/>',
    "calendar": '<rect x="3" y="4.5" width="18" height="16" rx="2"/><path d="M3 9h18M8 3v4M16 3v4"/>',
    "bell": '<path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6z"/><path d="M10.5 20a1.5 1.5 0 0 0 3 0"/>',
    "logout": '<path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4"/><path d="M16 15l3-3-3-3M9 12h10"/>',
    "send": '<path d="M4 12l16-8-6 16-3-6-7-2z"/>',
    "alert": '<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
    "chat-empty": '<path d="M4 5h16a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H9l-4 4v-4H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3.5M8.5 21.5h7"/>',
    "stop": '<rect x="7" y="7" width="10" height="10" rx="2"/>',
}

_NAV = [
    ("inbox", "/dashboard/inbox", "Inbox", "inbox"),
    ("calls", "/dashboard", "Calls", "phone"),
    ("appointments", "/dashboard/appointments", "Appointments", "calendar"),
    ("reminders", "/dashboard/reminders", "Reminders", "bell"),
    ("test", "/dashboard/test", "Test agent", "mic"),
]


def icon(name: str, *, cls: str = "") -> str:
    body = _ICONS.get(name, "")
    c = f' class="{cls}"' if cls else ""
    return (
        f'<svg{c} viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{body}</svg>'
    )


def tag(value: object, css_class: str) -> str:
    text = html.escape(str(value if value not in (None, "") else "-")).replace("_", " ")
    return f'<span class="tag {css_class}">{text}</span>'


def local(moment: datetime | None, cfg: ClinicConfig, *, fmt: str = "%a %d %b, %H:%M") -> str:
    if moment is None:
        return "-"
    return moment.astimezone(cfg.tz).strftime(fmt)


def _reminder_state(cfg: ClinicConfig) -> tuple[str, str]:
    if not cfg.reminders.enabled:
        return "off", "Reminders off"
    if cfg.reminders.dry_run:
        return "dry", "Reminders: dry-run"
    return "live", "Reminders live"


def _banner(cfg: ClinicConfig) -> str:
    if not cfg.reminders.enabled:
        msg = ("Reminders are switched off in config, so the agent does not promise them. "
               "Turn on <code>reminders.enabled</code> once 10DLC registration is approved.")
    elif cfg.reminders.dry_run:
        msg = ("Reminders are in <strong>dry-run</strong>: message bodies are recorded, "
               "nothing is actually sent.")
    else:
        return ""
    return f'<div class="banner">{icon("alert")}<div>{msg}</div></div>'


def shell(
    title: str,
    active: str,
    cfg: ClinicConfig,
    body: str,
    *,
    lead: str = "",
    inbox_count: int = 0,
    full_bleed: bool = False,
) -> str:
    """The authenticated app frame: side rail, top bar, and content."""
    name = html.escape(cfg.clinic.name)

    links = []
    for key, href, label, ic in _NAV:
        badge = ""
        if key == "inbox" and inbox_count:
            badge = f'<span class="count">{inbox_count}</span>'
        on = " on" if key == active else ""
        links.append(
            f'<a href="{href}" class="{on.strip()}"{" aria-current=page" if on else ""}>'
            f'{icon(ic)}<span class="t">{label}</span>{badge}</a>'
        )

    dot, status_text = _reminder_state(cfg)
    lead_html = f'<span class="lead">{html.escape(lead)}</span>' if lead else ""
    banner = _banner(cfg)
    content_cls = "content" + (" full" if full_bleed else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)} · {name}</title>
<style>{CSS}</style></head>
<body>
<div class="app">
  <aside class="rail">
    <div class="brand">
      <div class="mark">{icon("inbox")}</div>
      <div><div class="name">{name}</div><div class="sub">Operator console</div></div>
    </div>
    <nav class="nav" aria-label="Sections">{''.join(links)}</nav>
    <div class="rail-foot">
      <div class="rail-status"><span class="dot {dot}"></span>{html.escape(status_text)}</div>
      <form method="post" action="/logout">
        <button class="logout" type="submit">{icon("logout")}<span>Sign out</span></button>
      </form>
    </div>
  </aside>
  <div class="main">
    <div class="topbar"><h1>{html.escape(title)}</h1>{lead_html}</div>
    <div class="{content_cls}">{banner}{body}</div>
  </div>
</div>
</body></html>"""


def empty_state(ic: str, heading: str, message: str) -> str:
    return (
        f'<div class="empty"><div class="ic">{icon(ic)}</div>'
        f'<h3>{html.escape(heading)}</h3><p>{html.escape(message)}</p></div>'
    )


def now_utc() -> datetime:
    return datetime.now(UTC)
